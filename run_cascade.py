#!/usr/bin/env python3
"""Cascade driver — asks a text question, receives a spoken answer.

Scaffolded with `jaato-scaffold new cascade`, then edited: the WORKLIST
points at a PROFILE (not inline model/provider), and the driver captures
the model's speech.

WHAT THIS DEMONSTRATES

The `speaker` profile declares a tier with ``modalities: {audio:
outbound}``.  Entering that tier is what puts ``modalities:
["text","audio"]`` on the request — this driver never asks for audio, the
tier does.  The bytes come back as ``ToolOutputEvent`` carrying
``mime_type``/``data_b64`` under the reserved ``call_id`` "model-output",
which is how model-generated media is told apart from a tool's.

Usage:
  python run_cascade.py [cascade_id]

To hear it live, start the observer FIRST on the same id -- a run is
shorter than an observer takes to start:

  CID=$(python -c "import uuid;print(uuid.uuid4().hex)")
  python run_observer.py $CID &
  python run_cascade.py  $CID

Preflight:
  jaato-doctor --workspace . --env-file .env
"""
import argparse
import asyncio
import base64
import sys
import uuid
import wave
from pathlib import Path

from jaato_sdk import ClientType, EventType, IPCClient, SessionCreateFailed

HERE = Path(__file__).resolve().parent
ENV_FILE = str(HERE / ".env")
WORKSPACE = str(HERE)

#: The daemon carrying the media-delivery code.  NOT the default
#: /tmp/jaato.sock — a daemon without that branch accepts the profile and
#: then silently delivers no audio, which looks like a model problem.
SOCKET = "/tmp/jaato-audio.sock"

#: Where the cascade id is published for `run_observer.py`.  A file
#: rather than an import: the two scripts share an id, not a module, so
#: either can be rewritten without touching the other.
CASCADE_ID_FILE = HERE / ".jaato" / "last_cascade_id"

OUT_DIR = HERE / "out"

#: Each stage: (profile, agent, prompt).  One stage today; a list so
#: later stages drop in without restructuring.
WORKLIST = [
    ("speaker", "speaker", "What colour is the sky on a clear day?"),
]

#: OpenAI streams audio as headerless pcm16 — 24 kHz mono signed 16-bit
#: little-endian.  Headerless means these cannot be recovered from the
#: payload, so writing a playable WAV means supplying them.
PCM_RATE, PCM_CHANNELS, PCM_WIDTH = 24000, 1, 2

