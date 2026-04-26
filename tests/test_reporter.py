"""Tests for the Reporter agent."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from network_engineer.agents.reporter import audit_report, changes_report, daily_report
from network_engineer.tools.schemas import Finding, Severity

# ── Helpers ───────────────────────────────────────────────────────────────────

def _finding(severity: Severity, code: str = "TEST_CODE", title: str = "Test") -> Finding:
    return Finding(severity=severity, code=code, title=title, detail="Detail text.")


def _net_info(**kwargs: object) -> dict:
    base = {
        "mode": "live",
        "site_id": "abc",
        "device_count": 9,
        "client_count": 36,
        "network_count": 4,
        "network_app_version": "10.3.55",
        "hostname": "Vickers-UDM",
        "uptime_days": 34.8,
        "protect_camera_count": 5,
    }
    base.update(kwargs)
    return base


# ── audit_report ──────────────────────────────────────────────────────────────

def test_audit_report_empty_findings() -> None:
    report = audit_report([])
    assert "No findings" in report
    assert "# Network Audit Report" in report


def test_audit_report_contains_all_severities() -> None:
    findings = [
        _finding(Severity.CRITICAL, "A"),
        _finding(Severity.HIGH, "B"),
        _finding(Severity.MEDIUM, "C"),
        _finding(Severity.LOW, "D"),
        _finding(Severity.INFO, "E"),
    ]
    report = audit_report(findings)
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
        assert sev in report


def test_audit_report_severity_order() -> None:
    findings = [
        _finding(Severity.LOW, "L"),
        _finding(Severity.CRITICAL, "C"),
        _finding(Severity.HIGH, "H"),
    ]
    report = audit_report(findings)
    assert report.index("CRITICAL") < report.index("HIGH") < report.index("LOW")


def test_audit_report_finding_code_present() -> None:
    findings = [_finding(Severity.HIGH, "WIFI_CHANNEL_CONFLICT", "Ch 48 conflict")]
    report = audit_report(findings)
    assert "WIFI_CHANNEL_CONFLICT" in report
    assert "Ch 48 conflict" in report


def test_audit_report_custom_title() -> None:
    report = audit_report([], title="My Custom Report")
    assert "My Custom Report" in report


def test_audit_report_summary_counts() -> None:
    findings = [
        _finding(Severity.HIGH, "A"),
        _finding(Severity.HIGH, "B"),
        _finding(Severity.MEDIUM, "C"),
    ]
    report = audit_report(findings)
    assert "3 finding(s)" in report
    assert "HIGH: 2" in report
    assert "MEDIUM: 1" in report


def test_audit_report_actionable_banner() -> None:
    findings = [_finding(Severity.HIGH, "X")]
    report = audit_report(findings)
    assert "require attention" in report


def test_audit_report_info_no_actionable_banner() -> None:
    findings = [_finding(Severity.INFO, "X")]
    report = audit_report(findings)
    assert "require attention" not in report


def test_audit_report_evidence_rendered() -> None:
    f = Finding(
        severity=Severity.HIGH,
        code="PORT_FORWARD_SENSITIVE",
        title="FTP exposed",
        detail="Details here.",
        evidence={"port": "21", "forward_to": "192.168.1.10:21"},
    )
    report = audit_report([f])
    assert "port: 21" in report
    assert "forward_to: 192.168.1.10:21" in report


# ── daily_report ──────────────────────────────────────────────────────────────

def test_daily_report_contains_overview() -> None:
    report = daily_report([], _net_info())
    assert "10.3.55" in report
    assert "Vickers-UDM" in report
    assert "34.8" in report
    assert "36" in report


def test_daily_report_all_clear_when_no_findings() -> None:
    report = daily_report([], _net_info())
    assert "ALL CLEAR" in report


def test_daily_report_critical_health_status() -> None:
    findings = [_finding(Severity.CRITICAL, "X")]
    report = daily_report(findings, _net_info())
    assert "CRITICAL" in report


def test_daily_report_high_health_status() -> None:
    findings = [_finding(Severity.HIGH, "X")]
    report = daily_report(findings, _net_info())
    assert "HIGH PRIORITY" in report


def test_daily_report_immediate_action_section() -> None:
    findings = [_finding(Severity.HIGH, "WIFI_CHANNEL_CONFLICT", "Ch conflict")]
    report = daily_report(findings, _net_info())
    assert "Immediate Action Required" in report
    assert "WIFI_CHANNEL_CONFLICT" in report


def test_daily_report_no_changes_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daily report shows the 'no changes' message when the action log is empty."""
    import network_engineer.agents.reporter as rmod
    monkeypatch.setattr(rmod, "_ACTION_LOG", tmp_path / "empty.log")
    report = daily_report([], _net_info())
    assert "No agent-applied changes" in report


