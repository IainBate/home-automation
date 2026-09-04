# HVAC & Thermostat Automation — Project Plan

Status: proposed, not yet implemented. Companion to `docs/daemon_design.md` (read
that first for the two-tier polling / priority-decision conventions this plan
reuses rather than reinvents).

Source specs (both in `~/Downloads/drive-download-20260901T213737Z-1-001/`):
- *Home Thermostat and HVAC control specification.docx* — the actual
  requirements (referred to below as **the spec**). Authoritative.
- *HVAC Control System - Home Automation Implementation.docx* — a sketch
  implementation (`main.py`/`logic.py`/`api_wrappers.py`, synchronous
  `requests`, single global `state` dict, no locking, no retry/revert, no
  single-API-call mode+temp batching despite the spec requiring it). It does
  not match either repo's real client libraries (pyairstage, evohome-async)
  or this project's architecture, and several of its behaviours contradict
  the spec it's supposedly implementing. Treat it as a rough statement of
  intent only, not a blueprint — this plan does not follow its structure.

## 1. Recommendation: build this in `home_automation`, not `heating_automation`

`heating_automation` (`~/heating_automation`) and `home_automation`
(`~/home_automation`) currently overlap almost completely on this feature:

| Concern | `heating_automation` | `home_automation` |
|---|---|---|
| Fujitsu Airstage read | `ACClient.get_status` | `airstage_client.fetch_airstage_status` |
| Fujitsu Airstage write | `ACClient.set_power/set_mode/set_temperature/set_minimum_heat` (built, tested) | none yet |
| Same two physical units | Playroom `192.168.68.107`, Upstairs `192.168.68.106` | same IPs, config names "Playroom"/"Landing" |
| Honeywell thermostat read | `EvohomeClient` (evohome-async/TCC v2) — kept as a correct reference implementation for genuine Evohome hardware, unused by this household | `resideo_client.fetch_resideo_status` — **resolved 2026-09-04**: rebuilt on local HomeKit (`aiohomekit`), read-only, live in production on the dashboard |
| Honeywell thermostat write | `EvohomeClient.set_temperature/set_system_mode/reset_zone` | none — **and none needed**: clarified 2026-09-04 that the spec's Phase 2 "Writes: house target temperature" targets the Airstage Playroom unit, not the T6R. The T6R is read-only for this entire project |
| Known blocker | (not diagnosed in this repo) | **Resolved 2026-09-04**: was `config.yaml`'s `resideo.enabled: false` (real device is a Lyric unit, not genuine Evohome) — fixed by pairing local HomeKit instead of using TCC v2; `resideo.enabled: true` now, verified live against the real device |
| Daemon/scheduler/state persistence | none | `base_daemon.TwoTierPollingDaemon`, `state_store.locked_json_state`, `core_logic/*_decision_logic.py`, `config_manager` schema validation, `dashboard_server.py` |
| Deployment | no systemd/cron entries found | full `docs/PI4_DEPLOYMENT.md`, `.service` files, dashboard, health checks |

Two independent sessions reached the identical Lyric-vs-Evohome diagnosis by
building the same client against the same account — that's wasted effort, and
without consolidation a third session would likely do it again. `home_automation`
also has everything the spec actually needs and `heating_automation` lacks:
the daemon loop, race-safe state file, schedule-normalisation-shaped precedent
(`hotwater_decision_logic.py`), config schema validation, and the dashboard
that's meant to show status "on your phone."

**Recommendation:** implement the new work directly in `home_automation`,
port `heating_automation`'s AC *write* methods in (the one piece
`home_automation` doesn't already have — the T6R is read-only for this
project, so no Evohome write methods are needed, see §3.2), then retire
`heating_automation` (or leave it as an inert reference — see §6). This is
a plan step, not done yet — nothing has been moved.

## 2. Target architecture

```
config.yaml (hvac_automation, airstage, resideo sections)
    → config_manager.load_static_config()
        │
        ├── src/api_clients/airstage_client.py   (extend: add write ops)
        │     fetch_airstage_status()  [existing, read]
        │     set_airstage_power/mode/temperature/minimum_heat()  [new, from heating_automation.ACClient]
        │     set_mode() always targets ALL configured zones — shared outdoor unit
        │
        ├── src/api_clients/resideo_client.py   (DONE 2026-09-04 — see §3)
        │     fetch_resideo_status()  [existing, read-only via local HomeKit/aiohomekit]
        │     Read-only for this entire project (confirmed 2026-09-04): the spec's
        │     T6R "write" targets the Airstage Playroom unit, not the T6R itself — no
        │     ThermostatBackend abstraction, write interface, or cloud-fallback backend
        │     needed. Decision logic calls fetch_resideo_status() directly; None means
        │     "unavailable this poll," same convention every other client here uses.
        │
        ├── src/core_logic/hvac_schedule_logic.py   (NEW, pure functions)
        │     normalise_schedule(), active_period_for(day, time)  — spec Phase 3
        │
        ├── src/core_logic/hvac_decision_logic.py   (NEW, pure functions/dataclasses)
        │     HvacDecisionContext / decide_next_action()  — spec Phase 4
        │     mirrors hotwater_decision_logic.py's shape: no I/O, fully unit-testable
        │
        ├── src/utils/state_store.py   (existing — reused, not modified)
        │     config/hvac_automation_state.json via locked_json_state()
        │
        ├── scripts/hvac_mode_daemon.py   (NEW, built on TwoTierPollingDaemon)
        │     registers: thermostat poll (10 min), HVAC target update (30 min),
        │     HVAC time/date sync (60 min, +5 min offset — see §4 gap)
        │
        ├── scripts/hvac_away_mode.py   (NEW, mirrors holiday_mode.py/service_mode.py)
        │     CLI to set/clear/query the away-mode flag the daemon reads
        │
        └── src/dashboard/status_collector.py, dashboard_server.py   (extend)
              existing Airstage/Resideo cards + new automation-state card
              (current mode/target, thermostat backend health: paired / cloud
              fallback / unavailable)
```

