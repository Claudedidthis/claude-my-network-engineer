"""Write-boundary authorization — every UnifiClient mutation requires an ApprovedAction.

Defense in depth on top of the orchestrator's permissions.check() gate. Even if a
caller bypasses the orchestrator and reaches into UnifiClient's public write
methods directly, the call refuses without a fresh, valid ApprovedAction whose
five bindings all match the call:

    1. action_name        — must match the permission_model entry the method represents
    2. payload_hash       — sha256 of the canonicalised call args
    3. expires_at         — short window; expired authorizations are rejected
    4. approval_tier      — must match permissions.check(action_name)
    5. authorization_id   — single-use; the client tracks consumed IDs and rejects replay

Provenance fields (approved_by, source_envelope_id, source_snapshot_id) ride along
for the audit log but do not gate execution.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from network_engineer.tools.permissions import Tier, check


class UnauthorizedWriteError(RuntimeError):
    """Raised when a write is attempted without a valid, matching, unexpired,
    single-use ApprovedAction."""


def canonical_payload_hash(action: str, payload: dict[str, Any]) -> str:
    """SHA-256 over a canonicalised (action, payload) tuple.

    Stable across processes: keys sorted, no whitespace, datetimes/Paths
    coerced via str(). Both the authorization minter and the client
    consumer must derive the *same* shape from their inputs — see the
    payload-projection comments on each client write method.
    """
    canonical = json.dumps(
        {"action": action, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovedAction(BaseModel):
    """Single-use, time-bounded authorization for one specific write."""

    authorization_id: str = Field(default_factory=lambda: f"auth-{uuid4()}")
    action_name: str
    payload_hash: str
    approval_tier: Literal["AUTO", "REQUIRES_APPROVAL"]
    approved_by: str
    approved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    source_envelope_id: str | None = None
    source_snapshot_id: str | None = None

    @model_validator(mode="after")
    def _tier_matches_permission_model(self) -> Self:
        """The claimed tier must match what permissions.check() returns.

        NEVER-tier actions cannot be authorized at all. Mismatched tiers
        (e.g. claiming AUTO for a REQUIRES_APPROVAL action) are rejected.
        """
        actual = check(self.action_name)
        if actual is Tier.NEVER:
            raise ValueError(
                f"Cannot authorize {self.action_name!r}: permission model "
                f"forbids it (tier=NEVER)."
            )
        if actual.value != self.approval_tier:
            raise ValueError(
                f"approval_tier={self.approval_tier!r} does not match "
                f"permission model tier {actual.value!r} for action "
                f"{self.action_name!r}."
            )
        return self

    @model_validator(mode="after")
    def _expiry_in_future(self) -> Self:
        if self.expires_at <= self.approved_at:
            raise ValueError("expires_at must be after approved_at")
        return self

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at

    def matches(self, action: str, payload: dict[str, Any]) -> bool:
        return (
            self.action_name == action
            and self.payload_hash == canonical_payload_hash(action, payload)
        )


def auto_authorize(
    *,
    action: str,
    payload: dict[str, Any],
    approved_by: str,
    source_envelope_id: str | None = None,
    source_snapshot_id: str | None = None,
    ttl_seconds: int = 300,
) -> ApprovedAction:
    """Mint an AUTO-tier authorization. Raises ValueError if action isn't AUTO."""
    tier = check(action)
    if tier is not Tier.AUTO:
        raise ValueError(
            f"auto_authorize requires an AUTO-tier action; "
            f"{action!r} is {tier.value}."
        )
    now = datetime.now(UTC)
    return ApprovedAction(
        action_name=action,
        payload_hash=canonical_payload_hash(action, payload),
        approval_tier="AUTO",
        approved_by=approved_by,
        approved_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        source_envelope_id=source_envelope_id,
        source_snapshot_id=source_snapshot_id,
    )


def human_authorize(
    *,
    action: str,
    payload: dict[str, Any],
    approved_by: str,
    source_envelope_id: str,
    source_snapshot_id: str,
    ttl_seconds: int = 600,
) -> ApprovedAction:
    """Mint a REQUIRES_APPROVAL-tier authorization (after explicit human consent)."""
    tier = check(action)
    if tier is not Tier.REQUIRES_APPROVAL:
        raise ValueError(
            f"human_authorize requires a REQUIRES_APPROVAL-tier action; "
            f"{action!r} is {tier.value}."
        )
    now = datetime.now(UTC)
    return ApprovedAction(
        action_name=action,
        payload_hash=canonical_payload_hash(action, payload),
        approval_tier="REQUIRES_APPROVAL",
        approved_by=approved_by,
        approved_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        source_envelope_id=source_envelope_id,
        source_snapshot_id=source_snapshot_id,
    )
