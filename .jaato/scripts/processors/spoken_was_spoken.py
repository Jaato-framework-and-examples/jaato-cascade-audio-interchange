"""Refuse a completion that CLAIMS speech the session never delegated.

The duet's contract is that the planner cannot speak: it decides, hands
the sentence to an audio tier, and reports in ``spoken`` what was said.
Nothing in the framework enforces the middle step, and a real run showed
why that matters — asked for a twenty-line story, the planner wrote the
story itself, never entered the executor tier, and signalled completion
with its own text as ``spoken``:

    [user]        'Narrate a short story of no more than 20 lines'
    [gpt-4o-mini] '<the whole story, as text>'      <- answered directly
    [user]        NUDGE
    [gpt-4o-mini] CALL signal_completion({"spoken": "<the whole story>"})

No audio was produced and the session still reported success.  The
payload asserted something that had not happened, and only the driver
noticed, because it had no bytes to write.

WHAT THIS CAN AND CANNOT PROVE.  ``context.tool_calls`` is the session's
real tool-call ledger, so this proves the DELEGATION WAS REQUESTED --
`enter_tier` was called, naming a tier, and the call succeeded.  It does
NOT prove audio arrived: no media count is exposed to a processor, and
the ledger cannot see the bytes.  So this is a strong proxy, not proof,
and it is worth being precise about which one it is: it catches the
failure that actually happened (never delegating), and would not catch a
delegation whose model then said nothing.
"""
from typing import Any, Dict, List

#: The tier that can actually emit audio.  Declared here rather than
#: discovered, because a validator that infers its own expectations from
#: the thing it is validating proves nothing.
SPEAKING_TIER = "executor"


def validate(payload: Dict[str, Any], context: Any) -> List[str]:
    """Return errors; empty means the completion may proceed."""
    errors: List[str] = []
    spoken = (payload or {}).get("spoken") or ""

    # ``ToolCallEntry`` is a TypedDict, so ledger entries are plain
    # dicts -- ``getattr(call, "name")`` silently yields None on every
    # one of them, which blocks every completion and loops the agent
    # until its nudges run out.  (Measured once: 225s of audio and a
    # NudgeExhausted, because the first draft of this file used
    # attribute access and a unit test that fed it objects agreed.)
    entered = [
        call for call in (context.tool_calls or [])
        if call.get("name") == "enter_tier"
        and (call.get("args") or {}).get("name") == SPEAKING_TIER
    ]
    succeeded = [call for call in entered if call.get("success")]

    if spoken.strip() and not entered:
        errors.append(
            f"`spoken` claims something was said out loud, but this session "
            f"never called enter_tier({SPEAKING_TIER!r}) — so nothing was "
            f"ever spoken. Enter the {SPEAKING_TIER} tier and say it, or "
            f"report the failure in `errors` instead of asserting it in "
            f"`spoken`."
        )
    elif entered and not succeeded:
        errors.append(
            f"enter_tier({SPEAKING_TIER!r}) was called but did not succeed, "
            f"so `spoken` cannot describe anything that was said."
        )
    return errors
