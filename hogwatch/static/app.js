"use strict";
/* HogWatch dashboard. No external libraries: the page must keep working while
   the internet is down, which is exactly when you'll want to look at it.
   Device and program names come from the network (anyone can name their device
   anything), so text is always inserted with textContent, never innerHTML. */

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";

function build(el, attrs, kids) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const h = (tag, attrs, ...kids) => build(document.createElement(tag), attrs, kids);
const s = (tag, attrs, ...kids) => build(document.createElementNS(SVGNS, tag), attrs, kids);

/* ------------------------------------------------------------------ formatting */

const fmtRate = (x) => (x == null ? "–" : x < 0.05 ? "0" : x < 10 ? x.toFixed(1) : Math.round(x).toLocaleString());
const fmtMs = (x) => (x == null ? "–" : Math.round(x).toLocaleString());
function fmtVolume(mb) {
  if (mb == null) return "–";
  if (mb >= 1000) return (mb / 1000).toFixed(mb >= 10000 ? 0 : 1) + " GB";
  if (mb >= 1) return Math.round(mb) + " MB";
  return Math.max(0, Math.round(mb * 1000)) + " KB";
}
function fmtDur(sec) {
  sec = Math.round(sec);
  if (sec < 60) return sec + " sec";
  const m = Math.floor(sec / 60);
  if (m < 60) return m + " min";
  const hr = Math.floor(m / 60), rm = m % 60;
  return hr + " hr" + (rm ? " " + rm + " min" : "");
}
function fmtClock(ts, forceDay) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (!forceDay && d.toDateString() === new Date().toDateString()) return t;
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + ", " + t;
}
const KIND_LABEL = {
  directv: "DirecTV", tv: "TV / streaming", console: "Game console", phone: "Phone", tablet: "Tablet",
  computer: "Computer", speaker: "Smart speaker", camera: "Camera",
};

/* ------------------------------------------------------------------ state & fetching */

const state = { hours: 1, now: null, timeline: null, usage: null, incidents: null, incidentLimit: 15, offline: false };
try { const saved = +localStorage.getItem("hw-hours"); if (saved) state.hours = saved; } catch (e) { /* storage blocked */ }

async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(path + " returned " + r.status);
  return r.json();
}

async function refreshNow() {
  try {
    state.now = await getJSON("/api/now");
    state.offline = false;
  } catch (e) {
    state.offline = true;
  }
  renderNow();
}

async function refreshHistory() {
  const body = $("history-body");
  body.classList.add("loading"); // keep the old render visible, just dimmed
  try {
    const [tl, usage, inc, hic] = await Promise.all([
      getJSON("/api/timeline?hours=" + state.hours),
      getJSON("/api/usage?hours=" + state.hours),
      getJSON("/api/incidents?days=" + Math.max(1, state.hours / 24)),
      getJSON("/api/hiccups?hours=" + state.hours),
    ]);
    Object.assign(state, { timeline: tl, usage, incidents: inc, hiccups: hic });
    renderHistory();
  } catch (e) {
    /* server unreachable: keep what's on screen; the status card says so */
  } finally {
    body.classList.remove("loading");
  }
}

/* ------------------------------------------------------------------ right now */

function badge(level, glyph) {
  return h("span", { class: "badge " + level, "aria-hidden": "true" }, glyph);
}

function renderNow() {
  const n = state.now;
  renderSources(n);
  const st = $("status");
  st.replaceChildren();
  if (state.offline || !n) {
    st.append(badge("muted", "–"), h("div", { class: "status-text" },
      h("div", { class: "status-line" }, "HogWatch isn't running"),
      h("p", { class: "status-meta" }, "Start it with start-hogwatch.cmd in the hogwatch folder, then reload this page.")));
    return;
  }
  const lat = n.latency || {};
  const S = {
    ok: ["good", "✓", "Internet is running normally"],
    slow: ["serious", "!", "Internet is SLOW right now"],
    down: ["critical", "×", "Internet is DOWN right now"],
    starting: ["muted", "…", "Starting up…"],
  }[n.status] || ["muted", "?", n.status];
  const cap = n.capacity;
  st.append(
    h("div", { class: "hero", "aria-label": "Current ping" },
      h("span", { class: "hero-num" }, fmtMs(lat.internet_ms)), h("span", { class: "hero-unit" }, "ms ping")),
    h("div", { class: "status-text" },
      h("div", { class: "status-line" }, badge(S[0], S[1]), S[2]),
      h("p", { class: "status-meta" },
        `Normally ${fmtMs(lat.normal_ms)} ms · counts as slow above ${fmtMs(lat.slow_above_ms)} ms · ` +
        `lost ${Math.round(lat.loss_pct || 0)}%`),
      h("p", { class: "status-meta" },
        (n.path && n.path.node ? `${n.path.node} eero (yours): ${fmtMs(lat.node_ms)} ms · ` : "") +
        `${(n.path && n.path.gateway) || "main"} eero: ${fmtMs(lat.lan_ms)} ms · AT&T gateway: ${fmtMs(lat.isp_ms)} ms` +
        (cap && (cap.down || cap.up) ? ` · plan speed: ${fmtRate(cap.down)} down / ${fmtRate(cap.up)} up Mbps` : ""))),
  );

  const li = $("live-incident");
  li.replaceChildren();
  if (n.incident) {
    const d = n.incident.details || {};
    li.append(h("div", { class: "alert", role: "status" },
      h("h3", {}, badge("critical", "!"), `Slowdown happening now (started ${fmtClock(n.incident.start_ts)})`),
      h("p", {}, n.incident.headline),
      suspectList(d.suspects, 3)));
  }
  renderNowDevices(n);
  renderNowApps(n.pc || {});
  renderSetup(n);
}

