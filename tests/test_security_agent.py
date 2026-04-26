"""Tests for the Security Agent."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from network_engineer.agents.ai_runtime import AIRuntime
from network_engineer.agents.security_agent import (
    SecurityTier,
    _build_proposal,
    _looks_like_mac,
    classify_all,
    classify_client,
    propose_vlans,
    render_markdown,
)
from network_engineer.tools.schemas import ApprovalStatus, ChangeReview, Recommendation

# ── classify_client ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Macbook Pro", SecurityTier.TRUST),
        ("iPhone 15", SecurityTier.TRUST),
        ("iPad Air", SecurityTier.TRUST),
        ("Mac Studio", SecurityTier.TRUST),
        ("Lutron light controller", SecurityTier.IOT),
        ("Philips Hue", SecurityTier.IOT),
        ("Zigbee for solar", SecurityTier.IOT),
        ("Xerox WorkCentre", SecurityTier.IOT),
        ("Xbox Series X", SecurityTier.IOT),
        ("Sonos Roam", SecurityTier.IOT),
        ("Indoor camera 1", SecurityTier.CAMERA),
        ("Outdoor camera 1", SecurityTier.CAMERA),
        ("Garage Cam", SecurityTier.CAMERA),
        ("Front Doorbell", SecurityTier.CAMERA),
        ("G4 Pro", SecurityTier.CAMERA),
        ("UP Chime PoE", SecurityTier.CAMERA),
    ],
)
def test_classify_client_matches_known_devices(name: str, expected: SecurityTier) -> None:
    client = {"name": name, "macAddress": "aa:bb:cc:dd:ee:ff"}
    assert classify_client(client) == expected


def test_classify_client_unknown_when_name_is_mac() -> None:
    client = {"name": "b0:b9:8a:00:00:01", "macAddress": "b0:b9:8a:00:00:01"}
    assert classify_client(client) == SecurityTier.UNKNOWN


def test_classify_client_unknown_when_no_name() -> None:
    client = {"macAddress": "aa:bb:cc:dd:ee:ff"}
    assert classify_client(client) == SecurityTier.UNKNOWN


def test_classify_client_uses_hostname_when_name_missing() -> None:
    client = {"hostname": "macbook-pro", "macAddress": "aa:bb:cc:dd:ee:ff"}
    assert classify_client(client) == SecurityTier.TRUST


def test_classify_client_camera_takes_priority_over_iot() -> None:
    # "Hue camera" — hue keyword (IoT) and "camera" (Camera) — Camera should win
    client = {"name": "Hue Outdoor camera", "macAddress": "aa:bb:cc:dd:ee:ff"}
    assert classify_client(client) == SecurityTier.CAMERA


def test_looks_like_mac() -> None:
    assert _looks_like_mac("aa:bb:cc:dd:ee:ff") is True
    assert _looks_like_mac("AA:BB:CC:DD:EE:FF") is True
    assert _looks_like_mac("Lutron light controller") is False
    assert _looks_like_mac("aa:bb:cc:dd") is False


def test_classify_all_buckets_clients_correctly() -> None:
    clients = [
        {"name": "Macbook Pro", "macAddress": "aa:00:00:00:00:01"},
        {"name": "Philips Hue", "macAddress": "aa:00:00:00:00:02"},
        {"name": "Indoor camera 1", "macAddress": "aa:00:00:00:00:03"},
        {"name": "iPad", "macAddress": "aa:00:00:00:00:04"},
        {"name": "aa:00:00:00:00:05", "macAddress": "aa:00:00:00:00:05"},
    ]
    buckets = classify_all(clients)
    assert len(buckets[SecurityTier.TRUST]) == 2
    assert len(buckets[SecurityTier.IOT]) == 1
    assert len(buckets[SecurityTier.CAMERA]) == 1
    assert len(buckets[SecurityTier.UNKNOWN]) == 1


# ── _build_proposal — structure & content ────────────────────────────────────

def _toy_snapshot() -> dict[str, Any]:
    return {
        "networks": [
            {"name": "Default", "vlan": None},
            {"name": "DMZ", "vlan": None},
        ],
        "wifi_networks": [
            {"name": "Home", "security": "wpapsk", "enabled": True},
            {"name": "Guest-Voucher-Net", "security": "open", "is_guest": True, "enabled": True},
        ],
        "clients": [
            {"name": "Macbook Pro", "ipAddress": "192.168.1.50",
             "macAddress": "aa:00:00:00:00:01"},
            {"name": "Philips Hue", "ipAddress": "192.168.1.22",
             "macAddress": "aa:00:00:00:00:02"},
            {"name": "Lutron light controller", "ipAddress": "192.168.1.114",
             "macAddress": "aa:00:00:00:00:03"},
            {"name": "Indoor camera 1", "ipAddress": "192.168.1.210",
             "macAddress": "aa:00:00:00:00:04"},
            {"name": "Front Doorbell", "ipAddress": "192.168.1.211",
             "macAddress": "aa:00:00:00:00:05"},
        ],
        "firewall_rules": [],
    }


def test_proposal_returns_recommendation() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    assert isinstance(rec, Recommendation)


def test_proposal_action_is_propose_segmentation_strategy() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    assert rec.action == "propose_segmentation_strategy"
    assert rec.status == ApprovalStatus.PENDING


def test_proposal_output_kind_is_strategy_only() -> None:
    """The output is policy-only — no concrete CIDRs/VLAN IDs are emitted."""
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    assert rec.proposed_change["output_kind"] == "segmentation_strategy"


def test_proposal_includes_four_tiers() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    tiers = rec.proposed_change["tiers"]
    assert len(tiers) == 4
    names = [t["tier"] for t in tiers]
    assert "Trust" in names
    assert "IoT" in names
    assert "Cameras" in names
    assert "Guest" in names


def test_proposal_cameras_have_no_internet_access() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    cameras = next(t for t in rec.proposed_change["tiers"] if t["tier"] == "Cameras")
    assert cameras["internet_access"] is False


def test_proposal_tiers_have_no_concrete_subnets() -> None:
    """Concrete CIDRs must NEVER appear in the strategy-only proposal —
    they are the topology allocator's job (see task #45)."""
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    blob = json.dumps(rec.proposed_change["tiers"], default=str)
    assert "192.168." not in blob
    assert "10.0.0." not in blob
    assert "<to-allocate>" in blob