def test_daily_report_has_date() -> None:
    report = daily_report([], _net_info())
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert today in report


# ── changes_report ────────────────────────────────────────────────────────────

def test_changes_report_no_log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import network_engineer.agents.reporter as rmod
    monkeypatch.setattr(rmod, "_ACTION_LOG", tmp_path / "nonexistent.log")
    report = changes_report()
    assert "No agent activity" in report


def test_changes_report_with_applied_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import network_engineer.agents.reporter as rmod

    log_file = tmp_path / "agent_actions.log"
    ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    log_file.write_text(
        json.dumps({
            "ts": ts,
            "msg": "action_applied",
            "action": "rename_device",
            "agent": "optimizer",
            "tier": "AUTO",
        }) + "\n"
    )
    monkeypatch.setattr(rmod, "_ACTION_LOG", log_file)

    report = changes_report(days=1)
    assert "rename_device" in report
    assert "Applied Changes" in report


def test_changes_report_with_refused_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import network_engineer.agents.reporter as rmod

    log_file = tmp_path / "agent_actions.log"
    ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    log_file.write_text(
        json.dumps({
            "ts": ts,
            "msg": "action_refused",
            "action": "factory_reset_any_device",
            "agent": "orchestrator",
            "reason": "NEVER tier",
        }) + "\n"
    )
    monkeypatch.setattr(rmod, "_ACTION_LOG", log_file)

    report = changes_report(days=1)
    assert "factory_reset_any_device" in report
    assert "Refused" in report


def test_changes_report_filters_old_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import network_engineer.agents.reporter as rmod

    log_file = tmp_path / "agent_actions.log"
    old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    log_file.write_text(
        json.dumps({
            "ts": old_ts,
            "msg": "action_applied",
            "action": "rename_device",
            "agent": "optimizer",
            "tier": "AUTO",
        }) + "\n"
    )
    monkeypatch.setattr(rmod, "_ACTION_LOG", log_file)

    report = changes_report(days=1)
    assert "No agent activity" in report


# ── Live integration ──────────────────────────────────────────────────────────

_LIVE = pytest.mark.skipif(
    not os.getenv("UNIFI_HOST"), reason="UNIFI_HOST not set — live tests require home LAN"
)


@_LIVE
def test_live_daily_report() -> None:
    from network_engineer.agents.auditor import run_from_client
    from network_engineer.tools.unifi_client import UnifiClient

    client = UnifiClient(use_fixtures=False)
    findings = run_from_client(client)
    info = client.test_connection()
    report = daily_report(findings, info)

    assert "Daily Network Report" in report
    assert "10.3.55" in report  # known network app version
    assert len(report) > 500


@_LIVE
def test_live_audit_report() -> None:
    from network_engineer.agents.auditor import run_from_client
    from network_engineer.tools.unifi_client import UnifiClient

    client = UnifiClient(use_fixtures=False)
    findings = run_from_client(client)
    report = audit_report(findings)

    assert "Network Audit Report" in report
    assert "WIFI_CHANNEL_CONFLICT" in report
