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
import os
import sys
import uuid
from pathlib import Path

from jaato_sdk.client.ipc import DEFAULT_SOCKET_PATH
from jaato_sdk import ClientType, IPCClient, SessionCreateFailed
from jaato_sdk.client.convenience import AgentError

from speech_collector import SpeechCollector

HERE = Path(__file__).resolve().parent
ENV_FILE = str(HERE / ".env")
WORKSPACE = str(HERE)

#: The daemon to talk to.  Defaults to the SDK's own default, so this
#: example works against a stock `jaato-server` with no configuration --
#: and stays correct on Windows, where the default is a named pipe and
#: any hardcoded "/tmp/..." would be wrong.
#:
#: Override with JAATO_IPC_SOCKET (or `run.sh --socket`) when the daemon
#: listens elsewhere -- a second daemon for development, a per-user
#: socket, a container path.  This file used to hardcode the authors'
#: own test socket, which is exactly the kind of local detail an example
#: must not carry.
SOCKET = os.environ.get("JAATO_IPC_SOCKET") or DEFAULT_SOCKET_PATH

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
    parser.add_argument(
        "-s", "--scenario", default=None,
        help="which scenario to run — 'speaker' (one audio model answers "
             "out loud) or 'duet' (a text model decides and completes, an "
             "audio tier only speaks)")
    return parser.parse_args(argv)


#: Wire protocol that first carries media on ToolOutputEvent
#: (`mime_type` / `data_b64` / `sequence` / `stream_id` / `final`).
#: Declaring it is what turns "the model never spoke" into a refused
#: connection naming the version: an older daemon simply never sends
#: those fields, which is indistinguishable from a silent model.
MEDIA_PROTOCOL = "1.4"




def speech_sink(collector):
    """Adapt the SDK's ``on_media`` events to the collector's bytes.

    This is the framework-facing half of collecting speech, and it lives
    HERE rather than in ``speech_collector`` on purpose: unpacking a
    ``ToolOutputEvent`` is knowledge about jaato, and the collector is
    meant to be readable without any.

    No filtering: ``on_media`` delivers the model's own speech and
    nothing else, so re-checking the mime type or call_id would be
    second-guessing a contract the SDK states -- and a second place for
    that rule to live.
    """
    def _sink(ev) -> None:
        collector.add(ev.stream_id, ev.sequence, base64.b64decode(ev.data_b64))
    return _sink


async def _run_stage(cascade_id, profile, agent, prompt, speech) -> tuple:
    """Run one gated stage; return ``(payload, error)``.

    Uses the SDK's session FACADE rather than the event-loop primitives.
    `Session.complete` owns the send-and-wait recipe for a
    COMPLETION-GATED profile: it captures the typed payload, raises on an
    error terminal, and settles when the SESSION does rather than when
    its first turn ends -- which matters here, because this stage is
    routinely nudged and a turn boundary would report success while the
    model still had a tool call to make (jaato #767).

    `on_media` is the same facade's sink for the model's own speech: the
    payload comes back, the audio is handed over as it streams.
    """
    try:
        async with IPCClient.session(
                socket_path=SOCKET,
                env_file=ENV_FILE,
                workspace_path=WORKSPACE,
                client_type=ClientType.API,   # keeps signal_completion
                min_protocol_version=MEDIA_PROTOCOL,
                connect_timeout=120.0,
                profile=profile,
                agent=agent,
                cascade_driver_id=cascade_id,   # shared slot -> warm imports
        ) as session:
            payload = await session.complete(prompt, on_media=speech_sink(speech))
        return payload, None
    except (AgentError, SessionCreateFailed) as exc:
        # A refused or failed stage is a TYPED outcome, not a timeout: an
        # exhausted budget ceiling means nothing ran, which is not the
        # same as a stage that ran and failed.
        return None, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    """Fire the worklist, then write whatever the model said."""
    args = _parse_args(sys.argv[1:])

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
    # ``--profile`` names the stage's profile AND its agent: in this
    # workspace they share a name by construction (speaker/speaker,
    # duet/duet), because a stage's persona and its model binding are two
    # halves of one scenario.  Selecting them separately would let a
    # persona run against a profile it was not written for.
    # A scenario names its profile AND its agent, which share a name here
    # by construction (speaker/speaker, duet/duet): a stage's persona and
    # its model binding are two halves of one scenario, and selecting them
    # separately would let a persona run against a profile it was not
    # written for.
    if args.scenario:
        _, _, prompt = worklist[0]
        worklist[0] = (args.scenario, args.scenario, prompt)
    if args.prompt:
        profile, agent, _ = worklist[0]
        worklist[0] = (profile, agent, args.prompt)

    for i, (profile, agent, prompt) in enumerate(worklist, 1):
        payload, error = await _run_stage(
            cascade_id, profile, agent, prompt, speech)
        if error:
            print(f"stage {i} [{profile}]: {error}")
            failed = True
            break
        # The completion payload is the stage's ANSWER -- the schema asks
        # for `spoken`, so read it.  Reporting only "the session ended"
        # would throw away the transcript the model was made to produce.
        spoken = (payload or {}).get("spoken", "")
        print(f"stage {i} [{profile}]: {spoken or '(no transcript)'}")
        for warning in (payload or {}).get("warnings", []):
            print(f"  warning: {warning}")

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
