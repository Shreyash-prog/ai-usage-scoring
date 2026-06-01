// Candidate UI controller (main spec §15). No build step: plain ES module that
// drives Monaco (loaded via the AMD loader in the page) and one WebSocket.

const MONACO_BASE = "https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min";
const PASTE_MIN_MATCH = 40; // §15.5: a >=40-char substring shared with a recent
                            // AI response marks a paste as source_hint="chat".
const RECENT_RESPONSES_MAX = 10;

const els = {
  candidateLabel: document.getElementById("candidate-label"),
  taskProgress: document.getElementById("task-progress"),
  statusBanner: document.getElementById("status-banner"),
  taskBody: document.getElementById("task-body"),
  output: document.getElementById("output"),
  chatLog: document.getElementById("chat-log"),
  chatInput: document.getElementById("chat-input"),
  attachEditor: document.getElementById("attach-editor"),
  attachOutput: document.getElementById("attach-output"),
  sendBtn: document.getElementById("send-btn"),
  runBtn: document.getElementById("run-btn"),
  runTestsBtn: document.getElementById("run-tests-btn"),
  submitBtn: document.getElementById("submit-task-btn"),
  endBtn: document.getElementById("end-session-btn"),
};

const state = {
  sessionId: null,
  ws: null,
  editor: null,
  currentTask: null,
  recentResponses: [], // normalized AI response texts, newest last
  currentAiEl: null,
  lastSnapshotText: "",
  lastSnapshotAt: 0,
  idleTimer: null,
};

// --- helpers ----------------------------------------------------------------

function setStatus(text) {
  els.statusBanner.textContent = text || "";
}

function setActiveControls(active) {
  for (const b of [els.sendBtn, els.runBtn, els.runTestsBtn, els.submitBtn]) {
    b.disabled = !active;
  }
  els.chatInput.disabled = !active;
}

function normalize(text) {
  return text.replace(/\s+/g, " ").trim();
}

// §15.5: does the pasted text share a contiguous >=40-char run (after whitespace
// normalization) with any recent AI response? If so it almost certainly came from
// chat. We slide a 40-char window across the paste and test substring membership.
function detectPasteSource(pasted) {
  const np = normalize(pasted);
  if (np.length < PASTE_MIN_MATCH) return "unknown";
  for (const resp of state.recentResponses) {
    for (let i = 0; i + PASTE_MIN_MATCH <= np.length; i++) {
      if (resp.includes(np.substr(i, PASTE_MIN_MATCH))) return "chat";
    }
  }
  return "unknown";
}

function rememberResponse(fullText) {
  state.recentResponses.push(normalize(fullText));
  if (state.recentResponses.length > RECENT_RESPONSES_MAX) {
    state.recentResponses.shift();
  }
}

function send(msg) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(msg));
  }
}

function appendChat(who, text, cls) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.innerHTML = `<span class="who">${who}:</span> `;
  div.appendChild(document.createTextNode(text));
  els.chatLog.appendChild(div);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
  return div;
}

// --- snapshots (§12.4) ------------------------------------------------------

function takeSnapshot(trigger, force = false) {
  const code = state.editor ? state.editor.getValue() : "";
  const now = Date.now();
  // Rate limit: at most one snapshot/second, except forced (pre_chat/pre_run/manual).
  if (!force && now - state.lastSnapshotAt < 1000) return;
  state.lastSnapshotAt = now;
  state.lastSnapshotText = code;
  send({ type: "editor.snapshot", code, trigger });
}

function onEditorChange() {
  const code = state.editor.getValue();
  // Large-diff trigger — char-count delta as a lightweight proxy for the spec's
  // Levenshtein>100 (no diff lib in the browser; sufficient for snapshot timing).
  if (Math.abs(code.length - state.lastSnapshotText.length) > 100) {
    takeSnapshot("large_diff");
  }
  clearTimeout(state.idleTimer);
  state.idleTimer = setTimeout(() => takeSnapshot("idle"), 5000);
}

// --- server message handling ------------------------------------------------

function onTaskPresented(msg) {
  const task = msg.task;
  state.currentTask = task;
  els.taskProgress.textContent = `Task ${msg.task_idx + 1} of ${msg.total_tasks}`;
  els.taskBody.textContent = task.description_md;
  els.chatLog.innerHTML = "";
  els.output.textContent = "";
  state.recentResponses = [];
  if (state.editor) {
    state.editor.setValue(task.starter_code || "");
    state.lastSnapshotText = task.starter_code || "";
  }
  setActiveControls(true);
  setStatus("");
}

function onChatToken(msg) {
  if (!state.currentAiEl) {
    state.currentAiEl = appendChat("AI", "", "ai");
  }
  state.currentAiEl.appendChild(document.createTextNode(msg.text));
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
}

