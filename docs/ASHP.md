# ASHP / Dual-HVAC Integration — Proposal

Status: proposal, not yet planned or implemented — planning deliberately not
started yet (2026-09-08: several Open Questions below are resolved, but the
hardware-verification step they depend on hasn't happened). Moved here from
the repo root 2026-09-08. Companion to
`docs/hvac_thermostat_automation_plan.md` (the Landing/Playroom Airstage
automation this proposal builds on top of — read that first) and
`docs/daemon_design.md`.

**Housekeeping note:** this is a working proposal doc, not meant to be
permanent alongside the canonical plan — once this is folded into
`docs/hvac_thermostat_automation_plan.md` (per the "extend existing
hvac_automation" decision below) or otherwise superseded by an actual plan
document, **delete this file** rather than leaving it as a second,
drifting source of truth.

Role & Task:
Act as a Principal System Architect. Refactor and implement heating automation logic in the codebase to integrate dual HVAC units (Landing and Playroom) with the primary Air Source Heat Pump (ASHP). The codebase already contains a global target temperature state; build upon it using the rules, predictive learning, and validation constraints below.

---

### Functional Requirements

1. **ASHP-mode Day/Night Schedule & Zone Control** (superseded 2026-09-09,
   replacing the original -4°C/20°C/25°C numbers below with a concrete
   clock — see the owner's exact wording preserved at the bottom of this
   section)
   * This schedule applies **only while the whole-house mode is `ASHP_ON`**
     (per requirement 2 below) — while `ASHP_OFF`, the existing
     `hvac_automation` subsystem (already recovered, see
     `hvac_thermostat_automation_plan.md`) runs completely unchanged, on its
     own `schedule.yaml`-driven targets, and this ASHP-mode schedule has no
     effect at all.
   * **06:00–22:00** (`ashp_day_start_time`/`ashp_night_start_time`,
     configurable, shared boundary for both the ASHP and HVAC halves below):
     * ASHP target: **18°C** (`ashp_day_target_c`, configurable).
     * HVAC (both Airstage units): **OFF**.
   * **22:00–06:00**:
     * ASHP target: **14°C** (`ashp_night_target_c`, configurable).
     * HVAC: **ON**, at fixed targets independent of the normal
       `hvac_automation` schedule/decision logic (which is suspended
       whole-house while `ASHP_ON` — see the interference note below):
       Landing **18°C** (`ashp_night_landing_target_c`, configurable),
       Playroom **25°C** (`ashp_night_playroom_target_c`, configurable).
   * **Both ASHP and HVAC are legitimately ON simultaneously 22:00–06:00** —
     confirmed explicitly by the project owner, overriding an earlier
     "never simultaneously control HVAC and ASHP" framing from this same
     conversation. The actual constraint (see §6 and V&V below) is narrower:
     while `ASHP_ON`, the *normal* `hvac_automation` decision logic
     (`hvac_decision_logic.determine_hvac_decision`, `schedule.yaml`) must
     not also be independently writing targets to the same units — this
     fixed 22:00–06:00 Landing/Playroom schedule is the only thing allowed
     to drive the HVAC units during `ASHP_ON`, precisely so the two control
     loops can't fight over the same hardware.
   * The original sustained-deficit trigger (requirement 2) and
     temperature/forecast deactivation logic (requirement 3) are **unchanged
     and still govern the `ASHP_OFF ↔ ASHP_ON` transition** — confirmed by
     the project owner ("the md file was probably right... stays off until
     triggered"). Only the *contents* of what `ASHP_ON` actually sets
     changed, from the original flat "-4°C below daytime" number to this
     explicit clock.
   * Owner's exact wording, preserved verbatim since it took several rounds
     to pin down precisely: *"6am-10pm - ASHP on at 18 and HVAC off.
     10pm-6am - ASHP at 14 and HVAC at Landing 18 and Playroom 25... Both
     can be on at the same time, i.e. 10pm-6am, however during this time
     the non-ASHP-on-mode HVAC automation doesn't control the temperature
     of the HVACs - instead say what I have used here."*

2. **ASHP Activation Logic (Sustained Deficit Trigger)**
   * Keep the ASHP **OFF** by default across seasons. Primary heating must be handled by the local HVAC units.
   * Automatically trigger ASHP **ON** if all conditions are met:
     1. Current mode is Day.
     2. Playroom HVAC is running at its maximum setting — the same
        `hvac_ceiling_c` as Night Mode's Playroom setpoint above (decided
        default **25°C**, not the hardware's raw 30°C `mode_temp_limits`
        ceiling — see Open Question 6).
     3. The indoor ambient temperature remains consistently below the global target temperature for a continuous duration of 2 hours during peak daytime heating.

3. **Predictive Learning & ASHP Deactivation Logic**
   * Record the outdoor ambient temperature baseline whenever the ASHP is forced ON.
   * Deactivate the ASHP only when predictive criteria confirm it is no longer required:
     * **Temperature Threshold:** Outdoor daytime temperature rises by ≥ 2°C above the recorded activation baseline.
     * **Lookahead Anti-Flapping Rule:** Analyze the 48-hour weather forecast. Do NOT turn off the ASHP if the forecasted outdoor temperature drops back to or below the activation threshold within the next 48 hours. Prevent daily toggling ("flapping").

---

### Verification & Validation (V&V) Constraints

1. **State Machine Integrity:** Ensure deterministic transitions between just
   two whole-house states, **`ASHP_OFF`** and **`ASHP_ON`** — simplified
   2026-09-09 from the original four (`HVAC_ONLY_DAY`/`HVAC_ONLY_NIGHT`/
   `DUAL_HEAT_DAY`/`DUAL_HEAT_NIGHT`) now that day/night is handled *inside*
   `ASHP_ON` by its own fixed clock (requirement 1) rather than needing a
   separate top-level state per time-of-day. Write unit tests covering both
   transitions and every point in the ASHP-mode clock.
2. **No Double Control (new, 2026-09-09):** While `ASHP_ON`, the normal
   `hvac_automation` decision logic must not independently write targets to
   either Airstage unit — only requirement 1's fixed 22:00–06:00
   Landing/Playroom schedule may drive them. Verify this directly (e.g. by
   asserting `determine_hvac_decision`/`hvac_target_update` is not invoked,
   or is a deliberate no-op, whenever `ASHP_ON`) rather than only inferring
   it from correct output — this is precisely the failure mode §6 (external
   interference detection) exists to catch if it ever regresses.
3. **Hysteresis & Anti-Short Cycling:** Enforce a minimum runtime guard (e.g., 6 hours) once the ASHP turns ON, and a minimum rest guard once turned OFF.
4. **Boundary & Safety Checks:**
   * Clamp all HVAC setpoints: Min 10°C, Max 30°C.
   * Add explicit fallback handling for missing or stale weather API data (default to historical baseline if external API fails).
5. **Automated Testing:** Implement mock tests simulating:
   * A 2.5-hour temperature deficit triggering ASHP activation.
   * A 2°C outdoor temp increase WITH a forecasted drop 24 hours later (ASHP must remain ON).
   * A 2°C outdoor temp increase WITH stable warm forecast (ASHP must turn OFF).
   * Every quarter-hour boundary of the ASHP-mode clock (05:59/06:00,
     21:59/22:00, and the wrap at 05:59/06:00 the next day) producing the
     right ASHP + HVAC targets, including confirming HVAC is genuinely OFF
     (not just "no target set") 06:00–22:00.
   * `ASHP_ON` and `ASHP_OFF` both hitting the exact same fixed-point
     targets when re-evaluated repeatedly with no state change (idempotent,
     no unnecessary re-writes to hardware).

---

### 5. Dashboard Visibility

Whatever this proposal builds must be visible on the existing dashboard
(`src/dashboard/`), matching how every other automation subsystem in this
repo already surfaces its live state there (battery mode, hot water, and the
existing Airstage/HVAC automation cards). At minimum the dashboard should
show, once implemented:

* Current state-machine state (`HVAC_ONLY_DAY` / `HVAC_ONLY_NIGHT` /
  `DUAL_HEAT_DAY` / `DUAL_HEAT_NIGHT`) and whether the ASHP is currently ON
  or OFF.
* If ON: the recorded outdoor-temperature activation baseline, how long it's
  been running (relevant to the minimum-runtime guard), and the current
  deactivation check status (current outdoor temp vs. baseline+2°C, and
  whether the 48h-forecast anti-flapping rule is currently holding it on).
* If OFF: time remaining on the minimum-rest guard, if still within it.
* Whether the last weather-forecast fetch succeeded or is falling back to
  the historical baseline (V&V constraint 3).

Whether this becomes a new dashboard card or extends the existing
Airstage/HVAC automation card(s) (`status_collector.py`'s
`_attach_hvac_automation_summary`, `static_page.py`'s `airstageZoneCard`) is
an open question — see Q7 below.

---

### 6. External Interference Detection ("fighting the system")

**Status: built, 2026-09-09, scoped to ASHP only.** `src/core_logic/interference_logic.py`
(the shared, subsystem-agnostic detection logic - `ControlledAttributeState`/
`evaluate`/`note_reasserted`/`record_verified_write`, fully unit-tested) plus
its wiring into `scripts/ashp_automation_core.py`'s T6R write path
(`_apply_ashp_target_with_interference_check`), config keys
`ashp.interference_dwell_minutes`/`ashp.interference_min_reasserts` (defaults
30 min / 1 reassertion), and a persisted `ashp_interference` state-file key.
Detected interference logs a warning (`logger.warning`, greppable) and is
**not surfaced on the dashboard yet** - only a log line today. Confirmed
2026-09-09 with the project owner: this is explicitly an **efficiency**
concern, not a safety one - both sides are always issuing ordinary, in-range
commands, they just disagree; nothing about the hardware is left in an
unsafe state by it, so the automation's response is "keep re-asserting and
log it", not to back off or halt.

**Scope note:** unlike the rest of this doc, this requirement isn't
inherently ASHP-specific — it applies equally to hot water
(`hotwater_decision_logic.py` / `melcloud_client.py`) and the existing
HVAC/Airstage automation (`hvac_decision_logic.py` / `airstage_client.py`).
It was built for ASHP first (the newest, highest-risk write path — it
shares the T6R with whatever else might be controlling it) using
`interference_logic.py` as a genuinely reusable module, not an ASHP-specific
one — wiring it into the other two subsystems' own writes is a follow-up
using the same two functions, not a redesign, whenever that's wanted.

* **Problem:** every one of these subsystems writes a setpoint/mode to a
  device and then, on its next poll, reads that device's state back. Nothing
  currently distinguishes "the device is still catching up to our last
  write" from "something else keeps changing it back" — a remotely-set
  schedule on the ASHP/HVAC vendor app, someone using a physical remote or
  the vendor's own app, or a second automation writing to the same device.
  Today that would just look like the automation's own decision logic
  flapping, with no signal pointing at an external cause.
* **Detection principle: continuous divergence, not a single blip.** A
  one-off mismatch between commanded and observed state (the device hasn't
  applied the write yet, a stale poll, a single dropped command) is normal
  and expected — it should NOT be flagged. What should be flagged is a
  **sustained** mismatch: the device's reported setpoint/mode disagrees with
  what this software last commanded, persistently, across multiple
  consecutive poll cycles, for longer than the subsystem's own settle time.
  That pattern — reverting to (or moving to) the same other value repeatedly
  after being corrected — is the signature of an external actor actively
  re-asserting a competing setting, not noise.
* **Precedent to build on:** the SolaX side already persists a
  last-commanded-state record for exactly this kind of "what did we last
  tell it to do, and when" comparison — `_modbus_mode_controller.py`'s mode
  change log, read back via `get_mode_change_log_path()` /
  `read_json_state()` and surfaced on the dashboard as
  `last_mode_change_reason` / `last_mode_change_at`
  (`status_collector.py:189,210-211`). Hot water, HVAC, and ASHP have no
  equivalent record of "last setpoint/mode this software commanded" to diff
  the next poll's actual reading against — that would need adding per
  subsystem.
* **Behavior once detected — decided 2026-09-09:** log it
  (`external_override_suspected`, via `logger.warning`) and **keep
  re-asserting the automation's own setpoint** — no back-off, no
  alert-and-hold. Confirmed with the project owner this is an efficiency
  concern (wasted write cycles/energy from two things disagreeing), not a
  safety one, so there's no reason to make the automation less assertive
  about its own correct setting. Not yet surfaced on the dashboard, only in
  the log — a dashboard badge is a reasonable follow-up, not done in this pass.
* **Decided 2026-09-09 (ASHP only so far):** `dwell_minutes`/`min_reasserts`
  are both `ashp.*` config keys (30 min / 1 reassertion default) — genuinely
  configurable per subsystem when this extends beyond ASHP, not a single
  hard-coded constant. Still open for hot water/HVAC's own eventual wiring:
  what their own `dwell_minutes`/`min_reasserts` values should default to,
  given their poll intervals already differ from ASHP's and from each
  other (see `daemon_design.md`).

---

### Open Questions (raised 2026-09-08, before planning)

Found while reviewing the existing codebase against this proposal — several
touch on decisions this project already made deliberately, so need explicit
sign-off (or a deliberate reversal) before a plan can be written:

1. **How does software actually turn the ASHP on/off and change its target
   temperature?** This is the central open question and the plan can't be
   written without an answer. Two candidate control paths already exist in
   this codebase, and neither is currently wired up for this purpose:
   - **`src/api_clients/resideo_client.py`** (the T6R thermostat wired to
     the ASHP) — its docstring states plainly: *"It never writes to Target
     Temperature or Target Heating Cooling State, even though the paired
     accessory technically permits it: controlling the ASHP via the T6R is
     deliberately out of scope for now."* This proposal would reverse that
     decision if the T6R is the intended control point.
   - **MELCloud** (`src/api_clients/melcloud_client.py`) — already
     authenticated and in production for the hot water tank on this same
     Ecodan unit. The underlying `pymelcloud` library also models
     zone-based space-heating control (`SetTemperatureZone1`,
     `OperationModeZone1`, flow-temperature setpoints), gated by whether
     this specific installation reports `HasThermostatZone1` — **not
     verified against the real device**, since this project has only ever
     used MELCloud for the tank.
   - It's also possible neither actually works: some Ecodan installations
     run space heating purely off the wired room thermostat's call-for-heat
     signal, in which case a MELCloud zone setpoint write might have no
     effect on when the compressor actually runs, and "turning the ASHP
     off" might require something else entirely (a relay, a physical
     switch, an installer-level setting).
   - **Recommendation, matching how `docs/hvac_thermostat_automation_plan.md`
     §4.1 handled the equivalent uncertainty for Airstage's batched writes:
     verify this empirically against the real hardware first**, before any
     decision-logic/daemon code is written against an untested assumption.
   - **Decided 2026-09-08: verify against real hardware first**, per the
     recommendation above. **This is a hard blocker on planning** — nothing
     in this doc should be turned into an implementation plan until it's
     known which of the two candidate paths (or neither) actually controls
     the ASHP.

2. **Does this integrate with the existing `hvac_automation` subsystem, or
   is it a new, separate one?** `docs/hvac_thermostat_automation_plan.md`'s
   design (**correction, 2026-09-09: not actually live** — the daemon/core
   scripts were never committed anywhere despite the plan doc's "DONE"
   claim; recovered from stale `.pyc` bytecode and committed this same day,
   see that doc's step 5 for the full story — `schedule.yaml` has since
   also been recovered, from a separate old project's session history;
   `config/hvac_automation_state.json` still doesn't exist, but needs no
   recovery - it's created automatically on first real run) already has a
   master/mirror zone schedule, day/night targets
   (`heat_target_c`/`cool_target_c`), mode escalation, dwell timers, Away
   mode, and `config/hvac_automation_state.json`. This proposal's "global
   target temperature state" almost certainly refers to that same schedule
   target. Should the ASHP tri-state logic be added as a new layer inside
   `hvac_decision_logic.py`/`hvac_mode_daemon.py`, or built as an
   independent `ashp_decision_logic.py`/`ashp_mode_daemon.py` pair that
   only *reads* the existing HVAC state (matching this repo's usual
   one-subsystem-per-daemon convention)?
   - **Decided 2026-09-08: extend the existing `hvac_automation` subsystem**
     (`hvac_decision_logic.py`/`hvac_mode_daemon.py`/
     `hvac_automation_state.json`) rather than building a separate daemon.
     This also settles Open Question 8 (no new systemd unit or
     `SERVICE_HEALTH_CHECKS` entry needed — it rides on the existing
     `home_automation_hvac.service`).

3. **What supplies "indoor ambient temperature"?** The existing T6R read
   (`fetch_resideo_status`) and Playroom's own Airstage sensor
   (`current_temperature_c`) are both already available and could disagree.
   Which is the source of truth for the 2-hour sustained-deficit check?

4. **Outdoor temperature — current reading and 48h forecast source.**
   Airstage already reports a live `outdoor_temperature_c` per zone
   (usable for the activation baseline). For the 48-hour forecast, this
   project already depends on **Open-Meteo** (free, no API key) for
   `solar_forecast_predictor.py`, which pulls a weather forecast for this
   exact location already — reusing that same client for temperature is
   the obvious default rather than adding a new weather API/dependency.
   Confirm that's acceptable, or specify a different source.
   - **Decided 2026-09-08: reuse Open-Meteo.**

5. **Are the numbers in this doc final, or examples?** Several read as
   possibly illustrative rather than firm requirements — worth confirming
   before they're hard-coded: night setpoints (Landing 20°C / Playroom
   28°C), the 2-hour sustained-deficit window, the 2°C deactivation
   threshold, the 48-hour lookahead window, and the "e.g., 6 hours" min
   runtime/rest guard.
   - **Decided 2026-09-08: all thresholds configurable in `config.yaml`**
     (matching every other subsystem in this repo), with the numbers in
     this doc as starting defaults — **except** the Playroom HVAC ceiling,
     which the project owner set explicitly to **25°C**, not 28°C (used
     both for Night Mode's Playroom setpoint and the "Playroom at its
     maximum" ASHP-activation trigger — see requirements 1 and 2.2 above,
     and Open Question 6's resolution below). Still open: the exact
     `config.yaml` key names/section and the remaining defaults (2h
     deficit window, 2°C threshold, 48h lookahead, runtime/rest guard) —
     to be finalized during planning.

6. **Where does night-mode's Playroom ceiling sit relative to the existing
   `mode_temp_limits`?** The live HVAC plan already clamps `heat` mode to
   16–30°C — confirm the ASHP-activation ceiling is meant to be the same
   number as Night Mode's Playroom setpoint, not two independently-configured
   numbers that could drift apart.
   - **Decided 2026-09-08: yes, one number** — a new, tighter
     `hvac_ceiling_c` (default 25°C) distinct from `mode_temp_limits`'s raw
     hardware clamp (16–30°C), shared by both Night Mode's Playroom setpoint
     and the ASHP-activation "running at maximum" check. See requirements 1
     and 2.2 above.

7. **Dashboard placement** (see §5 above): extend the existing per-zone
   Airstage/HVAC card, or add a distinct "ASHP" card? The existing plan's
   precedent (`docs/hvac_thermostat_automation_plan.md` step 7) was to
   extend existing cards rather than add new ones — but a whole-house ASHP
   state machine arguably isn't a property of either zone individually, so
   the same reasoning may not transfer directly.

8. **Service/health-check wiring**: if this becomes its own daemon (per Q2),
   should it get its own systemd unit and an entry in `status_collector.py`'s
   `SERVICE_HEALTH_CHECKS` (battery/hot-water/dashboard/Ohme already have
   one), so a stuck or crashed ASHP daemon shows up the same way those do?

9. **Where should external-interference detection (§6) actually live? —
   Resolved 2026-09-09, option (b).** Built as a shared, subsystem-agnostic
   utility (`src/core_logic/interference_logic.py`, per option (a)'s own
   description) but *wired in* only for ASHP so far, not planned/implemented
   for all three up front — hot water/HVAC-Airstage remain a follow-up using
   the same module, exactly as option (b) described. This was practical
   rather than a considered rejection of (a)/(c): §6 surfaced while doing the
   ASHP work itself, and the project owner asked for it specifically as a
   precondition of deploying ASHP ("I don't want to deploy something where
   there is a genuine risk of systems working in opposition") — building the
   reusable core made it cheap to extend later, without waiting to also
   design hot water/HVAC's own wiring first.