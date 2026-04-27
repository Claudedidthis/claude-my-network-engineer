"""Backward-compat shim — upgrade_agent moved to network_engineer.tools.upgrade_agent.

Per docs/agent_architecture.md (2026-04-26): upgrade scoring is a deterministic
catalog tool. Canonical path is now `network_engineer.tools.upgrade_agent`.
"""
from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "network_engineer.agents.upgrade_agent has moved to "
    "network_engineer.tools.upgrade_agent (see docs/agent_architecture.md).",
    DeprecationWarning,
    stacklevel=2,
)

from network_engineer.tools.upgrade_agent import *  # noqa: F401,F403,E402
