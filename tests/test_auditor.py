"""Tests for the Auditor agent.

All tests run against crafted fixture dicts — no real UDM required.
The live-data integration test (bottom) is skipped when UNIFI_HOST is unset.
"""
from __future__ import annotations

import os

import pytest

from network_engineer.agents.auditor import run
from network_engineer.tools.schemas import Finding, Severity

# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_snapshot() -> dict:
    return {
        "devices": [],
        "device_stats": [],
        "clients": [],
        "wifi_networks": [],
        "firewall_rules": [],
        "port_forwards": [],
        "settings": [],
    }


def _guest_portal_settings(auth: str = "hotspot") -> list[dict]:
    return [{"key": "guest_access", "portal_enabled": True, "auth": auth,
             "voucher_enabled": True}]


def _device_stat(name: str, radios: list[dict]) -> dict:
    """Build a minimal device_stats entry."""
    return {
        "name": name,
        "mac": "aa:bb:cc:dd:ee:ff",
        "radio_table": radios,
        "radio_table_stats": [
            {"radio": r["radio"], "channel": r.get("channel_actual", r.get("channel", 0))}
            for r in radios
        ],
    }


# ── WIFI_CHANNEL_CONFLICT ─────────────────────────────────────────────────────

def test_channel_conflict_detected_5ghz() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [
        _device_stat("AP-Alpha", [{"radio": "na", "channel": "auto", "channel_actual": 48}]),
        _device_stat("AP-Beta",  [{"radio": "na", "channel": "48",   "channel_actual": 48}]),
    ]
    findings = run(snap)
    codes = [f.code for f in findings]
    assert "WIFI_CHANNEL_CONFLICT" in codes
    conflict = next(f for f in findings if f.code == "WIFI_CHANNEL_CONFLICT")
    assert "AP-Alpha" in conflict.evidence["access_points"]
    assert "AP-Beta" in conflict.evidence["access_points"]
    assert conflict.evidence["band"] == "5GHz"
    assert conflict.evidence["channel"] == "48"


def test_channel_conflict_detected_24ghz() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [
        _device_stat("AP-1", [{"radio": "ng", "channel": "11", "channel_actual": 11}]),
        _device_stat("AP-2", [{"radio": "ng", "channel": "11", "channel_actual": 11}]),
    ]
    findings = run(snap)
    assert any(f.code == "WIFI_CHANNEL_CONFLICT" and f.evidence["band"] == "2.4GHz"
               for f in findings)


def test_no_conflict_on_different_channels() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [
        _device_stat("AP-1", [{"radio": "na", "channel": "36", "channel_actual": 36}]),
        _device_stat("AP-2", [{"radio": "na", "channel": "48", "channel_actual": 48}]),
        _device_stat("AP-3", [{"radio": "na", "channel": "161","channel_actual": 161}]),
    ]
    findings = run(snap)
    assert not any(f.code == "WIFI_CHANNEL_CONFLICT" for f in findings)


def test_single_ap_no_conflict() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [
        _device_stat("AP-1", [{"radio": "na", "channel": "auto", "channel_actual": 48}]),
    ]
    findings = run(snap)
    assert not any(f.code == "WIFI_CHANNEL_CONFLICT" for f in findings)


# ── WIFI_NO_ENCRYPTION ────────────────────────────────────────────────────────

def test_open_wifi_flagged_critical() -> None:
    snap = _empty_snapshot()
    snap["wifi_networks"] = [{"name": "BadOpen", "security": "open", "enabled": True,
                               "is_guest": False}]
    findings = run(snap)
    assert any(f.code == "WIFI_NO_ENCRYPTION" and f.severity == Severity.CRITICAL
               for f in findings)


