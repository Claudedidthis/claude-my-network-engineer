#!/usr/bin/env python3
"""Generate snapshots/INDEX.md — an ordered, annotated log of every snapshot.

Scans snapshots/ for *.json (config + full backups) and *.unifi (native UniFi OS
backups). Parses the timestamp from each filename, looks up correlated action-log
entries (snapshot_before / snapshot_after fields in agent_actions.log), and writes
a markdown table sorted descending (newest first).

Re-run anytime: it overwrites INDEX.md from the current contents of snapshots/.

Usage:
    .venv/bin/python scripts/snapshot_index.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SNAPS_DIR = _REPO_ROOT / "snapshots"
_INDEX_FILE = _SNAPS_DIR / "INDEX.md"
_ACTION_LOG = _REPO_ROOT / "logs" / "agent_actions.log"

# Timestamp pattern: 2026-04-25_073148  (date_HHMMSS at the start of filename)
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})")


def _classify(name: str) -> str:
    if name.endswith("_full_backup.json"):
        return "full backup"
    if name.endswith("_unifi_os_backup.unifi"):
        return "UniFi OS backup"
    if name.endswith("_snapshot.json"):
        return "config snapshot"
    return "other"


def _parse_timestamp(name: str) -> datetime | None:
    m = _TS_RE.match(name)
    if not m:
        # Try date-only files (e.g. unifi OS backup)
        date_only = re.match(r"^(\d{4}-\d{2}-\d{2})", name)
        if date_only:
            return datetime.fromisoformat(date_only.group(1) + "T00:00:00")
        return None
    date, hh, mm, ss = m.groups()
    return datetime.fromisoformat(f"{date}T{hh}:{mm}:{ss}")


def _load_action_log() -> list[dict]:
    if not _ACTION_LOG.exists():
        return []
    entries = []
    for line in _ACTION_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _annotations_for(filename: str, log_entries: list[dict]) -> list[str]:
    """Find action log entries whose snapshot_before/snapshot_after points to this file."""
    notes: list[str] = []
    for entry in log_entries:
        detail = entry.get("detail", {})
        if not isinstance(detail, dict):
            continue
        if filename in str(detail.get("snapshot_before", "")):
            notes.append(f"BEFORE: {entry.get('action', '?')}")
        if filename in str(detail.get("snapshot_after", "")):
            notes.append(f"AFTER:  {entry.get('action', '?')}")
    return notes


def _human_size(bytes_: int) -> str:
    if bytes_ < 1024:
        return f"{bytes_} B"
    if bytes_ < 1024 * 1024:
        return f"{bytes_ / 1024:.1f} KB"
    return f"{bytes_ / 1024 / 1024:.2f} MB"


def main() -> int:
    if not _SNAPS_DIR.exists():
        print(f"ERROR: {_SNAPS_DIR} does not exist", file=sys.stderr)
        return 1

    files = [
        p for p in _SNAPS_DIR.iterdir()
        if p.is_file() and p.name not in ("INDEX.md", ".gitkeep")
        and not p.name.startswith(".")
    ]
    if not files:
        print(f"No snapshot files in {_SNAPS_DIR}")
        return 0

    log_entries = _load_action_log()

    rows = []
    for p in files:
        ts = _parse_timestamp(p.name)
        rows.append({
            "ts": ts,
            "name": p.name,
            "size": p.stat().st_size,
            "kind": _classify(p.name),
            "notes": _annotations_for(p.name, log_entries),
        })
    # Sort descending (newest first); files with no parsed timestamp go last
    rows.sort(key=lambda r: (r["ts"] is None, -(r["ts"].timestamp() if r["ts"] else 0)))

    lines: list[str] = [
        "# Snapshot Index",
        "",
        f"_Generated: {datetime.now(UTC).isoformat(timespec='seconds')}_  ·  "
        f"{len(rows)} file(s) totalling "
        f"{_human_size(sum(r['size'] for r in rows))}",
        "",
        "Sorted newest → oldest. Re-generate anytime: "
        "`.venv/bin/python scripts/snapshot_index.py`",
        "",
        "| # | When | Type | Size | File | Action context |",
        "|--:|------|------|-----:|------|----------------|",
    ]
    for i, r in enumerate(rows, start=1):
        when = r["ts"].isoformat() if r["ts"] else "—"
        notes = "; ".join(r["notes"]) if r["notes"] else ""
        lines.append(
            f"| {i} | `{when}` | {r['kind']} | {_human_size(r['size'])} | "
            f"`{r['name']}` | {notes} |"
        )

    lines += ["", "## Legend", "",
              "- **config snapshot** — pre/post-change config dump from `nye test --snapshot` "
              "or the Optimizer's safety pipeline.",
              "- **full backup** — every readable endpoint (deeper than a snapshot).",
              "- **UniFi OS backup** — native `.unifi` backup file from the UDM web UI.",
              "- **Action context** — agent_actions.log entries that referenced this file as "
              "their `snapshot_before` or `snapshot_after`.",
              ""]

    _INDEX_FILE.write_text("\n".join(lines))
    print(f"Wrote {_INDEX_FILE} ({len(rows)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
