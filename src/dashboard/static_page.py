"""The dashboard's single HTML page - inline CSS/JS, no external assets.

No CDN dependency on purpose: the page must keep showing the last cached
solar/battery reading even if the Pi's internet connection (which MELCloud/
Ohme need, but the SolaX Modbus reads and the LAN itself don't) is down.
"""

from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Home Automation</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f2f2f7;
    --card-bg: #ffffff;
    --text: #1c1c1e;
    --muted: #6e6e73;
    --accent: #007aff;
    --good: #34c759;
    --warn: #ff9500;
    --bad: #ff3b30;
    --border: rgba(60,60,67,0.15);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #000000;
      --card-bg: #1c1c1e;
      --text: #f2f2f7;
      --muted: #98989d;
      --border: rgba(255,255,255,0.12);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 16px 16px 40px;
    padding-top: max(16px, env(safe-area-inset-top));
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 16px;
  }
  h1 { font-size: 22px; margin: 0; }
  #updated { font-size: 13px; color: var(--muted); }
  #cards {
    display: grid;
    grid-template-columns: 1fr;
    gap: 14px;
  }
  @media (min-width: 700px) {
    #cards { grid-template-columns: repeat(2, 1fr); }
  }
  @media (min-width: 1100px) {
    #cards { grid-template-columns: repeat(3, 1fr); gap: 10px; }
    body { padding: 10px 18px 18px; padding-top: max(10px, env(safe-area-inset-top)); }
    header { margin-bottom: 8px; }
    .card { padding: 12px 14px; }
    .card h2 { margin: 0 0 6px; }
    .ring-row { padding: 2px 0 6px; gap: 10px; }
    .row { padding: 3px 0; font-size: 15px; }
    .row .label { font-size: 13px; }
    .bar-track { margin-top: 4px; }
  }
  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px;
  }
  .card.has-details { cursor: pointer; }
  .card h2 {
    font-size: 15px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    margin: 0 0 12px;
    display: flex;
    justify-content: space-between;
  }
  .expand-hint {
    color: var(--muted);
    font-size: 12px;
    transition: transform 0.15s ease;
  }
  .card.expanded .expand-hint { transform: rotate(180deg); }
  .details {
    display: none;
    margin-top: 4px;
    padding-top: 4px;
    border-top: 1px dashed var(--border);
  }
  .card.expanded .details { display: block; }
  .ring-row {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 4px 0 10px;
  }
  .ring-row .ring-label { font-size: 15px; color: var(--muted); line-height: 1.4; }
  .ring-row .ring-label strong { display: block; font-size: 17px; color: var(--text); }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 0;
    font-size: 17px;
  }
  .row + .row { border-top: 1px solid var(--border); }
  .row .label { color: var(--muted); font-size: 15px; }
  .row .value { font-weight: 600; text-align: right; }
  .subtext { color: var(--muted); font-size: 13px; font-weight: 400; margin-right: 8px; }
  .badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 13px;
    font-weight: 600;
    color: white;
    background: var(--accent);
  }
  .badge.good { background: var(--good); }
  .badge.warn { background: var(--warn); }
  .badge.bad { background: var(--bad); }
  .bar-track {
    background: var(--border);
    border-radius: 6px;
    height: 10px;
    overflow: hidden;
    margin-top: 6px;
  }
  .bar-fill { height: 100%; background: var(--good); border-radius: 6px; }
  .error-text { color: var(--bad); font-size: 15px; }
  .value .delta { color: var(--muted); font-weight: 400; font-size: 13px; margin-left: 6px; }
  .stale-banner {
    display: none;
    background: var(--warn);
    color: white;
    padding: 8px 14px;
    border-radius: 10px;
    font-size: 14px;
    margin-bottom: 14px;
  }
</style>
</head>
<body>
<header>
  <h1>Home Automation</h1>
  <div id="updated">loading&hellip;</div>
</header>
<div id="stale-banner" class="stale-banner">Data may be out of date - dashboard hasn't refreshed recently</div>
<div id="cards"></div>

<script>
const POLL_MS = 15000;
const STALE_AFTER_S = 180;

