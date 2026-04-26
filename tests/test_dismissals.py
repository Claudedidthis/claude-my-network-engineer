"""Tests for the dismissals registry — TTL, fingerprint auto-revocation,
stale-dismissal surfacing (directive 1.4)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from network_engineer.tools.dismissals import (
    DismissalRegistry,
    StaleDismissal,
    fingerprint_target,
)
from network_engineer.tools.schemas import Dismissal


# ── Fixture writers ─────────────────────────────────────────────────────────

def _write_dismissals(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"dismissals": entries}))


# ── Match: basic active case ────────────────────────────────────────────────

def test_active_dismissal_matches_finding(tmp_path: Path) -> None:
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "WIFI_NO_ENCRYPTION",
        "match_field": "ssid",
        "match_key": "Guest_Open",
        "reason": "Intentional captive portal",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
    }])
    reg = DismissalRegistry.load(path=p)
    match = reg.matches("WIFI_NO_ENCRYPTION", {"ssid": "Guest_Open"})
    assert match is not None
    assert match.reason == "Intentional captive portal"


def test_match_is_case_insensitive(tmp_path: Path) -> None:
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "DEVICE_OFFLINE",
        "match_field": "name",
        "match_key": "Spare-Camera",
        "reason": "intentionally off",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
    }])
    reg = DismissalRegistry.load(path=p)
    match = reg.matches("DEVICE_OFFLINE", {"name": "spare-camera"})
    assert match is not None


def test_no_match_for_different_code(tmp_path: Path) -> None:
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "DEVICE_OFFLINE",
        "match_field": "name", "match_key": "X",
        "reason": "r",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
    }])
    reg = DismissalRegistry.load(path=p)
    assert reg.matches("WIFI_NO_ENCRYPTION", {"name": "X"}) is None


# ── TTL expiry ──────────────────────────────────────────────────────────────

def test_expired_dismissal_does_not_match(tmp_path: Path) -> None:
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "WIFI_NO_ENCRYPTION",
        "match_field": "ssid", "match_key": "Old_SSID",
        "reason": "expired entry",
        "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    }])
    reg = DismissalRegistry.load(path=p)
    assert reg.matches("WIFI_NO_ENCRYPTION", {"ssid": "Old_SSID"}) is None


def test_dismissal_expiring_in_future_matches(tmp_path: Path) -> None:
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "WIFI_NO_ENCRYPTION",
        "match_field": "ssid", "match_key": "Future",
        "reason": "still active",
        "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    }])
    reg = DismissalRegistry.load(path=p)
    assert reg.matches("WIFI_NO_ENCRYPTION", {"ssid": "Future"}) is not None


def test_legacy_entry_without_expires_at_gets_default_ttl(tmp_path: Path) -> None:
    """Backwards-compatibility: entries without expires_at receive a 90-day
    TTL relative to created_at at load time and a warning is emitted."""
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "WIFI_NO_ENCRYPTION",
        "match_field": "ssid", "match_key": "Legacy",
        "reason": "old yaml",
        "created_at": datetime.now(UTC).isoformat(),
        # no expires_at
    }])
    reg = DismissalRegistry.load(path=p)
    assert len(reg.dismissals) == 1
    d = reg.dismissals[0]
    assert d.expires_at is not None
    # Should be ~90 days from created_at
    delta = d.expires_at - d.created_at
    assert 89 <= delta.days <= 91


def test_legacy_entry_default_ttl_relative_to_created_at(tmp_path: Path) -> None:
    """If created_at is in the past, expires_at = created_at + 90d may
    already be expired — and matches() correctly filters it out."""
    p = tmp_path / "dismissals.yaml"
    long_ago = datetime.now(UTC) - timedelta(days=200)
    _write_dismissals(p, [{
        "finding_code": "WIFI_NO_ENCRYPTION",
        "match_field": "ssid", "match_key": "Ancient",
        "reason": "from 2024",
        "created_at": long_ago.isoformat(),
    }])
    reg = DismissalRegistry.load(path=p)
    # Default TTL was 90 days, created_at was 200 days ago → expired now
    assert reg.matches("WIFI_NO_ENCRYPTION", {"ssid": "Ancient"}) is None


# ── Stale-dismissal surfacing ───────────────────────────────────────────────

def test_stale_dismissals_surfaces_expired_entries(tmp_path: Path) -> None:
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [
        {
            "finding_code": "WIFI_NO_ENCRYPTION",
            "match_field": "ssid", "match_key": "ActiveOne",
            "reason": "still active",
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
        {
            "finding_code": "DEVICE_OFFLINE",
            "match_field": "name", "match_key": "ExpiredOne",
            "reason": "stale",
            "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        },
    ])
    reg = DismissalRegistry.load(path=p)
    stale = reg.stale_dismissals()
    assert len(stale) == 1
    assert stale[0].dismissal.match_key == "ExpiredOne"
    assert stale[0].reason == "expired"
    assert isinstance(stale[0], StaleDismissal)


def test_stale_dismissals_empty_when_none_expired(tmp_path: Path) -> None:
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "WIFI_NO_ENCRYPTION",
        "match_field": "ssid", "match_key": "Active",
        "reason": "ok",
        "expires_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
    }])
    reg = DismissalRegistry.load(path=p)
    assert reg.stale_dismissals() == []


# ── Fingerprint auto-revocation ─────────────────────────────────────────────

def test_fingerprint_target_is_stable_across_key_order() -> None:
    f1 = fingerprint_target({"name": "G4 Pro", "mac": "aa:bb:cc:dd:ee:ff"})
    f2 = fingerprint_target({"mac": "aa:bb:cc:dd:ee:ff", "name": "G4 Pro"})
    assert f1 == f2


def test_fingerprint_changes_on_attribute_change() -> None:
    f1 = fingerprint_target({"name": "G4 Pro", "model": "G4"})
    f2 = fingerprint_target({"name": "G4 Pro", "model": "G4-PRO-2"})
    assert f1 != f2


def test_fingerprint_includes_versioned_algorithm() -> None:
    f = fingerprint_target({"x": 1})
    assert f.startswith("sha256-v1:")


def test_reconfirm_on_change_auto_revokes_when_attributes_change(tmp_path: Path) -> None:
    """Live attributes hash differently than captured fingerprint → dismissal
    is auto-revoked, matches() returns None, stale_dismissals() surfaces it."""
    captured_fp = fingerprint_target({"model": "G4 Pro", "firmware": "1.0.0"})
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "DEVICE_OFFLINE",
        "match_field": "name", "match_key": "G4 Pro",
        "reason": "Backup unit, intentionally off",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "target_fingerprint": captured_fp,
        "reconfirm_on_change": True,
    }])
    reg = DismissalRegistry.load(path=p)

    # Live state: firmware changed → fingerprint differs → auto-revoke
    live_attrs = {"model": "G4 Pro", "firmware": "2.0.0"}
    match = reg.matches(
        "DEVICE_OFFLINE",
        {"name": "G4 Pro"},
        live_target_attributes=live_attrs,
    )
    assert match is None

    stale = reg.stale_dismissals()
    assert len(stale) == 1
    assert stale[0].reason == "fingerprint_mismatch"


def test_reconfirm_on_change_passes_when_attributes_match(tmp_path: Path) -> None:
    captured_fp = fingerprint_target({"model": "G4 Pro", "firmware": "1.0.0"})
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "DEVICE_OFFLINE",
        "match_field": "name", "match_key": "G4 Pro",
        "reason": "Backup unit",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "target_fingerprint": captured_fp,
        "reconfirm_on_change": True,
    }])
    reg = DismissalRegistry.load(path=p)

    live_attrs = {"model": "G4 Pro", "firmware": "1.0.0"}
    match = reg.matches(
        "DEVICE_OFFLINE",
        {"name": "G4 Pro"},
        live_target_attributes=live_attrs,
    )
    assert match is not None


def test_reconfirm_off_skips_fingerprint_check_even_when_diverged(tmp_path: Path) -> None:
    """When reconfirm_on_change=False (default), fingerprint divergence
    does not cause revocation. Operator opts in explicitly."""
    captured_fp = fingerprint_target({"model": "G4 Pro", "firmware": "1.0.0"})
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "DEVICE_OFFLINE",
        "match_field": "name", "match_key": "G4 Pro",
        "reason": "Backup unit",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "target_fingerprint": captured_fp,
        "reconfirm_on_change": False,
    }])
    reg = DismissalRegistry.load(path=p)
    match = reg.matches(
        "DEVICE_OFFLINE",
        {"name": "G4 Pro"},
        live_target_attributes={"model": "G4 Pro", "firmware": "999"},
    )
    assert match is not None


def test_reconfirm_skipped_when_live_attributes_not_supplied(tmp_path: Path) -> None:
    """Caller didn't pass live_target_attributes → fingerprint check skipped,
    dismissal still active. This is the legacy code path; doesn't break."""
    captured_fp = fingerprint_target({"model": "G4 Pro"})
    p = tmp_path / "dismissals.yaml"
    _write_dismissals(p, [{
        "finding_code": "DEVICE_OFFLINE",
        "match_field": "name", "match_key": "G4 Pro",
        "reason": "Backup",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "target_fingerprint": captured_fp,
        "reconfirm_on_change": True,
    }])
    reg = DismissalRegistry.load(path=p)
    # No live_target_attributes → fingerprint check skipped → still active
    match = reg.matches("DEVICE_OFFLINE", {"name": "G4 Pro"})
    assert match is not None


# ── Persistence round-trip ─────────────────────────────────────────────────

def test_save_and_reload_round_trips_new_fields(tmp_path: Path) -> None:
    """target_fingerprint, reconfirm_on_change, target_fingerprint_alg
    survive a save/load cycle."""
    p = tmp_path / "dismissals.yaml"
    reg = DismissalRegistry(path=p)
    fp = fingerprint_target({"model": "G4 Pro"})
    reg.add(Dismissal(
        finding_code="DEVICE_OFFLINE",
        match_field="name", match_key="G4 Pro",
        reason="Backup",
        expires_at=datetime.now(UTC) + timedelta(days=30),
        target_fingerprint=fp,
        reconfirm_on_change=True,
    ))
    reg.save()

    reloaded = DismissalRegistry.load(path=p)
    assert len(reloaded) == 1
    d = reloaded.dismissals[0]
    assert d.target_fingerprint == fp
    assert d.reconfirm_on_change is True
    assert d.target_fingerprint_alg == "sha256-v1"
