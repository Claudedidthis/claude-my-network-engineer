"""Deterministic approval gate for write operations.

Why this exists
---------------
A live runaway session on 2026-04-27 (paste-fed input loop) showed how
trivially a prompt-injection / accidental-input scenario could induce the
model to emit "✅ Approval logged" speak text. That speak was theater —
no write tool was wired — but if a write *had* been wired, an attacker
could plausibly synthesize approval through the LLM's interpretation of
ambiguous input.

The fix: take approval decisions OUT of the LLM. The model does NOT
decide whether the operator approved. Deterministic Python does. The
model can announce intent, can describe the change, can emit the write
tool_use — but the loop only executes the write if a fresh,
operator-typed numeric code matches the one the gate generated and the
operator can see in front of them.

Threat model
------------
Defends against:
  • Prompt injection of approval phrases ("yes", "approve", "looks good")
    embedded in operator messages, tool outputs, durable memory, etc.
  • Paste-buffer input where pasted content contains ambiguous "approve"
    strings.
  • Model hallucination of approval ("That reads as your approval.")

Does NOT defend against:
  • Operator with a controller GUI open and the typed code in plaintext
    on screen — by design, the operator IS the trust anchor.
  • Race conditions in a shared terminal — the gate has a TTL but assumes
    a single-operator interactive flow.

Design
------
  ApprovalGate.request(action_id, description) → numeric code (string)
    Generates a uniformly-random N-digit code (default 3, configurable
    up the stack). Stores a PendingApproval keyed by action_id with the
    code, a TTL, and the original description.

  ApprovalGate.submit(typed) → ApprovalResult
    Compares operator-typed string against the most recent pending
    approval's code. Equality is byte-strict after stripping whitespace —
    NO substring matching, NO case folding beyond strip. On match, marks
    the pending approval as `satisfied`. On any mismatch, the pending
    approval is CANCELLED — operators don't get to retry, they get to
    start over (resists guessing).

  ApprovalGate.consume(action_id) → bool
    Called by the loop right before executing a gated tool. Returns True
    iff there's a satisfied, non-expired pending approval for that exact
    action_id; on success it ALSO clears the gate so the same approval
    can't be reused for a second write.

The model never sees code generation or matching logic — only the
description of what's pending. The operator sees the code (printed by
the CLI, not by the model), types it back, and the CLI compares.

Why a singleton: the gate is loop-scoped state. We pass the gate
instance into both the loop and the CLI renderer so they share it.
Plain instance variable, no module-level globals.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Literal


# Default code length. 3 digits = 1000 combinations; with one-strike
# cancellation that's effectively un-guessable in a single attempt. Bump
# to 4-6 for higher-stakes operations if needed.
_DEFAULT_CODE_DIGITS = 3
_DEFAULT_TTL_SECONDS = 120


@dataclass
class PendingApproval:
    action_id: str
    description: str
    code: str
    created_at: float
    expires_at: float
    state: Literal["pending", "satisfied", "cancelled", "expired"] = "pending"

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) > self.expires_at


@dataclass
class ApprovalResult:
    """Outcome of an operator-typed input checked against the gate."""

    matched: bool
    action_id: str | None = None
    description: str | None = None
    reason: str = ""  # short human-readable explanation when matched=False


@dataclass
class ApprovalGate:
    """Deterministic write-approval gate. One loop, one operator.

    Holds at most ONE pending approval at a time — requesting a new one
    while another is pending cancels the old one. This keeps the operator
    UX simple: there's always exactly one thing being approved.
    """

    code_digits: int = _DEFAULT_CODE_DIGITS
    default_ttl_seconds: int = _DEFAULT_TTL_SECONDS
    _pending: PendingApproval | None = field(default=None, init=False)
    _last_consumed_action_id: str | None = field(default=None, init=False)

    def request(
        self,
        *,
        action_id: str,
        description: str,
        ttl_seconds: int | None = None,
    ) -> PendingApproval:
        """Generate a fresh code and register the pending approval. Cancels
        any previous still-pending approval (only one in flight at a time)."""
        if self._pending is not None and self._pending.state == "pending":
            self._pending.state = "cancelled"
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        now = time.monotonic()
        # secrets.randbelow gives uniform randomness; format with leading zeros.
        upper = 10 ** self.code_digits
        code_int = secrets.randbelow(upper)
        code = str(code_int).zfill(self.code_digits)
        self._pending = PendingApproval(
            action_id=action_id,
            description=description,
            code=code,
            created_at=now,
            expires_at=now + ttl,
        )
        return self._pending

    def submit(self, typed: str) -> ApprovalResult:
        """Check operator-typed input against the active pending code.

        Equality is byte-strict after stripping whitespace. Anything other
        than an exact match cancels the pending approval — operators do
        not get retries (resists slow-guessing under the TTL window).
        """
        pending = self._pending
        if pending is None:
            return ApprovalResult(matched=False, reason="no approval pending")
        if pending.state != "pending":
            return ApprovalResult(
                matched=False,
                reason=f"approval is in state {pending.state!r}, not pending",
            )
        if pending.is_expired():
            pending.state = "expired"
            return ApprovalResult(
                matched=False,
                action_id=pending.action_id,
                description=pending.description,
                reason="approval window expired",
            )
        cleaned = (typed or "").strip()
        if cleaned == pending.code:
            pending.state = "satisfied"
            return ApprovalResult(
                matched=True,
                action_id=pending.action_id,
                description=pending.description,
            )
        pending.state = "cancelled"
        return ApprovalResult(
            matched=False,
            action_id=pending.action_id,
            description=pending.description,
            reason="code did not match — approval cancelled",
        )

    def consume(self, action_id: str) -> bool:
        """Atomically check + clear: returns True iff a satisfied, non-expired
        approval exists for exactly this action_id, and clears the gate.

        Called by the loop immediately before executing a gated tool. After
        consume() returns True, the approval is gone — a second write needs
        a second approval cycle.
        """
        pending = self._pending
        if pending is None:
            return False
        if pending.action_id != action_id:
            return False
        if pending.state != "satisfied":
            return False
        if pending.is_expired():
            pending.state = "expired"
            return False
        # Success — record and clear.
        self._last_consumed_action_id = pending.action_id
        self._pending = None
        return True

    def cancel(self) -> None:
        """Operator-side cancel (e.g. typed 'no')."""
        if self._pending is not None and self._pending.state == "pending":
            self._pending.state = "cancelled"

    @property
    def has_pending(self) -> bool:
        return self._pending is not None and self._pending.state == "pending"

    @property
    def pending(self) -> PendingApproval | None:
        return self._pending
