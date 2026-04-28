"""Unit tests for WebConductorIO — the WebSocket adapter for the Conductor.

These tests pin the adapter's contract independently of FastAPI plumbing:
  • on_say / on_status events land on the outbound queue with the right shape
  • on_user_input blocks until something arrives on inbound
  • disconnect() raises SessionEnded inside on_user_input
  • signal_session_end() posts session_end + drain-stop sentinel in order
"""
from __future__ import annotations

import threading

import pytest

from network_engineer.tools.conductor_io import ConductorIO
from network_engineer.ui.web_adapter import (
    SessionEnded,
    WebConductorIO,
    _END_DRAIN,
)


def test_on_say_enqueues_speak_event() -> None:
    a = WebConductorIO()
    a.on_say("hello world")
    item = a.outbound.get_nowait()
    assert item == {"type": "speak", "text": "hello world"}


def test_on_status_enqueues_status_event_with_discriminator_spread() -> None:
    a = WebConductorIO()
    a.on_status({"event": "tool_starting", "tool": "read_snapshot",
                 "args_keys": []})
    item = a.outbound.get_nowait()
    assert item == {
        "type": "status",
        "event": "tool_starting",
        "tool": "read_snapshot",
        "args_keys": [],
    }


def test_on_user_input_blocks_then_returns_text() -> None:
    a = WebConductorIO()

    received: list[str] = []

    def _consumer() -> None:
        received.append(a.on_user_input("> "))

    t = threading.Thread(target=_consumer, daemon=True)
    t.start()
    # The thread is now blocked on the empty queue.
    a.inbound.put({"type": "user_input", "text": "operator-typed"})
    t.join(timeout=2.0)
    assert not t.is_alive(), "on_user_input did not unblock after inbound put"
    assert received == ["operator-typed"]


def test_disconnect_raises_session_ended_inside_on_user_input() -> None:
    a = WebConductorIO()

    raised: list[Exception] = []

    def _consumer() -> None:
        try:
            a.on_user_input("> ")
        except SessionEnded as e:
            raised.append(e)

    t = threading.Thread(target=_consumer, daemon=True)
    t.start()
    a.disconnect()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert len(raised) == 1


def test_signal_session_end_posts_event_then_drain_sentinel() -> None:
    a = WebConductorIO()
    a.signal_session_end(reason="done")
    first = a.outbound.get_nowait()
    second = a.outbound.get_nowait()
    assert first == {"type": "session_end", "reason": "done"}
    assert second is _END_DRAIN


def test_adapter_satisfies_conductor_io_protocol() -> None:
    """Structural conformance — same Protocol as the CLI renderer."""
    a = WebConductorIO()
    assert isinstance(a, ConductorIO)
    assert a.mode == "web"


def test_non_dict_inbound_returns_empty_string() -> None:
    """Defense against unexpected items on the inbound queue (shouldn't
    happen via the WS handler, which only forwards dicts; this guards
    against future bridges putting other shapes on the queue)."""
    a = WebConductorIO()
    a.inbound.put("a bare string")
    assert a.on_user_input("> ") == ""


def test_dict_without_text_field_returns_empty() -> None:
    a = WebConductorIO()
    a.inbound.put({"type": "user_input"})  # no text field
    assert a.on_user_input("> ") == ""


# ── Stage 3: approval channel ───────────────────────────────────────────────


def test_wait_for_approval_returns_true_on_matching_approve() -> None:
    a = WebConductorIO()
    a.approvals.put({"type": "approve", "action_id": "act-1"})
    assert a.wait_for_approval("act-1") is True


def test_wait_for_approval_returns_false_on_matching_reject() -> None:
    a = WebConductorIO()
    a.approvals.put({"type": "reject", "action_id": "act-1"})
    assert a.wait_for_approval("act-1") is False


def test_wait_for_approval_ignores_stale_action_id_and_keeps_waiting() -> None:
    """Stale id arrives, adapter discards it; real id arrives, adapter
    returns True. Important: the adapter does NOT submit any structured
    answer for stale ids — the loop never sees them."""
    a = WebConductorIO()
    received: list[bool] = []

    def _consumer() -> None:
        received.append(a.wait_for_approval("real"))

    t = threading.Thread(target=_consumer, daemon=True)
    t.start()
    a.approvals.put({"type": "approve", "action_id": "stale"})
    # Brief pause so the consumer thread sees the stale message and
    # discards it before the real one arrives.
    import time
    time.sleep(0.05)
    a.approvals.put({"type": "approve", "action_id": "real"})
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert received == [True]


def test_disconnect_unblocks_wait_for_approval_with_false() -> None:
    a = WebConductorIO()
    received: list[bool] = []

    def _consumer() -> None:
        received.append(a.wait_for_approval("act-1"))

    t = threading.Thread(target=_consumer, daemon=True)
    t.start()
    a.disconnect()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert received == [False]


def test_wait_for_approval_ignores_non_dict_items() -> None:
    """Defense against future bridges putting other shapes on the
    approvals queue (shouldn't happen via the WS handler — it filters
    by type — but it's belt-and-braces here)."""
    a = WebConductorIO()
    a.approvals.put("a bare string")
    a.approvals.put({"type": "approve", "action_id": "act-1"})
    assert a.wait_for_approval("act-1") is True


def test_wait_for_approval_ignores_unknown_kind_keeps_waiting() -> None:
    """An item with an unrecognized type field shouldn't match approve
    or reject — keep waiting."""
    a = WebConductorIO()
    a.approvals.put({"type": "approve_maybe", "action_id": "act-1"})
    a.approvals.put({"type": "reject", "action_id": "act-1"})
    assert a.wait_for_approval("act-1") is False
