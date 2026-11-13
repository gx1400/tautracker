/* tautrack client.
 *
 * Read-only. Charts are uPlot (Q6); the sky view is hand-drawn SVG because it needs
 * semantics a generic polar scatter does not provide: per-PRN labels, colour by
 * signal, tracked-vs-acquiring, and the elevation-mask ring.
 */
"use strict";

const SPAN_SECONDS = {
  "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "6h": 21600, "12h": 43200,
  "24h": 86400, "3d": 259200, "1w": 604800, "2w": 1209600,
  "1mo": 2592000, "6mo": 15552000, "1y": 31536000,
};

const state = {
  span: "1h",
  charts: new Map(),   // metric -> {plot, host, card}
  trails: false,
};

const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

async function getJSON(url) {
  const r = await fetch(url, { headers: { "Accept": "application/json" } });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function fmt(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 100000) return v.toExponential(2);
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(a < 1 ? 3 : digits);
}

/* ------------------------------------------------------------------ status */

function stateClass(mode) {
  if (!mode) return "";
  if (/^Locked/i.test(mode)) return "ok";
  if (/Holdover/i.test(mode)) return "bad";
  if (/Recovery|stabiliz/i.test(mode)) return "warn";
  return "";
}

function tile(label, value, sub, cls) {
  const d = document.createElement("div");
  d.className = "tile" + (cls ? " " + cls : "");
  d.innerHTML =
    `<div class="label"></div><div class="value"></div><div class="sub"></div>`;
  d.querySelector(".label").textContent = label;
  d.querySelector(".value").textContent = value;
  d.querySelector(".sub").textContent = sub || "";
  return d;
}

function renderStatus(s) {
  const dot = document.getElementById("conn-dot");
  const txt = document.getElementById("conn-text");
  dot.className = "dot " + (s.connected ? "ok" : "bad");
  txt.textContent = s.connected ? "polling" : (s.last_error ? "disconnected" : "waiting");

  document.getElementById("device").textContent = s.device
    ? `${s.device.model || "?"}  ·  S/N ${s.device.serial || "?"}  ·  fw ${s.device.firmware || "?"}`
    : "no device has reported yet";

  const L = s.latest || {};
  const num = (k) => (L[k] ? L[k].value : null);
  const str = (k) => (L[k] ? L[k].text : null);

  const tiles = document.getElementById("tiles");
  tiles.textContent = "";

  const mode = str("mode");
  tiles.append(tile("SmartClock", mode || "—", str("sync_banner") || "", stateClass(mode)));

  const ffom = num("ffom"), tfom = num("tfom");
  tiles.append(tile("FFOM / TFOM", `${fmt(ffom)} / ${fmt(tfom)}`,
    ffom === 0 ? "loop settled" : "settling", ffom === 0 ? "ok" : "warn"));

  const ti = num("pps_ti_ns");
  tiles.append(tile("1PPS offset", ti === null ? "—" : `${fmt(ti, 1)} ns`,
    ti === null ? "unavailable while unlocked" : "vs GPS"));

  tiles.append(tile("EFC", `${fmt(num("efc_percent"), 4)} %`,
    num("efc_dac") !== null ? `DAC ${fmt(num("efc_dac"))}` : ""));

  const trk = num("satellites_tracked");
  tiles.append(tile("Satellites", fmt(trk),
    num("satellites_visible") !== null ? `${fmt(num("satellites_visible"))} visible` : "",
    trk >= 4 ? "ok" : (trk >= 1 ? "warn" : "bad")));

  const pm = str("position_mode");
  tiles.append(tile("Position", pm || "—", str("survey_blocked") || ""));

  const pred = num("holdover_predict_us_24h");
  tiles.append(tile("Holdover predict", pred === null ? "—" : `${fmt(pred, 1)} µs`, "per 24 h"));

  const health = num("health_ok");
  tiles.append(tile("Health", health === null ? "—" : (health ? "OK" : "FAULT"),
    "", health === null ? "" : (health ? "ok" : "bad")));

  // Clocks. All unit-derived values share one format so they compare by eye.
  const c = s.clocks || {};
  document.getElementById("clk-unit").textContent = c.unit_reported || "—";
  document.getElementById("clk-unit-note").textContent = c.unit_unconfirmed
    ? "time not yet confirmed by GPS"
    : "verbatim, date affected by rollover";
  document.getElementById("clk-utc").textContent = c.unit_corrected_utc || "—";
  document.getElementById("clk-local").textContent = c.unit_corrected_local || "—";
  document.getElementById("clk-local-note").textContent =
    c.rollover_days ? `offset ${c.rollover_days} d applied` : "no rollover offset";
  document.getElementById("clk-tzname").textContent = c.zone_label || c.timezone || "";

  document.getElementById("clk-host").textContent =
    c.host_local ? `${c.host_local} ${c.zone_label || ""}`.trim() : "—";

  // The delta is only meaningful against a live device. Under replay it is just the
  // age of the recorded screen, so say so rather than implying instrument error.
  const deltaEl = document.getElementById("clk-delta");
  if (c.host_delta_seconds === null || c.host_delta_seconds === undefined) {
    deltaEl.textContent = "";
    deltaEl.className = "hostclock-delta";
  } else if (c.live === false) {
    deltaEl.textContent = `replay — recorded ${humanDelta(c.host_delta_seconds)} ago`;
    deltaEl.className = "hostclock-delta replay";
  } else {
    const d = c.host_delta_seconds;
    deltaEl.textContent = `unit ${d >= 0 ? "behind" : "ahead of"} host by ${humanDelta(Math.abs(d))}`;
    deltaEl.className = "hostclock-delta " + (Math.abs(d) <= 2 ? "ok" : "warn");
  }
}

