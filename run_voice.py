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

One session, many turns.  The `voice` profile declares no completion
schema, so nothing terminates the session between questions and the
model keeps the conversation: a follow-up like "and tomorrow?" resolves
against what was already asked.  A profile that DID complete would end
the session after the first reply, and the next utterance would replay a
history whose tool_calls never got their response -- a 400 from the
upstream, or a hang.

Usage:
  python run_voice.py            # talk until Ctrl-C
  python run_voice.py --once     # one utterance, then exit

  # the helpdesk, with its simulated systems -- start the mock first:
  python mock_helpdesk.py &
  python run_voice.py --scenario helpdesk

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
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from jaato_sdk import ClientType, EventType, IPCClient, SessionCreateFailed
from jaato_sdk.client.convenience import AgentError
from jaato_sdk.client.ipc import DEFAULT_SOCKET_PATH

from ptt_capture import BYTES_PER_SECOND, PushToTalkMic, SourceMuted, SignalLost
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

#: The demos, each naming the three things that must agree.
SCENARIOS = {
    "voice": {"profile": "voice", "agent": "voice", "greet": False},
    "helpdesk": {"profile": "helpdesk", "agent": "helpdesk", "greet": True},
}

#: Repeated with every utterance, because ONE session transcribes the
#: whole call and a session that only ever hears speech starts answering
#: it: by the second turn the listener replied "Claro, entiendo, y
#: lamento lo del golpe" instead of writing down what was said. The
#: persona alone did not hold it; restating the task each time does.
TRANSCRIBE_CUE = "Transcribe literalmente este audio. Solo la transcripción."

#: Reply audio is pcm16 at 24 kHz mono -- the only format OpenAI emits
#: while streaming.  Used to report a spoken reply's LENGTH, since its
#: words do not arrive as text.
SPEECH_BYTES_PER_SECOND = 24000 * 2


def playback_sink(player: PulsePlayer, meter: Dict[str, int],
                  words: Optional[list] = None):
    """Adapt ``on_media`` events to the player, one stream at a time.

    The mirror of ``speech_sink`` in run_cascade.py, which collects to a
    file; here the bytes go straight out a speaker, because a
    conversation that arrives as a WAV afterwards is not a conversation.
    """
    def _sink(ev) -> None:
        payload = base64.b64decode(ev.data_b64)
        meter["bytes"] += len(payload)
        player.feed(ev.stream_id, ev.mime_type, payload)
        # The FINAL chunk of model speech carries the utterance's own
        # transcript (#869) -- the provider's words for the audio it just
        # emitted, not either tier's account of what it meant to say. It
        # is empty when the model also wrote text, so this never
        # duplicates what AGENT_OUTPUT already delivered.
        if ev.final:
            if words is not None and getattr(ev, "chunk", ""):
                words.append(ev.chunk)
            player.finish(ev.stream_id)
    return _sink


#: What opens a call the AGENT speaks first.
#:
#: A stage direction, not a question.  The persona owns the words of the
#: greeting -- who the agent is and how it answers the phone belong to
#: the persona, not to the driver -- and this only tells it that the
#: line is now open.  Square brackets and the third person keep it
#: readable as direction rather than as something to say aloud.
OPENING_CUE = "[La llamada se ha establecido. El cliente está a la escucha.]"