function fmtPower(w) {
  if (w === null || w === undefined) return "&mdash;";
  return `${Math.round(w)} W`;
}
function fmtTemp(c) {
  if (c === null || c === undefined) return "&mdash;";
  return `${c.toFixed(1)}&deg;C`;
}
function fmtPct(p) {
  if (p === null || p === undefined) return "&mdash;";
  return `${Math.round(p)}%`;
}
function fmtSignedKwh(kwh) {
  if (kwh === null || kwh === undefined) return "";
  const sign = kwh > 0 ? "+" : "";
  return `${sign}${kwh.toFixed(1)} kWh`;
}
function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return "&mdash;";
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(seconds / 3600);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}
function titleCase(s) {
  if (!s) return "Unknown";
  return s.replace(/_/g, " ").replace(/\\w\\S*/g, t => t[0].toUpperCase() + t.slice(1).toLowerCase());
}
function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function socColor(pct) {
  return pct < 20 ? "var(--bad)" : pct < 50 ? "var(--warn)" : "var(--good)";
}

function socBar(pct) {
  if (pct === null || pct === undefined) return "";
  return progressBar(pct, socColor(pct));
}

function card(title, bodyHtml, detailsHtml) {
  const hasDetails = !!detailsHtml;
  return `<div class="card${hasDetails ? " has-details" : ""}">
    <h2>${title}${hasDetails ? '<span class="expand-hint">&#9662;</span>' : ""}</h2>
    ${bodyHtml}
    ${hasDetails ? `<div class="details">${detailsHtml}</div>` : ""}
  </div>`;
}

function unavailableCard(title, error, isDisabled) {
  if (isDisabled) return "";  // Not configured yet - omit the card entirely rather than clutter the page
  return `<div class="card"><h2>${title}</h2><div class="error-text">Unavailable${error ? ": " + escapeHtml(error) : ""}</div></div>`;
}

// Progress ring/bar colour conventions used across cards:
//  - "fuller is better" metrics (battery charge): red<20%, orange<50%, else green.
//  - "fuller is worse" metrics (Claude usage): green<60%, orange<90%, else red.
function usageColor(percentUsed) {
  return percentUsed >= 90 ? "var(--bad)" : percentUsed >= 60 ? "var(--warn)" : "var(--good)";
}

function progressBar(percent, color) {
  const clamped = Math.max(0, Math.min(100, percent));
  return `<div class="bar-track"><div class="bar-fill" style="width:${clamped}%; background:${color}"></div></div>`;
}

