"""Tests for the permission model and orchestrator.

Table-driven: covers every action in config/permission_model.yaml, the
default-deny rule for unlisted actions, and the orchestrator's three code paths
(NEVER → PermissionDeniedError, REQUIRES_APPROVAL → ApprovalRequiredError, AUTO → result).
"""
from __future__ import annotations

import pytest

from network_engineer.tools.permissions import Tier, check, is_approved_required, is_auto, is_never

# ---------------------------------------------------------------------------
# Permission model — table-driven against every entry in the YAML
# ---------------------------------------------------------------------------

AUTO_ACTIONS = [
    "rename_device",
    "update_device_description",
    "set_ap_channel_2_4ghz",
    "set_ap_channel_5ghz",
    "set_ap_tx_power",
    "set_band_steering",
    "restart_offline_ap",
    "block_known_client_by_mac",
    "unblock_known_client_by_mac",
    "update_dns_assignment_non_gateway",
    "tag_client_with_known_device_type",
]

REQUIRES_APPROVAL_ACTIONS = [
    "create_vlan",
    "modify_vlan",
    "delete_vlan",
    "create_firewall_rule",
    "modify_firewall_rule",
    "delete_firewall_rule",
    "create_port_forward",
    "modify_port_forward",
    "delete_port_forward",
    "change_default_gateway",
    "change_routing",
    "create_wifi_network",
    "modify_wifi_network",
    "delete_wifi_network",
    "dhcp_scope_change",
    "ip_assignment_change",
    "switch_port_profile_change",
    "any_change_affecting_protect_cameras",
    "firmware_update_any_device",
    "admin_account_change",
    "api_key_change",
    "any_change_to_udm_itself",
    "enable_network_segment",
    "disable_network_segment",
    "ids_ips_rule_change",
    "rollback_from_snapshot",
]

NEVER_ACTIONS = [
    "delete_device_from_controller",
    "factory_reset_any_device",
    "disable_wan",
    "expose_management_to_internet",
    "store_credentials_plaintext",
    "modify_env_file",
    "push_change_without_snapshot",
    "apply_more_than_one_change_per_operation",
    "disable_firewall_entirely",
    "create_open_wifi_network",
]

UNLISTED_ACTIONS = [
    "do_something_unknown",
    "totally_new_action",
    "reboot_everything",
    "",
    "   ",
]


@pytest.mark.parametrize("action", AUTO_ACTIONS)
def test_auto_actions(action: str) -> None:
    assert check(action) is Tier.AUTO
    assert is_auto(action)
    assert not is_approved_required(action)
    assert not is_never(action)


@pytest.mark.parametrize("action", REQUIRES_APPROVAL_ACTIONS)
def test_requires_approval_actions(action: str) -> None:
    assert check(action) is Tier.REQUIRES_APPROVAL
    assert is_approved_required(action)
    assert not is_auto(action)
    assert not is_never(action)


@pytest.mark.parametrize("action", NEVER_ACTIONS)
def test_never_actions(action: str) -> None:
    assert check(action) is Tier.NEVER
    assert is_never(action)
    assert not is_auto(action)
    assert not is_approved_required(action)


@pytest.mark.parametrize("action", UNLISTED_ACTIONS)
def test_unlisted_actions_default_to_requires_approval(action: str) -> None:
    """Unknown actions must never silently become AUTO."""
    assert check(action) is Tier.REQUIRES_APPROVAL


def test_tier_enum_values() -> None:
    assert Tier.AUTO.value == "AUTO"
    assert Tier.REQUIRES_APPROVAL.value == "REQUIRES_APPROVAL"
    assert Tier.NEVER.value == "NEVER"


def test_tier_is_string_comparable() -> None:
    assert Tier.AUTO == "AUTO"
    assert Tier.NEVER == "NEVER"
    assert Tier.REQUIRES_APPROVAL == "REQUIRES_APPROVAL"


# ---------------------------------------------------------------------------
# Orchestrator — three code paths
# ---------------------------------------------------------------------------

from network_engineer.agents.orchestrator import (  # noqa: E402
    ApprovalRequiredError,
    PermissionDeniedError,
    TaskResult,
    run,
    run_approved,
)


def test_orchestrator_never_raises_permission_denied() -> None:
    task = {"action": "delete_device_from_controller", "params": {}}
    with pytest.raises(PermissionDeniedError, match="NEVER tier"):
        run(task)


def test_orchestrator_never_cannot_be_overridden_by_approval() -> None:
    task = {"action": "factory_reset_any_device", "params": {}}
    with pytest.raises(PermissionDeniedError):
        run_approved(task)


def test_orchestrator_requires_approval_raises_and_logs(tmp_path: pytest.TempPathFactory) -> None:
    task = {
        "action": "create_vlan",
        "params": {"vlan_id": 99, "name": "test"},
        "agent": "test_agent",
        "rationale": "Need a new IoT VLAN",
        "rollback_plan": "delete_vlan",
    }
    with pytest.raises(ApprovalRequiredError) as exc_info:
        run(task)
    err = exc_info.value
    assert err.action == "create_vlan"
    assert err.proposal["params"]["vlan_id"] == 99
    assert err.proposal["requested_by"] == "test_agent"
    assert err.proposal["rationale"] == "Need a new IoT VLAN"


def test_orchestrator_auto_returns_result() -> None:
    task = {"action": "rename_device", "params": {"id": "abc", "name": "New Name"}}
    result = run(task)
    assert isinstance(result, TaskResult)
    assert result.action == "rename_device"
    assert result.tier is Tier.AUTO
    assert result.status == "stubbed"


def test_orchestrator_auto_includes_timestamp() -> None:
    task = {"action": "set_ap_tx_power", "params": {"id": "abc", "power": 20}}
    result = run(task)
    assert result.timestamp  # non-empty ISO string


def test_orchestrator_run_approved_skips_approval_gate() -> None:
    task = {
        "action": "create_vlan",
        "params": {"vlan_id": 50},
        "agent": "orchestrator",
    }
    result = run_approved(task)
    assert result.action == "create_vlan"
    assert result.tier is Tier.REQUIRES_APPROVAL
    assert result.status == "stubbed"


def test_orchestrator_missing_action_raises_value_error() -> None:
    with pytest.raises(ValueError, match="action"):
        run({})


def test_orchestrator_unlisted_action_requires_approval() -> None:
    task = {"action": "do_something_totally_new", "params": {}}
    with pytest.raises(ApprovalRequiredError):
        run(task)


def test_approval_required_message_contains_action() -> None:
    task = {"action": "modify_vlan", "params": {}}
    with pytest.raises(ApprovalRequiredError, match="modify_vlan"):
        run(task)
