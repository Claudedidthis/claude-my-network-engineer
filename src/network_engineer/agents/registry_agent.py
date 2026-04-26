"""Registry agent — interactive walkthrough that populates the device + client register.

The walkthrough flow (per device or client lacking annotations):

  1. Show what we know:
       MAC, OUI manufacturer, UniFi-reported hostname/fingerprint, IP,
       last-seen AP, and (for devices) physical type/model.

  2. Ask "do you recognize this?"
       y → ask for: tier_override (clients only), owner (clients only),
                    location, role, criticality, notes
       n → print manufacturer-specific identification tips and offer:
             - skip for now
             - mark UNKNOWN tier and continue
             - take the answers anyway

  3. Save after each entry — partial fills are fine, the walkthrough is resumable.

The flow is also the design template for Phase 13's iOS guided-ID UI.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from network_engineer.tools.logging_setup import get_logger
from network_engineer.tools.registry import (
    Registry,
    identification_tips,
    manufacturer_for_mac,
    normalize_mac,
)
from network_engineer.tools.schemas import (
    ClientRegistryEntry,
    DeviceRegistryEntry,
    SecurityTier,
    Severity,
)

log = get_logger("agents.registry")

# Input function indirection — tests inject their own to bypass stdin.
_DEFAULT_INPUT: Callable[[str], str] = input


# ── Bootstrap (non-interactive) ──────────────────────────────────────────────

def bootstrap(
    client: Any,
    *,
    registry: Registry | None = None,
    auto_classify_clients: bool = True,
) -> tuple[Registry, dict[str, int]]:
    """Add registry entries for every live device + client we don't already track.

    Existing entries are NEVER overwritten — operator edits win. This is the
    non-interactive seed that the walkthrough then enriches.

    Returns (registry, counts) where counts has keys
    'devices_added', 'clients_added', 'clients_auto_classified'.
    """
    registry = registry or Registry.load()
    counts = {"devices_added": 0, "clients_added": 0, "clients_auto_classified": 0}

    # UniFi-managed devices
    for d in client.get_device_stats():
        mac = normalize_mac(d.get("mac", ""))
        if not mac:
            continue
        if registry.get_device(mac):
            continue
        registry.upsert_device(DeviceRegistryEntry(
            mac=mac, name_hint=d.get("name") or "?", source="auto",
        ))
        counts["devices_added"] += 1

    # Clients
    if auto_classify_clients:
        # Local import to avoid the security_agent → registry circular path
        from network_engineer.agents.security_agent import classify_client
    for c in client.get_clients():
        mac = normalize_mac(c.get("macAddress", ""))
        if not mac:
            continue
        if registry.get_client(mac):
            continue

        tier_override: SecurityTier | None = None
        if auto_classify_clients:
            # Pass an empty registry to bypass any pre-existing override
            tier = classify_client(c, registry=Registry())
            if tier != SecurityTier.UNKNOWN:
                tier_override = tier
                counts["clients_auto_classified"] += 1

        registry.upsert_client(ClientRegistryEntry(
            mac=mac,
            name_hint=c.get("name") or c.get("hostname"),
            tier_override=tier_override,
            source="auto",
        ))
        counts["clients_added"] += 1

    registry.save()
    return registry, counts


# ── Interactive walkthrough ──────────────────────────────────────────────────

def walkthrough(
    client: Any,
    *,
    registry: Registry | None = None,
    input_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Walk the operator through every unannotated device + client."""
    ask = input_fn or _DEFAULT_INPUT
    say = print_fn or print
    registry = registry or Registry.load()

    # Build a quick lookup of live data for context display
    devices_live = {normalize_mac(d.get("mac", "")): d for d in client.get_device_stats()}
    clients_live = {normalize_mac(c.get("macAddress", "")): c for c in client.get_clients()}

    counts = {"devices_annotated": 0, "clients_annotated": 0, "skipped": 0}

    say("\n" + "=" * 60)
    say("Registry walkthrough — fill in operator knowledge")
    say("=" * 60)
    say("Type 'q' or 'quit' at any prompt to exit and save progress.")

    # Devices first (usually fewer, all known to operator)
    devices_pending = registry.unannotated_devices()
    if devices_pending:
        say(f"\n--- {len(devices_pending)} device(s) need annotation ---\n")
        for entry in devices_pending:
            try:
                if _walk_device(entry, devices_live.get(entry.mac, {}), ask, say):
                    counts["devices_annotated"] += 1
                else:
                    counts["skipped"] += 1
                registry.upsert_device(entry)
                registry.save()
            except _QuitError:
                say("\nSaving and exiting.")
                return counts

    # Then clients
    clients_pending = registry.unannotated_clients()
    if clients_pending:
        say(f"\n--- {len(clients_pending)} client(s) need annotation ---\n")
        for entry in clients_pending:
            try:
                if _walk_client(entry, clients_live.get(entry.mac, {}), ask, say):
                    counts["clients_annotated"] += 1
                else:
                    counts["skipped"] += 1
                registry.upsert_client(entry)
                registry.save()
            except _QuitError:
                say("\nSaving and exiting.")
                return counts

    if not devices_pending and not clients_pending:
        say("\n✅  Nothing to do — every entry is already annotated.")

    say("\n" + "=" * 60)
    say(f"Done: {counts['devices_annotated']} device(s) + "
        f"{counts['clients_annotated']} client(s) annotated, {counts['skipped']} skipped.")
    say("=" * 60)
    return counts


