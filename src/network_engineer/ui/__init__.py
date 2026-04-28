"""Web UI for the Conductor — FastAPI server + browser-side single-page UI.

Stage 1 (this commit): scaffold + placeholder page that proves the WebSocket
plumbing works end-to-end. No Conductor wiring yet.
Stage 2: WebSocket adapter that bridges the Conductor's blocking I/O to the
browser.
Stage 3: structured approval panel for write operations.

Optional dependency: install with `pip install '.[server]'` to pull in
fastapi + uvicorn + websockets. Forks that only use the terminal `nye`
REPL do not need this extra.
"""