function renderSources(n) {
  const ul = $("sources");
  ul.replaceChildren();
  const chip = (color, text) => h("li", { class: "chip" }, h("span", { class: "dot", style: `background:var(${color})` }), text);
  if (state.offline || !n) { ul.append(chip("--critical", "Not running")); return; }
  ul.append(chip("--good", "Watching for " + fmtDur(n.uptime_s || 0)));
  const e = n.eero || {};
  ul.append({
    ok: chip("--good", "eero: connected" + (e.network ? ` (${e.network})` : "")),
    login_needed: chip("--critical", "eero: login expired"),
    error: chip("--serious", "eero: can't reach"),
  }[e.status] || chip("--muted", "eero: not connected"));
  ul.append(n.pc && n.pc.per_app ? chip("--good", "This PC: per-program on") : chip("--serious", "This PC: totals only"));
}

function rateBars(rows, scaleMax) {
  /* rows: [{name, meta:[...], tags:[...], down, up}] -> paired horizontal bars, values at the tips. */
  const pct = (v) => Math.max(0.4, Math.min(100, (100 * (v || 0)) / scaleMax)) + "%";
  return h("div", { class: "bars" }, rows.map((r) =>
    h("div", { class: "bar-row" },
      h("div", { class: "bar-label" },
        h("span", { class: "bar-name" }, r.name),
        (r.tags || []).map((t) => h("span", { class: "tag" }, t)),
        r.meta ? h("span", { class: "bar-meta" }, r.meta) : null),
      h("div", { class: "bar-track" },
        h("div", {}, h("div", { class: "bar", style: `width:${pct(r.down)};background:var(--series-1)` })),
        h("span", { class: "bar-val" }, `↓ ${fmtRate(r.down)} Mbps`)),
      h("div", { class: "bar-track" },
        h("div", {}, h("div", { class: "bar", style: `width:${pct(r.up)};background:var(--series-2)` })),
        h("span", { class: "bar-val" }, `↑ ${fmtRate(r.up)} Mbps`)))));
}

function barLegend() {
  return h("div", { class: "legend" },
    h("span", {}, h("i", { class: "key-rect", style: "background:var(--series-1)" }), "Download"),
    h("span", {}, h("i", { class: "key-rect", style: "background:var(--series-2)" }), "Upload"));
}

function renderNowDevices(n) {
  const box = $("now-devices");
  const e = n.eero || {};
  box.replaceChildren();
  $("eero-sub").textContent = e.ts ? `From the eero, updated ${new Date(e.ts * 1000).toLocaleTimeString()}` :
    "From the eero, updated every 30 seconds";
  if (e.status === "not_connected") {
    box.append(h("p", { class: "empty" }, "Not connected to the eero yet, so other devices can't be seen. See “Connect the eero” below."));
    return;
  }
  if (e.status === "login_needed") {
    box.append(h("p", { class: "empty" }, "The eero login expired. Double-click eero-login.cmd to sign in again."));
    return;
  }
  if (e.status === "error") box.append(h("p", { class: "empty" }, "Can't reach the eero cloud right now: " + (e.message || "")));
  const devs = e.devices || [];
  if (!devs.length) { box.append(h("p", { class: "empty" }, "No devices reported yet.")); return; }
  if (!e.usage_reported) {
    box.append(h("p", { class: "empty" }, `The eero lists ${devs.length} connected devices but isn't reporting their speeds. ` +
      "Run eero-check.cmd for details."));
    return;
  }
  const active = devs.filter((d) => (d.down_mbps || 0) + (d.up_mbps || 0) >= 0.05).slice(0, 10);
  const idle = devs.length - active.length;
  if (!active.length) {
    box.append(h("p", { class: "empty" }, `All ${devs.length} devices are idle.`));
    return;
  }
  const scale = Math.max(1, ...active.map((d) => Math.max(d.down_mbps || 0, d.up_mbps || 0)));
  box.append(barLegend(), rateBars(active.map((d) => ({
    name: d.name,
    tags: [d.this_pc ? "this PC" : null, KIND_LABEL[d.kind]].filter(Boolean),
    meta: [d.owner, d.connection === "wired" ? "wired" : "Wi-Fi", d.node].filter(Boolean).join(" · "),
    down: d.down_mbps, up: d.up_mbps,
  })), scale));
  if (idle > 0) box.append(h("p", { class: "empty" }, `${idle} other device${idle === 1 ? " is" : "s are"} idle.`));
}

