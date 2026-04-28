// Conductor UI — Stage 2: real Conductor events over WebSocket.
// Stage 3 will add the structured approval panel for write operations.
"use strict";

const WS_URL = (() => {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/conductor`;
})();

const elements = {
  dot:        document.getElementById("conn-dot"),
  label:      document.getElementById("conn-label"),
  convo:      document.getElementById("convo"),
  form:       document.getElementById("input-form"),
  input:      document.getElementById("user-input"),
  sendBtn:    document.getElementById("send-btn"),
  approvals:  document.getElementById("approvals"),
  approveBtn: document.getElementById("approve-btn"),
  rejectBtn:  document.getElementById("reject-btn"),
  apprTool:   document.getElementById("approval-tool"),
  apprDesc:   document.getElementById("approval-desc"),
  apprArgs:   document.getElementById("approval-args"),
};

let socket = null;
let inputMode = "idle";  // "idle" | "awaiting_reply" | "interjection"
let pendingApproval = null;  // {action_id, tool, description, args} | null

function setConnState(state) {
  elements.dot.className = `dot dot-${state}`;
  elements.label.textContent = {
    connecting:   "connecting…",
    connected:    "connected",
    disconnected: "disconnected — refresh to reconnect",
  }[state] || state;
  const enabled = state === "connected";
  elements.sendBtn.disabled = !enabled;
  elements.input.disabled   = !enabled;
}

function setInputMode(mode) {
  inputMode = mode;
  if (mode === "awaiting_reply") {
    elements.input.placeholder = "Reply required — type and press Enter";
    elements.input.classList.add("input-required");
  } else if (mode === "interjection") {
    elements.input.placeholder = "Press Enter to continue, or type to interject";
    elements.input.classList.remove("input-required");
  } else {
    elements.input.placeholder = "Type a message and press Enter";
    elements.input.classList.remove("input-required");
  }
}

function appendBubble(kind, text) {
  // Remove the placeholder once we have any real content.
  const placeholder = elements.convo.querySelector(".placeholder");
  if (placeholder) placeholder.remove();

  const bubble = document.createElement("div");
  bubble.className = `bubble bubble-${kind}`;
  // textContent (NOT innerHTML) — defends against XSS in any LLM-emitted text.
  bubble.textContent = text;
  elements.convo.appendChild(bubble);
  elements.convo.scrollTop = elements.convo.scrollHeight;
}

function appendStatus(text) {
  // Smaller dimmer line for tool start/done, etc.
  const placeholder = elements.convo.querySelector(".placeholder");
  if (placeholder) placeholder.remove();

  const line = document.createElement("div");
  line.className = "status-line";
  line.textContent = text;
  elements.convo.appendChild(line);
  elements.convo.scrollTop = elements.convo.scrollHeight;
}

function handleStatus(event) {
  // Render a curated subset; the rest are silently received.
  const ev = event.event;
  if (ev === "tool_starting") {
    appendStatus(`→ running ${event.tool}…`);
  } else if (ev === "tool_done") {
    const dur = event.duration_s ?? 0;
    if (event.had_error) {
      appendStatus(`→ ${event.tool} failed in ${dur}s (${event.error_type || "error"})`);
    } else {
      appendStatus(`→ ${event.tool} done in ${dur}s`);
    }
  } else if (ev === "tool_unknown") {
    appendStatus(`→ unknown tool requested: ${event.tool}`);
  } else if (ev === "awaiting_reply") {
    setInputMode("awaiting_reply");
  } else if (ev === "interjection_window_open") {
    setInputMode("interjection");
  } else if (ev === "approval_required") {
    showApprovalPanel({
      action_id:   event.action_id,
      tool:        event.tool,
      description: event.description || "",
      args:        event.args || {},
    });
    // Surface a status line in the conversation feed too — the panel
    // is the action surface, but a feed entry preserves the timeline.
    appendStatus(`🔐 approval requested for ${event.tool}`);
  } else if (ev === "approval_granted") {
    hideApprovalPanel();
    appendStatus(`✓ approval granted for ${event.tool}`);
  } else if (ev === "approval_denied") {
    hideApprovalPanel();
    appendStatus(`✗ approval denied for ${event.tool}: ${event.reason || ""}`);
  } else if (ev === "approval_misconfigured") {
    hideApprovalPanel();
    appendStatus(`⚠ approval misconfigured for ${event.tool}: ${event.reason || ""}`);
  }
  // Any other event types: ignored. The server may add new ones; the UI
  // tolerating unknowns is part of the schema-drift defense.
}

function showApprovalPanel({ action_id, tool, description, args }) {
  pendingApproval = { action_id, tool, description, args };
  // textContent throughout — defends against XSS in any LLM-emitted text
  // that flows through tool name / description / args.
  elements.apprTool.textContent = tool || "(unknown tool)";
  elements.apprDesc.textContent = description;
  // Pretty-print args. Even a hostile JSON payload renders as text via
  // textContent — no innerHTML, no eval.
  elements.apprArgs.textContent = JSON.stringify(args, null, 2);
  elements.approveBtn.disabled = false;
  elements.rejectBtn.disabled  = false;
  elements.approvals.classList.remove("hidden");
}

function hideApprovalPanel() {
  pendingApproval = null;
  elements.approvals.classList.add("hidden");
  elements.approveBtn.disabled = true;
  elements.rejectBtn.disabled  = true;
}

function sendApprovalDecision(action) {
  // action ∈ {"approve", "reject"}. We capture the action_id at click
  // time — even if the panel is racy and a new approval has just
  // arrived, sending the OLD action_id back is benign: the gate's
  // submit_via_ui rejects mismatched ids.
  if (!pendingApproval) return;
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({
    type:      action,
    action_id: pendingApproval.action_id,
  }));
  // Disable buttons immediately to prevent double-clicks; the
  // approval_granted/denied status will hide the panel.
  elements.approveBtn.disabled = true;
  elements.rejectBtn.disabled  = true;
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case "speak":
      appendBubble("agent", msg.text || "");
      // After the speak, default to interjection mode unless a status
      // event later flips it to awaiting_reply.
      setInputMode("interjection");
      break;
    case "status":
      handleStatus(msg);
      break;
    case "session_end":
      appendStatus(`session ended${msg.reason ? `: ${msg.reason}` : ""}`);
      setInputMode("idle");
      elements.input.disabled = true;
      elements.sendBtn.disabled = true;
      break;
    case "error":
      appendStatus(`error: ${msg.reason || "(unknown)"}`);
      break;
    default:
      // Unknown shape — render verbatim for debugging.
      appendStatus(`[${msg.type || "?"}] ${JSON.stringify(msg)}`);
  }
}

function connect() {
  setConnState("connecting");
  socket = new WebSocket(WS_URL);

  socket.addEventListener("open",  () => setConnState("connected"));

  socket.addEventListener("message", (evt) => {
    let parsed;
    try {
      parsed = JSON.parse(evt.data);
    } catch (err) {
      appendStatus(`(non-JSON server message: ${evt.data})`);
      return;
    }
    handleServerMessage(parsed);
  });

  socket.addEventListener("close", () => setConnState("disconnected"));
  socket.addEventListener("error", () => setConnState("disconnected"));
}

elements.form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = elements.input.value;
  // In interjection mode an empty string is a valid signal ("just continue"),
  // BUT we don't render an empty bubble. In awaiting_reply mode, the
  // server-side loop will refuse empty replies, so we let the operator
  // type something. We send whatever's there and clear the box.
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ type: "user_input", text }));
  if (text) appendBubble("user", text);
  elements.input.value = "";
});

elements.approveBtn.addEventListener("click", () => sendApprovalDecision("approve"));
elements.rejectBtn.addEventListener("click",  () => sendApprovalDecision("reject"));

// Buttons start disabled — only enabled when an approval_required event arrives.
elements.approveBtn.disabled = true;
elements.rejectBtn.disabled  = true;

connect();