def test_proposal_firewall_strategy_uses_tier_names_not_subnets() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    rules = rec.proposed_change["firewall_strategy"]
    assert any(r["src"] == "IoT" and r["dst"] == "Trust" and r["action"] == "drop" for r in rules)
    assert any(r["src"] == "Cameras" and r["dst"] == "WAN" and r["action"] == "drop" for r in rules)
    # No 192.168.10/20/30/40 subnets anywhere
    blob = json.dumps(rules, default=str)
    assert "192.168.10" not in blob
    assert "192.168.20" not in blob
    assert "192.168.30" not in blob
    assert "192.168.40" not in blob


def test_proposal_caveats_section_present() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    assert "topology_caveats" in rec.proposed_change
    assert "policy-only" in rec.proposed_change["topology_caveats"]


def test_proposal_existing_topology_surfaced() -> None:
    """current_state must surface existing VLAN IDs / subnets / gateway IP
    so the operator (or a future allocator) sees the constraints."""
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    topology = rec.current_state["existing_topology"]
    assert "existing_vlan_ids" in topology
    assert "existing_subnets" in topology
    assert "gateway_ip" in topology
    assert "gateway_ip_source" in topology


def test_proposal_does_not_hardcode_controller_ip() -> None:
    """The Cameras→controller rule must reference the *discovered* gateway,
    not a literal 192.168.1.1 default."""
    snap = _toy_snapshot()
    # Toy snapshot's clients are on 192.168.1.x → inferred gateway is 192.168.1.1
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(snap, runtime=runtime)
    rules = rec.proposed_change["firewall_strategy"]
    cam_rule = next(r for r in rules if "controller" in r["name"].lower())
    # The rule's destination is the *discovered* gateway, not a baked-in literal
    assert rec.current_state["existing_topology"]["gateway_ip"] == cam_rule["dst"]
    assert rec.current_state["existing_topology"]["gateway_ip_source"] == "inferred_from_client_ip_prefix"


def test_proposal_emits_gateway_placeholder_when_unknown() -> None:
    """When no clients exist and no gateway device is detected, the rule
    falls back to the symbolic <gateway> placeholder."""
    empty = {
        "networks": [], "wifi_networks": [], "clients": [],
        "devices": [], "firewall_rules": [], "network_config": [],
    }
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(empty, runtime=runtime)
    rules = rec.proposed_change["firewall_strategy"]
    cam_rule = next(r for r in rules if "controller" in r["name"].lower())
    assert cam_rule["dst"] == "<gateway>"


def test_proposal_includes_migration_phases() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    phases = rec.proposed_change["migration_phases"]
    assert len(phases) == 6
    assert all("phase" in p and "action" in p for p in phases)


def test_proposal_classifies_clients_into_buckets() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    breakdown = rec.current_state["client_breakdown"]
    assert "TRUST" in breakdown
    assert "IOT" in breakdown
    assert "CAMERA" in breakdown
    iot_names = [c["name"] for c in breakdown["IOT"]]
    assert "Philips Hue" in iot_names
    assert "Lutron light controller" in iot_names
    camera_names = [c["name"] for c in breakdown["CAMERA"]]
    assert "Indoor camera 1" in camera_names


def test_proposal_has_rationale_risk_and_rollback() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    assert len(rec.rationale) > 100
    assert len(rec.risk) > 50
    assert len(rec.rollback_plan) > 50