function renderNowApps(pc) {
  const box = $("now-apps");
  box.replaceChildren();
  $("pc-sub").textContent = `Last 10 seconds · this PC in total: ↓ ${fmtRate(pc.down_mbps)} / ↑ ${fmtRate(pc.up_mbps)} Mbps`;
  if (!pc.per_app) {
    box.append(h("p", { class: "empty" },
      "Per-program detail needs administrator rights. Start HogWatch with start-hogwatch.cmd and click Yes on the Windows prompt."));
    return;
  }
  const apps = (pc.apps || []).filter((a) => a.down_mbps + a.up_mbps >= 0.01);
  if (!apps.length) { box.append(h("p", { class: "empty" }, "Nothing on this PC is using the internet right now.")); return; }
  const scale = Math.max(1, ...apps.map((a) => Math.max(a.down_mbps, a.up_mbps)));
  box.append(barLegend(), rateBars(apps.map((a) => ({ name: a.app, down: a.down_mbps, up: a.up_mbps })), scale));
}

function renderSetup(n) {
  const box = $("setup");
  box.replaceChildren();
  const e = n.eero || {};
  if (e.status === "not_connected" || e.status === "login_needed") {
    box.append(h("div", { class: "alert info" },
      h("h3", {}, "Connect the eero to see every device"),
      h("p", {}, "HogWatch can already see this PC. To see your roommates' devices and the DirecTV boxes, it needs to read the eero:"),
      h("ol", { class: "steps" },
        h("li", {}, "Ask the eero owner to add you as an admin: in the eero app, ", h("strong", {}, "Settings › Network settings › Admins › Add an admin"), "."),
        h("li", {}, "Accept the invite in your own eero app."),
        h("li", {}, "In the hogwatch folder, double-click ", h("code", {}, "eero-login.cmd"),
          " and enter your eero email or phone number, then the code eero sends you."),
        h("li", {}, "That's it. This page picks it up within 15 seconds, with no restart."))));
  }
  if (n.pc && !n.pc.per_app) {
    box.append(h("div", { class: "alert info" },
      h("h3", {}, "See which program on this PC is using the internet"),
      h("p", {}, "Windows only shares per-program network data with administrator tools. Close this HogWatch window, then start it with ",
        h("code", {}, "start-hogwatch.cmd"), " and click Yes on the Windows prompt.")));
  }
}

/* ------------------------------------------------------------------ lag spikes */

function pathLine(path) {
  /* "This PC → Upstairs eero (by cable) → Main eero (wirelessly, 5 GHz) → AT&T → internet" */
  if (!path || !path.chain || !path.chain.length) return null;
  const units = Object.fromEntries((path.units || []).map((u) => [u.name, u]));
  const me = ((state.now && state.now.eero && state.now.eero.devices) || []).find((d) => d.this_pc);
  const parts = [h("strong", {}, "This PC")];
  path.chain.forEach((name, i) => {
    let how;
    if (i === 0) how = me && me.connection === "wifi" ? "by Wi-Fi" : "by cable";
    else {
      const prev = units[path.chain[i - 1]] || {};
      how = prev.wired ? "by cable" : `wirelessly${prev.radio ? ", " + prev.radio : ""}`;
    }
    parts.push(" → ", h("strong", {}, `${name} eero`), ` (${how})`);
  });
  parts.push(" → AT&T → internet");
  return h("p", { class: "path" }, "Your connection: ", parts);
}

const WHERE_ADVICE = {
  eero_link: "Most of your lag spikes are on the wireless link between your eeros, not your internet line. " +
    "An Ethernet cable between those two eeros (a wired backhaul) would fix them. Moving the eeros closer together would also help.",
  pc_link: "Most lag spikes are between this PC and the eero it plugs into. Try a different cable and the eero's other port.",
  eero: "Most lag spikes are inside your main eero. Restart it, and check the eero app for an update.",
  att: "Most lag spikes are at the AT&T gateway. Restart it; if they continue, give AT&T these times.",
  internet: "Most lag spikes happen past your eeros, on AT&T's line or beyond. If they're frequent, give AT&T these times.",
};