#: The reserved call_id for media the MODEL produced, as opposed to
#: media a tool returned.
MODEL_MEDIA_CALL_ID = "model-output"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Read the cascade id and the question off the command line.

    Accepting the id matters for observation.  A one-stage run finishes
    in about seven seconds -- less time than a second Python process
    needs to boot, connect and send ``cascade.register`` -- so an
    observer started AFTER this driver reliably attaches to a cascade
    that has already ended and sees nothing.  Agreeing the id up front
    lets the observer be listening before the first request goes out;
    `run.sh --observe` does exactly that.
    """
    parser = argparse.ArgumentParser(
        description="Ask a question and receive a spoken answer.")
    parser.add_argument(
        "cascade_id", nargs="?", default=None,
        help="cascade id to run under (default: a fresh one)")
    parser.add_argument(
        "-p", "--prompt", default=None,
        help="the question to ask (default: the first stage's own prompt)")
    return parser.parse_args(argv)


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


class SpeechCollector:
    """Reassembles model-generated audio from ToolOutputEvent chunks.

    Chunks are keyed by ``stream_id`` because one session may produce
    several utterances, and ordered by ``sequence`` rather than by
    arrival: the framework's per-client queue may evict media under
    backpressure, so a gap is possible, and sorting surfaces it instead
    of silently splicing the audio.
    """

    def __init__(self) -> None:
        self._chunks: dict[str, list[tuple[int, bytes]]] = {}

    def offer(self, ev) -> None:
        """Take one event; ignore anything that is not model speech."""
        if not (ev.mime_type and ev.data_b64):
            return
        if ev.call_id != MODEL_MEDIA_CALL_ID:
            return          # a tool's bytes, not the model's voice
        seq = ev.sequence if ev.sequence is not None else 0
        self._chunks.setdefault(ev.stream_id, []).append(
            (seq, base64.b64decode(ev.data_b64)))

    def write_wav(self, path: Path) -> tuple[int, float, list[int]]:
        """Write every stream to one WAV; return (bytes, seconds, gaps)."""
        raw = b""
        gaps: list[int] = []
        for stream_id in sorted(self._chunks):
            ordered = sorted(self._chunks[stream_id])
            got = [s for s, _ in ordered]
            expected = list(range(len(ordered)))
            if got != expected:
                gaps.extend(sorted(set(expected) - set(got)))
            raw += b"".join(data for _, data in ordered)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(PCM_CHANNELS)
            w.setsampwidth(PCM_WIDTH)
            w.setframerate(PCM_RATE)
            w.writeframes(raw)
        seconds = len(raw) / float(PCM_RATE * PCM_CHANNELS * PCM_WIDTH)
        return len(raw), seconds, gaps

    def __bool__(self) -> bool:
        return bool(self._chunks)


async def _run_stage(client, cascade_id, profile, agent, prompt, speech) -> str:
    """Run one stage to terminal completion; return its reason.

    The stage is COMPLETION-GATED: its profile declares a
    ``completion_payload_schema``, which enables ``signal_completion``,
    and calling that is what emits SESSION_TERMINATED.  A non-gated stage
    would end its turn without terminating and this wait would hang.
    """
    done = asyncio.Event()
    outcome: dict = {}

    def on_done(ev):
        outcome["reason"] = getattr(ev, "reason", None)
        outcome["error_summary"] = getattr(ev, "error_summary", None)
        done.set()

    unsubscribe = client.subscribe(EventType.TOOL_OUTPUT, speech.offer)
    client.subscribe_once(EventType.SESSION_TERMINATED, on_done)
    try:
        try:
            await client.create_session(
                profile=profile, agent=agent,
                cascade_driver_id=cascade_id,   # shared slot → warm imports
                timeout=60.0)
        except SessionCreateFailed as exc:
            # A refused stage is a TYPED outcome, not a timeout — an
            # exhausted budget ceiling means nothing ran, which is not
            # the same as a stage that ran and failed.
            return f"spawn_refused: {exc}"
        await client.send_message(prompt)
        await done.wait()
    finally:
        unsubscribe()
    return outcome.get("reason") or "unknown"


async def main() -> int:
    """Fire the worklist, then write whatever the model said."""
    args = _parse_args(sys.argv[1:])
    client = _new_client()
    if not await client.connect(timeout=120.0):
        print("could not connect/autostart the daemon — run jaato-doctor")
        return 1

    cascade_id = args.cascade_id or uuid.uuid4().hex
    CASCADE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    CASCADE_ID_FILE.write_text(cascade_id)
    print(f"cascade {cascade_id}", flush=True)

    speech = SpeechCollector()
    failed = False
    # ``--prompt`` replaces the FIRST stage's question only.  A later
    # stage's prompt is the cascade's own wiring -- what stage 2 asks
    # depends on what stage 1 produced -- so it is not the caller's to
    # set from a flag.
    worklist = list(WORKLIST)
    if args.prompt:
        profile, agent, _ = worklist[0]
        worklist[0] = (profile, agent, args.prompt)
    for i, (profile, agent, prompt) in enumerate(worklist, 1):
        reason = await _run_stage(
            client, cascade_id, profile, agent, prompt, speech)
        print(f"stage {i} [{profile}]: {reason}")
        if reason == "error":
            failed = True
            break
    await client.disconnect()

    # Report the audio even when a stage failed: partial speech is
    # evidence about what went wrong, and discarding it would throw away
    # the one artifact this cascade exists to produce.
    if speech:
        out = OUT_DIR / "answer.wav"
        size, seconds, gaps = speech.write_wav(out)
        print(f"wrote {out}  —  {size} bytes, {seconds:.2f}s")
        if gaps:
            print(f"  WARNING: missing chunk sequences {gaps} — "
                  f"audio has gaps (queue backpressure?)")
    else:
        print("no audio delivered — is SOCKET the media-capable daemon?")
        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
