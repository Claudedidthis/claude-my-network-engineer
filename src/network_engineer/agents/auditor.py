"""Auditor agent — read-only analysis of a network snapshot.

Runs a set of deterministic checks against snapshot data and returns a list of
Finding objects ordered by severity. No writes; no network calls beyond loading
the snapshot via UnifiClient.

Checks implemented:
  WIFI_CHANNEL_CONFLICT       — two or more APs on the same channel (co-channel interference)
  WIFI_NO_ENCRYPTION          — a WiFi network with no encryption (open network)
  PORT_FORWARD_SENSITIVE      — port forward exposing a sensitive service to any source IP
  PORT_FORWARD_UNRESTRICTED   — any enabled port forward with src=any (lower severity catch-all)
  NO_CUSTOM_FIREWALL_RULES    — zero custom firewall rules (policy gap)
  DEVICE_OFFLINE_UNEXPECTED   — device offline that is not in the known-offline list
  AP_CHANNEL_AUTO_5GHZ        — 5GHz radio left on auto (can cause uncontrolled conflicts)
  DUPLICATE_CLIENT_IP         — two connected clients sharing the same IP address
"""
from __future__ import annotations

import re
from typing import Any

from network_engineer.tools.logging_setup import get_logger
from network_engineer.tools.schemas import Finding, Severity

log = get_logger("agents.auditor")

# ── Constants ─────────────────────────────────────────────────────────────────

# Ports that are sensitive to expose directly to the internet
_SENSITIVE_PORTS: set[str] = {
    "21",    # FTP — plaintext, credential exposure
    "22",    # SSH — brute-force target
    "23",    # Telnet — plaintext
    "3389",  # RDP — frequent ransomware vector
    "5900",  # VNC
    "8080",  # Alternate HTTP / UniFi inform
    "8443",  # UniFi HTTPS management
    "1194",  # OpenVPN (exposing VPN server config)
    "1723",  # PPTP VPN
}

# Per-fork operator suppressions live in config/dismissals.yaml — never hardcode.
# Earlier versions hardcoded operator-specific allowlists (an offline-device set,
# an open-SSID allowlist) directly in agent code. Those were operator-specific
# facts that did not belong in shared agent code; they have been migrated to the
# operator-supplied dismissals registry. See tools/dismissals.py and
# examples/dismissals.example.yaml.

_MAC_RE = re.compile(r"^([0-9a-f]{2}[:\-]){5}[0-9a-f]{2}$", re.IGNORECASE)


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_wifi_channel_conflicts(device_stats: list[dict[str, Any]]) -> list[Finding]:
    """Detect co-channel interference: multiple APs on the same band+channel."""
    findings: list[Finding] = []

    # Build {band: {channel: [ap_name, ...]}}
    # Prefer radio_table_stats (actual current channel) over radio_table (config — may say "auto")
    channel_map: dict[str, dict[str, list[str]]] = {"2.4GHz": {}, "5GHz": {}}

    for device in device_stats:
        name = device.get("name", device.get("mac", "unknown"))
        radios_stats: list[dict[str, Any]] = device.get("radio_table_stats", [])
        radios_config: list[dict[str, Any]] = device.get("radio_table", [])

        # Build a lookup from config so we can tell if the channel was manually set
        config_by_radio = {r.get("radio"): r for r in radios_config}

        for stat in radios_stats:
            radio = stat.get("radio", "")
            channel = stat.get("channel")
            if not channel:
                continue

            band = "2.4GHz" if radio == "ng" else "5GHz" if radio in ("na", "ac") else None
            if not band:
                continue

            config = config_by_radio.get(radio, {})
            is_auto = str(config.get("channel", "auto")).lower() == "auto"

            key = str(channel)
            channel_map[band].setdefault(key, [])
            channel_map[band][key].append((name, is_auto))

    for band, channels in channel_map.items():
        for ch, aps in channels.items():
            if len(aps) < 2:
                continue
            ap_names = [ap[0] for ap in aps]
            any_auto = any(ap[1] for ap in aps)
            findings.append(
                Finding(
                    severity=Severity.HIGH,
                    code="WIFI_CHANNEL_CONFLICT",
                    title=f"Co-channel interference: {band} ch {ch} used by {len(aps)} APs",
                    detail=(
                        f"{', '.join(ap_names)} are all operating on {band} channel {ch}. "
                        "Clients associated to any of these APs compete for the same airtime, "
                        "increasing retry rates and reducing throughput. "
                        + (
                            "Some radios are set to 'auto' — consider pinning them to "
                            "non-overlapping channels to prevent future conflicts."
                            if any_auto
                            else "Pin each AP to a distinct non-overlapping channel."
                        )
                    ),
                    evidence={"band": band, "channel": ch, "access_points": ap_names},
                )
            )

    return findings