function progressRing(percent, color, size = 72) {
  const strokeWidth = 8;
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.max(0, Math.min(100, percent));
  const offset = circumference * (1 - clamped / 100);
  const center = size / 2;
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <circle cx="${center}" cy="${center}" r="${radius}" stroke="var(--border)" stroke-width="${strokeWidth}" fill="none"/>
    <circle cx="${center}" cy="${center}" r="${radius}" stroke="${color}" stroke-width="${strokeWidth}" fill="none"
      stroke-dasharray="${circumference}" stroke-dashoffset="${offset}" stroke-linecap="round"
      transform="rotate(-90 ${center} ${center})"/>
    <text x="${center}" y="${center + 5}" text-anchor="middle" font-size="16" font-weight="700" fill="var(--text)">${Math.round(clamped)}%</text>
  </svg>`;
}

function solarCard(d) {
  if (!d.available) return unavailableCard("Solar &amp; Battery", d.error, d.disabled);
  const gridLabel = (d.grid_power_w ?? 0) >= 0 ? "Exporting" : "Importing";
  const socAvg = [d.soc_percent_master, d.soc_percent_slave].filter(v => v !== null && v !== undefined);
  const soc = socAvg.length ? socAvg.reduce((a, b) => a + b, 0) / socAvg.length : null;
  const body = `
    <div class="row"><span class="label">Mode</span><span class="value"><span class="badge">${escapeHtml(d.work_mode || "Unknown")}</span></span></div>
    <div class="row"><span class="label">Solar (PV) now</span><span class="value">${fmtPower(d.pv_power_w)}</span></div>
    <div class="row"><span class="label">Grid</span><span class="value">${gridLabel} ${fmtPower(Math.abs(d.grid_power_w ?? 0))}</span></div>
    <div class="row"><span class="label">Battery flow</span><span class="value">${fmtPower(d.battery_power_w)}</span></div>
    <div class="row"><span class="label">Battery charge</span><span class="value">${fmtPct(soc)}</span></div>
    ${socBar(soc)}
    <div class="row"><span class="label">Today's generation</span><span class="value">${d.daily_yield_kwh !== null && d.daily_yield_kwh !== undefined ? d.daily_yield_kwh.toFixed(1) + " kWh" : "&mdash;"}</span></div>
  `;
  const details = d.last_mode_change_at ? `
    <div class="row"><span class="label">Last mode change</span><span class="value">${escapeHtml(d.last_mode_change_reason || "")}</span></div>
    <div class="row"><span class="label">When</span><span class="value">${escapeHtml(d.last_mode_change_at)}</span></div>
  ` : "";
  return card("Solar &amp; Battery", body, details);
}

function evCard(d) {
  if (!d.available) return unavailableCard("EV Charging", d.error, d.disabled);
  const statusClass = d.status === "charging" ? "good" : d.status === "plugged_in" ? "warn" : "";
  const body = `
    <div class="row"><span class="label">Status</span><span class="value"><span class="badge ${statusClass}">${escapeHtml(titleCase(d.status))}</span></span></div>
    <div class="row"><span class="label">Plugged in</span><span class="value">${d.plugged_in ? "Yes" : "No"}</span></div>
    <div class="row"><span class="label">Charging power</span><span class="value">${fmtPower(d.power_watts)}</span></div>
    <div class="row"><span class="label">Car battery</span><span class="value">${fmtPct(d.battery_percent)}</span></div>
    <div class="row"><span class="label">Target</span><span class="value">${fmtPct(d.target_soc)}</span></div>
    ${d.current_vehicle ? `<div class="row"><span class="label">Vehicle</span><span class="value">${escapeHtml(d.current_vehicle)}</span></div>` : ""}
  `;
  return card("EV Charging", body, "");
}

function hotWaterCard(d) {
  if (!d.available) return unavailableCard("Hot Water", d.error, d.disabled);
  const body = `
    <div class="row"><span class="label">Tank temperature</span><span class="value">${fmtTemp(d.tank_temperature_c)}</span></div>
    <div class="row"><span class="label">Target</span><span class="value">${fmtTemp(d.target_tank_temperature_c)}</span></div>
    <div class="row"><span class="label">Activity</span><span class="value">${escapeHtml(titleCase(d.status))}</span></div>
    ${d.force_heat_active ? `<div class="row"><span class="label">Force heat</span><span class="value"><span class="badge warn">Active</span></span></div>` : ""}
    ${d.legionella_cycle_in_progress ? `<div class="row"><span class="label">Legionella cycle</span><span class="value"><span class="badge warn">In progress</span></span></div>` : ""}
    ${d.automation_holiday_active ? `<div class="row"><span class="label">Automation holiday</span><span class="value"><span class="badge warn">Active until ${escapeHtml(d.automation_holiday_until ?? "")}</span></span></div>` : ""}
  `;
  const details = `
    <div class="row"><span class="label">Power</span><span class="value">${d.power_on ? "On" : "Off"}</span></div>
    <div class="row"><span class="label">Device holiday mode</span><span class="value">${d.holiday_mode ? "On" : "Off"}</span></div>
    ${d.force_heat_activated_at ? `<div class="row"><span class="label">Force heat since</span><span class="value">${escapeHtml(d.force_heat_activated_at)}</span></div>` : ""}
    ${d.legionella_last_completed_at ? `<div class="row"><span class="label">Last legionella cycle</span><span class="value">${escapeHtml(d.legionella_last_completed_at)}</span></div>` : ""}
  `;
  return card("Hot Water", body, details);
}

function airstageCard(d) {
  if (!d.available) return unavailableCard("Air Conditioning", d.error, d.disabled);
  return d.zones.map(airstageZoneCard).join("");
}

function airstageZoneCard(zone) {
  const title = `Air Conditioning - ${escapeHtml(zone.name)}`;
  if (!zone.available) return unavailableCard(title, zone.error, zone.disabled);

  const modeClass = zone.mode === "OFF" ? "" : "good";
  const body = `
    <div class="row"><span class="label">Mode</span><span class="value"><span class="badge ${modeClass}">${escapeHtml(titleCase(zone.mode))}</span></span></div>
    <div class="row"><span class="label">Room temperature</span><span class="value">${fmtTemp(zone.current_temperature_c)}</span></div>
    <div class="row"><span class="label">Target</span><span class="value">${fmtTemp(zone.target_temperature_c)}</span></div>
  `;
  const details = zone.outdoor_temperature_c !== null && zone.outdoor_temperature_c !== undefined
    ? `<div class="row"><span class="label">Outdoor temperature</span><span class="value">${fmtTemp(zone.outdoor_temperature_c)}</span></div>`
    : "";
  return card(title, body, details);
}

function resideoCard(d) {
  if (!d.available) return unavailableCard("Thermostat", d.error, d.disabled);
  const modeClass = d.mode === "off" ? "" : "good";
  const callingClass = d.calling_for_heat ? "good" : "";
  const body = `
    <div class="row"><span class="label">Mode</span><span class="value"><span class="badge ${modeClass}">${escapeHtml(titleCase(d.mode))}</span></span></div>
    <div class="row"><span class="label">Calling for heat</span><span class="value"><span class="badge ${callingClass}">${d.calling_for_heat ? "Yes" : "No"}</span></span></div>
    <div class="row"><span class="label">Current</span><span class="value">${fmtTemp(d.current_temperature_c)}</span></div>
    ${d.target_temperature_c !== null && d.target_temperature_c !== undefined ? `<div class="row"><span class="label">Target</span><span class="value">${fmtTemp(d.target_temperature_c)}</span></div>` : ""}
  `;
  const title = "Thermostat" + (d.device_name ? " - " + escapeHtml(d.device_name) : "");
  return card(title, body, "");
}

function solarForecastCard(d) {
  if (!d.available) return unavailableCard("Solar Forecast", d.error, d.disabled);
  const w = d.current_weather;
  const hasYesterday = d.yesterday_actual_kwh !== null && d.yesterday_actual_kwh !== undefined;
  const yesterdayDelta = hasYesterday && d.yesterday_error_kwh !== null && d.yesterday_error_kwh !== undefined
    ? `<span class="delta">(${fmtSignedKwh(d.yesterday_error_kwh)} vs forecast)</span>`
    : "";
  const body = `
    ${hasYesterday ? `<div class="row"><span class="label">Yesterday</span><span class="value">${d.yesterday_actual_kwh.toFixed(1)} kWh${yesterdayDelta}</span></div>` : ""}
    <div class="row"><span class="label">Today</span><span class="value">${d.today_kwh !== null && d.today_kwh !== undefined ? d.today_kwh.toFixed(1) + " kWh" : "&mdash;"}</span></div>
    <div class="row"><span class="label">Tomorrow</span><span class="value">${d.tomorrow_kwh !== null && d.tomorrow_kwh !== undefined ? d.tomorrow_kwh.toFixed(1) + " kWh" : "&mdash;"}</span></div>
    ${w ? `<div class="row"><span class="label">Right now</span><span class="value">${escapeHtml(w.description)}, ${fmtTemp(w.temperature_c)}</span></div>` : ""}
  `;
  const yesterdayForecastRow = d.yesterday_forecast_kwh !== null && d.yesterday_forecast_kwh !== undefined
    ? `<div class="row"><span class="label">Yesterday's forecast</span><span class="value">${d.yesterday_forecast_kwh.toFixed(1)} kWh</span></div>`
    : "";
  const details = yesterdayForecastRow + (d.model_trained_at
    ? `<div class="row"><span class="label">Model last trained</span><span class="value">${escapeHtml(d.model_trained_at)}</span></div>`
    : "");
  return card("Solar Forecast", body, details);
}

