#!/usr/bin/env python3
"""Voice driver — you speak to the agent, it speaks back.

The other direction from `run_cascade.py`.  There, a TEXT question came
back as speech; here the question is speech too, so the framework is
carrying audio both ways in one turn.

WHAT THIS DEMONSTRATES

The `voice` profile declares one tier with ``modalities: {audio:
bidirectional}``.  That single word is doing two separate jobs, and it
is worth not conflating them:

* OUTBOUND is what puts ``modalities: ["text","audio"]`` on the request,
  exactly as the `speaker` scenario does.
* INBOUND is what lets an ``audio/*`` attachment reach the wire as an
  ``input_audio`` block.  Before #830 there was nowhere to send it: the
  clause that correctly withholds video withheld audio too, however
  loudly the catalog declared the model could hear.

This driver never mentions audio in either direction.  It attaches WAV
bytes and reads speech back; the tier is what makes both legal.

The microphone half is `ptt_capture.py`, which knows nothing about
jaato — it turns a push-to-talk key into utterances and stops there.
The join between the two is this file, and it is deliberately thin.

Usage:
  python run_voice.py            # talk until Ctrl-C
  python run_voice.py --once     # one utterance, then exit

Preflight:
  jaato-doctor --workspace . --env-file .env
  pactl list sources short | grep wraith_mic
"""
import argparse
import asyncio
import base64
import os
import queue
import sys
from pathlib import Path

from jaato_sdk import ClientType, IPCClient, SessionCreateFailed
from jaato_sdk.client.convenience import AgentError
from jaato_sdk.client.ipc import DEFAULT_SOCKET_PATH

from ptt_capture import PushToTalkMic, SourceMuted, SignalLost
from pulse_playback import PulsePlayer

HERE = Path(__file__).resolve().parent
ENV_FILE = str(HERE / ".env")
WORKSPACE = str(HERE)
SOCKET = os.environ.get("JAATO_IPC_SOCKET") or DEFAULT_SOCKET_PATH

#: 1.4 carries model media on ``ToolOutputEvent``; see run_cascade.py.
MEDIA_PROTOCOL = "1.4"

#: The mime the framework dispatches on.  ``ptt_capture`` emits RIFF
#: WAV, and the provider maps ``audio/wav`` to the ``input_audio``
#: format token -- a mime it cannot map is withheld rather than sent
#: mislabelled (#829), so this string is load-bearing.
UTTERANCE_MIME = "audio/wav"


def playback_sink(player: PulsePlayer):
    """Adapt ``on_media`` events to the player, one stream at a time.

    The mirror of ``speech_sink`` in run_cascade.py, which collects to a
    file; here the bytes go straight out a speaker, because a
    conversation that arrives as a WAV afterwards is not a conversation.
    """
    def _sink(ev) -> None:
        player.feed(ev.stream_id, ev.mime_type, base64.b64decode(ev.data_b64))
        if ev.final:
            player.finish(ev.stream_id)
    return _sink


async def answer(session, wav: bytes) -> str:
    """Hand one utterance to the model and let it speak the reply.

    The prompt is EMPTY, and that is the point: the question IS the
    attachment.  A text prompt beside it would be a second question the
    persona has to choose between.  This form used to be discarded
    silently while reporting a completed turn (#838); it is the natural
    call and it is now the one made.
    """
    player = PulsePlayer()
    try:
        payload = await session.complete(
            "",
            attachments=[{"mime_type": UTTERANCE_MIME,
                          "data": wav,
                          "display_name": "utterance.wav"}],
            on_media=playback_sink(player),
        )
    finally:
        player.finish_all()
    return (payload or {}).get("spoken", "")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Speak to the agent; it speaks back.")
    parser.add_argument("--once", action="store_true",
                        help="handle one utterance and exit")
    args = parser.parse_args()

    # The mic thread and the asyncio loop are different worlds, so
    # utterances cross on a queue rather than by calling into the loop
    # from a callback.  Bounded, and for the reason ptt_capture bounds
    # its own: a consumer slower than the speaker must refuse work, not
    # accumulate an ever-staler backlog.
    inbox: "queue.Queue[bytes]" = queue.Queue(maxsize=4)

    def on_utterance(u) -> None:
        if not u.complete:
            print("  (press dropped — discarded)", flush=True)
            return
        try:
            inbox.put_nowait(u.wav())
        except queue.Full:
            print("  (still answering — utterance refused)", flush=True)

    try:
        mic = PushToTalkMic(on_utterance=on_utterance)
        mic.start()
    except SourceMuted as exc:
        print(f"microphone: {exc}", file=sys.stderr)
        return 2

    print("listening — hold the push-to-talk key and speak (Ctrl-C to stop)",
          flush=True)
    try:
        async with IPCClient.session(
                socket_path=SOCKET,
                env_file=ENV_FILE,
                workspace_path=WORKSPACE,
                client_type=ClientType.API,   # keeps signal_completion
                min_protocol_version=MEDIA_PROTOCOL,
                connect_timeout=120.0,
                profile="voice",
                agent="voice",
        ) as session:
            while True:
                try:
                    wav = await asyncio.to_thread(inbox.get, True, 0.5)
                except queue.Empty:
                    mic.raise_if_faulted()    # surface a dead half promptly
                    continue
                print(f"  heard {len(wav)} bytes; asking...", flush=True)
                spoken = await answer(session, wav)
                print(f"  said: {spoken or '(nothing)'}", flush=True)
                if args.once:
                    return 0
    except KeyboardInterrupt:
        return 0
    except SignalLost as exc:
        print(f"microphone: {exc}", file=sys.stderr)
        return 2
    except (AgentError, SessionCreateFailed) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        mic.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
