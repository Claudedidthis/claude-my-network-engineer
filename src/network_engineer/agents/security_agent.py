"""Security Agent — generates structured Recommendations for human approval.

This agent is read-only by design: every action it could trigger is in the
REQUIRES_APPROVAL tier, so the agent never executes. It produces a
Recommendation and writes it to logs/recommendations.log for human review
(via iOS in Phase 13+).

Primary deliverable: a complete VLAN architecture proposal that segments
trusted devices, IoT, cameras, and guests onto isolated VLANs with
appropriate inter-VLAN firewall rules.
"""
from __future__ import annotations

from typing import Any

from network_engineer.agents.ai_runtime import AIRuntime
from network_engineer.tools.logging_setup import get_logger, log_recommendation
from network_engineer.tools.schemas import (
    ApprovalStatus,
    Recommendation,
    SecurityTier,
)

# Re-export so existing callers can keep importing SecurityTier from this module.
__all__ = ["SecurityTier", "classify_all", "classify_client", "propose_vlans", "render_markdown"]

log = get_logger("agents.security_agent")


# ── Client classification ─────────────────────────────────────────────────────


_CAMERA_KEYWORDS = (
    "cam ", " cam", "camera", "doorbell", "protect", "uvc-", "g4 ", "g5 ",
    "chime",
)

_IOT_KEYWORDS = (
    "hue", "lutron", "nest", "crestron", "philips", "zigbee", "lifx", "ring",
    "alexa", "echo", "sonos", "roku", "smartthings", "ecobee", "thermostat",
    "kasa", "govee", "xerox", "xbox", "playstation", "switchbot", "tplink",
    "tp-link", "bridge",
)

_TRUST_KEYWORDS = (
    "macbook", "iphone", "ipad", "imac", "mac mini", "mac studio", "macos",
    "thinkpad", "dell", "hp ", "linux", "windows", "laptop", "ubuntu",
    "lenovo", "surface", "pixel",
)


def classify_client(client: dict[str, Any], registry: Any = None) -> SecurityTier:
    """Classify a single client. Registry override wins over heuristics.

    If `registry` is None, the global Registry is consulted (loaded lazily).
    Pass an explicit empty Registry in tests to bypass.
    """
    mac = (client.get("macAddress") or client.get("mac") or "").lower()

    # Registry tier_override wins
    if registry is None:
        from network_engineer.tools.registry import Registry
        registry = Registry.load()
    entry = registry.get_client(mac) if mac else None
    if entry and entry.tier_override:
        return SecurityTier(entry.tier_override)

    # Heuristic fallback
    name = (client.get("name") or client.get("hostname") or "").strip()
    if not name or name.lower() == mac or _looks_like_mac(name):
        return SecurityTier.UNKNOWN

    n = name.lower()
    if any(kw in n for kw in _CAMERA_KEYWORDS):
        return SecurityTier.CAMERA
    if any(kw in n for kw in _IOT_KEYWORDS):
        return SecurityTier.IOT
    if any(kw in n for kw in _TRUST_KEYWORDS):
        return SecurityTier.TRUST
    return SecurityTier.UNKNOWN


def _looks_like_mac(name: str) -> bool:
    """True if *name* is shaped like a MAC address (`aa:bb:cc:dd:ee:ff`)."""
    s = name.strip()
    return len(s) == 17 and s.count(":") == 5


def classify_all(clients: list[dict[str, Any]]) -> dict[SecurityTier, list[dict[str, Any]]]:
    """Group clients by their classified tier."""
    buckets: dict[SecurityTier, list[dict[str, Any]]] = {t: [] for t in SecurityTier}
    for c in clients:
        buckets[classify_client(c)].append(c)
    return buckets


# ── Target segmentation strategy (proposal) ───────────────────────────────────
#
# This is intentionally STRATEGY-ONLY: we name the tiers and the inter-tier
# policies but do NOT emit concrete CIDRs, VLAN IDs, controller IPs, or
# DHCP scopes. Concrete numbers depend on:
#   - existing networks the operator already has
#   - already-used VLAN IDs
#   - DHCP scope conflicts and static reservations
#   - gateway/controller IP discovery
#   - origin-story networks the operator has reasons to keep
#   - mDNS reflector capability of the controller version
#
# A future "topology allocator" component will read those constraints and
# produce concrete numbers; the placeholder for that work is task #45 in the
# tracker. Until then this agent emits a *segmentation strategy* — a
# Recommendation the operator (or the allocator) turns into a concrete plan.
#
# Symbolic placeholders used below:
#   <to-allocate>   — a CIDR/VLAN ID the operator or allocator must choose
#   <gateway>       — the existing gateway/controller IP, read from the snapshot
#                     and surfaced in current_state.gateway_ip


