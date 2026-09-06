#!/usr/bin/env python3
"""Cascade observer — attach, live-trace a run, and PLAY what it says.

Scaffolded with `jaato-scaffold new observer`, then edited: the cascade
id comes from argv or the file `run_cascade.py` publishes, the trace
includes `tool.output` so audio delivery is visible, and model speech is
handed to a player as it arrives.

WHAT THIS FILE IS FOR
Reading it should teach ONE thing: how a process that created nothing
attaches to a cascade and consumes its events.  So the speaker driving
lives in `pulse_playback.py`, which knows nothing about jaato — it takes
a mime type and bytes.  What is left here is the SDK: connect, declare
the protocol you need, iterate `cascade_events`, and tell model speech
from a tool's bytes with `ToolOutputEvent.is_model_speech()`.

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
import sys
from pathlib import Path

from jaato_sdk import (MODEL_MEDIA_CALL_ID, ClientType, EventType,
                       IPCClient)
from jaato_sdk.events import ToolOutputEvent

from pulse_playback import PulsePlayer

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
