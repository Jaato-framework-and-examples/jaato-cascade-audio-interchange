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

## Running it

```bash
./run.sh                                  # driver only, writes out/answer.wav
./run.sh -o                               # driver + observer, plays it live
./run.sh -p "¿Por qué el mar es salado?"  # ask something else
./run.sh -o -q -p "Count to three."       # observe, trace without sound
```

`PYTHON` overrides the interpreter (default `python3`, so an activated
virtualenv is used as-is).

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
| `run_cascade.py` | Fires one stage, reassembles the audio, writes `out/answer.wav` |
| `run_observer.py` | Attaches to a cascade id, traces events, plays speech live |
| `.jaato/profiles/_base_speaker.yaml` | Tier-1 base: no plugins, completion gating, no provider bound |
| `.jaato/profiles/openrouter_gpt_audio_mini/speaker.yaml` | Tier-2 set: binds OpenRouter + `openai/gpt-audio-mini`, declares the speaking tier |
| `.jaato/agents/speaker.md` | The persona — answer in one spoken sentence |

The base profile stays provider-agnostic on purpose: to try another
audio model, add a sibling set directory and select it with
`JAATO_PROFILE_SET`.

## Known issues

Framework defects found while building this are tracked in
[KNOWN_ISSUES.md](KNOWN_ISSUES.md), each linked to its upstream issue.
