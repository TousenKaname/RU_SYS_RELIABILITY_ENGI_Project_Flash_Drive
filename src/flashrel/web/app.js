/* FlashRel Rig dashboard: polls /api/state every 2 s and renders the active view. */
"use strict";

const TOKEN = window.FLASHREL_TOKEN;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let S = null;               // latest snapshot
let tab = localStorage.getItem("fr-tab") || "overview";
let survUnit = "cycles";
let evFilter = "key";
let drawerId = null;
let drawerData = null;
let setupData = null;
let setupTimer = null;
let sortKey = "port";
let sortDir = 1;
const seenJobs = new Set();

/* ---------- formatting ---------------------------------------------------------------- */
const fmtGB = (b) => (b == null ? "—" : b >= 1e12 ? (b / 1e12).toFixed(2) + " TB" : (b / 1e9).toFixed(b >= 1e11 ? 0 : 2) + " GB");
const fmtMB = (b) => (b == null ? "—" : (b / 1e6).toFixed(b >= 1e9 ? 0 : 1) + " MB");
const fmtNum = (x, d = 0) => (x == null || x === "" || isNaN(x) ? "—" : Number(x).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));
const fmtK = (x) => (x == null ? "—" : x >= 1e6 ? (x / 1e6).toFixed(2) + "M" : x >= 1e4 ? (x / 1e3).toFixed(1) + "k" : String(x));
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  const core = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}`;
  return d ? `${d}d ${core}` : core;
}
function fmtShort(s) {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}
const ago = (iso) => (iso ? fmtShort((Date.now() - new Date(iso).getTime()) / 1000) + " ago" : "—");
function hhmm(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  return d.toDateString() === now.toDateString() ? t : `${d.getMonth() + 1}/${d.getDate()} ${t}`;
}
const WL = { large: "L", medium: "M", small: "S", check: "C" };
const GCOL = (g) => (S && S.campaign.groups[g] ? S.campaign.groups[g].color : "#64748b");

/* ---------- API ------------------------------------------------------------------------- */
async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-FlashRel-Token": TOKEN }, body: JSON.stringify(body || {}) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status}`);
  return data;
}
async function runJob(kind, args = {}, label = kind) {
  try {
    const j = await post("/api/job", { kind, args });
    toast(`${label}: started`);
    seenJobs.add(j.id);
    return j;
  } catch (e) { toast(`${label}: ${e.message}`, "err"); }
}
function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = esc(text);
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 9000 : 4500);
}
function confirmBox(title, html, okLabel = "OK", danger = false) {
  return new Promise((resolve) => {
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = html;
    const ok = $("#modal-ok");
    ok.textContent = okLabel;
    ok.className = "btn " + (danger ? "danger" : "primary");
    $("#modal").hidden = false;
    const done = (v) => { $("#modal").hidden = true; ok.onclick = null; $("#modal-cancel").onclick = null; resolve(v); };
    ok.onclick = () => done(true);
    $("#modal-cancel").onclick = () => done(false);
  });
}

/* ---------- drive visuals ---------------------------------------------------------------- */
const PHASE = {
  write: ["var(--st-write)", "WRITE"], verify: ["var(--st-verify)", "VERIFY"], delete: ["var(--st-delete)", "DELETE"],
  setup: ["var(--st-setup)", "SETUP"], check: ["var(--st-setup)", "CHECK"], sentinel: ["var(--st-setup)", "CHECKING"],
  locating: ["var(--st-setup)", "LOCATING"], recovering: ["var(--st-recover)", "RECOVERY"], "paused (hub)": ["var(--st-paused)", "HUB EVENT"],
};
function visual(d) {
  if (!d.enrolled) return { cls: "empty", color: "var(--line2)", label: "NOT ENROLLED", pulse: false };
  if (d.status === "failed") return { cls: "failed", color: "var(--st-failed)", label: "FAILED", pulse: false };
  if (d.status === "attention") return { cls: "attention", color: "var(--st-attn)", label: "ATTENTION", pulse: true };
  if (d.status === "censored") return { cls: "censored", color: "var(--st-censored)", label: "CENSORED", pulse: false };
  if (d.status === "paused") return { cls: "", color: "var(--st-paused)", label: "HUB EVENT", pulse: true };
  if (d.status === "recovering") return { cls: "", color: "var(--st-recover)", label: "RECOVERY", pulse: true };
  if (d.status === "enrolled" || !d.worker.running) {
    const label = d.status === "enrolled" ? "READY" : d.worker.giving_up ? "WORKER FAILING" : "IDLE";
    return { cls: d.present === false ? "missing" : "", color: d.worker.giving_up ? "var(--bad)" : "var(--st-idle)", label, pulse: false };
  }
  const ph = PHASE[d.phase] || ["var(--st-setup)", String(d.phase || "").toUpperCase()];
  return { cls: d.present === false ? "missing" : "", color: ph[0], label: ph[1], pulse: true };
}
function stickSvg(color) {
  return `<svg class="stick" viewBox="0 0 18 30"><rect x="5.5" y="1" width="7" height="6" rx="1" fill="none" stroke="${color}" stroke-width="1.4"/><rect x="2" y="7" width="14" height="22" rx="4" fill="none" stroke="${color}" stroke-width="1.6"/><circle cx="9" cy="18" r="2" fill="${color}"/></svg>`;
}

/* ---------- header and KPIs -------------------------------------------------------------- */
function renderHeader() {
  const r = S.run, c = S.campaign;
  $("#ver").textContent = `v${S.version}`;
  $("#campaign-title").textContent = `${c.name} · ${c.title}`;
  const pill = $("#phase-pill");
  pill.textContent = r.phase;
  pill.className = `pill ${r.phase}`;
  const ab = $("#admin-badge");
  ab.textContent = S.host.admin ? "administrator" : "no admin rights";
  ab.className = `badge ${S.host.admin ? "ok" : "bad"}`;
  $("#sim-badge").hidden = !S.sim;
  const clock = $("#clock");
  if (r.started_at) {
    const total = c.duration_h * 3600, el = r.elapsed_s || 0;
    const pct = Math.min(100, (100 * el) / total);
    clock.innerHTML = `<div class="big">T+${fmtDur(el)}</div><div class="sub">${r.remaining_s > 0 ? fmtDur(r.remaining_s) + " left · stop " + hhmm(r.stop_at) : "stop time reached " + hhmm(r.stop_at)}</div><div class="bar"><i style="width:${pct}%"></i></div>`;
  } else {
    clock.innerHTML = `<div class="sub">not started · ${c.duration_h} h test</div>`;
  }
  const banner = $("#banner");
  const msgs = [];
  if (S.run.phase === "stopped") msgs.push(["info", "The test has stopped. Follow the <b>Post-test</b> tab, then export the data (Data &amp; control)."]);
  const attn = Object.values(S.drives).filter((d) => d.role === "test" && d.status === "attention");
  if (attn.length && S.run.phase === "running") msgs.push(["", `${attn.length} drive(s) in <b>attention</b> (${attn.map((d) => d.id).join(", ")}): leave them plugged in; they are checked after the stop.`]);
  const hung = Object.values(S.drives).filter((d) => d.worker.hang || d.worker.giving_up);
  if (hung.length) msgs.push(["", `Worker problem on ${hung.map((d) => d.id).join(", ")} (see the Drives tab).`]);
  if (msgs.length) { banner.hidden = false; banner.className = `banner ${msgs[0][0]}`; banner.innerHTML = msgs.map((m) => m[1]).join("<br>"); }
  else banner.hidden = true;
}

