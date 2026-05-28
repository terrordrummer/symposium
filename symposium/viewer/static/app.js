"use strict";

// ---------------------------------------------------------------------------
// Symposium live viewer — read-only consumer of the SSE stream.
// ---------------------------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";
const CENTER = 500, RADIUS = 360, NODE_R = 58, COORD_R = 70;

const els = {
  runSelect: document.getElementById("runSelect"),
  followNewest: document.getElementById("followNewest"),
  ttsToggle: document.getElementById("ttsToggle"),
  status: document.getElementById("status"),
  nodes: document.getElementById("nodes"),
  edges: document.getElementById("edges"),
  chatLog: document.getElementById("chatLog"),
  chatHead: document.getElementById("chatHead"),
  problem: document.getElementById("problem"),
};

const state = {
  source: null,
  runPath: null,
  pinned: null,          // ?run= forces a single run, disables follow
  positions: new Map(),  // id -> {x, y}
  panelOrder: [],        // persona ids in turn order (drives "who's next")
  coordinator: null,
  thinking: null,        // node predicted to be generating right now
  pendingThinking: null, // last prediction, applied once the run is confirmed live
  spoke: null,           // node that just finished (brief flash)
  active: false,         // run still live? (no thinking glow once done)
  lastPrimaryTurn: -1,   // highest primary turn_index seen this round
};

