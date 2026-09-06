# jaato-cascade-audio-interchange

A two-process demonstration that a jaato session can **answer a text
question out loud**, and that a second, unrelated process can **listen to
it as it speaks**.

The model is asked "What colour is the sky on a clear day?" and replies
with audio. `run_cascade.py` saves that audio as a WAV; `run_observer.py`
— which never creates a session and never sends a message — plays the
same bytes through PulseAudio as they arrive.

```
run_cascade.py  ──create_session(profile="speaker")──▶  daemon ──▶ OpenRouter
      ▲                                                   │        gpt-audio-mini
      └── ToolOutputEvent(mime_type, data_b64, sequence) ◀─┤
                                                           │
run_observer.py ──cascade_events(cascade_id) ─────────────◀┘   ──▶ paplay
```

## What it actually demonstrates

**The tier declares the role; nothing in this client asks for audio.**
The `speaker` profile has one tier carrying `modalities: {audio:
outbound}`. Entering that tier is what puts `modalities: ["text",
"audio"]` and `audio: {voice, format}` on the request. Neither script
mentions audio to the daemon — remove the tier key and the same code
gets a text answer.

**Model speech is told apart from a tool's bytes by `call_id`.** Media
the model produced arrives under the reserved `call_id` `"model-output"`;
media a tool returned arrives under that tool's call id. Both travel on
the same `ToolOutputEvent` channel.

**The two scripts share an id, not a module.** The observer never imports
the driver. They agree on a cascade id and nothing else, so either can be
rewritten without touching the other.

**They divide along first-party vs third-party, not lifecycle vs data.**
The driver owns the cascade's lifecycle *and reads its own result* — it
opens each stage with `IPCClient.session(...)`, gets the typed completion
payload back from `session.complete(...)`, and receives the model's speech
through that call's `on_media` sink. The observer is a *third party*: it
created nothing, and attaches to a cascade id it was handed
(`cascade_events(role="observer")`). Those are two different SDK
mechanisms for two different jobs, which is why both touch media without
duplicating a responsibility.

**No event-loop plumbing.** The driver contains no `asyncio.Event`, no
`subscribe`, no `done.wait()`. `Session.complete` owns that recipe —
including settling when the *session* does rather than when its first turn
ends, which matters here because this stage is routinely nudged and a turn
boundary would report success while the model still had a tool call to
make.

## Running it

```bash
./run.sh                                  # driver only, writes out/answer.wav
./run.sh -o                               # driver + observer, plays it live
./run.sh -p "¿Por qué el mar es salado?"  # ask something else
./run.sh -o -q -p "Count to three."       # observe, trace without sound
./run.sh -s duet                          # the two-tier scenario, below
```

`-s` picks the **scenario** (`speaker`, the default, or `duet`); `-p`
gives the question. An unknown scenario is refused immediately, listing
the ones that exist — the profile file is on disk, so there is no reason
to spend a 60-second daemon timeout discovering a typo.

`PYTHON` overrides the interpreter (default `python3`, so an activated
virtualenv is used as-is).

The daemon defaults to the SDK's own default socket, so this runs against
a stock `jaato-server` with nothing to configure. Point it elsewhere with
`--socket /path/to.sock` or `JAATO_IPC_SOCKET` — a second daemon for
development, a per-user socket, a container path. Nothing here hardcodes
a path, which also keeps it correct on Windows, where the default is a
named pipe rather than a file.

With `-o` the observer's trace streams into the terminal as chunks
arrive, and the wrapper waits for playback to finish before returning —
it says so while it waits, because a 30-second answer means 30 seconds
between the driver's last line and the shell prompt.

`-o` exists because **ordering matters**: a one-stage run takes about
seven seconds, less than a second Python process needs to boot, connect
and register, so an observer started after the driver attaches to a
cascade that has already ended and hears nothing at all. `run.sh -o`
starts the observer first and waits for it to report itself attached
before firing.

The two scripts can still be driven directly, sharing a cascade id:

```bash
CID=$(python -c "import uuid;print(uuid.uuid4().hex)")
python run_observer.py $CID &
python run_cascade.py  $CID --prompt "What makes a rainbow?"
```