def test_open_guest_with_portal_is_info_not_critical() -> None:
    snap = _empty_snapshot()
    snap["wifi_networks"] = [{"name": "Guest-Voucher-Net", "security": "open", "enabled": True,
                               "is_guest": True}]
    snap["settings"] = _guest_portal_settings()
    findings = run(snap)
    # Must NOT be CRITICAL
    assert not any(f.code == "WIFI_NO_ENCRYPTION" for f in findings)
    # Must produce an INFO finding acknowledging the portal
    assert any(f.code == "WIFI_GUEST_PORTAL_OPEN" and f.severity == Severity.INFO
               for f in findings)


def test_dismissed_open_ssid_suppressed_regardless_of_settings() -> None:
    """An open SSID with an active operator dismissal is downgraded to INFO,
    regardless of whether site `settings` are present in the snapshot."""
    from network_engineer.tools.dismissals import DismissalRegistry
    from network_engineer.tools.schemas import Dismissal

    dismissals = DismissalRegistry()
    dismissals.add(Dismissal(
        finding_code="WIFI_NO_ENCRYPTION",
        match_field="ssid",
        match_key="MyVoucherNet",
        reason="Test fixture: voucher portal SSID.",
    ))

    snap = _empty_snapshot()
    snap["wifi_networks"] = [
        {"name": "MyVoucherNet", "security": "open", "enabled": True, "is_guest": True}
    ]
    snap["settings"] = []
    findings = run(snap, dismissals=dismissals)
    assert not any(f.code == "WIFI_NO_ENCRYPTION" for f in findings)
    info = next((f for f in findings if f.code == "WIFI_GUEST_PORTAL_OPEN"), None)
    assert info is not None
    assert info.severity == Severity.INFO
    assert info.evidence.get("source") == "dismissals_registry"
    assert "voucher" in info.evidence.get("reason", "").lower()


def test_open_guest_without_portal_is_still_critical() -> None:
    snap = _empty_snapshot()
    snap["wifi_networks"] = [{"name": "BrokenGuest", "security": "open", "enabled": True,
                               "is_guest": True}]
    snap["settings"] = []  # no portal configured
    findings = run(snap)
    assert any(f.code == "WIFI_NO_ENCRYPTION" and f.severity == Severity.CRITICAL
               for f in findings)


def test_open_wifi_disabled_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["wifi_networks"] = [{"name": "OldGuest", "security": "open", "enabled": False}]
    findings = run(snap)
    assert not any(f.code in ("WIFI_NO_ENCRYPTION", "WIFI_GUEST_PORTAL_OPEN") for f in findings)


def test_wpa2_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["wifi_networks"] = [{"name": "Home", "security": "wpapsk", "enabled": True}]
    findings = run(snap)
    assert not any(f.code == "WIFI_NO_ENCRYPTION" for f in findings)


# ── PORT_FORWARD_SENSITIVE ────────────────────────────────────────────────────

def test_ftp_port_forward_flagged() -> None:
    snap = _empty_snapshot()
    snap["port_forwards"] = [
        {"name": "nas", "dst_port": "21", "fwd": "192.168.1.10", "fwd_port": "21",
         "proto": "tcp", "src": "any", "enabled": True}
    ]
    findings = run(snap)
    assert any(f.code == "PORT_FORWARD_SENSITIVE" and f.severity == Severity.HIGH
               for f in findings)


def test_disabled_port_forward_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["port_forwards"] = [
        {"name": "ssh", "dst_port": "22", "fwd": "192.168.1.10", "fwd_port": "22",
         "proto": "tcp", "src": "any", "enabled": False}
    ]
    findings = run(snap)
    assert not any(f.code == "PORT_FORWARD_SENSITIVE" for f in findings)


def test_source_restricted_port_forward_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["port_forwards"] = [
        {"name": "ssh", "dst_port": "22", "fwd": "192.168.1.10", "fwd_port": "22",
         "proto": "tcp", "src": "1.2.3.4", "enabled": True}
    ]
    findings = run(snap)
    assert not any(f.code == "PORT_FORWARD_SENSITIVE" for f in findings)