function renderHiccups() {
  const box = $("hiccups");
  box.replaceChildren();
  const hc = state.hiccups;
  if (!hc) return;
  const path = hc.path || (state.now && state.now.path);
  const line = pathLine(path);
  if (line) box.append(line);
  const total = hc.summary.reduce((a, x) => a + x.n, 0);
  if (!total) {
    box.append(h("p", { class: "empty" }, "No lag spikes in this period."));
  } else {
    const inGame = hc.summary.reduce((a, x) => a + x.in_game, 0);
    const top = hc.summary[0];
    box.append(
      h("p", { class: "inc-head" }, h("strong", {}, `${total} lag spike${total === 1 ? "" : "s"}`),
        ` in this period${inGame ? `, ${inGame} while a game was running` : ""}:`),
      h("ul", { class: "where-list" }, hc.summary.map((x) =>
        h("li", {}, h("strong", {}, `${x.n}`), ` ${x.where_text} (${fmtDur(x.secs)} in total)`))));
    const advice = top.where_ === "eero_link" && path && path.wired
      ? "Most lag spikes are on the cable between your eeros. Check that both ends are firmly plugged in, or try a different cable."
      : WHERE_ADVICE[top.where_];
    if (total >= 3 && top.n / total >= 0.6 && advice) {
      box.append(h("div", { class: "alert info" }, h("p", {}, advice)));
    }
    const rows = hc.items.slice(0, state.hiccupLimit || 25);
    box.append(h("details", { open: total <= 10 ? true : null }, h("summary", {}, "Each lag spike"),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", { class: "num" }, "Length"), h("th", {}, "How bad"),
          h("th", {}, "Where"), h("th", {}, "Game"), h("th", {}, "Busy on the network then"))),
        h("tbody", {}, rows.map((x) => h("tr", {},
          h("td", {}, new Date(x.start_ts * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit", second: "2-digit" })),
          h("td", { class: "num" }, `${Math.max(1, Math.round(x.end_ts - x.start_ts))} sec`),
          h("td", {}, (x.lost ? `${x.lost} ping${x.lost === 1 ? "" : "s"} lost` : "") +
            (x.lost && x.worst_ms ? ", " : "") + (x.worst_ms ? `worst ${fmtMs(x.worst_ms)} ms` : "")),
          h("td", {}, x.where_text),
          h("td", {}, x.game || "–"),
          h("td", {}, x.busy.length ? x.busy.slice(0, 3).map((b, i) => [i ? ", " : "",
            `${b.name} ↓${fmtRate(b.down_mbps)} ↑${fmtRate(b.up_mbps)}`, b.on_link && !b.this_pc ? " (shares your link)" : ""]) : "nobody heavy"))))))));
  }
  if (hc.eero_events && hc.eero_events.length) {
    box.append(h("p", { class: "card-sub", style: "margin-top:12px;margin-bottom:4px" },
      "eeros that lost their connection and reconnected (this can happen between pings, so it's extra evidence):"),
      h("ul", { class: "where-list" }, hc.eero_events.slice(0, 10).map((e) => h("li", {},
        `${fmtClock(e.ts)}: ${e.unit} eero reconnected` +
        (e.detail.gateway ? " (its internet connection dropped)" :
          e.detail.upstream ? ` (its ${e.detail.radio ? e.detail.radio + " " : ""}wireless link to ${e.detail.upstream} dropped)` : "")))));
  }
}

/* ------------------------------------------------------------------ incidents */

const INC_KIND = {
  eero_link: ["critical", "×", "Link between eeros dropped"],
  congested: ["serious", "!", "Internet crawled"],
  outage: ["critical", "×", "Internet dropped out"],
  home: ["serious", "!", "Home network slow"],
  home_outage: ["critical", "×", "PC lost the eero"],
  ongoing: ["critical", "!", "In progress"],
};

function suspectList(suspects, limit) {
  const busy = (suspects || []).filter((x) => x.heavy || x.guess !== "light use");
  if (!busy.length) return null;
  return h("ul", { class: "suspects" }, busy.slice(0, limit).map((x) =>
    h("li", {}, h("strong", {}, x.label), `: ↓ ${fmtRate(x.down_mbps)} / ↑ ${fmtRate(x.up_mbps)} Mbps, probably ${x.guess}.`)));
}