This is the same shape as the hot-water subsystem end to end:
`hotwater_decision_logic.py` (pure decision) + `hotwater_automation_core.py`
(state/config glue) + `hotwater_mode_daemon.py` (daemon) + `hotwater_auto_check.py`
(cron one-shot) + `holiday_mode.py`/`service_mode.py` (human override CLIs).
The HVAC subsystem should look identical, just with `hvac_` names and no
cron one-shot equivalent (nothing here is cheap enough to run from cron —
the whole point is a continuously-running decision loop, like the battery
daemon).

## 3. T6R integration status — done, not a stub

This section originally planned a stub-until-paired strategy (build the
scheduler/decision-logic/daemon/dashboard against an interface, ship a
`HomeKitThermostatBackend` that returns `available=False` until pairing
happened). **That's no longer needed — the pairing and read path are both
done and live in production**, and the write side this section was designed
to eventually add was based on a misreading of the spec.

### 3.1 What's actually done

The T6R (a Resideo "Lyric" device, not genuine Evohome — confirmed by
testing real credentials against TCC v2, which correctly rejected them) is
read via local Apple HomeKit (`aiohomekit`), paired 2026-09-04. See
`~/heating_automation` project's memory for the pairing process and the
library bugs it took workarounds for (this `aiohomekit` version requires a
live mDNS browser registered before a Controller will even start, even for
reading an already-paired accessory via its cached IP/port; a separate bug
in its IP pairing path silently drops the saved credentials unless the
alias is registered into `controller.aliases` before `save_data()`).

`src/api_clients/resideo_client.py` was rebuilt on this pairing and is live
in production, already serving the dashboard's `resideo` card — verified
via the running dashboard's `/api/status` endpoint. Pairing credentials
live at `~/.local/share/aiohomekit/pairing.json` on the Pi (shared between
`heating_automation` and `home_automation`, since both run as the `pi`
user) — no cloud account, developer API key, or secret in `secrets.yaml`
needed.

### 3.2 Scope clarified 2026-09-04: the T6R is read-only for this entire project

