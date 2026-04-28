"""WebConductorIO — bridges the Conductor's blocking I/O to a WebSocket.

Threading model
---------------
The Conductor is synchronous: its run_agent loop blocks on input(), runs
tools synchronously, and emits events via callbacks. FastAPI's WS handler
is async. We bridge the two:

    [browser] ─WS─► [async WS handler] ─inbound queue─► [worker thread / Conductor]
    [browser] ◄─WS─ [async WS handler] ◄─outbound queue─ [worker thread / Conductor]

Both queues are stdlib `queue.Queue` (thread-safe). The sync side (worker
thread) blocks on .get()/.put() naturally. The async side uses
`asyncio.to_thread(q.get)` to wait without starving the event loop.

Message schemas
---------------
Server → client:
  {"type": "speak",       "text": "..."}                        Conductor speech
  {"type": "status",      "event": "tool_starting"|"tool_done"|
                                   "awaiting_reply"|"interjection_window_open"|
                                   "approval_required"|"approval_denied"|
                                   "approval_granted", ...}     Loop state events
  {"type": "session_end", "reason": "..."}                       Conductor exited

Client → server:
  {"type": "user_input",  "text": "..."}                         Operator typed something

All other shapes are ignored on each side (defense-in-depth against future
schema drift).

Disconnect handling
-------------------
When the WebSocket drops, the WS handler calls `adapter.disconnect()`. That
puts a sentinel on the inbound queue; the next call to `on_user_input`
raises SessionEnded, which propagates up out of run_agent and Conductor.run
exits cleanly through the existing exception path.
"""
from __future__ import annotations

import logging
import queue
from typing import Any

log = logging.getLogger("network_engineer.ui.web_adapter")


class SessionEnded(Exception):
    """Raised by on_user_input when the WebSocket has dropped.

    Surfaces up through run_agent into Conductor.run's exception handler,
    which writes the session digest and returns cleanly. This is the web
    counterpart of the CLI's KeyboardInterrupt path.
    """


# Sentinel pushed into inbound when the WS disconnects. Distinguished from a
# real `dict` user-input message by identity check.
_DISCONNECT = object()
# Sentinel pushed into outbound to tell the async drain task to stop.
_END_DRAIN = object()


class WebConductorIO:
    """ConductorIO adapter that funnels events through two thread-safe queues.

    Construct one per WebSocket connection. The adapter satisfies the
    ConductorIO Protocol structurally — it has `mode = "web"` plus
    on_say/on_user_input/on_status — and exposes two bridge primitives the
    server uses: `disconnect()` to wake a blocked on_user_input, and
    `signal_session_end()` to let the drain task wind down once the
    Conductor has returned.
    """

    mode: str = "web"

    # Outbound is bounded so a slow / disconnected WS provides backpressure
    # to the Conductor (its on_say / on_status calls block when the queue
    # is full) instead of letting the queue grow unboundedly. 128 events
    # is plenty for any sane interactive session — a typical turn emits
    # 1-5 events.
    _OUTBOUND_MAXSIZE = 128

    def __init__(self) -> None:
        self.outbound: queue.Queue[Any] = queue.Queue(maxsize=self._OUTBOUND_MAXSIZE)
        self.inbound: queue.Queue[Any] = queue.Queue()

    # ── ConductorIO callbacks ───────────────────────────────────────────

    def on_say(self, text: str) -> None:
        """Conductor speech (both speak text and ask questions). The UI
        renders these as agent message bubbles. The status event that
        follows (awaiting_reply vs interjection_window_open) tells the UI
        whether a reply is required."""
        self.outbound.put({"type": "speak", "text": text})

    def on_status(self, event: dict[str, Any]) -> None:
        """Loop state events. Forwarded as `{"type": "status", ...event}`
        so the UI can switch input affordances (input box vs approval
        panel, etc) and render tool progress lines."""
        # Spread event keys onto the message; "event" key carries the
        # discriminator the UI uses to switch behavior.
        self.outbound.put({"type": "status", **event})

    def on_user_input(self, prompt: str) -> str:
        """Block the worker thread until the browser sends a user_input
        message (or the WS drops)."""
        item = self.inbound.get()
        if item is _DISCONNECT:
            raise SessionEnded("WebSocket disconnected; ending session.")
        if isinstance(item, dict):
            text = item.get("text", "")
            return str(text)
        # Defensive: anything we don't recognize is a no-op empty string,
        # which the loop typically treats as "no interjection."
        return ""

    # ── Bridge primitives used by the server's WS handler ───────────────

    def disconnect(self) -> None:
        """Wake any blocking on_user_input call so the Conductor can exit."""
        self.inbound.put(_DISCONNECT)

    def signal_session_end(self, reason: str = "") -> None:
        """Push a session_end + drain-stop sentinel after the Conductor
        has returned. The drain task forwards session_end to the browser
        and then exits."""
        self.outbound.put({"type": "session_end", "reason": reason})
        self.outbound.put(_END_DRAIN)
