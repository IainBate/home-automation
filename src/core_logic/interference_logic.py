"""External Interference Detection - pure functions.

Detects a **sustained** mismatch between what this software last verified
it wrote to a device and what it currently observes - the signature of an
external actor (a remote schedule on the vendor app, someone using a
physical remote, a second automation) repeatedly re-asserting a competing
setting on the same device. See docs/ASHP.md §6 for the full design
rationale; this module is the shared, subsystem-agnostic core it
describes, first wired into scripts/ashp_automation_core.py (the newest,
highest-risk write path - it shares the T6R with whatever else might be
controlling it). Extending it to hvac_automation_core.py/
hotwater_automation_core.py's own writes is a natural follow-up using the
same two functions below - not done in this pass, scoped to ASHP only.

**This is an efficiency signal, not a safety one** (confirmed with the
project owner 2026-09-09): two systems fighting over the same setpoint
wastes write cycles and energy and is confusing to observe, but the
device itself is never left in an unsafe state by it - both sides are
issuing ordinary, in-range commands, just disagreeing about what the
ordinary command should be.

Detection principle - continuous divergence, not a single blip: a
one-off mismatch (the device hasn't caught up to a just-verified write
yet, a stale poll) is normal and must never be flagged. What's flagged is
a mismatch that (a) persists to the *same* foreign value across multiple
consecutive polls, for longer than `dwell_minutes`, AND (b) survives at
least `min_reasserts` of this software actively re-commanding its own
value in response - a device drifting through different random values
each poll is noise, and a mismatch this software hasn't yet tried to
correct isn't evidence of anything fighting it.

Precedent: `_modbus_mode_controller.py`'s mode-change log already
persists "what did we last tell it to do, and when" for the SolaX side
(surfaced on the dashboard as `last_mode_change_reason`/
`last_mode_change_at`) - this module generalises that same idea into a
comparable-against-live-reads state machine, clock-injected like every
other decision module in this codebase (`now` passed in, never
`datetime.now()` called here).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

ControlledValue = Any  # whatever type a given attribute's commanded/observed value is (float, str, bool, ...)


@dataclass
class ControlledAttributeState:
    """Persisted per-attribute tracking - one instance per (device, characteristic).

    Attributes:
        commanded_value: The last value this software verified it
            successfully wrote, or None if nothing has been commanded yet.
            Only ever set via record_verified_write() - see that
            function's docstring for why an unverified write must never
            reach here.
        commanded_at: When commanded_value was last set.
        diverged_since: When the observed value first started disagreeing
            with commanded_value, continuously since - None while they
            agree, or immediately after any agreeing sample.
        diverged_to: The foreign value repeatedly observed while
            diverged. A later sample disagreeing with THIS (not just with
            commanded_value) restarts the clock - see module docstring on
            "the same foreign value" being the actual signature.
        reassert_count: How many times this software has re-commanded
            commanded_value (via note_reasserted()) while still observing
            divergence.

    """

    commanded_value: ControlledValue = None
    commanded_at: datetime | None = None
    diverged_since: datetime | None = None
    diverged_to: ControlledValue = None
    reassert_count: int = 0


@dataclass
class InterferenceVerdict:
    """The result of one evaluate() call. Advisory only - see module docstring."""

    status: str  # "ok" | "settling" | "external_override_suspected"
    reason: str
    diverged_for_seconds: float | None = None
    foreign_value: ControlledValue = None


def record_verified_write(
    state: ControlledAttributeState, value: ControlledValue, now: datetime
) -> ControlledAttributeState:
    """Record a write this software has confirmed actually applied.

    Call this ONLY after the device has been read back and verified to
    match `value` - never on a bare send. Starting the divergence clock
    from an unverified write would make "the device hasn't caught up yet"
    (completely normal) indistinguishable from real interference, which
    is exactly the false-positive this module exists to avoid.

    Resets divergence tracking entirely - a fresh command is a clean
    slate, not a continuation of whatever was diverging before.
    """
    return ControlledAttributeState(commanded_value=value, commanded_at=now)


def note_reasserted(state: ControlledAttributeState) -> ControlledAttributeState:
    """Record that this software just re-sent commanded_value while still diverged.

    Call after re-writing the same commanded value in response to an
    "settling"/"external_override_suspected" verdict (not after a normal
    fresh command - use record_verified_write for that). Bumps the
    counter min_reasserts gates on, so a flag requires surviving at least
    one real correction attempt, not merely existing for a while.
    """
    return replace(state, reassert_count=state.reassert_count + 1)


def evaluate(
    state: ControlledAttributeState,
    observed_value: ControlledValue,
    now: datetime,
    *,
    dwell_minutes: float,
    min_reasserts: int,
) -> tuple[ControlledAttributeState, InterferenceVerdict]:
    """Update divergence tracking against a fresh observation, and return a verdict.

    Call once per poll, before deciding whether to (re-)write. Returns
    the updated state (persist it) and a verdict describing what was
    found - "external_override_suspected" only once both the dwell time
    and the reassert count are satisfied; "settling" for a divergence
    that's real but hasn't cleared either bar yet; "ok" whenever nothing
    has been commanded, or the observation currently agrees.
    """
    if state.commanded_value is None:
        return state, InterferenceVerdict("ok", "nothing commanded yet")

    if observed_value == state.commanded_value:
        return (
            replace(state, diverged_since=None, diverged_to=None, reassert_count=0),
            InterferenceVerdict("ok", "matches last commanded value"),
        )

    if state.diverged_since is None or state.diverged_to != observed_value:
        # First sample of a new divergence, or it moved to yet another
        # different foreign value - either way this is a fresh clock, not
        # a continuation (see module docstring: drifting between several
        # different values isn't the same-actor signature this looks for).
        new_state = replace(state, diverged_since=now, diverged_to=observed_value, reassert_count=0)
        return new_state, InterferenceVerdict(
            "settling",
            f"observed {observed_value!r}, commanded {state.commanded_value!r} - just diverged",
            diverged_for_seconds=0.0,
            foreign_value=observed_value,
        )

    diverged_for = now - state.diverged_since
    if diverged_for >= timedelta(minutes=dwell_minutes) and state.reassert_count >= min_reasserts:
        return state, InterferenceVerdict(
            "external_override_suspected",
            f"observed {observed_value!r} instead of commanded {state.commanded_value!r} for "
            f"{diverged_for}, surviving {state.reassert_count} re-assertion(s)",
            diverged_for_seconds=diverged_for.total_seconds(),
            foreign_value=observed_value,
        )
    return state, InterferenceVerdict(
        "settling",
        f"diverged to {observed_value!r} for {diverged_for}, {state.reassert_count} "
        f"re-assertion(s) so far - below dwell_minutes={dwell_minutes}/min_reasserts={min_reasserts}",
        diverged_for_seconds=diverged_for.total_seconds(),
        foreign_value=observed_value,
    )
