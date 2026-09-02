# Pi4 Migration — Staging Plan

Runbook for moving this repo from the Mac (development) to a Raspberry Pi 4
(production, 24/7). Written for a Claude instance to execute — either
running on the Mac and driving the Pi over SSH, or running directly on the
Pi. Every step is designed to be validated with fakes/mocks/dry-runs before
anything touches real hardware or goes live under systemd/cron.

Status as of 2026-08-31: no Pi deployment has happened yet for the newer
subsystems (hotwater, battery-evening-prediction, dashboard, solar-forecast,
airstage). Only `battery_mode_daemon.py` has a known-good Pi history
(`requirements.txt`'s "Pi:" version comments, `scripts/home_automation.service`).

## 0. Prerequisites before touching the Pi

This repo currently has a large amount of **uncommitted** work in the
working tree (`git status`), including a subsystem still mid-edit in a
parallel session:

- `src/api_clients/resideo_client.py` currently has a broken import
  (`get_resideo_token_state_path` doesn't exist yet in `src/utils/paths.py`)
  and isn't wired into `config.yaml` or the dashboard. **Exclude it from
  this migration** — it's early WIP with no consumers yet, not a regression
  in anything that matters here.
- Everything else uncommitted (hotwater automation, battery evening
  prediction, the shared state store/daemon base, the dashboard, the solar
  forecast trainer/predictor, the airstage client) is finished and covered
  by tests — confirmed by running the full suite (see step 6) — but is
  still sitting as uncommitted changes, not a clean commit.

**Decide and do one of these before migrating** (a user decision, not
Claude's to make silently):
1. Commit the finished work (everything except `resideo_client.py` and its
   test file) to `main` first, so the Pi installs from a clean git state
   via `git clone`/`git pull` rather than a raw file copy of a dirty tree.
2. Or explicitly accept doing a raw file copy (rsync) of the current working
   tree, dirty state and all, and skip only the broken resideo file by name.

Confirm with the user which of these before proceeding past step 0.

## 1. What ships to the Pi, and how each piece runs

| Component | Entry point | Run mode | Notes |
|---|---|---|---|
| Battery mode management | `scripts/battery_mode_daemon.py` | systemd (continuous) | Existing unit: `scripts/home_automation.service`. Already Pi-proven. |
| Hot water force-heat + legionella | `scripts/hotwater_mode_daemon.py` | systemd (continuous) | Needs a *continuous* process — the "EV started charging" trigger can happen any time. Do **not** also cron `scripts/hotwater_auto_check.py` — it's the same core logic, one-shot; pick one, not both. |
| Dashboard (read-only status page) | `scripts/dashboard_server.py` | systemd (continuous) | Flask dev server, LAN-only by design (`web_interface.host: 0.0.0.0`, port 8000). Read-only: never calls a mode-change function. |
| SolaX Cloud historical data logger | `scripts/solax_cloud_data_logger.py` | cron | No cadence documented in-file (older script); recommend once nightly — it's incremental, and only feeds the evening predictor + solar-forecast trainer, neither of which need finer granularity. Needs **real** SolaX Cloud API credentials (`token_id`/`wifisn`) — currently `config.yaml` has these as `"NOT_USED_FOR_MODBUS"` placeholders, which is correct for Modbus but means this logger won't authenticate until real cloud credentials are filled in. |
| Battery evening SoC predictor | `scripts/battery_evening_predictor.py` | cron, `55 20 * * *` | Docstring-documented cadence. Feeds `hotwater_automation_core.py`'s force-heat decision — needs to land before `hotwater_automation.trigger_hour`. When `solar_forecast.enabled` is true it also reads that subsystem's forecast file to correct its prediction — order after the solar forecast predictor's hourly cron entry below so a same-run-cycle forecast is available, though it degrades gracefully to the plain historical average if one isn't. |
| Solar forecast trainer | `scripts/solar_forecast_trainer.py` | cron, `0 3 * * 0` (weekly) | Docstring-documented. Writes `data/solar_forecast_model.joblib`. Must run at least once before the predictor has a model to load. |
| Solar forecast predictor | `scripts/solar_forecast_predictor.py` | cron, `0 * * * *` (hourly) | Docstring-documented. Display-only — dashboard reads its output, nothing automation-critical depends on it. |
| Manual/on-demand CLI tools | `solax_modbus_status_report.py`, `solax_modbus_read_and_set_workmode.py`, `ohme_ev_control.py`, `melcloud_hotwater_control.py`, `power_usage_analysis.py` | manual only | No daemon/cron needed. Useful for step 7's live-connectivity checks. |

Airstage (heat pump) has no daemon of its own — it's read-only status
consumed by the dashboard (`src/dashboard/status_collector.py`) only, gated
behind `config.yaml`'s `airstage.enabled`.

## 2. Packages to install on the Pi

`requirements.txt` is already accurate — cross-checked by AST-scanning every
import in `src/` and `scripts/` against it; nothing third-party is missing.
Standard install:

```bash
cd /home/pi/home_automation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Notes:
- `requirements.txt` already carries "Pi:" version comments for most
  packages (from a prior Pi install) — e.g. `pymodbus 3.11.4`, `aiohttp
  3.13.3`, `ohme 1.6.0`. A fresh resolve may land higher; that's fine except
  for `aioresponses` (test-only), which needs `aiohttp==3.13.3` specifically
  (see `requirements-dev.txt`'s comment) or the test suite's HTTP-mocking
  breaks — this only affects running tests, not production.
- `scikit-learn` (for the solar forecast trainer) pulls in `numpy`/`scipy`/
  `joblib` transitively. On Pi4/Raspberry Pi OS, make sure pip is resolving
  against **piwheels** (usually already configured by default on Raspberry
  Pi OS) so these install as prebuilt ARM wheels — building from source on a
  Pi4 can take a very long time.
- `pymelcloud` has no "Pi:" comment yet — it's never been run there. Flag
  this as the one dependency without prior Pi validation; step 6/7 covers
  catching any surprise there before going live.

## 3. What to copy vs. regenerate vs. leave alone

**Code**: per the decision in step 0 — either `git clone`/`git pull`, or
`rsync` the working tree excluding `venv/`, `__pycache__/`, `logs/` (see
`.gitignore` for the full exclusion list already in place).

**Secrets — copy manually, never via git in plaintext** (`secrets.yaml` is
gitignored by design; see `secrets.yaml.example`):
```bash
scp secrets.yaml pi@<pi-ip>:/home/pi/home_automation/secrets.yaml
```
There's also an encrypted backup checked into git (`secrets.yaml.enc`), for
the case where neither this Mac nor the Pi survives to be the `scp` source.
Run `bash scripts/encrypt_secrets.sh` after any edit to `secrets.yaml` and
commit the result; `setup_pi.sh` offers to decrypt it automatically on a
fresh install (or run `bash scripts/decrypt_secrets.sh` manually) using the
same passphrase you set when encrypting.

**`config.yaml` — copy, then review these fields for the new machine**:
- `solaX_cloud_api.master_ip` / `slave_ip` — same LAN, should be unchanged,
  but confirm the Pi is actually on that same subnet.
- `airstage.ip_address` / `device_id` — same reasoning.
- `web_interface.host`/`port` — `0.0.0.0:8000` is already Pi-appropriate
  (LAN-wide, not internet-facing unless separately port-forwarded).
- `solar_forecast.enabled` and `location.latitude`/`longitude` — only
  meaningful once real coordinates are set (currently `0.0`/`0.0` disables
  it per the schema's own comment).
- `solaX_cloud_api.token_id`/`master_wifisn`/`slave_wifisn` — leave as
  `"NOT_USED_FOR_MODBUS"` placeholders **unless** you're also enabling
  `solax_cloud_data_logger.py`, in which case these need real values.

**Historical data / model artifacts — decide on continuity vs. fresh start**:
- `data/solax_historical_data.json` — copying it gives the evening predictor
  and solar-forecast trainer months of history immediately; starting fresh
  means the evening predictor has no "analog day" data until the logger
  accumulates enough, and the solar forecast model trains on much less data.
  Recommend copying it — it's read-only input, low risk either way.
- `data/solar_forecast_model.joblib` — don't copy; regenerate on the Pi by
  running `solar_forecast_trainer.py` there once (see step 7), so the model
  reflects whatever historical data actually ended up on the Pi.

**Runtime state files under `config/` (all gitignored, all safe to start
fresh — each one's absence just means "no prior state", and every consumer
is written to treat that as a safe default)**:
- `config/solax_mode_change_log.json` — missing means no interval
  restriction applies to the *next* write (not "no restriction ever" —
  it starts tracking from that point).
- `config/hotwater_automation_state.json` — missing means "not currently
  force-heating, no legionella cycle in progress" — the correct safe
  default.
- `config/battery_evening_prediction.json` — missing means the force-heat
  check falls back to a live SoC reading instead of the evening forecast
  (documented fallback, not a crash).
- `config/solar_forecast.json` — missing means the dashboard shows no solar
  forecast until the predictor's next cron run.
Starting these fresh on the Pi (i.e., *not* copying them) is the simpler and
recommended default — continuity here buys little and copying a stale mode-
change-log timestamp from a different machine's clock is a needless risk to
reason about.

**`logs/` and `cache/`** — don't copy; both are regenerated automatically
(`logs/` via `Path("logs").mkdir(exist_ok=True)` in
`src/daemon_support/base_daemon.py`, rotating with 7-day retention built in;
`cache/` similarly). Note `.gitignore`'s comment that `logs/` is expected to
be a symlink on the target machine, not a plain directory — that's optional
(e.g. if you want logs on external storage to spare the Pi's SD card), skip
it if you don't need it.

## 4. systemd units

`scripts/home_automation.service` already exists for the battery daemon and
is Pi-proven — reuse its pattern for the two other continuous processes.
Battery's existing unit:

```ini
[Unit]
Description=Home Automation System
After=network.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/home/pi/home_automation
ExecStart=/home/pi/home_automation/venv/bin/python /home/pi/home_automation/scripts/battery_mode_daemon.py /home/pi/home_automation/battery_mode_daemon_config.json
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Create two more, same pattern, different `ExecStart`:
- `home_automation_hotwater.service` → `ExecStart=.../venv/bin/python .../scripts/hotwater_mode_daemon.py` (no JSON config arg — it reads `config.yaml` directly).
- `home_automation_dashboard.service` → `ExecStart=.../venv/bin/python .../scripts/dashboard_server.py`.

Enable all three: `sudo systemctl enable --now home_automation.service home_automation_hotwater.service home_automation_dashboard.service`.

## 5. cron entries

Add via `crontab -e` (as the `pi` user, so relative-to-cwd behavior like
`logs/` creation matches systemd's `WorkingDirectory`):

```cron
# Solar PV historical data (feeds evening predictor + solar forecast trainer)
15 2 * * * cd /home/pi/home_automation && venv/bin/python3 scripts/solax_cloud_data_logger.py

# Evening battery SoC prediction — must land before hotwater_automation.trigger_hour
55 20 * * * cd /home/pi/home_automation && venv/bin/python3 scripts/battery_evening_predictor.py --quiet

# Solar forecast model retrain — weekly is enough per the script's own docstring
0 3 * * 0 cd /home/pi/home_automation && venv/bin/python3 scripts/solar_forecast_trainer.py --quiet

# Solar forecast prediction refresh — hourly, display-only
0 * * * * cd /home/pi/home_automation && venv/bin/python3 scripts/solar_forecast_predictor.py --quiet
```

Sanity-check the crontab's `PATH`/`HOME` aren't needed here since every
command already `cd`s into the project and uses an absolute venv path.

## 6. Pre-go-live validation (do this before enabling any systemd unit or cron entry)

The test suite is already built for exactly this — every hardware/cloud
integration point is either a real local Modbus TCP fake server
(`tests/api_clients/solax_fake_server.py`) or HTTP-layer-mocked via
`aioresponses` (MELCloud, Ohme). None of it touches real hardware, so it's
safe to run for real on the Pi:

```bash
cd /home/pi/home_automation
source venv/bin/activate
pytest -q
```

Confirmed on the Mac (this session, 2026-08-31): **370 passed**, excluding
only the in-progress `resideo_client.py` (step 0) — includes the dashboard,
solar-forecast, and airstage subsystems, all green. Re-running this exact
command on the Pi is the real ARM-architecture check — it's the only way to
catch anything `pymelcloud`'s never-tested-on-Pi status (step 2) or any
ARM-specific wheel issue might surface, before it matters.

If anything fails on the Pi that passed on the Mac, stop and diagnose before
proceeding — don't route around a test failure to get to "live" faster.

## 7. Live-hardware smoke test (still before enabling daemons/cron)

Once the venv is proven and secrets/config are in place, use the read-only
manual CLI tools to confirm the Pi can actually reach everything, with zero
risk (all read-only or explicitly dry-run):

```bash
# SolaX inverters reachable over Modbus from the Pi's LAN position
python3 scripts/solax_modbus_status_report.py

# Ohme cloud reachable, credentials valid
python3 scripts/ohme_ev_control.py --status

# MELCloud reachable, credentials valid
python3 scripts/melcloud_hotwater_control.py --status

# Hot water automation's own config validates end-to-end
python3 scripts/hotwater_auto_check.py --dry-run

# Battery daemon's decision logic without writing any mode (check for a
# --dry-run flag on battery_mode_daemon.py; confirm before relying on it)
```

Only proceed to step 4/5 (enabling daemons and cron) once every one of these
succeeds against the real hardware from the Pi's network position.

## 8. Cutover and rollback

- Enable the three systemd units and the four cron entries (steps 4–5).
- Watch `journalctl -u home_automation.service -f` (and the two other unit
  names) plus `logs/*.log` for the first real cycle of each daemon.
- If anything misbehaves: `sudo systemctl stop <unit>` immediately stops
  that daemon without affecting the others (each is independent — the
  dashboard, battery daemon, and hotwater daemon never share state or
  process boundaries). Comment out the relevant cron line to pause a
  scheduled job.
- Nothing here is one-way: config/state files are all safe to delete and
  let regenerate (step 3), so a full reset on the Pi is always available if
  needed.
