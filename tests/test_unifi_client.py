"""Tests for tools/unifi_client.py.

Fixture mode is always used here — no real UDM required. Live-mode tests that
need network access are skipped automatically when UNIFI_HOST is unset.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from network_engineer.tools.unifi_client import SCHEMA_VERSION, UnifiClient, UnifiClientError

# ---------------------------------------------------------------------------
# Fixture-mode tests (always run)
# ---------------------------------------------------------------------------


def test_fixture_mode_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFI_MODE", "fixtures")
    client = UnifiClient()
    assert client._mode == "fixtures"
    assert client._site_id == "fixture-site"


def test_fixture_mode_kwarg() -> None:
    client = UnifiClient(use_fixtures=True)
    assert client._mode == "fixtures"


def test_fixture_site_id_is_constant() -> None:
    c1 = UnifiClient(use_fixtures=True)
    c2 = UnifiClient(use_fixtures=True)
    assert c1._site_id == c2._site_id == "fixture-site"


def test_fixture_loads_all_keys() -> None:
    client = UnifiClient(use_fixtures=True)
    expected_keys = {
        "devices",
        "clients",
        "networks",
        "wifi_networks",
        "firewall_rules",
        "traffic_rules",
        "traffic_routes",
        "port_forwards",
        "port_profiles",
        "health",
        "alerts",
        "protect_cameras",
        "protect_alerts",
    }
    for key in expected_keys:
        result = getattr(client, f"get_{key}")()
        assert isinstance(result, list), f"get_{key}() should return a list"


def test_fixture_returns_lists() -> None:
    client = UnifiClient(use_fixtures=True)
    assert isinstance(client.get_devices(), list)
    assert isinstance(client.get_clients(), list)
    assert isinstance(client.get_networks(), list)


def test_test_connection_fixture() -> None:
    client = UnifiClient(use_fixtures=True)
    info = client.test_connection()
    assert info["mode"] == "fixtures"
    assert info["site_id"] == "fixture-site"
    assert isinstance(info["device_count"], int)
    assert isinstance(info["client_count"], int)
    assert isinstance(info["network_count"], int)


def test_missing_fixture_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.json"
    client = UnifiClient(use_fixtures=True, fixtures_path=missing)
    with pytest.raises(UnifiClientError, match="Fixture file not found"):
        client.get_devices()


def test_fixture_cache_is_loaded_once(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "captured_at": "2026-04-24T00:00:00",
                "site_id": "fixture-site",
                "devices": [{"id": "abc", "model": "U6-Pro"}],
                "clients": [],
                "networks": [],
                "wifi_networks": [],
                "firewall_rules": [],
                "traffic_rules": [],
                "port_forwards": [],
                "port_profiles": [],
                "health": [],
                "alerts": [],
                "protect_cameras": [],
                "protect_alerts": [],
            }
        )
    )
    client = UnifiClient(use_fixtures=True, fixtures_path=snap)
    d1 = client.get_devices()
    d2 = client.get_devices()
    assert d1 is d2  # same list object — loaded once, then cached


def test_fixture_device_shape(tmp_path: Path) -> None:
    device = {"id": "aabbccddeeff", "model": "U6-Pro", "name": "Living Room AP"}
    snap = tmp_path / "snap.json"
    snap.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "captured_at": "2026-04-24T00:00:00",
                "site_id": "fixture-site",
                "devices": [device],
                "clients": [],
                "networks": [],
                "wifi_networks": [],
                "firewall_rules": [],
                "traffic_rules": [],
                "port_forwards": [],
                "port_profiles": [],
                "health": [],
                "alerts": [],
                "protect_cameras": [],
                "protect_alerts": [],
            }
        )
    )
    client = UnifiClient(use_fixtures=True, fixtures_path=snap)
    devices = client.get_devices()
    assert len(devices) == 1
    assert devices[0]["id"] == "aabbccddeeff"
    assert devices[0]["model"] == "U6-Pro"


def test_snapshot_fixture_mode(tmp_path: Path) -> None:
    snap_data = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": "2026-04-24T00:00:00",
        "site_id": "fixture-site",
        "sites": [],
        "devices": [],
        "clients": [],
        "networks": [],
        "wifi_networks": [],
        "firewall_rules": [],
        "traffic_rules": [],
        "port_forwards": [],
        "port_profiles": [],
        "health": [],
        "alerts": [],
        "protect_cameras": [],
        "protect_alerts": [],
    }
    fixture_file = tmp_path / "snap.json"
    fixture_file.write_text(json.dumps(snap_data))

    client = UnifiClient(use_fixtures=True, fixtures_path=fixture_file)

    import network_engineer.tools.unifi_client as uc_mod

    original = uc_mod._SNAPSHOTS_DIR
    uc_mod._SNAPSHOTS_DIR = tmp_path / "snapshots"
    try:
        out = client.snapshot()
        assert out.exists()
        result = json.loads(out.read_text())
        assert result["schema_version"] == SCHEMA_VERSION
        assert result["site_id"] == "fixture-site"
    finally:
        uc_mod._SNAPSHOTS_DIR = original


# ---------------------------------------------------------------------------
# Live-mode tests (skipped when UNIFI_HOST is not set)
# ---------------------------------------------------------------------------

_LIVE = pytest.mark.skipif(
    not os.getenv("UNIFI_HOST"), reason="UNIFI_HOST not set — live tests require home LAN"
)


@_LIVE
def test_live_site_id_resolved() -> None:
    client = UnifiClient(use_fixtures=False)
    assert client._site_id and client._site_id != "fixture-site"


@_LIVE
def test_live_devices_non_empty() -> None:
    client = UnifiClient(use_fixtures=False)
    devices = client.get_devices()
    assert len(devices) > 0


@_LIVE
def test_live_clients_non_empty() -> None:
    client = UnifiClient(use_fixtures=False)
    clients = client.get_clients()
    assert len(clients) > 0


@_LIVE
def test_live_test_connection() -> None:
    client = UnifiClient(use_fixtures=False)
    info = client.test_connection()
    assert info["mode"] == "live"
    assert info["device_count"] > 0
    assert info["client_count"] > 0
