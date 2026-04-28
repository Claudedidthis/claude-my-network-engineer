"""Server tests: FastAPI scaffold + Conductor WebSocket bridge.

Three test groups:
  • Static surface — /health, /, /static/* serve the right content.
  • WS bridge plumbing — handshake works; speak events reach the browser;
    user input reaches the Conductor; session_end ends the conversation.
  • Defenses — origin allowlist, concurrent-session cap, malformed input.

Each test injects a fake `conductor_runner` callable so we exercise the
real adapter + WS plumbing without touching Anthropic or UniFi.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("websockets")

from fastapi.testclient import TestClient  # noqa: E402

from network_engineer.ui.server import create_app  # noqa: E402
from network_engineer.ui.web_adapter import WebConductorIO  # noqa: E402


# ── Fake runners that drive the adapter through scripted sequences ────────


def _runner_speak_then_exit(adapter: WebConductorIO) -> None:
    """Greet, then exit cleanly — lets us assert the speak event arrives."""
    adapter.on_say("Welcome to the test session.")
    # Conductor returning is what triggers session_end downstream.


def _runner_echo_one_input(adapter: WebConductorIO) -> None:
    """Block on one user input, echo it back as a speak, then exit.

    Mimics the simplest possible Conductor turn: agent speaks, operator
    replies, agent acknowledges. Lets us assert the inbound queue path
    works end-to-end."""
    adapter.on_say("Say something.")
    reply = adapter.on_user_input("> ")
    adapter.on_say(f"You said: {reply}")


def _runner_emit_status(adapter: WebConductorIO) -> None:
    """Emit one status event so the UI can verify the status path."""
    adapter.on_status({"event": "tool_starting", "tool": "read_snapshot"})
    adapter.on_status({"event": "tool_done", "tool": "read_snapshot",
                       "duration_s": 0.42, "had_error": False})


@pytest.fixture()
def client() -> TestClient:
    """Default client with a no-op runner that exits immediately."""
    return TestClient(create_app(conductor_runner=lambda adapter: None))


def test_health_endpoint_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_serves_html_shell(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<title>Conductor</title>" in body
    assert "/static/app.js" in body
    assert "/static/style.css" in body


def test_static_js_served(client: TestClient) -> None:
    response = client.get("/static/app.js")
    assert response.status_code == 200
    # Verify it's the actual app.js — check for a string only it contains.
    assert "WebSocket" in response.text


def test_static_css_served(client: TestClient) -> None:
    response = client.get("/static/style.css")
    assert response.status_code == 200
    # Sanity-check we're getting real CSS, not a 404 page.
    assert ".bubble" in response.text


def test_create_app_is_a_factory() -> None:
    """create_app() must return fresh instances — important so tests don't
    share routing state and so the server can be re-instantiated under
    uvicorn workers if we ever scale beyond one."""
    a = create_app(conductor_runner=lambda _: None)
    b = create_app(conductor_runner=lambda _: None)
    assert a is not b


# ── WebSocket bridge — Conductor I/O round-trips ────────────────────────────


def test_ws_speak_event_reaches_browser() -> None:
    """When the Conductor calls on_say, the browser should receive a
    {"type":"speak", "text":...} message followed by session_end when
    the runner returns."""
    app = create_app(conductor_runner=_runner_speak_then_exit)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        # Collect events until session_end. Bound the loop so a bug
        # doesn't hang the test.
        events = []
        for _ in range(10):
            msg = ws.receive_json()
            events.append(msg)
            if msg.get("type") == "session_end":
                break
        speaks = [e for e in events if e.get("type") == "speak"]
        assert len(speaks) == 1
        assert speaks[0]["text"] == "Welcome to the test session."
        assert any(e.get("type") == "session_end" for e in events)


def test_ws_user_input_reaches_conductor() -> None:
    """The runner blocks on on_user_input; the browser sends a
    user_input message; the runner echoes it back."""
    app = create_app(conductor_runner=_runner_echo_one_input)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        # First speak: "Say something."
        first = ws.receive_json()
        assert first["type"] == "speak"
        assert "Say something" in first["text"]

        ws.send_json({"type": "user_input", "text": "hello from browser"})

        # Second speak: echo of operator input.
        second = ws.receive_json()
        assert second["type"] == "speak"
        assert "hello from browser" in second["text"]

        # session_end follows.
        end = ws.receive_json()
        assert end["type"] == "session_end"


def test_ws_status_events_forwarded_with_event_discriminator() -> None:
    """on_status events should arrive as {"type": "status", "event": ...,
    plus the rest of the payload}."""
    app = create_app(conductor_runner=_runner_emit_status)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        events = []
        for _ in range(10):
            msg = ws.receive_json()
            events.append(msg)
            if msg.get("type") == "session_end":
                break
        statuses = [e for e in events if e.get("type") == "status"]
        assert len(statuses) >= 2
        starting = next(e for e in statuses if e["event"] == "tool_starting")
        done = next(e for e in statuses if e["event"] == "tool_done")
        assert starting["tool"] == "read_snapshot"
        assert done["duration_s"] == 0.42


def test_ws_disconnect_unblocks_a_waiting_runner() -> None:
    """Operator closing the browser tab while the Conductor is blocked
    on input must unblock it via the SessionEnded path. We assert the
    runner thread completes without hanging."""
    completed: list[bool] = []

    def _runner_that_blocks(adapter: WebConductorIO) -> None:
        adapter.on_say("Waiting…")
        try:
            adapter.on_user_input("> ")
        except Exception:  # SessionEnded
            pass
        completed.append(True)

    app = create_app(conductor_runner=_runner_that_blocks)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        ws.receive_json()  # the speak
        # Closing the WS via context manager exit triggers disconnect.
    # Give the worker thread a beat to wake and complete.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not completed:
        time.sleep(0.05)
    assert completed == [True], (
        "runner thread did not exit after WS drop — disconnect path is broken"
    )


# ── Defenses ────────────────────────────────────────────────────────────────


def test_ws_rejects_disallowed_origin() -> None:
    """A handshake from a non-allowlisted Origin should be closed before
    accept(). evil.com must not get a Conductor session."""
    app = create_app(conductor_runner=lambda _: None)
    client = TestClient(app)
    # TestClient's websocket_connect supports custom headers via subprotocols
    # / extra_headers in newer Starlette; for older, we use the underlying
    # WebSocketTestSession primitive. Either way, sending a hostile Origin
    # should result in a closed connection.
    with pytest.raises(Exception):  # WebSocketDisconnect or ConnectionClosed
        with client.websocket_connect(
            "/ws/conductor",
            headers={"origin": "http://evil.com"},
        ) as ws:
            # If accept happened, this receive raises on close-from-server.
            ws.receive_json()


def test_ws_accepts_localhost_origin() -> None:
    """Same-origin (127.0.0.1, localhost, [::1]) must be allowed."""
    app = create_app(conductor_runner=_runner_speak_then_exit)
    with TestClient(app).websocket_connect(
        "/ws/conductor",
        headers={"origin": "http://localhost:8088"},
    ) as ws:
        first = ws.receive_json()
        assert first["type"] == "speak"


def test_ws_accepts_missing_origin_header() -> None:
    """No Origin header at all (curl, websockets-cli, tests) is allowed —
    network reachability is gated by the localhost bind."""
    app = create_app(conductor_runner=_runner_speak_then_exit)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        first = ws.receive_json()
        assert first["type"] == "speak"


def test_session_cap_uses_race_free_acquire(monkeypatch: Any) -> None:
    """Stage 2 security review caught a TOCTOU bug in the cap check
    (the previous code did `not _session_sem.locked() and _value > 0`
    then `await acquire()` — two concurrent handshakes could both pass
    the check and one would block forever holding accept() open).
    Verify the bug is gone: when the semaphore is exhausted, a new
    handshake is rejected synchronously with code 1013 + an error
    frame, with no blocking acquire."""
    import threading as _t
    from network_engineer.ui import server as srv

    # Drain the semaphore so the next handshake hits the cap.
    sem = _t.BoundedSemaphore(value=1)
    sem.acquire()  # cap is now 0
    monkeypatch.setattr(srv, "_session_sem", sem)
    monkeypatch.setattr(srv, "_MAX_CONCURRENT_SESSIONS", 1)

    app = create_app(conductor_runner=lambda _: None)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "max concurrent sessions" in msg["reason"]


def test_ws_handler_ignores_malformed_messages() -> None:
    """A non-dict client message must not crash the bridge — defense
    against schema drift / hostile clients."""
    app = create_app(conductor_runner=_runner_echo_one_input)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        ws.receive_json()  # speak
        # Send a list, then a real input. The list should be silently
        # dropped; the dict should land normally.
        ws.send_json([1, 2, 3])
        ws.send_json({"type": "user_input", "text": "real reply"})
        echo = ws.receive_json()
        assert echo["type"] == "speak"
        assert "real reply" in echo["text"]


# ── Approval flow integration (Stage 3) ─────────────────────────────────────


def _runner_request_approval_then_apply(adapter: WebConductorIO) -> None:
    """Simulate a gated tool call: emit approval_required, wait for the
    operator's button click via wait_for_approval, then act based on the
    answer. Exercises the full approval channel end-to-end."""
    action_id = "act-test-12345"
    adapter.on_status({
        "event": "approval_required",
        "tool": "apply_change",
        "action_id": action_id,
        "description": "apply_change(label='demo')",
        "args": {"label": "demo"},
    })
    approved = adapter.wait_for_approval(action_id)
    if approved:
        adapter.on_status({
            "event": "approval_granted",
            "tool": "apply_change",
            "action_id": action_id,
        })
        adapter.on_say("Change applied.")
    else:
        adapter.on_status({
            "event": "approval_denied",
            "tool": "apply_change",
            "reason": "operator rejected",
        })
        adapter.on_say("Change cancelled.")


def test_ws_approve_button_unblocks_runner_with_true() -> None:
    """Full happy path through the bridge: status event arrives, browser
    sends {type:'approve', action_id}, runner sees True, applies."""
    app = create_app(conductor_runner=_runner_request_approval_then_apply)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        # Receive approval_required.
        appr = ws.receive_json()
        assert appr["type"] == "status"
        assert appr["event"] == "approval_required"
        action_id = appr["action_id"]
        assert appr["args"] == {"label": "demo"}

        # Send the approval.
        ws.send_json({"type": "approve", "action_id": action_id})

        # Collect remaining events until session_end.
        events = [appr]
        for _ in range(10):
            events.append(ws.receive_json())
            if events[-1].get("type") == "session_end":
                break
        granted = next(e for e in events if e.get("event") == "approval_granted")
        speak = next(e for e in events if e.get("type") == "speak")
        assert granted["action_id"] == action_id
        assert "Change applied" in speak["text"]


def test_ws_reject_button_unblocks_runner_with_false() -> None:
    """Rejection path: browser sends {type:'reject'}, runner sees False,
    cancels."""
    app = create_app(conductor_runner=_runner_request_approval_then_apply)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        appr = ws.receive_json()
        action_id = appr["action_id"]
        ws.send_json({"type": "reject", "action_id": action_id})

        events = [appr]
        for _ in range(10):
            events.append(ws.receive_json())
            if events[-1].get("type") == "session_end":
                break
        denied = next(e for e in events if e.get("event") == "approval_denied")
        speak = next(e for e in events if e.get("type") == "speak")
        assert denied["reason"] == "operator rejected"
        assert "cancelled" in speak["text"]


def test_ws_stale_action_id_is_ignored_by_adapter() -> None:
    """A buggy/stale client sending an action_id that doesn't match the
    pending approval must not satisfy the gate. The adapter waits for
    a matching id; the stale message is dropped."""
    state: dict[str, Any] = {}

    def _runner(adapter: WebConductorIO) -> None:
        adapter.on_status({
            "event": "approval_required",
            "tool": "apply_change",
            "action_id": "real-id",
            "description": "real",
            "args": {},
        })
        # Should ignore the stale id and pick up the real one.
        approved = adapter.wait_for_approval("real-id")
        state["approved"] = approved
        adapter.on_say(f"approved={approved}")

    app = create_app(conductor_runner=_runner)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        appr = ws.receive_json()
        assert appr["action_id"] == "real-id"
        # Send a stale approval first — must be dropped silently.
        ws.send_json({"type": "approve", "action_id": "stale-id"})
        # Then the real approval.
        ws.send_json({"type": "approve", "action_id": "real-id"})
        # Read until speak.
        for _ in range(10):
            evt = ws.receive_json()
            if evt.get("type") == "speak":
                assert "approved=True" in evt["text"]
                break
        else:
            pytest.fail("never saw speak event with the real-id approval")


def test_ws_disconnect_unblocks_a_runner_waiting_on_approval() -> None:
    """If the operator closes the tab while the runner is blocked on
    wait_for_approval, the runner must unblock (via _DISCONNECT) and
    return False — the gate path is tested separately."""
    state: dict[str, Any] = {}

    def _runner(adapter: WebConductorIO) -> None:
        adapter.on_status({
            "event": "approval_required",
            "tool": "apply_change",
            "action_id": "stuck",
            "description": "stuck",
            "args": {},
        })
        try:
            approved = adapter.wait_for_approval("stuck")
        except Exception:
            approved = None
        state["approved"] = approved

    app = create_app(conductor_runner=_runner)
    with TestClient(app).websocket_connect("/ws/conductor") as ws:
        ws.receive_json()  # approval_required
        # Closing the WS via context exit triggers disconnect.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and "approved" not in state:
        time.sleep(0.05)
    assert state.get("approved") is False, (
        "wait_for_approval should return False on disconnect, got "
        f"{state.get('approved')!r}"
    )