// ---- color helpers --------------------------------------------------------
function hashStr(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
function hueFor(s) { return hashStr(s) % 360; }
function edgeColor(type) { return `hsl(${hueFor(type)} 80% 62%)`; }
function classColor(pc) {
  if (pc === "domain") return getCss("--domain");
  if (pc === "horizontal") return getCss("--horizontal");
  return getCss("--accent");
}
function getCss(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

// ---- run discovery / selection -------------------------------------------
async function fetchRuns() {
  try {
    const r = await fetch("/api/runs", { cache: "no-store" });
    const j = await r.json();
    return j.runs || [];
  } catch { return []; }
}

function populateRunSelect(runs) {
  const cur = state.runPath;
  els.runSelect.innerHTML = "";
  for (const run of runs) {
    const opt = document.createElement("option");
    opt.value = run.path;
    opt.textContent = run.name + (run.active ? "  ●" : "");
    if (run.path === cur) opt.selected = true;
    els.runSelect.appendChild(opt);
  }
}

async function bootstrap() {
  const params = new URLSearchParams(location.search);
  state.pinned = params.get("run");
  if (state.pinned) els.followNewest.checked = false;

  await refreshAndMaybeSwitch(true);
  // Poll for new runs so "follow newest" latches onto a run created after
  // the viewer was opened (skill launches watch first, then deliberates).
  setInterval(() => refreshAndMaybeSwitch(false), 2500);
}

async function refreshAndMaybeSwitch(initial) {
  const runs = await fetchRuns();
  populateRunSelect(runs);
  if (runs.length === 0) { setStatus("waiting for a run…", "idle"); return; }

  let target = state.pinned;
  if (!target) {
    if (els.followNewest.checked || initial) target = runs[0].path;
    else target = state.runPath || runs[0].path;
  }
  if (target && target !== state.runPath) connect(target);
}

els.runSelect.addEventListener("change", () => {
  els.followNewest.checked = false;
  connect(els.runSelect.value);
});

// ---- SSE connection -------------------------------------------------------
function connect(runPath) {
  if (state.source) { state.source.close(); state.source = null; }
  resetStage();
  state.runPath = runPath;
  // reflect selection in the dropdown without rebuilding it
  for (const o of els.runSelect.options) o.selected = (o.value === runPath);

  const src = new EventSource(`/api/stream?run=${encodeURIComponent(runPath)}`);
  state.source = src;
  src.addEventListener("config", e => onConfig(JSON.parse(e.data)));
  src.addEventListener("message", e => onMessage(JSON.parse(e.data)));
  src.addEventListener("status", e => onStatus(JSON.parse(e.data)));
  src.addEventListener("end", e => onEnd(JSON.parse(e.data)));
  src.onerror = () => setStatus("reconnecting…", "idle");
}

function resetStage() {
  els.nodes.innerHTML = "";
  els.edges.innerHTML = "";
  els.chatLog.innerHTML = "";
  els.problem.textContent = "";
  state.positions.clear();
  state.panelOrder = [];
  state.coordinator = null;
  state.thinking = null;
  state.pendingThinking = null;
  state.spoke = null;
  state.active = false;
  state.lastPrimaryTurn = -1;
  cancelSpeech();
}

// ---- circle layout (config event) ----------------------------------------
function onConfig(cfg) {
  els.chatHead.textContent = cfg.session_id || "deliberation";
  els.problem.textContent = cfg.problem_statement
    ? "“" + truncate(cfg.problem_statement, 220) + "”" : "";
  state.coordinator = cfg.coordinator;

  buildVoiceMap(cfg);

  const panel = (cfg.personas || []);
  state.panelOrder = panel.map(p => p.id);
  const n = panel.length;
  panel.forEach((p, i) => {
    const angle = (i / Math.max(1, n)) * 2 * Math.PI - Math.PI / 2;
    const x = CENTER + RADIUS * Math.cos(angle);
    const y = CENTER + RADIUS * Math.sin(angle);
    addNode(p.id, p.label, p.reasoning_scope, classColor(p.persona_class), x, y, NODE_R);
  });

  if (cfg.coordinator) {
    addNode(cfg.coordinator, "coordinator", "synthesizes the round",
            getCss("--accent2"), CENTER, CENTER, COORD_R, true);
  }
}

function addNode(id, label, scope, color, x, y, r, isCoord) {
  state.positions.set(id, { x, y, r });
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", "node");
  g.dataset.id = id;

  const c = document.createElementNS(SVG_NS, "circle");
  c.setAttribute("cx", x); c.setAttribute("cy", y); c.setAttribute("r", r);
  c.setAttribute("fill", isCoord ? "#13213e" : "#101626");
  c.setAttribute("stroke", color); c.setAttribute("stroke-width", "3");
  g.appendChild(c);

  const t = document.createElementNS(SVG_NS, "text");
  t.setAttribute("x", x); t.setAttribute("y", y - 6);
  t.textContent = label;
  g.appendChild(t);

  if (scope) {
    const s = document.createElementNS(SVG_NS, "text");
    s.setAttribute("class", "scope");
    s.setAttribute("x", x); s.setAttribute("y", y + 18);
    s.textContent = truncate(scope, 22);
    g.appendChild(s);
  }
  els.nodes.appendChild(g);
}

// ---- message event --------------------------------------------------------
function onMessage(m) {
  addBubble(m);
  markSpoke(m.speaker);                          // who JUST finished (brief flash)
  // directed-question arrows, drawn when ASKED (requester → responder)
  for (const dr of (m.direct_requests || [])) drawArc(m.speaker, dr.target, dr.type);
  state.pendingThinking = predictNext(m);         // who is generating NOW
  setThinking(state.pendingThinking);
  if (els.ttsToggle.checked && m.text) enqueueSpeech(m.text, m.speaker);
}

// Predict the next agent to generate, from the deterministic turn rotation
// (panel order, then coordinator, then next round). The journal only records
// COMPLETED turns, so "who's thinking now" must be inferred from the last one.
function predictNext(m) {
  const order = state.panelOrder;
  const coord = state.coordinator;
  if (!order.length) return null;
  switch (m.type) {
    case "problem_statement":
      state.lastPrimaryTurn = -1;
      return order[0];
    case "primary_turn": {
      const i = (typeof m.turn_index === "number") ? m.turn_index : order.indexOf(m.speaker);
      state.lastPrimaryTurn = Math.max(state.lastPrimaryTurn, i);
      return (i >= 0 && i < order.length - 1) ? order[i + 1] : coord;
    }
    case "branch_turn":
      // a side-answer landed; resume the main rotation from the last primary
      return (state.lastPrimaryTurn < order.length - 1)
        ? order[state.lastPrimaryTurn + 1] : coord;
    case "coordination_turn":
      state.lastPrimaryTurn = -1;
      return order[0];                            // next round opens
    case "synthesis":
    default:
      return null;                                 // deliberation done
  }
}

function setThinking(id) {
  if (state.thinking) { const p = nodeEl(state.thinking); if (p) p.classList.remove("thinking"); }
  state.thinking = null;
  if (!state.active || !id) return;               // no guessing once the run ends
  const el = nodeEl(id);
  if (el) { el.classList.add("thinking"); state.thinking = id; }
}

function markSpoke(id) {
  if (state.spoke) { const p = nodeEl(state.spoke); if (p) p.classList.remove("spoke"); }
  const el = nodeEl(id);
  if (!el) { state.spoke = null; return; }        // "runtime" / unknown: no node
  // restart the fade animation
  el.classList.remove("spoke"); void el.offsetWidth; el.classList.add("spoke");
  state.spoke = id;
}

function nodeEl(id) { return els.nodes.querySelector(`.node[data-id="${cssEscape(id)}"]`); }

function addBubble(m) {
  const div = document.createElement("div");
  div.className = "bubble " + (m.type || "");
  const color = m.speaker === state.coordinator ? getCss("--accent2") : edgeColor(m.speaker || "?");

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.innerHTML =
    `<span class="dot" style="background:${color}"></span>` +
    `<span class="who">${escapeHtml(m.speaker || "?")}</span>` +
    `<span class="type">· ${escapeHtml(m.type || "")}</span>` +
    `<span class="rt">r${m.round ?? "-"}/t${m.turn_index ?? "-"}</span>`;
  div.appendChild(meta);

  const text = document.createElement("div");
  text.className = "text";
  text.textContent = m.text || "";
  div.appendChild(text);

  // outgoing asks: THIS turn poses a directed question to another agent
  for (const dr of (m.direct_requests || [])) {
    const b = document.createElement("span");
    b.className = "req-badge";
    b.style.color = edgeColor(dr.type || "ask");
    b.textContent = `→ chiede a ${dr.target} · ${dr.type}`;
    if (dr.content) b.title = (dr.content + "").slice(0, 400);
    div.appendChild(b);
  }
  // inbound answer: this branch turn replies to someone's earlier question
  if (m.edge) {
    const b = document.createElement("span");
    b.className = "req-badge";
    b.style.color = edgeColor(m.edge.type);
    b.textContent = `← risponde a ${m.edge.from || "?"} · ${m.edge.type}`;
    if (m.edge.content) b.title = (m.edge.content + "").slice(0, 400);
    div.appendChild(b);
  }

  const atBottom = els.chatLog.scrollHeight - els.chatLog.scrollTop - els.chatLog.clientHeight < 60;
  els.chatLog.appendChild(div);
  if (atBottom) els.chatLog.scrollTop = els.chatLog.scrollHeight;
}

// ---- directed-request arcs ------------------------------------------------
function drawArc(fromId, toId, type) {
  const from = state.positions.get(fromId);
  const to = state.positions.get(toId);
  if (!to) return;
  // If asker unknown, bow out from the center toward the answerer.
  const a = from || { x: CENTER, y: CENTER, r: 0 };

  const color = edgeColor(type);
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", "edge");
  g.style.color = color;

  // trim endpoints to the node rims
  const dx = to.x - a.x, dy = to.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len, uy = dy / len;
  const sx = a.x + ux * (a.r + 4), sy = a.y + uy * (a.r + 4);
  const ex = to.x - ux * (to.r + 10), ey = to.y - uy * (to.r + 10);
  // control point: bow perpendicular for a readable arc
  const mx = (sx + ex) / 2, my = (sy + ey) / 2;
  const bow = Math.min(140, len * 0.28);
  const cx = mx - uy * bow, cy = my + ux * bow;

  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("class", "wire");
  path.setAttribute("d", `M${sx},${sy} Q${cx},${cy} ${ex},${ey}`);
  path.setAttribute("stroke", color);
  path.setAttribute("marker-end", "url(#arrowhead)");
  g.appendChild(path);

  const label = document.createElementNS(SVG_NS, "text");
  label.setAttribute("x", cx); label.setAttribute("y", cy);
  label.setAttribute("text-anchor", "middle");
  label.setAttribute("fill", color);
  label.textContent = type;
  g.appendChild(label);

  els.edges.appendChild(g);
  // fade and remove after a few seconds so the canvas stays readable
  setTimeout(() => { g.classList.add("fade"); setTimeout(() => g.remove(), 1300); }, 6000);
}

// ---- status / end ---------------------------------------------------------
function onStatus(s) {
  state.active = !!s.run_active;
  if (s.run_active) {
    setStatus(`live · ${s.total} turns`, "live");
    if (!state.thinking) setThinking(state.pendingThinking);  // resume after history replay
  } else if (s.lock_stale) {
    setStatus(`stale · ${s.total} turns`, "done");
  }
  if (!state.active) setThinking(null);   // stop guessing once it's not live
}
function onEnd(e) {
  state.active = false;
  setStatus(`done · ${e.outcome || "ended"}`, "done");
  setThinking(null);
}
function setStatus(txt, kind) {
  els.status.textContent = txt;
  els.status.className = "status status-" + kind;
}

// ---- text-to-speech (one voice per persona) -------------------------------
const tts = { voices: [], map: new Map(), queue: [], busy: false };

function loadVoices() {
  if (!("speechSynthesis" in window)) return;
  tts.voices = speechSynthesis.getVoices().filter(v => v.lang && v.lang.startsWith("en"));
  if (tts.voices.length === 0) tts.voices = speechSynthesis.getVoices();
}
if ("speechSynthesis" in window) {
  loadVoices();
  speechSynthesis.onvoiceschanged = loadVoices;
} else {
  els.ttsToggle.disabled = true;
  els.ttsToggle.parentElement.title = "speech synthesis not available in this browser";
}

function buildVoiceMap(cfg) {
  tts.map.clear();
  loadVoices();
  const ids = (cfg.personas || []).map(p => p.id);
  if (cfg.coordinator) ids.push(cfg.coordinator);
  ids.forEach(id => {
    const n = Math.max(1, tts.voices.length);
    const h = hashStr(id);
    const voice = tts.voices[h % n] || null;
    // jitter to de-collide when few system voices exist
    const pitch = id === cfg.coordinator ? 0.8 : 0.9 + ((h >> 3) % 5) * 0.08;
    const rate = 0.95 + ((h >> 7) % 4) * 0.05;
    tts.map.set(id, { voice, pitch, rate });
  });
}

function enqueueSpeech(text, id) {
  if (!("speechSynthesis" in window)) return;
  tts.queue.push({ text: truncate(text, 600), id });
  if (!tts.busy) speakNext();
}
function speakNext() {
  if (tts.queue.length === 0) { tts.busy = false; return; }
  if (!els.ttsToggle.checked) { tts.queue = []; tts.busy = false; return; }
  tts.busy = true;
  const { text, id } = tts.queue.shift();
  const u = new SpeechSynthesisUtterance(text);
  const cfg = tts.map.get(id);
  if (cfg) { if (cfg.voice) u.voice = cfg.voice; u.pitch = cfg.pitch; u.rate = cfg.rate; }
  u.onend = speakNext;
  u.onerror = speakNext;
  speechSynthesis.speak(u);
}
function cancelSpeech() {
  tts.queue = []; tts.busy = false;
  if ("speechSynthesis" in window) speechSynthesis.cancel();
}
els.ttsToggle.addEventListener("change", () => { if (!els.ttsToggle.checked) cancelSpeech(); });

// ---- utils ----------------------------------------------------------------
function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }
function escapeHtml(s) { return (s + "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function cssEscape(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : (s + "").replace(/"/g, '\\"'); }

bootstrap();
