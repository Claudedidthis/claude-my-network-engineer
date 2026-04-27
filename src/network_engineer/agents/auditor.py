"""Backward-compat shim — auditor moved to network_engineer.tools.auditor.

Per docs/agent_architecture.md (2026-04-26): the auditor is a deterministic
tool, not an agent. Its canonical path is now `network_engineer.tools.auditor`.
This shim re-exports the public API for one release so existing imports keep
working with a deprecation warning. The shim will be removed in a future release.
"""
from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "network_engineer.agents.auditor has moved to network_engineer.tools.auditor "
    "(see docs/agent_architecture.md). The agents.* path will be removed in a "
    "future release.",
    DeprecationWarning,
    stacklevel=2,
)

from network_engineer.tools.auditor import *  # noqa: F401,F403,E402
from network_engineer.tools.auditor import (  # noqa: F401,E402  re-export common imports
    run,
    run_from_client,
)