function onChatDone(msg) {
  if (!state.currentAiEl) appendChat("AI", msg.full_text, "ai");
  state.currentAiEl = null;
  rememberResponse(msg.full_text);
  setActiveControls(true);
}

function onExecResult(msg) {
  const parts = [];
  if (msg.stdout) parts.push(msg.stdout);
  if (msg.stderr) parts.push(msg.stderr);
  let text = parts.join("\n") || "(no output)";
  if (msg.exit_code === 124) text += "\n[Execution timed out]";
  else if (msg.exit_code !== 0) text += `\n[exit code ${msg.exit_code}]`;
  text += ` (${msg.runtime_ms} ms)`;
  els.output.textContent = text;
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case "task.presented": onTaskPresented(msg); break;
    case "chat.token": onChatToken(msg); break;
    case "chat.done": onChatDone(msg); break;
    case "chat.error":
      appendChat("AI", msg.error, "err");
      state.currentAiEl = null;
      setActiveControls(true);
      break;
    case "exec.result": onExecResult(msg); break;
    case "ack": break; // persistence acknowledgement; nothing to render
    case "session.done":
      setStatus("Session complete — thank you!");
      setActiveControls(false);
      els.endBtn.disabled = true;
      break;
    default: console.warn("Unknown server message", msg);
  }
}

// --- user actions -----------------------------------------------------------

function sendChat() {
  const text = els.chatInput.value.trim();
  if (!text) return;
  takeSnapshot("pre_chat", true); // so the server can attach current editor code
  appendChat("You", text, "user");
  send({
    type: "chat.send",
    text,
    attach_editor: els.attachEditor.checked,
    attach_output: els.attachOutput.checked,
  });
  els.chatInput.value = "";
  setActiveControls(false);
}

function runCode(extra = "") {
  takeSnapshot("pre_run", true);
  const code = state.editor.getValue() + extra;
  els.output.textContent = "Running…";
  send({ type: "code.run", code, stdin: null });
}

function submitTask() {
  send({ type: "task.submit", final_code: state.editor.getValue() });
  setActiveControls(false);
  setStatus("Submitting…");
}

// --- bootstrap --------------------------------------------------------------

async function createSession() {
  const params = new URLSearchParams(location.search);
  const candidateName = params.get("candidate_name") || "Anonymous";
  els.candidateLabel.textContent = `Candidate: ${candidateName}`;
  const res = await fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ candidate_name: candidateName }),
  });
  if (!res.ok) {
    const detail = await res.text();
    setStatus(`Could not start session: ${detail}`);
    throw new Error(detail);
  }
  const data = await res.json();
  state.sessionId = data.session_id;
}

function connectWs() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${scheme}://${location.host}/ws/session/${state.sessionId}`);
  state.ws.onopen = () => send({ type: "hello", last_seq: 0 });
  state.ws.onmessage = (ev) => handleServerMessage(JSON.parse(ev.data));
  state.ws.onclose = () => setStatus("Disconnected.");
  state.ws.onerror = () => setStatus("Connection error.");
}

function wireEvents() {
  els.sendBtn.addEventListener("click", sendChat);
  els.chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  els.runBtn.addEventListener("click", () => runCode());
  els.runTestsBtn.addEventListener("click", () => {
    const tc = state.currentTask && state.currentTask.test_code;
    runCode(tc ? `\n\n# --- tests ---\n${tc}` : "");
  });
  els.submitBtn.addEventListener("click", submitTask);
  els.endBtn.addEventListener("click", () => send({ type: "session.end" }));
}

function initEditor() {
  window.MonacoEnvironment = {
    getWorkerUrl: () =>
      URL.createObjectURL(
        new Blob(
          [
            `self.MonacoEnvironment={baseUrl:'${MONACO_BASE}/'};` +
              `importScripts('${MONACO_BASE}/vs/base/worker/workerMain.js');`,
          ],
          { type: "text/javascript" },
        ),
      ),
  };
  window.require.config({ paths: { vs: `${MONACO_BASE}/vs` } });
  window.require(["vs/editor/editor.main"], () => {
    state.editor = monaco.editor.create(document.getElementById("editor"), {
      value: "",
      language: "python",
      automaticLayout: true,
      minimap: { enabled: false },
      fontSize: 13,
    });
    state.editor.onDidChangeModelContent(onEditorChange);

    // Paste-source detection: capture the raw clipboard text on the editor node.
    state.editor.getDomNode().addEventListener("paste", (e) => {
      const pasted = (e.clipboardData || window.clipboardData).getData("text");
      if (!pasted) return;
      send({
        type: "editor.paste",
        text: pasted,
        source_hint: detectPasteSource(pasted),
        char_count: pasted.length,
      });
    });
  });
}

async function main() {
  wireEvents();
  initEditor();
  setStatus("Starting session…");
  try {
    await createSession();
    connectWs();
  } catch (err) {
    console.error(err);
  }
}

main();
