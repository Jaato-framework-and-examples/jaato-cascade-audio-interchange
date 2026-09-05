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

The driver alone writes a WAV:

```bash
python run_cascade.py
paplay out/answer.wav
```

To *hear it live*, the observer must be attached **before** the driver
fires. A one-stage run takes about seven seconds — less time than a
second Python process needs to boot, connect and register — so an
observer started afterwards reliably attaches to a cascade that has
already ended and sees nothing at all:

```bash
CID=$(python -c "import uuid;print(uuid.uuid4().hex)")
python run_observer.py $CID &     # attaches first
python run_cascade.py  $CID       # then fires
```

Add `--no-audio` to the observer to trace without sound.

### Preflight

```bash
jaato-doctor --workspace . --env-file .env
```

### The SDK must match the daemon

Both scripts refuse to start against a `jaato_sdk` whose
`ToolOutputEvent` has no `mime_type` / `data_b64` — the fields only exist
once binary media delivery is present. Against an older SDK the daemon
still sends them and pydantic silently drops them, so the failure would
otherwise surface as an `AttributeError` deep inside an event handler.

This is easy to hit with two checkouts and one virtualenv: an editable
install pointing at one tree while the daemon runs another via
`PYTHONPATH`. Point them at the same tree.

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