def _guest_portal_active(settings: list[dict[str, Any]]) -> bool:
    """Return True if the site has an active guest hotspot portal with voucher/hotspot auth."""
    for s in settings:
        if s.get("key") == "guest_access":
            return bool(s.get("portal_enabled")) and s.get("auth") in (
                "hotspot", "radius", "password"
            )
    return False


def _check_wifi_encryption(
    wifi_networks: list[dict[str, Any]],
    settings: list[dict[str, Any]],
    dismissals: Any = None,
) -> list[Finding]:
    portal_active = _guest_portal_active(settings)
    findings: list[Finding] = []

    for wlan in wifi_networks:
        if not wlan.get("enabled", True):
            continue
        security = wlan.get("security", "")
        if security not in ("open", "", None):
            continue

        ssid = wlan.get("name", "unknown")
        is_guest = wlan.get("is_guest", False)

        # Operator-supplied dismissals registry suppresses intentional open SSIDs
        # (captive portals, lab networks, etc.) — the per-fork mechanism that
        # replaced the old hardcoded _KNOWN_CAPTIVE_PORTAL_SSIDS allowlist.
        evidence_for_match = {"ssid": ssid, "security": security, "is_guest": is_guest}
        if dismissals is not None:
            match = dismissals.matches("WIFI_NO_ENCRYPTION", evidence_for_match)
            if match is not None:
                findings.append(
                    Finding(
                        severity=Severity.INFO,
                        code="WIFI_GUEST_PORTAL_OPEN",
                        title=f"Open SSID '{ssid}' dismissed by operator",
                        detail=(
                            f"'{ssid}' is intentionally configured as an open SSID. "
                            f"Operator reason: {match.reason}"
                        ),
                        evidence={
                            "ssid": ssid, "security": security, "is_guest": is_guest,
                            "dismissed": True, "reason": match.reason,
                            "source": "dismissals_registry",
                        },
                    )
                )
                continue

        if is_guest and portal_active:
            # Open WiFi is intentional — the portal (voucher/hotspot) gates access
            findings.append(
                Finding(
                    severity=Severity.INFO,
                    code="WIFI_GUEST_PORTAL_OPEN",
                    title=f"Guest network '{ssid}' uses open WiFi with hotspot portal",
                    detail=(
                        f"'{ssid}' has no WPA encryption at the WiFi layer, but it is a guest "
                        "network protected by an active hotspot portal (voucher-based access). "
                        "This is a deliberate design: the open layer allows portal redirect; "
                        "the portal gates actual internet access. "
                        "Note: traffic between portal-authenticated clients is still unencrypted "
                        "over the air — consider OWE (Opportunistic Wireless Encryption) if your "
                        "APs support it for per-client encryption without a password."
                    ),
                    evidence={
                        "ssid": ssid,
                        "security": security,
                        "is_guest": True,
                        "portal_protected": True,
                    },
                )
            )
        else:
            findings.append(
                Finding(
                    severity=Severity.CRITICAL,
                    code="WIFI_NO_ENCRYPTION",
                    title=f"Open WiFi network: {ssid}",
                    detail=(
                        f"The SSID '{ssid}' has no encryption. Any device within "
                        "range can join and observe all traffic on that network segment. "
                        "Enable WPA2/WPA3 Personal or Enterprise immediately."
                    ),
                    evidence={"ssid": ssid, "security": security, "is_guest": is_guest},
                )
            )
    return findings


