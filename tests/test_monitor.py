"""Tests for the Monitor agent."""
from __future__ import annotations

import os

import pytest

from network_engineer.agents.monitor import run
from network_engineer.tools.schemas import NetworkEvent, Severity

# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_snapshot() -> dict:
    return {
        "health": [],
        "devices": [],
        "device_stats": [],
        "client_stats": [],
    }


def _www_health(latency: int = 5, drops: int = 0, dns_success_rate: float | None = None) -> dict:
    entry: dict = {"subsystem": "www", "status": "ok", "latency": latency, "drops": drops}
    if dns_success_rate is not None:
        entry["dns_success_rate"] = dns_success_rate
    return entry


def _radio_stats(
    name: str = "AP-1",
    band: str = "na",
    channel: int = 36,
    tx_packets: int = 1000,
    tx_retries: int = 100,
    satisfaction: int = 80,
) -> dict:
    return {
        "name": name,
        "radio_table_stats": [
            {
                "radio": band,
                "channel": channel,
                "tx_packets": tx_packets,
                "tx_retries": tx_retries,
                "satisfaction": satisfaction,
            }
        ],
    }


def _wireless_client(signal: int = -60, hostname: str = "laptop") -> dict:
    return {"hostname": hostname, "ip": "192.168.1.50", "signal": signal}


def _wired_client() -> dict:
    return {"hostname": "nas", "ip": "192.168.1.10"}


def _device(name: str = "Switch-1", state: str = "ONLINE", model: str = "US-8") -> dict:
    return {"name": name, "state": state, "model": model, "ipAddress": "192.168.1.5",
            "macAddress": "aa:bb:cc:dd:ee:ff"}


# ── All clear ─────────────────────────────────────────────────────────────────

def test_all_clear_returns_empty() -> None:
    snap = _empty_snapshot()
    events = run(snap)
    assert events == []


# ── WAN latency ───────────────────────────────────────────────────────────────

def test_wan_latency_warning() -> None:
    snap = _empty_snapshot()
    snap["health"] = [_www_health(latency=60)]
    events = run(snap)
    codes = [e.event_type for e in events]
    assert "WAN_LATENCY_HIGH" in codes
    evt = next(e for e in events if e.event_type == "WAN_LATENCY_HIGH")
    assert evt.severity == Severity.HIGH


def test_wan_latency_critical() -> None:
    snap = _empty_snapshot()
    snap["health"] = [_www_health(latency=120)]
    events = run(snap)
    evt = next(e for e in events if e.event_type == "WAN_LATENCY_HIGH")
    assert evt.severity == Severity.CRITICAL


def test_wan_latency_ok_no_event() -> None:
    snap = _empty_snapshot()
    snap["health"] = [_www_health(latency=20)]
    events = run(snap)
    assert not any(e.event_type == "WAN_LATENCY_HIGH" for e in events)


def test_wan_drops_flagged() -> None:
    snap = _empty_snapshot()
    snap["health"] = [_www_health(drops=3)]
    events = run(snap)
    assert any(e.event_type == "WAN_DROPS" for e in events)


def test_wan_drops_zero_no_event() -> None:
    snap = _empty_snapshot()
    snap["health"] = [_www_health(drops=0)]
    events = run(snap)
    assert not any(e.event_type == "WAN_DROPS" for e in events)


def test_non_www_subsystem_ignored() -> None:
    snap = _empty_snapshot()
    snap["health"] = [{"subsystem": "dns", "status": "ok", "latency": 500}]
    events = run(snap)
    assert not any(e.event_type == "WAN_LATENCY_HIGH" for e in events)


# ── DNS success rate ───────────────────────────────────────────────────────────

def test_dns_success_rate_low_generates_warning() -> None:
    """93% DNS success rate (below 95% threshold) must produce a HIGH event."""
    snap = _empty_snapshot()
    snap["health"] = [_www_health(dns_success_rate=0.93)]
    events = run(snap)
    dns_events = [e for e in events if e.event_type == "DNS_SUCCESS_RATE_LOW"]
    assert dns_events, "Expected DNS_SUCCESS_RATE_LOW event for 93% success rate"
    assert dns_events[0].severity == Severity.HIGH


