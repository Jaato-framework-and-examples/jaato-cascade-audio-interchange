#!/usr/bin/env python3
"""Cascade observer — attach, live-trace a run, and PLAY what it says.

Scaffolded with `jaato-scaffold new observer`, then edited: the cascade
id comes from argv or the file `run_cascade.py` publishes, the trace
includes `tool.output` so audio delivery is visible, and model speech is
piped to PulseAudio as it arrives.

DECOUPLED BY DESIGN
This shares an ID with the driver, not a module.  It never imports
`run_cascade`, never creates a session, and never sends a message, so
the two can be rewritten independently.

Usage:
  python run_observer.py [cascade_id]      # defaults to the last run
  python run_observer.py --no-audio        # trace only, stay silent

Start it BEFORE the driver to hear a run from its first word; started
after, it plays whatever is still in flight.

Preflight:
  jaato-doctor --workspace . --env-file .env
"""
import asyncio
import base64
import shutil
import subprocess
import sys
from pathlib import Path

from jaato_sdk import (MODEL_MEDIA_CALL_ID, ClientType, EventType,
                       IPCClient)
from jaato_sdk.events import ToolOutputEvent

HERE = Path(__file__).resolve().parent
ENV_FILE = str(HERE / ".env")
WORKSPACE = str(HERE)

#: Must be the daemon the driver used — a cascade id means nothing to
#: any other daemon.
SOCKET = "/tmp/jaato-audio.sock"

CASCADE_ID_FILE = HERE / ".jaato" / "last_cascade_id"

#: CLASS names, not wire values.  The daemon filters with
#: ``type(event).__name__ in event_types`` (session_manager.py), so
#: "tool.output" — the EventType VALUE, and what `jaato-scaffold new
#: observer` emits — matches nothing and the observer sits silent
#: forever.  `ToolOutputEvent` is the one carrying model speech.
EVENT_TYPES = [
    "ToolCallStartEvent",
    "ToolOutputEvent",
    "TurnCompletedEvent",
    "AgentCompletedEvent",
    "SessionTerminatedEvent",
]

#: MODEL_MEDIA_CALL_ID comes from the SDK.  It used to be a literal
#: here, which made this file a second home for a value the daemon
#: owns -- the kind of copy that is only discovered when it drifts.

#: Events after which no more speech can arrive for the current
#: utterance, so the players may drain and exit.
_END_OF_SPEECH = frozenset({"TurnCompletedEvent", "SessionTerminatedEvent"})


#: Wire protocol that first carries media on ToolOutputEvent
#: (`mime_type` / `data_b64` / `sequence` / `stream_id` / `final`).
#: Declaring it is what turns "the model never spoke" into a refused
#: connection naming the version: an older daemon simply never sends
#: those fields, which is indistinguishable from a silent model.
MEDIA_PROTOCOL = "1.4"


def _new_client() -> IPCClient:
    """Construct the API client with the known-good knobs."""
    return IPCClient(
        SOCKET,
        client_type=ClientType.API,   # load-bearing: keeps signal_completion
        min_protocol_version=MEDIA_PROTOCOL,
        auto_start=True,
        env_file=ENV_FILE,            # never None (handshake crashes on None)
        workspace_path=WORKSPACE,
    )


def _pcm_params(mime_type: str) -> dict[str, str] | None:
    """Read rate/channels/encoding out of a mime type, or None.

    The framework tags streamed speech as e.g.
    ``audio/pcm;rate=24000;channels=1;encoding=s16le``.  Those parameters
    are spelled out precisely because the payload is HEADERLESS — nothing
    in the bytes says how to play them — so they are parsed here rather
    than assumed.  A mime type this player cannot satisfy returns None
    and is reported instead of being guessed at.
    """
    base, _, params = mime_type.partition(";")
    if base.strip().lower() != "audio/pcm":
        return None
    parsed = {}
    for item in params.split(";"):
        key, _, value = item.partition("=")
        if key.strip():
            parsed[key.strip().lower()] = value.strip()
    if not {"rate", "channels", "encoding"} <= parsed.keys():
        return None
    return parsed