def _proposed_tiers() -> list[dict[str, Any]]:
    """Policy tiers — no concrete CIDRs/VLAN IDs.

    Each tier is a *role* (Trust/IoT/Cameras/Guest), an inter-tier policy
    statement, and a description of what membership implies. Concrete
    network numbers are deferred to topology allocation.
    """
    return [
        {
            "tier": "Trust",
            "purpose": "Laptops, phones, workstations — full internal access.",
            "internet_access": True,
            "inter_tier_policy": "Allowed to all tiers (initiator).",
            "vlan_id": "<to-allocate>",
            "subnet": "<to-allocate>",
        },
        {
            "tier": "IoT",
            "purpose": "Smart-home devices (Hue, Lutron, Crestron, printers, "
                       "consoles, smart TVs).",
            "internet_access": True,
            "inter_tier_policy": "Blocked from Trust/Cameras/Guest as initiator; "
                                  "Trust → IoT allowed for control plane.",
            "vlan_id": "<to-allocate>",
            "subnet": "<to-allocate>",
        },
        {
            "tier": "Cameras",
            "purpose": "UniFi Protect cameras and doorbells.",
            "internet_access": False,
            "inter_tier_policy": "Cameras → WAN blocked (firmware-stage exfil "
                                  "defense); Cameras → controller allowed; "
                                  "Trust → Cameras allowed for viewing.",
            "vlan_id": "<to-allocate>",
            "subnet": "<to-allocate>",
        },
        {
            "tier": "Guest",
            "purpose": "Guest portal SSID and visitor traffic.",
            "internet_access": True,
            "inter_tier_policy": "Isolated from every internal tier.",
            "vlan_id": "<to-allocate>",
            "subnet": "<to-allocate>",
        },
    ]


def _proposed_firewall_strategy(gateway_ip: str | None) -> list[dict[str, Any]]:
    """Symbolic inter-tier rules — references tier names + <gateway> placeholder.

    The Cameras→controller rule resolves to whatever the snapshot tells us
    the gateway IP is (read from current_state); when unknown, the rule is
    emitted with the literal token <gateway> for the operator to fill in.
    """
    gw = gateway_ip or "<gateway>"
    return [
        {"name": "Allow Trust → all tiers", "src": "Trust", "dst": "any",
         "action": "allow"},
        {"name": "Block IoT → Trust",     "src": "IoT",     "dst": "Trust",
         "action": "drop"},
        {"name": "Block IoT → Cameras",   "src": "IoT",     "dst": "Cameras",
         "action": "drop"},
        {"name": "Block IoT → Guest",     "src": "IoT",     "dst": "Guest",
         "action": "drop"},
        {"name": "Block Cameras → WAN",   "src": "Cameras", "dst": "WAN",
         "action": "drop"},
        {"name": "Allow Cameras → controller",
         "src": "Cameras", "dst": gw, "action": "allow",
         "note": "Cameras must reach the UniFi Protect controller. The "
                 "destination is the controller IP discovered in current_state."},
        {"name": "Allow Trust → Cameras (viewing)",
         "src": "Trust", "dst": "Cameras", "action": "allow"},
        {"name": "Block Guest → internal",
         "src": "Guest", "dst": "Trust,IoT,Cameras", "action": "drop"},
        {
            "name": "Allow mDNS reflector Trust ↔ IoT",
            "src": "Trust,IoT", "dst": "224.0.0.251:5353", "action": "allow",
            "note": "Required for AirPlay, Hue, Sonos, Chromecast discovery "
                    "across tiers. UniFi has a built-in mDNS reflector toggle.",
        },
    ]