function renderIncidents() {
  const box = $("incidents");
  box.replaceChildren();
  const since = Date.now() / 1000 - state.hours * 3600;
  const list = (state.incidents || []).filter((i) => (i.end_ts || Date.now() / 1000) >= since);
  if (!list.length) {
    box.append(h("p", { class: "empty" }, "No slowdowns in this period."));
    return;
  }
  const wrap = h("div", { class: "incidents" });
  for (const inc of list.slice(0, state.incidentLimit)) {
    const d = inc.details || {};
    const dur = (inc.end_ts || Date.now() / 1000) - inc.start_ts;
    const k = INC_KIND[inc.end_ts ? inc.kind : "ongoing"] || INC_KIND.congested;
    const body = h("div", { class: "inc-body" });
    if (d.suspects && d.suspects.length) {
      body.append(h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Device"), h("th", { class: "num" }, "Down Mbps"),
          h("th", { class: "num" }, "Up Mbps"), h("th", {}, "Probably"))),
        h("tbody", {}, d.suspects.map((x) => h("tr", {},
          h("td", {}, x.label, x.share_pct ? h("span", { class: "sub" }, `${x.share_pct}% of your ${x.share_dir} speed`) : null),
          h("td", { class: "num" }, fmtRate(x.down_mbps)), h("td", { class: "num" }, fmtRate(x.up_mbps)),
          h("td", {}, x.guess)))))));
    } else if (d.eero_connected === false) {
      body.append(h("p", {}, "Other devices weren't visible (eero not connected)."));
    }
    if (d.pc && d.pc.measured) {
      const apps = (d.pc.apps || []).map((a) => `${a.app} ↓ ${fmtRate(a.down_mbps)} / ↑ ${fmtRate(a.up_mbps)}`);
      body.append(h("p", {}, h("strong", {}, "This PC: "), `↓ ${fmtRate(d.pc.down_mbps)} / ↑ ${fmtRate(d.pc.up_mbps)} Mbps`,
        apps.length ? `. Programs: ${apps.join(", ")} Mbps` : (d.pc.per_app_available ? "" : " (per-program detail was off)")));
    }
    if (d.notes && d.notes.length) body.append(h("ul", {}, d.notes.map((x) => h("li", {}, x))));
    body.append(h("p", { class: "card-sub" },
      `Ping: typical ${fmtMs(d.median_ms)} ms, worst ${fmtMs(d.peak_ms)} ms, normal ${fmtMs(d.normal_ms)} ms, ` +
      `${d.loss_pct || 0}% lost. eero: typical ${fmtMs(d.lan_median_ms)} ms. AT&T gateway: typical ${fmtMs(d.isp_median_ms)} ms.`));
    wrap.append(h("article", { class: "incident" + (inc.end_ts && dur < 30 ? " short" : "") },
      h("div", { class: "inc-top" },
        h("span", { class: "inc-kind" }, badge(k[0], k[1]), k[2] + (inc.end_ts && dur < 30 ? " (brief)" : "")),
        h("span", {}, fmtClock(inc.start_ts)),
        h("span", {}, inc.end_ts ? fmtDur(dur) : "ongoing for " + fmtDur(dur)),
        inc.peak_ms ? h("span", {}, `worst ${fmtMs(inc.peak_ms)} ms`) : null),
      h("p", { class: "inc-head" }, inc.headline || ""),
      h("details", {}, h("summary", {}, "Details"), body)));
  }
  box.append(wrap);
  if (list.length > state.incidentLimit) {
    box.append(h("button", {
      type: "button", class: "more-btn",
      onclick: () => { state.incidentLimit += 30; renderIncidents(); },
    }, `Show ${Math.min(30, list.length - state.incidentLimit)} more`));
  }
}

/* ------------------------------------------------------------------ line chart */

function niceTicks(max) {
  if (!(max > 0)) max = 1;
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const f = raw / mag;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
  const out = [];
  for (let v = 0; v <= Math.ceil(max / step) * step + step / 1e6; v += step) out.push(+v.toPrecision(10));
  return out;
}

const TIME_STEPS = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
function timeTicks(start, end, maxTicks) {
  const step = TIME_STEPS.find((st) => (end - start) / st <= maxTicks) || 86400;
  const off = new Date(start * 1000).getTimezoneOffset() * 60; // align ticks to local clock time
  const out = [];
  for (let t = Math.ceil((start + off) / step) * step - off; t <= end; t += step) out.push(t);
  return { ticks: out, step };
}
function tickLabel(t, step, span) {
  const d = new Date(t * 1000);
  if (step >= 86400) return d.toLocaleDateString([], { weekday: "short", day: "numeric" });
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: step % 3600 ? "2-digit" : undefined });
  return span > 86400 ? d.toLocaleDateString([], { weekday: "short" }) + " " + time : time;
}

const tooltip = () => $("tooltip");
function placeTooltip(x, y) {
  const tt = tooltip();
  const r = tt.getBoundingClientRect();
  let left = x + 16, top = y + 16;
  if (left + r.width > window.innerWidth - 8) left = x - r.width - 16;
  if (top + r.height > window.innerHeight - 8) top = y - r.height - 16;
  tt.style.left = Math.max(8, left) + "px";
  tt.style.top = Math.max(8, top) + "px";
}

/**
 * Multi-series line chart with a crosshair tooltip.
 * opts: {series:[{name, color, points:[[t, v, extra?]]}], start, end, bucket, bands:[[t0,t1]],
 *        unit, yFloor, height, label, empty}
 */