def test_dns_success_rate_ok_no_event() -> None:
    snap = _empty_snapshot()
    snap["health"] = [_www_health(dns_success_rate=0.98)]
    events = run(snap)
    assert not any(e.event_type == "DNS_SUCCESS_RATE_LOW" for e in events)


def test_dns_success_rate_absent_no_event() -> None:
    snap = _empty_snapshot()
    snap["health"] = [_www_health()]  # no dns_success_rate key
    events = run(snap)
    assert not any(e.event_type == "DNS_SUCCESS_RATE_LOW" for e in events)


def test_dns_success_rate_metrics_populated() -> None:
    snap = _empty_snapshot()
    snap["health"] = [_www_health(dns_success_rate=0.93)]
    events = run(snap)
    evt = next(e for e in events if e.event_type == "DNS_SUCCESS_RATE_LOW")
    assert evt.metrics["dns_success_rate"] == pytest.approx(0.93)
    assert evt.metrics["minimum"] == pytest.approx(0.95)


# ── WiFi tx retry rate ────────────────────────────────────────────────────────

def test_tx_retry_warning() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [_radio_stats(tx_packets=1000, tx_retries=220)]  # 22%
    events = run(snap)
    evt = next((e for e in events if e.event_type == "WIFI_TX_RETRY_HIGH"), None)
    assert evt is not None
    assert evt.severity == Severity.HIGH


def test_tx_retry_critical() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [_radio_stats(tx_packets=1000, tx_retries=400)]  # 40%
    events = run(snap)
    evt = next(e for e in events if e.event_type == "WIFI_TX_RETRY_HIGH")
    assert evt.severity == Severity.CRITICAL


def test_tx_retry_ok_no_event() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [_radio_stats(tx_packets=1000, tx_retries=100)]  # 10%
    events = run(snap)
    assert not any(e.event_type == "WIFI_TX_RETRY_HIGH" for e in events)


def test_tx_retry_zero_packets_skipped() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [_radio_stats(tx_packets=0, tx_retries=0)]
    events = run(snap)
    assert not any(e.event_type == "WIFI_TX_RETRY_HIGH" for e in events)


def test_tx_retry_metrics_contain_device_name() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [_radio_stats(name="FlexHD", tx_packets=1000, tx_retries=250)]
    events = run(snap)
    evt = next(e for e in events if e.event_type == "WIFI_TX_RETRY_HIGH")
    assert evt.metrics["device"] == "FlexHD"


# ── WiFi satisfaction ─────────────────────────────────────────────────────────

def test_low_satisfaction_flagged() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [_radio_stats(satisfaction=30)]
    events = run(snap)
    assert any(e.event_type == "WIFI_LOW_SATISFACTION" for e in events)


def test_satisfaction_at_floor_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [_radio_stats(satisfaction=50)]
    events = run(snap)
    assert not any(e.event_type == "WIFI_LOW_SATISFACTION" for e in events)


def test_satisfaction_none_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["device_stats"] = [
        {
            "name": "AP-1",
            "radio_table_stats": [{"radio": "na", "channel": 36, "tx_packets": 0,
                                    "tx_retries": 0}],
        }
    ]
    events = run(snap)
    assert not any(e.event_type == "WIFI_LOW_SATISFACTION" for e in events)


# ── Client signal ─────────────────────────────────────────────────────────────

def test_poor_signal_client_flagged() -> None:
    snap = _empty_snapshot()
    snap["client_stats"] = [_wireless_client(signal=-80)]
    events = run(snap)
    assert any(e.event_type == "CLIENT_POOR_SIGNAL" for e in events)


def test_good_signal_client_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["client_stats"] = [_wireless_client(signal=-60)]
    events = run(snap)
    assert not any(e.event_type == "CLIENT_POOR_SIGNAL" for e in events)