function humanDelta(seconds) {
  const s = Math.round(Math.abs(seconds));
  if (s < 90) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/* ------------------------------------------------------------------ charts */

function makePlot(host, title, zeroLine) {
  const accent = css("--accent") || "#4aa3ff";
  const grid = css("--line") || "#273041";
  const muted = css("--muted") || "#8b98a9";

  const opts = {
    width: host.clientWidth || 300,
    height: 120,
    padding: [8, 8, 0, 0],
    cursor: { show: true, y: false, drag: { x: false, y: false } },
    legend: { show: false },
    scales: { x: { time: true } },
    axes: [
      { stroke: muted, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid },
        size: 28, font: "10px ui-monospace, monospace" },
      { stroke: muted, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid },
        size: 44, font: "10px ui-monospace, monospace" },
    ],
    series: [
      {},
      {
        stroke: accent,
        width: 1.6,
        fill: accent + "22",
        points: { show: false },
        // Gaps are real: a transport outage must read as missing data, not as a
        // straight line interpolated across it (FR-6).
        spanGaps: false,
      },
      { stroke: accent + "55", width: 1, points: { show: false }, spanGaps: false },
      { stroke: accent + "55", width: 1, points: { show: false }, spanGaps: false },
    ],
  };
  if (zeroLine) {
    opts.hooks = {
      draw: [(u) => {
        const y = u.valToPos(0, "y", true);
        if (!Number.isFinite(y)) return;
        const ctx = u.ctx;
        ctx.save();
        ctx.strokeStyle = muted;
        ctx.globalAlpha = 0.45;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(u.bbox.left, y);
        ctx.lineTo(u.bbox.left + u.bbox.width, y);
        ctx.stroke();
        ctx.restore();
      }],
    };
  }
  return new uPlot(opts, [[], [], [], []], host);
}