function lineChart(root, opts) {
  root._chart = opts;
  root.replaceChildren();
  const series = opts.series.filter((x) => x.points.some((p) => p[1] != null));
  if (!series.length) {
    root.append(h("p", { class: "empty" }, opts.empty || "No data for this period yet."));
    return;
  }
  const W = Math.max(280, Math.floor(root.clientWidth || 600));
  const H = opts.height || 220;
  const vals = series.flatMap((x) => x.points.map((p) => p[1]).filter((v) => v != null));
  const ticks = niceTicks(Math.max(opts.yFloor || 1, ...vals));
  const yMax = ticks[ticks.length - 1];
  const directLabels = series.length <= 4 && W >= 520;
  const labelW = directLabels ? Math.min(120, 14 + 6.6 * Math.max(...series.map((x) => x.name.length))) : 0;
  const m = { l: 12 + 7 * yMax.toLocaleString().length, r: 10 + labelW, t: 12, b: 26 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const span = opts.end - opts.start;
  const X = (t) => m.l + ((t - opts.start) / span) * iw;
  const Y = (v) => m.t + ih - (v / yMax) * ih;

  if (series.length >= 2 || (opts.bands && opts.bands.length)) {
    root.append(h("div", { class: "legend" },
      series.map((x) => h("span", {}, h("i", { class: "key-line", style: `background:${x.color}` }), x.name)),
      opts.bands && opts.bands.length ? h("span", {}, h("i", { class: "key-rect", style: "background:var(--band)" }), "Slowdown") : null));
  }

  const g = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", tabindex: "0",
    "aria-label": (opts.label || "Chart") + ". Use left and right arrow keys to read values." });

  for (const [a, b] of opts.bands || []) {
    const x0 = X(Math.max(a, opts.start)), x1 = X(Math.min(b, opts.end));
    if (x1 > x0) g.append(s("rect", { x: x0, y: m.t, width: Math.max(2, x1 - x0), height: ih, style: "fill:var(--band)" }));
  }
  for (const v of ticks) {
    g.append(s("line", { x1: m.l, x2: m.l + iw, y1: Y(v), y2: Y(v), style: `stroke:var(${v === 0 ? "--axis" : "--grid"});stroke-width:1` }));
    g.append(s("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end", class: "axis-text" }, v.toLocaleString()));
  }
  const tt = timeTicks(opts.start, opts.end, Math.max(2, Math.floor(iw / 90)));
  for (const t of tt.ticks) {
    g.append(s("text", { x: X(t), y: H - 6, "text-anchor": "middle", class: "axis-text" }, tickLabel(t, tt.step, span)));
  }

  const gap = (opts.bucket || 10) * 2.5;
  const ends = [];
  for (const x of series) {
    let d = "", prev = null, runLen = 0;
    const singles = [];
    for (const [t, v] of x.points) {
      if (v == null) { if (runLen === 1) singles.push(prev); prev = null; runLen = 0; continue; }
      const cont = prev && t - prev[0] <= gap;
      if (!cont && runLen === 1) singles.push(prev);
      d += (cont ? "L" : "M") + X(t).toFixed(1) + " " + Y(v).toFixed(1);
      runLen = cont ? runLen + 1 : 1;
      prev = [t, v];
    }
    if (runLen === 1 && prev) singles.push(prev);
    g.append(s("path", { d, style: `fill:none;stroke:${x.color};stroke-width:2;stroke-linejoin:round;stroke-linecap:round` }));
    for (const [t, v] of singles) g.append(s("circle", { cx: X(t), cy: Y(v), r: 2.5, style: `fill:${x.color}` }));
    const last = [...x.points].reverse().find((p) => p[1] != null);
    if (last) ends.push({ x, t: last[0], v: last[1] });
  }

  if (directLabels) {
    // Label line ends in the right margin; drop a label rather than nudge it away from its line.
    const placed = [];
    for (const e of ends.sort((a, b) => Y(a.v) - Y(b.v))) {
      const y = Y(e.v);
      g.append(s("circle", { cx: X(e.t), cy: y, r: 4, style: `fill:${e.x.color};stroke:var(--surface);stroke-width:2` }));
      if (placed.some((py) => Math.abs(py - y) < 13)) continue;
      placed.push(y);
      g.append(s("text", { x: m.l + iw + 8, y: y + 4, class: "end-label" }, e.x.name));
    }
  }

  // Hover / keyboard layer: snap to the nearest time that has data and list every series there.
  const times = [...new Set(series.flatMap((x) => x.points.filter((p) => p[1] != null).map((p) => p[0])))].sort((a, b) => a - b);
  const byTime = series.map((x) => new Map(x.points.map((p) => [p[0], p])));
  const cross = s("line", { y1: m.t, y2: m.t + ih, style: "stroke:var(--axis);stroke-width:1", visibility: "hidden" });
  const dots = s("g", {});
  g.append(cross, dots);
  const hit = s("rect", { x: m.l, y: m.t, width: iw, height: ih, style: "fill:transparent;cursor:crosshair" });
  g.append(hit);
  let idx = -1;

  function show(i, cx, cy) {
    if (i < 0 || i >= times.length) return;
    idx = i;
    const t = times[i];
    cross.setAttribute("x1", X(t)); cross.setAttribute("x2", X(t)); cross.setAttribute("visibility", "visible");
    dots.replaceChildren();
    const rows = [];
    series.forEach((x, k) => {
      const p = byTime[k].get(t);
      if (!p || p[1] == null) return;
      dots.append(s("circle", { cx: X(t), cy: Y(p[1]), r: 4, style: `fill:${x.color};stroke:var(--surface);stroke-width:2` }));
      rows.push({ x, p });
    });
    rows.sort((a, b) => b.p[1] - a.p[1]);
    const el = tooltip();
    el.replaceChildren(
      h("div", { class: "tt-time" }, fmtClock(t, span > 86400)),
      ...rows.map(({ x, p }) => h("div", { class: "tt-row" },
        h("i", { class: "key-line", style: `background:${x.color}` }),
        h("span", { class: "tt-val" }, `${fmtRate(p[1])} ${opts.unit}`),
        h("span", { class: "tt-name" }, x.name + (p[2] ? ` · ${p[2]}` : "")))));
    el.hidden = false;
    if (cx == null) {
      const box = g.getBoundingClientRect();
      cx = box.left + (X(t) / W) * box.width;
      cy = box.top + box.height / 3;
    }
    placeTooltip(cx, cy);
  }
  function hide() { cross.setAttribute("visibility", "hidden"); dots.replaceChildren(); tooltip().hidden = true; }
  function nearest(clientX) {
    const box = g.getBoundingClientRect();
    const t = opts.start + (((clientX - box.left) * (W / box.width) - m.l) / iw) * span;
    let lo = 0, hi = times.length - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (times[mid] < t) lo = mid; else hi = mid; }
    return Math.abs(times[lo] - t) <= Math.abs(times[hi] - t) ? lo : hi;
  }
  hit.addEventListener("pointermove", (ev) => show(nearest(ev.clientX), ev.clientX, ev.clientY));
  hit.addEventListener("pointerleave", hide);
  g.addEventListener("blur", hide);
  g.addEventListener("focus", () => show(times.length - 1));
  g.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowLeft") { show(Math.max(0, idx - 1)); ev.preventDefault(); }
    if (ev.key === "ArrowRight") { show(Math.min(times.length - 1, idx + 1)); ev.preventDefault(); }
    if (ev.key === "Escape") hide();
  });
  root.append(g);

  // Table view: every plotted value, readable without hovering.
  const rowsByT = times.slice().reverse();
  root.append(h("details", {}, h("summary", {}, "Show as table"),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Time"), series.map((x) => h("th", { class: "num" }, `${x.name} (${opts.unit})`)))),
      h("tbody", {}, rowsByT.map((t) => h("tr", {}, h("td", {}, fmtClock(t, span > 86400)),
        series.map((x, k) => { const p = byTime[k].get(t); return h("td", { class: "num" }, p && p[1] != null ? fmtRate(p[1]) : "–"); }))))))));
}

