// Conductor UI — Stage 1 scaffold.
// Opens a WebSocket to /ws/conductor, renders messages, sends user input.
// Stage 2 will replace the simple echo behavior with real Conductor events.
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
};

let socket = null;

function setConnState(state) {
  // state ∈ {"connecting", "connected", "disconnected"}
  elements.dot.className = `dot dot-${state}`;
  elements.label.textContent = {
    connecting:   "connecting…",
    connected:    "connected",
    disconnected: "disconnected — refresh to retry",
  }[state] || state;
  elements.sendBtn.disabled = state !== "connected";
  elements.input.disabled   = state !== "connected";
}

function appendBubble(kind, text) {
  // Remove the placeholder once we have any real content.
  const placeholder = elements.convo.querySelector(".placeholder");
  if (placeholder) placeholder.remove();

  const bubble = document.createElement("div");
  bubble.className = `bubble bubble-${kind}`;
  bubble.textContent = text;
  elements.convo.appendChild(bubble);
  elements.convo.scrollTop = elements.convo.scrollHeight;
}

function handleServerMessage(msg) {
  // Stage 1: server emits {type: "hello"} on connect, {type: "echo"} per send.
  // Stage 2 will add speak / ask / status / approval_required / session_end.
  switch (msg.type) {
    case "hello":
      appendBubble("system", msg.message || "connected");
      break;
    case "echo":
      appendBubble("echo", `echo ← ${JSON.stringify(msg.received)}`);
      break;
    default:
      // Render unknown types verbatim — useful as a debug fallback.
      appendBubble("system", `[${msg.type}] ${JSON.stringify(msg)}`);
  }
}

function connect() {
  setConnState("connecting");
  socket = new WebSocket(WS_URL);

  socket.addEventListener("open", () => setConnState("connected"));

  socket.addEventListener("message", (evt) => {
    let parsed;
    try {
      parsed = JSON.parse(evt.data);
    } catch (err) {
      appendBubble("system", `(non-JSON server message: ${evt.data})`);
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
  if (!text || !socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ type: "user_input", text }));
  appendBubble("user", text);
  elements.input.value = "";
});

connect();
