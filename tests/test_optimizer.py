"""Tests for the Optimizer agent, config_diff, and rollback helpers."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from network_engineer.tools.optimizer import (
    OptimizerError,
    apply_change,
    rename_device,
    resolve_channel_conflicts,
)
from network_engineer.tools.config_diff import diff_snapshot_files, diff_snapshots
from network_engineer.tools.rollback import (
    device_name_from_snapshot,
    radio_channel_from_snapshot,
)

# ── Mock client ───────────────────────────────────────────────────────────────

class MockClient:
    """A mock UnifiClient that tracks calls and optionally simulates controller updates."""

    def __init__(
        self,
        device_stats: list[dict[str, Any]] | None = None,
        apply_updates: bool = True,
        snapshot_path: Path = Path("/tmp/fake_snapshot.json"),
    ) -> None:
        self._device_stats: list[dict[str, Any]] = device_stats or []
        self._apply_updates = apply_updates
        self._snapshot_path = snapshot_path
        self.calls: list[tuple] = []
        self._consumed_authorization_ids: set[str] = set()

    def snapshot(self) -> Path:
        self.calls.append(("snapshot",))
        return self._snapshot_path

    def get_device_stats(self) -> list[dict[str, Any]]:
        return self._device_stats

    def get_devices(self) -> list[dict[str, Any]]:
        return []

    def get_clients(self) -> list[dict[str, Any]]:
        return []

    def get_wifi_networks(self) -> list[dict[str, Any]]:
        return []

    def get_firewall_rules(self) -> list[dict[str, Any]]:
        return []

    def get_port_forwards(self) -> list[dict[str, Any]]:
        return []

    def get_settings(self) -> list[dict[str, Any]]:
        return []

    def _consume_authorization(
        self, authorization: Any, expected_action: str, expected_payload: dict[str, Any]
    ) -> None:
        """Mirrors UnifiClient._consume_authorization so tests exercise the same
        path. The optimizer mints an ApprovedAction; the mock validates it just
        like the real client would."""
        from network_engineer.tools.authorization import (
            UnauthorizedWriteError,
            canonical_payload_hash,
        )
        if authorization is None:
            raise UnauthorizedWriteError("No authorization provided")
        if authorization.action_name != expected_action:
            raise UnauthorizedWriteError(
                f"action mismatch: {authorization.action_name!r} vs {expected_action!r}"
            )
        if authorization.payload_hash != canonical_payload_hash(
            expected_action, expected_payload
        ):
            raise UnauthorizedWriteError("payload hash mismatch")
        if authorization.is_expired():
            raise UnauthorizedWriteError("expired")
        if authorization.authorization_id in self._consumed_authorization_ids:
            raise UnauthorizedWriteError("replay")
        self._consumed_authorization_ids.add(authorization.authorization_id)

    def set_device_name(self, device_id: str, name: str, *, authorization: Any) -> None:
        self._consume_authorization(
            authorization, "rename_device",
            {"device_id": device_id, "name": name},
        )
        self.calls.append(("set_device_name", device_id, name))
        if self._apply_updates:
            for dev in self._device_stats:
                if dev.get("_id") == device_id:
                    dev["name"] = name

    def set_ap_channel(
        self, device_id: str, radio: str, channel: int | str, *, authorization: Any
    ) -> None:
        action = "set_ap_channel_5ghz" if radio == "na" else "set_ap_channel_2_4ghz"
        self._consume_authorization(
            authorization, action,
            {"device_id": device_id, "radio": radio, "channel": str(channel)},
        )
        self.calls.append(("set_ap_channel", device_id, radio, str(channel)))
        if self._apply_updates:
            for dev in self._device_stats:
                if dev.get("_id") == device_id:
                    for r in dev.get("radio_table", []):
                        if r.get("radio") == radio:
                            r["channel"] = str(channel)

    def set_ap_tx_power(
        self, device_id: str, radio: str, tx_power_mode: str,
        tx_power: int | None = None, *, authorization: Any,
    ) -> None:
        expected_payload: dict[str, Any] = {
            "device_id": device_id, "radio": radio, "tx_power_mode": tx_power_mode,
        }
        if tx_power is not None:
            expected_payload["tx_power"] = tx_power
        self._consume_authorization(authorization, "set_ap_tx_power", expected_payload)
        self.calls.append(("set_ap_tx_power", device_id, radio, tx_power_mode))
        if self._apply_updates:
            for dev in self._device_stats:
                if dev.get("_id") == device_id:
                    for r in dev.get("radio_table", []):
                        if r.get("radio") == radio:
                            r["tx_power_mode"] = tx_power_mode

    def restart_device(self, mac: str, *, authorization: Any) -> None:
        self._consume_authorization(authorization, "restart_offline_ap", {"mac": mac})
        self.calls.append(("restart_device", mac))


def _ap(
    name: str = "AP-1",
    device_id: str = "id-1",
    mac: str = "aa:bb:cc:dd:ee:01",
    channel_5: str = "48",
    channel_24: str = "11",
    tx_power_mode: str = "auto",
) -> dict[str, Any]:
    return {
        "_id": device_id,
        "name": name,
        "mac": mac,
        "radio_table": [
            {"radio": "na", "channel": channel_5, "tx_power_mode": tx_power_mode},
            {"radio": "ng", "channel": channel_24, "tx_power_mode": tx_power_mode},
        ],
        # radio_table_stats holds the actual operating channel (auditor uses this for conflicts)
        "radio_table_stats": [
            {"radio": "na", "channel": int(channel_5) if channel_5.isdigit() else 0},
            {"radio": "ng", "channel": int(channel_24) if channel_24.isdigit() else 0},
        ],
    }


# ── apply_change — happy path ─────────────────────────────────────────────────

def test_rename_device_applied(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(device_stats=[_ap(name="AP-Old", device_id="id-1")], snapshot_path=snap)
    result = apply_change(
        client, "rename_device",
        {"device_id": "id-1", "name": "AP-New", "original_name": "AP-Old"},
        verify_wait_s=0,
    )
    assert result.status == "applied"
    assert result.rolled_back is False
    assert result.snapshot_after is not None


def test_rename_device_calls_set_device_name(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(device_stats=[_ap(name="AP-Old", device_id="id-1")], snapshot_path=snap)
    apply_change(
        client, "rename_device",
        {"device_id": "id-1", "name": "AP-New", "original_name": "AP-Old"},
        verify_wait_s=0,
    )
    write_calls = [c for c in client.calls if c[0] == "set_device_name"]
    assert len(write_calls) == 1
    assert write_calls[0] == ("set_device_name", "id-1", "AP-New")


def test_snapshot_taken_before_and_after(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(device_stats=[_ap(name="AP-Old", device_id="id-1")], snapshot_path=snap)
    apply_change(
        client, "rename_device",
        {"device_id": "id-1", "name": "AP-New", "original_name": "AP-Old"},
        verify_wait_s=0,
    )
    snapshot_calls = [c for c in client.calls if c[0] == "snapshot"]
    assert len(snapshot_calls) == 2  # before and after


def test_set_ap_channel_5ghz_applied(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(device_stats=[_ap(channel_5="48")], snapshot_path=snap)
    result = apply_change(
        client, "set_ap_channel_5ghz",
        {"device_id": "id-1", "channel": 36, "original_channel": "48"},
        verify_wait_s=0,
    )
    assert result.status == "applied"


def test_set_ap_channel_2_4ghz_applied(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(device_stats=[_ap(channel_24="11")], snapshot_path=snap)
    result = apply_change(
        client, "set_ap_channel_2_4ghz",
        {"device_id": "id-1", "channel": 6, "original_channel": "11"},
        verify_wait_s=0,
    )
    assert result.status == "applied"


def test_restart_ap_always_verifies(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(device_stats=[_ap()], snapshot_path=snap)
    result = apply_change(
        client, "restart_offline_ap",
        {"mac": "aa:bb:cc:dd:ee:01"},
        verify_wait_s=0,
    )
    assert result.status == "applied"


# ── apply_change — verify failure + rollback ──────────────────────────────────

def test_verify_failure_triggers_rollback(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(
        device_stats=[_ap(name="AP-Old", device_id="id-1")],
        apply_updates=False,
        snapshot_path=snap,
    )
    result = apply_change(
        client, "rename_device",
        {"device_id": "id-1", "name": "AP-New", "original_name": "AP-Old"},
        verify_wait_s=0,
    )
    assert result.status == "rolled_back"
    assert result.rolled_back is True
    assert result.snapshot_after is None


def test_rollback_restores_original_name(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(
        device_stats=[_ap(name="AP-Old", device_id="id-1")],
        apply_updates=False,
        snapshot_path=snap,
    )
    apply_change(
        client, "rename_device",
        {"device_id": "id-1", "name": "AP-New", "original_name": "AP-Old"},
        verify_wait_s=0,
    )
    rollback_calls = [
        c for c in client.calls
        if c[0] == "set_device_name" and c[2] == "AP-Old"
    ]
    assert rollback_calls, "Expected rollback call restoring original name"


def test_verify_failure_channel_triggers_rollback(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(
        device_stats=[_ap(channel_5="48")],
        apply_updates=False,
        snapshot_path=snap,
    )
    result = apply_change(
        client, "set_ap_channel_5ghz",
        {"device_id": "id-1", "channel": 36, "original_channel": "48"},
        verify_wait_s=0,
    )
    assert result.status == "rolled_back"
    rollback_calls = [c for c in client.calls if c[0] == "set_ap_channel" and c[3] == "48"]
    assert rollback_calls, "Expected rollback call restoring original channel"


# ── Permission gate ───────────────────────────────────────────────────────────

def test_requires_approval_action_raises(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(snapshot_path=snap)
    with pytest.raises(OptimizerError, match="REQUIRES_APPROVAL"):
        apply_change(client, "create_vlan", {}, verify_wait_s=0)


def test_never_action_raises(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(snapshot_path=snap)
    with pytest.raises(OptimizerError):
        apply_change(client, "factory_reset_any_device", {}, verify_wait_s=0)


def test_unknown_action_raises(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    # rename_device is AUTO but params are wrong — will fail at apply/verify
    client = MockClient(device_stats=[_ap()], snapshot_path=snap)
    with pytest.raises(OptimizerError):
        # unlisted action → REQUIRES_APPROVAL (default-deny) → raises
        apply_change(client, "frobnicate_the_network", {}, verify_wait_s=0)


# ── rename_device helper ──────────────────────────────────────────────────────

def test_rename_device_helper_finds_by_name(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(
        device_stats=[_ap(name="FlexHD", device_id="flex-id-1")],
        snapshot_path=snap,
    )
    result = rename_device(client, "FlexHD", "FlexHD-Office")
    assert result.status == "applied"
    write_calls = [c for c in client.calls if c[0] == "set_device_name"]
    assert write_calls[0][2] == "FlexHD-Office"


def test_rename_device_not_found_raises(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(device_stats=[], snapshot_path=snap)
    with pytest.raises(OptimizerError, match="not found"):
        rename_device(client, "Nonexistent-AP", "New-Name")


# ── resolve_channel_conflicts ─────────────────────────────────────────────────

def test_resolve_conflicts_no_conflicts_returns_empty(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(
        device_stats=[
            _ap(name="AP-1", device_id="id-1", channel_5="36", channel_24="1"),
            _ap(name="AP-2", device_id="id-2", mac="aa:00:00:00:00:02",
                channel_5="48", channel_24="6"),
        ],
        snapshot_path=snap,
    )
    results = resolve_channel_conflicts(client)
    assert results == []


def test_resolve_conflicts_fixes_channel_conflict(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    client = MockClient(
        device_stats=[
            _ap(name="AP-Alpha", device_id="id-alpha", channel_5="48"),
            {
                "_id": "id-beta", "name": "AP-Beta", "mac": "aa:00:00:00:00:02",
                "radio_table": [{"radio": "na", "channel": "48", "tx_power_mode": "auto"}],
                "radio_table_stats": [{"radio": "na", "channel": 48}],
            },
        ],
        snapshot_path=snap,
    )
    results = resolve_channel_conflicts(client)
    assert len(results) == 1
    assert results[0].status == "applied"
    channel_calls = [c for c in client.calls if c[0] == "set_ap_channel"]
    assert channel_calls, "Expected a channel change call"
    # The new channel should not be 48
    assert channel_calls[0][3] != "48"


# ── config_diff ───────────────────────────────────────────────────────────────

def test_diff_device_rename(tmp_path: Path) -> None:
    before = {
        "devices": [{"macAddress": "aa:bb:cc:dd:ee:01", "name": "AP-Old", "state": "ONLINE"}],
        "device_stats": [],
    }
    after = {
        "devices": [{"macAddress": "aa:bb:cc:dd:ee:01", "name": "AP-New", "state": "ONLINE"}],
        "device_stats": [],
    }
    result = diff_snapshots(before, after)
    assert "AP-Old" in result
    assert "AP-New" in result
    assert "name" in result


def test_diff_channel_change(tmp_path: Path) -> None:
    before = {
        "devices": [],
        "device_stats": [{
            "mac": "aa:bb:cc:dd:ee:01", "name": "AP-1",
            "radio_table": [{"radio": "na", "channel": "48"}],
        }],
    }
    after = {
        "devices": [],
        "device_stats": [{
            "mac": "aa:bb:cc:dd:ee:01", "name": "AP-1",
            "radio_table": [{"radio": "na", "channel": "36"}],
        }],
    }
    result = diff_snapshots(before, after)
    assert "48" in result
    assert "36" in result
    assert "5GHz" in result


def test_diff_no_changes() -> None:
    snap = {
        "devices": [{"macAddress": "aa:bb:cc:dd:ee:01", "name": "AP-1", "state": "ONLINE"}],
        "device_stats": [],
    }
    result = diff_snapshots(snap, snap)
    assert "No meaningful differences" in result


def test_diff_snapshot_files(tmp_path: Path) -> None:
    import json
    before = {"devices": [{"macAddress": "aa:bb:cc:dd:ee:01", "name": "AP-Old", "state": "ONLINE"}],
              "device_stats": []}
    after = {"devices": [{"macAddress": "aa:bb:cc:dd:ee:01", "name": "AP-New", "state": "ONLINE"}],
             "device_stats": []}
    before_f = tmp_path / "before.json"
    after_f = tmp_path / "after.json"
    before_f.write_text(json.dumps(before))
    after_f.write_text(json.dumps(after))
    result = diff_snapshot_files(before_f, after_f)
    assert "AP-Old" in result
    assert "AP-New" in result


def test_diff_device_added() -> None:
    before = {"devices": [], "device_stats": []}
    after = {
        "devices": [{"macAddress": "aa:bb:cc:dd:ee:01", "name": "New-AP", "state": "ONLINE"}],
        "device_stats": [],
    }
    result = diff_snapshots(before, after)
    assert "New-AP" in result
    assert "added" in result


def test_diff_device_removed() -> None:
    before = {
        "devices": [{"macAddress": "aa:bb:cc:dd:ee:01", "name": "Gone-AP", "state": "ONLINE"}],
        "device_stats": [],
    }
    after = {"devices": [], "device_stats": []}
    result = diff_snapshots(before, after)
    assert "Gone-AP" in result
    assert "removed" in result


# ── rollback helpers ──────────────────────────────────────────────────────────

def test_device_name_from_snapshot() -> None:
    snap = {
        "devices": [
            {"macAddress": "aa:bb:cc:dd:ee:01", "name": "AP-Alpha"},
            {"macAddress": "aa:bb:cc:dd:ee:02", "name": "AP-Beta"},
        ]
    }
    assert device_name_from_snapshot(snap, "aa:bb:cc:dd:ee:01") == "AP-Alpha"
    assert device_name_from_snapshot(snap, "aa:bb:cc:dd:ee:02") == "AP-Beta"
    assert device_name_from_snapshot(snap, "xx:xx:xx:xx:xx:xx") is None


def test_radio_channel_from_snapshot() -> None:
    snap = {
        "device_stats": [{
            "mac": "aa:bb:cc:dd:ee:01",
            "radio_table": [
                {"radio": "na", "channel": "36"},
                {"radio": "ng", "channel": "11"},
            ],
        }]
    }
    assert radio_channel_from_snapshot(snap, "aa:bb:cc:dd:ee:01", "na") == "36"
    assert radio_channel_from_snapshot(snap, "aa:bb:cc:dd:ee:01", "ng") == "11"
    assert radio_channel_from_snapshot(snap, "xx:xx:xx:xx:xx:xx", "na") is None


# ── Live integration ──────────────────────────────────────────────────────────

_LIVE = pytest.mark.skipif(
    not os.getenv("UNIFI_HOST"), reason="UNIFI_HOST not set — live tests require home LAN"
)


@pytest.mark.skipif(
    not os.getenv("UNIFI_HOST") or not os.getenv("NYE_TEST_RENAME_DEVICE"),
    reason="set UNIFI_HOST and NYE_TEST_RENAME_DEVICE=<actual device name> to run",
)
def test_live_rename_and_restore() -> None:
    """Rename a real device and rename it back. Verifies both directions work.

    Set NYE_TEST_RENAME_DEVICE in your shell to the *current* display name of
    a device on the live UDM you are willing to round-trip rename. The test
    skips cleanly when not configured, so it never produces false failures
    from a hardcoded device name.
    """
    from network_engineer.tools.optimizer import rename_device
    from network_engineer.tools.unifi_client import UnifiClient

    client = UnifiClient(use_fixtures=False)
    original = os.environ["NYE_TEST_RENAME_DEVICE"]

    result_rename = rename_device(client, original, original + "-renamed")
    assert result_rename.status == "applied", f"Rename failed: {result_rename.detail}"

    result_restore = rename_device(client, original + "-renamed", original)
    assert result_restore.status == "applied", f"Restore failed: {result_restore.detail}"