function batteryForecastCard(d) {
  if (!d.available) return unavailableCard("Battery Forecast", d.error, d.disabled);
  if (!d.checkpoints || !d.checkpoints.length) {
    return card("Battery Forecast", `<div class="row"><span class="label">No remaining checkpoints for today</span></div>`, "");
  }
  const rows = d.checkpoints.map(c => `
    <div class="row"><span class="label">${escapeHtml(c.label)}${c.priority ? " &#9733;" : ""}</span><span class="value">${fmtPct(c.predicted_soc_percent)}</span></div>
  `).join("");
  return card("Battery Forecast", rows, "");
}

function mgSaicCard(d) {
  if (!d.available) return unavailableCard("Car (MG)", d.error, d.disabled);
  const title = "Car" + (d.vehicle_name ? " - " + escapeHtml(d.vehicle_name) : "");
  const body = `
    <div class="row"><span class="label">Battery</span><span class="value">${fmtPct(d.battery_percent)}</span></div>
    ${socBar(d.battery_percent)}
    <div class="row"><span class="label">Range</span><span class="value">${d.range_km !== null && d.range_km !== undefined ? Math.round(d.range_km * 0.621371) + " mi" : "&mdash;"}</span></div>
    <div class="row"><span class="label">Status</span><span class="value">${d.is_charging ? '<span class="badge good">Charging</span>' : d.is_parked ? "Parked" : "Driving"}</span></div>
  `;
  return card(title, body, "");
}

