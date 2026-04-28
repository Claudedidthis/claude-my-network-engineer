"""Unit tests for ApprovalGate.

Verifies the deterministic guarantees that the gate is supposed to provide:
  • Generated code length matches code_digits (zero-padded)
  • Equality is byte-strict after stripping whitespace — no substring
    match, no case folding, no inference
  • Wrong submission cancels the pending approval (no slow-guessing)
  • consume() requires action_id match AND state=satisfied AND not expired
  • Requesting a new approval cancels the previous one (only one in flight)
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from network_engineer.tools.approval_gate import ApprovalGate


def test_generated_code_has_right_length_and_is_zero_padded() -> None:
    gate = ApprovalGate(code_digits=4)
    with patch(
        "network_engineer.tools.approval_gate.secrets.randbelow",
        return_value=42,
    ):
        pending = gate.request(action_id="a1", description="test")
    assert pending.code == "0042"
    assert len(pending.code) == 4


def test_correct_code_satisfies_then_consume_clears_gate() -> None:
    gate = ApprovalGate(code_digits=3)
    with patch(
        "network_engineer.tools.approval_gate.secrets.randbelow",
        return_value=472,
    ):
        gate.request(action_id="apply-vlan-20", description="create VLAN 20")
    result = gate.submit("472")
    assert result.matched is True
    assert result.action_id == "apply-vlan-20"
    assert gate.consume("apply-vlan-20") is True
    # After consume, gate is cleared — second consume returns False.
    assert gate.consume("apply-vlan-20") is False


def test_consume_refuses_mismatched_action_id() -> None:
    """Even with a satisfied approval, a write tool with a DIFFERENT
    action_id must not be authorized — defense against the model swapping
    a write at the last moment."""
    gate = ApprovalGate(code_digits=3)
    with patch(
        "network_engineer.tools.approval_gate.secrets.randbelow",
        return_value=472,
    ):
        gate.request(action_id="apply-vlan-20", description="create VLAN 20")
    gate.submit("472")  # satisfied
    assert gate.consume("apply-different-thing") is False
    # And the original approval is still satisfied for the right id...
    assert gate.consume("apply-vlan-20") is True


def test_wrong_code_cancels_pending_no_retry() -> None:
    gate = ApprovalGate(code_digits=3)
    with patch(
        "network_engineer.tools.approval_gate.secrets.randbelow",
        return_value=472,
    ):
        gate.request(action_id="a1", description="test")
    bad = gate.submit("999")
    assert bad.matched is False
    assert "cancelled" in bad.reason
    # Even if the operator now types the right code, it's gone.
    second = gate.submit("472")
    assert second.matched is False
    assert "not pending" in second.reason


def test_substring_does_not_match() -> None:
    """Defense against an operator pasting a blob that contains the code
    embedded in other text. Equality is byte-strict; substrings fail."""
    gate = ApprovalGate(code_digits=3)
    with patch(
        "network_engineer.tools.approval_gate.secrets.randbelow",
        return_value=472,
    ):
        gate.request(action_id="a1", description="test")
    result = gate.submit("type 472 to approve")
    assert result.matched is False


def test_whitespace_around_code_is_tolerated() -> None:
    """Operators commonly hit space + Enter, or have a trailing newline
    from a paste. Strip leading/trailing whitespace only."""
    gate = ApprovalGate(code_digits=3)
    with patch(
        "network_engineer.tools.approval_gate.secrets.randbelow",
        return_value=472,
    ):
        gate.request(action_id="a1", description="test")
    result = gate.submit("  472  ")
    assert result.matched is True


def test_expired_approval_does_not_match() -> None:
    gate = ApprovalGate(code_digits=3, default_ttl_seconds=120)
    with patch(
        "network_engineer.tools.approval_gate.secrets.randbelow",
        return_value=472,
    ):
        pending = gate.request(action_id="a1", description="test")
    # Force the pending to look expired.
    pending.expires_at = time.monotonic() - 1
    result = gate.submit("472")
    assert result.matched is False
    assert "expired" in result.reason


def test_new_request_cancels_previous() -> None:
    """Requesting a fresh approval invalidates any in-flight one. Operators
    only ever face one pending challenge at a time."""
    gate = ApprovalGate(code_digits=3)
    with patch(
        "network_engineer.tools.approval_gate.secrets.randbelow",
        side_effect=[100, 200],
    ):
        first = gate.request(action_id="a1", description="first")
        second = gate.request(action_id="a2", description="second")
    assert first.state == "cancelled"
    assert second.state == "pending"
    # The new code matches for the new id; the old code never gets a
    # chance because the old approval was cancelled when the new one
    # superseded it.
    assert gate.submit("200").matched is True
    assert gate.consume("a1") is False
    assert gate.consume("a2") is True


def test_submit_with_no_pending_returns_clean_failure() -> None:
    gate = ApprovalGate(code_digits=3)
    result = gate.submit("anything")
    assert result.matched is False
    assert "no approval pending" in result.reason


def test_submit_via_ui_with_matching_action_id_satisfies() -> None:
    """Web-mode happy path: a button click whose action_id matches the
    pending approval satisfies the gate. The CODE is irrelevant — the
    structurally-unguessable action_id + the operator's same-origin
    button click together prove presence."""
    gate = ApprovalGate(code_digits=3, mode="web")
    gate.request(action_id="apply-1", description="apply change")
    result = gate.submit_via_ui("apply-1")
    assert result.matched is True
    assert result.action_id == "apply-1"
    assert gate.consume("apply-1") is True


def test_submit_via_ui_with_wrong_action_id_cancels() -> None:
    """A click carrying a stale or fabricated action_id must NOT
    satisfy the gate, AND must cancel the pending approval — defense
    in depth against a buggy client sending the wrong id."""
    gate = ApprovalGate(code_digits=3, mode="web")
    gate.request(action_id="apply-real", description="real")
    result = gate.submit_via_ui("apply-stale")
    assert result.matched is False
    assert "did not match" in result.reason
    # And the pending is now cancelled — even a follow-up correct-id
    # click cannot satisfy.
    second = gate.submit_via_ui("apply-real")
    assert second.matched is False
    assert "not pending" in second.reason


def test_submit_via_ui_with_expired_approval_refuses() -> None:
    gate = ApprovalGate(code_digits=3, mode="web", default_ttl_seconds=120)
    pending = gate.request(action_id="apply-1", description="apply")
    pending.expires_at = time.monotonic() - 1
    result = gate.submit_via_ui("apply-1")
    assert result.matched is False
    assert "expired" in result.reason


def test_submit_via_ui_with_no_pending_returns_clean_failure() -> None:
    gate = ApprovalGate(code_digits=3, mode="web")
    result = gate.submit_via_ui("anything")
    assert result.matched is False
    assert "no approval pending" in result.reason


def test_gate_mode_attribute_default_and_explicit() -> None:
    """Default mode is cli; explicit web mode is stored verbatim."""
    assert ApprovalGate().mode == "cli"
    assert ApprovalGate(mode="web").mode == "web"


def test_cancel_voids_a_pending_approval() -> None:
    gate = ApprovalGate(code_digits=3)
    with patch(
        "network_engineer.tools.approval_gate.secrets.randbelow",
        return_value=472,
    ):
        gate.request(action_id="a1", description="test")
    gate.cancel()
    assert not gate.has_pending
    assert gate.submit("472").matched is False
