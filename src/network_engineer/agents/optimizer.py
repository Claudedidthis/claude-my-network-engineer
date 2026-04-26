"""Optimizer agent — applies AUTO-tier network changes through the full safety pipeline.

Every change follows the §9 safety checklist:
  1. Permission check  — action must be AUTO tier (raises OptimizerError otherwise)
  2. Snapshot before   — config snapshot written to snapshots/
  3. Apply             — exactly one write to the controller
  4. Wait              — let the UDM propagate the change
  5. Verify            — re-read live state and confirm the change took effect
  6. Snapshot after    — second snapshot for the diff record
  7. Log               — structured entry to agent_actions.log

On verify failure: log the failure, restore the original value, return status="rolled_back".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from network_engineer.tools.authorization import auto_authorize
from network_engineer.tools.logging_setup import get_logger, log_action
from network_engineer.tools.permissions import Tier, check

log = get_logger("agents.optimizer")


class OptimizerError(RuntimeError):
    """Raised when an operation cannot proceed."""


@dataclass
class OptimizerResult:
    action: str
    status: str                             # "applied" | "rolled_back" | "failed"
    snapshot_before: Path
    snapshot_after: Path | None = None
    rolled_back: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# ── Apply / verify / rollback dispatch ───────────────────────────────────────

def _lookup_device(client: Any, name: str) -> dict[str, Any]:
    """Find a device_stats entry by display name or raise OptimizerError."""
    for dev in client.get_device_stats():
        if dev.get("name") == name:
            return dev
    raise OptimizerError(f"Device not found: {name!r}")


def _authorize_apply(
    action: str, params: dict[str, Any], snapshot_id: str | None = None,
) -> Any:
    """Mint a fresh AUTO authorization for the upcoming write call.

    The payload shape MUST match what the matching client method derives
    inside _consume_authorization — see unifi_client.py for each method's
    expected_payload. Any drift here will surface as an
    UnauthorizedWriteError at the client boundary, not silent acceptance.
    """
    if action == "rename_device":
        payload = {"device_id": params["device_id"], "name": params["name"]}
    elif action == "set_ap_channel_5ghz":
        payload = {
            "device_id": params["device_id"], "radio": "na",
            "channel": str(params["channel"]),
        }
    elif action == "set_ap_channel_2_4ghz":
        payload = {
            "device_id": params["device_id"], "radio": "ng",
            "channel": str(params["channel"]),
        }
    elif action == "set_ap_tx_power":
        payload = {
            "device_id": params["device_id"],
            "radio": params["radio"],
            "tx_power_mode": params["tx_power_mode"],
        }
        if params.get("tx_power") is not None:
            payload["tx_power"] = params["tx_power"]
    elif action == "restart_offline_ap":
        payload = {"mac": params["mac"]}
    else:
        raise OptimizerError(f"No payload extractor for action: {action!r}")
    return auto_authorize(
        action=action,
        payload=payload,
        approved_by="optimizer",
        source_snapshot_id=snapshot_id,
    )


def _do_apply(
    client: Any, action: str, params: dict[str, Any], snapshot_id: str | None = None,
) -> None:
    auth = _authorize_apply(action, params, snapshot_id=snapshot_id)
    if action == "rename_device":
        client.set_device_name(params["device_id"], params["name"], authorization=auth)
    elif action == "set_ap_channel_5ghz":
        client.set_ap_channel(
            params["device_id"], "na", params["channel"], authorization=auth,
        )
    elif action == "set_ap_channel_2_4ghz":
        client.set_ap_channel(
            params["device_id"], "ng", params["channel"], authorization=auth,
        )
    elif action == "set_ap_tx_power":
        client.set_ap_tx_power(
            params["device_id"], params["radio"], params["tx_power_mode"],
            params.get("tx_power"), authorization=auth,
        )
    elif action == "restart_offline_ap":
        client.restart_device(params["mac"], authorization=auth)
    else:
        raise OptimizerError(f"No apply handler for action: {action!r}")


def _do_verify(client: Any, action: str, params: dict[str, Any]) -> tuple[bool, str]:
    """Re-read live state; return (verified, human-readable note)."""
    if action == "rename_device":
        device_id = params["device_id"]
        expected = params["name"]
        for dev in client.get_device_stats():
            if dev.get("_id") == device_id:
                actual = dev.get("name")
                if actual == expected:
                    return True, f"name confirmed: {actual!r}"
                return False, f"name is still {actual!r}, expected {expected!r}"
        return False, f"device {device_id} not found after rename"

    if action in ("set_ap_channel_5ghz", "set_ap_channel_2_4ghz"):
        radio = "na" if action == "set_ap_channel_5ghz" else "ng"
        device_id = params["device_id"]
        expected = str(params["channel"])
        for dev in client.get_device_stats():
            if dev.get("_id") != device_id:
                continue
            for r in dev.get("radio_table", []):
                if r.get("radio") == radio:
                    actual = str(r.get("channel", ""))
                    if actual == expected:
                        return True, f"channel confirmed: {actual}"
                    return False, f"channel is {actual!r}, expected {expected!r}"
        return False, f"device {device_id} or radio {radio!r} not found after channel set"

    if action == "set_ap_tx_power":
        device_id = params["device_id"]
        radio = params["radio"]
        expected_mode = params["tx_power_mode"]
        for dev in client.get_device_stats():
            if dev.get("_id") != device_id:
                continue
            for r in dev.get("radio_table", []):
                if r.get("radio") == radio:
                    actual = r.get("tx_power_mode")
                    if actual == expected_mode:
                        return True, f"tx_power_mode confirmed: {actual!r}"
                    return False, f"tx_power_mode is {actual!r}, expected {expected_mode!r}"
        return False, "device or radio not found"

    if action == "restart_offline_ap":
        # Restart takes 30+ s — we mark it advisory and verify manually
        return True, "restart command accepted (confirm device comes back online)"

    return False, f"No verify handler for action: {action!r}"


def _do_rollback(client: Any, action: str, params: dict[str, Any]) -> None:
    """Reverse a change. Called only on verify failure.

    Rollback is itself a write, so it mints a fresh authorization scoped
    to the *original* payload. Re-using the forward authorization would
    fail (single-use + payload-hash mismatch), which is the desired
    invariant — the client refuses replay.
    """
    if action == "rename_device":
        original = params.get("original_name")
        if original:
            rb_params = {"device_id": params["device_id"], "name": original}
            auth = auto_authorize(
                action="rename_device", payload=rb_params,
                approved_by="optimizer.rollback",
            )
            client.set_device_name(params["device_id"], original, authorization=auth)
    elif action == "set_ap_channel_5ghz":
        original = params.get("original_channel")
        if original:
            rb_params = {
                "device_id": params["device_id"], "radio": "na",
                "channel": str(original),
            }
            auth = auto_authorize(
                action="set_ap_channel_5ghz", payload=rb_params,
                approved_by="optimizer.rollback",
            )
            client.set_ap_channel(
                params["device_id"], "na", original, authorization=auth,
            )
    elif action == "set_ap_channel_2_4ghz":
        original = params.get("original_channel")
        if original:
            rb_params = {
                "device_id": params["device_id"], "radio": "ng",
                "channel": str(original),
            }
            auth = auto_authorize(
                action="set_ap_channel_2_4ghz", payload=rb_params,
                approved_by="optimizer.rollback",
            )
            client.set_ap_channel(
                params["device_id"], "ng", original, authorization=auth,
            )
    elif action == "set_ap_tx_power":
        original = params.get("original_tx_power_mode")
        if original:
            rb_params = {
                "device_id": params["device_id"],
                "radio": params["radio"],
                "tx_power_mode": original,
            }
            auth = auto_authorize(
                action="set_ap_tx_power", payload=rb_params,
                approved_by="optimizer.rollback",
            )
            client.set_ap_tx_power(
                params["device_id"], params["radio"], original, authorization=auth,
            )
    # restart_offline_ap: cannot be undone — no rollback


# ── Main safety pipeline ──────────────────────────────────────────────────────

def apply_change(
    client: Any,
    action: str,
    params: dict[str, Any],
    *,
    rationale: str = "",
    verify_wait_s: int = 5,
) -> OptimizerResult:
    """Execute a single AUTO-tier change through the full §9 safety pipeline."""
    tier = check(action)
    if tier is not Tier.AUTO:
        raise OptimizerError(
            f"Action {action!r} is {tier} — only AUTO actions may be applied directly. "
            "Use the orchestrator approval workflow for REQUIRES_APPROVAL actions."
        )

    snapshot_before = client.snapshot()
    log.info(
        "optimizer_snapshot_before",
        extra={"agent": "optimizer", "action": "snapshot_before", "path": str(snapshot_before)},
    )

    try:
        _do_apply(client, action, params, snapshot_id=snapshot_before.stem)
    except Exception as exc:
        log.error(
            "optimizer_apply_failed",
            extra={"agent": "optimizer", "action": action, "error": str(exc)},
        )
        return OptimizerResult(
            action=action, status="failed", snapshot_before=snapshot_before,
            detail={"error": str(exc), "rationale": rationale},
        )

    time.sleep(verify_wait_s)

    verified, verify_note = _do_verify(client, action, params)
    if not verified:
        log.warning(
            "optimizer_verify_failed",
            extra={"agent": "optimizer", "action": action, "note": verify_note},
        )
        try:
            _do_rollback(client, action, params)
            log.warning(
                "optimizer_rolled_back",
                extra={"agent": "optimizer", "action": action},
            )
        except Exception as rb_exc:
            log.error(
                "optimizer_rollback_failed",
                extra={"agent": "optimizer", "action": action, "error": str(rb_exc)},
            )
        log_action(
            "optimizer", action,
            {"status": "rolled_back", "verify_note": verify_note, "rationale": rationale},
            tier=tier.value,
        )
        return OptimizerResult(
            action=action, status="rolled_back", snapshot_before=snapshot_before,
            rolled_back=True,
            detail={"verify_note": verify_note, "rationale": rationale},
        )

    snapshot_after = client.snapshot()
    log_action(
        "optimizer", action,
        {
            "status": "applied", "verify_note": verify_note, "rationale": rationale,
            "snapshot_before": str(snapshot_before), "snapshot_after": str(snapshot_after),
        },
        tier=tier.value,
    )
    log.info(
        "optimizer_applied",
        extra={"agent": "optimizer", "action": action, "verify_note": verify_note},
    )
    return OptimizerResult(
        action=action, status="applied",
        snapshot_before=snapshot_before, snapshot_after=snapshot_after,
        detail={"verify_note": verify_note, "rationale": rationale},
    )


# ── High-level tasks ──────────────────────────────────────────────────────────

def rename_device(client: Any, device_name: str, new_name: str) -> OptimizerResult:
    """Rename a device by its current display name. AUTO tier."""
    dev = _lookup_device(client, device_name)
    device_id = dev.get("_id")
    if not device_id:
        raise OptimizerError(f"Device {device_name!r} has no _id in device_stats")
    return apply_change(
        client,
        "rename_device",
        {
            "device_id": device_id,
            "name": new_name,
            "original_name": device_name,
            "mac": dev.get("mac"),
        },
        rationale=f"Rename {device_name!r} → {new_name!r}",
        verify_wait_s=3,
    )


def resolve_channel_conflicts(client: Any) -> list[OptimizerResult]:
    """Detect channel conflicts via the auditor and fix each one. AUTO tier."""
    from network_engineer.agents.auditor import run as audit_run

    snapshot_data = {
        "devices": client.get_devices(),
        "device_stats": client.get_device_stats(),
        "clients": client.get_clients(),
        "wifi_networks": client.get_wifi_networks(),
        "firewall_rules": client.get_firewall_rules(),
        "port_forwards": client.get_port_forwards(),
        "settings": client.get_settings(),
    }
    findings = audit_run(snapshot_data)
    conflicts = [f for f in findings if f.code == "WIFI_CHANNEL_CONFLICT"]

    if not conflicts:
        log.info(
            "optimizer_no_conflicts",
            extra={"agent": "optimizer", "action": "channel_scan"},
        )
        return []

    results: list[OptimizerResult] = []
    for finding in conflicts:
        ev = finding.evidence or {}
        band = ev.get("band", "5GHz")
        conflicting_channel_str = ev.get("channel", "")
        ap_names = ev.get("access_points", [])
        first_ap = ap_names[0] if ap_names else None

        if not first_ap or not conflicting_channel_str:
            continue

        try:
            dev = _lookup_device(client, first_ap)
        except OptimizerError as exc:
            log.warning(
                "optimizer_device_not_found",
                extra={"agent": "optimizer", "error": str(exc)},
            )
            continue

        device_id = dev.get("_id")
        if not device_id:
            continue

        radio = "na" if band == "5GHz" else "ng"
        try:
            conflicting_channel = int(conflicting_channel_str)
        except (ValueError, TypeError):
            continue

        new_channel = _pick_non_conflicting_channel(client, band, conflicting_channel, first_ap)
        action = "set_ap_channel_5ghz" if band == "5GHz" else "set_ap_channel_2_4ghz"
        original = _current_channel(dev, radio)

        result = apply_change(
            client,
            action,
            {
                "device_id": device_id,
                "channel": new_channel,
                "original_channel": original,
            },
            rationale=(
                f"Resolve {band} ch{conflicting_channel} conflict detected by auditor — "
                f"moving {first_ap} to ch{new_channel}"
            ),
            verify_wait_s=15,
        )
        results.append(result)

    return results


def _current_channel(dev: dict[str, Any], radio: str) -> str:
    for r in dev.get("radio_table", []):
        if r.get("radio") == radio:
            return str(r.get("channel", "auto"))
    return "auto"


def _pick_non_conflicting_channel(
    client: Any, band: str, conflicting: int, exclude_device: str
) -> int:
    """Pick the least-used non-conflicting channel for the given band."""
    radio = "na" if band == "5GHz" else "ng"
    in_use: set[int] = set()
    for dev in client.get_device_stats():
        if dev.get("name") == exclude_device:
            continue
        for r in dev.get("radio_table", []):
            if r.get("radio") == radio:
                ch = r.get("channel_actual") or r.get("channel")
                try:
                    in_use.add(int(ch))
                except (TypeError, ValueError):
                    pass

    candidates = [36, 40, 44, 48, 149, 153, 157, 161] if band == "5GHz" else [1, 6, 11]
    for ch in candidates:
        if ch != conflicting and ch not in in_use:
            return ch
    return next((ch for ch in candidates if ch != conflicting), candidates[0])