function claudeUsageCard(d) {
  if (!d.available) return unavailableCard("Claude Usage", d.error, d.disabled);
  const session = d.buckets.find(b => b.kind === "session");
  const others = d.buckets.filter(b => b.kind !== "session");

  const ring = session
    ? `<div class="ring-row">
        ${progressRing(session.percent_used, usageColor(session.percent_used))}
        <div class="ring-label"><strong>${Math.round(session.percent_used)}% used</strong>${escapeHtml(session.label)}</div>
      </div>`
    : "";
  const otherRows = others.map(b => `
    <div class="row"><span class="label">${escapeHtml(b.label)}</span><span class="value">${Math.round(b.percent_used)}%</span></div>
    ${progressBar(b.percent_used, usageColor(b.percent_used))}
  `).join("");

  const details = d.extra_usage_percent !== null && d.extra_usage_percent !== undefined
    ? `<div class="row"><span class="label">Extra usage credits</span><span class="value">${Math.round(d.extra_usage_percent)}%</span></div>`
    : "";
  return card("Claude Usage", ring + otherRows, details);
}

const HEALTH_STATUS_BADGE = { healthy: "good", unhealthy: "bad", disabled: "" };
const HEALTH_STATUS_LABEL = { healthy: "Healthy", unhealthy: "Unhealthy", disabled: "Disabled" };

function serviceHealthCard(d) {
  if (!d.available) return unavailableCard("Service Health", d.error, d.disabled);
  const rows = d.services.map(s => {
    const badgeClass = HEALTH_STATUS_BADGE[s.health_status] ?? "";
    const badgeLabel = HEALTH_STATUS_LABEL[s.health_status] ?? titleCase(s.health_status);
    const stateDetail = !s.installed ? "not deployed" : !s.active ? escapeHtml(s.active_state) : "";
    const detailNote = stateDetail
      ? `<div class="row"><span class="label">Detail</span><span class="value">${stateDetail}</span></div>`
      : "";
    const logAge = s.log_age_seconds !== null && s.log_age_seconds !== undefined
      ? `<span class="subtext">${fmtAge(s.log_age_seconds)}</span>`
      : "";
    return `<div class="row"><span class="label">${escapeHtml(s.label)}</span><span class="value">${logAge}<span class="badge ${badgeClass}">${escapeHtml(badgeLabel)}</span></span></div>${detailNote}`;
  }).join("");
  return card("Service Health", rows, "");
}

document.getElementById("cards").addEventListener("click", (e) => {
  const clickedCard = e.target.closest(".card.has-details");
  if (clickedCard) clickedCard.classList.toggle("expanded");
});

async function refresh() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    if (!res.ok) throw new Error("status " + res.status);
    const data = await res.json();
    if (!data.ready) {
      document.getElementById("updated").textContent = "Waiting for first reading...";
      return;
    }
    document.getElementById("cards").innerHTML =
      serviceHealthCard(data.service_health) +
      solarCard(data.solar_battery) + evCard(data.ev_charging) + hotWaterCard(data.hot_water) +
      airstageCard(data.airstage) + resideoCard(data.resideo) +
      mgSaicCard(data.mg_saic) +
      solarForecastCard(data.solar_forecast) + batteryForecastCard(data.battery_forecast) +
      claudeUsageCard(data.claude_usage);

    const age = data.poll_age_seconds ?? 0;
    document.getElementById("updated").textContent = age < 5 ? "Updated just now" : `Updated ${Math.round(age)}s ago`;
    document.getElementById("stale-banner").style.display = age > STALE_AFTER_S ? "block" : "none";
  } catch (e) {
    document.getElementById("updated").textContent = "Connection lost - retrying...";
  }
}

refresh();
setInterval(refresh, POLL_MS);
</script>
</body>
</html>
"""
