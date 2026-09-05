# Known issues

Defects found while building this demo. Each is filed against the
framework repo ([Jaato-framework-and-examples/jaato][repo]); this file
records what the workaround costs *here*, so a reader who hits the
symptom knows why the code looks the way it does.

[repo]: https://github.com/Jaato-framework-and-examples/jaato

| # | Title | Status here |
|---|-------|-------------|
| [#820][820] | `jaato-scaffold`: `--provider`/`--model` shouldn't be required for cascade/observer | no workaround needed |
| [#821][821] | `jaato-scaffold new observer` emits wire event-type values; the daemon filters on class names | **worked around** |
| [#822][822] | Tiered profile without top-level `provider:` fails bootstrap | **worked around** |
| [#823][823] | `jaato-doctor` doesn't detect client/daemon SDK checkout skew | **worked around** |

[820]: https://github.com/Jaato-framework-and-examples/jaato/issues/820
[821]: https://github.com/Jaato-framework-and-examples/jaato/issues/821
[822]: https://github.com/Jaato-framework-and-examples/jaato/issues/822
[823]: https://github.com/Jaato-framework-and-examples/jaato/issues/823

---

## #821 — a scaffolded observer is silently deaf

`jaato-scaffold new observer` generates an `event_types` filter using
`EventType` **values** (`"tool.output"`), but the daemon matches on the
Python **class name**:

```python
# server/session_manager.py
return type(event).__name__ in self.event_types
```

`type(event).__name__` is `"ToolOutputEvent"`, never `"tool.output"`, so
no generated filter can match. Registration succeeds, the banner prints,
and the observer then receives nothing for the life of the cascade — no
error, no warning. It is indistinguishable from "the cascade produced no
events", which is why it cost two debugging rounds.

**Workaround:** `EVENT_TYPES` in `run_observer.py` spells class names.

## #822 — a tier declaring a provider is not enough

The runner's bootstrap envelope validates `provider_name`, and
`runner_spawn.py` resolves it from the profile's **top level only** —
nothing consults `model_tiers[<initial>].provider`. A profile whose tier
fully declares model *and* provider fails with
`envelope.provider_name is empty`, surfacing client-side as a bare
60-second timeout.

`jaato-scaffold validate` actively advises removing the key ("the
session bootstraps from the initial tier"), which is precisely what does
not happen — so following the validator produces a profile that
validates cleanly and cannot start a session.

**Workaround:** `speaker.yaml` keeps top-level `model:` / `provider:`
duplicating the tier, with a comment saying why they cannot be dropped.

## #823 — client and daemon can run different checkouts

An editable install pointing at one tree while the daemon runs another
via `PYTHONPATH` gives the two halves different event shapes. The daemon
sends `mime_type` / `data_b64`; the client's older `ToolOutputEvent` has
nowhere to put them, so pydantic drops them silently and the failure
appears as an `AttributeError` several frames from anything you wrote.

Every surface reports healthy throughout: socket listening, handshake
fine, session runs, events arrive.

**Workaround:** both scripts call `require_media_sdk()` before
connecting and refuse with one sentence naming the offending
`jaato_sdk.__file__`.

---

# Open questions (not yet filed)

## `openai/gpt-audio-mini` writes tool names as prose

Measured across four consecutive sessions, the model ends its speaking
turn by emitting the bare token `signal_completion` as **text** rather
than as a tool call:

```
[model] The sky is usually blue on a clear day.

        signal_completion                       ← text, not a call
[user]  Your session is about to end without calling `signal_completion`…
[model] CALL signal_completion {"spoken": "The sky is usually blue on a clear day."}
```

Two consequences. An audio turn renders the whole assistant text as
speech, so **the listener hears the words "signal completion" spoken
aloud**. And because no call registered, the stage costs a second billed
audio generation for the nudge round-trip.

Three responses were considered:

- **`quirks.prose_tool_calls`** — ruled out on inspection. That protocol
  carries a fenced ` ```tool_call ` JSON block *inside the model's text*
  (`_prose_tools.py`), and this stage's text is what gets spoken: the
  model would read the JSON out loud, and stripping the block from the
  text afterwards cannot unspeak the audio. It also withholds the native
  `tools` array that the model uses correctly on the second turn.
- **`api_params.tool_choice: required`** — the right lever, but inert on
  OpenRouter: `openrouter/provider.py` never forwards `tool_choice`, and
  the contract honestly declares `tool_choice_forwarding=False`. The
  implementation exists on `_openai_compat/base.py`, which openrouter
  does not inherit — the same gap that made model audio unreachable
  through OpenRouter in the first place.
- **Splitting the roles across two tiers** — a speaking tier answers, a
  cheap text tier owns completion. No framework change, but heavier for
  a one-question stage.

Unresolved. The persona has been left as-is so the behaviour stays
reproducible.

## A stale validator warning

`jaato-scaffold validate` reports on any outbound modality:

> `outbound_modality_not_deliverable`: … no adapter parses
> model-generated media and the streaming callback is text-only, so
> nothing can deliver it yet.

That was true when written and is false now — this demo plays the audio.
The check should be conditioned on the provider's `output_media`
capability rather than asserted flatly, or every operator who writes a
working speaking tier is told their profile is inert.