async function loadChart(card) {
  const metric = card.dataset.metric;
  const host = card.querySelector('[data-role="host"]');
  const lastEl = card.querySelector('[data-role="last"]');
  const resEl = card.querySelector('[data-role="res"]');

  let d;
  try {
    d = await getJSON(`/api/series?metric=${encodeURIComponent(metric)}&span=${state.span}`);
  } catch {
    host.innerHTML = '<div class="empty">no data yet</div>';
    return;
  }

  if (!d.ts || d.ts.length === 0) {
    host.innerHTML = '<div class="empty">no data in this window</div>';
    state.charts.delete(metric);
    lastEl.textContent = "—";
    resEl.textContent = "";
    return;
  }

  // Say which storage tier answered rather than implying raw fidelity over a year.
  resEl.textContent = d.resolution === "raw" ? "" :
    (d.resolution === "hour" ? "hourly min/avg/max" : "daily min/avg/max");

  const vals = d.value.map((v) => (v === null ? null : v));
  const series = [d.ts, vals];
  // Trends carry min and max so a brief excursion stays visible after downsampling.
  series.push(d.min ? d.min : vals.map(() => null));
  series.push(d.max ? d.max : vals.map(() => null));

  let entry = state.charts.get(metric);
  if (!entry || !host.querySelector(".uplot")) {
    host.textContent = "";
    entry = { plot: makePlot(host, metric, card.dataset.zero === "1"), host };
    state.charts.set(metric, entry);
  }
  entry.plot.setSize({ width: host.clientWidth || 300, height: 120 });
  entry.plot.setData(series);

  for (let i = vals.length - 1; i >= 0; i--) {
    if (vals[i] !== null) { lastEl.textContent = fmt(vals[i]); break; }
  }
}

async function loadAllCharts() {
  const cards = [...document.querySelectorAll(".chart-card")];
  await Promise.all(cards.map((c) => loadChart(c).catch(() => {})));
}

/* ------------------------------------------------------------------ sky */

const SKY = { cx: 160, cy: 160, r: 138 };

function skyXY(elevation, azimuth) {
  // Zenith at the centre, horizon at the rim; north up, east right.
  const rr = SKY.r * (1 - Math.max(0, Math.min(90, elevation)) / 90);
  const a = ((azimuth - 90) * Math.PI) / 180;
  return [SKY.cx + rr * Math.cos(a), SKY.cy + rr * Math.sin(a)];
}

function signalColour(ss) {
  if (ss === null || ss === undefined) return css("--muted") || "#8b98a9";
  if (ss >= 80) return css("--ok") || "#3fb950";
  if (ss >= 45) return css("--accent") || "#4aa3ff";
  return css("--warn") || "#d29922";
}