def _detect_existing_topology(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Read what the snapshot tells us about the current topology.

    The Recommendation surfaces this in current_state so the operator (or a
    future topology allocator) has the constraints visible: existing VLAN
    IDs, existing subnets, the gateway IP, the existing client /24 in use,
    DHCP-enabled networks. This is *read*, not invented.
    """
    networks = snapshot.get("networks", []) or []
    network_config = snapshot.get("network_config", []) or []
    devices = snapshot.get("devices", []) or []
    clients = snapshot.get("clients", []) or []

    existing_vlan_ids: list[int] = []
    existing_subnets: list[str] = []
    for n in networks:
        vlan = n.get("vlan")
        if isinstance(vlan, int) and vlan > 0:
            existing_vlan_ids.append(vlan)
        subnet = n.get("ip_subnet") or n.get("subnet")
        if isinstance(subnet, str) and subnet:
            existing_subnets.append(subnet)
    for nc in network_config:
        vlan = nc.get("vlan")
        try:
            if vlan is not None and int(vlan) > 0 and int(vlan) not in existing_vlan_ids:
                existing_vlan_ids.append(int(vlan))
        except (TypeError, ValueError):
            pass
        subnet = nc.get("ip_subnet")
        if isinstance(subnet, str) and subnet and subnet not in existing_subnets:
            existing_subnets.append(subnet)

    gateway_ip: str | None = None
    for d in devices:
        if "gateway" in str(d.get("model", "")).lower() or d.get("type") == "ugw":
            ip = d.get("ipAddress") or d.get("ip")
            if isinstance(ip, str) and ip:
                gateway_ip = ip
                break

    if gateway_ip is None:
        ip_counts: dict[str, int] = {}
        for c in clients:
            ip = c.get("ipAddress") or c.get("ip")
            if isinstance(ip, str) and ip.count(".") == 3:
                prefix = ip.rsplit(".", 1)[0]
                ip_counts[prefix] = ip_counts.get(prefix, 0) + 1
        if ip_counts:
            common_prefix = max(ip_counts.items(), key=lambda kv: kv[1])[0]
            gateway_ip = f"{common_prefix}.1"

    return {
        "existing_vlan_ids": sorted(set(existing_vlan_ids)),
        "existing_subnets": sorted(set(existing_subnets)),
        "gateway_ip": gateway_ip,
        "gateway_ip_source": (
            "discovered_from_devices" if gateway_ip and any(
                "gateway" in str(d.get("model", "")).lower() or d.get("type") == "ugw"
                for d in devices
            )
            else ("inferred_from_client_ip_prefix" if gateway_ip else "unknown")
        ),
    }


def _migration_phases() -> list[dict[str, str]]:
    return [
        {
            "phase": "1",
            "action": "Take a full UniFi config backup. Create VLAN networks 10/20/30/40 "
                      "in UniFi Network. No clients moved yet — VLANs exist but unused.",
        },
        {
            "phase": "2",
            "action": "Configure inter-VLAN firewall rules with action=log (not drop). "
                      "Run for 48 hours and review the traffic log for unexpected flows.",
        },
        {
            "phase": "3",
            "action": "Move cameras to Cameras VLAN. Verify all 5 cameras come back "
                      "online in Protect. Confirm doorbell still rings.",
        },
        {
            "phase": "4",
            "action": "Move IoT clients to IoT VLAN one room at a time. Test each app "
                      "(Hue, Lutron Caseta, Crestron, Xbox Live) after moving.",
        },
        {
            "phase": "5",
            "action": "Move trusted clients to Trust VLAN. Reconfigure WiFi SSIDs so "
                      "the trusted SSID maps to VLAN 10 and the operator's existing guest SSID to VLAN 40.",
        },
        {
            "phase": "6",
            "action": "Switch firewall rules from log to drop. Monitor for 24 hours; "
                      "if a critical service breaks, revert that one rule and diagnose.",
        },
    ]


# ── Main entry point ──────────────────────────────────────────────────────────

def propose_vlans(
    client: Any,
    *,
    runtime: AIRuntime | None = None,
) -> Recommendation:
    """Build a complete VLAN architecture Recommendation.

    Pulls live network state, classifies clients into security tiers, attaches
    an AI narrative review (or deterministic fallback when AI is disabled),
    writes the recommendation to recommendations.log, and returns it.
    """
    snapshot = {
        "networks": client.get_networks(),
        "network_config": _safe_get(client, "get_network_config"),
        "wifi_networks": client.get_wifi_networks(),
        "clients": client.get_clients(),
        "devices": _safe_get(client, "get_devices"),
        "firewall_rules": client.get_firewall_rules(),
    }
    return _build_proposal(snapshot, runtime=runtime)


def _safe_get(client: Any, method_name: str) -> list[dict[str, Any]]:
    """Call client.<method_name>() if available; otherwise return []."""
    method = getattr(client, method_name, None)
    if not callable(method):
        return []
    try:
        result = method()
    except Exception:
        return []
    return result if isinstance(result, list) else []


def _build_proposal(
    snapshot: dict[str, Any],
    *,
    runtime: AIRuntime | None = None,
) -> Recommendation:
    """Construct the Recommendation from an in-memory snapshot. Used by tests."""
    buckets = classify_all(snapshot.get("clients", []))
    topology = _detect_existing_topology(snapshot)

    proposed_change: dict[str, Any] = {
        "output_kind": "segmentation_strategy",
        "tiers": _proposed_tiers(),
        "firewall_strategy": _proposed_firewall_strategy(topology["gateway_ip"]),
        "migration_phases": _migration_phases(),
        "topology_caveats": (
            "This proposal is policy-only. Concrete VLAN IDs, CIDRs, DHCP "
            "scopes, and static reservations must be selected by the "
            "operator (or a future topology allocator) using the existing "
            "topology surfaced in current_state.existing_topology — "
            "specifically existing_vlan_ids and existing_subnets — to avoid "
            "collisions, plus origin_stories.yaml to avoid disturbing "
            "intentionally-shaped networks (e.g. a DMZ kept for a third-party "
            "installer)."
        ),
    }

    current_state: dict[str, Any] = {
        "current_networks": [n.get("name") for n in snapshot.get("networks", [])],
        "current_firewall_rule_count": len(snapshot.get("firewall_rules", [])),
        "existing_topology": topology,
        "client_breakdown": {
            tier.value: [
                {
                    "name": c.get("name") or c.get("hostname"),
                    "ip": c.get("ipAddress"),
                    "mac": c.get("macAddress"),
                }
                for c in lst
            ]
            for tier, lst in buckets.items()
            if lst
        },
    }

    n_iot = len(buckets[SecurityTier.IOT])
    n_cam = len(buckets[SecurityTier.CAMERA])
    n_trust = len(buckets[SecurityTier.TRUST])
    n_total = sum(len(lst) for lst in buckets.values())

    rationale = (
        f"This network runs {n_total} clients on a single flat /24. "
        f"IoT devices ({n_iot}), cameras ({n_cam}), and trusted endpoints "
        f"({n_trust}) all share the same broadcast domain and reachability "
        "envelope. Segmenting by security tier is the highest-impact security "
        "change available: it caps the blast radius of any compromised IoT "
        "device, prevents cameras from initiating outbound internet flows "
        "(defense against firmware-stage exfil), and isolates guest SSID "
        "traffic from internal services."
    )

    risk = (
        "Medium during migration, low after. Specific risks: "
        "(a) misconfigured Trust↔IoT rule could break Hue/Lutron/Sonos app control "
        "until mDNS reflection is enabled; "
        "(b) cameras may temporarily lose connection to Protect during the VLAN move "
        "(usually <60s, sometimes a controller restart is needed); "
        "(c) Chromecast and AirPlay rely on multicast and require an mDNS reflector "
        "across VLANs — verify before phase 6; "
        "(d) any client with a manually configured static IP on 192.168.1.x will "
        "need its IP updated when moved."
    )

    rollback_plan = (
        "Every migration phase is reversible. Phases 1-2 add VLANs without moving "
        "clients — deleting them is harmless. Phases 3-5 move clients; reverting "
        "is a port-profile flip back to Default VLAN per port (or per SSID). "
        "Only phase 6 makes firewall rules enforcing — and a single rule revert "
        "(action: drop → allow) restores the prior reachability while a problem "
        "is diagnosed. Take a full UniFi config backup before phase 1 (use "
        "`nye test --snapshot` plus the UniFi OS backup file)."
    )

    rec = Recommendation(
        action="propose_segmentation_strategy",
        title="Segmentation strategy: Trust / IoT / Cameras / Guest tiers",
        rationale=rationale,
        current_state=current_state,
        proposed_change=proposed_change,
        rollback_plan=rollback_plan,
        risk=risk,
        status=ApprovalStatus.PENDING,
        agent="security_agent",
    )

    # AI narrative review (or deterministic fallback when disabled)
    if runtime is None:
        runtime = AIRuntime()
    review = runtime.review_config_change(
        proposed_change, current_state, action="create_vlan",
    )
    rec.proposed_change["ai_review"] = review.model_dump(mode="json")

    log_recommendation(
        "security_agent",
        "propose_segmentation_strategy",
        rec.model_dump(mode="json"),
    )
    log.info(
        "security_proposal_emitted",
        extra={
            "agent": "security_agent",
            "action": "propose_segmentation_strategy",
            "iot_count": n_iot,
            "camera_count": n_cam,
            "trust_count": n_trust,
            "ai_verdict": review.verdict,
        },
    )
    return rec


# ── Markdown rendering (for human review) ─────────────────────────────────────

def render_markdown(rec: Recommendation) -> str:
    """Render a VLAN-architecture Recommendation as readable markdown."""
    lines: list[str] = [
        f"# {rec.title}",
        f"_Generated by `{rec.agent}` at {rec.created_at.isoformat()} — status: {rec.status}_",
        "",
        "## Why",
        "",
        rec.rationale,
        "",
        "## Current State",
        "",
        f"- **Networks:** {', '.join(rec.current_state.get('current_networks', [])) or '(none)'}",
        f"- **Firewall rules:** {rec.current_state.get('current_firewall_rule_count', 0)}",
        "",
        "**Client breakdown:**",
        "",
    ]
    for tier, clients in rec.current_state.get("client_breakdown", {}).items():
        lines.append(f"- **{tier}** ({len(clients)}): " +
                     ", ".join(c.get("name", "?") for c in clients[:8]) +
                     (" …" if len(clients) > 8 else ""))
    topology = rec.current_state.get("existing_topology", {})
    if topology:
        lines += [
            "",
            "## Existing Topology (read from snapshot)",
            "",
            f"- **Existing VLAN IDs:** "
            f"{topology.get('existing_vlan_ids') or '(none detected)'}",
            f"- **Existing subnets:** "
            f"{topology.get('existing_subnets') or '(none detected)'}",
            f"- **Gateway/controller IP:** "
            f"`{topology.get('gateway_ip') or 'unknown'}` "
            f"(source: {topology.get('gateway_ip_source', 'unknown')})",
        ]

    lines += [
        "",
        "## Proposed Tiers (policy-only — concrete numbers TBD)",
        "",
        "| Tier | VLAN ID | Subnet | Internet | Inter-tier policy |",
        "|------|--------:|--------|:--------:|-------------------|",
    ]
    for t in rec.proposed_change.get("tiers", []):
        lines.append(
            f"| {t['tier']} | {t['vlan_id']} | `{t['subnet']}` | "
            f"{'allow' if t.get('internet_access') else 'block'} | "
            f"{t['inter_tier_policy']} |"
        )

    lines += ["", "## Firewall Strategy", ""]
    for r in rec.proposed_change.get("firewall_strategy", []):
        action = r.get("action", "?")
        marker = "(allow)" if action == "allow" else "(drop) "
        lines.append(f"- {marker} **{r['name']}** — `{r['src']}` → `{r['dst']}`")

    caveats = rec.proposed_change.get("topology_caveats")
    if caveats:
        lines += ["", "## Topology Caveats", "", caveats]

    lines += ["", "## Migration Plan", ""]
    for p in rec.proposed_change.get("migration_phases", []):
        lines.append(f"**Phase {p['phase']}** — {p['action']}")
        lines.append("")

    lines += [
        "## Risk",
        "",
        rec.risk,
        "",
        "## Rollback Plan",
        "",
        rec.rollback_plan,
        "",
    ]

    review = rec.proposed_change.get("ai_review")
    if review:
        verdict = review.get("verdict", "?")
        lines += [
            "## AI Review",
            "",
            f"**Verdict:** `{verdict}`  (model: `{review.get('model_used') or 'fallback'}`)",
            "",
            review.get("reasoning", ""),
            "",
        ]
        if review.get("concerns"):
            lines.append("**Concerns:**")
            lines += [f"- {c}" for c in review["concerns"]]
            lines.append("")
        if review.get("suggested_alternatives"):
            lines.append("**Suggested alternatives:**")
            lines += [f"- {a}" for a in review["suggested_alternatives"]]
            lines.append("")
        if review.get("questions"):
            lines.append("**Open questions:**")
            lines += [f"- {q}" for q in review["questions"]]
            lines.append("")

    return "\n".join(lines)