function renderKpis() {
  const k = S.kpi, st = k.by_status || {};
  const active = (st.active || 0) + (st.recovering || 0) + (st.paused || 0);
  const failed = (st.failed || 0) + (st.attention || 0);
  const groups = Object.entries(k.failures_by_group || {});
  const gbars = groups.map(([g, [f, n]]) => `<div class="g"><div class="t"><span>${esc(g)}</span><b>${f}/${n}</b></div><div class="track"><i style="width:${n ? (100 * f) / n : 0}%;background:${GCOL(g)}"></i></div></div>`).join("");
  const tiles = [
    ["Drives on test", `${k.test_drives}`, `${st.enrolled || 0} ready · ${k.by_status.missing || 0} missing`],
    ["Cycling now", `${active}`, `${st.recovering || 0} in recovery · ${st.paused || 0} paused`],
    ["Failed", `${failed}<small>/ ${k.test_drives}</small>`, `${st.failed || 0} end of life · ${st.attention || 0} attention`],
    ["Censored", `${st.censored || 0}`, S.run.phase === "running" ? "at the stop time" : "survived the test"],
    ["Cycles completed", fmtNum(k.cycles_total), `${k.failed_cycles} failed cycles`],
    ["Data written", `${fmtNum(k.tb_written, 2)}<small>TB</small>`, `${fmtNum(k.tb_read, 2)} TB read back`],
  ];
  $("#kpis").innerHTML = tiles.map(([l, v, f], i) => `<div class="kpi"><div class="lbl">${l}</div><div class="val">${v}</div><div class="foot">${f}</div>${i === 2 ? `<div class="gbar">${gbars}</div>` : ""}</div>`).join("");
}

/* ---------- hubs ------------------------------------------------------------------------- */
function portCard(slot) {
  const d = S.drives[slot.drive];
  if (!d) return `<div class="port empty"><div class="row1">P${slot.port}</div></div>`;
  const v = visual(d), g = d.group, gc = GCOL(g);
  let body = "";
  if (["failed", "attention", "censored"].includes(d.status)) {
    const ff = d.first_failure, eol = d.end_of_life;
    const what = d.status === "censored" ? `${d.cycles_done} cycles · ${fmtNum(d.fdw, 1)} FDW` :
      `${(eol || ff || {}).code || ""} · cycle ${(eol || ff || {}).cycle ?? "?"}`;
    body = `<div class="status-big" style="color:${v.color}">${v.label}</div><div class="line"><span>${esc(what)}</span></div>
      <div class="line"><span>${d.failures} failure${d.failures === 1 ? "" : "s"}</span><span>${fmtK(d.file_ops)} ops</span></div>`;
  } else if (d.worker.running && d.phase) {
    const pctB = d.bytes_total ? d.bytes_done / d.bytes_total : 0, pctF = d.files_total ? d.files_done / d.files_total : 0;
    const pct = Math.min(1, d.phase === "delete" ? pctF : Math.max(pctB, d.phase === "verify" ? pctB : pctF));
    const rate = d.phase === "delete" ? `${fmtNum(d.rate_fps, 1)} files/s` : `${fmtNum(d.rate_mbps, 2)} MB/s`;
    body = `<div class="line"><span>cycle <b>${d.cycle ?? "—"}</b></span><span class="wl">${WL[d.workload] || "?"}</span></div>
      <div class="prog"><i style="width:${(100 * pct).toFixed(1)}%;background:${v.color}"></i></div>
      <div class="line"><span style="color:${v.color};font-weight:700">${v.label}</span><span>${rate}</span></div>
      <div class="line"><span>${fmtK(d.files_done)}/${fmtK(d.files_total)} files</span><span>${fmtNum(d.fdw, 1)} FDW</span></div>`;
  } else {
    body = `<div class="status-big" style="color:${v.color}">${v.label}</div><div class="line"><span>${d.cycles_done} cycles</span><span>${d.letter ? esc(d.letter) + ":" : ""}</span></div>`;
  }
  const ls = d.last_sentinel;
  const sent = ls ? (ls.ok ? `<span class="ok" title="last check of data at rest and file-system records">✓ ${esc(ago(ls.time).replace(" ago", ""))}</span>` : `<span class="bad" title="${esc(ls.detail || "")}">✗ ${esc(ls.failed)}</span>`) : `<span>·</span>`;
  const extra = [];
  if (d.transients) extra.push(`<span class="warn">D3×${d.transients}</span>`);
  if (d.recoveries) extra.push(`<span class="warn">D2×${d.recoveries}</span>`);
  if (d.slow && d.slow.length) extra.push(`<span class="warn">D1</span>`);
  if (d.present === false && d.enrolled && !["failed", "attention", "censored"].includes(d.status)) extra.push(`<span class="bad">missing</span>`);
  return `<div class="port ${v.cls}" style="--gc:${gc}" data-drive="${esc(d.id)}" title="${esc(d.id)} · click for details">
    <div class="row1"><span class="led ${v.pulse ? "pulse" : ""}" style="background:${v.color};--lc:${v.color}"></span>P${slot.port}<span class="chip">${esc(g)}</span></div>
    <div class="id">${esc(d.id)}</div>${body}<div class="health">${sent}${extra.join("")}</div>${stickSvg(gc)}</div>`;
}

function renderHubs() {
  const c = S.campaign;
  const html = c.hubs.map((h) => {
    const slots = c.slots.filter((s) => s.hub === h).sort((a, b) => a.port - b.port);
    const ds = slots.map((s) => S.drives[s.drive]).filter(Boolean);
    const active = ds.filter((d) => d.worker.running).length;
    const failed = ds.filter((d) => ["failed", "attention"].includes(d.status)).length;
    const series = (S.hub_load || {})[h] || [];
    const mbps = series.length ? series[series.length - 1][1] : 0;
    const keys = (S.host.hub_keys || {})[h] || [];
    const keyTxt = keys.length ? keys.map((k) => k.split("#").slice(-1)[0]).join(", ") : "PC port not recorded yet";
    return `<div class="hub"><div class="hub-head"><div class="hub-name"><span class="plug"></span>HUB ${esc(h)}</div>
      <div class="hub-meta">${esc(c.hub_model.split("(")[0])} · PC port ${esc(keyTxt)}</div>
      <div class="hub-stats"><span>active <b>${active}</b>/10</span><span>failed <b>${failed}</b></span><span>link <b>${fmtNum(mbps, 1)}</b> MB/s</span></div></div>
      <div class="ports">${slots.map(portCard).join("")}</div></div>`;
  }).join("");
  $("#hubs").innerHTML = html;
  $$("#hubs .port[data-drive]").forEach((el) => (el.onclick = () => openDrawer(el.dataset.drive)));
}

