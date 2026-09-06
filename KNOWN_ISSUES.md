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
| [#823][823] | `jaato-doctor` doesn't detect client/daemon SDK checkout skew | superseded — see below |
| [#827][827] | `jaato-scaffold new cascade` hand-rolls the stage loop and discards the payload | **fixed here by hand** |

[820]: https://github.com/Jaato-framework-and-examples/jaato/issues/820
[821]: https://github.com/Jaato-framework-and-examples/jaato/issues/821
[822]: https://github.com/Jaato-framework-and-examples/jaato/issues/822
[823]: https://github.com/Jaato-framework-and-examples/jaato/issues/823
[827]: https://github.com/Jaato-framework-and-examples/jaato/issues/827

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

## #827 — the generated cascade driver discards its own payload

`jaato-scaffold new cascade` emits the stage loop as `asyncio.Event` +
`subscribe_once(SESSION_TERMINATED)` + `done.wait()`, returning the
terminal *reason*. That is correct for knowing a gated stage ended, and
tells you nothing about what it produced — so this workspace declared a
`completion_payload_schema` with a `spoken` field and never read it,
printing `natural` while the model's transcript sat unread in
`AGENT_COMPLETED.payload`.

**Fixed here by hand:** `run_cascade.py` uses `IPCClient.session(...)` +
`session.complete(prompt, on_media=...)`, which returns the payload,
raises `AgentError` on an error terminal, and settles when the session
does rather than when its first turn ends (jaato #767 — this stage is
routinely nudged, so a turn boundary would report success while the
model still had a tool call to make). The driver lost 21 lines and all
of its event-loop primitives.

Revert to the generated shape when #827 lands, rather than keeping a
local divergence.

## #823 — client and daemon can run different checkouts

An editable install pointing at one tree while the daemon runs another
via `PYTHONPATH` gives the two halves different event shapes. The daemon
sends `mime_type` / `data_b64`; the client's older `ToolOutputEvent` has
nowhere to put them, so pydantic drops them silently and the failure
appears as an `AttributeError` several frames from anything you wrote.

Every surface reports healthy throughout: socket listening, handshake
fine, session runs, events arrive.

**No workaround here any more.** The first version of this tree carried
a hand-rolled `require_media_sdk()` that inspected `ToolOutputEvent`'s
fields at startup. That duplicated a mechanism the SDK already has: a
client declares the wire protocol it needs and the handshake refuses
anything older. Media delivery is protocol **1.4**, so both scripts now
say so once:

```python
MEDIA_PROTOCOL = "1.4"
IPCClient(SOCKET, ..., min_protocol_version=MEDIA_PROTOCOL)
```

Against an older daemon that produces

```
IncompatibleServerError: Server protocol 1.3 is not supported by this
client (requires >= 1.4): server minor 3 is below client's required
minor 4 — daemon is missing fields the client depends on.
Daemon package: 0.7.0.
```

That covers the direction that matters in deployment — *the daemon is
too old*. It does **not** cover the dev-environment skew #823 describes
(an editable install pointing at one checkout while the daemon runs
another via `PYTHONPATH`), because there the daemon is new enough and
the client simply cannot parse what it sends. That remains #823's job.

Using the mechanism also turned up a bug in it: the compat gate built
its error out of instance state that `disconnect()` had already cleared,
so `IncompatibleServerError` was always constructed with `None` and its
own constructor then crashed on `None.split(".")`. The refusal surfaced
as `ConnectionError: Handshake failed: 'NoneType' object has no
attribute 'split'` — meaning the gate had never once produced its
intended message. Fixed upstream on the media branch.

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
- **`api_params.tool_choice: required`** — implemented, then rejected on
  measurement. OpenRouter genuinely never forwarded `tool_choice` (the
  contract declared `tool_choice_forwarding=False`, and the provider had
  no such parameter at all), so it was wired up: `complete()` now takes
  the argument its own contract already declared, the knob is declared,
  and the capability is true. It reaches the wire and does exactly what
  it promises — **one** generation, a native call, no nudge. It also
  produces **zero audio**, measured over three consecutive runs: the
  model satisfies `required` by calling the tool and saying nothing,
  which removes the one thing this stage exists to produce. The knob is
  therefore useful and correct, and wrong here.
- **Splitting the roles across two tiers** — a speaking tier answers, a
  cheap text tier owns completion. No framework change, but heavier for
  a one-question stage.

**Partly solved, by the persona.** The persona now states that
everything written is spoken aloud, and that finishing is an action
rather than something you say. Measured over three Spanish runs, the
model stopped writing the tool name entirely:

```
'El cielo en un día despejado suele ser azul.'
'El cielo en un día despejado suele ser de un azul muy claro.'
'El cielo en un día despejado suele ser azul.'
```

Spanish answers went from **7.90s to 3.2-4.05s** — over half of that
audio had been the model reading a function call out loud, JSON braces
and field names included, which is also the clearest evidence for why
`prose_tool_calls` is wrong for a speaking stage.

What it did NOT fix: the model still emits no native tool call on its
first turn. It simply stopped narrating one. So the nudge still fires
and every stage still costs two generations — history is still
`user → model(text) → user(nudge) → model(CALL)`. The audible defect is
gone; the tool-calling deficiency behind it is not.

**And the completion payload is unreliable because of it.** The driver
prints `payload["spoken"]`, which is the model's own transcript of what
it said — correct when it completes on turn one. When the nudge fires,
the model fills that field describing the NUDGE instead:

```
[model] 'A rainbow is a colorful arc in the sky formed by sunlight
         passing through raindrops.'          <- turn 1, spoken
[user ] 'Your session is about to end without calling signal_completion...'
[model] CALL {"spoken": "Everything is complete on my end."}
[model] 'Got it, I understand. Everything is complete on my end.'  <- also SPOKEN
```

So the driver reports "Everything is complete on my end", and the WAV
runs ~10s instead of ~3 because the second turn speaks as well. Reading
the payload is still right — it is the schema's whole purpose — but on
this model its value is only trustworthy on a single-generation run.

## The two-tier hand-off depends on the completion nudge

`.jaato/profiles/openrouter_gpt_audio_mini/duet.yaml` is the reproduction
case for this, kept because it is the shape a real deployment wants: a
cheap text model decides the answer and closes the session, an audio tier
only says it out loud.

It runs, and the tier swap works in both directions. But the audio tier
never hands back on its own. Four runs, every one:

```
122955: enter_tier(executor) -> **NUDGE** -> enter_tier(planner) -> signal_completion
123040: enter_tier(planner) -> enter_tier(executor) -> **NUDGE** -> signal_completion
123050: enter_tier(planner) -> enter_tier(executor) -> **NUDGE** -> enter_tier(planner) -> signal_completion
123103: enter_tier(planner) -> enter_tier(executor) -> **NUDGE** -> enter_tier(planner) -> signal_completion
```

The audio model speaks and stops. The return trip happens only because
the framework's completion nudge prods it — a safety net doing structural
work it was never designed for. A profile that is not completion-gated,
or one that spends its nudge budget, stalls in the audio tier.

In run `123040` the audio model called `signal_completion` **itself**,
from inside the executor tier, never returning to the planner and against
the persona's explicit instruction. So *which* model closes the session is
not deterministic either.

Worth stating precisely, because the earlier note here overstated it: this
model is not "unable to emit tool calls". It emitted `enter_tier` and
`signal_completion` correctly. What it does not do is emit a tool call
**unprompted at the end of a spoken turn** — which is why the nudge
rescues every run.

**Fixed upstream by `exit_on: completion`.** The executor tier now
declares it, and the framework enters the tier, lets it do one
completion, returns to the planner, and reports what the delegate
produced as a mid-turn message. The model in the speaking tier does
nothing to hand back. Same profile, measured after:

```
enter_tier(executor) -> [delegation report] -> signal_completion
```

The nudge is gone, and so is the model's manual `enter_tier(planner)`.

Returning the tier BINDING alone was not enough and is worth recording:
the delegate's completion settling is what ENDS the turn, so switching
back handed the wheel to a tier with no turn to steer — the manual
`enter_tier` disappeared but the nudge remained. Reporting the outcome
is what returns control, through the mid-turn path the framework already
has. It also closes a quieter hole: model media never enters history, so
without the report the caller could only learn what was said when a
transcript happened to arrive.

## Nothing checks that a `spoken` payload was ever spoken

A `duet` run asked for a twenty-line story. The planner wrote the story
in its OWN turn, never entered the executor tier, and then called
`signal_completion` with the story as `spoken` — asserting it had been
said aloud when nothing had been. The session reported success; only the
driver noticed, because it had no audio to write.

```
[user]        'Narrate a short story of no more than 20 lines'
[gpt-4o-mini] '<the whole story, as text>'      <- answered directly
[user]        NUDGE
[gpt-4o-mini] CALL signal_completion({"spoken": "<the whole story>"})
```

Two faults, and only one of them was the persona's.

The persona opened with "decide the one short sentence that answers the
question", so a request for twenty lines contradicted step 1 — and the
model resolved that by abandoning the whole procedure rather than just
that step. It now states that the planner CANNOT speak, that its own
text reaches nobody, and that `enter_tier` comes FIRST, before composing
anything, whatever the answer's length. Re-measured on the same prompt:

```
enter_tier(executor)[gpt-4o-mini] -> SPOKE[gpt-audio-mini]
  -> [delegation report] -> signal_completion[gpt-4o-mini]
```

30.85s of audio, no nudge.

The framework fault is unfixed: **a completion-gated stage can claim in
its payload that it spoke, with `media_chunks == 0`, and nothing
objects.** `exit_on: completion` guarantees the RETURN from a
delegation; nothing guarantees the delegation happens at all. A profile
that declares an outbound audio tier and finishes having emitted no
audio is, at minimum, worth a warning — the driver already treats it as
a failure, but the session does not.

## Overlapping playback — a turn ending is not a stream ending

Heard as several answers at once on a long narration. The cause was in
this repo, not the framework.

`run_observer.py` closed its players on `TurnCompletedEvent`, added so a
`paplay` fed chunk-by-chunk would not hold an open stdin forever. That is
the wrong signal: a duet run completes several turns AROUND the speech,
so the player was closed mid-utterance and the next chunk for the SAME
stream created a fresh `paplay` — which began playing while the previous
one was still draining. One 31s narration produced **nine** players from
a single observer (eight zombies and one stopped), and they outlived the
run, so the next one played on top of them.

A second constant made it worse: `PulsePlayer.finish` waited
`timeout=30`, and the narrations were 30.85s and 31.10s. The wait expired,
the observer gave up and exited, and `paplay` was orphaned — after which
`run.sh` could not see it either, because it polls by parent pid and the
parent had just died.

Both are fixed:

* players close when the STREAM changes or the SESSION ends, never on a
  turn boundary;
* the drain deadline is derived from the bytes actually written
  (`bytes / (rate x channels x width) + 15s`) instead of a constant that
  cannot bound a wait whose length is the caller's data. A player past
  that deadline is killed rather than orphaned.

Measured after: peak 1 concurrent player on a 31s narration, and nothing
left running.

`final=True` on the last media chunk would make the stream boundary
explicit and remove the inference entirely — the field is in the contract
and the framework never sets it for model speech.

---

# Fixed upstream while building this

## A stale validator warning — fixed

`jaato-scaffold validate` used to report, on *any* outbound modality,
that no adapter could deliver model media and the declaration was inert.
True when written; false the moment one could — at which point it told
every author of a working speaking tier that their profile did nothing.

It now asks the provider instead of asserting: the warning fires only
when the named provider does not declare `output_media`. Six adapters
that decode model media now declare it (openrouter, plus the
`_openai_compat` five), and this workspace's profile validates with no
errors and no warnings.
