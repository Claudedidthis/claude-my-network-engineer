"""Reporter agent — formats audit findings and agent activity into readable markdown.

Three report types:
  audit   — full findings list from the Auditor, grouped by severity
  daily   — one-page network health summary: overview + finding digest + recent changes
  changes — change log parsed from agent_actions.log

All report functions return a markdown string. The CLI writes to stdout or --out FILE.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from network_engineer.tools.logging_setup import get_logger
from network_engineer.tools.schemas import Finding, Severity

log = get_logger("agents.reporter")

_LOGS_DIR = Path(__file__).resolve().parents[3] / "logs"
_ACTION_LOG = _LOGS_DIR / "agent_actions.log"

_ICON = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH:     "🟠",
    Severity.MEDIUM:   "🟡",
    Severity.LOW:      "🔵",
    Severity.INFO:     "⚪",
}
_SEVERITY_ORDER = [
    Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO,
]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _severity_counts(findings: list[Finding]) -> dict[Severity, int]:
    counts: dict[Severity, int] = {s: 0 for s in Severity}
    for f in findings:
        counts[f.severity] += 1
    return counts


def _summary_line(counts: dict[Severity, int]) -> str:
    parts = [
        f"{_ICON[s]} {s}: {counts[s]}"
        for s in _SEVERITY_ORDER
        if counts[s] > 0
    ]
    return "  ".join(parts) if parts else "✅ No findings"


def _read_action_log(since: datetime | None = None) -> list[dict[str, Any]]:
    """Parse agent_actions.log; optionally filter to entries after *since*."""
    if not _ACTION_LOG.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in _ACTION_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since:
            ts_str = entry.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts < since:
                    continue
            except ValueError:
                pass
        entries.append(entry)
    return entries


def _finding_block(f: Finding) -> str:
    lines = [
        f"### {_ICON[f.severity]} `{f.code}`",
        f"**{f.title}**",
        "",
        f.detail,
    ]
    if f.evidence:
        lines += ["", "```"]
        for k, v in f.evidence.items():
            lines.append(f"{k}: {v}")
        lines.append("```")
    return "\n".join(lines)


# ── Report generators ─────────────────────────────────────────────────────────

def audit_report(findings: list[Finding], *, title: str = "Network Audit Report") -> str:
    """Full findings list grouped by severity."""
    counts = _severity_counts(findings)
    total = len(findings)

    lines: list[str] = [
        f"# {title}",
        f"_Generated: {_now_str()}_",
        "",
        "## Summary",
        "",
        f"**{total} finding(s)**   {_summary_line(counts)}",
        "",
    ]

    if not findings:
        lines += ["✅ No findings — network looks clean.", ""]
        return "\n".join(lines)

    actionable = [f for f in findings if f.is_actionable()]
    if actionable:
        lines += [
            f"> **{len(actionable)} finding(s) require attention** "
            f"(CRITICAL / HIGH / MEDIUM).",
            "",
        ]

    for severity in _SEVERITY_ORDER:
        group = [f for f in findings if f.severity == severity]
        if not group:
            continue
        lines += [
            "---",
            "",
            f"## {_ICON[severity]} {severity} ({len(group)})",
            "",
        ]
        for f in group:
            lines += [_finding_block(f), ""]

    return "\n".join(lines)


def daily_report(
    findings: list[Finding],
    network_info: dict[str, Any],
) -> str:
    """One-page daily health summary."""
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    counts = _severity_counts(findings)

    # Network overview section
    version = network_info.get("network_app_version", "unknown")
    hostname = network_info.get("hostname", "unknown")
    uptime = network_info.get("uptime_days", "?")
    devices = network_info.get("device_count", "?")
    clients = network_info.get("client_count", "?")
    networks = network_info.get("network_count", "?")
    cameras = network_info.get("protect_camera_count", "?")

    # Health status line
    if counts[Severity.CRITICAL] > 0:
        health_icon, health_label = "🔴", "CRITICAL ISSUES"
    elif counts[Severity.HIGH] > 0:
        health_icon, health_label = "🟠", "HIGH PRIORITY FINDINGS"
    elif counts[Severity.MEDIUM] > 0:
        health_icon, health_label = "🟡", "MEDIUM FINDINGS"
    else:
        health_icon, health_label = "✅", "ALL CLEAR"

    # Recent changes (last 24 h)
    since = datetime.now(UTC) - timedelta(hours=24)
    recent_actions = _read_action_log(since=since)
    applied = [e for e in recent_actions if e.get("msg") == "action_applied"]

    lines: list[str] = [
        f"# Daily Network Report — {date_str}",
        f"_Generated: {_now_str()}_",
        "",
        "---",
        "",
        "## Network Overview",
        "",
        "| | |",
        "|---|---|",
        f"| **Controller** | UniFi Network {version} |",
        f"| **Host** | {hostname} |",
        f"| **Uptime** | {uptime} days |",
        f"| **Devices** | {devices} online |",
        f"| **Clients** | {clients} connected |",
        f"| **Networks** | {networks} |",
        f"| **Cameras** | {cameras} (Protect) |",
        "",
        "---",
        "",
        "## Health Status",
        "",
        f"{health_icon} **{health_label}**   {_summary_line(counts)}",
        "",
    ]

    # Immediate action items
    critical_high = [f for f in findings if f.severity in (Severity.CRITICAL, Severity.HIGH)]
    if critical_high:
        lines += [
            "### Immediate Action Required",
            "",
        ]
        for f in critical_high:
            lines.append(f"- {_ICON[f.severity]} **{f.code}** — {f.title}")
        lines.append("")

    medium = [f for f in findings if f.severity == Severity.MEDIUM]
    if medium:
        lines += ["### Worth Investigating", ""]
        for f in medium:
            lines.append(f"- {_ICON[f.severity]} **{f.code}** — {f.title}")
        lines.append("")

    # Recent changes
    lines += [
        "---",
        "",
        "## Recent Changes (last 24 h)",
        "",
    ]
    if not applied:
        lines += ["_No agent-applied changes in the last 24 hours._", ""]
    else:
        for entry in applied:
            ts = entry.get("ts", "?")[:16]
            action = entry.get("action", "?")
            agent = entry.get("agent", "?")
            lines.append(f"- `{ts}` **{action}** by {agent}")
        lines.append("")

    lines += [
        "---",
        "",
        "_Full audit: run `nye audit` or `nye report audit`_",
        "",
    ]
    return "\n".join(lines)


def changes_report(days: int = 7) -> str:
    """Change log from agent_actions.log for the last *days* days."""
    since = datetime.now(UTC) - timedelta(days=days)
    entries = _read_action_log(since=since)
    applied = [e for e in entries if e.get("msg") == "action_applied"]
    refused = [e for e in entries if e.get("msg") == "action_refused"]
    pending = [e for e in entries if e.get("msg") == "approval_required"]

    lines: list[str] = [
        f"# Change Log — Last {days} Days",
        f"_Generated: {_now_str()}_",
        "",
        "| Applied | Refused | Pending Approval |",
        "|---|---|---|",
        f"| {len(applied)} | {len(refused)} | {len(pending)} |",
        "",
    ]

    if applied:
        lines += ["## Applied Changes", ""]
        for e in applied:
            ts = e.get("ts", "?")[:16]
            lines.append(
                f"- `{ts}` **{e.get('action', '?')}** "
                f"by {e.get('agent', '?')} [{e.get('tier', '?')}]"
            )
        lines.append("")

    if refused:
        lines += ["## Refused Actions", ""]
        for e in refused:
            ts = e.get("ts", "?")[:16]
            lines.append(
                f"- `{ts}` ~~{e.get('action', '?')}~~ — {e.get('reason', 'NEVER tier')}"
            )
        lines.append("")

    if pending:
        lines += ["## Pending Approval", ""]
        for e in pending:
            ts = e.get("ts", "?")[:16]
            lines.append(f"- `{ts}` **{e.get('action', '?')}** awaiting human sign-off")
        lines.append("")

    if not applied and not refused and not pending:
        lines += ["_No agent activity recorded in this period._", ""]

    return "\n".join(lines)