# ── Per-entry walks ──────────────────────────────────────────────────────────

class _QuitError(Exception):
    """Sentinel raised when the operator types 'q'."""


def _ask(prompt: str, ask: Callable[[str], str]) -> str:
    answer = ask(prompt).strip()
    if answer.lower() in ("q", "quit", "exit"):
        raise _QuitError
    return answer


def _walk_device(
    entry: DeviceRegistryEntry,
    live: dict[str, Any],
    ask: Callable[[str], str],
    say: Callable[[str], None],
) -> bool:
    """Walk one device. Returns True if anything was filled in, False if skipped."""
    name = entry.name_hint or live.get("name") or "?"
    model = live.get("model") or "?"
    ip = live.get("ip") or "?"

    say("─" * 60)
    say(f"Device: {name}  (model: {model}, mac: {entry.mac}, ip: {ip})")
    say("─" * 60)

    skip = _ask("Annotate this device now? [y/N/q]: ", ask).lower()
    if skip not in ("y", "yes"):
        return False

    location = _ask("  Location (e.g. 'master bedroom'): ", ask)
    if location:
        entry.location = location

    rationale = _ask("  Why is it here? (optional, press Enter to skip): ", ask)
    if rationale:
        entry.rationale = rationale

    role = _ask("  Role [primary/secondary/mesh/wired-backbone/gateway, Enter to skip]: ", ask)
    if role:
        entry.role = role

    crit = _ask("  Criticality [CRITICAL/HIGH/MEDIUM/LOW, Enter to skip]: ", ask).upper()
    if crit in {s.value for s in Severity}:
        entry.criticality = Severity(crit)

    notes = _ask("  Notes (optional): ", ask)
    if notes:
        entry.notes = notes

    entry.source = "manual"
    say(f"  → saved {name}")
    return True


def _walk_client(
    entry: ClientRegistryEntry,
    live: dict[str, Any],
    ask: Callable[[str], str],
    say: Callable[[str], None],
) -> bool:
    """Walk one client with manufacturer/identification assistance."""
    name = entry.name_hint or live.get("name") or live.get("hostname") or "(unnamed)"
    ip = live.get("ip") or live.get("ipAddress") or "?"
    fingerprint = live.get("fingerprint") or live.get("oui") or ""
    manufacturer = manufacturer_for_mac(entry.mac)
    is_wireless = bool(live.get("signal")) or bool(live.get("essid"))
    radio = live.get("radio_proto") or live.get("radio") or ""

    say("─" * 60)
    say(f"Client: {name}")
    say(f"  mac:           {entry.mac}")
    say(f"  manufacturer:  {manufacturer}")
    if fingerprint:
        say(f"  fingerprint:   {fingerprint}")
    say(f"  ip:            {ip}")
    say(f"  connection:    {'wireless ' + radio if is_wireless else 'wired'}".rstrip())
    say("─" * 60)

    answer = _ask("Do you recognize this device? [y/n/skip/q]: ", ask).lower()

    if answer in ("skip", "s", ""):
        return False

    if answer not in ("y", "yes"):
        # Show identification tips
        tips = identification_tips(manufacturer)
        say("\n  How to identify this device:")
        for t in tips:
            say(f"    • {t}")
        answer2 = _ask("\n  Identified now? [y/N/skip/q]: ", ask).lower()
        if answer2 not in ("y", "yes"):
            # Mark as UNKNOWN so we don't re-prompt every walkthrough
            entry.tier_override = SecurityTier.UNKNOWN
            entry.notes = "Operator could not identify on walkthrough — revisit later."
            entry.source = "manual"
            say("  → marked UNKNOWN; will revisit later.")
            return True

    # Capture the operator's annotations
    tier_in = _ask("  Tier [TRUST/IOT/CAMERA/GUEST/UNKNOWN]: ", ask).upper()
    if tier_in in {s.value for s in SecurityTier}:
        entry.tier_override = SecurityTier(tier_in)
    else:
        entry.tier_override = SecurityTier.UNKNOWN

    owner = _ask("  Owner (e.g. 'alex', 'shared', 'kid', 'guest'): ", ask)
    if owner:
        entry.owner = owner

    location = _ask("  Location (e.g. 'kitchen', 'whole house', 'office'): ", ask)
    if location:
        entry.location = location

    role = _ask("  Role (e.g. 'primary phone', 'smart-home hub', Enter to skip): ", ask)
    if role:
        entry.role = role

    crit = _ask("  Criticality [CRITICAL/HIGH/MEDIUM/LOW, Enter to skip]: ", ask).upper()
    if crit in {s.value for s in Severity}:
        entry.criticality = Severity(crit)

    notes = _ask("  Notes (optional): ", ask)
    if notes:
        entry.notes = notes

    entry.source = "manual"
    say(f"  → saved as {entry.tier_override}.")
    return True