/* ---------- charts ------------------------------------------------------------------------ */
function niceMax(x) {
  if (!(x > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(x)));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= x) return m * p;
  return 10 * p;
}
function svgSurvival(summary) {
  const W = 520, H = 250, L = 38, R = 10, T = 10, B = 28;
  const groups = Object.entries((summary || {}).groups || {});
  let xmax = 1;
  groups.forEach(([, g]) => { g.km.forEach((s) => (xmax = Math.max(xmax, s.t))); g.censored.forEach((t) => (xmax = Math.max(xmax, t))); });
  xmax = niceMax(xmax * 1.05);
  const X = (t) => L + ((W - L - R) * t) / xmax, Y = (s) => T + (H - T - B) * (1 - s);
  let out = `<svg class="chart" viewBox="0 0 ${W} ${H}"><g class="grid">`;
  [0, 0.25, 0.5, 0.75, 1].forEach((s) => (out += `<line x1="${L}" x2="${W - R}" y1="${Y(s)}" y2="${Y(s)}"/>`));
  out += `</g><g class="axis">`;
  [0, 0.5, 1].forEach((s) => (out += `<text x="${L - 6}" y="${Y(s) + 4}" text-anchor="end">${s.toFixed(1)}</text>`));
  for (let i = 0; i <= 4; i++) { const t = (xmax * i) / 4; out += `<text x="${X(t)}" y="${H - 8}" text-anchor="middle">${fmtNum(t, t < 10 && t % 1 ? 1 : 0)}</text>`; }
  out += `<text x="${W - R}" y="${T + 12}" text-anchor="end" class="lab">${esc((summary || {}).unit || "")} →</text></g>`;
  groups.forEach(([name, g]) => {
    const col = GCOL(name);
    let d = `M${X(0)},${Y(1)}`, s = 1;
    g.km.slice(1).forEach((p) => { d += ` H${X(p.t)} V${Y(p.s)}`; s = p.s; });
    const tEnd = Math.max(g.km[g.km.length - 1].t, ...(g.censored.length ? g.censored : [0]));
    d += ` H${X(tEnd)}`;
    out += `<path d="${d}" fill="none" stroke="${col}" stroke-width="2.4"/>`;
    g.censored.forEach((t) => {
      let sv = 1; g.km.forEach((p) => { if (p.t <= t) sv = p.s; });
      out += `<line x1="${X(t)}" x2="${X(t)}" y1="${Y(sv) - 5}" y2="${Y(sv) + 5}" stroke="${col}" stroke-width="1.6"/>`;
    });
  });
  out += `</svg>`;
  return out;
}
function renderSurvival() {
  const sum = (S.survival || {})[survUnit];
  if (!sum || !Object.keys(sum.groups || {}).length) { $("#survival").innerHTML = `<div class="list-msg">The curves appear once the drives have started.</div>`; return; }
  const rows = Object.entries(sum.groups).map(([g, x]) => {
    const w = x.weibull;
    const fit = w ? `<td>${fmtNum(w.beta, 2)}</td><td>${fmtNum(w.eta, 1)}</td><td>${fmtNum(w.mttf, 1)}</td><td>${fmtNum(w.b10, 1)}</td>` :
      `<td colspan="4" class="muted">${x.failures < 2 ? (x.weibayes_b2 ? `Weibayes η ≥ ${fmtNum(x.weibayes_b2, 1)} (β = 2, 90 %)` : "needs 2 failures") : ""}</td>`;
    return `<tr><td><span class="dot" style="background:${GCOL(g)}"></span>${esc(g)}</td><td>${x.failures}/${x.n}</td>${fit}</tr>`;
  }).join("");
  $("#survival").innerHTML = svgSurvival(sum) +
    `<table class="fits"><tr><th>group</th><th>failed</th><th>β</th><th>η</th><th>MTTF</th><th>B10</th></tr>${rows}</table>
     <div class="small muted" style="margin-top:4px">Kaplan–Meier of the first failure; Weibull MLE with right censoring (live estimate, ${esc(sum.unit)}).</div>`;
}
function svgLines(series, { ymin = 0, cap = null, unit = "" } = {}) {
  const W = 520, H = 230, L = 38, R = 10, T = 10, B = 26;
  const all = series.flatMap((s) => s.points.map((p) => p[1]));
  const ymax = niceMax(Math.max(cap ? cap * 1.1 : 1, ...all, 1));
  const n = Math.max(2, ...series.map((s) => s.points.length));
  const X = (i) => L + ((W - L - R) * i) / (n - 1), Y = (v) => T + (H - T - B) * (1 - (v - ymin) / (ymax - ymin));
  let out = `<svg class="chart" viewBox="0 0 ${W} ${H}"><g class="grid">`;
  for (let i = 0; i <= 4; i++) out += `<line x1="${L}" x2="${W - R}" y1="${Y((ymax * i) / 4)}" y2="${Y((ymax * i) / 4)}"/>`;
  out += `</g><g class="axis">`;
  for (let i = 0; i <= 4; i += 2) out += `<text x="${L - 6}" y="${Y((ymax * i) / 4) + 4}" text-anchor="end">${fmtNum((ymax * i) / 4)}</text>`;
  out += `<text x="${W - R}" y="${T + 12}" text-anchor="end" class="lab">${esc(unit)}</text></g>`;
  if (cap) out += `<line class="cap" x1="${L}" x2="${W - R}" y1="${Y(cap)}" y2="${Y(cap)}"/>`;
  series.forEach((s) => {
    const off = n - s.points.length;
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(i + off).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
    if (d) out += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2"/>`;
  });
  return out + `</svg>`;
}
function svgScatter(series, { unit = "", xlabel = "" } = {}) {
  const W = 520, H = 220, L = 38, R = 10, T = 10, B = 26;
  const pts = series.flatMap((s) => s.points);
  const xmax = Math.max(2, ...pts.map((p) => p[0])), ymax = niceMax(Math.max(1, ...pts.map((p) => p[1])));
  const X = (x) => L + ((W - L - R) * x) / xmax, Y = (y) => T + (H - T - B) * (1 - y / ymax);
  let out = `<svg class="chart" viewBox="0 0 ${W} ${H}"><g class="grid">`;
  for (let i = 0; i <= 4; i++) out += `<line x1="${L}" x2="${W - R}" y1="${Y((ymax * i) / 4)}" y2="${Y((ymax * i) / 4)}"/>`;
  out += `</g><g class="axis">`;
  for (let i = 0; i <= 4; i += 2) out += `<text x="${L - 6}" y="${Y((ymax * i) / 4) + 4}" text-anchor="end">${fmtNum((ymax * i) / 4, ymax < 5 ? 1 : 0)}</text>`;
  for (let i = 0; i <= 4; i++) { const x = Math.round((xmax * i) / 4); out += `<text x="${X(x)}" y="${H - 8}" text-anchor="middle">${x}</text>`; }
  out += `<text x="${W - R}" y="${T + 12}" text-anchor="end" class="lab">${esc(unit)}</text></g>`;
  series.forEach((s) => {
    if (s.points.length > 1) out += `<path d="${s.points.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ")}" fill="none" stroke="${s.color}" stroke-width="1.4" opacity=".55"/>`;
    s.points.forEach((p) => (out += `<circle cx="${X(p[0]).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="3.2" fill="${s.color}"/>`));
  });
  (series.marks || []).forEach((m) => (out += `<line x1="${X(m)}" x2="${X(m)}" y1="${T}" y2="${H - B}" stroke="var(--bad)" stroke-dasharray="3 3"/>`));
  return out + `</svg>`;
}
function renderHubLoad() {
  const cols = ["#38bdf8", "#f472b6", "#a3e635"];
  const series = S.campaign.hubs.map((h, i) => ({ name: h, color: cols[i % 3], points: (S.hub_load[h] || []).slice(-360) }));
  const first = series[0].points[0], last = series[0].points[series[0].points.length - 1];
  $("#hubload").innerHTML = svgLines(series, { cap: 40, unit: "MB/s (read + write)" }) +
    `<div class="legend">${series.map((s) => `<span><span class="dot" style="background:${s.color}"></span>hub ${esc(s.name)} <b>${fmtNum(s.points.length ? s.points[s.points.length - 1][1] : 0, 1)}</b> MB/s</span>`).join("")}
     <span>${first ? `${hhmm(first[0])} – ${hhmm(last[0])}` : ""}</span></div>`;
}

