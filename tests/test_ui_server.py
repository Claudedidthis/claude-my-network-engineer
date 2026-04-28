"""Stage 1 tests: FastAPI scaffold serves the SPA shell, /health, and the
WebSocket handshakes correctly with echo.

These tests run against the FastAPI TestClient (in-process, no real port).
They prove plumbing — Stage 2 is where Conductor-shaped events join the
party. The fastapi+websockets+httpx deps are required (server extra);
tests skip cleanly if they're missing so CI without the [server] extra
doesn't fail spuriously.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("websockets")

from fastapi.testclient import TestClient  # noqa: E402

from network_engineer.ui.server import create_app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


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


def test_ws_handshake_and_hello(client: TestClient) -> None:
    """On connect, the server should send a hello frame describing the
    Stage 1 scaffold so the browser-side UI can render an initial system
    line. Stage 2 will adjust this contract."""
    with client.websocket_connect("/ws/conductor") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["stage"] == 1
        assert "message" in hello


def test_ws_echoes_user_input(client: TestClient) -> None:
    """Stage 1 plumbing test: anything the browser sends comes back as
    {"type":"echo","received":<sent>} so the UI can verify the loop
    is bidirectional before any Conductor wiring exists."""
    with client.websocket_connect("/ws/conductor") as ws:
        ws.receive_json()  # discard the hello frame
        ws.send_json({"type": "user_input", "text": "ping"})
        echo = ws.receive_json()
        assert echo["type"] == "echo"
        assert echo["received"] == {"type": "user_input", "text": "ping"}


def test_create_app_is_a_factory() -> None:
    """create_app() must return fresh instances — important so tests don't
    share routing state and so the server can be re-instantiated under
    uvicorn workers if we ever scale beyond one."""
    a = create_app()
    b = create_app()
    assert a is not b
