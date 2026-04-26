"""Tests for the write-boundary authorization layer.

Covers ApprovedAction's five bindings and the UnifiClient consumption path:
  1. action_name match
  2. payload_hash match
  3. expiry
  4. tier-vs-permission-model match
  5. single-use replay protection
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from network_engineer.tools.authorization import (
    ApprovedAction,
    UnauthorizedWriteError,
    auto_authorize,
    canonical_payload_hash,
    human_authorize,
)
from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError


# ── canonical_payload_hash ──────────────────────────────────────────────────

def test_payload_hash_is_stable_across_key_order() -> None:
    h1 = canonical_payload_hash("rename_device", {"name": "AP-X", "device_id": "id-1"})
    h2 = canonical_payload_hash("rename_device", {"device_id": "id-1", "name": "AP-X"})
    assert h1 == h2


def test_payload_hash_distinguishes_action() -> None:
    h1 = canonical_payload_hash("rename_device", {"device_id": "id-1", "name": "X"})
    h2 = canonical_payload_hash("set_ap_channel_5ghz", {"device_id": "id-1", "name": "X"})
    assert h1 != h2


def test_payload_hash_distinguishes_payload() -> None:
    h1 = canonical_payload_hash("rename_device", {"device_id": "id-1", "name": "X"})
    h2 = canonical_payload_hash("rename_device", {"device_id": "id-1", "name": "Y"})
    assert h1 != h2


# ── ApprovedAction validators ───────────────────────────────────────────────

def test_auto_authorize_for_known_auto_action() -> None:
    auth = auto_authorize(
        action="rename_device",
        payload={"device_id": "id-1", "name": "AP-New"},
        approved_by="optimizer",
    )
    assert auth.approval_tier == "AUTO"
    assert auth.action_name == "rename_device"
    assert not auth.is_expired()


def test_auto_authorize_rejects_non_auto_action() -> None:
    with pytest.raises(ValueError, match="AUTO-tier"):
        auto_authorize(
            action="delete_port_forward",  # REQUIRES_APPROVAL
            payload={"forward_id": "fwd-1"},
            approved_by="optimizer",
        )


def test_human_authorize_for_requires_approval_action() -> None:
    auth = human_authorize(
        action="delete_port_forward",
        payload={"forward_id": "fwd-1"},
        approved_by="operator",
        source_envelope_id="env-1",
        source_snapshot_id="snap-1",
    )
    assert auth.approval_tier == "REQUIRES_APPROVAL"


def test_human_authorize_rejects_auto_action() -> None:
    with pytest.raises(ValueError, match="REQUIRES_APPROVAL-tier"):
        human_authorize(
            action="rename_device",  # AUTO
            payload={"device_id": "id-1", "name": "AP-X"},
            approved_by="operator",
            source_envelope_id="env-1",
            source_snapshot_id="snap-1",
        )


def test_authorization_for_never_action_rejected() -> None:
    """Constructing an ApprovedAction for a NEVER action must always fail."""
    with pytest.raises(ValidationError, match="forbids"):
        ApprovedAction(
            action_name="factory_reset_any_device",
            payload_hash="x" * 64,
            approval_tier="AUTO",
            approved_by="anyone",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_tier_must_match_permission_model() -> None:
    """Claiming AUTO for a REQUIRES_APPROVAL action is rejected."""
    with pytest.raises(ValidationError, match="does not match"):
        ApprovedAction(
            action_name="delete_port_forward",  # REQUIRES_APPROVAL
            payload_hash="x" * 64,
            approval_tier="AUTO",  # wrong tier
            approved_by="optimizer",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_expires_at_must_be_after_approved_at() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="expires_at"):
        ApprovedAction(
            action_name="rename_device",
            payload_hash="x" * 64,
            approval_tier="AUTO",
            approved_by="optimizer",
            approved_at=now,
            expires_at=now,  # not after
        )


def test_unknown_action_defaults_to_requires_approval() -> None:
    """Unlisted actions default to REQUIRES_APPROVAL — auto_authorize must reject them."""
    with pytest.raises(ValueError, match="AUTO-tier"):
        auto_authorize(
            action="frobnicate_the_network",
            payload={},
            approved_by="optimizer",
        )


def test_is_expired_true_after_ttl() -> None:
    auth = auto_authorize(
        action="rename_device",
        payload={"device_id": "id-1", "name": "X"},
        approved_by="optimizer",
        ttl_seconds=60,
    )
    assert not auth.is_expired()
    assert auth.is_expired(now=auth.expires_at + timedelta(seconds=1))


def test_matches_returns_true_for_same_action_and_payload() -> None:
    payload = {"device_id": "id-1", "name": "AP-X"}
    auth = auto_authorize(action="rename_device", payload=payload, approved_by="optimizer")
    assert auth.matches("rename_device", payload)
    assert not auth.matches("rename_device", {"device_id": "id-1", "name": "AP-Y"})
    assert not auth.matches("set_ap_channel_5ghz", payload)


# ── UnifiClient consumption path (fixture mode) ────────────────────────────

def test_client_rejects_write_without_authorization() -> None:
    """Calling a write method without the required kwarg is a TypeError
    (the kwarg is keyword-only and required)."""
    client = UnifiClient(use_fixtures=True)
    with pytest.raises(TypeError, match="authorization"):
        client.delete_port_forward("fwd-1")  # type: ignore[call-arg]


def test_client_rejects_action_mismatch() -> None:
    """Authorization for action X cannot drive a call to action Y."""
    client = UnifiClient(use_fixtures=True)
    auth = auto_authorize(
        action="rename_device",
        payload={"device_id": "id-1", "name": "AP-X"},
        approved_by="t",
    )
    with pytest.raises(UnauthorizedWriteError, match="rename_device"):
        client.set_ap_channel("id-1", "na", 36, authorization=auth)


def test_client_rejects_payload_mismatch() -> None:
    """Same action but different args must fail the payload hash check."""
    client = UnifiClient(use_fixtures=True)
    auth = auto_authorize(
        action="rename_device",
        payload={"device_id": "id-1", "name": "AP-X"},
        approved_by="t",
    )
    with pytest.raises(UnauthorizedWriteError, match="hash mismatch"):
        client.set_device_name("id-1", "AP-DIFFERENT", authorization=auth)


def test_client_rejects_expired_authorization() -> None:
    client = UnifiClient(use_fixtures=True)
    now = datetime.now(UTC)
    auth = ApprovedAction(
        action_name="rename_device",
        payload_hash=canonical_payload_hash(
            "rename_device", {"device_id": "id-1", "name": "AP-X"},
        ),
        approval_tier="AUTO",
        approved_by="t",
        approved_at=now - timedelta(hours=1),
        expires_at=now - timedelta(minutes=1),
    )
    with pytest.raises(UnauthorizedWriteError, match="expired"):
        client.set_device_name("id-1", "AP-X", authorization=auth)


def test_client_rejects_replay() -> None:
    """A successful (or attempted) consume burns the authorization id."""
    client = UnifiClient(use_fixtures=True)
    auth = auto_authorize(
        action="rename_device",
        payload={"device_id": "id-1", "name": "AP-X"},
        approved_by="t",
    )
    # First call: authorization passes the boundary, then fixture-mode raises
    # in _net_put — but the id is ALREADY consumed (single-use even on failure).
    with pytest.raises(UnifiClientError, match="fixture mode"):
        client.set_device_name("id-1", "AP-X", authorization=auth)
    # Second call with same auth: rejected as replay.
    with pytest.raises(UnauthorizedWriteError, match="already.*consumed"):
        client.set_device_name("id-1", "AP-X", authorization=auth)


def test_consume_atomic_against_failed_transport() -> None:
    """Even if the underlying transport fails, the authorization is consumed
    (no replay window). This is the desired property."""
    client = UnifiClient(use_fixtures=True)
    auth = auto_authorize(
        action="set_ap_channel_5ghz",
        payload={"device_id": "id-1", "radio": "na", "channel": "36"},
        approved_by="t",
    )
    # _get_device_by_id will raise UnifiClientError on the empty fixture, but
    # the auth check happens BEFORE _get_device_by_id, so the id is consumed.
    with pytest.raises(UnifiClientError):
        client.set_ap_channel("id-1", "na", 36, authorization=auth)
    assert auth.authorization_id in client._consumed_authorization_ids


def test_distinct_authorizations_each_one_shot() -> None:
    """Two separate authorizations work independently."""
    client = UnifiClient(use_fixtures=True)
    auth1 = auto_authorize(
        action="rename_device",
        payload={"device_id": "id-1", "name": "AP-X"},
        approved_by="t",
    )
    auth2 = auto_authorize(
        action="rename_device",
        payload={"device_id": "id-1", "name": "AP-X"},
        approved_by="t",
    )
    assert auth1.authorization_id != auth2.authorization_id
    with pytest.raises(UnifiClientError):  # fixture-mode reject downstream
        client.set_device_name("id-1", "AP-X", authorization=auth1)
    with pytest.raises(UnifiClientError):
        client.set_device_name("id-1", "AP-X", authorization=auth2)
    # Both consumed; neither can be replayed.
    with pytest.raises(UnauthorizedWriteError, match="consumed"):
        client.set_device_name("id-1", "AP-X", authorization=auth1)
    with pytest.raises(UnauthorizedWriteError, match="consumed"):
        client.set_device_name("id-1", "AP-X", authorization=auth2)
