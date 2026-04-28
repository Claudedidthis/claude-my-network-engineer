"""ConductorIO — the I/O contract every Conductor renderer satisfies.

Two adapters will exist:

  CLI mode  (existing) — _CliRenderer in agents/conductor.py.
                         Renders to stdout, reads from stdin (paste-safe).
  Web mode  (Stage 2)  — WebConductorIO in ui/web_adapter.py.
                         Bridges to a FastAPI WebSocket; on_user_input
                         blocks on a thread-safe queue fed by the WS
                         handler.

Formalizing the contract as a Protocol means the loop never has to know
which adapter it's running under — it just calls the same callbacks.
The `mode` discriminator lets the loop and the ApprovalGate adjust
behavior where the surface genuinely differs (CLI prints a numeric
code; Web emits a structured action card the UI renders into a
button-driven panel).

Why a Protocol and not an ABC: callers of the loop (tests, programmatic
embedders) often pass plain functions or lambdas for the callbacks; we
don't want to force them to construct a class. The Protocol expresses
the shape; structural typing covers function-level callers via the
`as_io` adapter helper if they want a typed wrapper, or they can just
keep passing callables to run_agent's existing kwargs.

Caveats on `runtime_checkable`
------------------------------

`isinstance(x, ConductorIO)` only verifies member *presence*, not
signatures. An implementation with a wrong-arity `on_status` will
still pass the structural check; the loop will only fail at call
time. Treat the isinstance check as a "did you forget a method?"
guard — not a type-correctness contract. For tighter enforcement,
mypy + the imported Protocol annotation catches mismatches at
typecheck time.
"""
from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable


IOMode = Literal["cli", "web"]


@runtime_checkable
class ConductorIO(Protocol):
    """The shape every Conductor I/O renderer fulfills.

    Implementations MUST provide:
      mode             — "cli" or "web". Lets components that diverge by
                         surface (e.g. ApprovalGate code-vs-button)
                         branch without sniffing the renderer's class.
      on_say(text)     — surface assistant speech to the operator. No
                         return value; the loop continues immediately.
      on_user_input(p) — block the calling thread until the operator
                         provides input. Must be safe to call from the
                         agent loop's main thread (which is where the
                         loop runs). Web adapters block on a queue fed
                         by the WS handler in another task; CLI
                         adapters block on stdin (paste-safe). Returns
                         the operator's text exactly as received.
      on_status(event) — non-text status events (tool_starting,
                         approval_required, etc). Renderers decide
                         whether to ignore (e.g. CLI hides "thinking")
                         or display (e.g. Web pushes the structured
                         event over WS for the panel to render).

    Implementations MAY provide (out-of-band extension; NOT part of the
    structural Protocol check — adapters that need it call it directly,
    typed callers narrow the type themselves):
      submit_approval(action_id, approved) — only meaningful for web
                         adapters. The CLI never calls this; web
                         adapters use it to ferry button-click signals
                         from the browser to the deterministic
                         ApprovalGate.
    """

    mode: IOMode

    def on_say(self, text: str) -> None: ...
    def on_user_input(self, prompt: str) -> str: ...
    def on_status(self, event: dict[str, Any]) -> None: ...