class Transcript:
    """A timestamped record of the call, printed when it ends.

    The per-line output while a call runs is for watching; this is for
    reading afterwards. It answers the questions a trace dump does not:
    when did the caller speak and for how long, what did the agent
    actually consult, how long did each reply take to arrive.

    What it CANNOT hold is the words. A spoken turn returns no text --
    the provider builds its transcript after streaming and no event
    carries it -- and nothing transcribes the caller at all. So this
    records durations, tool calls and whatever text a turn did return,
    and says plainly where words are unavailable rather than leaving a
    blank that reads like silence.
    """

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._rows: list[tuple[float, str, str]] = []

    def add(self, actor: str, detail: str) -> None:
        self._rows.append((time.monotonic() - self._t0, actor, detail))

    def render(self) -> str:
        if not self._rows:
            return "  (nothing happened)"
        width = max(len(a) for _, a, _ in self._rows)
        lines = []
        for at, actor, detail in self._rows:
            stamp = f"{int(at) // 60:02d}:{at % 60:05.2f}"
            first, *rest = detail.splitlines() or [""]
            lines.append(f"  {stamp}  {actor:<{width}}  {first}")
            pad = " " * (10 + width + 2)
            lines.extend(f"{pad}{line}" for line in rest)
        return "\n".join(lines)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# call transcript — {datetime.now().isoformat(timespec='seconds')}\n"
            f"# mm:ss.ss from the start of the call\n\n{self.render()}\n",
            encoding="utf-8")


def trace_tools(session, transcript: "Transcript") -> None:
    """Print every tool call and permission decision as it happens.

    A voice demo is opaque in a way a text one is not: the audience
    hears a sentence and cannot tell whether the agent CONSULTED a
    system or merely said it would.  Those are the two outcomes this
    whole design is trying to keep apart, so the one channel that can
    distinguish them should not be silent.

    It earns its place as a diagnostic too.  A permission prompt nobody
    can answer ends the turn with no error at all, and an announced-
    but-uncalled tool looks identical from outside to a called one --
    both cost a debugging round here before this existed.
    """
    def _start(ev) -> None:
        args = getattr(ev, "arguments", None) or getattr(ev, "args", None) or ""
        name = getattr(ev, "tool_name", "?")
        print(f"    -> {name}({str(args)[:110]})", flush=True)
        transcript.add("system", f"calls {name}({str(args)[:160]})")

    def _end(ev) -> None:
        ok = getattr(ev, "success", None)
        mark = "ok" if ok is None or ok else "FAILED"
        result = str(getattr(ev, "result", "") or "")[:110]
        name = getattr(ev, "tool_name", "?")
        print(f"    <- {name} {mark} {result}", flush=True)
        transcript.add("system", f"{name} returned {mark} {result[:160]}".rstrip())

    def _perm(ev) -> None:
        name = getattr(ev, "tool_name", "?")
        print(f"    !! permission requested for {name} "
              f"— nothing here can answer it", flush=True)
        transcript.add("system", f"PERMISSION REQUESTED for {name} — unanswerable here")

    session._client.subscribe(EventType.TOOL_CALL_START, _start)
    session._client.subscribe(EventType.TOOL_CALL_END, _end)
    session._client.subscribe(EventType.PERMISSION_REQUESTED, _perm)


async def speak(session, prompt: str, wav: Optional[bytes] = None) -> str:
    """Run one turn and play whatever the model says.

    One function for both kinds of turn because they differ only in what
    is handed over: the opening carries a stage direction and no audio,
    every later turn carries audio and no words.

    ``ask`` rather than ``complete``: one call is one TURN, and this
    session runs many of them.  ``complete`` waits for the session to
    terminate, which is right for a one-shot stage and wrong here --
    the `voice` profile declares no completion schema precisely so the
    session survives the reply and remembers it.

    On an audio turn the prompt is EMPTY, and that is the point: the
    question IS the attachment.  A text prompt beside it would be a
    second question the persona has to choose between.  It is also the
    only way in: neither `session.wake` nor `inject_prompt` carries an
    attachment (#845), so audio reaches a live session through
    ``send_message`` or not at all.
    """
    player = PulsePlayer()
    meter = {"bytes": 0}
    words: list = []
    attachments = None if wav is None else [
        {"mime_type": UTTERANCE_MIME, "data": wav,
         "display_name": "utterance.wav"}]
    try:
        text = await session.ask(
            prompt,
            attachments=attachments,
            on_media=playback_sink(player, meter, words),
        )
    finally:
        player.finish_all()
    # A spoken turn usually returns NO text.  The provider builds the
    # transcript and attaches it to the response AFTER streaming
    # (`ensure_spoken_part`), so it reaches history -- the model
    # remembers what it said -- but it is never streamed as output
    # tokens, and no AGENT_OUTPUT event carries it.  Measured: 14 media
    # chunks / 5.45s of speech, one AGENT_OUTPUT, `source='user'`, empty.
    #
    # So the reply is REPORTED, not transcribed.  The listener heard it;
    # inventing a transcript here would mean transcribing our own audio
    # to narrate something the person already has.
    seconds = meter["bytes"] / SPEECH_BYTES_PER_SECOND
    spoken = " ".join(w.strip() for w in words if w.strip())
    if spoken:
        return f"{spoken}   [{seconds:.1f}s]"
    return text.strip() or f"(spoke {seconds:.1f}s)"


