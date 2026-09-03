"""Executes the dashboard page's own JavaScript and checks what it renders.

src/dashboard/static_page.py is a single Python string holding the entire
page - HTML, CSS and ~450 lines of client-side JS. Coverage tools report
that file as 100% covered, which is an artefact of it being one string
assignment: none of the rendering logic inside it has ever run under a test.
Every change to it (including the solar-forecast "Yesterday" row) has been
verified only by loading the real page by hand.

This runs the real card renderers against realistic /api/status payloads
under node, with a DOM stub covering just what the render path touches, and
asserts on the HTML they produce. It's deliberately not a browser test: no
new heavyweight dependency, and node is already installed on both the Mac
and the Pi (it's how Claude Code itself runs there). If node is ever
missing, these skip rather than fail - the structural checks below still
run everywhere.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from src.dashboard.static_page import DASHBOARD_HTML

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _extract_page_script() -> str:
    """Pull the page's <script> body out of the served HTML."""
    scripts = re.findall(r"<script>(.*?)</script>", DASHBOARD_HTML, re.DOTALL)
    assert scripts, "the dashboard page should contain an inline <script> block"
    # The last block is the app code; any earlier ones would be setup.
    return scripts[-1]


# A minimal stand-in for the browser APIs the render path touches. escapeHtml
# builds an element and reads innerHTML back, which is the only DOM behaviour
# the pure render functions actually depend on.
DOM_STUB = """
const __els = {};
function __mkEl() {
  return {
    _text: "",
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
    get innerHTML() {
      return this._text
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    },
    set innerHTML(v) { this._html = v; },
    style: {},
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener() {},
  };
}
global.document = {
  createElement: __mkEl,
  getElementById(id) { return (__els[id] = __els[id] || __mkEl()); },
};
global.setInterval = () => 0;
global.fetch = () => Promise.reject(new Error("no network in tests"));
"""


def _run_js(snippet: str) -> str:
    """Run the page's script plus a snippet under node, return stdout."""
    program = "\n".join([DOM_STUB, _extract_page_script(), snippet])
    result = subprocess.run(
        [NODE, "-e", program], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr}"
    return result.stdout


# --- The page's JS is at least syntactically valid -------------------------


@requires_node
def test_the_page_script_parses_and_loads():
    """A syntax error anywhere in the page's JS blanks the entire dashboard -
    every card, not just the broken one."""
    assert _run_js("console.log('loaded');").strip() == "loaded"


# --- Formatting helpers ----------------------------------------------------


@requires_node
def test_formatting_helpers_handle_missing_values():
    """Every field on the status API can legitimately be null when a
    subsystem is degraded; none of them may render as "null" or "NaN"."""
    out = _run_js(
        """
        console.log(JSON.stringify({
          power: fmtPower(1234.6), powerNull: fmtPower(null),
          temp: fmtTemp(45.64), tempNull: fmtTemp(null),
          pct: fmtPct(62.4), pctNull: fmtPct(undefined),
          agePast: fmtAge(3600), ageNow: fmtAge(10),
          signedPos: fmtSignedKwh(2.04), signedNeg: fmtSignedKwh(-2.04),
          signedNull: fmtSignedKwh(null),
        }));
        """
    )
    values = json.loads(out)
    assert values["power"] == "1235 W"
    assert values["temp"] == "45.6&deg;C"
    assert values["pct"] == "62%"
    assert values["agePast"] == "1h ago"
    assert values["ageNow"] == "just now"
    assert values["signedPos"] == "+2.0 kWh"
    assert values["signedNeg"] == "-2.0 kWh"
    for key in ("powerNull", "tempNull", "pctNull"):
        assert values[key] == "&mdash;"
    assert values["signedNull"] == ""


@requires_node
def test_soc_colour_thresholds():
    """Red below 20%, amber below 50%, green above - the convention the whole
    page's "fuller is better" metrics share."""
    out = _run_js("console.log(JSON.stringify([socColor(10), socColor(35), socColor(80)]));")
    low, mid, high = json.loads(out)
    assert low != mid != high
    assert "--bad" in low
    assert "--warn" in mid
    assert "--good" in high


# --- Card rendering --------------------------------------------------------


@requires_node
def test_solar_forecast_card_renders_the_retrospective_error():
    """The "Yesterday: 40.0 kWh (+2.0 kWh vs forecast)" row - shipped with no
    automated coverage at all until now."""
    payload = {
        "available": True,
        "today_kwh": 42.3,
        "tomorrow_kwh": 38.1,
        "yesterday_actual_kwh": 40.0,
        "yesterday_error_kwh": 2.0,
        "yesterday_forecast_kwh": 38.0,
        "current_weather": {"description": "Clear", "temperature_c": 17.9},
        "model_trained_at": "2026-09-01T18:33:09Z",
    }
    html = _run_js(f"console.log(solarForecastCard({json.dumps(payload)}));")

    assert "Yesterday" in html
    assert "40.0 kWh" in html
    assert "+2.0 kWh vs forecast" in html
    assert "42.3 kWh" in html  # today
    assert "38.1 kWh" in html  # tomorrow