/* ------------------------------------------------------------------ history */

function deviceColors(macs) {
  /* A device keeps its color across refreshes and ranges (color follows the device, not its rank). */
  let map = {};
  try { map = JSON.parse(localStorage.getItem("hw-colors") || "{}"); } catch (e) { map = {}; }
  const used = new Set();
  const out = {};
  for (const mac of macs) {
    let slot = map[mac];
    if (!slot || used.has(slot)) {
      slot = [1, 2, 3, 4, 5, 6, 7, 8].find((n) => !used.has(n) && !Object.values(map).includes(n)) ||
        [1, 2, 3, 4, 5, 6, 7, 8].find((n) => !used.has(n));
      map[mac] = slot;
    }
    used.add(slot);
    out[mac] = `var(--series-${slot})`;
  }
  try { localStorage.setItem("hw-colors", JSON.stringify(map)); } catch (e) { /* ignore */ }
  return out;
}

function renderCharts() {
  const tl = state.timeline;
  if (!tl) return;
  const bands = (tl.incidents || []).map((i) => [i.start_ts, i.end_ts || tl.end]);
  const lat = tl.latency || {};
  const path = (state.now && state.now.path) || {};
  const pts = (rows, extra) => (rows || []).map((r) => [r[0], r[1], extra ? extra(r) : null]);
  const lossNote = (r) => (r[3] ? `worst ${fmtMs(r[2])} ms, ${r[3]}% lost` : `worst ${fmtMs(r[2])} ms`);
  lineChart($("chart-ping"), {
    label: "Ping over time", unit: "ms", start: tl.start, end: tl.end, bucket: tl.bucket, bands, yFloor: 50,
    series: [
      { name: "Internet", color: "var(--series-1)", points: pts(lat.internet, lossNote) },
      { name: "AT&T gateway", color: "var(--series-2)", points: pts(lat.isp) },
      { name: `${path.gateway || "Main"} eero`, color: "var(--series-3)", points: pts(lat.lan, lossNote) },
      { name: `${path.node || "Your"} eero`, color: "var(--series-4)", points: pts(lat.node, lossNote) },
    ],
  });

  const e = tl.eero || { devices: [], series: {}, total: [] };
  const colors = deviceColors(e.devices.map((d) => d.mac));
  const times = (e.total || []).map((r) => r[0]);
  const devSeries = (col) => e.devices.map((d) => {
    const m = new Map((e.series[d.mac] || []).map((r) => [r[0], r[col]]));
    // A poll without this device means it was offline: plot 0, not a gap.
    return { name: d.name + (d.owner ? ` (${d.owner})` : ""), color: colors[d.mac], points: times.map((t) => [t, m.get(t) || 0]) };
  });
  const emptyEero = state.now && state.now.eero && state.now.eero.status === "ok"
    ? "No device data for this period yet." : "Connect the eero to see each device (see “Connect the eero” above).";
  lineChart($("chart-dev-down"), { label: "Download by device", unit: "Mbps", start: tl.start, end: tl.end,
    bucket: e.bucket, series: devSeries(1), bands, empty: emptyEero, height: 200 });
  lineChart($("chart-dev-up"), { label: "Upload by device", unit: "Mbps", start: tl.start, end: tl.end,
    bucket: e.bucket, series: devSeries(2), bands, empty: emptyEero, height: 200 });

  lineChart($("chart-pc"), {
    label: "This PC's traffic", unit: "Mbps", start: tl.start, end: tl.end, bucket: tl.bucket, bands, height: 200,
    series: [
      { name: "Download", color: "var(--series-1)", points: (tl.pc || []).map((r) => [r[0], r[1]]) },
      { name: "Upload", color: "var(--series-2)", points: (tl.pc || []).map((r) => [r[0], r[2]]) },
    ],
  });
}

