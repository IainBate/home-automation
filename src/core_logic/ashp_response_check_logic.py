"""ASHP/MELCloud Response Corroboration - pure functions.

Cross-references the ASHP's T6R heat-call (docs/ASHP.md §1's confirmed
control path) against MELCloud's own device-wide status for the same Ecodan
unit (already fetched and cached by hotwater_automation_core.py's force-heat
check via src/api_clients/melcloud_status_cache.py - see that cache's
"status" field, sourced from melcloud_client.py's HotWaterStatus enum, which
despite its tank-focused name already carries HEAT_ZONES: pymelcloud's
AtwDevice.status property is device-wide ("what is the compressor currently
doing"), not zone-specific).

This answers a different question than src/core_logic/interference_logic.py:
interference_logic asks "is something ELSE overriding what we commanded on
the SAME device/characteristic" (T6R vs T6R); this asks "did our T6R command
actually make the physical unit respond at all" (T6R vs MELCloud, two
independent APIs for the one physical heat pump). Same "advisory only, not a
safety signal" stance either way (confirmed with the project owner
2026-09-09/10) - this never changes what gets written to the ASHP, only logs.

Response lag is expected and must not cause false positives (confirmed with
the project owner 2026-09-10): anti-short-cycle protection and defrost
cycles delay the compressor's own response by several minutes, and
MELCloud's own cache is only refreshed every ~10-15 minutes (see
melcloud_status_cache.DEFAULT_MAX_AGE_SECONDS) - so this never judges a
single poll, only sustained non-response across response_window_minutes.
STATUS_HEAT_WATER/STATUS_DEFROST/STATUS_LEGIONELLA are treated as "busy
elsewhere", not "not responding" - the compressor is genuinely active on a
legitimate competing job (the shared Ecodan unit also serves the hot water
tank), which this module can observe but not distinguish from "the ASHP call
will simply never be served" without also modelling that arbitration -
scoped out of this pass; a human reading the logged reason can tell the
difference.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

# MELCloud device statuses (melcloud_client.py's HotWaterStatus values) that
# mean the compressor is genuinely doing something else, not idling - not
# evidence the ASHP call is going unserved, just not yet served.
_BUSY_ELSEWHERE_STATUSES = frozenset({"heat_water", "defrost", "legionella"})

# Statuses that mean "not responding" once observed_status is neither
# HEAT_ZONES nor one of the above - "idle", "standby", "cool" all qualify by
# simply not being in either set above.


@dataclass
class AshpResponseCheckState:
    """Persisted across polls - one instance per ASHP install.

    Attributes:
        active_since: When the CURRENT continuous ashp_active=True call
            began, or None while ashp_active is False. Reset to None (not
            carried through) the moment ashp_active goes False - a fresh
            activation later starts a clean clock, never a continuation of
            an earlier one.

    """

    active_since: datetime | None = None


@dataclass
class AshpResponseVerdict:
    """The result of one evaluate_ashp_response() call. Advisory only.

    Attributes:
        status: "not_active" (ASHP isn't currently calling for heat) |
            "settling" (active, but either just started or still within
            response_window_minutes) | "unknown" (no fresh MELCloud status
            to judge by) | "ok" (MELCloud confirms HEAT_ZONES) |
            "busy_elsewhere" (compressor active on tank/defrost/legionella
            instead, past the window) | "no_response_suspected" (past the
            window, MELCloud shows neither heat_zones nor a busy-elsewhere
            status - the only status this module's caller should ever log).
        reason: Human-readable explanation, for logging.
        active_for_seconds: How long ashp_active has been continuously True,
            or None while not active.

    """

    status: str
    reason: str
    active_for_seconds: float | None = None


def evaluate_ashp_response(
    state: AshpResponseCheckState,
    *,
    ashp_active: bool,
    observed_status: str | None,
    now: datetime,
    response_window_minutes: float,
) -> tuple[AshpResponseCheckState, AshpResponseVerdict]:
    """Update response tracking against a fresh MELCloud observation.

    Call once per ASHP decision-check cycle (scripts/ashp_automation_core.py's
    run_ashp_decision_check), regardless of whether this cycle wrote anything -
    unlike interference_logic.evaluate, there is no "fresh command" special
    case here, since a genuine response can take longer than the 30s poll
    cadence in either scenario.
    """
    if not ashp_active:
        return AshpResponseCheckState(active_since=None), AshpResponseVerdict(
            "not_active", "ASHP is not currently calling for heat"
        )

    if state.active_since is None:
        new_state = replace(state, active_since=now)
        return new_state, AshpResponseVerdict(
            "settling",
            "Just started calling for heat - too early to judge a response",
            active_for_seconds=0.0,
        )

    active_for_seconds = (now - state.active_since).total_seconds()

    if observed_status is None or observed_status == "unknown":
        return state, AshpResponseVerdict(
            "unknown",
            "No fresh MELCloud status available to corroborate the T6R call",
            active_for_seconds=active_for_seconds,
        )

    if observed_status == "heat_zones":
        return state, AshpResponseVerdict(
            "ok",
            "MELCloud confirms the heat pump is actively heating zones",
            active_for_seconds=active_for_seconds,
        )

    if active_for_seconds < response_window_minutes * 60.0:
        return state, AshpResponseVerdict(
            "settling",
            f"MELCloud reports {observed_status!r} after "
            f"{active_for_seconds:.0f}s - still within the "
            f"{response_window_minutes:.0f}-minute response window",
            active_for_seconds=active_for_seconds,
        )

    if observed_status in _BUSY_ELSEWHERE_STATUSES:
        return state, AshpResponseVerdict(
            "busy_elsewhere",
            f"MELCloud reports {observed_status!r} - the heat pump is busy on a "
            f"legitimate competing job (tank heating/defrost), not necessarily "
            f"unresponsive",
            active_for_seconds=active_for_seconds,
        )

    return state, AshpResponseVerdict(
        "no_response_suspected",
        f"T6R has been calling for heat continuously for "
        f"{active_for_seconds / 60.0:.0f} minutes but MELCloud still reports "
        f"{observed_status!r} - the heat pump may not be responding",
        active_for_seconds=active_for_seconds,
    )
