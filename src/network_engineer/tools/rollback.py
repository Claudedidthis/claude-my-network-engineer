"""rollback — targeted field-level rollback helpers for the optimizer.

The optimizer records the pre-change state from a snapshot before applying
any write, then uses these helpers to restore it if verification fails.

Full network restore is not possible via the UniFi API. Only fields explicitly
changed by the optimizer can be reverted this way.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RollbackError(RuntimeError):
    """Raised when a rollback cannot be completed."""


def load_snapshot(path: Path) -> dict[str, Any]:
    """Load a snapshot JSON file and return its contents."""
    return json.loads(path.read_text())


def device_name_from_snapshot(snapshot: dict[str, Any], mac: str) -> str | None:
    """Return the display name recorded for a device in the snapshot, keyed by MAC."""
    for dev in snapshot.get("devices", []):
        if dev.get("macAddress") == mac:
            return dev.get("name")
    return None


def radio_channel_from_snapshot(
    snapshot: dict[str, Any], device_mac: str, radio: str
) -> str | None:
    """Return the channel string recorded for a radio in the snapshot.

    radio: "na" (5 GHz) or "ng" (2.4 GHz)
    Returns None if the device or radio is not found.
    """
    for dev in snapshot.get("device_stats", []):
        if dev.get("mac") != device_mac:
            continue
        for r in dev.get("radio_table", []):
            if r.get("radio") == radio:
                return str(r.get("channel", "auto"))
    return None
