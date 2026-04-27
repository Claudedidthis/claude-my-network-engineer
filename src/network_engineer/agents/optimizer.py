"""Backward-compat shim — optimizer moved to network_engineer.tools.optimizer.

Per docs/agent_architecture.md (2026-04-26): the optimizer is a deterministic
tool (apply / verify / rollback), not an agent. Its canonical path is now
`network_engineer.tools.optimizer`. This shim re-exports the public API for
one release.
"""
from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "network_engineer.agents.optimizer has moved to network_engineer.tools.optimizer "
    "(see docs/agent_architecture.md).",
    DeprecationWarning,
    stacklevel=2,
)

from network_engineer.tools.optimizer import *  # noqa: F401,F403,E402
from network_engineer.tools.optimizer import (  # noqa: F401,E402
    OptimizerError,
    OptimizerResult,
    apply_change,
    rename_device,
    resolve_channel_conflicts,
)
