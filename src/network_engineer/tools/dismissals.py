"""Dismissals — operator-confirmed suppression of specific finding patterns.

Replaces hardcoded allowlists that lived in agent code in earlier versions
(an offline-device set, an open-SSID allowlist). Those were operator-specific
facts; this is the per-fork operator-controlled mechanism that supersedes them.

When an agent produces a Finding, it consults the dismissals registry: if any
active dismissal matches the finding's code AND a value in its evidence dict,
the finding is suppressed (or downgraded with the operator's reason attached).

Storage:  config/dismissals.yaml  (gitignored, per-fork)
Example:  examples/dismissals.example.yaml  (checked in)

Mirrored to Supabase `dismissals` table in Phase 11.

Auditor / monitor / etc. should call `dismissals.matches(finding)` before
emitting; a non-None match returns the operator's reason and the agent should
either skip the finding or emit it as INFO with the reason in evidence.

TTL + auto-revocation (directive 1.4)
-------------------------------------
Every dismissal has an effective expiry. Entries missing `expires_at` on
disk are assigned a default 90-day TTL at load time (with a warning). The
runtime contract: `matches()` returns None for any expired or
fingerprint-mismatched dismissal. Operators see "dismissal stale —
re-confirm?" surfaced via `stale_dismissals()` so the suppression doesn't
silently disappear.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from network_engineer.tools.logging_setup import get_logger
from network_engineer.tools.schemas import Dismissal

log = get_logger("tools.dismissals")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PATH = _REPO_ROOT / "config" / "dismissals.yaml"

# Default TTL applied to legacy entries that don't carry an expires_at.
# Conservative: long enough not to surprise existing operators, short
# enough that genuine stale state surfaces within a quarter.
_DEFAULT_TTL = timedelta(days=90)


@dataclass(frozen=True)
class StaleDismissal:
    """One expired-or-revoked dismissal, surfaced to the auditor.

    The auditor emits an INFO-severity finding from each StaleDismissal so
    the operator is prompted to either renew or remove the entry — rather
    than the suppression silently disappearing.
    """
    dismissal: Dismissal
    reason: str  # "expired" | "fingerprint_mismatch"
    expired_at: datetime | None = None


def fingerprint_target(stable_attributes: dict[str, Any]) -> str:
    """Compute a stable fingerprint of a dismissal's target attributes.

    Algorithm: sha256(json.dumps(sorted)). Versioned via
    Dismissal.target_fingerprint_alg so the algorithm can evolve.
    """
    canonical = json.dumps(stable_attributes, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256-v1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DismissalRegistry:
    """In-memory dismissals with YAML persistence and finding-match logic."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH
        self.dismissals: list[Dismissal] = []
        # IDs (positional) auto-revoked during this session — surface as
        # stale-with-reason="fingerprint_mismatch" via stale_dismissals().
        self._auto_revoked: dict[int, datetime] = {}

    @classmethod
    def load(cls, path: Path | None = None) -> DismissalRegistry:
        registry = cls(path=path)
        if not registry.path.exists():
            return registry

        raw = yaml.safe_load(registry.path.read_text()) or {}
        for entry in raw.get("dismissals", []):
            d = Dismissal(**entry)
            if d.expires_at is None:
                # Legacy entry with no expiry → assign default TTL relative
                # to created_at and surface a one-time warning so operators
                # know to add explicit expiries on next edit.
                d = d.model_copy(update={
                    "expires_at": d.created_at + _DEFAULT_TTL,
                })
                log.warning(
                    "dismissal_missing_expires_at_defaulted",
                    extra={
                        "agent": "dismissals",
                        "finding_code": d.finding_code,
                        "match_field": d.match_field,
                        "match_key": d.match_key,
                        "default_expires_at": d.expires_at.isoformat(),
                        "ttl_days": _DEFAULT_TTL.days,
                    },
                )
            registry.dismissals.append(d)
        return registry

    def matches(
        self,
        finding_code: str,
        evidence: dict[str, Any] | None = None,
        *,
        live_target_attributes: dict[str, Any] | None = None,
    ) -> Dismissal | None:
        """Return the first active dismissal that matches this finding, or None.

        A dismissal is *inactive* (and ignored here) when it has expired,
        when it has been auto-revoked due to a fingerprint mismatch, or
        when `reconfirm_on_change=True` and `live_target_attributes` hash
        differently than the captured `target_fingerprint`.

        `live_target_attributes` is the caller-supplied dict of stable
        attributes for the target — when not provided, fingerprint
        reconfirmation is skipped (the dismissal still applies if not
        expired). Pass it to enforce auto-revocation.
        """
        ev = evidence or {}
        now = datetime.now(UTC)
        for idx, d in enumerate(self.dismissals):
            if d.finding_code != finding_code:
                continue
            if d.expires_at is not None and d.expires_at <= now:
                continue
            if idx in self._auto_revoked:
                continue
            ev_value = ev.get(d.match_field)
            if ev_value is None:
                continue
            if str(ev_value).lower() != d.match_key.lower():
                continue

            # Match on code + evidence. Now apply fingerprint reconfirmation
            # if requested and live attributes were supplied.
            if (
                d.reconfirm_on_change
                and d.target_fingerprint is not None
                and live_target_attributes is not None
            ):
                live_fp = fingerprint_target(live_target_attributes)
                if live_fp != d.target_fingerprint:
                    log.warning(
                        "dismissal_auto_revoked_fingerprint_mismatch",
                        extra={
                            "agent": "dismissals",
                            "finding_code": d.finding_code,
                            "match_key": d.match_key,
                            "captured_fingerprint": d.target_fingerprint,
                            "live_fingerprint": live_fp,
                        },
                    )
                    self._auto_revoked[idx] = now
                    continue
            return d
        return None

    def stale_dismissals(self) -> list[StaleDismissal]:
        """Return every dismissal that is currently inactive due to expiry
        or auto-revocation, with the reason. The auditor surfaces these as
        INFO-severity findings so the operator is prompted to re-confirm
        rather than the suppression silently disappearing."""
        now = datetime.now(UTC)
        out: list[StaleDismissal] = []
        for idx, d in enumerate(self.dismissals):
            if idx in self._auto_revoked:
                out.append(StaleDismissal(
                    dismissal=d,
                    reason="fingerprint_mismatch",
                    expired_at=self._auto_revoked[idx],
                ))
                continue
            if d.expires_at is not None and d.expires_at <= now:
                out.append(StaleDismissal(
                    dismissal=d,
                    reason="expired",
                    expired_at=d.expires_at,
                ))
        return out

    def add(self, dismissal: Dismissal) -> None:
        self.dismissals.append(dismissal)

    def save(self) -> None:
        if not self.dismissals:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "dismissals": [
                d.model_dump(mode="json", exclude_none=True)
                for d in self.dismissals
            ],
        }
        self.path.write_text(
            yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=120),
        )

    def __len__(self) -> int:
        return len(self.dismissals)
