"""config_diff — human-readable diff of two network snapshots."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Return a text diff of two snapshot dicts, highlighting meaningful changes."""
    lines: list[str] = []
    _diff_devices(before.get("devices", []), after.get("devices", []), lines)
    _diff_radios(before.get("device_stats", []), after.get("device_stats", []), lines)
    return "\n".join(lines) if lines else "_No meaningful differences detected._"


def diff_snapshot_files(before_path: Path, after_path: Path) -> str:
    """Diff two snapshot JSON files on disk."""
    return diff_snapshots(
        json.loads(before_path.read_text()),
        json.loads(after_path.read_text()),
    )


def _index(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items if key in item}


def _diff_devices(before: list, after: list, lines: list[str]) -> None:
    b = _index(before, "macAddress")
    a = _index(after, "macAddress")
    changes: list[str] = []
    for mac, bdev in b.items():
        adev = a.get(mac)
        if adev is None:
            changes.append(f"  removed: {bdev.get('name', mac)}")
            continue
        if bdev.get("name") != adev.get("name"):
            changes.append(f"  {mac}: name {bdev.get('name')!r} → {adev.get('name')!r}")
        if bdev.get("state") != adev.get("state"):
            changes.append(
                f"  {adev.get('name', mac)}: state {bdev.get('state')} → {adev.get('state')}"
            )
    for mac in set(a) - set(b):
        changes.append(f"  added: {a[mac].get('name', mac)}")
    if changes:
        lines += ["devices:", *changes]


def _diff_radios(before: list, after: list, lines: list[str]) -> None:
    b = {d.get("mac", d.get("name", "?")): d for d in before}
    a = {d.get("mac", d.get("name", "?")): d for d in after}
    changes: list[str] = []
    for key, bdev in b.items():
        adev = a.get(key)
        if adev is None:
            continue
        name = adev.get("name", key)
        b_radios = {r["radio"]: r for r in bdev.get("radio_table", [])}
        a_radios = {r["radio"]: r for r in adev.get("radio_table", [])}
        for radio, brad in b_radios.items():
            arad = a_radios.get(radio)
            if arad is None:
                continue
            band = "5GHz" if radio == "na" else "2.4GHz"
            if brad.get("channel") != arad.get("channel"):
                changes.append(
                    f"  {name} {band} channel: {brad.get('channel')!r} → {arad.get('channel')!r}"
                )
            if brad.get("tx_power_mode") != arad.get("tx_power_mode"):
                changes.append(
                    f"  {name} {band} tx_power_mode: "
                    f"{brad.get('tx_power_mode')!r} → {arad.get('tx_power_mode')!r}"
                )
    if changes:
        lines += ["radios:", *changes]
