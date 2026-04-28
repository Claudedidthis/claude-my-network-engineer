"""FastAPI server for the Conductor web UI.

Endpoints:
  GET  /              — serves index.html
  GET  /static/<path> — JS + CSS
  GET  /health        — boot + readiness check (returns {"status":"ok"})
  WS   /ws/conductor  — full Conductor bridge. Each connection spawns a
                        Conductor in a worker thread; events flow through
                        WebConductorIO's two queue.Queue's.

Defaults bind to 127.0.0.1 only — there is NO authentication on this
server. Exposing it on a LAN/public address requires real auth, which is
out of scope for the MVP. The `serve()` helper enforces a localhost bind
unless the operator explicitly passes a different host.

Defenses (carried over from Stage 1 security review):
  • Origin allowlist for WS handshakes — only same-origin localhost frames
    are accepted. Blocks cross-site WebSocket hijacking once cookies/auth
    arrive in a future stage; harmless before then.
  • Concurrent-session cap (default 3). One operator typically wants one
    Conductor; the cap exists so a runaway script can't pin the LLM.
  • WS frame size cap via uvicorn's `ws_max_size` (256 KiB). Operator
    messages are tiny; this is roomy and prevents OOM from a malicious
    huge frame.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from network_engineer.ui.web_adapter import (
    SessionEnded,
    WebConductorIO,
    _END_DRAIN,
)

log = logging.getLogger("network_engineer.ui.server")

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Same-origin allowlist for WebSocket handshakes. The `nye serve` default
# bind is 127.0.0.1, and these are the only origins a same-origin SPA
# should send. An empty Origin header is allowed (curl, websockets-cli,
# tests) — we'd lose those if we required Origin to be present, and the
# 127.0.0.1 bind already gates network reachability.
_ALLOWED_ORIGIN_PREFIXES: tuple[str, ...] = (
    "http://127.0.0.1:",
    "http://localhost:",
    "http://[::1]:",
)

# Concurrent Conductor sessions allowed at once. Process-local. Tunable.
# Use threading.BoundedSemaphore (NOT asyncio.Semaphore) because
# `.acquire(blocking=False)` is the only race-free non-blocking try; the
# previous "check `_value` then await acquire" pattern was a TOCTOU bug
# where two concurrent handshakes could both acquire and one would block
# forever holding accept() open. (Caught by Stage 2 security review.)
_MAX_CONCURRENT_SESSIONS = 3
_session_sem = threading.BoundedSemaphore(value=_MAX_CONCURRENT_SESSIONS)

# WS frame size cap: 256 KiB. Plenty for operator messages; small enough
# that a hostile frame can't OOM. uvicorn picks this up via `ws_max_size`.
_WS_MAX_SIZE_BYTES = 256 * 1024


def _origin_allowed(origin: str) -> bool:
    if not origin:
        return True
    return any(origin.startswith(p) for p in _ALLOWED_ORIGIN_PREFIXES)


def create_app(
    *,
    conductor_runner: Any = None,
) -> FastAPI:
    """Build the FastAPI app. Factory style so tests can construct fresh
    instances without sharing global state.

    `conductor_runner` is an optional callable `(WebConductorIO) -> None`
    that runs (or fakes) the Conductor for a session. Defaults to
    `_run_conductor_sync` which builds the real Conductor against the
    real AIRuntime + UnifiClient. Tests inject a scripted version that
    drives the adapter through a known sequence of speak/status events
    without making real Anthropic calls.
    """
    runner = conductor_runner if conductor_runner is not None else _run_conductor_sync
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
        """Spin up a Conductor for this connection and bridge its I/O
        through WebConductorIO. The Conductor runs in a worker thread;
        two stdlib queues ferry events between the async WS handler and
        the sync agent loop."""
        # Origin allowlist BEFORE accept() so we never even shake hands
        # with cross-origin attackers.
        origin = ws.headers.get("origin", "")
        if not _origin_allowed(origin):
            await ws.close(code=1008, reason="origin not allowed")
            log.warning("ws_origin_rejected", extra={"origin": origin})
            return

        # Concurrent-session cap — race-free non-blocking acquire.
        # threading.BoundedSemaphore.acquire(blocking=False) returns True
        # iff a slot was claimed; no TOCTOU window.
        if not _session_sem.acquire(blocking=False):
            await ws.accept()
            await ws.send_json({
                "type": "error",
                "reason": (
                    f"max concurrent sessions ({_MAX_CONCURRENT_SESSIONS}) "
                    "reached. Close another tab and reconnect."
                ),
            })
            await ws.close(code=1013)
            return

        try:
            await ws.accept()
            await _run_session(ws, runner)
        finally:
            _session_sem.release()

    return app


async def _run_session(ws: WebSocket, runner: Any) -> None:
    """Drive one Conductor session over an accepted WebSocket.

    Three concurrent flows:
      • forward_outbound — pulls from adapter.outbound queue (sync,
        thread-safe; awaited via asyncio.to_thread) and sends each event
        as JSON over the WS until it sees the _END_DRAIN sentinel.
      • forward_inbound — pulls JSON from the WS, validates loose shape,
        pushes into adapter.inbound for the Conductor to consume on its
        next on_user_input call.
      • conductor_future — asyncio.run_in_executor wrapping the blocking
        Conductor.run() call. Resolves when the agent emits done_for_now,
        max_turns trips, or SessionEnded propagates from a WS drop.

    Termination paths:
      A. Conductor returns normally → adapter.signal_session_end() is
         posted → drain task forwards session_end then exits on
         _END_DRAIN → we cancel the receive task and close the WS.
      B. WS drops → forward_inbound catches WebSocketDisconnect →
         adapter.disconnect() pushes _DISCONNECT → on_user_input raises
         SessionEnded → Conductor.run exits cleanly → path A's cleanup
         runs.
    """
    adapter = WebConductorIO()
    loop = asyncio.get_running_loop()

    async def forward_outbound() -> None:
        """Pull events from the Conductor's outbound queue and send over WS."""
        while True:
            item = await asyncio.to_thread(adapter.outbound.get)
            if item is _END_DRAIN:
                return
            try:
                await ws.send_json(item)
            except Exception:
                # WS is gone; stop draining. Conductor's exit path will
                # signal disconnect on its own.
                return

    async def forward_inbound() -> None:
        """Pull operator input from WS and push into the inbound queue."""
        try:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict):
                    adapter.inbound.put(msg)
                # Anything else: silently drop (defense against schema drift)
        except WebSocketDisconnect:
            log.debug("ws_disconnect")
        except Exception as exc:
            log.warning("ws_recv_error", extra={"error_type": exc.__class__.__name__})
        finally:
            # Whatever caused us to stop reading, wake the Conductor so
            # it can exit gracefully if it was blocked on input.
            adapter.disconnect()

    drain_task = asyncio.create_task(forward_outbound(), name="ws_drain")
    recv_task = asyncio.create_task(forward_inbound(), name="ws_recv")

    # Run the (blocking) Conductor in a worker thread.
    conductor_future = loop.run_in_executor(None, runner, adapter)

    try:
        # The Conductor finishing is the canonical termination signal.
        # If the WS drops first, forward_inbound triggers a disconnect,
        # the Conductor exits via SessionEnded, conductor_future
        # resolves, and we land here.
        await conductor_future
    except Exception as exc:
        log.exception("conductor_thread_error",
                      extra={"error_type": exc.__class__.__name__})
    finally:
        # Always push the session_end + drain-stop sentinel — even if the
        # Conductor crashed — so forward_outbound exits its blocking
        # to_thread(queue.get) call rather than leaking the thread.
        adapter.signal_session_end()
        try:
            await asyncio.wait_for(drain_task, timeout=2.0)
        except (TimeoutError, asyncio.TimeoutError):
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain_task
        recv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await recv_task
        try:
            await ws.close()
        except Exception:
            pass

    # Known limitation (Stage 2 security review B/D): there is no kill-
    # switch for a runaway Conductor. If the agent loops on tool calls and
    # never reaches an on_user_input boundary, the disconnect sentinel sits
    # unconsumed and the worker thread runs to natural completion. With a
    # 100-turn cap and small token budgets this is bounded; a hard
    # cancellation requires threading a cancel-token through run_agent,
    # which is a separate refactor.