async def _nothing() -> str:
    """Stand in for the listener when transcription is off."""
    return ""


@asynccontextmanager
async def _listener_session(disabled: bool, profile: str, agent: str):
    """Open the parallel listener, or yield None when it is not wanted.

    A context manager so the caller's `async with` reads the same either
    way, and so a listener that cannot start is not fatal: the record is
    worth less than the call. The `voice` scenario has no systems to
    consult and its own transcript already; only the helpdesk's caller
    side is missing words, so a scenario without a `listener` profile
    simply runs without one.
    """
    if disabled:
        yield None
        return
    try:
        async with IPCClient.session(
                socket_path=SOCKET, env_file=ENV_FILE,
                workspace_path=WORKSPACE, client_type=ClientType.API,
                min_protocol_version=MEDIA_PROTOCOL, connect_timeout=120.0,
                profile="listener", agent="listener") as listener:
            yield listener
    except (AgentError, SessionCreateFailed, OSError) as exc:
        print(f"  (no transcription of your side: {type(exc).__name__})",
              flush=True)
        yield None


async def hear(listener, wav: bytes) -> str:
    """Transcribe one caller utterance, on a session of its own.

    Runs BESIDE the answering turn, not after it. The agent spends
    seconds thinking and speaking; the transcription of what was just
    said fits inside that window, so a two-sided record costs the call
    no extra time -- only the tokens.

    A separate session, not a tier: tiers take turns within one
    conversation, and this must happen SIMULTANEOUSLY with the answer.
    It is also the right isolation -- `listener` must not see the
    helpdesk's history, or it would start answering the caller instead
    of writing down what they said.

    Returns "" on any failure. A transcript is a record of the call, not
    part of it: losing one must never cost the caller their answer.
    """
    try:
        return (await listener.ask(TRANSCRIBE_CUE, attachments=[{
            "mime_type": UTTERANCE_MIME, "data": wav,
            "display_name": "caller.wav"}])).strip()
    except Exception as exc:
        # Name AND message: an earlier version reported only the type,
        # and "(not transcribed: NameError)" said nothing about which
        # name was missing -- which was the whole question.
        return f"(not transcribed: {type(exc).__name__}: {exc})"


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Speak to the agent; it speaks back.")
    parser.add_argument("--once", action="store_true",
                        help="handle one utterance and exit")
    parser.add_argument("--no-transcribe", action="store_true",
                        help="skip the parallel listener that writes down "
                             "what the CALLER said (it costs tokens, not time)")
    parser.add_argument("-s", "--scenario", choices=sorted(SCENARIOS),
                        default="voice",
                        help="which demo to run (default: voice)")
    parser.add_argument("--profile", default=None,
                        help="override the scenario's profile")
    parser.add_argument("-a", "--agent", default=None,
                        help="override the scenario's persona")
    parser.add_argument("-g", "--greet", action="store_true", default=None,
                        help="let the agent speak first (the helpdesk "
                             "scenario does this anyway)")
    args = parser.parse_args()

    # A SCENARIO picks profile, persona and opening together, because
    # they are not independent: `helpdesk` needs Esteban AND the tiers
    # that let him consult the systems.  Choosing them separately is a
    # trap -- `--agent helpdesk` alone ran the helpdesk PERSONA against
    # the plain `voice` profile, which has no planner tier and no
    # service connector, so the agent was told to consult systems it did
    # not have and went looking for tools instead.
    scenario = SCENARIOS[args.scenario]
    profile = args.profile or scenario["profile"]
    agent = args.agent or scenario["agent"]
    greet = scenario["greet"] if args.greet is None else args.greet

    # The mic thread and the asyncio loop are different worlds, so
    # utterances cross on a queue rather than by calling into the loop
    # from a callback.  Bounded, and for the reason ptt_capture bounds
    # its own: a consumer slower than the speaker must refuse work, not
    # accumulate an ever-staler backlog.
    inbox: "queue.Queue[bytes]" = queue.Queue(maxsize=4)
    transcript = Transcript()

    def on_utterance(u) -> None:
        if not u.complete:
            print("  (press dropped — discarded)", flush=True)
            transcript.add("caller", f"press dropped after {u.seconds:.1f}s — discarded")
            return
        try:
            inbox.put_nowait(u.wav())
        except queue.Full:
            print("  (still answering — utterance refused)", flush=True)
            transcript.add("caller", "spoke while the agent was still busy — REFUSED")

    try:
        mic = PushToTalkMic(on_utterance=on_utterance)
        mic.start()
    except SourceMuted as exc:
        print(f"microphone: {exc}", file=sys.stderr)
        return 2

    print(f"scenario {args.scenario}: profile={profile} agent={agent}"
          f"{' (agent opens the call)' if greet else ''}", flush=True)
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
                profile=profile,
                agent=agent,
        ) as session, _listener_session(
                args.no_transcribe, profile, agent) as listening:
            trace_tools(session, transcript)
            # The agent answers the phone.  This happens BEFORE the mic
            # is read, so the caller hears the greeting and then decides
            # what to ask -- which is the order a real call has, and the
            # reason it is not just another turn in the loop.
            if greet:
                print("  opening the call...", flush=True)
                said = await speak(session, OPENING_CUE)
                print(f"  said: {said}", flush=True)
                transcript.add("ESTEBAN", said)
            while True:
                try:
                    wav = await asyncio.to_thread(inbox.get, True, 0.5)
                except queue.Empty:
                    mic.raise_if_faulted()    # surface a dead half promptly
                    continue
                print(f"  heard {len(wav)} bytes; asking...", flush=True)
                spoken_for = (len(wav) - 44) / BYTES_PER_SECOND
                asked = time.monotonic()
                # Both at once. The transcription fits inside the time
                # the agent spends answering, so the record costs the
                # call nothing but tokens.
                said, heard_words = await asyncio.gather(
                    speak(session, "", wav),
                    hear(listening, wav) if listening else _nothing())
                transcript.add(
                    "caller",
                    heard_words or f"spoke for {spoken_for:.1f}s")
                if heard_words:
                    print(f"  heard: {heard_words}", flush=True)
                print(f"  said: {said.strip() or '(nothing)'}", flush=True)
                transcript.add(
                    "ESTEBAN",
                    f"{said.strip() or '(nothing)'}"
                    f"   [after {time.monotonic() - asked:.1f}s]")
                if args.once:
                    return 0
    except KeyboardInterrupt:
        # Ctrl-C is how a call ENDS here, not a crash.  It used to escape
        # as a traceback, which buried whatever had just happened under a
        # stack that described nothing about the conversation.
        print("\n  (call ended)", flush=True)
        return 0
    except SignalLost as exc:
        print(f"microphone: {exc}", file=sys.stderr)
        return 2
    except (AgentError, SessionCreateFailed) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        mic.stop()
        path = HERE / "out" / f"call-{datetime.now():%Y%m%d-%H%M%S}.txt"
        print(f"\n  transcript of the call — mm:ss from its start\n")
        print(transcript.render(), flush=True)
        transcript.write(path)
        print(f"\n  saved to {path.relative_to(HERE)}", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
