"""Backward-compat shim — monitor moved to network_engineer.tools.monitor.

Per docs/agent_architecture.md (2026-04-26): the monitor is a deterministic
tool (polling loop with thresholds), not an agent. Canonical path is now
`network_engineer.tools.monitor`.
"""
from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "network_engineer.agents.monitor has moved to network_engineer.tools.monitor "
    "(see docs/agent_architecture.md).",
    DeprecationWarning,
    stacklevel=2,
)

from network_engineer.tools.monitor import *  # noqa: F401,F403,E402
from network_engineer.tools.monitor import (  # noqa: F401,E402
    run,
    run_from_client,
    watch,
)