def test_proposal_attaches_ai_review() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    review = rec.proposed_change["ai_review"]
    assert review["generated_by"] == "deterministic_fallback"
    assert review["verdict"] == "risky"   # default-deny when AI is off


def test_proposal_runs_without_error_when_log_path_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recommendation log path is fixed in logging_setup; this just smoke-tests
    that propose runs to completion (log handler creation + emit don't raise)."""
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    assert rec.action == "propose_segmentation_strategy"


# ── render_markdown ───────────────────────────────────────────────────────────

def test_render_markdown_contains_key_sections() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    md = render_markdown(rec)
    assert "# " in md     # title
    assert "## Why" in md
    assert "## Current State" in md
    assert "## Proposed Tiers" in md
    assert "## Firewall Strategy" in md
    assert "## Migration Plan" in md
    assert "## Risk" in md
    assert "## Rollback Plan" in md
    assert "## AI Review" in md
    assert "## Topology Caveats" in md


def test_render_markdown_lists_classified_clients() -> None:
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    md = render_markdown(rec)
    assert "Philips Hue" in md
    assert "Indoor camera 1" in md
    assert "Macbook Pro" in md


def test_render_markdown_does_not_emit_concrete_subnets() -> None:
    """Strategy-only output: the rendered markdown shows tier names and
    <to-allocate> placeholders, never concrete CIDRs."""
    runtime = AIRuntime(enabled=False)
    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    md = render_markdown(rec)
    assert "<to-allocate>" in md
    assert "192.168.10.0/24" not in md
    assert "192.168.20.0/24" not in md
    assert "192.168.30.0/24" not in md
    assert "192.168.40.0/24" not in md


# ── Mocked AI review ──────────────────────────────────────────────────────────

class _FakeUsage:
    input_tokens = 1000
    output_tokens = 500
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, response: str) -> None:
        self.response = response
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.last_kwargs = kwargs
        return _FakeMessage(self.response)


class _FakeAnthropic:
    def __init__(self, response: str) -> None:
        self.messages = _FakeMessages(response)


def test_proposal_with_ai_runtime_attaches_real_review() -> None:
    response = json.dumps({
        "verdict": "risky",
        "reasoning": "Multi-phase VLAN rollouts often break mDNS for HomeKit and Sonos.",
        "concerns": ["Lutron Caseta often requires Trust→IoT mDNS reflection"],
        "questions": ["Will Sonos still group across Trust and IoT?"],
        "suggested_alternatives": ["Use UniFi mDNS reflector on the gateway"],
    })
    fake = _FakeAnthropic(response)
    runtime = AIRuntime(enabled=True, client=fake)

    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    review = rec.proposed_change["ai_review"]

    assert review["generated_by"] == "ai"
    assert review["verdict"] == "risky"
    assert "Lutron" in review["concerns"][0]
    # create_vlan in the action triggers Opus escalation
    assert fake.messages.last_kwargs["model"] == "claude-opus-4-7"


def test_proposal_with_ai_runtime_renders_review_in_markdown() -> None:
    response = json.dumps({
        "verdict": "safe", "reasoning": "Phased migration is sound.",
        "concerns": [], "questions": [], "suggested_alternatives": [],
    })
    fake = _FakeAnthropic(response)
    runtime = AIRuntime(enabled=True, client=fake)

    rec = _build_proposal(_toy_snapshot(), runtime=runtime)
    md = render_markdown(rec)
    assert "**Verdict:** `safe`" in md
    assert "Phased migration is sound" in md


def test_review_returns_change_review_type() -> None:
    response = json.dumps({
        "verdict": "safe", "reasoning": "ok",
        "concerns": [], "questions": [], "suggested_alternatives": [],
    })
    fake = _FakeAnthropic(response)
    runtime = AIRuntime(enabled=True, client=fake)
    review = runtime.review_config_change({}, {}, action="create_vlan")
    assert isinstance(review, ChangeReview)
    assert review.verdict == "safe"


# ── Live integration ──────────────────────────────────────────────────────────

_LIVE = pytest.mark.skipif(
    not os.getenv("UNIFI_HOST"), reason="UNIFI_HOST not set — live tests require home LAN"
)


@_LIVE
def test_live_propose_vlans_against_real_unifi() -> None:
    from network_engineer.tools.unifi_client import UnifiClient

    client = UnifiClient(use_fixtures=False)
    rec = propose_vlans(client)

    assert isinstance(rec, Recommendation)
    assert rec.action == "propose_segmentation_strategy"
    assert len(rec.proposed_change["tiers"]) == 4
    breakdown = rec.current_state["client_breakdown"]
    # The home network has cameras and IoT clients — should be detected
    assert "CAMERA" in breakdown
    assert "IOT" in breakdown
    # Live network produces a non-empty existing_topology read
    topology = rec.current_state["existing_topology"]
    assert topology["gateway_ip"] is not None