**It answers in the language you ask in** — the persona sets the form,
not the language.

### Preflight

```bash
jaato-doctor --workspace . --env-file .env
```

### The daemon must speak protocol 1.4

Media on `ToolOutputEvent` (`mime_type`, `data_b64`, `sequence`,
`stream_id`, `final`) arrived in wire protocol **1.4**, so both scripts
declare it:

```python
IPCClient(SOCKET, ..., min_protocol_version="1.4")
```

Against an older daemon the connection is refused by name rather than
running and hearing nothing — those fields are simply never sent, which
is indistinguishable from a model that chose not to speak.

Separately, note that an editable install pointing at one checkout while
the daemon runs another via `PYTHONPATH` gives the two halves different
event shapes, and no version check catches that (the daemon is new
enough; the client just cannot parse it). See KNOWN_ISSUES.md #823.

## Layout

| Path | What it is |
|------|-----------|
| `run.sh` | Wrapper: runs the driver alone, or with the observer attached first |
| `run_cascade.py` | Fires one stage, reassembles the audio, writes `out/answer.wav` |
| `run_observer.py` | Attaches to a cascade id, traces events, hands speech to the player |
| `pulse_playback.py` | PulseAudio playback for headerless PCM — knows nothing about jaato |
| `speech_collector.py` | Reassembles chunks into a WAV — likewise knows nothing about jaato |
| `.jaato/profiles/_base_speaker.yaml` | Tier-1 base: no plugins, completion gating, no provider bound |
| `.jaato/profiles/openrouter_gpt_audio_mini/speaker.yaml` | Tier-2 set: binds OpenRouter + `openai/gpt-audio-mini`, declares the speaking tier |
| `.jaato/agents/speaker.md` | The `speaker` persona — answer in one spoken sentence |
| `.jaato/profiles/openrouter_gpt_audio_mini/duet.yaml` | The second scenario: a text planner plus a speaking tier |
| `.jaato/agents/duet.md` | The `duet` persona — decide, delegate the speaking, complete |

The base profile stays provider-agnostic on purpose: to try another
audio model, add a sibling set directory and select it with
`JAATO_PROFILE_SET`.  Both agents live in one set because they differ by
SCENARIO, not by binding — a second provider set would then give you both
of them without restating either.

## Two scenarios

**`speaker`** (the default) — one audio model answers out loud and closes
the session. This is what the sections above describe.

**`duet`** (`./run.sh -s duet`) — a cheap text model decides the answer
and closes the session; an audio tier is entered only to say it. It is the shape a real
deployment usually wants, and it exists here because it exposes something
the single-tier demo cannot.

`enter_tier` is a MODE SWITCH, so returning from the speaking tier needs a
deliberate act by the model in it — routinely the model LEAST able to
perform one. Measured over four runs, the audio tier never handed back:
it said its sentence and stopped, and the framework's completion nudge —
a safety net for an agent that forgot to finish — was the only thing that
ever unblocked the return. In one run the audio model closed the session
itself, from the wrong tier, against its persona.

The executor tier therefore declares `exit_on: completion`: the framework
enters it, lets it do one completion, returns to the planner, and reports
what the delegate produced. The speaking model does nothing to hand back.
After that change the nudge disappears from every run:

```
before:  enter_tier(executor) → NUDGE → enter_tier(planner) → signal_completion
after:   enter_tier(executor) → [delegation report] → signal_completion
```

Returning the tier BINDING alone was not enough, and that is the
interesting part: the delegate's completion settling is what ENDS the
turn, so switching back handed the wheel to a tier with no turn to steer.
Reporting the outcome is what returns control — through the ordinary
mid-turn path, not through the nudge.

> **`exit_on` needs a framework that has it.** An older one parses the
> profile and ignores the key, so `duet` still runs — with the nudge, as
> above. That is the symptom to expect if the delegation report never
> appears in the trace; check for `TIER_EXIT_ARMED` in
> `.jaato/logs/duet_trace.jsonl` before suspecting the profile.

## Known issues

Framework defects found while building this are tracked in
[KNOWN_ISSUES.md](KNOWN_ISSUES.md), each linked to its upstream issue.
