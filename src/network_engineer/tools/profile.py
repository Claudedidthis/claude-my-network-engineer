"""Household profile loader/saver — single-row YAML for now, Supabase-mirrored later.

The profile captures situated context about the household: who lives there, what
the network is for, what the operator cares about, what the physical layout looks
like. Every other agent consults this to tune thresholds, severity bands, and
narrative tone.

Storage:  config/household_profile.yaml  (gitignored, per-fork)
Example:  examples/household_profile.example.yaml  (checked in)
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from network_engineer.tools.schemas import HouseholdProfile

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PATH = _REPO_ROOT / "config" / "household_profile.yaml"


def load_profile(path: Path | None = None) -> HouseholdProfile | None:
    """Return the household profile, or None if no profile has been captured yet."""
    p = path or _DEFAULT_PATH
    if not p.exists():
        return None
    raw = yaml.safe_load(p.read_text()) or {}
    if not raw:
        return None
    return HouseholdProfile(**raw)


def save_profile(profile: HouseholdProfile, path: Path | None = None) -> Path:
    """Write the profile to YAML. Bumps updated_at."""
    p = path or _DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    profile.updated_at = datetime.now(UTC)
    data = profile.model_dump(mode="json", exclude_none=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=120))
    return p


def has_profile(path: Path | None = None) -> bool:
    """True if a profile has been captured."""
    return (path or _DEFAULT_PATH).exists()