/* ---------- events ------------------------------------------------------------------------ */
const KEY_TYPES = new Set(["cycle_failure", "recovered", "attention", "hard_failure", "censored", "hub_event", "port_fault",
  "retired", "slow", "transient", "interrupted", "selftest", "posttest", "recheck", "portcheck"]);
function evText(e) {
  const d = e.detail || {};
  const c = e.cycle;
  switch (e.type) {
    case "cycle_failure": {
      const why = d.files_missing ? `${d.files_missing} files missing` : d.files_corrupt ? `${d.files_corrupt} files corrupt` :
        (d.sentinel && d.sentinel.failed) ? `${d.sentinel.failed} damaged at rest` : ((d.errors || [])[0] || {}).detail || d.reason || d.outcome || "";
      return ["bad", `<b>${esc(e.code)} cycle failure</b> in cycle ${c} (${esc(d.workload || "")}${d.phase ? ", " + esc(d.phase) : ""}): ${esc(String(why).slice(0, 90))}`];
    }
    case "recovered": return ["good", `<b>recovered (D2)</b> after ${esc(d.failure_code || "")}: fresh 64 MiB check passed`];
    case "attention": return ["bad", `<b>attention</b> (${esc(e.code)}): ${esc(d.reason || "")}`];
    case "hard_failure": return ["bad", `<b>end of life</b> (${esc(e.code)}): ${esc(d.reason || "")}`];
    case "censored": return ["", `<b>censored</b> at ${d.cycles_done} cycles`];
    case "transient": return ["warn", `<b>D3</b> transient error ×${d.count} in cycle ${c}`];
    case "slow": return ["warn", `<b>D1 slowdown</b> (${esc(d.workload)}): ${d.recent_mbps} MB/s vs ${d.baseline_mbps}`];
    case "hub_event": return ["warn", `<b>hub event (F5)</b>, not counted: ${esc(d.where || "")}`];
    case "port_fault": return ["warn", `<b>F5</b>: ${esc(d.text || "re-attributed to the port")}`];
    case "interrupted": return ["", `cycle ${c} not counted (${esc(d.reason || "")})`];
    case "device_reset": return ["", `USB device restarted (${d.ok ? "ok" : "failed"})`];
    case "started": return ["", `started on hub ${esc(d.hub)} port ${d.port}`];
    case "enrolled": return ["", `enrolled for hub ${esc(d.hub || "—")} port ${d.port || "—"}`];
    case "selftest": return [d.passed ? "good" : "bad", `<b>self-test ${d.passed ? "passed" : "FAILED"}</b>`];
    case "posttest": case "recheck": return [d.passed ? "good" : "bad", `<b>${e.type === "recheck" ? "re-check on another port" : "post-test check"} ${d.passed ? "passed" : "failed"}</b>`];
    case "portcheck": return [d.passed ? "good" : "bad", `<b>port check ${esc(d.slot)}</b> with ${esc(d.spare)}: ${d.passed ? "port ok" : "port FAILED"}`];
    case "retired": return ["bad", `<b>retired</b> as ${esc(d.classification)}`];
    case "note": return ["", esc(d.text || "")];
    default: return ["", esc(e.type)];
  }
}
function renderEvents() {
  const list = (S.events || []).filter((e) => evFilter === "all" || KEY_TYPES.has(e.type)).slice(-80).reverse();
  $("#events").innerHTML = list.length ? list.map((e) => {
    const [cls, txt] = evText(e);
    return `<div class="ev ${cls}"><span class="t">${hhmm(e.time)}</span><span class="d" style="color:${GCOL((S.drives[e.drive] || {}).group)}">${esc(e.drive || "")}</span><span class="x">${txt}</span></div>`;
  }).join("") : `<div class="list-msg">No events yet.</div>`;
}