@requires_node
def test_solar_forecast_card_omits_yesterday_when_there_is_no_actual():
    """Before the history logger has a full day, the row must disappear
    rather than render an em-dash or a bogus zero."""
    payload = {
        "available": True,
        "today_kwh": 42.3,
        "tomorrow_kwh": 38.1,
        "yesterday_actual_kwh": None,
        "yesterday_error_kwh": None,
        "current_weather": None,
    }
    html = _run_js(f"console.log(solarForecastCard({json.dumps(payload)}));")

    assert "Yesterday" not in html
    assert "42.3 kWh" in html


@requires_node
def test_unavailable_subsystem_renders_an_error_not_a_broken_card():
    payload = {"available": False, "error": "Could not read from SolaX inverter(s)"}
    html = _run_js(f"console.log(solarCard({json.dumps(payload)}));")

    assert "Unavailable" in html
    assert "Could not read from SolaX inverter(s)" in html


@requires_node
def test_disabled_subsystem_renders_nothing_at_all():
    """A subsystem that isn't configured is omitted, not shown as broken."""
    payload = {"available": False, "disabled": True, "error": "disabled in config.yaml"}
    assert _run_js(f"console.log(JSON.stringify(evCard({json.dumps(payload)})));").strip() == '""'


@requires_node
def test_service_health_card_marks_an_unhealthy_daemon():
    payload = {
        "available": True,
        "services": [
            {"key": "battery_daemon", "label": "Battery Daemon", "health_status": "healthy",
             "installed": True, "active": True, "active_state": "active", "log_age_seconds": 30},
            {"key": "ohme_poller", "label": "Ohme Poller", "health_status": "unhealthy",
             "installed": True, "active": True, "active_state": "active", "log_age_seconds": 30},
        ],
    }
    html = _run_js(f"console.log(serviceHealthCard({json.dumps(payload)}));")

    assert "Battery Daemon" in html
    assert "Ohme Poller" in html
    assert "Unhealthy" in html


@requires_node
def test_service_health_card_puts_log_age_next_to_the_status_badge():
    """Saves vertical space: the log-age reading sits inline next to the
    status badge on the service's own row, not on a separate row below it.
    """
    payload = {
        "available": True,
        "services": [
            {"key": "battery_daemon", "label": "Battery Daemon", "health_status": "healthy",
             "installed": True, "active": True, "active_state": "active", "log_age_seconds": 30},
        ],
    }
    html = _run_js(f"console.log(serviceHealthCard({json.dumps(payload)}));")

    assert "Last log activity" not in html
    assert 'class="subtext"' in html
    # Same row: no row boundary between the label and the badge.
    row_start = html.index('<div class="row">')
    row_end = html.index("</div>", html.index("badge", row_start))
    row_html = html[row_start:row_end]
    assert "Battery Daemon" in row_html
    assert "subtext" in row_html
    assert "badge" in row_html


@requires_node
def test_hot_water_card_surfaces_an_active_legionella_cycle():
    payload = {
        "available": True,
        "tank_temperature_c": 58.0,
        "target_tank_temperature_c": 60.0,
        "status": "heating",
        "force_heat_active": True,
        "legionella_cycle_in_progress": True,
        "automation_holiday_active": False,
        "power_on": True,
        "holiday_mode": False,
    }
    html = _run_js(f"console.log(hotWaterCard({json.dumps(payload)}));")

    assert "58.0&deg;C" in html
    assert "Legionella cycle" in html
    assert "Force heat" in html


@requires_node
def test_escape_html_neutralises_markup_from_the_api():
    """Device/vehicle names come from third-party APIs and land in innerHTML."""
    out = _run_js("""console.log(escapeHtml('<img src=x onerror=alert(1)>'));""")
    assert "<img" not in out
    assert "&lt;img" in out


# --- Structural checks (no node needed) ------------------------------------


def test_page_declares_the_elements_its_script_updates():
    """refresh() writes into these by id; a rename on one side only would
    leave the page permanently stuck on "loading...."."""
    for element_id in ("cards", "updated", "stale-banner"):
        assert f'id="{element_id}"' in DASHBOARD_HTML


def test_page_has_no_external_resource_references():
    """The page must keep working when the Pi's internet is down - that's why
    the CSS/JS are inlined rather than pulled from a CDN."""
    assert "http://" not in DASHBOARD_HTML
    assert "https://" not in DASHBOARD_HTML
