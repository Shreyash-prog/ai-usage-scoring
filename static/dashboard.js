// Dashboard controller (main spec §15.3): session list, live score bars,
// evidence drawer, and a functional replay scrubber. Plain ES module, no build step.

const DIMENSIONS = ["prompt_quality", "verification", "iteration"];
const DIM_LABEL = {
  prompt_quality: "Prompt Quality",
  verification: "Verification",
  iteration: "Iteration",
};

const els = {
  activeList: document.getElementById("active-list"),
  completedList: document.getElementById("completed-list"),
  detail: document.getElementById("detail"),
  detailTitle: document.getElementById("detail-title"),
  phaseTag: document.getElementById("phase-tag"),
  scores: document.getElementById("scores"),
  scrubber: document.getElementById("scrubber"),
  replayPos: document.getElementById("replay-pos"),
  replayChat: document.getElementById("replay-chat"),
  replayEditor: document.getElementById("replay-editor"),
  evidence: document.getElementById("evidence"),
};

const state = {
  selected: null,
  ws: null,
  events: [],          // PersistedEvents in seq order
  scores: {},          // dimension -> score row (latest)
};

// --- session list -----------------------------------------------------------

async function refreshSessions() {
  let sessions;
  try {
    sessions = await (await fetch("/api/sessions")).json();
  } catch {
    return;
  }
  const active = sessions.filter((s) => s.status === "active");
  const done = sessions.filter((s) => s.status !== "active");
  renderList(els.activeList, active);
  renderList(els.completedList, done);
}

function renderList(ul, sessions) {
  ul.innerHTML = "";
  for (const s of sessions) {
    const li = document.createElement("li");
    const taskInfo = `task ${s.current_task_idx + 1}/${s.task_sequence.length}`;
    li.textContent = `${s.candidate_name} — ${taskInfo} — ${s.status}`;
    if (s.id === state.selected) li.classList.add("selected");
    li.addEventListener("click", () => selectSession(s));
    ul.appendChild(li);
  }
}

// --- detail view ------------------------------------------------------------

function selectSession(session) {
  state.selected = session.id;
  state.events = [];
  state.scores = {};
  els.detail.hidden = false;
  els.detailTitle.textContent =
    `Session: ${session.candidate_name} — ${session.status}`;
  els.phaseTag.textContent = "";
  renderScores();
  renderEvidence();
  els.replayChat.innerHTML = "";
  els.replayEditor.textContent = "";
  connectWs(session.id);
  refreshSessions(); // update selection highlight
}

function connectWs(sessionId) {
  if (state.ws) {
    try { state.ws.close(); } catch { /* ignore */ }
  }
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/ws/dashboard/${sessionId}`);
  state.ws = ws;
  ws.onopen = () => ws.send(JSON.stringify({ type: "hello", last_seq: 0 }));
  ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
}

function handleMessage(msg) {
  if (msg.type === "event") {
    state.events.push(msg.event);
    state.events.sort((a, b) => a.seq - b.seq);
    onEventsChanged();
  } else if (msg.type === "score.update") {
    state.scores[msg.dimension] = msg;
    if (msg.phase === "final") els.phaseTag.textContent = "(final)";
    renderScores();
    renderEvidence();
  }
}

// --- score bars -------------------------------------------------------------

function renderScores() {
  els.scores.innerHTML = "";
  for (const dim of DIMENSIONS) {
    const row = state.scores[dim];
    const score = row ? row.score : 0;
    const conf = row ? row.confidence : 0;
    const div = document.createElement("div");
    div.className = "bar-row";
    div.innerHTML =
      `<span class="bar-label">${DIM_LABEL[dim]}</span>` +
      `<span class="bar-track"><span class="bar-fill" style="width:${score}%"></span></span>` +
      `<span class="bar-num">${score.toFixed(1)}  conf ${conf.toFixed(2)}</span>`;
    els.scores.appendChild(div);
  }
}

// --- evidence drawer (clickable seq refs) -----------------------------------

function renderEvidence() {
  els.evidence.innerHTML = "";
  for (const dim of DIMENSIONS) {
    const row = state.scores[dim];
    if (!row) continue;
    const seqs = row.evidence_snippets || [];
    const wrap = document.createElement("div");
    wrap.className = "ev-dim";
    const name = document.createElement("span");
    name.className = "ev-name";
    name.textContent = `${DIM_LABEL[dim]}: `;
    wrap.appendChild(name);
    if (!seqs.length) {
      wrap.appendChild(document.createTextNode("—"));
    }
    for (const seq of seqs) {
      const ref = document.createElement("span");
      ref.className = "seq-ref";
      ref.textContent = `seq ${seq}`;
      ref.title = "Jump replay to this event";
      ref.addEventListener("click", () => jumpToSeq(seq));
      wrap.appendChild(ref);
    }
    els.evidence.appendChild(wrap);
  }
}

// --- replay scrubber --------------------------------------------------------

function onEventsChanged() {
  const max = state.events.length;
  const atEnd = Number(els.scrubber.value) >= Number(els.scrubber.max);
  els.scrubber.max = String(max);
  if (atEnd) els.scrubber.value = String(max); // follow live tail
  renderReplay();
}

function jumpToSeq(seq) {
  const idx = state.events.findIndex((e) => e.seq === seq);
  if (idx >= 0) {
    els.scrubber.value = String(idx + 1);
    renderReplay();
  }
}

function renderReplay() {
  const k = Number(els.scrubber.value);
  const upto = state.events.slice(0, k);
  els.replayPos.textContent = `seq ${k} / ${state.events.length}`;

  // Editor: last snapshot at or before position k.
  let code = "";
  for (const e of upto) {
    if (e.type === "editor.snapshot") code = e.payload.code || "";
  }
  els.replayEditor.textContent = code;

  // Chat: prompts and responses up to position k.
  els.replayChat.innerHTML = "";
  for (const e of upto) {
    if (e.type === "chat.prompt_sent") addReplayMsg("You", e.payload.text, "user");
    else if (e.type === "chat.response_received") addReplayMsg("AI", e.payload.text, "ai");
  }
  els.replayChat.scrollTop = els.replayChat.scrollHeight;
}

function addReplayMsg(who, text, cls) {
  const div = document.createElement("div");
  div.className = `replay-msg ${cls}`;
  div.innerHTML = `<span class="who">${who}:</span> `;
  div.appendChild(document.createTextNode(text || ""));
  els.replayChat.appendChild(div);
}

// --- bootstrap --------------------------------------------------------------

els.scrubber.addEventListener("input", renderReplay);
refreshSessions();
setInterval(refreshSessions, 3000);