def test_non_sensitive_open_port_forward_is_low() -> None:
    snap = _empty_snapshot()
    snap["port_forwards"] = [
        {"name": "game", "dst_port": "27015", "fwd": "192.168.1.10", "fwd_port": "27015",
         "proto": "udp", "src": "any", "enabled": True}
    ]
    findings = run(snap)
    low = [f for f in findings if f.code == "PORT_FORWARD_UNRESTRICTED"]
    assert low
    assert low[0].severity == Severity.LOW


# ── NO_CUSTOM_FIREWALL_RULES ──────────────────────────────────────────────────

def test_no_firewall_rules_flagged() -> None:
    snap = _empty_snapshot()
    findings = run(snap)
    assert any(f.code == "NO_CUSTOM_FIREWALL_RULES" and f.severity == Severity.MEDIUM
               for f in findings)


def test_with_firewall_rules_no_finding() -> None:
    snap = _empty_snapshot()
    snap["firewall_rules"] = [{"_id": "abc", "name": "Block IoT to trusted"}]
    findings = run(snap)
    assert not any(f.code == "NO_CUSTOM_FIREWALL_RULES" for f in findings)


# ── DEVICE_OFFLINE_UNEXPECTED ─────────────────────────────────────────────────

def test_offline_device_flagged() -> None:
    snap = _empty_snapshot()
    snap["devices"] = [{"name": "Switch-1", "model": "US-8", "state": "OFFLINE",
                        "macAddress": "aa:bb:cc:dd:ee:01", "ipAddress": "192.168.1.5"}]
    findings = run(snap)
    assert any(f.code == "DEVICE_OFFLINE_UNEXPECTED" and f.severity == Severity.HIGH
               for f in findings)


def test_dismissed_offline_device_not_flagged() -> None:
    """An offline device with an active dismissal is suppressed. Replaces the
    previous hardcoded `_KNOWN_OFFLINE = {'G4 Pro'}` with the dismissal registry."""
    from network_engineer.tools.dismissals import DismissalRegistry
    from network_engineer.tools.schemas import Dismissal

    dismissals = DismissalRegistry()
    dismissals.add(Dismissal(
        finding_code="DEVICE_OFFLINE_UNEXPECTED",
        match_field="name",
        match_key="Spare Cam",
        reason="Test fixture: backup unit, intentionally powered off.",
    ))

    snap = _empty_snapshot()
    snap["devices"] = [{"name": "Spare Cam", "model": "UVC-G4-PRO", "state": "OFFLINE",
                        "macAddress": "aa:bb:cc:dd:ee:02", "ipAddress": "192.168.1.6"}]
    findings = run(snap, dismissals=dismissals)
    assert not any(f.code == "DEVICE_OFFLINE_UNEXPECTED" for f in findings)


def test_online_device_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["devices"] = [{"name": "AP-1", "model": "U6-LR", "state": "ONLINE",
                        "macAddress": "aa:bb:cc:dd:ee:03", "ipAddress": "192.168.1.7"}]
    findings = run(snap)
    assert not any(f.code == "DEVICE_OFFLINE_UNEXPECTED" for f in findings)


# ── DUPLICATE_CLIENT_IP ───────────────────────────────────────────────────────

def test_duplicate_ip_detected() -> None:
    snap = _empty_snapshot()
    snap["clients"] = [
        {"name": "Client-A", "ipAddress": "192.168.1.50", "macAddress": "aa:00:00:00:00:01"},
        {"name": "Client-B", "ipAddress": "192.168.1.50", "macAddress": "aa:00:00:00:00:02"},
    ]
    findings = run(snap)
    assert any(f.code == "DUPLICATE_CLIENT_IP" for f in findings)
    dup = next(f for f in findings if f.code == "DUPLICATE_CLIENT_IP")
    assert dup.evidence["ip"] == "192.168.1.50"
    assert len(dup.evidence["clients"]) == 2


