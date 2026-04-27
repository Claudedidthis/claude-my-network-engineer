"""Monitor agent — polls live metrics and emits NetworkEvents when thresholds are breached.

Runs deterministic threshold checks against a metric snapshot; never writes to the network.
Events at WARNING+ are logged to agent_actions.log via logging_setup.

Checks implemented:
  WAN_LATENCY_HIGH          — www subsystem latency vs warn/critical thresholds
  WAN_DROPS                 — non-zero drop count on the WAN interface
  WIFI_TX_RETRY_HIGH        — per-radio tx_retries/tx_packets vs warn/critical thresholds
  WIFI_LOW_SATISFACTION     — per-radio satisfaction score below acceptable floor
  CLIENT_POOR_SIGNAL        — wireless clients below the minimum signal threshold
  VPN_SUBSYSTEM_ERROR       — VPN health reports error status
  DNS_SUCCESS_RATE_LOW      — DNS success rate below configured minimum (when data available)
  DEVICE_OFFLINE            — any device not in ONLINE state (continuous check)
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from network_engineer.tools.logging_setup import get_logger
from network_engineer.tools.schemas import NetworkEvent, Severity

log = get_logger("agents.monitor")

_THRESHOLDS_PATH = Path(__file__).resolve().parents[3] / "config" / "alert_thresholds.yaml"

# Operator suppressions for intentionally-offline devices live in
# config/dismissals.yaml under the DEVICE_OFFLINE_UNEXPECTED finding code,
# matching the same migration done in agents/auditor.py. Earlier versions
# hardcoded an offline-device set here; that was operator-specific and has
# been removed.


def _load_thresholds() -> dict[str, Any]:
    return yaml.safe_load(_THRESHOLDS_PATH.read_text())


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_wan(health: list[dict[str, Any]], thresholds: dict[str, Any]) -> list[NetworkEvent]:
    events: list[NetworkEvent] = []
    wan_t = thresholds.get("wan", {})
    warn_ms = wan_t.get("latency_warning_ms", 50)
    crit_ms = wan_t.get("latency_critical_ms", 100)

    for sub in health:
        if sub.get("subsystem") != "www":
            continue

        latency = sub.get("latency")
        if latency is not None:
            if latency >= crit_ms:
                events.append(NetworkEvent(
                    event_type="WAN_LATENCY_HIGH",
                    severity=Severity.CRITICAL,
                    message=f"WAN latency {latency}ms exceeds critical threshold {crit_ms}ms",
                    metrics={"latency_ms": latency, "threshold_ms": crit_ms},
                    agent="monitor",
                ))
            elif latency >= warn_ms:
                events.append(NetworkEvent(
                    event_type="WAN_LATENCY_HIGH",
                    severity=Severity.HIGH,
                    message=f"WAN latency {latency}ms exceeds warning threshold {warn_ms}ms",
                    metrics={"latency_ms": latency, "threshold_ms": warn_ms},
                    agent="monitor",
                ))

        drops = sub.get("drops")
        if drops and drops > 0:
            events.append(NetworkEvent(
                event_type="WAN_DROPS",
                severity=Severity.MEDIUM,
                message=f"WAN interface reported {drops} packet drop(s)",
                metrics={"drops": drops},
                agent="monitor",
            ))

        # DNS success rate — present when UDM has run DNS probes
        dns_rate = sub.get("dns_success_rate")
        if dns_rate is not None:
            min_rate = thresholds.get("wifi", {}).get("dns_success_min", 0.95)
            if dns_rate < min_rate:
                events.append(NetworkEvent(
                    event_type="DNS_SUCCESS_RATE_LOW",
                    severity=Severity.HIGH,
                    message=(
                        f"DNS success rate {dns_rate:.1%} is below minimum {min_rate:.1%}"
                    ),
                    metrics={"dns_success_rate": dns_rate, "minimum": min_rate},
                    agent="monitor",
                ))

    return events


def _check_wifi_radios(
    device_stats: list[dict[str, Any]],
    thresholds: dict[str, Any],
) -> list[NetworkEvent]:
    events: list[NetworkEvent] = []
    wifi_t = thresholds.get("wifi", {})
    retry_warn = wifi_t.get("tx_retry_rate_warning", 0.20)
    retry_crit = wifi_t.get("tx_retry_rate_critical", 0.35)
    sat_floor = wifi_t.get("satisfaction_floor", 50)

    for device in device_stats:
        name = device.get("name", device.get("mac", "unknown"))
        for radio in device.get("radio_table_stats", []):
            radio_band = "2.4GHz" if radio.get("radio") == "ng" else "5GHz"
            channel = radio.get("channel", "?")
            tx_packets = radio.get("tx_packets") or 0
            tx_retries = radio.get("tx_retries") or 0

            if tx_packets > 0:
                retry_rate = tx_retries / tx_packets
                if retry_rate >= retry_crit:
                    events.append(NetworkEvent(
                        event_type="WIFI_TX_RETRY_HIGH",
                        severity=Severity.CRITICAL,
                        message=(
                            f"{name} {radio_band} ch{channel}: "
                            f"tx retry rate {retry_rate:.1%} exceeds critical threshold "
                            f"{retry_crit:.0%}"
                        ),
                        metrics={
                            "device": name, "band": radio_band, "channel": channel,
                            "retry_rate": round(retry_rate, 4),
                            "tx_packets": tx_packets, "tx_retries": tx_retries,
                            "threshold": retry_crit,
                        },
                        agent="monitor",
                    ))
                elif retry_rate >= retry_warn:
                    events.append(NetworkEvent(
                        event_type="WIFI_TX_RETRY_HIGH",
                        severity=Severity.HIGH,
                        message=(
                            f"{name} {radio_band} ch{channel}: "
                            f"tx retry rate {retry_rate:.1%} exceeds warning threshold "
                            f"{retry_warn:.0%}"
                        ),
                        metrics={
                            "device": name, "band": radio_band, "channel": channel,
                            "retry_rate": round(retry_rate, 4),
                            "tx_packets": tx_packets, "tx_retries": tx_retries,
                            "threshold": retry_warn,
                        },
                        agent="monitor",
                    ))

            satisfaction = radio.get("satisfaction")
            if satisfaction is not None and satisfaction >= 0 and satisfaction < sat_floor:
                events.append(NetworkEvent(
                    event_type="WIFI_LOW_SATISFACTION",
                    severity=Severity.MEDIUM,
                    message=(
                        f"{name} {radio_band} ch{channel}: "
                        f"satisfaction score {satisfaction} below floor {sat_floor}"
                    ),
                    metrics={
                        "device": name, "band": radio_band,
                        "satisfaction": satisfaction, "floor": sat_floor,
                    },
                    agent="monitor",
                ))

    return events


def _check_client_signal(
    client_stats: list[dict[str, Any]],
    thresholds: dict[str, Any],
) -> list[NetworkEvent]:
    events: list[NetworkEvent] = []
    min_signal = thresholds.get("wifi", {}).get("client_signal_poor", -75)

    poor: list[dict[str, Any]] = []
    for client in client_stats:
        signal = client.get("signal")
        if signal is None:
            continue  # wired client
        if signal < min_signal:
            poor.append({
                "name": client.get("hostname") or client.get("ip") or "unknown",
                "signal_dbm": signal,
                "ip": client.get("ip"),
            })

    if poor:
        events.append(NetworkEvent(
            event_type="CLIENT_POOR_SIGNAL",
            severity=Severity.LOW,
            message=(
                f"{len(poor)} wireless client(s) below signal floor {min_signal} dBm"
            ),
            metrics={"min_signal_dbm": min_signal, "poor_clients": poor},
            agent="monitor",
        ))

    return events


def _check_vpn(health: list[dict[str, Any]]) -> list[NetworkEvent]:
    events: list[NetworkEvent] = []
    for sub in health:
        if sub.get("subsystem") == "vpn" and sub.get("status") == "error":
            events.append(NetworkEvent(
                event_type="VPN_SUBSYSTEM_ERROR",
                severity=Severity.LOW,
                message="VPN subsystem reports error status (may be expected if no VPN is active)",
                metrics={"subsystem": "vpn", "status": "error"},
                agent="monitor",
            ))
    return events


def _check_devices(
    devices: list[dict[str, Any]], dismissals: Any = None,
) -> list[NetworkEvent]:
    events: list[NetworkEvent] = []
    for device in devices:
        if device.get("state", "ONLINE") == "ONLINE":
            continue
        name = device.get("name", device.get("macAddress", "unknown"))
        # Operator dismissals registry suppresses intentional-offline devices —
        # mirrors the auditor.py migration of the previously-hardcoded allowlist.
        if dismissals is not None:
            match = dismissals.matches(
                "DEVICE_OFFLINE",
                {"name": name, "mac": device.get("macAddress", "")},
            )
            if match is not None:
                log.debug("monitor suppressing offline device '%s' (%s)", name, match.reason)
                continue
        events.append(NetworkEvent(
            event_type="DEVICE_OFFLINE",
            severity=Severity.HIGH,
            message=f"Device offline: {name} ({device.get('model', '?')})",
            metrics={
                "name": name,
                "model": device.get("model"),
                "state": device.get("state"),
                "ip": device.get("ipAddress"),
            },
            agent="monitor",
        ))
    return events


# ── Main entry points ─────────────────────────────────────────────────────────

_SEVERITY_ORDER = {
    Severity.CRITICAL: 0, Severity.HIGH: 1,
    Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4,
}


def run(snapshot: dict[str, Any], dismissals: Any = None) -> list[NetworkEvent]:
    """Evaluate all threshold checks against *snapshot* and return NetworkEvents.

    *dismissals* is an optional DismissalRegistry; if None, loaded lazily
    from config/dismissals.yaml. Suppresses operator-confirmed intentional
    states (offline devices, etc.). Tests pass an empty registry to bypass.
    """
    if dismissals is None:
        from network_engineer.tools.dismissals import DismissalRegistry
        dismissals = DismissalRegistry.load()

    thresholds = _load_thresholds()

    events: list[NetworkEvent] = []
    events += _check_wan(snapshot.get("health", []), thresholds)
    events += _check_wifi_radios(snapshot.get("device_stats", []), thresholds)
    events += _check_client_signal(snapshot.get("client_stats", []), thresholds)
    events += _check_vpn(snapshot.get("health", []))
    events += _check_devices(snapshot.get("devices", []), dismissals=dismissals)

    events.sort(key=lambda e: _SEVERITY_ORDER[e.severity])

    for event in events:
        if event.severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM):
            log.warning(
                event.event_type,
                extra={
                    "agent": "monitor",
                    "action": event.event_type,
                    "severity": event.severity,
                    "metrics": event.metrics,
                },
            )

    log.info(
        "monitor_sweep_complete",
        extra={
            "agent": "monitor",
            "action": "monitor_sweep",
            "event_count": len(events),
            "critical": sum(1 for e in events if e.severity == Severity.CRITICAL),
            "high": sum(1 for e in events if e.severity == Severity.HIGH),
        },
    )
    return events


def run_from_client(client: Any) -> list[NetworkEvent]:
    """Pull a live metric snapshot then run all checks."""
    snapshot = {
        "health": client.get_health(),
        "devices": client.get_devices(),
        "device_stats": client.get_device_stats(),
        "client_stats": client.get_client_stats(),
    }
    return run(snapshot)


def watch(client: Any, interval: int = 300) -> None:
    """Poll continuously, logging events on each sweep. Ctrl-C to stop."""
    log.info("monitor_watch_start", extra={"agent": "monitor", "action": "watch_start",
                                           "interval_seconds": interval})
    try:
        while True:
            events = run_from_client(client)
            _print_sweep(events)
            log.info("monitor_sleep", extra={"agent": "monitor", "action": "sleep",
                                             "seconds": interval})
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


_SWEEP_ICON = {
    Severity.CRITICAL: "🔴", Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡", Severity.LOW: "🔵", Severity.INFO: "⚪",
}


def _print_sweep(events: list[NetworkEvent]) -> None:
    from datetime import UTC, datetime
    ts = datetime.now(UTC).strftime("%H:%M:%S UTC")
    if not events:
        print(f"[{ts}] ✅  All metrics within thresholds")
        return
    counts = {}
    for e in events:
        counts[e.severity] = counts.get(e.severity, 0) + 1
    summary = "  ".join(f"{_SWEEP_ICON[s]} {s}: {n}" for s, n in counts.items())
    print(f"[{ts}] {len(events)} event(s)   {summary}")
    for e in events:
        print(f"  {_SWEEP_ICON[e.severity]} {e.event_type}: {e.message}")
