"""FastAPI server for the Conductor web UI.

Stage 1 scope: scaffold only. Endpoints:
  GET  /              — serves index.html
  GET  /static/<path> — JS + CSS
  GET  /health        — boot + readiness check (returns {"status":"ok"})
  WS   /ws/conductor  — handshake + echo. Real Conductor wiring lands in
                        Stage 2; the echo here proves the bidirectional
                        plumbing works without needing the LLM.

Defaults bind to 127.0.0.1 only — there is NO authentication on this
server. Exposing it on a LAN/public address requires real auth, which is
out of scope for the MVP. The `serve()` helper enforces a localhost bind
unless the operator explicitly passes a different host.
"""
from __future__ import annotations

import logging
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("network_engineer.ui.server")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    """Build the FastAPI app. Factory style so tests can construct fresh
    instances without sharing global state."""
    app = FastAPI(
        title="ClaudeMyNetworkEngineer Conductor",
        description="Web UI for the Conductor agent. Localhost-only.",
        version="0.1.0",
    )

    @app.get("/health")
    def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def _index() -> FileResponse:
        # Serve the SPA shell. All app logic is in /static/app.js.
        return FileResponse(_STATIC_DIR / "index.html")

    # Static assets (JS/CSS). The SPA loads them from /static/*.
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )

    @app.websocket("/ws/conductor")
    async def _ws_conductor(ws: WebSocket) -> None:
        """Stage 1: handshake + echo. Stage 2 will replace the body with
        the Conductor bridge.

        Echoes every message back wrapped in `{"type": "echo", "received": ...}`
        so the browser-side UI can prove the connection is bidirectional.
        """
        await ws.accept()
        await ws.send_json({"type": "hello", "stage": 1, "message": (
            "Conductor UI scaffold. WebSocket is live. The Conductor bridge "
            "lands in Stage 2 — for now this endpoint just echoes."
        )})
        try:
            while True:
                data: Any = await ws.receive_json()
                await ws.send_json({"type": "echo", "received": data})
        except WebSocketDisconnect:
            log.debug("ws_disconnect")

    return app


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8088,
    open_browser: bool = True,
) -> None:
    """Run the FastAPI server.

    Defaults to localhost-only. If `host` is not 127.0.0.1 / localhost the
    operator is opting in to wider exposure; we log a warning because there
    is no auth layer yet.
    """
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "ui_server_non_localhost_bind",
            extra={
                "host": host,
                "warning": (
                    "Server has NO authentication. Binding to a non-localhost "
                    "address exposes the Conductor to anyone on that network."
                ),
            },
        )

    url = f"http://{host}:{port}/"
    print(f"Conductor UI starting on {url} (Ctrl+C to stop)")

    if open_browser and host in ("127.0.0.1", "localhost"):
        # Best-effort browser launch — fine if it fails (headless, CI).
        try:
            webbrowser.open(url)
        except Exception:
            pass

    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level="info",
    )