def test_unique_ips_no_duplicate_finding() -> None:
    snap = _empty_snapshot()
    snap["clients"] = [
        {"name": "Client-A", "ipAddress": "192.168.1.50", "macAddress": "aa:00:00:00:00:01"},
        {"name": "Client-B", "ipAddress": "192.168.1.51", "macAddress": "aa:00:00:00:00:02"},
    ]
    findings = run(snap)
    assert not any(f.code == "DUPLICATE_CLIENT_IP" for f in findings)


# ── AP_CHANNEL_AUTO_5GHZ ──────────────────────────────────────────────────────

def test_auto_5ghz_flagged() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [
        {"name": "FlexHD", "radio_table": [{"radio": "na", "channel": "auto"}],
         "radio_table_stats": []},
    ]
    findings = run(snap)
    assert any(f.code == "AP_CHANNEL_AUTO_5GHZ" for f in findings)


def test_pinned_5ghz_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [
        {"name": "U6-LR", "radio_table": [{"radio": "na", "channel": "149"}],
         "radio_table_stats": []},
    ]
    findings = run(snap)
    assert not any(f.code == "AP_CHANNEL_AUTO_5GHZ" for f in findings)


# ── Ordering and Finding model ────────────────────────────────────────────────

def test_findings_sorted_by_severity() -> None:
    snap = _empty_snapshot()
    snap["wifi_networks"] = [{"name": "Open", "security": "open", "enabled": True}]
    snap["port_forwards"] = [
        {"name": "nas", "dst_port": "21", "fwd": "192.168.1.10", "fwd_port": "21",
         "proto": "tcp", "src": "any", "enabled": True},
    ]
    findings = run(snap)
    severities = [f.severity for f in findings]
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    assert severities == sorted(severities, key=lambda s: order[s])


def test_finding_is_actionable() -> None:
    f = Finding(severity=Severity.HIGH, code="X", title="t", detail="d")
    assert f.is_actionable()

    f2 = Finding(severity=Severity.INFO, code="X", title="t", detail="d")
    assert not f2.is_actionable()


def test_finding_has_timestamp() -> None:
    f = Finding(severity=Severity.LOW, code="X", title="t", detail="d")
    assert f.captured_at is not None


# ── Live integration test ─────────────────────────────────────────────────────

_LIVE = pytest.mark.skipif(
    not os.getenv("UNIFI_HOST"), reason="UNIFI_HOST not set — live tests require home LAN"
)


@_LIVE
def test_live_audit_produces_findings() -> None:
    from network_engineer.agents.auditor import run_from_client
    from network_engineer.tools.unifi_client import UnifiClient

    client = UnifiClient(use_fixtures=False)
    findings = run_from_client(client)

    assert isinstance(findings, list)
    assert len(findings) > 0, "Live network should produce at least one finding"

    # We know the FlexHD/U6 IW ch 48 conflict exists right now
    codes = [f.code for f in findings]
    assert "WIFI_CHANNEL_CONFLICT" in codes, "Expected ch 48 conflict finding on live network"


@_LIVE
def test_live_audit_no_unrationalized_high_severity_port_forwards() -> None:
    """Regression: after the 2026-04-25 cleanup that deleted dead NAS port
    forwards, the live network should have no HIGH-severity sensitive port
    forwards (FTP, SSH, etc.) without an operator origin story.

    Replaces the prior `finds FTP port forward` assertion which was tied to
    pre-cleanup state and went stale once the forwards were removed.
    """
    from network_engineer.agents.auditor import run_from_client
    from network_engineer.tools.unifi_client import UnifiClient

    client = UnifiClient(use_fixtures=False)
    findings = run_from_client(client)
    sensitive_unrationalized = [
        f for f in findings
        if f.code == "PORT_FORWARD_SENSITIVE"
        and not f.evidence.get("rationale")
    ]
    assert not sensitive_unrationalized, (
        "Live network has un-explained HIGH-severity port forwards: "
        + ", ".join(f.evidence.get("name", "?") for f in sensitive_unrationalized)
    )
