"""Device + Client registry — operator knowledge layer.

Stores per-MAC annotations the UniFi controller cannot give you: location,
deployment rationale, criticality, ownership, classification overrides.

Storage:
  config/device_register.yaml  — UniFi-managed devices (APs, switches, gateway)
  config/client_register.yaml  — stations / clients
  Both files are gitignored. Examples are checked in as
  examples/*.example.yaml.

Phase 11 mirror:
  Schemas in tools/schemas.py document the Supabase DDL. The Pydantic models
  serialize 1:1 to the table columns. cloud_sync.py (Phase 11) will push/pull
  rows by mac primary key + updated_at timestamp for incremental sync.

Lookup helpers:
  Registry.load()                    — load both registers from disk
  registry.get_device(mac)           — by MAC, normalized
  registry.get_client(mac)           — by MAC, normalized
  registry.upsert_device(entry)      — add/update + bump updated_at
  registry.upsert_client(entry)      — add/update + bump updated_at
  registry.save()                    — write both YAML files

OUI / identification helpers (used by the interactive walkthrough):
  manufacturer_for_mac(mac)          — best-effort manufacturer name from OUI
  identification_tips(manufacturer)  — list of tips for ID'ing an unknown device
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from network_engineer.tools.schemas import (
    ClientRegistryEntry,
    DeviceRegistryEntry,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DEVICE_PATH = _REPO_ROOT / "config" / "device_register.yaml"
_DEFAULT_CLIENT_PATH = _REPO_ROOT / "config" / "client_register.yaml"
_OUI_PATH = _REPO_ROOT / "config" / "oui_common.yaml"
_TIPS_PATH = _REPO_ROOT / "config" / "identification_tips.yaml"


# ── MAC normalization ────────────────────────────────────────────────────────

def normalize_mac(mac: str) -> str:
    """Normalize a MAC address to lowercase colon-separated form.

    Accepts: aa:bb:cc:dd:ee:ff, AA-BB-CC-DD-EE-FF, aabb.ccdd.eeff, aabbccddeeff.
    """
    if not mac:
        return ""
    raw = mac.strip().lower()
    stripped = "".join(c for c in raw if c in "0123456789abcdef")
    if len(stripped) != 12:
        return raw  # unrecognized — return as-is so we don't silently corrupt
    return ":".join(stripped[i:i + 2] for i in range(0, 12, 2))


def oui_for_mac(mac: str) -> str:
    """Return the 6-hex-char OUI prefix (no separators) for a MAC."""
    s = normalize_mac(mac).replace(":", "")
    return s[:6] if len(s) >= 6 else ""


# ── OUI / identification tips ────────────────────────────────────────────────

_OUI_CACHE: dict[str, str] | None = None
_TIPS_CACHE: dict[str, list[str]] | None = None


def _load_oui() -> dict[str, str]:
    global _OUI_CACHE
    if _OUI_CACHE is None:
        if _OUI_PATH.exists():
            data = yaml.safe_load(_OUI_PATH.read_text()) or {}
            _OUI_CACHE = {k.lower(): v for k, v in data.items()}
        else:
            _OUI_CACHE = {}
    return _OUI_CACHE


def _load_tips() -> dict[str, list[str]]:
    global _TIPS_CACHE
    if _TIPS_CACHE is None:
        if _TIPS_PATH.exists():
            _TIPS_CACHE = yaml.safe_load(_TIPS_PATH.read_text()) or {}
        else:
            _TIPS_CACHE = {}
    return _TIPS_CACHE


def manufacturer_for_mac(mac: str) -> str:
    """Best-effort manufacturer name for a MAC. Returns 'Unknown' on miss."""
    oui = oui_for_mac(mac)
    if not oui:
        return "Unknown"
    return _load_oui().get(oui, "Unknown")


def identification_tips(manufacturer: str) -> list[str]:
    """Practical tips for identifying a device by its manufacturer name."""
    tips = _load_tips()
    # Case-insensitive substring match
    needle = (manufacturer or "").lower()
    for key, value in tips.items():
        if key.lower() in needle or needle in key.lower():
            return list(value)
    return list(tips.get("unknown", []))


# ── Registry class ───────────────────────────────────────────────────────────

class Registry:
    """In-memory device + client registries with YAML persistence."""

    def __init__(
        self,
        *,
        devices: dict[str, DeviceRegistryEntry] | None = None,
        clients: dict[str, ClientRegistryEntry] | None = None,
        device_path: Path | None = None,
        client_path: Path | None = None,
    ) -> None:
        self.devices: dict[str, DeviceRegistryEntry] = dict(devices or {})
        self.clients: dict[str, ClientRegistryEntry] = dict(clients or {})
        self.device_path = device_path or _DEFAULT_DEVICE_PATH
        self.client_path = client_path or _DEFAULT_CLIENT_PATH

    # ── Load / save ──────────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        *,
        device_path: Path | None = None,
        client_path: Path | None = None,
    ) -> Registry:
        """Load both registers from disk; missing files yield an empty registry."""
        registry = cls(device_path=device_path, client_path=client_path)
        if registry.device_path.exists():
            data = yaml.safe_load(registry.device_path.read_text()) or {}
            for raw in data.get("devices", []):
                entry = DeviceRegistryEntry(**raw)
                registry.devices[normalize_mac(entry.mac)] = entry
        if registry.client_path.exists():
            data = yaml.safe_load(registry.client_path.read_text()) or {}
            for raw in data.get("clients", []):
                entry = ClientRegistryEntry(**raw)
                registry.clients[normalize_mac(entry.mac)] = entry
        return registry

    def save(self) -> None:
        """Write both YAML files atomically. Skips writing empty registers."""
        if self.devices:
            self.device_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "devices": [
                    e.model_dump(mode="json", exclude_none=True)
                    for e in sorted(self.devices.values(), key=lambda x: x.mac)
                ],
            }
            self.device_path.write_text(_dump_yaml(data))
        if self.clients:
            self.client_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "clients": [
                    e.model_dump(mode="json", exclude_none=True)
                    for e in sorted(self.clients.values(), key=lambda x: x.mac)
                ],
            }
            self.client_path.write_text(_dump_yaml(data))

    # ── Lookup ───────────────────────────────────────────────────────────

    def get_device(self, mac: str) -> DeviceRegistryEntry | None:
        return self.devices.get(normalize_mac(mac))

    def get_client(self, mac: str) -> ClientRegistryEntry | None:
        return self.clients.get(normalize_mac(mac))

    # ── Mutate ───────────────────────────────────────────────────────────

    def upsert_device(self, entry: DeviceRegistryEntry) -> DeviceRegistryEntry:
        entry.mac = normalize_mac(entry.mac)
        entry.updated_at = datetime.now(UTC)
        self.devices[entry.mac] = entry
        return entry

    def upsert_client(self, entry: ClientRegistryEntry) -> ClientRegistryEntry:
        entry.mac = normalize_mac(entry.mac)
        entry.updated_at = datetime.now(UTC)
        self.clients[entry.mac] = entry
        return entry

    # ── Filters ──────────────────────────────────────────────────────────

    def unannotated_devices(self) -> list[DeviceRegistryEntry]:
        """Devices missing the high-value operator fields (location at minimum)."""
        return [e for e in self.devices.values() if not e.location and not e.deleted_at]

    def unannotated_clients(self) -> list[ClientRegistryEntry]:
        """Clients missing tier_override (most-needed field)."""
        return [
            e for e in self.clients.values()
            if e.tier_override is None and not e.deleted_at
        ]


def _dump_yaml(data: Any) -> str:
    """Dump YAML with stable, readable settings."""
    return yaml.safe_dump(
        data, sort_keys=False, default_flow_style=False, allow_unicode=True, width=120,
    )