def test_wired_client_no_signal_skipped() -> None:
    snap = _empty_snapshot()
    snap["client_stats"] = [_wired_client()]
    events = run(snap)
    assert not any(e.event_type == "CLIENT_POOR_SIGNAL" for e in events)


def test_multiple_poor_clients_single_event() -> None:
    snap = _empty_snapshot()
    snap["client_stats"] = [
        _wireless_client(signal=-80, hostname="a"),
        _wireless_client(signal=-85, hostname="b"),
    ]
    events = run(snap)
    poor_events = [e for e in events if e.event_type == "CLIENT_POOR_SIGNAL"]
    assert len(poor_events) == 1
    assert poor_events[0].metrics["poor_clients"].__len__() == 2


# ── VPN ───────────────────────────────────────────────────────────────────────

def test_vpn_error_flagged() -> None:
    snap = _empty_snapshot()
    snap["health"] = [{"subsystem": "vpn", "status": "error"}]
    events = run(snap)
    assert any(e.event_type == "VPN_SUBSYSTEM_ERROR" for e in events)


def test_vpn_ok_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["health"] = [{"subsystem": "vpn", "status": "ok"}]
    events = run(snap)
    assert not any(e.event_type == "VPN_SUBSYSTEM_ERROR" for e in events)


# ── Device offline ────────────────────────────────────────────────────────────

def test_offline_device_flagged() -> None:
    snap = _empty_snapshot()
    snap["devices"] = [_device(name="Switch-1", state="OFFLINE")]
    events = run(snap)
    assert any(e.event_type == "DEVICE_OFFLINE" for e in events)


def test_online_device_not_flagged() -> None:
    snap = _empty_snapshot()
    snap["devices"] = [_device(name="AP-1", state="ONLINE")]
    events = run(snap)
    assert not any(e.event_type == "DEVICE_OFFLINE" for e in events)


def test_dismissed_offline_device_suppressed() -> None:
    """An offline device with an active dismissal is suppressed. Replaces the
    previous hardcoded `_KNOWN_OFFLINE` allowlist with the dismissal registry."""
    from network_engineer.tools.dismissals import DismissalRegistry
    from network_engineer.tools.schemas import Dismissal

    dismissals = DismissalRegistry()
    dismissals.add(Dismissal(
        finding_code="DEVICE_OFFLINE",
        match_field="name",
        match_key="Spare Cam",
        reason="Test fixture: backup unit, intentionally powered off.",
    ))

    snap = _empty_snapshot()
    snap["devices"] = [_device(name="Spare Cam", state="OFFLINE", model="UVC-G4-PRO")]
    events = run(snap, dismissals=dismissals)
    assert not any(e.event_type == "DEVICE_OFFLINE" for e in events)


# ── Event ordering ────────────────────────────────────────────────────────────

def test_events_sorted_by_severity() -> None:
    snap = _empty_snapshot()
    snap["health"] = [_www_health(latency=120, drops=1, dns_success_rate=0.93)]
    snap["devices"] = [_device(name="Switch-1", state="OFFLINE")]
    events = run(snap)
    severity_order = {
        Severity.CRITICAL: 0, Severity.HIGH: 1,
        Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4,
    }
    severities = [e.severity for e in events]
    assert severities == sorted(severities, key=lambda s: severity_order[s])


def test_event_agent_field_is_monitor() -> None:
    snap = _empty_snapshot()
    snap["health"] = [_www_health(latency=120)]
    events = run(snap)
    assert all(e.agent == "monitor" for e in events)


# ── Live integration ──────────────────────────────────────────────────────────

_LIVE = pytest.mark.skipif(
    not os.getenv("UNIFI_HOST"), reason="UNIFI_HOST not set — live tests require home LAN"
)


@_LIVE
def test_live_monitor_sweep() -> None:
    from network_engineer.agents.monitor import run_from_client
    from network_engineer.tools.unifi_client import UnifiClient

    client = UnifiClient(use_fixtures=False)
    events = run_from_client(client)

    assert isinstance(events, list)
    assert all(isinstance(e, NetworkEvent) for e in events)
