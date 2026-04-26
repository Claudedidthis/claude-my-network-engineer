"""Permission gate — every proposed write passes through check() before execution.

Loads config/permission_model.yaml once at import time and caches it. Returns one
of three Tier values; defaults to REQUIRES_APPROVAL for any action not explicitly
listed so unknown actions are never silently auto-approved.
"""
from __future__ import annotations

import functools
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "permission_model.yaml"


class Tier(StrEnum):
    AUTO = "AUTO"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    NEVER = "NEVER"


@functools.lru_cache(maxsize=1)
def _load_model() -> dict[str, Any]:
    return yaml.safe_load(_CONFIG_PATH.read_text())


def _action_index() -> dict[str, Tier]:
    model = _load_model()
    index: dict[str, Tier] = {}
    for tier in Tier:
        for action in model.get(tier.value, {}).get("actions", []):
            index[action] = tier
    return index


@functools.lru_cache(maxsize=1)
def _cached_index() -> dict[str, Tier]:
    return _action_index()


def check(action: str) -> Tier:
    """Return the Tier for *action*.

    Any action not listed in permission_model.yaml is treated as REQUIRES_APPROVAL,
    never AUTO — unknown writes must always be explicitly approved.
    """
    return _cached_index().get(action, Tier.REQUIRES_APPROVAL)


def is_auto(action: str) -> bool:
    return check(action) is Tier.AUTO


def is_approved_required(action: str) -> bool:
    return check(action) is Tier.REQUIRES_APPROVAL


def is_never(action: str) -> bool:
    return check(action) is Tier.NEVER


def reload() -> None:
    """Force a re-read of the YAML (useful in tests that patch the config path)."""
    _load_model.cache_clear()
    _cached_index.cache_clear()