function renderTables() {
  const u = state.usage;
  if (!u) return;
  const dev = $("table-devices");
  dev.replaceChildren();
  if (!u.devices.length) {
    dev.append(h("p", { class: "empty" }, "No eero data for this period."));
  } else {
    dev.append(h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Device"), h("th", { class: "num" }, "Downloaded"), h("th", { class: "num" }, "Uploaded"),
        h("th", { class: "num" }, "Busiest ↓"), h("th", { class: "num" }, "Busiest ↑"))),
      h("tbody", {}, u.devices.map((d) => h("tr", {},
        h("td", {}, d.name || d.mac, h("span", { class: "sub" },
          [d.owner, KIND_LABEL[d.kind], d.connection === "wired" ? "wired" : d.connection ? "Wi-Fi" : null, d.node].filter(Boolean).join(" · "))),
        h("td", { class: "num" }, fmtVolume(d.down_mb)), h("td", { class: "num" }, fmtVolume(d.up_mb)),
        h("td", { class: "num" }, fmtRate(d.peak_down) + " Mbps"), h("td", { class: "num" }, fmtRate(d.peak_up) + " Mbps")))))),
      h("p", { class: "card-sub" }, "Estimated from the eero's live readings every 30 seconds."));
  }
  const apps = $("table-apps");
  apps.replaceChildren();
  const pcTot = u.pc_total || {};
  if (!u.apps.length) {
    apps.append(h("p", { class: "empty" }, state.now && state.now.pc && !state.now.pc.per_app
      ? "Per-program detail needs HogWatch to run as administrator (start-hogwatch.cmd)."
      : "No program data for this period yet."));
  } else {
    apps.append(h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Program"), h("th", { class: "num" }, "Downloaded"), h("th", { class: "num" }, "Uploaded"),
        h("th", { class: "num" }, "Busiest"))),
      h("tbody", {}, u.apps.map((a) => h("tr", {},
        h("td", {}, a.app, a.top_hosts && a.top_hosts.length ? h("span", { class: "sub" },
          "mostly " + a.top_hosts.map((x) => x.name || x.ip).join(", ")) : null),
        h("td", { class: "num" }, fmtVolume(a.down_mb)), h("td", { class: "num" }, fmtVolume(a.up_mb)),
        h("td", { class: "num" }, fmtRate(Math.max(a.peak_down, a.peak_up)) + " Mbps")))))));
  }
  apps.append(h("p", { class: "card-sub" },
    `Whole PC in this period: ${fmtVolume(pcTot.down_mb)} down, ${fmtVolume(pcTot.up_mb)} up (includes home-network traffic).`));
}

function renderHistory() {
  renderHiccups();
  renderIncidents();
  renderCharts();
  renderTables();
}

/* ------------------------------------------------------------------ wiring */

function setRange(hours) {
  state.hours = hours;
  state.incidentLimit = 15;
  try { localStorage.setItem("hw-hours", String(hours)); } catch (e) { /* ignore */ }
  for (const b of document.querySelectorAll("#range button")) b.setAttribute("aria-checked", String(+b.dataset.hours === hours));
  refreshHistory();
}
for (const b of document.querySelectorAll("#range button")) b.addEventListener("click", () => setRange(+b.dataset.hours));
$("range").addEventListener("keydown", (ev) => {
  const btns = [...document.querySelectorAll("#range button")];
  const i = btns.findIndex((b) => b.getAttribute("aria-checked") === "true");
  const step = ev.key === "ArrowRight" ? 1 : ev.key === "ArrowLeft" ? -1 : 0;
  if (!step) return;
  const next = btns[(i + step + btns.length) % btns.length];
  next.focus();
  setRange(+next.dataset.hours);
  ev.preventDefault();
});

let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderCharts, 150); });

setRange(state.hours);
refreshNow();
setInterval(refreshNow, 3000);
setInterval(refreshHistory, 30000);
