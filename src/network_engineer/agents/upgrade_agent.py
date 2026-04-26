"""Upgrade Agent — scores devices against the upgrade catalog and emits recommendations.

Read-only. Runs a weekly sweep (or on-demand via `nye upgrade scan`):

  1. Load `config/upgrade_catalog.yaml` (EOL flags, Wi-Fi gen, successors)
  2. Pull live device list via UnifiClient.get_devices()
  3. For each device, look up its catalog entry and score it
       eol             +40
       aging           +15
       has_successor   +20
       (multiplied by 1.2 when device serves >10 active clients)
  4. Filter: keep candidates with score >= 15 (configurable)
  5. Optionally augment each with an AIRuntime.score_upgrade_recommendation
     narrative (Haiku 4.5)
  6. Emit UpgradeRecommendation entries to logs/upgrade_recommendations.log

Severity bands (from the catalog YAML):
    score >= 70 → HIGH
    score >= 40 → MEDIUM
    score >= 15 → LOW
    score <  15 → no recommendation emitted
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from network_engineer.agents.ai_runtime import AIRuntime
from network_engineer.tools.logging_setup import get_logger
from network_engineer.tools.schemas import Severity, UpgradeRecommendation

log = get_logger("agents.upgrade")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CATALOG = _REPO_ROOT / "config" / "upgrade_catalog.yaml"


# ── Catalog loading ───────────────────────────────────────────────────────────

def load_catalog(path: Path | None = None) -> dict[str, Any]:
    """Load and return the upgrade catalog YAML."""
    return yaml.safe_load((path or _DEFAULT_CATALOG).read_text())


def _lookup_entry(catalog: dict[str, Any], model: str) -> dict[str, Any] | None:
    """Find a catalog entry for *model*. Tries exact match then `match_models` list."""
    if not model:
        return None
    entries = catalog.get("catalog", [])

    # Exact `model` match
    for entry in entries:
        if entry.get("model") == model:
            return entry

    # Match list (alternate model strings)
    for entry in entries:
        if model in entry.get("match_models", []):
            return entry

    return None


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_device(
    device: dict[str, Any],
    catalog: dict[str, Any],
    *,
    client_count: int = 0,
) -> tuple[int, dict[str, int], dict[str, Any] | None]:
    """Compute (score, factors, catalog_entry) for one device.

    Returns score=0, factors={}, entry=None when the device has no catalog entry —
    callers treat this as "unknown — no recommendation".
    """
    weights = catalog.get("weights", {})
    entry = _lookup_entry(catalog, device.get("model", ""))
    if entry is None:
        return 0, {}, None

    factors: dict[str, int] = {}
    if entry.get("eol"):
        factors["eol"] = int(weights.get("eol", 40))
    if entry.get("aging"):
        factors["aging"] = int(weights.get("aging", 15))
    if entry.get("successor"):
        factors["has_successor"] = int(weights.get("has_successor", 20))

    raw = sum(factors.values())

    # High-traffic multiplier
    multiplier = float(weights.get("high_traffic_mult", 1.2))
    if client_count > 10 and multiplier > 1.0:
        factored = int(raw * multiplier)
        factors["high_traffic_multiplier_pct"] = int((multiplier - 1.0) * 100)
        raw = factored

    score = min(100, raw)
    return score, factors, entry


def _severity_for_score(score: int, catalog: dict[str, Any]) -> Severity:
    bands = catalog.get("severity_bands", {})
    if score >= int(bands.get("HIGH", 70)):
        return Severity.HIGH
    if score >= int(bands.get("MEDIUM", 40)):
        return Severity.MEDIUM
    if score >= int(bands.get("LOW", 15)):
        return Severity.LOW
    return Severity.INFO


def _recommendation_kind(entry: dict[str, Any]) -> str:
    if entry.get("eol"):
        return "replace_device"
    if entry.get("successor"):
        return "replace_device"
    return "monitor"


def _build_reason(entry: dict[str, Any], factors: dict[str, int]) -> str:
    parts: list[str] = []
    if "eol" in factors:
        eol_date = entry.get("eol_date", "an earlier date")
        parts.append(f"reached end-of-life on {eol_date}")
    if "aging" in factors:
        parts.append("hardware generation is behind the current line")
    if "has_successor" in factors:
        succ = entry.get("successor")
        cost = entry.get("successor_msrp_usd")
        if cost:
            parts.append(f"successor available: {succ} (~${cost})")
        else:
            parts.append(f"successor available: {succ}")
    if entry.get("notes"):
        parts.append(entry["notes"])
    return ". ".join(parts) + "." if parts else "Catalog flagged for review."


# ── Client count per device ──────────────────────────────────────────────────

def _count_clients_per_device(clients: list[dict[str, Any]]) -> Counter[str]:
    """Map AP MAC → number of clients currently associated with it."""
    counts: Counter[str] = Counter()
    for c in clients:
        ap_mac = c.get("ap_mac") or c.get("apMac") or c.get("uplinkMac")
        if ap_mac:
            counts[ap_mac.lower()] += 1
    return counts


# ── Main entry point ──────────────────────────────────────────────────────────

def scan(
    client: Any,
    *,
    runtime: AIRuntime | None = None,
    catalog: dict[str, Any] | None = None,
) -> list[UpgradeRecommendation]:
    """Run the upgrade sweep and return all recommendations above the emit threshold."""
    cat = catalog if catalog is not None else load_catalog()
    devices = client.get_devices()
    try:
        clients_list = client.get_clients()
    except Exception:
        clients_list = []
    return _scan_with_data(devices, clients_list, cat, runtime=runtime)


def _scan_with_data(
    devices: list[dict[str, Any]],
    clients_list: list[dict[str, Any]],
    catalog: dict[str, Any],
    *,
    runtime: AIRuntime | None = None,
) -> list[UpgradeRecommendation]:
    """Score and emit recommendations for an in-memory device list. Used by tests."""
    threshold = int(catalog.get("weights", {}).get("threshold_emit", 15))
    client_counts = _count_clients_per_device(clients_list)

    if runtime is None:
        runtime = AIRuntime()

    results: list[UpgradeRecommendation] = []
    for device in devices:
        device_mac = (device.get("macAddress") or device.get("mac") or "").lower()
        score, factors, entry = score_device(
            device, catalog, client_count=client_counts.get(device_mac, 0),
        )
        if entry is None or score < threshold:
            continue

        severity = _severity_for_score(score, catalog)
        kind = _recommendation_kind(entry)
        reason = _build_reason(entry, factors)

        narrative = ""
        ai_score: int | None = None
        if runtime.enabled:
            candidate_payload = {
                "device": {
                    "name": device.get("name"),
                    "model": device.get("model"),
                    "state": device.get("state"),
                    "firmware": device.get("firmware") or device.get("version") or "",
                },
                "catalog_entry": entry,
                "deterministic_score": score,
                "factors": factors,
            }
            try:
                ai_result = runtime.score_upgrade_recommendation(candidate_payload)
                narrative = str(ai_result.get("narrative", ""))
                ai_score = ai_result.get("score")
            except Exception as exc:
                log.warning(
                    "upgrade_ai_narrative_failed",
                    extra={"agent": "upgrade", "error": str(exc),
                           "device": device.get("name")},
                )

        rec = UpgradeRecommendation(
            device_id=device.get("id") or device_mac or device.get("name", "unknown"),
            device_name=device.get("name", "unknown"),
            device_model=device.get("model", ""),
            current_firmware=device.get("firmware") or device.get("version") or "",
            recommendation=kind,
            reason=reason,
            urgency=severity,
            score=score,
            factors=factors,
            successor_model=entry.get("successor"),
            successor_msrp_usd=entry.get("successor_msrp_usd"),
            narrative=narrative,
        )
        results.append(rec)

        # Log to upgrade_recommendations.log via the dedicated logger
        log.info(
            "upgrade_candidate",
            extra={
                "agent": "upgrade",
                "action": "upgrade_recommendation",
                "device": rec.device_name,
                "model": rec.device_model,
                "score": rec.score,
                "urgency": rec.urgency,
                "factors": rec.factors,
                "successor": rec.successor_model,
                "successor_msrp_usd": rec.successor_msrp_usd,
                "ai_score": ai_score,
                "ai_narrative_present": bool(narrative),
            },
        )

    # Sort by score descending
    results.sort(key=lambda r: r.score, reverse=True)
    log.info(
        "upgrade_sweep_complete",
        extra={
            "agent": "upgrade",
            "action": "sweep_complete",
            "total_devices": len(devices),
            "candidates": len(results),
            "ai_enabled": runtime.enabled,
        },
    )
    return results


# ── Markdown rendering ────────────────────────────────────────────────────────

_SEVERITY_ICON = {
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "🔵",
    Severity.INFO: "⚪",
    Severity.CRITICAL: "🔴",
}


def render_markdown(recs: list[UpgradeRecommendation]) -> str:
    """Render a sweep result as readable markdown."""
    if not recs:
        return "_No upgrade candidates — every catalogued device is current._\n"

    lines: list[str] = ["# Upgrade Recommendations", ""]
    lines.append(f"_{len(recs)} candidate(s) detected._")
    lines.append("")
    lines.append("| Score | Urgency | Device | Model | Successor | Cost |")
    lines.append("|---:|:--:|--------|-------|-----------|-----:|")
    for r in recs:
        icon = _SEVERITY_ICON.get(r.urgency, "")
        cost = f"${r.successor_msrp_usd}" if r.successor_msrp_usd else "—"
        lines.append(
            f"| **{r.score}** | {icon} {r.urgency} | {r.device_name} | "
            f"`{r.device_model}` | {r.successor_model or '—'} | {cost} |"
        )
    lines.append("")
    for r in recs:
        lines.append(f"## {_SEVERITY_ICON.get(r.urgency, '')} {r.device_name} — score {r.score}")
        lines.append(f"_Model: `{r.device_model}` → recommendation: **{r.recommendation}**_")
        lines.append("")
        lines.append(r.reason)
        if r.narrative:
            lines += ["", "_AI narrative:_  " + r.narrative]
        if r.factors:
            factor_parts = ", ".join(f"{k}=+{v}" for k, v in r.factors.items())
            lines += ["", f"Score factors: {factor_parts}"]
        lines.append("")
    return "\n".join(lines)


def to_json_log_format(recs: list[UpgradeRecommendation]) -> list[dict[str, Any]]:
    """Render the sweep as a JSON-serializable list (matches upgrade_recommendations.log shape)."""
    return [json.loads(r.model_dump_json()) for r in recs]