/* ---------- drives table -------------------------------------------------------------------- */
function renderDrivesTable() {
  const cols = [
    ["port", "Hub/port"], ["id", "Drive"], ["group", "Group"], ["status", "Status"], ["cycles_done", "Cycles"],
    ["phase", "Now"], ["fdw", "FDW"], ["file_ops", "File ops"], ["failures", "Fail"], ["transients", "D3"],
    ["recoveries", "D2"], ["first", "First failure"], ["wL", "Write L MB/s"], ["wS", "Write S MB/s"], ["letter", "Letter"], ["sent", "Last check"],
  ];
  const rows = Object.values(S.drives).map((d) => ({
    ...d, port: d.role === "test" ? `${d.hub}${String(d.port).padStart(2, "0")}` : "spare",
    first: d.first_failure ? `c${d.first_failure.cycle} ${d.first_failure.code}` : "",
    wL: ((d.last_mbps || {}).large || {}).write, wS: ((d.last_mbps || {}).small || {}).write,
    sent: d.last_sentinel ? (d.last_sentinel.ok ? "ok " : "FAIL ") + ago(d.last_sentinel.time) : "",
  }));
  rows.sort((a, b) => { const x = a[sortKey] ?? "", y = b[sortKey] ?? ""; return (x > y ? 1 : x < y ? -1 : 0) * sortDir; });
  $("#drives-table").innerHTML = `<tr>${cols.map(([k, l]) => `<th data-k="${k}">${l}${sortKey === k ? (sortDir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr>` +
    rows.map((d) => {
      const v = visual(d);
      return `<tr data-drive="${esc(d.id)}"><td>${esc(d.port)}</td><td><b>${esc(d.id)}</b></td><td><span class="dot" style="background:${GCOL(d.group)}"></span>${esc(d.group)}</td>
      <td><span class="st" style="color:${v.color}">${esc(d.status)}</span></td><td class="num">${d.cycles_done}</td>
      <td>${d.worker.running ? `${esc(d.phase || "")} c${d.cycle ?? ""} ${WL[d.workload] || ""}` : ""}</td><td class="num">${fmtNum(d.fdw, 2)}</td>
      <td class="num">${fmtK(d.file_ops)}</td><td class="num">${d.failures}</td><td class="num">${d.transients}</td><td class="num">${d.recoveries}</td>
      <td>${esc(d.first)}</td><td class="num">${fmtNum(d.wL, 2)}</td><td class="num">${fmtNum(d.wS, 2)}</td><td>${esc(d.letter || "")}</td><td>${esc(d.sent)}</td></tr>`;
    }).join("");
  $$("#drives-table th").forEach((th) => (th.onclick = () => { const k = th.dataset.k; sortDir = sortKey === k ? -sortDir : 1; sortKey = k; renderDrivesTable(); }));
  $$("#drives-table tr[data-drive]").forEach((tr) => (tr.onclick = () => openDrawer(tr.dataset.drive)));
  $("#drives-note").textContent = "FDW = full-drive writes (bytes written / usable capacity). Click a row for details.";
}

/* ---------- drawer ------------------------------------------------------------------------- */
async function openDrawer(id) {
  drawerId = id;
  $("#drawer").classList.add("open");
  $("#scrim").classList.add("on");
  $("#drawer-body").innerHTML = `<div class="list-msg">Loading ${esc(id)}…</div>`;
  await refreshDrawer();
}
function closeDrawer() { drawerId = null; $("#drawer").classList.remove("open"); $("#scrim").classList.remove("on"); }
async function refreshDrawer() {
  if (!drawerId) return;
  try { drawerData = await getJSON(`/api/drive/${encodeURIComponent(drawerId)}`); } catch (e) { return; }
  renderDrawer();
}
function renderDrawer() {
  const x = drawerData, d = S && S.drives[drawerId];
  if (!x || !d) return;
  const v = visual(d), it = x.intake || {}, st = x.state || {};
  const cyc = (x.cycles || []).map((r) => ({ ...r, cycle: +r.cycle, w: +r.write_mbps, r: +r.verify_mbps }));
  const wlCol = { large: "#38bdf8", medium: "#a78bfa", small: "#fb7185" };
  const series = ["large", "medium", "small"].map((w) => ({ name: w, color: wlCol[w], points: cyc.filter((r) => r.workload === w && r.outcome === "pass").map((r) => [r.cycle, r.w]) }));
  series.marks = cyc.filter((r) => r.outcome !== "pass").map((r) => r.cycle);
  const ff = st.first_failure, eol = st.end_of_life;
  const stats = [
    ["Cycles", st.cycles_done ?? 0], ["Failures", (st.failure_cycles || []).length], ["Full-drive writes", fmtNum(d.fdw, 2)], ["File operations", fmtK(d.file_ops)],
    ["Transient (D3)", st.transients || 0], ["Recovered (D2)", st.recoveries || 0], ["First failure", ff ? `c${ff.cycle} ${ff.code}` : "—"], ["End of life", eol ? `c${eol.cycle} ${eol.code}${eol.confirmed ? "" : "?"}` : "—"],
  ];
  const lastRows = cyc.slice(-25).reverse().map((r) => `<tr><td>${r.cycle}</td><td>${WL[r.workload] || r.workload}</td><td style="color:${r.outcome === "pass" ? "var(--ok)" : "var(--bad)"}">${esc(r.outcome)}</td>
    <td class="num">${fmtShort(+r.duration_s)}</td><td class="num">${fmtNum(r.w, 2)}</td><td class="num">${fmtNum(r.r, 2)}</td><td class="num">${fmtNum(+r.delete_fps, 1)}</td>
    <td class="num">${r.files_corrupt}/${r.files_missing}</td><td class="num">${r.transient_errors}</td></tr>`).join("");
  const sent = (x.sentinel || []).slice(-8).reverse().map((s) => `<tr><td>${hhmm(s.time)}</td><td>${esc(s.phase)}</td><td style="color:${s.ok === "1" ? "var(--ok)" : "var(--bad)"}">${s.ok === "1" ? "ok" : esc(s.failed || "fail")}</td><td>${esc(s.identity)}</td><td>${esc(s.sentinel)}</td><td>${esc(s.boot)}</td><td>${esc(s.upcase)}</td><td>${esc(s.rootdir)}</td></tr>`).join("");
  const evs = (x.events || []).slice(-60).reverse().map((e) => { const [cls, txt] = evText(e); return `<div class="ev ${cls}"><span class="t">${hhmm(e.time)}</span><span class="d">c${e.cycle}</span><span class="x">${txt}</span></div>`; }).join("");
  const kv = [
    ["Hub / port", d.role === "test" ? `${d.hub} / ${d.port}` : "spare"], ["USB serial", it.usb_serial], ["VID:PID", it.vid_pid], ["Model", it.model || `${it.vendor || ""} ${it.product || ""}`],
    ["Usable bytes", it.usable_bytes ? `${fmtNum(it.usable_bytes)} (${fmtGB(it.usable_bytes)})` : ""], ["PC port (hub key)", it.hub_key], ["Port chain in hub", it.port_chain],
    ["Removal policy", it.removal_policy == null ? "" : `${it.removal_policy}${it.removal_policy === 3 ? " (quick removal)" : ""}`], ["Raw exFAT checks", (it.exfat || {}).ok ? "on" : "off"],
    ["Enrolled", hhmm(it.enrolled_at)], ["Prior use (run 1)", it.prior ? `${it.prior.cycles} cycles, ${fmtGB(it.prior.bytes_written)}` : "new"], ["Warnings", (it.warnings || []).join("; ")],
  ].map(([k, val]) => `<div class="k">${k}</div><div class="v">${esc(val ?? "")}</div>`).join("");
  $("#drawer-body").innerHTML = `
    <div class="dh"><span class="led ${v.pulse ? "pulse" : ""}" style="background:${v.color};--lc:${v.color}"></span><span class="id">${esc(d.id)}</span>
      <span class="chip" style="--gc:${GCOL(d.group)};background:${GCOL(d.group)}">${esc(d.group)}</span><span class="pill">${esc(d.status)}</span>
      <span class="muted small">${d.worker.running ? `${esc(d.phase)} · cycle ${d.cycle} (${esc(d.workload)})` : ""}</span></div>
    <div class="stats">${stats.map(([l, val]) => `<div class="s"><div class="l">${l}</div><div class="v">${esc(val)}</div></div>`).join("")}</div>
    <h3>Write throughput per passing cycle</h3>${svgScatter(series, { unit: "MB/s vs cycle" })}
    <div class="legend">${series.map((s) => `<span><span class="dot" style="background:${s.color}"></span>${s.name}</span>`).join("")}<span><span class="dot" style="background:var(--bad)"></span>failed cycle</span></div>
    <h3 style="margin-top:14px">Cycles</h3><div class="table-wrap"><table class="tbl"><tr><th>#</th><th>W</th><th>outcome</th><th>time</th><th>write</th><th>read</th><th>del/s</th><th>bad/miss</th><th>D3</th></tr>${lastRows}</table></div>
    <h3 style="margin-top:14px">Checks of data at rest and file-system records</h3><div class="table-wrap"><table class="tbl"><tr><th>time</th><th>phase</th><th>result</th><th>identity</th><th>sentinel</th><th>boot</th><th>up-case</th><th>root dir</th></tr>${sent}</table></div>
    <h3 style="margin-top:14px">Events</h3><div class="events">${evs}</div>
    <h3 style="margin-top:14px">Intake</h3><div class="kv">${kv}</div>
    ${S.sim ? `<div class="btn-row"><button class="btn small" id="sim-unplug">Simulate: unplug 20 s</button><button class="btn small" id="sim-gone">Simulate: unplug for good</button><button class="btn small" id="sim-replug">Simulate: re-plug</button></div>` : ""}`;
  if (S.sim) {
    $("#sim-unplug").onclick = () => runJob("sim_unplug", { drive: d.id, seconds: 20 }, "unplug");
    $("#sim-gone").onclick = () => runJob("sim_unplug", { drive: d.id, seconds: 999999 }, "unplug");
    $("#sim-replug").onclick = () => runJob("sim_replug", { drive: d.id }, "re-plug");
  }
}

/* ---------- setup ------------------------------------------------------------------------- */
function planGrid(hub, nextId) {
  const slots = S.campaign.slots.filter((s) => s.hub === hub).sort((a, b) => a.port - b.port);
  return `<div class="small muted">Hub ${esc(hub)} — port 1 is the port nearest the cable (put a numbered label on each port)</div><div class="plan">${slots.map((s) => {
    const d = S.drives[s.drive] || {};
    return `<div class="slot ${d.enrolled ? "done" : ""} ${s.drive === nextId ? "next" : ""}" style="border-left:4px solid ${GCOL(d.group)}"><div class="p">${esc(hub)} · port ${s.port}</div><div class="d">${esc(s.drive)}</div></div>`;
  }).join("")}</div>`;
}
function checkRow(c) {
  const ic = { ok: "✓", warn: "!", fail: "✗", info: "i" }[c.status] || "·";
  const fix = c.fix ? `<button class="btn small" data-fix="${esc(c.id)}">${esc(c.fix)}</button>` : "";
  return `<div class="check ${esc(c.status)}"><span class="ic">${ic}</span><div><div class="tt">${esc(c.title)}</div><div class="dd">${esc(c.detail)}</div></div>${fix}</div>`;
}
function renderSetup() {
  const c = S.campaign, phase = S.run.phase;
  if (phase !== "setup") {
    $("#setup").innerHTML = `<div class="card"><h3>Setup is closed</h3><p class="muted">The test started ${hhmm(S.run.started_at)}. Host checks:</p><div class="checks">${(S.host.checks || []).map(checkRow).join("")}</div></div>`;
    bindFixButtons();
    return;
  }
  const order = c.slots.slice().sort((a, b) => (a.hub + String(a.port).padStart(2, "0") > b.hub + String(b.port).padStart(2, "0") ? 1 : -1)).map((s) => s.drive);
  const spares = c.spares;
  const nextSpare = spares.find((s) => !(S.drives[s] || {}).enrolled);
  const nextTest = order.find((id) => !(S.drives[id] || {}).enrolled);
  const nextId = nextSpare || nextTest;
  const nextSlot = c.slots.find((s) => s.drive === nextId);
  const selftest = (setupData || {}).selftest;
  const cands = ((setupData || {}).candidates || []).filter((x) => !x.enrolled_as);
  const enrolledHere = ((setupData || {}).candidates || []).filter((x) => x.enrolled_as);
  const ready = S.readiness || {};
  const checks = S.host.checks || [];
  const nOk = checks.filter((x) => x.status === "ok").length;
  const step = (n, cls, title, body) => `<div class="step ${cls}"><div class="num">${n}</div><div class="card"><h3>${title}</h3>${body}</div></div>`;
  const nextGroup = nextId && S.drives[nextId] ? S.campaign.groups[S.drives[nextId].group] : null;
  const fits = (x) => !nextGroup || !x.total || Math.abs(x.total / (nextGroup.capacity_gb * 1e9) - 1) <= 0.15;
  const candRows = cands.length ? `<table class="tbl"><tr><th>Letter</th><th>Label</th><th>File system</th><th>Size</th><th>USB serial</th><th>PC port / chain</th><th>Earlier ID</th><th></th></tr>` +
    cands.map((x, i) => `<tr><td><b>${esc(x.letter ? x.letter + ":" : "—")}</b></td><td>${esc(x.label)}</td><td>${esc(x.fs || "RAW")}</td><td>${fmtGB(x.total)}</td><td class="mono">${esc(x.serial)}</td>
      <td class="small">${esc((x.hub_key || "").split("#").slice(-1)[0] || "—")} ${esc(x.port_chain || "")}</td><td>${esc(x.old_identity || "")}</td>
      <td>${!nextId ? "" : fits(x) ? `<button class="btn small primary" data-enroll="${i}">Enroll as ${esc(nextId)}</button>` : `<span class="small muted">not a ${esc(S.drives[nextId].group)} stick</span>`}</td></tr>`).join("") + `</table>` +
      (cands.length > 1 ? `<div class="small" style="color:var(--warn);margin-top:6px">Several new sticks are plugged in: make sure you pick the one labelled ${esc(nextId || "")} (plug them in one at a time to be safe).</div>` : "") :
    `<div class="list-msg">No new USB drive found. Plug in <b>${esc(nextId || "the next drive")}</b>${nextSlot ? ` (hub ${esc(nextSlot.hub)}, port ${nextSlot.port})` : ""} and wait a few seconds.</div>`;
  $("#setup").innerHTML = `<div class="steps">
    ${step(1, nOk === checks.length && checks.length ? "done" : "now", "Prepare the PC", `<p class="muted">Run the rig from the PC's own disk (e.g. <code>C:\\FlashRel-Rig</code>). Fix what is red or yellow; every fix is logged and can be undone after the test.</p>
      <div class="checks">${checks.map(checkRow).join("") || `<div class="list-msg">checking…</div>`}</div>
      <div class="btn-row"><button class="btn" id="btn-recheck">Check again</button><button class="btn" id="btn-undo">Undo host changes</button></div>`)}
    ${step(2, "", "Connect the two hubs", `<ul class="muted">
      <li>Plug each hub's 12 V adapter in (one power strip), switch every port switch <b>on</b> and leave them on.</li>
      <li>Plug each hub's cable straight into a <b>rear</b> USB port of the PC (not the front panel, no extension, never one hub into the other). Use two ports far apart, ideally on different USB controllers.</li>
      <li>Keyboard and mouse stay on other PC ports. No test drive goes into the PC itself; the two spare drives stay unplugged except for the self-test and the post-test port checks.</li>
      <li>Stick a label with the drive ID on every stick, and number the hub ports 1–10.</li></ul>
      ${planGrid("A", nextId)}${planGrid("B", nextId)}<div class="small muted">Spares: ${spares.map(esc).join(", ")}</div>`)}
    ${step(3, selftest && selftest.passed ? "done" : nextSpare ? "now" : "", "Enroll a spare and run the self-test", `<p class="muted">Plug in spare <b>${esc(spares[0])}</b> (any hub port), enroll it below, then run the self-test (≈ 2 minutes). Do the same with <b>${esc(spares[1])}</b>, then unplug both spares and keep them for the post-test.</p>
      <div class="btn-row">${spares.map((s) => `<button class="btn" data-selftest="${esc(s)}" ${(S.drives[s] || {}).enrolled ? "" : "disabled"}>Self-test on ${esc(s)}</button>`).join("")}</div>
      ${selftest ? `<div class="checks">${selftest.checks.map((k) => checkRow({ status: k.ok ? "ok" : k.warn_only ? "warn" : "fail", title: k.title, detail: typeof k.detail === "string" ? k.detail : JSON.stringify(k.detail) })).join("")}</div><div class="small muted">self-test on ${esc(selftest.drive)} at ${hhmm(selftest.time)}: <b>${selftest.passed ? "passed" : "FAILED"}</b></div>` : ""}`)}
    ${step(4, nextTest ? (nextSpare ? "" : "now") : "done", "Enroll the 20 test drives, one at a time", `
      ${nextId ? `<div class="next-card"><div class="muted small">next</div><div class="big">${esc(nextId)} ${nextSlot ? `→ hub ${esc(nextSlot.hub)}, port ${nextSlot.port}` : "(spare)"}</div>
      <div class="muted small">Plug in the stick labelled <b>${esc(nextId)}</b>${nextSlot ? ` into hub ${esc(nextSlot.hub)} port ${nextSlot.port}` : ""}. Leave the enrolled drives plugged in. Enrolling <b>formats the stick</b> (exFAT, 32 KB) and writes its identity.</div></div>` : `<div class="next-card"><div class="big">All drives enrolled ✓</div></div>`}
      ${candRows}
      ${S.sim ? `<div class="btn-row"><button class="btn" id="btn-sim-enroll">Enroll all (simulation)</button></div>` : ""}
      <div class="small muted" style="margin-top:6px">Enrolled and plugged in: ${enrolledHere.map((x) => esc(x.enrolled_as)).join(", ") || "none yet"}</div>`)}
    ${step(5, "", "Start the 5-day test", `
      ${(ready.problems || []).length ? `<ul class="problems">${ready.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : `<p style="color:var(--ok)"><b>Ready.</b></p>`}
      ${(ready.warnings || []).length ? `<ul class="warnings">${ready.warnings.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
      <p class="muted">The 20 workers start 15 s apart, alternating between the hubs. The test then runs unattended for ${c.duration_h} hours and censors the drives still working at the stop.</p>
      <button class="btn primary" id="btn-start" ${(ready.problems || []).length && !S.sim ? "disabled" : ""}>Start the ${c.duration_h}-hour test</button>`)}
  </div>`;
  bindFixButtons();
  $("#btn-recheck").onclick = () => runJob("checks", {}, "host checks");
  $("#btn-undo").onclick = async () => { if (await confirmBox("Undo host changes", "Put back every PC setting the rig changed (sleep, USB suspend, Windows Update pause, AutoPlay, Defender exclusion, auto-resume)?", "Undo")) runJob("undo", {}, "undo"); };
  $$("[data-selftest]").forEach((b) => (b.onclick = () => runJob("selftest", { drive: b.dataset.selftest }, `self-test ${b.dataset.selftest}`)));
  $$("[data-enroll]").forEach((b) => (b.onclick = async () => {
    const x = cands[+b.dataset.enroll];
    const ok = await confirmBox(`Enroll ${nextId}`, `<p>Format <b>${esc(x.letter ? x.letter + ":" : x.root)}</b> (${fmtGB(x.total)}, ${esc(x.fs || "RAW")}, label “${esc(x.label)}”, USB serial <code>${esc(x.serial)}</code>) as <b>${esc(nextId)}</b>?</p><p style="color:var(--bad)"><b>Everything on this stick will be erased.</b></p><p class="muted">Check that it is the stick labelled ${esc(nextId)}.</p>`, "Format and enroll", true);
    if (ok) runJob("enroll", { drive: nextId, guid: x.guid, root: x.root, serial: x.serial }, `enroll ${nextId}`);
  }));
  if (S.sim) $("#btn-sim-enroll").onclick = () => runJob("sim_enroll_all", {}, "enroll all");
  $("#btn-start").onclick = startTest;
}
function bindFixButtons() {
  $$("[data-fix]").forEach((b) => (b.onclick = async () => {
    if (await confirmBox("Change a PC setting", `<p>${esc(b.textContent)}?</p><p class="muted">The previous value is logged; “Undo host changes” puts it back.</p>`, "Apply")) runJob("fix", { check: b.dataset.fix }, b.textContent);
  }));
}
async function startTest(force = false) {
  try {
    const r = await post("/api/run/start", { force });
    if (r.ok) { toast(`Test started; it stops at ${new Date(r.stop_at).toLocaleString()}`, "ok"); setTab("overview"); return; }
    if (r.needs_confirmation) {
      const ok = await confirmBox("Start with warnings?", `<ul class="warnings">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul><p>Start the ${S.campaign.duration_h}-hour test anyway?</p>`, "Start the test");
      if (ok) startTest(true);
      return;
    }
    toast((r.problems || [r.error]).join("; "), "err");
  } catch (e) { toast(e.message, "err"); }
}

/* ---------- post-test ---------------------------------------------------------------------- */
function renderPost() {
  const phase = S.run.phase, pt = (setupData || {}).posttest || {};
  const drives = S.campaign.slots.slice().sort((a, b) => (a.hub + String(a.port).padStart(2, "0") > b.hub + String(b.port).padStart(2, "0") ? 1 : -1)).map((s) => S.drives[s.drive]).filter(Boolean);
  const res = (sec, id) => { const r = (pt[sec] || {})[id]; return r ? `<span style="color:${r.passed ? "var(--ok)" : "var(--bad)"}">${r.passed ? "passed" : "failed"}</span> <span class="muted tiny">${hhmm(r.time)}</span>` : `<span class="muted">—</span>`; };
  const portRes = (d) => { const r = (pt.portcheck || {})[`${d.hub}${String(d.port).padStart(2, "0")}`]; return r ? `<span style="color:${r.passed ? "var(--ok)" : "var(--bad)"}">${r.passed ? "port ok" : "PORT FAILED"}</span>` : `<span class="muted">—</span>`; };
  const can = phase === "stopped" || phase === "finished";
  const rows = drives.map((d) => {
    const stopped = ["attention", "failed"].includes(d.status);
    return `<tr><td>${esc(d.hub)}${String(d.port).padStart(2, "0")}</td><td><b>${esc(d.id)}</b></td><td><span class="st" style="color:${visual(d).color}">${esc(d.status)}</span></td>
      <td>${d.first_failure ? `c${d.first_failure.cycle} ${esc(d.first_failure.code)}` : ""}</td><td>${res("posttest", d.id)}</td><td>${stopped ? res("recheck", d.id) : ""}</td><td>${stopped ? portRes(d) : ""}</td>
      <td><div class="btn-row" style="margin:0">
        <button class="btn small" data-post="${esc(d.id)}" ${can ? "" : "disabled"}>Post-test check</button>
        ${stopped ? `<button class="btn small" data-recheck="${esc(d.id)}" ${can ? "" : "disabled"}>Re-check (other port)</button>
        <button class="btn small danger" data-retire="${esc(d.id)}" ${can ? "" : "disabled"}>Retire: failed</button>
        <button class="btn small" data-works="${esc(d.id)}" ${can ? "" : "disabled"}>Works again</button>
        <button class="btn small" data-portfault="${esc(d.id)}" ${can ? "" : "disabled"}>Port fault</button>` : ""}</div></td></tr>`;
  }).join("");
  $("#post").innerHTML = `<div class="card"><h3>Post-test protocol</h3>
    ${can ? "" : `<p class="banner info">Available after the stop (${S.run.stop_at ? hhmm(S.run.stop_at) : "120 h after the start"}).</p>`}
    <ol class="muted">
      <li><b>Post-test check</b> of every drive on its test port (project 4.4: access the drive again; files of the last cycle are re-read for data retention; 64 MiB fresh check).</li>
      <li>Unplug the <b>censored</b> drives (they survived) to free ports.</li>
      <li>For each drive in <b>attention</b>: move it to a free port, then <b>Re-check (other port)</b>.</li>
      <li>Put a spare into the port the drive left and press <b>Port check</b> for that spare. If the spare fails there too, the port is at fault: press <b>Port fault</b>. If the spare passes: press <b>Retire: failed</b> when the re-check failed, or <b>Works again</b> when it passed.</li>
      <li>Never let Windows “scan and fix” or format a failed drive: the damage is the evidence.</li>
      <li>Export the data (Data &amp; control) and press <b>Finish</b>.</li></ol>
    <div class="btn-row">${S.campaign.spares.map((s) => `<button class="btn" data-portcheck="${esc(s)}" ${can ? "" : "disabled"}>Port check with ${esc(s)}</button>`).join("")}
      <button class="btn primary" id="btn-finish" ${phase === "stopped" ? "" : "disabled"}>Finish the test</button></div></div>
    <div class="card" style="margin-top:14px"><div class="table-wrap"><table class="tbl"><tr><th>Port</th><th>Drive</th><th>Status</th><th>First failure</th><th>Post-test</th><th>Re-check</th><th>Port check</th><th></th></tr>${rows}</table></div></div>`;
  $$("[data-post]").forEach((b) => (b.onclick = () => runJob("posttest", { drive: b.dataset.post }, `post-test ${b.dataset.post}`)));
  $$("[data-recheck]").forEach((b) => (b.onclick = () => runJob("recheck", { drive: b.dataset.recheck }, `re-check ${b.dataset.recheck}`)));
  $$("[data-portcheck]").forEach((b) => (b.onclick = () => runJob("portcheck", { drive: b.dataset.portcheck }, `port check ${b.dataset.portcheck}`)));
  $$("[data-retire]").forEach((b) => (b.onclick = async () => { if (await confirmBox(`Retire ${b.dataset.retire}`, "Confirm the end of life of this drive (it failed its re-check on another port, and the spare passed on its port)?", "Retire", true)) runJob("retire", { drive: b.dataset.retire, classification: "failed", reason: "post-test protocol" }, "retire"); }));
  $$("[data-works]").forEach((b) => (b.onclick = async () => { if (await confirmBox(`${b.dataset.works} works again`, "It passed the re-check on another port and the spare passed on its port: keep its first failure, but do not count an end of life?", "Confirm")) runJob("retire", { drive: b.dataset.works, classification: "recovered", reason: "re-check passed on another port" }, "works again"); }));
  $$("[data-portfault]").forEach((b) => (b.onclick = async () => { if (await confirmBox(`Port fault for ${b.dataset.portfault}`, "The spare failed on this drive's port: count the failure against the port (F5), not the drive?", "Re-attribute")) runJob("retire", { drive: b.dataset.portfault, classification: "port_fault", reason: "spare failed on the same port" }, "port fault"); }));
  const fin = $("#btn-finish");
  if (fin) fin.onclick = async () => { if (await confirmBox("Finish the test", "Mark the campaign finished? (Export the data first.)", "Finish")) { try { await post("/api/run/finish", {}); toast("finished", "ok"); } catch (e) { toast(e.message, "err"); } } };
}

/* ---------- data and control --------------------------------------------------------------- */
function renderData() {
  const sel = $("#note-drive");
  if (sel.options.length <= 1) Object.keys(S.drives).sort().forEach((id) => sel.add(new Option(id, id)));
  const ex = (S.jobs || []).filter((j) => j.kind === "export" && j.status === "done").slice(-1)[0];
  $("#export-result").innerHTML = ex ? `Last export: <code>${esc(ex.result.zip)}</code> (${fmtNum(ex.result.files)} files, ${fmtMB(ex.result.size)})` : "";
  const ph = S.run.phase;
  $("#btn-pause").disabled = ph !== "running";
  $("#btn-resume").disabled = ph !== "paused";
  $("#btn-end").disabled = !["running", "paused"].includes(ph);
}
async function renderHostEvents() {
  // host events come from the snapshot's job/status context; show the operator-facing ones
  const list = (S.events || []).filter((e) => e.type === "note").slice(-40).reverse();
  $("#host-events").innerHTML = list.map((e) => `<div class="ev"><span class="t">${hhmm(e.time)}</span><span class="d">${esc(e.drive)}</span><span class="x">${esc((e.detail || {}).text || "")}</span></div>`).join("") || `<div class="list-msg">No notes yet.</div>`;
}

/* ---------- jobs feedback ------------------------------------------------------------------- */
function watchJobs() {
  (S.jobs || []).forEach((j) => {
    if (!seenJobs.has(j.id) || j._shown) return;
    if (j.status === "done") { toast(`${j.kind} ${j.args.drive || ""}: done`, "ok"); seenJobs.delete(j.id); if (tab === "setup" || tab === "post") loadSetup(); }
    if (j.status === "error") { toast(`${j.kind} ${j.args.drive || ""}: ${j.error}`, "err"); seenJobs.delete(j.id); }
  });
  const running = (S.jobs || []).find((j) => j.status === "running");
  document.title = running ? `⏳ ${running.kind} · FlashRel` : S.run.phase === "running" ? `● ${fmtDur(S.run.elapsed_s)} · FlashRel` : "FlashRel Rig";
}

/* ---------- tabs and polling ---------------------------------------------------------------- */
function setTab(t) {
  tab = t;
  localStorage.setItem("fr-tab", t);
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${t}`));
  clearInterval(setupTimer);
  if (t === "setup" || t === "post") { loadSetup(); setupTimer = setInterval(loadSetup, 3000); }
  render();
}
async function loadSetup() {
  try { setupData = await getJSON("/api/setup"); if (tab === "setup") renderSetup(); if (tab === "post") renderPost(); } catch (e) { /* offline */ }
}
function render() {
  if (!S) return;
  renderHeader();
  watchJobs();
  if (tab === "overview") { renderKpis(); renderHubs(); renderSurvival(); renderHubLoad(); renderEvents(); }
  if (tab === "drives") renderDrivesTable();
  if (tab === "setup" && !document.querySelector("#modal:not([hidden])")) renderSetup();
  if (tab === "post" && !document.querySelector("#modal:not([hidden])")) renderPost();
  if (tab === "data") { renderData(); renderHostEvents(); }
  if (drawerId && drawerData) renderDrawer();
}
let drawerTick = 0;
async function poll() {
  try {
    S = await getJSON("/api/state");
    render();
    if (drawerId && ++drawerTick % 3 === 0) refreshDrawer();
  } catch (e) {
    const b = $("#banner");
    b.hidden = false; b.className = "banner";
    b.innerHTML = "The rig software is not answering. If it was shut down, start it again with <b>Start FlashRel.bat</b>.";
  } finally { setTimeout(poll, 2000); }
}

function init() {
  const qp = new URLSearchParams(location.search);  // e.g. ?tab=setup&theme=light&drive=A8-06
  if (qp.get("tab")) tab = qp.get("tab");
  const theme = qp.get("theme") || localStorage.getItem("fr-theme") || "dark";
  document.documentElement.dataset.theme = theme;
  if (qp.get("drive")) setTimeout(() => openDrawer(qp.get("drive")), 300);
  $("#theme-btn").onclick = () => { const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = t; localStorage.setItem("fr-theme", t); };
  $$("#tabs button").forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));
  $$("#surv-unit button").forEach((b) => (b.onclick = () => { survUnit = b.dataset.u; $$("#surv-unit button").forEach((x) => x.classList.toggle("on", x === b)); renderSurvival(); }));
  $$("#ev-filter button").forEach((b) => (b.onclick = () => { evFilter = b.dataset.f; $$("#ev-filter button").forEach((x) => x.classList.toggle("on", x === b)); renderEvents(); }));
  $("#drawer-close").onclick = closeDrawer;
  $("#scrim").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  $("#btn-export").onclick = () => runJob("export", {}, "export");
  $("#btn-note").onclick = async () => { try { await post("/api/note", { text: $("#note-text").value, drive: $("#note-drive").value }); $("#note-text").value = ""; toast("note saved", "ok"); } catch (e) { toast(e.message, "err"); } };
  $("#btn-temp").onclick = async () => { try { await post("/api/temperature", { celsius: parseFloat($("#temp-c").value), location: $("#temp-loc").value }); $("#temp-c").value = ""; toast("reading saved", "ok"); } catch (e) { toast(e.message, "err"); } };
  $("#btn-pause").onclick = async () => { if (await confirmBox("Pause the test", "Every drive stops after the current file; the cycles in progress are not counted. The test clock keeps running.", "Pause")) post("/api/run/pause", { reason: "operator" }).then(() => toast("pausing…")).catch((e) => toast(e.message, "err")); };
  $("#btn-resume").onclick = () => post("/api/run/resume", {}).then(() => toast("resuming", "ok")).catch((e) => toast(e.message, "err"));
  $("#btn-end").onclick = async () => { if (await confirmBox("End the test now", "All running drives are censored now and the post-test protocol begins. This cannot be undone.", "End the test", true)) post("/api/run/end", { reason: "operator" }).then(() => toast("ending…")).catch((e) => toast(e.message, "err")); };
  $("#btn-shutdown").onclick = async () => { if (await confirmBox("Shut down the rig software", "Workers stop after the current file (not counted). Start the rig again with Start FlashRel.bat to continue the test.", "Shut down", true)) post("/api/shutdown", { confirm: true }).then(() => toast("shutting down")).catch((e) => toast(e.message, "err")); };
  setTab(tab);
  poll();
}
init();