function renderSky(data) {
  const svg = document.getElementById("sky");
  const grid = css("--line") || "#273041";
  const muted = css("--muted") || "#8b98a9";
  const parts = [];

  for (const el of [0, 30, 60]) {
    const rr = SKY.r * (1 - el / 90);
    parts.push(`<circle cx="${SKY.cx}" cy="${SKY.cy}" r="${rr.toFixed(1)}" fill="none" stroke="${grid}" stroke-width="1"/>`);
  }
  for (let az = 0; az < 360; az += 30) {
    const [x, y] = skyXY(0, az);
    parts.push(`<line x1="${SKY.cx}" y1="${SKY.cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${grid}" stroke-width="0.6"/>`);
  }
  // The elevation-mask ring: satellites below it are never used, so showing it makes
  // the receiver's own cutoff visible rather than implied.
  if (data.elevation_mask) {
    const rr = SKY.r * (1 - data.elevation_mask / 90);
    parts.push(`<circle cx="${SKY.cx}" cy="${SKY.cy}" r="${rr.toFixed(1)}" fill="none" stroke="${css("--warn")}" stroke-width="1" stroke-dasharray="4 3" opacity="0.75"/>`);
  }
  for (const [label, az] of [["N", 0], ["E", 90], ["S", 180], ["W", 270]]) {
    const [x, y] = skyXY(-6, az);
    parts.push(`<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" fill="${muted}" font-size="10" text-anchor="middle" dominant-baseline="middle">${label}</text>`);
  }

  if (state.trails && data.history && data.history.length) {
    const byPrn = new Map();
    for (const h of data.history) {
      if (h.elevation === null || h.azimuth === null) continue;
      if (!byPrn.has(h.prn)) byPrn.set(h.prn, []);
      byPrn.get(h.prn).push(skyXY(h.elevation, h.azimuth));
    }
    for (const pts of byPrn.values()) {
      if (pts.length < 2) continue;
      const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
      parts.push(`<path d="${d}" fill="none" stroke="${css("--accent")}" stroke-width="1" opacity="0.28"/>`);
    }
  }

  for (const s of data.current || []) {
    if (s.elevation === null || s.azimuth === null) continue;
    const [x, y] = skyXY(s.elevation, s.azimuth);
    const colour = s.tracked ? signalColour(s.signal) : muted;
    const r = s.tracked ? 7 : 5;
    parts.push(
      `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}" fill="${colour}" ` +
      `fill-opacity="${s.tracked ? 0.9 : 0.35}" stroke="${colour}" stroke-width="1.2"/>`);
    parts.push(
      `<text x="${x.toFixed(1)}" y="${(y + 0.5).toFixed(1)}" font-size="7.5" ` +
      `text-anchor="middle" dominant-baseline="middle" fill="${s.tracked ? "#06121f" : muted}" ` +
      `font-family="ui-monospace, monospace">${s.prn}</text>`);
  }

  svg.innerHTML = parts.join("");

  const list = document.getElementById("sat-list");
  list.textContent = "";
  const sats = [...(data.current || [])].sort(
    (a, b) => (b.tracked - a.tracked) || ((b.signal || 0) - (a.signal || 0)));
  if (!sats.length) {
    list.innerHTML = '<li class="empty">no satellites reported</li>';
    return;
  }
  for (const s of sats) {
    const li = document.createElement("li");
    li.className = (s.tracked ? "tracked" : "") + (s.acquiring ? " acq" : "");
    const pct = Math.max(4, Math.min(100, ((s.signal || 0) / 170) * 100));
    li.innerHTML =
      `<span class="prn">${s.prn}</span>` +
      `<span class="bar"><span style="width:${pct}%"></span></span>` +
      `<span class="el">${s.elevation === null ? "—" : s.elevation + "°"}` +
      `${s.signal !== null && s.signal !== undefined ? " / " + s.signal : (s.acquiring ? " acq" : "")}</span>`;
    list.append(li);
  }
}

async function loadSky() {
  try {
    const q = state.trails ? `?trail=${state.span}` : "";
    renderSky(await getJSON(`/api/satellites${q}`));
  } catch { /* nothing reported yet */ }
}

/* ------------------------------------------------------------------ events */

async function loadEvents() {
  let d;
  try { d = await getJSON("/api/events?limit=25"); } catch { return; }
  const ul = document.getElementById("events");
  ul.textContent = "";
  for (const e of d.events) {
    const li = document.createElement("li");
    li.className = e.level;
    const when = new Date(e.ts * 1000).toISOString().slice(11, 19);
    li.innerHTML = `<span class="when"></span><span class="src"></span><span class="msg"></span>`;
    li.querySelector(".when").textContent = when;
    li.querySelector(".src").textContent = e.source;
    li.querySelector(".msg").textContent = e.message;
    ul.append(li);
  }
}

/* ------------------------------------------------------------------ wiring */

async function refreshFast() {
  try { renderStatus(await getJSON("/api/status")); } catch { /* keep last view */ }
  await loadSky();
}

async function refreshSlow() {
  await loadAllCharts();
  await loadEvents();
}

document.querySelectorAll(".span").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".span").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.span = b.dataset.span;
    loadAllCharts();
    if (state.trails) loadSky();
  });
});

document.getElementById("trail-toggle").addEventListener("change", (e) => {
  state.trails = e.target.checked;
  loadSky();
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    for (const [metric, entry] of state.charts) {
      entry.plot.setSize({ width: entry.host.clientWidth || 300, height: 120 });
    }
  }, 150);
});

refreshFast();
refreshSlow();
setInterval(refreshFast, 5000);
setInterval(refreshSlow, 30000);
