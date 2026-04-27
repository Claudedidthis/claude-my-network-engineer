"""Backward-compat shim — reporter moved to network_engineer.tools.reporter.

Per docs/agent_architecture.md (2026-04-26): the reporter is a deterministic
template renderer. Canonical path is now `network_engineer.tools.reporter`.
"""
from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "network_engineer.agents.reporter has moved to network_engineer.tools.reporter "
    "(see docs/agent_architecture.md).",
    DeprecationWarning,
    stacklevel=2,
)

from network_engineer.tools.reporter import *  # noqa: F401,F403,E402
from network_engineer.tools.reporter import (  # noqa: F401,E402
    audit_report,
    changes_report,
    daily_report,
)