def _check_port_forwards(
    port_forwards: list[dict[str, Any]],
    origin_stories: Any = None,
) -> list[Finding]:
    findings: list[Finding] = []
    for pf in port_forwards:
        if not pf.get("enabled", False):
            continue
        src = pf.get("src", "any") or "any"
        if src.lower() != "any":
            continue  # source-restricted — not flagging

        dst_port = str(pf.get("dst_port", ""))
        name = pf.get("name", "unnamed")
        fwd = pf.get("fwd", "")
        fwd_port = pf.get("fwd_port", dst_port)

        # Operator origin story — has the operator explained why this exists?
        story = origin_stories.get("port_forward", name) if origin_stories else None
        if story is not None and story.do_not_touch:
            # Operator marked it do-not-touch — emit INFO only, do not flag
            findings.append(
                Finding(
                    severity=Severity.INFO,
                    code="PORT_FORWARD_DOCUMENTED",
                    title=f"Port forward '{name}' has operator rationale (do-not-touch)",
                    detail=(
                        f"Port {dst_port} → {fwd}:{fwd_port}. "
                        f"Operator rationale: {story.rationale}"
                    ),
                    evidence={
                        "name": name, "dst_port": dst_port,
                        "forward_to": f"{fwd}:{fwd_port}",
                        "rationale": story.rationale,
                        "do_not_touch": True,
                    },
                )
            )
            continue

        if dst_port in _SENSITIVE_PORTS:
            severity = Severity.HIGH
            detail = (
                f"Port forward '{name}' exposes port {dst_port} ({_port_name(dst_port)}) "
                f"to any source IP, forwarding to {fwd}:{fwd_port}. "
                "Restrict the source to known IP ranges or use a VPN instead of "
                "direct port exposure."
            )
            if story is not None:
                # Operator has rationale but didn't mark do_not_touch — downgrade
                # one severity level (HIGH → MEDIUM) and append the rationale.
                severity = Severity.MEDIUM
                detail += f"\n\nOperator rationale on file: {story.rationale}"
            findings.append(
                Finding(
                    severity=severity,
                    code="PORT_FORWARD_SENSITIVE",
                    title=f"Sensitive port {dst_port} exposed to internet: '{name}'",
                    detail=detail,
                    evidence={
                        "name": name,
                        "dst_port": dst_port,
                        "forward_to": f"{fwd}:{fwd_port}",
                        "protocol": pf.get("proto"),
                        "src": src,
                        "rationale": story.rationale if story else None,
                    },
                )
            )
        else:
            severity = Severity.LOW
            detail = (
                f"Port {dst_port} is forwarded to {fwd}:{fwd_port} from any source IP. "
                "Consider restricting to known IPs or a VPN if this service is not "
                "intended for general internet access."
            )
            if story is not None:
                severity = Severity.INFO
                detail += f"\n\nOperator rationale on file: {story.rationale}"
            findings.append(
                Finding(
                    severity=severity,
                    code="PORT_FORWARD_UNRESTRICTED",
                    title=f"Port forward '{name}' has no source restriction",
                    detail=detail,
                    evidence={
                        "name": name,
                        "dst_port": dst_port,
                        "forward_to": f"{fwd}:{fwd_port}",
                        "protocol": pf.get("proto"),
                        "rationale": story.rationale if story else None,
                    },
                )
            )
    return findings


def _port_name(port: str) -> str:
    names = {
        "21": "FTP", "22": "SSH", "23": "Telnet", "80": "HTTP",
        "443": "HTTPS", "3389": "RDP", "5900": "VNC",
        "8080": "HTTP-alt / UniFi Inform", "8443": "HTTPS-alt / UniFi UI",
        "1194": "OpenVPN", "1723": "PPTP",
    }
    return names.get(port, f"port {port}")


def _check_firewall_rules(firewall_rules: list[dict[str, Any]]) -> list[Finding]:
    if firewall_rules:
        return []
    return [
        Finding(
            severity=Severity.MEDIUM,
            code="NO_CUSTOM_FIREWALL_RULES",
            title="Zero custom firewall rules — network relies entirely on default policy",
            detail=(
                "No custom firewall rules are defined. The network is protected only by UniFi's "
                "default inter-VLAN routing policy. Without explicit rules, IoT devices, cameras, "
                "and trusted hosts can potentially communicate freely across VLANs. "
                "Add rules to block IoT-to-trusted and camera-to-internet traffic as a minimum."
            ),
            evidence={"firewall_rule_count": 0},
        )
    ]


def _check_offline_devices(
    devices: list[dict[str, Any]], dismissals: Any = None,
) -> list[Finding]:
    findings: list[Finding] = []
    for device in devices:
        if device.get("state", "ONLINE") == "ONLINE":
            continue
        name = device.get("name", device.get("mac", "unknown"))
        if dismissals is not None:
            match = dismissals.matches(
                "DEVICE_OFFLINE_UNEXPECTED",
                {"name": name, "mac": device.get("macAddress", "")},
            )
            if match is not None:
                log.debug(
                    "device '%s' dismissed (%s) — skipping", name, match.reason,
                )
                continue
        findings.append(
            Finding(
                severity=Severity.HIGH,
                code="DEVICE_OFFLINE_UNEXPECTED",
                title=f"Device offline: {name}",
                detail=(
                    f"'{name}' ({device.get('model', '?')}) is reporting as "
                    f"{device.get('state', 'unknown')}. "
                    "Check physical connections, PoE budget, and recent firmware changes."
                ),
                evidence={
                    "name": name,
                    "model": device.get("model"),
                    "state": device.get("state"),
                    "ip": device.get("ipAddress"),
                    "mac": device.get("macAddress"),
                },
            )
        )
    return findings