The spec's Phase 2 "Writes: house target temperature" was initially misread
(by this plan, and independently by the session that built
`heating_automation`'s `EvohomeClient`) as writing to the T6R itself —
confirmed with the project owner that it actually means the Airstage
Playroom unit's target temperature, driven by the schedule and the room
reading the T6R provides. **There is no `ThermostatBackend` interface, no
write method, no cloud-fallback backend, and no ported `EvohomeClient`
write methods anywhere in this plan.** `heating_automation`'s
`EvohomeClient` remains committed and correct, just unused by this
project — it's a genuine reference implementation for anyone with real
multi-zone Evohome hardware, not dead code to delete.

`hvac_decision_logic.py` and the daemon call `fetch_resideo_status(config)`
directly for room temperature. A `None` result (T6R unreachable, pairing
lost, LAN hiccup) is the same "unavailable this poll" case every other
client in this repo already requires callers to handle — no special
abstraction layer, `Protocol`, or backend-selection function needed for a
single, working, read-only client.

## 4. Known gaps between the spec and what's buildable today

Flagging these now rather than discovering them mid-implementation. The
single-API-call question (§4.1) was investigated in depth since it's the one
gap that could plausibly make the spec unachievable rather than just
imperfect. **Tested 2026-09-04: that spec clause is genuinely unachievable
on this firmware** — but the project isn't blocked by it, and the deviation
needs the owner's sign-off. See §4.1.

### 4.1 "Mode change and temperature update should be sent as a single API call"

**RESOLVED EMPIRICALLY 2026-09-04 — batching does NOT work. The optimistic
reading below was tested against the real Playroom unit and disproven.
Build the two-call fallback path. Three findings, all load-bearing for the
daemon's design:**

**Finding 1 — `/SetParam` honours exactly one key per call.** Full test
matrix against Playroom (powered off throughout, `iu_onoff` never touched,
all values restored):

| Test | Result |
|---|---|
| Batched `{iu_op_mode, iu_set_tmp}` in one call | **Neither applied** |
| Single `iu_set_tmp` alone | Applied |
| Single `iu_op_mode` alone | Applied |

Both keys work individually, so the failure isn't "mode can't be set while
off" or "temp can't be set while off" — it is specifically the combination.
The firmware ignores multi-key writes.

**Finding 2 — the HTTP response cannot be trusted as confirmation.** The
rejected batched write returned:

```json
{"value": {"iu_op_mode": "4", "iu_set_tmp": "200"}, "write_res": "ack",
 "read_res": "ack", "result": "OK", "cause": "", "error": ""}
```

`result: OK`, `write_res: ack`, and an echo of the requested values — for a
write it silently discarded. **Post-write verification by re-reading the
parameter is therefore mandatory, not optional.** Any code that treats a
2xx/`result: OK` as success will report phantom successes. This is a
stronger requirement than this plan originally assumed.

**Finding 3 — writes propagate with a delay; an immediate read-back can
return the stale value.** A successful single-key write read back as
unchanged when checked immediately, then correct a few seconds later. The
verification in Finding 2 therefore needs a short settle/retry window
(~3s, retried a few times), *not* a single immediate read. This matters
directly: without it the daemon would misread its own successful writes as
failures and trigger the spec's retry→revert path spuriously — and for a
mode change, "revert" means changing the *other* unit too, so a false
negative here causes real, visible churn on both units (§8.7).

**Consequence for the spec:** the spec's "Mode change and temperature
update should be sent as a single API call" is **not achievable on this
firmware** and needs the owner's sign-off to deviate. The practical impact
is small and the plan already reasoned this through (below): two local HTTP
calls land milliseconds apart, and the spec's own retry/revert rule already
exists to handle mode/temperature inconsistency. Sequence mode first, then
temperature, verifying each per Findings 2–3.

---

*Original (now disproven) reasoning, kept for the record:*

pyairstage's public `ApiLocal.set_parameter(dsn, name, value)` does only
send one parameter per call, but that's a limitation of pyairstage's
*function signature*, not of the device's wire protocol underneath it.
Traced the actual local endpoint (`airstageApi.py`'s `ApiLocal.set_parameter`,
installed version 3.2.2, confirmed identical to upstream
`danielkaldheim/pyairstage@master` — this isn't a version-lag problem):

```python
jsonPayload = {
    "device_id": dsn, "device_sub_id": 0, "req_id": "", "modified_by": "",
    "set_level": "02",
    "value": {str(name): str(value)},   # <- a dict, built here with one key
}
# POSTed as-is to http://<ip>/SetParam
```

`"value"` is a **dict**, wire-shaped to carry any number of `{parameter_name:
value}` pairs — pyairstage's method just always constructs it with exactly
one. This mirrors the *read* side, which we know for a fact batches many
parameters per call: `ApiLocal.get_devices()` already POSTs a `"list"` of
up to ~20 parameter names to `/GetParam` in one request (its own comment:
*"We cannot request more than 20 parameters at once"* — a stated device-side
*count* limit, not a *one-per-call* limit). A symmetric read/write protocol
design, one endpoint accepting a list and the other a dict, is exactly what
you'd expect if `/SetParam` also accepts multiple keys per call — nobody
appears to have tried it, though: no batch-write code exists in pyairstage's
CHANGELOG history back to v1.0.0, no related GitHub issues, and a scan of
sibling reverse-engineered Fujitsu clients (`homebridge-fujitsu-airstage`,
`ha_airstage`) turned up nothing that speaks to this specific question
either — this protocol is community-reverse-engineered with no official
docs, so absence of evidence isn't proof it works, just that it's
unexplored rather than known-broken.

**The fix:** don't route mode+temperature changes through pyairstage's
`AirstageAC.set_operation_mode()`/`set_target_temperature()` at all. Add a
small helper in the new `airstage_client.py` write path that builds the
combined payload directly and POSTs it via the same `ApiLocal` session
(reusing its connection/timeout/SSL-skip handling, not its single-parameter
method):

```python
async def _set_parameters_batch(api: ApiLocal, dsn: str, values: dict[str, str]) -> dict:
    payload = {
        "device_id": dsn, "device_sub_id": 0, "req_id": "", "modified_by": "",
        "set_level": "02",
        "value": {str(k): str(v) for k, v in values.items()},
    }
    return await api.async_call_api(
        "POST", f"{api.protocol}://{api.ip_address}/SetParam", json=json.dumps(payload)
    )

# mode+temp in one call:
await _set_parameters_batch(api, dsn, {
    ACParameter.OPERATION_MODE: int(OperationMode.HEAT),        # "iu_op_mode"
    ACParameter.TARGET_TEMPERATURE: int(round(temp_c * 2) / 2 * 10),  # "iu_set_tmp"
})
```

**This is empirically verifiable in about five minutes against the real
Playroom/Upstairs units** — send one combined request, check the response's
`"result"` field and then re-read both parameters to confirm both actually
changed. Recommend doing exactly that as the first implementation step for
the mode-change path (step 1 in §6), before building anything else on top of
it, so the daemon design isn't drafted against an untested assumption.

*(This verification was carried out on 2026-09-04 — see the findings at the
top of §4.1. Note that checking the response's `"result"` field, as
suggested here, would have produced the wrong answer: it reported `OK` for
the discarded write. Only the re-read revealed the truth.)*

**And even if it turns out the firmware silently only honours one key per
`SetParam` call:** the project still isn't blocked. Fall back to two
sequential calls (mode, then temperature) — detectable at runtime by
re-reading both parameters after the "batch" call and checking whether both
actually took, so the fallback path isn't silent. The brief inconsistency
window that creates is exactly what the spec's own adjacent rule already
exists to handle ("if a mode change succeeds on one unit but fails on the
other: retry... if the retry also fails: revert") — extend that same
retry/revert state machine to cover "temperature didn't follow the mode
change" as well as "mode failed on unit B." Two local HTTP calls on the same
LAN are tens of milliseconds apart; the risk this spec clause is actually
guarding against (a large, human-visible interval where mode and temperature
are inconsistent) doesn't really exist at that timescale either way. In
short: batching is very likely achievable and is the primary approach to
build and verify first, but the whole automation project's viability was
never actually contingent on it — the fallback delivers the same practical
outcome the spec is asking for.

*(Outcome 2026-09-04: batching turned out **not** to be achievable — the
"even if the firmware silently only honours one key" fallback described
just above is now the primary and only path, and "detectable at runtime by
re-reading both parameters" turned out to be the essential safeguard, not a
nicety. The closing claim held: the project's viability was never contingent
on batching. See the findings at the top of §4.1.)*

- **HVAC time/date sync** (spec Phase 4, hourly +5min offset):
  `heating_automation/CLAUDE.md` states plainly that date/time setting is
  not supported by pyairstage or the local LAN API. This check should be
  stubbed as a no-op that logs once ("unsupported by pyairstage") rather
  than silently omitted or faked — same stub philosophy as §3.
- **Mode-cycle boundaries**: spec orders modes coldest→warmest as
  `cool → dry → heat → minimum heat`, but minimum heat is "exclusively used
  in Away mode" and "bypasses all temperature validation." Read together
  with "mode is not cool" / "mode is not heat" as the two cycle-limit checks
  in Phase 4, minimum heat is *outside* the normal cycle — `cool` is the
  coldest state normal cycling ever reaches; minimum heat is only entered
  via Away-mode entry/exit, never via the 60-minute mode-change check.
  `hvac_decision_logic.py` should encode this as two disjoint state
  machines (normal 3-mode cycle; Away's separate minimum-heat override), not
  one 4-state cycle.
- **Away-mode power-on exception**: "Only a human may power units on/off
  except during Away mode entry" — the daemon is allowed to turn units *on*
  when Away starts (if off), but per "On exit, leave units on regardless of
  prior state," it must never turn them back *off* on exit, and never
  power-cycles them for any other reason (mode changes, temperature
  adjustment). Worth a code comment at the one call site that's allowed to
  invoke `set_power(True, ...)`, since it's a narrow, easy-to-accidentally-
  widen exception to an otherwise hard rule.
- **Upstairs' fixed temperature vs. Away**: "always fixed at a configurable
  value... regardless of Playroom's target" is itself overridden by Away
  mode (`10°C` on both). Precedence in `hvac_decision_logic.py`: Away >
  Upstairs-fixed-temp > normal adjustment logic.

## 5. Config additions (`config.yaml`, schema-validated like every other section)

```yaml
hvac_automation:
  enabled: false                      # start disabled, like resideo/hotwater were
  master_zone: "Playroom"
  mirror_zone: "Landing"              # matches existing airstage.zones naming
  mirror_zone_fixed_target_c: 18.0
  mode_temp_limits:
    heat: {min_c: 16.0, max_c: 30.0}
    dry:  {min_c: 18.0, max_c: 30.0}
    cool: {min_c: 18.0, max_c: 30.0}
  away_mode_target_c: 10.0
  startup_default_mode: "dry"
  poll_intervals:
    thermostat_seconds: 600           # 10 min
    hvac_target_seconds: 1800         # 30 min
    hvac_time_sync_seconds: 3600      # 60 min — currently a stubbed no-op, see §4
```

`resideo:` and `airstage:` sections already exist and need no schema
changes — `resideo.enabled: true` already, live since 2026-09-04 (§3). No
`thermostat:` section is needed at all: only `airstage_client.py`'s new
write functions are new client code: `resideo_client.py` stays read-only,
unchanged by this plan.

## 6. Migration steps (for when this plan is approved to execute)

1. ~~**Verify §4.1's batched `SetParam` against the real units first**~~ —
   **DONE 2026-09-04. Answer: batching does not work.** Build the two-call
   path (mode first, then temperature), with mandatory post-write re-read
   verification using a settle/retry window — the device acks writes it
   discards, and applies real writes with a lag. See §4.1's three findings;
   all of step 2 below must be written against them, not against the
   original single-call assumption.
2. ~~Port `heating_automation`'s four write methods into
   `airstage_client.py`~~ — **DONE 2026-09-04.** `set_airstage_power`,
   `set_airstage_mode` (no `zone_name` param — always all zones, structurally
   enforced), `set_airstage_temperature`, `set_airstage_minimum_heat`. Each
   writes via `ApiLocal.set_parameter` directly (not `AirstageAC`'s set_*
   methods, which only optimistically cache without verifying — see module
   docstring) and verifies with the settle/retry re-read §4.1 requires.
   Deliberately no fused mode+temperature function — see module docstring
   for why that's a decision-logic/daemon-layer concern, not this client's.
   Returns `{zone_name: bool}` per write, giving callers the per-zone
   granularity the retry/revert logic (§8.7) needs.
3. ~~Port its tests~~ — **DONE 2026-09-04.** 11 new tests in
   `test_airstage_client.py` (16 total in the file), covering: verification
   settling over several retries, a write silently discarded by the device
   never being reported as success, one zone's write failure not hiding the
   other zone's real result, `set_airstage_mode`'s missing `zone_name`
   parameter, temperature rounding/wire-scaling, and invalid-mode rejection.
   Full suite (757 tests) passes. **Also verified live 2026-09-04** against
   the real Playroom unit via `set_airstage_temperature` itself (not a
   throwaway script) — write verified, then restored to its original value,
   verified again. Unit stayed off throughout.
4. ~~Build `hvac_schedule_logic.py` and `hvac_decision_logic.py`~~ —
   **DONE 2026-09-04.** Both pure, no I/O. 72 new tests (829 total passing),
   covering every §7 scenario: windup/drift cap, strict debounce, mode
   ceilings and floors, restart mid-cycle, Away entry/exit, the §8.1
   symmetric gate, and both §8.7 mode-consistency conditions. The decision
   is *declarative* (it describes the end state that should hold, with None
   meaning "leave this alone"); sequencing, verification, retry and revert
   stay in the daemon and client, since the device takes one parameter per
   write and lies about whether writes applied (§4.1).

   Four things worth knowing, found while implementing:

   - **§8.4 can't be fully honoured as written.** It says Away exit should
     immediately apply the schedule's "mode + target", but the spec's
     schedule (Phase 3) carries *only* temperatures — there is no scheduled
     mode to restore. Away exit therefore restores the target immediately
     and turns minimum heat off, leaving mode to the normal cycle (whose
     dwell timers are reset on exit, so it settles for 60 min first).
   - **Mode divergence is only checked between units that are both powered
     on.** An off unit reads as mode "off", which would look like divergence
     forever — and "correcting" it would mean powering it on, which only a
     human (or Away entry) may do. An off unit is not fighting anything, so
     this is the right reading of §8.7 rather than a loophole in it.
   - **"Raise a below-minimum target to the new mode's minimum" is dormant
     under the spec's default limits** — heat→dry/cool is already forced to
     18°C by its own rule, and cool→dry share an 18°C floor, so the rule can
     never fire. Implemented and tested anyway (with custom limits), since
     `mode_temp_limits` is user-configurable per §5.
   - **§9's dry-mode dead period is real and now quantified.** With the
     drift cap at 3°C and 0.5°C steps every 30 min, a cold room in `dry`
     spends ~3 hours ratcheting the setpoint to the cap before mode
     escalation to `heat` can fire — `dry` cannot heat, so that time is
     wasted. §9 already accepted this as a known cost with a possible future
     refinement; noting the actual duration here so it isn't a surprise in
     the logs.
5. Build `scripts/hvac_mode_daemon.py` on `TwoTierPollingDaemon`, and
   `scripts/hvac_away_mode.py` mirroring `holiday_mode.py`. Reads room
   temperature via the already-live `fetch_resideo_status()` — no
   thermostat work left to do at this step.
6. Wire `hvac_automation.enabled: true` and exercise end-to-end against the
   real Airstage units. The T6R read path is already live (§3), so this is
   the first point the *full* loop — schedule, mode cycling, temperature
   adjustment, Away, and the room-temperature input — runs for real; it's
   not gated on any further thermostat work.
7. Extend `status_collector.py`/`dashboard_server.py` with the automation
   status card.
8. Decide the fate of `heating_automation`: once step 2 is ported and
   verified in `home_automation`, either delete it or leave it as an inert
   reference for `EvohomeClient` (its own CLAUDE.md/tests keep working
   standalone either way — nothing here depends on it continuing to exist).

Each step above is independently testable and independently useful. Step 1
is deliberately first: it's the cheapest possible way to retire the one open
question that could have reshaped the daemon's error-handling design, before
anything else gets built on top of an assumption.

## 7. Testing

Match existing conventions exactly:
- `hvac_schedule_logic.py`, `hvac_decision_logic.py`: pure-function unit
  tests, no mocks needed (same as `tests/core_logic/`). This is where most
  of the coverage should live — every branch in §8 below is a pure function
  of (room_temp, house_target, mode, hvac_target, dwell timestamps), so it's
  fully testable without hardware or a running daemon.
- `airstage_client.py` write functions, `thermostat_client.py` backends:
  mock at the `pyairstage`/`aiohomekit`/`evohomeasync2` boundary, never real
  network calls (same as `test_airstage_client.py`/`test_resideo_client.py`
  today).
- `hvac_mode_daemon.py`: test `_run_one_tick()` scheduling in isolation
  (same as existing daemon tests), not the real `run()` loop.
- Add a `tests/scenarios/` case (that directory already exists) once the
  full daemon is wired, covering, at minimum, every scenario §8 identifies:
  - Away entry with units off → power-on; Away exit → units stay on **and**
    immediately receive the schedule's current target (§8.4), not left at
    10°C until the next periodic check.
  - **Write verification (§4.1 findings 2–3)**: a write the device acks
    (`result: OK`) but discards must be detected as a *failure*, not a
    success — and a write that succeeds but propagates slowly must be
    detected as a *success* after the settle window, not a spurious
    failure. Both directions need a test; getting the second one wrong
    triggers needless retry→revert churn across both units.
  - **Mode consistency (§8.7)**: partial mode-change failure (one unit's
    batched call fails) → retry → revert — confirm both units end up
    reporting the *same* mode, never left split even transiently beyond the
    retry window.
  - **Mode consistency, out-of-band (§8.7)**: a mismatch between Playroom's
    and Landing's actual mode that the daemon didn't itself just cause (e.g.
    a human used a unit's physical remote) is detected and corrected
    immediately on the next poll, not deferred to the normal 30/60-minute
    cadence.
  - Human override → resumes at next scheduled interval, not mid-window,
    and does not require any override-specific code path (§8.3).
  - **Windup**: room stuck below target for many hours (simulated weather
    limit — room temp never rises regardless of HVAC target) → HVAC target
    must not run away to the mode ceiling/floor past the configured drift
    cap (§8.2); once room finally crosses target, HVAC target must return
    toward house_target within one or two ticks, not 18 ticks of 0.5°C
    unwind.
  - **Debounce**: a single contrary poll sample (room briefly crosses
    house_target for one 10-minute poll, then crosses back) must not reset
    dwell timers if the design lands on hysteresis (§8.5) — or must reset
    them if it lands on strict debounce — whichever the decision below
    settles on, but it must be a deliberate, tested choice either way, not
    whatever a naive `if` happens to do.
  - **Mode ceiling/floor**: room persistently below target in heat mode
    (already warmest) — confirm the decision logic never attempts to escalate
    past heat, and HVAC target settles at its cap rather than erroring.
  - **Restart mid-cycle**: kill and restart the daemon partway through a
    dwell window — confirm state (mode, HVAC target, dwell start times) is
    read back from `hvac_automation_state.json` unchanged, not reset to
    schedule/startup defaults (§8.6).

## 8. State-machine and physical-limit review

Walked the spec's Phase 4 rules through by hand (not just read them) looking
for states the daemon could get stuck in, states it could reach before the
hardware is actually capable of getting there, and asymmetries between the
warming and cooling directions. Four real issues, one clarification needed,
and one thing that's already correctly designed.

**Decided (2026-09-04):** all four recommendations below (§8.1 explicit
gate, §8.2 drift cap, §8.4 immediate Away-exit resume, §8.6 seed-only
startup default) are confirmed — build to the "Recommendation" text in each
subsection, not the spec-literal alternative.

### 8.1 Asymmetric mode-escalation conditions — decided: add the explicit gate

The spec's two mode-escalation rules aren't parallel:

> Room temp > house target for ≥ 60 min **AND mode is not cool AND HVAC
> target is already at its minimum** → switch to next colder mode.
>
> Room temp < house target for ≥ 60 min **AND mode is not heat** → switch to
> next warmer mode.

The cooling direction requires the HVAC target to already be maxed out in
its effort (at the mode's minimum, i.e. "asking for the coldest this mode
allows") before escalating hardware modes. The warming direction has no
equivalent "HVAC target is already at its maximum" clause — read literally,
it would escalate `dry → heat` after 60 minutes of a cold room even if
`hvac_target` were still sitting at 16°C, nowhere near tried.

In practice this mostly doesn't bite, because of an interaction with the
adjacent rule ("a target temperature change resets the 60-minute mode-change
timer"): as long as the 30-minute adjustment step keeps successfully raising
`hvac_target`, it keeps resetting the mode timer before 60 minutes can
accumulate, so mode escalation only becomes reachable once `hvac_target` has
already stopped changing — which in practice means it's capped. That's a
real mechanism, but it's an *emergent* property of two other rules
interacting, not something the code would visibly enforce — fragile against
a future change to either rule, and against edge cases like ties at exactly
30/60 minutes or a mid-window restart. **Recommendation: make it explicit.**
Add `AND HVAC target is already at its maximum` to the warming condition, so
the two directions are symmetric and the invariant is checked directly
rather than relied upon.

### 8.2 Setpoint windup on weather-limited days — decided: add the drift cap

This is the main "can it reach a state before the physical limit allows"
finding, and it's a classic control-systems problem (integral windup) that
the spec's incremental-nudge design is exposed to as written.

Walk it through: outdoor temperature is low enough that `heat` mode,
running flat out, still can't get the room above house_target (a real
possibility this is a heat pump/AC-based system, not a boiler — capacity is
outdoor-temperature-dependent). Every 30 minutes, "room < target → increase
HVAC target by 0.5°C" fires, and keeps firing, because the room genuinely
never catches up. `hvac_target` ratchets all the way to the hard cap
(30°C) and sits there — not because the room needs 30°C, but because the
control loop has no way to express "I'm trying as hard as this mode allows
and it's not enough" other than pushing the setpoint to its ceiling.

The problem surfaces once the weather improves: the room starts actually
warming toward the *real* desired temperature (whatever house_target
currently is, say 21°C), but `hvac_target` is sitting at 30°C. The "room >
target → decrease by 0.5°C" rule only starts once room temp exceeds
`hvac_target` itself (30°C) — not house_target — so the room will overshoot
past the actually-desired 21°C and keep climbing toward 30°C before the
system even starts backing off, and then only at 0.5°C per 30 minutes. That
could be several hours of a needlessly overheated house burning energy for
no comfort benefit, purely as an artifact of how far the setpoint wound up
during the cold snap. Exactly symmetric on the cooling side in a heatwave.

**Recommendation:** bound how far `hvac_target` is allowed to drift from
`house_target`, not just from the mode's hardware min/max — e.g. a
configurable `max_drift_c` (start around 3–5°C) on top of the existing
mode min/max clamp. Once `hvac_target` hits `house_target ± max_drift_c`,
that's the same "tried as hard as this mode allows" signal that currently
only exists implicitly, and it's what should gate §8.1's mode-escalation
check — replacing "target already at its minimum/maximum" (hardware limit)
with "target already at max drift from house_target" (a much tighter,
faster-to-reach, more sensible trigger) is probably closer to the spec's
actual intent than the literal 16–30°C range ever being hit in practice.

### 8.3 Human-override detection doesn't need its own state (clarification, good news)

Re-read "if a human changes mode or temperature, resume automated logic at
the next scheduled interval (not delayed by a full 30/60 min from the
moment of the change)" carefully: this doesn't actually require detecting
*that* a human made a change, or distinguishing a human's command from the
daemon's own last command. If the daemon simply reads live device state at
the start of every scheduled check (rather than trusting its last-known
in-memory state), a human's manual change is automatically picked up as the
new starting point for that check — with no separate "was this me or a
person" bookkeeping, and no special-cased delay logic. There is no delay to
avoid, because there was never a delay in the first place. **Recommendation:
implement it this way** — every check re-reads real device state first —
rather than building change-detection/attribution logic, which the spec
doesn't actually require and which would be one more place to get wrong.

The one piece of state that *is* still needed regardless: whether the
current mode/target differs from what was recorded at the last check, so
the daemon knows a mode change happened at all (human- or
automation-caused) and can decide whether to (re)start the 30-minute
post-mode-change adjustment suppression window. Recommend treating *any*
observed mode change — regardless of source — as starting that suppression
window, on the reasoning that re-adjusting temperature seconds after a
human just touched the unit would read as the automation fighting the
person, which is very unlikely to be the intent even though the spec only
discusses suppression in the context of the daemon's own mode changes.

### 8.4 Away-mode exit — decided: resume the schedule's target immediately

The spec says Away exit leaves units on "regardless of prior state," but
doesn't say what mode/target they should be *set to* on exit — only that
Away itself forces minimum-heat/10°C. Taken completely literally, exiting
Away could mean "stop forcing minimum-heat" and just... wait for the next
scheduled 30/60-minute check to notice and correct it, which could leave the
house at 10°C for up to an hour after someone gets home. That seems clearly
not the intent. **Recommendation:** Away exit should immediately apply
whatever the schedule's currently-active period specifies (mode + target),
not wait for the next periodic tick — this is the one state transition in
the whole design that should be immediate rather than governed by the
30/60-minute cadence, precisely because the state it's leaving (10°C) is
uncomfortable and was the automation's own doing, not a human's deliberate
choice to be respected until the next check.

### 8.5 Debounce vs. hysteresis on the 30/60-minute dwell windows (open question)

The thermostat is polled every 10 minutes, so each 30-minute window is ~3
samples and each 60-minute window is ~6. The spec doesn't say what happens
if room temp crosses back over house_target partway through a window (e.g.
oscillating right at the boundary). Two reasonable designs, with different
trade-offs:
- **Strict**: dwell timer resets the instant a single sample contradicts the
  condition (any wobble restarts the clock). Simple, but a room hovering
  exactly at the boundary could genuinely never accumulate 30/60 minutes,
  leaving the system stuck never adjusting even though it's persistently
  slightly off-target.
- **Hysteresis band**: only count a sample as "contradicting" if it crosses
  house_target by more than some small margin (e.g. 0.2°C), so sensor noise
  and normal minor fluctuation right at the setpoint doesn't perpetually
  reset the clock.
**Recommendation:** strict debounce (simpler, matches the spec's plain
reading, and 0.5°C adjustment steps are already coarse enough that boundary
flapping is unlikely to be a real problem in practice) — but call it out
explicitly in `hvac_decision_logic.py`'s docstring as a deliberate choice,
since it's exactly the kind of thing that's easy to get accidentally
inconsistent between the 30-minute and 60-minute checks if each is
implemented separately.

### 8.6 Startup default mode — decided: seed value, not a reset action

"Startup default mode: dry" is easy to misread as "force mode to dry every
time the daemon starts." Taken that way, a Pi reboot or a code-deploy
restart in the middle of winter would yank a correctly-running `heat` mode
back to `dry`, actively working against the room's own comfort for no
reason connected to anything the schedule or weather actually changed.
**Recommendation:** `dry` is the seed value written into
`hvac_automation_state.json` only when that file doesn't exist yet (a
genuinely first-ever run) — every other daemon in this repo already treats
its state file as authoritative across restarts (`hotwater_mode_daemon.py`,
`battery_mode_daemon.py`), and this should follow the same pattern rather
than being a special case.

### 8.7 Mode consistency between units — decided 2026-09-04: explicit invariant, immediate correction

The project owner flagged this directly: it's critical the two units are
never fighting each other (one heating while the other cools). This is
already **structurally guaranteed** — `set_mode()` (ported from
`heating_automation`'s `ACClient`, §6 step 2) always applies to both units
in a single operation, because they share one outdoor heat exchanger and
can physically only run one refrigerant direction at a time. But it needs
to be an explicit, *verified* property, not just an emergent one, for two
reasons:

1. **The spec's own retry/revert path is the one place they can transiently
   disagree.** "If a mode change succeeds on one unit but fails on the
   other: retry the failed unit immediately. If the retry also fails:
   revert the successful unit to its previous mode." Verification
   condition: after any retry/revert sequence completes, both units must
   report the same mode — never left split, even transiently beyond the
   retry window itself. Already listed as a required test scenario in §7.

2. **A human touching one unit's physical remote directly, independent of
   anything the daemon commanded, is not covered by the spec's "human
   override" section** — that section only discusses the daemon resuming
   control after a human's deliberate mode/temperature change, not one
   unit's mode silently diverging from the other. **Recommendation:** every
   poll reads both units' *actual* current mode (not the daemon's
   last-known/commanded state), and treats any Playroom/Landing mismatch as
   an immediate, high-priority correction — bypassing the normal 30/60-minute
   cadence entirely, since this is a materially different, higher-stakes
   situation than an ordinary "room hasn't hit target yet," and the
   fastest-possible response this design offers to any condition.

### 8.8 What's already correct: mode ceilings/floors themselves

One reassurance: `cool` and `heat` being the two ends of the normal 3-mode
cycle, with no wraparound and no escalation past either end, is already
sound and matches this project's other decision-logic modules' style
(`hotwater_decision_logic.py`'s dataclass-driven pure functions) — there's
no path in the spec's rules that cycles endlessly or reaches a mode with no
further transition out of it. The stuck-state risks are all in the
*temperature* ratchet (§8.2) and the *missing gate* (§8.1), not in the mode
state machine's shape itself.

## 9. Energy efficiency review

- **Dry-before-cool is already correct, and for a reason worth stating
  explicitly**: `dry` and `cool` share an *identical* target range (18–30°C)
  in the spec's own table — switching to `cool` never unlocks a colder
  achievable temperature than `dry` already allows, only a physically
  stronger (and more energy-hungry) mechanism to get there faster/further.
  Given the normal cycle always passes through `dry` before reaching `cool`,
  and (with §8.2's fix) only escalates past `dry` once `dry`'s own target
  has been pushed to its floor and the room still isn't responding, `cool`
  is only ever reached when `dry` has demonstrably failed to keep up — which
  is exactly the energy-conscious behaviour asked for. No change needed
  here beyond the §8.1/§8.2 fixes already recommended for other reasons.
- **"Don't heat when there's no need" is protected by the same dwell-time
  gates that prevent stuck states** — a 60-minute sustained-below-target
  requirement (§8.5's debounce) before any mode escalates toward `heat`
  means a transient dip can't trigger it, and once in `heat`, the daemon
  never demands more than `house_target` (+ §8.2's drift cap) asks for. The
  one real waste path is §8.2's windup: without the drift cap, a weather-
  limited cold snap can leave `heat` mode still demanding 30°C well after
  the room has caught up to the actually-desired temperature, burning energy
  for pure overshoot with no comfort benefit. §8.2's fix is the energy fix
  here, not a separate one.
- **`dry` mode physically can't heat** — worth being explicit about in
  `hvac_decision_logic.py`'s comments: the 30-minute "room < target →
  increase HVAC target" adjustment fires identically regardless of current
  mode, including in `dry`, even though raising `dry`'s setpoint when the
  room is cold does nothing physically (dry is a cooling/dehumidify
  function). The room just stays cold while the setpoint climbs uselessly
  toward `dry`'s own cap until mode escalation finally kicks in. §8.1/§8.2's
  fixes shorten this dead period considerably (drift-capped rather than
  ratcheting to 30°C) but don't eliminate it outright — an alternative worth
  a comment even if not built now: suppress the temperature-adjustment step
  entirely in `dry` mode when the *cold* direction is what's needed (it's
  still meaningful in the *warm* direction — reducing dry's target when
  room is too warm does help), and rely on the mode-escalation dwell timer
  alone to move to `heat`. Flagging as a possible future refinement rather
  than blocking on it now, since §8.2 already bounds the cost.
- **Away mode already chooses the efficient option** — forcing `minimum
  heat`/10°C rather than continuing to chase the normal schedule's target
  while nobody's home is itself the main energy-saving feature of the whole
  spec; no change needed.
- **The shared-outdoor-unit mode coupling (Upstairs mirrors Playroom) is an
  accepted hardware constraint, not an efficiency bug** — worth a one-line
  note only: even though mode is forced identical on both units, each
  indoor unit still modulates its own delivery independently against its
  own target (Upstairs's fixed, usually-lower 18°C vs. Playroom's schedule-
  driven target), so Upstairs will still idle/cycle back once it reaches its
  own target even while Playroom continues working harder toward a higher
  one under the same shared mode.

## 10. Impact on the existing battery/hot-water/dashboard subsystems

Checked this directly against the real files (not just by design intent),
since it's the difference between "should be fine" and "confirmed fine":

- **Config schema**: `config_manager.py`'s `CONFIG_SCHEMA` has no
  `additionalProperties: false` anywhere (grepped for it — zero matches), and
  `airstage`/`resideo` are already precedent for exactly this: documented in
  `"properties"` but absent from the top-level `"required"` list. New
  `hvac_automation:`/`thermostat:` sections are safe to add the same way —
  `jsonschema` accepts undeclared top-level keys by default, and even once
  declared, being additive/optional can't make an existing required section
  fail. No existing section's schema needs to change.
- **State files**: `state_store.py`'s locking is generic and keyed entirely
  by the path passed in — a new `config/hvac_automation_state.json` shares
  no file, lock, or in-memory state with `hotwater_automation_state.json` or
  the battery daemon's own state. No collision possible by construction.
- **Processes/services**: confirmed via the actual `.service` files — battery
  is `home_automation.service`, hot water is
  `home_automation_hotwater.service`, dashboard is
  `home_automation_dashboard.service`, Ohme is `home_automation_ohme.service`
  — four fully independent systemd units, none referencing each other. A new
  `home_automation_hvac.service` is a new unit file; none of the existing
  four need to change.
- **Shared modules** (`base_daemon.py`, `state_store.py`, `config_manager.py`):
  reused read-only, the same way `hotwater_mode_daemon.py` and
  `battery_mode_daemon.py` already both call into them without touching each
  other. This plan doesn't modify any of the three.
- **Dashboard**: `status_collector.py`'s `collect_status()` already collects
  every subsystem independently, each wrapped in its own `try/except` and
  its own `config.get(...).enabled` gate — a new `_collect_hvac_automation()`
  is one more entry in that same pattern; it can't affect
  `_collect_solar_battery`/`_collect_hot_water`/etc. even if it throws.
- **Dependencies**: `pyairstage`/`evohome-async` are already in
  `requirements.txt` (used read-only today); `aiohomekit` would be a new,
  additive line. `pymodbus` is pinned exactly (`==3.11.4`) after a past
  regression from a fresh install resolving newer — this plan doesn't touch
  that pin or any other existing version floor. Worth an explicit
  verification step at implementation time (`pip install` into a scratch
  venv, confirm nothing existing moves) rather than just assuming it's clean
  — not done yet, since no code has been written.

**One genuine, non-hypothetical shared-resource risk, not a hypothetical
one:** the dashboard's `StatusPoller` already polls the same two physical
Airstage units (Playroom/Landing) every 30 seconds
(`status_poll_interval_seconds`), and the new HVAC daemon would poll/write
those same two units on its own 10/30-minute cadence — two independent
processes occasionally hitting the same unit around the same moment.
pyairstage's own `get_devices()` carries a comment admitting the local
device's embedded HTTP server can return a "disconnected" error on
back-to-back requests even from a *single* process without a pause — so an
occasional cross-process collision is plausible, not paranoid. Consequence
if it happens: one transient "could not read from Airstage" on the
dashboard's card for that single 30-second poll (self-heals next poll,
`_collect_airstage`'s existing try/except already handles it, and it can't
blank the solar/battery/hot-water cards either way), or one retry on the
daemon's write path (already covered by the spec's own retry logic, §8).
Not a crash, not corrupted state, not a lasting effect on either subsystem —
but real enough to note rather than wave away, and worth keeping an eye on
in the daemon's logs once it's running for real.

**Net: nothing has actually changed yet** — only this planning document
exists so far (`docs/hvac_thermostat_automation_plan.md`); `config.yaml`,
`airstage_client.py`, `status_collector.py`, and every `.service` file are
untouched. Everything above is a review of what *would* happen once §6's
migration steps are carried out, not a report of changes already made.