class PulsePlayer:
    """Plays model speech through PulseAudio as chunks arrive.

    One `paplay` per stream_id, fed on stdin, so audio starts before the
    turn ends — the point of observing a run live rather than opening a
    file afterwards.

    Playback failure never stops the trace: this is a read-only observer,
    and losing sound is not a reason to stop reporting events.  It says
    so once, loudly, rather than failing silently.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._enabled = enabled and shutil.which("paplay") is not None
        self._complained = False
        if enabled and not self._enabled:
            print("  (paplay not on PATH — tracing without sound)")

    def feed(self, stream_id: str, mime_type: str, payload: bytes) -> None:
        """Push one chunk into this stream's player, starting it if needed."""
        if not self._enabled:
            return
        proc = self._procs.get(stream_id)
        if proc is None:
            params = _pcm_params(mime_type)
            if params is None:
                self._complain(f"cannot play {mime_type} — not headerless PCM")
                return
            try:
                proc = subprocess.Popen(
                    ["paplay", "--raw",
                     f"--format={params['encoding']}",
                     f"--rate={params['rate']}",
                     f"--channels={params['channels']}"],
                    stdin=subprocess.PIPE)
            except OSError as exc:
                self._complain(f"could not start paplay: {exc}")
                return
            self._procs[stream_id] = proc
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            self._complain(f"playback stream died: {exc}")

    def finish(self, stream_id: str) -> None:
        """Close one stream so paplay drains and exits."""
        proc = self._procs.pop(stream_id, None)
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        proc.wait(timeout=30)

    def finish_all(self) -> None:
        for stream_id in list(self._procs):
            self.finish(stream_id)

    def _complain(self, message: str) -> None:
        if not self._complained:
            print(f"  AUDIO: {message}")
            self._complained = True


def _resolve_cascade_id(args: list[str]) -> str | None:
    """argv wins over the file, so a past run can be re-observed."""
    for arg in args:
        if arg and not arg.startswith("-"):
            return arg.strip()
    if CASCADE_ID_FILE.exists():
        return CASCADE_ID_FILE.read_text().strip() or None
    return None


def _describe(ev) -> str:
    """One line per event, saying what it actually carried.

    A media chunk is summarised, never dumped: the payload is base64
    pcm16 and printing it would bury the trace it belongs to.
    """
    et = getattr(ev, "type", "?")
    et = getattr(et, "value", et)
    # Read session_id as a plain attribute, not getattr(ev, "session_id",
    # ""), which cannot tell an unrouted event from a pre-1.2 server.
    where = f"session={ev.session_id or '<unrouted>'}"

    if et == EventType.TOOL_OUTPUT.value and getattr(ev, "mime_type", None):
        origin = ("MODEL SPEECH" if ev.call_id == MODEL_MEDIA_CALL_ID
                  else f"tool media call_id={ev.call_id}")
        final = " FINAL" if getattr(ev, "final", False) else ""
        return (f"[{et}] {where}  {origin}  seq={ev.sequence} "
                f"{ev.mime_type} {len(ev.data_b64 or '')}B b64{final}")
    return f"[{et}] {where}"


async def main() -> int:
    """Attach to a cascade, trace it, and play what it says."""
    args = sys.argv[1:]
    cascade_id = _resolve_cascade_id(args)
    if not cascade_id:
        print(f"no cascade id — pass one, or run run_cascade.py first "
              f"(it writes {CASCADE_ID_FILE})")
        return 2

    player = PulsePlayer(enabled="--no-audio" not in args)
    client = _new_client()
    if not await client.connect(timeout=120.0):
        print("could not connect/autostart the daemon — run jaato-doctor")
        return 1

    # flush=True is load-bearing: stdout is block-buffered when
    # redirected to a file or pipe, so a launcher waiting for this line
    # to know the observer is attached would wait forever and fall back
    # to sleeping a guessed number of seconds.  `run.sh` waits on it.
    print(f"observing cascade {cascade_id} — Ctrl-C to stop", flush=True)
    chunks = 0
    try:
        async for ev in client.cascade_events(
                cascade_id, event_types=EVENT_TYPES, role="observer"):
            print(_describe(ev), flush=True)
            # isinstance first: this iterator yields EVERY subscribed
            # type, and `is_model_speech` is ToolOutputEvent's alone.
            if isinstance(ev, ToolOutputEvent) and ev.is_model_speech():
                chunks += 1
                player.feed(ev.stream_id, ev.mime_type,
                            base64.b64decode(ev.data_b64))
                if getattr(ev, "final", False):
                    player.finish(ev.stream_id)
            elif type(ev).__name__ in _END_OF_SPEECH:
                # Close the players when the TURN ends, because nothing
                # in the media stream itself says "that was the last
                # chunk": model speech never carries `final=True` today,
                # so waiting for it leaves paplay holding an open stdin
                # forever -- the process never exits, and neither does
                # anything waiting on it.  The turn ending is the signal
                # the model has stopped talking.
                player.finish_all()
    except (KeyboardInterrupt, asyncio.CancelledError):
        # asyncio.run() cancels the pending `q.get()` and re-raises
        # KeyboardInterrupt from the runner, so catching only the latter
        # here let a CancelledError escape and bury the summary under a
        # traceback.  Stopping an observer is a normal way to end it.
        pass
    finally:
        player.finish_all()
        await client.disconnect()

    print(f"observed and played {chunks} model-speech chunk(s)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        # Re-raised by asyncio.run after it cancels the task; the
        # summary has already printed by then.
        sys.exit(0)