def _check_duplicate_client_ips(clients: list[dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    ip_to_clients: dict[str, list[str]] = {}
    for client in clients:
        ip = client.get("ipAddress", "")
        if not ip or ip.startswith("169.254"):  # skip APIPA
            continue
        name = client.get("name") or client.get("macAddress", "?")
        ip_to_clients.setdefault(ip, []).append(name)

    for ip, names in ip_to_clients.items():
        if len(names) > 1:
            findings.append(
                Finding(
                    severity=Severity.MEDIUM,
                    code="DUPLICATE_CLIENT_IP",
                    title=f"IP address {ip} assigned to {len(names)} clients",
                    detail=(
                        f"Multiple clients share IP {ip}: {', '.join(names)}. "
                        "This indicates a DHCP conflict or stale lease. "
                        "Check DHCP server logs and consider assigning fixed IPs."
                    ),
                    evidence={"ip": ip, "clients": names},
                )
            )
    return findings


def _check_ap_auto_channel_5ghz(device_stats: list[dict[str, Any]]) -> list[Finding]:
    """Flag 5GHz radios left on auto — auto-selection can produce conflicts."""
    findings: list[Finding] = []
    auto_aps: list[str] = []

    for device in device_stats:
        name = device.get("name", device.get("mac", "unknown"))
        for radio in device.get("radio_table", []):
            if radio.get("radio") in ("na", "ac"):
                if str(radio.get("channel", "auto")).lower() == "auto":
                    auto_aps.append(name)

    if auto_aps:
        findings.append(
            Finding(
                severity=Severity.LOW,
                code="AP_CHANNEL_AUTO_5GHZ",
                title=f"{len(auto_aps)} AP(s) using automatic 5GHz channel selection",
                detail=(
                    f"{', '.join(auto_aps)} have 5GHz channel set to 'auto'. "
                    "Auto-selection can assign the same channel to neighboring APs — "
                    "as currently seen with the ch 48 conflict. "
                    "Pin 5GHz radios to distinct non-overlapping channels "
                    "(36, 40, 44, 48, 149, 153, 157, 161) so adjacent APs never share one."
                ),
                evidence={"access_points": auto_aps},
            )
        )
    return findings


# ── Main entry point ──────────────────────────────────────────────────────────

_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


def run(
    snapshot: dict[str, Any],
    dismissals: Any = None,
    origin_stories: Any = None,
) -> list[Finding]:
    """Run all audit checks against *snapshot* and return findings sorted by severity.

    *dismissals* is an optional DismissalRegistry; if None, loaded lazily from
    config/dismissals.yaml.
    *origin_stories* is an optional OriginStoryRegistry; if None, loaded lazily
    from config/origin_stories.yaml. Operator rationales soften / suppress
    findings on artifacts the operator has explained.
    Tests pass empty registries to bypass.
    """
    if dismissals is None:
        from network_engineer.tools.dismissals import DismissalRegistry
        dismissals = DismissalRegistry.load()
    if origin_stories is None:
        from network_engineer.tools.origin_stories import OriginStoryRegistry
        origin_stories = OriginStoryRegistry.load()

    log.info("audit_start", extra={"agent": "auditor", "action": "audit_start"})

    findings: list[Finding] = []

    findings += _check_wifi_channel_conflicts(snapshot.get("device_stats", []))
    findings += _check_wifi_encryption(
        snapshot.get("wifi_networks", []),
        snapshot.get("settings", []),
        dismissals=dismissals,
    )
    findings += _check_port_forwards(
        snapshot.get("port_forwards", []),
        origin_stories=origin_stories,
    )
    findings += _check_firewall_rules(snapshot.get("firewall_rules", []))
    findings += _check_offline_devices(snapshot.get("devices", []), dismissals=dismissals)
    findings += _check_duplicate_client_ips(snapshot.get("clients", []))
    findings += _check_ap_auto_channel_5ghz(snapshot.get("device_stats", []))

    findings.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.code))

    log.info(
        "audit_complete",
        extra={
            "agent": "auditor",
            "action": "audit_complete",
            "finding_count": len(findings),
            "critical": sum(1 for f in findings if f.severity == Severity.CRITICAL),
            "high": sum(1 for f in findings if f.severity == Severity.HIGH),
            "medium": sum(1 for f in findings if f.severity == Severity.MEDIUM),
            "low": sum(1 for f in findings if f.severity == Severity.LOW),
        },
    )
    return findings


def run_from_client(client: Any) -> list[Finding]:
    """Convenience: pull a fresh snapshot from *client* then audit it."""
    snapshot = {
        "devices": client.get_devices(),
        "device_stats": client.get_device_stats(),
        "clients": client.get_clients(),
        "wifi_networks": client.get_wifi_networks(),
        "firewall_rules": client.get_firewall_rules(),
        "port_forwards": client.get_port_forwards(),
        "settings": client.get_settings(),
    }
    return run(snapshot)
