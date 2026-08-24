"use strict";

// ---------------------------------------------------------------------------
// Symposium live viewer — immutable run playback plus local room controls.
// ---------------------------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";

// ---- auth relay -------------------------------------------------------------
// When the server was started with a bearer token, it is embedded in this
// page's own URL (?token=...). Relay it to every API call (EventSource cannot
// send headers, and relaying via the query keeps one code path for GET/POST).
const AUTH_TOKEN = new URLSearchParams(window.location.search).get("token") || "";
function withAuth(url) {
  if (!AUTH_TOKEN) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(AUTH_TOKEN)}`;
}

const els = {
  runSelect: document.getElementById("runSelect"),
  followNewest: document.getElementById("followNewest"),
  ttsToggle: document.getElementById("ttsToggle"),
  ttsStatus: document.getElementById("ttsStatus"),
  replaySpeed: document.getElementById("replaySpeed"),
  replayButton: document.getElementById("replayButton"),
  replayNextButton: document.getElementById("replayNextButton"),
  sartoriOpenButton: document.getElementById("sartoriOpenButton"),
  sartoriCloseButton: document.getElementById("sartoriCloseButton"),
  sartoriPanel: document.getElementById("sartoriPanel"),
  sartoriCommandForm: document.getElementById("sartoriCommandForm"),
  sartoriCommand: document.getElementById("sartoriCommand"),
  sartoriFeedback: document.getElementById("sartoriFeedback"),
  roomControlSelect: document.getElementById("roomControlSelect"),
  roomSwitchButton: document.getElementById("roomSwitchButton"),
  roomArchiveButton: document.getElementById("roomArchiveButton"),
  roomCreateForm: document.getElementById("roomCreateForm"),
  agentRegistry: document.getElementById("agentRegistry"),
  agentCreateForm: document.getElementById("agentCreateForm"),
  avatarPreview: document.getElementById("avatarPreview"),
  avatarPreviewMeta: document.getElementById("avatarPreviewMeta"),
  avatarChoice: document.getElementById("avatarChoice"),
  avatarRerollButton: document.getElementById("avatarRerollButton"),
  launcherControls: document.getElementById("launcherControls"),
  shutdownButton: document.getElementById("shutdownButton"),
  shutdownScreen: document.getElementById("shutdownScreen"),
  status: document.getElementById("status"),
  nodes: document.getElementById("nodes"),
  edges: document.getElementById("edges"),
  chatLog: document.getElementById("chatLog"),
  chatHead: document.getElementById("chatHead"),
  executionActivity: document.getElementById("executionActivity"),
  executionPhase: document.getElementById("executionPhase"),
  executionElapsed: document.getElementById("executionElapsed"),
  executionTitle: document.getElementById("executionTitle"),
  executionDetail: document.getElementById("executionDetail"),
  roomPromptForm: document.getElementById("roomPromptForm"),
  roomPrompt: document.getElementById("roomPrompt"),
  roomPromptButton: document.getElementById("roomPromptButton"),
  roomPromptStatus: document.getElementById("roomPromptStatus"),
  problem: document.getElementById("problem"),
  roomName: document.getElementById("roomName"),
  stage: document.getElementById("stage"),
};

const state = {
  source: null,
  runName: null,         // run-dir name (opaque id; resolved server-side)
  pinned: null,          // ?run= forces a single run, disables follow
  panelOrder: [],        // persona ids in turn order (drives "who's next")
  coordinator: null,
  displayNames: new Map(),
  workspaceManaged: false,
  activeRoomId: null,
  workspaceSnapshot: null,
  controlBusy: false,
  lastControlError: null,
  activeJobId: null,
  currentJob: null,
  jobTerminal: false,
  thinking: null,        // node predicted to be generating right now
  pendingThinking: null, // last prediction, applied once the run is confirmed live
  spoke: null,           // node that just finished (brief static emphasis)
  speaking: null,        // node currently holding the visual floor
  active: false,         // run still live? (no thinking glow once done)
  playback: false,       // client-controlled replay presents history one turn at a time
  playbackMode: "manual",
  playbackSpeed: 1,
  replayQueue: [],
  replayCurrentMessage: null,
  replayBusy: false,
  replaySourceDone: false,
  replayWaitingForAudio: false,
  replayTimer: null,
  deferredStatus: null,
  deferredEnd: null,
  lastPrimaryTurn: -1,   // highest primary turn_index seen this round
  availableAvatars: [],
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
    const r = await fetch(withAuth("/api/runs"), { cache: "no-store" });
    const j = await r.json();
    return j.runs || [];
  } catch { return []; }
}

async function fetchWorkspace() {
  try {
    const response = await fetch(withAuth("/api/workspace"), { cache: "no-store" });
    if (!response.ok) return null;
    return await response.json();
  } catch { return null; }
}

async function refreshSystem() {
  try {
    const response = await fetch(withAuth("/api/system"), { cache: "no-store" });
    if (!response.ok) return;
    const system = await response.json();
    els.launcherControls.hidden = !system.can_shutdown;
  } catch {
    els.launcherControls.hidden = true;
  }
}

function populateRunSelect(runs) {
  const cur = state.runName;
  els.runSelect.innerHTML = "";
  for (const run of runs) {
    const opt = document.createElement("option");
    opt.value = run.name;
    opt.textContent = run.name + (run.active ? "  ●" : "");
    if (run.name === cur) opt.selected = true;
    els.runSelect.appendChild(opt);
  }
}

async function bootstrap() {
  const params = new URLSearchParams(location.search);
  state.pinned = params.get("run");
  if (state.pinned) els.followNewest.checked = false;

  await Promise.all([
    refreshAndMaybeSwitch(true),
    refreshWorkspace(),
    refreshExecutions(),
    refreshSystem(),
    refreshTTSStatus(),
  ]);
  // Poll for new runs so "follow newest" latches onto a run created after
  // the viewer was opened (skill launches watch first, then deliberates).
  setInterval(() => refreshAndMaybeSwitch(false), 2500);
  setInterval(refreshWorkspace, 1200);
  setInterval(refreshExecutions, 1000);
  setInterval(renderExecutionActivity, 1000);
  setInterval(refreshTTSStatus, 2500);
}

async function refreshWorkspace() {
  const snapshot = await fetchWorkspace();
  if (snapshot && snapshot.initialized) syncWorkspace(snapshot);
}

async function refreshAndMaybeSwitch(initial) {
  const runs = await fetchRuns();
  populateRunSelect(runs);
  if (runs.length === 0) { setStatus("waiting for a run…", "idle"); return; }

  let target = state.pinned;
  if (!target) {
    if (els.followNewest.checked || initial) target = runs[0].name;
    else target = state.runName || runs[0].name;
  }
  if (target && target !== state.runName) connect(target);
}

els.runSelect.addEventListener("change", () => {
  els.followNewest.checked = false;
  connect(els.runSelect.value);
});
els.replayButton.addEventListener("click", () => {
  if (!state.runName) return;
  connect(state.runName, els.replaySpeed.value || "manual");
});
els.replayNextButton.addEventListener("click", () => {
  if (state.playback && state.playbackMode === "manual" &&
      state.replayBusy && !state.replayWaitingForAudio) {
    finishReplayTurn();
  }
});

// ---- SSE connection -------------------------------------------------------
function connect(runName, replayMode = null) {
  if (state.source) { state.source.close(); state.source = null; }
  resetStage();
  state.runName = runName;
  // reflect selection in the dropdown without rebuilding it
  for (const o of els.runSelect.options) o.selected = (o.value === runName);

  const params = new URLSearchParams({ run: runName });
  if (replayMode !== null) params.set("replay", String(replayMode));
  const src = new EventSource(withAuth(`/api/stream?${params.toString()}`));
  state.source = src;
  src.addEventListener("config", e => {
    onConfig(JSON.parse(e.data));
    refreshWorkspace();
  });
  src.addEventListener("message", e => onMessage(JSON.parse(e.data)));
  src.addEventListener("playback", e => onPlayback(JSON.parse(e.data)));
  src.addEventListener("status", e => onStatus(JSON.parse(e.data)));
  src.addEventListener("end", e => onEnd(JSON.parse(e.data)));
  src.onerror = () => setStatus("reconnecting…", "idle");
}

function resetStage() {
  if (state.replayTimer !== null) clearTimeout(state.replayTimer);
  els.nodes.innerHTML = "";
  els.edges.innerHTML = "";
  els.chatLog.innerHTML = "";
  els.problem.textContent = "";
  state.panelOrder = [];
  state.coordinator = null;
  state.displayNames.clear();
  state.activeRoomId = null;
  state.thinking = null;
  state.pendingThinking = null;
  state.spoke = null;
  state.speaking = null;
  state.active = false;
  state.playback = false;
  state.playbackMode = "manual";
  state.playbackSpeed = 1;
  state.replayQueue = [];
  state.replayCurrentMessage = null;
  state.replayBusy = false;
  state.replaySourceDone = false;
  state.replayWaitingForAudio = false;
  state.replayTimer = null;
  state.deferredStatus = null;
  state.deferredEnd = null;
  state.lastPrimaryTurn = -1;
  els.replayNextButton.disabled = true;
  cancelSpeech();
}

// ---- video-call layout (config event) ------------------------------------
function onConfig(cfg) {
  els.chatHead.textContent = cfg.session_id || "deliberation";
  if (!state.workspaceManaged) els.roomName.textContent = "Symposium";
  els.problem.textContent = cfg.problem_statement
    ? "“" + truncate(cfg.problem_statement, 220) + "”" : "";
  state.coordinator = cfg.coordinator;

  const panel = (cfg.personas || []);
  state.panelOrder = panel.map(p => p.id);
  panel.forEach(p => addParticipant(p, false));

  if (cfg.coordinator) {
    const coordinator = cfg.coordinator_profile || {
      id: cfg.coordinator,
      label: "Sartori",
      reasoning_scope: "coordinamento silenzioso",
      avatar: null,
    };
    addParticipant(coordinator, true);
  }
}

function syncWorkspace(snapshot) {
  const room = snapshot.active_room;
  const participants = snapshot.participants || [];
  const allowed = new Set(participants.map(participant => participant.id));
  const roomChanged = state.activeRoomId !== null && state.activeRoomId !== room.id;
  const controlsChanged = !state.workspaceSnapshot ||
    state.workspaceSnapshot.revision !== snapshot.revision;
  state.workspaceManaged = true;
  state.workspaceSnapshot = snapshot;
  state.availableAvatars = snapshot.available_avatars || [];
  state.activeRoomId = room.id;
  els.roomName.textContent = room.name;

  if (roomChanged) {
    state.jobTerminal = false;
    state.currentJob = null;
    renderExecutionActivity();
    els.roomPrompt.disabled = false;
    els.roomPromptButton.textContent = "Avvia discussione";
    setThinking(null);
    setSpeaking(null);
  }
  for (const tile of Array.from(els.nodes.querySelectorAll(".participant"))) {
    if (!allowed.has(tile.dataset.id)) {
      if (state.speaking === tile.dataset.id) setSpeaking(null);
      if (state.thinking === tile.dataset.id) setThinking(null);
      state.displayNames.delete(tile.dataset.id);
      tile.remove();
    }
  }
  for (const participant of participants) {
    addParticipant(participant, !!participant.is_coordinator);
    const tile = nodeEl(participant.id);
    if (tile && !tile.classList.contains("speaking") && !tile.classList.contains("thinking")) {
      setPresence(tile, presenceLabel(participant.presence));
    }
  }
  if (controlsChanged) {
    renderSartoriPanel(snapshot, roomChanged);
    updateRoomComposer(snapshot);
  }
}

function presenceLabel(presence) {
  return ({
    joining: "sta entrando",
    listening: "in ascolto",
    thinking: "sta pensando",
    speaking: "sta parlando",
    leaving: "sta uscendo",
    offline: "offline",
  })[presence] || "in ascolto";
}

// ---- Sartori browser controls ---------------------------------------------
function setSartoriPanel(open) {
  els.sartoriPanel.hidden = !open;
  els.sartoriOpenButton.setAttribute("aria-expanded", String(open));
  if (open) {
    refreshWorkspace();
    els.sartoriCommand.focus();
  }
}

els.sartoriOpenButton.addEventListener("click", () => setSartoriPanel(true));
els.sartoriCloseButton.addEventListener("click", () => setSartoriPanel(false));
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !els.sartoriPanel.hidden) setSartoriPanel(false);
});

function renderSartoriPanel(snapshot, roomChanged = false) {
  const previousRoom = roomChanged ? "" : els.roomControlSelect.value;
  els.roomControlSelect.innerHTML = "";
  for (const room of snapshot.rooms || []) {
    const option = document.createElement("option");
    option.value = room.id;
    option.textContent = `${room.name}${room.status === "archived" ? " · archiviata" : ""}`;
    option.disabled = room.status === "archived";
    option.selected = room.id === (previousRoom || snapshot.active_room.id);
    els.roomControlSelect.appendChild(option);
  }
  updateRoomButtons();

  const present = new Set((snapshot.participants || []).map(participant => participant.id));
  els.agentRegistry.innerHTML = "";
  for (const agent of snapshot.agents || []) {
    if (agent.status !== "active") continue;
    const row = document.createElement("div");
    row.className = "agent-row";
    const identity = document.createElement("div");
    identity.className = "agent-identity";
    if (agent.avatar && agent.avatar.portrait_url) {
      const portrait = document.createElement("img");
      portrait.className = "agent-avatar";
      portrait.src = agent.avatar.portrait_url;
      portrait.alt = "";
      identity.appendChild(portrait);
    }
    const identityText = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = agent.display_name;
    const detail = document.createElement("small");
    detail.textContent = (agent.capabilities || []).join(" · ") || agent.id;
    identityText.append(name, detail);
    identity.appendChild(identityText);
    row.appendChild(identity);

    if (agent.id === "coordinator") {
      const fixed = document.createElement("small");
      fixed.textContent = "sempre presente";
      row.appendChild(fixed);
    } else {
      const action = document.createElement("button");
      action.type = "button";
      action.className = "agent-action";
      action.textContent = present.has(agent.id) ? "Congeda" : "Invita";
      action.addEventListener("click", () => runControlAction({
        action: present.has(agent.id) ? "dismiss_agent" : "invite_agent",
        agent: agent.id,
      }));
      row.appendChild(action);
    }
    els.agentRegistry.appendChild(row);
  }
  if (!els.avatarChoice.value || !state.availableAvatars.some(
    avatar => avatar.asset_id === els.avatarChoice.value
  )) chooseRandomAvatar();
}

function chooseRandomAvatar() {
  const pool = state.availableAvatars || [];
  if (pool.length === 0) {
    els.avatarChoice.value = "";
    els.avatarPreview.removeAttribute("src");
    els.avatarPreviewMeta.textContent = "Nessun volto disponibile nel pool.";
    els.avatarRerollButton.disabled = true;
    return;
  }
  const previous = els.avatarChoice.value;
  const alternatives = pool.filter(avatar => avatar.asset_id !== previous);
  const candidates = alternatives.length ? alternatives : pool;
  const selected = candidates[Math.floor(Math.random() * candidates.length)];
  els.avatarChoice.value = selected.asset_id;
  els.avatarPreview.src = selected.portrait_url;
  const presentation = selected.voice?.presentation === "feminine" ? "femminile" : "maschile";
  els.avatarPreviewMeta.textContent = `Voce italiana ${presentation} coerente e permanente.`;
  els.avatarRerollButton.disabled = pool.length < 2;
}
els.avatarRerollButton.addEventListener("click", chooseRandomAvatar);

function updateRoomButtons() {
  const snapshot = state.workspaceSnapshot;
  if (!snapshot) return;
  const selected = (snapshot.rooms || []).find(room => room.id === els.roomControlSelect.value);
  els.roomSwitchButton.disabled = !selected || selected.status === "archived" || selected.active;
  els.roomArchiveButton.disabled = !selected || selected.active ||
    selected.id === snapshot.workspace.default_room_id || selected.status === "archived";
}
els.roomControlSelect.addEventListener("change", updateRoomButtons);

async function runControlAction(payload) {
  if (state.controlBusy) return null;
  state.controlBusy = true;
  state.lastControlError = null;
  setSartoriFeedback("Sartori sta eseguendo…", false);
  try {
    const response = await fetch(withAuth("/api/control"), {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Symposium-Request": "1",
      },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "operazione non riuscita");
    if (result.snapshot) syncWorkspace(result.snapshot);
    setSartoriFeedback(result.message || "Fatto.", false);
    return result;
  } catch (error) {
    state.lastControlError = error.message || String(error);
    setSartoriFeedback(state.lastControlError, true);
    return null;
  } finally {
    state.controlBusy = false;
  }
}

function setRoomPromptStatus(message, error = false) {
  els.roomPromptStatus.textContent = message;
  els.roomPromptStatus.classList.toggle("error", !!error);
}

function elapsedLabel(createdAt) {
  const started = Date.parse(createdAt || "");
  if (!Number.isFinite(started)) return "00:00";
  const total = Math.max(0, Math.floor((Date.now() - started) / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function jobSpeaker(job) {
  return state.thinking || state.pendingThinking || (job.participant_ids || [])[0] || null;
}

function setExecutionStep(activeStep) {
  for (const step of els.executionActivity.querySelectorAll("[data-step]")) {
    step.classList.toggle("active", step.dataset.step === activeStep);
    step.classList.toggle("complete",
      activeStep === "thinking" && step.dataset.step === "received" ||
      activeStep === "answer" && step.dataset.step !== "answer");
  }
}

function renderExecutionActivity(job = state.currentJob) {
  if (!job) {
    els.executionActivity.hidden = true;
    return;
  }
  state.currentJob = job;
  els.executionActivity.hidden = false;
  els.executionActivity.classList.remove("is-running", "is-failed", "is-complete");
  const active = job.status === "preparing" || job.status === "running";
  els.roomPrompt.disabled = active;
  els.roomPromptButton.disabled = active;
  els.roomPromptButton.textContent = active ? "Discussione in corso" : "Avvia discussione";
  els.executionElapsed.textContent = elapsedLabel(job.created_at);

  if (active) {
    const speakerId = jobSpeaker(job);
    const speaker = speakerId ? (state.displayNames.get(speakerId) || speakerId) : null;
    els.executionActivity.classList.add("is-running");
    els.executionPhase.textContent = job.status === "preparing"
      ? "preparazione automatica" : "elaborazione in corso";
    els.executionTitle.textContent = job.status === "preparing"
      ? "Sartori sta preparando la discussione"
      : `${speaker || "Il primo agente"} sta elaborando il suo intervento`;
    els.executionDetail.textContent =
      "Il loop è partito automaticamente: non devi fare nulla. La risposta comparirà qui appena pronta; il modello locale può richiedere alcuni minuti.";
    setExecutionStep(job.status === "preparing" ? "received" : "thinking");
    setStatus(`${speaker || "team"} elabora · ${els.executionElapsed.textContent}`, "live");
    return;
  }

  if (job.status === "failed" || job.outcome === "termination") {
    els.executionActivity.classList.add("is-failed");
    els.executionPhase.textContent = "discussione interrotta";
    els.executionTitle.textContent = "Il team non ha potuto completare la risposta";
    els.executionDetail.textContent = job.error ||
      `Motivo tecnico: ${job.termination_reason || "errore sconosciuto"}. Puoi riprovare.`;
    els.executionElapsed.textContent = "errore";
    setExecutionStep("thinking");
    setStatus("interrotta · controlla il messaggio", "error");
    return;
  }

  els.executionActivity.classList.add("is-complete");
  els.executionPhase.textContent = "discussione conclusa";
  els.executionTitle.textContent = "La risposta del team è pronta";
  els.executionDetail.textContent = "Puoi leggerla nella conversazione o avviare una nuova domanda.";
  els.executionElapsed.textContent = "completata";
  setExecutionStep("answer");
}

function updateRoomComposer(snapshot) {
  const speakers = (snapshot.participants || []).filter(participant => !participant.is_coordinator);
  const unavailable = speakers.length === 0;
  els.roomPromptButton.disabled = unavailable || !!state.activeJobId;
  if (state.jobTerminal) return;
  if (unavailable && !state.activeJobId) {
    setRoomPromptStatus("Invita almeno un agente che possa parlare.");
  } else if (!state.activeJobId) {
    setRoomPromptStatus(`${speakers.length} agent${speakers.length === 1 ? "e" : "i"} nella discussione.`);
  }
}

els.roomPromptForm.addEventListener("submit", async event => {
  event.preventDefault();
  const problem = els.roomPrompt.value.trim();
  if (!problem) return;
  state.jobTerminal = false;
  state.currentJob = {
    id: "pending",
    status: "preparing",
    created_at: new Date().toISOString(),
    participant_ids: (state.workspaceSnapshot?.participants || [])
      .filter(participant => !participant.is_coordinator)
      .map(participant => participant.id),
  };
  renderExecutionActivity();
  els.roomPromptButton.disabled = true;
  setRoomPromptStatus("Sartori sta preparando la discussione…");
  const result = await runControlAction({
    action: "start_session",
    problem,
    room: state.activeRoomId,
  });
  if (!result || !result.job) {
    state.currentJob = null;
    renderExecutionActivity();
    els.roomPrompt.disabled = false;
    els.roomPromptButton.disabled = false;
    setRoomPromptStatus(
      state.lastControlError || "Non è stato possibile avviare la discussione.",
      true,
    );
    return;
  }
  state.activeJobId = result.job.id;
  state.currentJob = result.job;
  els.roomPromptButton.disabled = true;
  state.pinned = null;
  els.followNewest.checked = true;
  if (location.search) history.replaceState({}, "", location.pathname);
  els.roomPromptForm.reset();
  setRoomPromptStatus("Discussione avviata · attendo il primo intervento…");
  refreshExecutions();
});

async function refreshExecutions() {
  try {
    const response = await fetch(withAuth("/api/executions"), { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json();
    if (!state.activeJobId) {
      const jobs = payload.jobs || [];
      const active = jobs.find(
        candidate => candidate.status === "preparing" || candidate.status === "running"
      );
      if (!active) {
        if (!state.currentJob) {
          const latest = jobs.find(candidate =>
            !state.activeRoomId || candidate.room_id === state.activeRoomId
          );
          if (latest) {
            state.currentJob = latest;
            state.jobTerminal = true;
            renderExecutionActivity(latest);
          }
        }
        return;
      }
      state.activeJobId = active.id;
      state.jobTerminal = false;
      els.roomPromptButton.disabled = true;
    }
    const job = (payload.jobs || []).find(candidate => candidate.id === state.activeJobId);
    if (!job) return;
    state.currentJob = job;
    renderExecutionActivity(job);

    if (job.status === "preparing" || job.status === "running") {
      setRoomPromptStatus(job.status === "preparing"
        ? "Sartori sta preparando la discussione…"
        : "Discussione in corso…");
      const runs = await fetchRuns();
      if (runs.some(run => run.name === job.run_name) && state.runName !== job.run_name) {
        connect(job.run_name);
      }
      return;
    }

    if (job.status === "failed") {
      setRoomPromptStatus(job.error || "La discussione non è riuscita.", true);
    } else if (job.outcome === "termination") {
      setRoomPromptStatus(`Discussione terminata · ${job.termination_reason || "senza sintesi"}`, true);
    } else {
      setRoomPromptStatus("Discussione conclusa.");
    }
    state.jobTerminal = true;
    state.activeJobId = null;
    const speakers = (state.workspaceSnapshot?.participants || [])
      .filter(participant => !participant.is_coordinator);
    els.roomPromptButton.disabled = speakers.length === 0;
    els.roomPrompt.disabled = false;
  } catch {
    // The SSE viewer remains usable if the ephemeral job-status poll fails.
  }
}

function setSartoriFeedback(message, error) {
  els.sartoriFeedback.textContent = message;
  els.sartoriFeedback.classList.toggle("error", !!error);
}

els.sartoriCommandForm.addEventListener("submit", async event => {
  event.preventDefault();
  const result = await runControlAction({ action: "command", command: els.sartoriCommand.value });
  if (result) els.sartoriCommandForm.reset();
});

els.roomSwitchButton.addEventListener("click", () => runControlAction({
  action: "switch_room",
  room: els.roomControlSelect.value,
}));
els.roomArchiveButton.addEventListener("click", () => runControlAction({
  action: "archive_room",
  room: els.roomControlSelect.value,
}));

els.roomCreateForm.addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(els.roomCreateForm);
  const result = await runControlAction({
    action: "create_room",
    name: String(form.get("name") || ""),
    purpose: String(form.get("purpose") || ""),
    activate: true,
  });
  if (result) els.roomCreateForm.reset();
});

els.agentCreateForm.addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(els.agentCreateForm);
  const displayName = String(form.get("display_name") || "");
  const requestedId = String(form.get("agent_id") || "").trim();
  const agentId = requestedId || localSlug(displayName);
  const capabilities = String(form.get("capabilities") || "")
    .split(",").map(value => value.trim()).filter(Boolean);
  const created = await runControlAction({
    action: "create_agent",
    agent_id: agentId,
    display_name: displayName,
    instructions: String(form.get("instructions") || ""),
    capabilities,
    avatar_id: String(form.get("avatar_id") || "") || null,
  });
  if (!created) return;
  if (form.get("invite")) {
    await runControlAction({ action: "invite_agent", agent: agentId });
  }
  els.agentCreateForm.reset();
  els.avatarChoice.value = "";
  chooseRandomAvatar();
});

els.shutdownButton.addEventListener("click", async () => {
  if (!window.confirm("Chiudere Symposium e interrompere il backend locale?")) return;
  els.shutdownButton.disabled = true;
  try {
    const response = await fetch(withAuth("/api/system/shutdown"), {
      method: "POST",
      cache: "no-store",
      headers: { "X-Symposium-Request": "1" },
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "arresto non riuscito");
    if (state.source) state.source.close();
    cancelSpeech();
    els.shutdownScreen.hidden = false;
  } catch (error) {
    els.shutdownButton.disabled = false;
    setSartoriFeedback(error.message || String(error), true);
  }
});

function addParticipant(persona, isCoordinator) {
  const id = persona.id;
  const avatar = persona.avatar || {};
  const label = avatar.display_name || persona.label || id;
  const role = isCoordinator
    ? "coordinatore · ascolto silenzioso"
    : (persona.reasoning_scope || "membro del panel");
  state.displayNames.set(id, label);
  const existing = nodeEl(id);
  if (existing) {
    existing.dataset.initials = initials(label);
    const currentName = existing.querySelector(".participant-name");
    const currentRole = existing.querySelector(".participant-role");
    if (currentName) currentName.textContent = label;
    if (currentRole) currentRole.textContent = role;
    const currentPortrait = existing.querySelector(".portrait");
    if (avatar.portrait_url && !currentPortrait) {
      const img = document.createElement("img");
      img.className = "portrait";
      img.src = avatar.portrait_url;
      img.alt = avatar.alt_text || `Ritratto sintetico di ${label}`;
      existing.prepend(img);
      existing.classList.remove("no-portrait");
    } else if (avatar.portrait_url && currentPortrait &&
               currentPortrait.getAttribute("src") !== avatar.portrait_url) {
      currentPortrait.src = avatar.portrait_url;
      currentPortrait.alt = avatar.alt_text || `Ritratto sintetico di ${label}`;
    }
    if (!existing.classList.contains("speaking") &&
        !existing.classList.contains("thinking")) {
      setPresence(existing, presenceLabel(persona.presence));
    }
    return;
  }

  const tile = document.createElement("article");
  tile.className = "participant node" + (isCoordinator ? " coordinator" : "");
  tile.dataset.id = id;
  tile.dataset.initials = initials(label);
  tile.style.setProperty("--tile-color", isCoordinator
    ? getCss("--accent2") : classColor(persona.persona_class));
  tile.style.setProperty("--identity-hue", String(hueFor(id)));
  tile.setAttribute("aria-label", `${label}, in ascolto`);

  if (avatar.portrait_url) {
    const img = document.createElement("img");
    img.className = "portrait";
    img.src = avatar.portrait_url;
    img.alt = avatar.alt_text || `Ritratto sintetico di ${label}`;
    img.loading = "eager";
    img.decoding = "async";
    tile.appendChild(img);
  } else {
    tile.classList.add("no-portrait");
    tile.style.setProperty("--tile-color", `hsl(${hueFor(id)} 58% 62%)`);
  }

  const meta = document.createElement("div");
  meta.className = "participant-meta";
  const identity = document.createElement("div");
  const name = document.createElement("strong");
  name.className = "participant-name";
  name.textContent = label;
  const scope = document.createElement("span");
  scope.className = "participant-role";
  scope.textContent = role;
  identity.append(name, scope);

  const presence = document.createElement("span");
  presence.className = "presence";
  presence.textContent = "in ascolto";
  meta.append(identity, presence);
  tile.appendChild(meta);
  els.nodes.appendChild(tile);
}

// ---- message event --------------------------------------------------------
function onMessage(m) {
  if (state.playback) {
    state.replayQueue.push(m);
    playNextReplayMessage();
    return;
  }
  presentMessage(m, true);
}

function presentMessage(m, narrate) {
  addBubble(m);
  markSpoke(m.speaker);                          // who JUST finished (brief emphasis)
  if (!els.ttsToggle.checked) setSpeaking(m.speaker); // visual floor while reading
  // directed-question arrows, drawn when ASKED (requester → responder)
  for (const dr of (m.direct_requests || [])) drawArc(m.speaker, dr.target, dr.type);
  state.pendingThinking = predictNext(m);         // who is generating NOW
  setThinking(state.pendingThinking);
  if (narrate && els.ttsToggle.checked && m.text && nodeEl(m.speaker)) {
    enqueueSpeech(m.text, m.speaker);
  }
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
  if (state.thinking) {
    const previous = nodeEl(state.thinking);
    if (previous) {
      previous.classList.remove("thinking");
      setPresence(previous, "in ascolto");
    }
  }
  state.thinking = null;
  els.nodes.classList.remove("thinker-focus");
  if (!state.active || !id) return;               // no guessing once the run ends
  const el = nodeEl(id);
  if (el) {
    el.classList.add("thinking");
    setPresence(el, "sta pensando");
    els.nodes.classList.add("thinker-focus");
    state.thinking = id;
  }
}

function markSpoke(id) {
  if (state.spoke) { const p = nodeEl(state.spoke); if (p) p.classList.remove("spoke"); }
  const el = nodeEl(id);
  if (!el) { state.spoke = null; return; }        // "runtime" / unknown: no node
  // Refresh the just-spoke emphasis even for consecutive turns.
  el.classList.remove("spoke"); void el.offsetWidth; el.classList.add("spoke");
  setPresence(el, "ha parlato");
  setTimeout(() => {
    el.classList.remove("spoke");
    if (!el.classList.contains("thinking") && !el.classList.contains("speaking")) {
      setPresence(el, "in ascolto");
    }
  }, 2200);
  state.spoke = id;
}

function nodeEl(id) { return els.nodes.querySelector(`.node[data-id="${cssEscape(id)}"]`); }

function setPresence(el, label) {
  const presence = el.querySelector(".presence");
  if (presence) presence.textContent = label;
  const name = state.displayNames.get(el.dataset.id) || el.dataset.id;
  el.setAttribute("aria-label", `${name}, ${label}`);
}

function setSpeaking(id) {
  if (state.speaking) {
    const previous = nodeEl(state.speaking);
    if (previous) {
      previous.classList.remove("speaking");
      if (!previous.classList.contains("thinking")) setPresence(previous, "in ascolto");
    }
  }
  state.speaking = null;
  els.nodes.classList.remove("speaker-focus");
  if (!id) return;
  const el = nodeEl(id);
  if (el) {
    el.classList.add("speaking");
    setPresence(el, "sta parlando");
    els.nodes.classList.add("speaker-focus");
    state.speaking = id;
  }
}

function addBubble(m) {
  const div = document.createElement("div");
  div.className = "bubble " + (m.type || "");
  const color = m.speaker === state.coordinator ? getCss("--accent2") : edgeColor(m.speaker || "?");

  const meta = document.createElement("div");
  meta.className = "meta";
  const speakerName = state.displayNames.get(m.speaker) || m.speaker || "?";
  meta.innerHTML =
    `<span class="dot" style="background:${color}"></span>` +
    `<span class="who">${escapeHtml(speakerName)}</span>` +
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
  const from = tileCenter(fromId);
  const to = tileCenter(toId);
  if (!to) return;
  const stageRect = els.stage.getBoundingClientRect();
  // If asker unknown, bow out from the room centre toward the answerer.
  const a = from || { x: stageRect.width / 2, y: stageRect.height / 2, r: 0 };

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

function tileCenter(id) {
  const tile = nodeEl(id);
  if (!tile) return null;
  const stageRect = els.stage.getBoundingClientRect();
  const rect = tile.getBoundingClientRect();
  return {
    x: rect.left - stageRect.left + rect.width / 2,
    y: rect.top - stageRect.top + rect.height / 2,
    r: Math.min(rect.width, rect.height) * .18,
  };
}

function onPlayback(event) {
  if (event.active) {
    state.playback = true;
    state.active = true;
    state.playbackMode = event.mode === "auto" ? "auto" : "manual";
    state.playbackSpeed = Number(event.speed) || 1;
    state.replaySourceDone = false;
    els.replayButton.disabled = true;
    els.replayNextButton.disabled = true;
    const label = state.playbackMode === "manual"
      ? "riproduzione manuale"
      : `riproduzione automatica · ${formatSpeed(state.playbackSpeed)}×`;
    setStatus(label, "live");
    return;
  }

  // The server has finished delivering history, but the browser may still
  // be presenting it. Do not clear the last speaker or rush the local queue.
  state.replaySourceDone = true;
  maybeCompleteReplay();
}

function playNextReplayMessage() {
  if (!state.playback || state.replayBusy || state.replayQueue.length === 0) {
    maybeCompleteReplay();
    return;
  }

  const message = state.replayQueue.shift();
  state.replayCurrentMessage = message;
  state.replayBusy = true;
  state.replayWaitingForAudio = false;
  els.replayNextButton.disabled = true;
  presentMessage(message, false);

  if (els.ttsToggle.checked && message.text && nodeEl(message.speaker)) {
    state.replayWaitingForAudio = true;
    setStatus("voce in corso · il relatore resta attivo", "live");
    enqueueSpeech(message.text, message.speaker, () => {
      if (!state.playback || !state.replayBusy || !state.replayWaitingForAudio) return;
      state.replayWaitingForAudio = false;
      prepareReplayAdvance(message, true);
    });
  } else {
    prepareReplayAdvance(message, false);
  }
}

function prepareReplayAdvance(message, audioCompleted) {
  if (!state.playback || !state.replayBusy) return;
  if (state.playbackMode === "manual") {
    els.replayNextButton.disabled = false;
    setStatus("turno pronto · leggi e poi premi Avanti", "live");
    return;
  }

  if (audioCompleted) {
    finishReplayTurn();
    return;
  }
  const delay = readingDelayMs(message.text || "", state.playbackSpeed);
  setStatus(`tempo di lettura · ${Math.ceil(delay / 1000)} s`, "live");
  state.replayTimer = setTimeout(() => {
    state.replayTimer = null;
    finishReplayTurn();
  }, delay);
}

function finishReplayTurn() {
  if (!state.playback || !state.replayBusy) return;
  if (state.replayTimer !== null) clearTimeout(state.replayTimer);
  state.replayTimer = null;
  state.replayBusy = false;
  state.replayWaitingForAudio = false;
  state.replayCurrentMessage = null;
  els.replayNextButton.disabled = true;
  playNextReplayMessage();
}

function maybeCompleteReplay() {
  if (!state.playback || !state.replaySourceDone ||
      state.replayBusy || state.replayQueue.length > 0) return;

  state.playback = false;
  state.active = false;
  els.replayButton.disabled = false;
  els.replayNextButton.disabled = true;
  setThinking(null);
  setSpeaking(null);

  const deferredEnd = state.deferredEnd;
  state.deferredEnd = null;
  state.deferredStatus = null;
  if (deferredEnd) finalizeEnd(deferredEnd);
  else setStatus("riproduzione completata", "done");
}

// ---- status / end ---------------------------------------------------------
function onStatus(s) {
  if (state.playback) {
    state.deferredStatus = s;
    return;
  }
  state.active = !!s.run_active;
  els.replayButton.disabled = !!s.run_active || state.playback;
  if (s.run_active) {
    if (!state.currentJob) setStatus(`in diretta · ${s.total} interventi`, "live");
    if (!state.thinking) setThinking(state.pendingThinking);  // resume after history replay
    renderExecutionActivity();
  } else if (s.lock_stale) {
    setStatus(`interrotta · ${s.total} interventi`, "done");
  }
  if (!state.active) {
    setThinking(null);
    if (!tts.busy) setSpeaking(null);
  }
}
function onEnd(e) {
  if (state.source) { state.source.close(); state.source = null; }
  if (state.playback) {
    state.deferredEnd = e;
    maybeCompleteReplay();
    return;
  }
  finalizeEnd(e);
}
function finalizeEnd(e) {
  state.active = false;
  els.replayButton.disabled = false;
  els.replayNextButton.disabled = true;
  if (e.outcome === "termination") {
    const fallbackJob = {
      status: "failed",
      outcome: "termination",
      termination_reason: e.termination_reason,
      error: e.error,
      created_at: state.currentJob?.created_at,
      participant_ids: state.panelOrder,
    };
    state.currentJob = state.currentJob
      ? { ...state.currentJob, ...fallbackJob }
      : fallbackJob;
    state.jobTerminal = true;
    renderExecutionActivity();
    setStatus("interrotta · controlla il messaggio", "error");
  } else {
    setStatus(`conclusa · ${e.outcome || "terminata"}`, "done");
  }
  setThinking(null);
  if (!tts.busy) setSpeaking(null);
}
function setStatus(txt, kind) {
  els.status.textContent = txt;
  els.status.className = "status status-" + kind;
}

// ---- local neural text-to-speech ------------------------------------------
const tts = {
  queue: [],
  busy: false,
  audio: null,
  objectUrl: null,
  controller: null,
  generation: 0,
  status: "unknown",
  setupRequested: false,
};

function renderTTSStatus(payload) {
  tts.status = payload.state || "unavailable";
  if (tts.status === "ready" && tts.setupRequested) {
    tts.setupRequested = false;
    els.ttsToggle.checked = true;
  }
  els.ttsStatus.classList.remove("ready", "error");
  if (tts.status === "ready") {
    if (!tts.busy) els.ttsStatus.textContent = "pronta";
    els.ttsStatus.classList.add("ready");
    els.ttsToggle.disabled = false;
  } else if (tts.status === "installing") {
    els.ttsStatus.textContent = payload.phase === "model" ? "scarico modello…" : "installazione…";
    els.ttsToggle.disabled = true;
  } else if (tts.status === "setup_required") {
    els.ttsStatus.textContent = "da configurare";
    els.ttsToggle.disabled = false;
  } else {
    els.ttsStatus.textContent = "non disponibile";
    els.ttsStatus.classList.add("error");
    els.ttsToggle.disabled = false;
  }
  els.ttsStatus.title = payload.message || "Voce neurale italiana eseguita in locale";
}

async function refreshTTSStatus() {
  try {
    const response = await fetch(withAuth("/api/tts/status"), { cache: "no-store" });
    if (!response.ok) return;
    renderTTSStatus(await response.json());
  } catch {
    renderTTSStatus({ state: "unavailable", message: "Backend vocale non raggiungibile" });
  }
}

async function setupLocalTTS() {
  tts.setupRequested = true;
  els.ttsToggle.checked = false;
  els.ttsToggle.disabled = true;
  els.ttsStatus.textContent = "avvio installazione…";
  try {
    const response = await fetch(withAuth("/api/tts/setup"), {
      method: "POST",
      cache: "no-store",
      headers: { "X-Symposium-Request": "1" },
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "installazione non riuscita");
    renderTTSStatus(payload);
    setSartoriFeedback(
      "Sto preparando la voce neurale locale. Puoi continuare a usare Symposium; al termine vedrai “pronta” in alto.",
      false,
    );
  } catch (error) {
    renderTTSStatus({ state: "error", message: error.message || String(error) });
    setSartoriFeedback(error.message || String(error), true);
  }
}

function enqueueSpeech(text, id, onDone = null) {
  const chunks = speechChunks(text);
  chunks.forEach((chunk, index) => {
    tts.queue.push({
      text: chunk,
      id,
      onDone: index === chunks.length - 1 ? onDone : null,
    });
  });
  if (!tts.busy) speakNext();
}
async function speakNext() {
  if (tts.queue.length === 0) { tts.busy = false; return; }
  if (!els.ttsToggle.checked) { tts.queue = []; tts.busy = false; return; }
  tts.busy = true;
  const { text, id, onDone } = tts.queue.shift();
  const generation = tts.generation;
  tts.controller = new AbortController();
  els.ttsStatus.textContent = "genero voce…";
  let settled = false;
  const finish = (completed = true) => {
    if (settled) return;
    settled = true;
    if (tts.audio) {
      tts.audio.onplay = null;
      tts.audio.onended = null;
      tts.audio.onerror = null;
      tts.audio = null;
    }
    if (tts.objectUrl) {
      URL.revokeObjectURL(tts.objectUrl);
      tts.objectUrl = null;
    }
    tts.controller = null;
    tts.busy = false;
    setSpeaking(null);
    if (completed && onDone) onDone();
    refreshTTSStatus();
    if (completed && generation === tts.generation) speakNext();
  };
  try {
    const response = await fetch(withAuth("/api/tts/synthesize"), {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Symposium-Request": "1",
      },
      body: JSON.stringify({ text, agent_id: id }),
      signal: tts.controller.signal,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || "sintesi vocale non riuscita");
    }
    if (generation !== tts.generation || !els.ttsToggle.checked) return finish(false);
    const blob = await response.blob();
    tts.objectUrl = URL.createObjectURL(blob);
    const audio = new Audio(tts.objectUrl);
    tts.audio = audio;
    audio.onplay = () => {
      setSpeaking(id);
      els.ttsStatus.textContent = "in riproduzione";
    };
    audio.onended = () => finish(true);
    audio.onerror = () => finish(true);
    await audio.play();
  } catch (error) {
    if (error.name !== "AbortError") {
      els.ttsStatus.textContent = "errore voce";
      els.ttsStatus.classList.add("error");
      setSartoriFeedback(error.message || String(error), true);
    }
    finish(true);
  }
}
function cancelNarration() {
  tts.generation += 1;
  tts.queue = [];
  if (tts.controller) tts.controller.abort();
  if (tts.audio) {
    tts.audio.pause();
    tts.audio.removeAttribute("src");
  }
  if (tts.objectUrl) URL.revokeObjectURL(tts.objectUrl);
  tts.audio = null;
  tts.objectUrl = null;
  tts.controller = null;
  tts.busy = false;
}
function cancelSpeech() {
  cancelNarration();
  setSpeaking(null);
}
els.ttsToggle.addEventListener("change", () => {
  if (els.ttsToggle.checked && tts.status !== "ready") {
    setupLocalTTS();
    return;
  }
  // Turning narration off must not erase the visual current-speaker focus.
  if (!els.ttsToggle.checked) {
    const resumeReading = state.playback && state.replayBusy && state.replayWaitingForAudio;
    cancelNarration();
    if (resumeReading) {
      state.replayWaitingForAudio = false;
      prepareReplayAdvance(state.replayCurrentMessage || { text: "" }, false);
    }
  }
});

// ---- utils ----------------------------------------------------------------
function speechChunks(text, maxLength = 500) {
  const words = String(text).trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [""];
  const chunks = [];
  let current = "";
  for (const word of words) {
    if (current && current.length + word.length + 1 > maxLength) {
      chunks.push(current);
      current = word;
    } else {
      current += (current ? " " : "") + word;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}
function readingDelayMs(text, speed) {
  const words = String(text).trim().split(/\s+/).filter(Boolean).length;
  const normal = Math.max(4000, Math.min(60000, 1500 + words * 400));
  return Math.round(normal / Math.max(0.5, Math.min(2, speed || 1)));
}
function formatSpeed(speed) { return Number(speed).toLocaleString("it-IT"); }
function localSlug(value) {
  const slug = String(value).normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 64);
  return slug || "nuovo-agente";
}
function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }
function initials(s) { return (s + "").split(/\s+/).filter(Boolean).slice(0, 2).map(p => p[0]).join("").toUpperCase(); }
function escapeHtml(s) { return (s + "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function cssEscape(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : (s + "").replace(/"/g, '\\"'); }

bootstrap();