def _run_conductor_sync(adapter: WebConductorIO) -> None:
    """Build a Conductor with the WebConductorIO adapter and run it.

    Runs in a worker thread (loop.run_in_executor). Any exception is
    logged and suppressed — the conductor_future resolves either way,
    and the calling async context handles WS cleanup.
    """
    try:
        from network_engineer.agents.ai_runtime import AIRuntime
        from network_engineer.agents.conductor import Conductor, ConductorConfig
        from network_engineer.tools.unifi_client import (
            UnifiClient,
            UnifiClientError,
        )
    except ImportError as exc:
        log.error("conductor_import_failed", extra={"error": str(exc)})
        adapter.outbound.put({
            "type": "speak",
            "text": f"Server failed to import Conductor dependencies: {exc}",
        })
        return

    try:
        client = UnifiClient()
    except (UnifiClientError, KeyError, RuntimeError) as exc:
        log.warning("unifi_client_init_failed", extra={"error": str(exc)})
        client = None

    try:
        ai = AIRuntime()
        conductor = Conductor(
            config=ConductorConfig(),
            ai_runtime=ai,
            unifi_client=client,
        )
        conductor.run(
            on_say=adapter.on_say,
            on_user_input=adapter.on_user_input,
            on_status=adapter.on_status,
        )
    except SessionEnded:
        # Operator closed the browser tab. Clean exit; the digest is
        # written by Conductor.run's own finally block.
        log.info("conductor_session_ended_via_ws_disconnect")
    except Exception as exc:
        log.exception("conductor_run_failed",
                      extra={"error_type": exc.__class__.__name__})
        adapter.outbound.put({
            "type": "speak",
            "text": f"Internal error: {exc.__class__.__name__}. The session "
                    "ended; check logs/conductor_debug.jsonl for details.",
        })


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
        ws_max_size=_WS_MAX_SIZE_BYTES,
    )
