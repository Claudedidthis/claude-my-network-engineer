"""Structured JSON-line logging for all agent activity.

Four log files under logs/:
  agent_actions.log        — every write the agent applies (AUTO actions)
  recommendations.log      — REQUIRES_APPROVAL proposals waiting for human sign-off
  upgrade_recommendations.log — device EOL / firmware upgrade suggestions (Phase 9)
  errors.log               — unexpected exceptions from any agent

Each line is a self-contained JSON object with at minimum:
  ts        ISO-8601 timestamp
  agent     agent name (orchestrator / auditor / optimizer / etc.)
  event     short event type tag
  + additional fields per event type

Call configure_logging() once at startup (CLI entry point or FastAPI lifespan).
Agents call get_logger(name) to get a pre-configured logger.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOGS_DIR = Path(__file__).resolve().parents[3] / "logs"

# Log file names
ACTION_LOG = "agent_actions.log"
RECOMMENDATION_LOG = "recommendations.log"
UPGRADE_LOG = "upgrade_recommendations.log"
ERROR_LOG = "errors.log"


class _JsonLineFormatter(logging.Formatter):
    """Render each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Any extra fields attached via logger.info("...", extra={...})
        for key, val in record.__dict__.items():
            if key not in (
                "args", "created", "exc_info", "exc_text", "filename", "funcName",
                "levelname", "levelno", "lineno", "message", "module", "msecs",
                "msg", "name", "pathname", "process", "processName", "relativeCreated",
                "stack_info", "thread", "threadName", "taskName",
            ):
                payload[key] = val
        return json.dumps(payload, default=str)


def _file_handler(filename: str, level: int = logging.DEBUG) -> logging.FileHandler:
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(_LOGS_DIR / filename)
    h.setLevel(level)
    h.setFormatter(_JsonLineFormatter())
    return h


def configure_logging(verbose: bool = False) -> None:
    """Wire up all log files. Call once at program startup."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Avoid double-adding handlers if called more than once
    if root.handlers:
        return

    # Console — human-readable, INFO+ by default
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
    )
    root.addHandler(console)

    # agent_actions.log — INFO+ from agent namespaces
    action_h = _file_handler(ACTION_LOG, logging.INFO)
    action_h.addFilter(lambda r: "agent" in r.name or "orchestrator" in r.name)
    root.addHandler(action_h)

    # recommendations.log — WARNING+ (recommendations are emitted at WARNING level)
    root.addHandler(_file_handler(RECOMMENDATION_LOG, logging.WARNING))

    # errors.log — ERROR+
    root.addHandler(_file_handler(ERROR_LOG, logging.ERROR))

    # upgrade_recommendations.log — dedicated logger, INFO+
    upgrade_h = _file_handler(UPGRADE_LOG, logging.INFO)
    upgrade_h.addFilter(lambda r: r.name == "network_engineer.agents.upgrade")
    root.addHandler(upgrade_h)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the network_engineer namespace."""
    if not name.startswith("network_engineer"):
        name = f"network_engineer.{name}"
    return logging.getLogger(name)


def log_action(
    agent: str,
    action: str,
    detail: dict[str, Any] | None = None,
    *,
    tier: str = "AUTO",
) -> None:
    """Convenience: write a structured action entry to agent_actions.log."""
    logger = get_logger(f"agents.{agent}")
    logger.info(
        "action_applied",
        extra={
            "agent": agent,
            "action": action,
            "tier": tier,
            "detail": detail or {},
        },
    )


def log_recommendation(
    agent: str,
    action: str,
    proposal: dict[str, Any],
) -> None:
    """Write a REQUIRES_APPROVAL proposal to recommendations.log."""
    logger = get_logger(f"agents.{agent}")
    logger.warning(
        "approval_required",
        extra={
            "agent": agent,
            "action": action,
            "tier": "REQUIRES_APPROVAL",
            "proposal": proposal,
        },
    )


def log_refused(agent: str, action: str, reason: str = "NEVER tier") -> None:
    """Write a refusal entry when an agent hits a NEVER action."""
    logger = get_logger(f"agents.{agent}")
    logger.error(
        "action_refused",
        extra={
            "agent": agent,
            "action": action,
            "tier": "NEVER",
            "reason": reason,
        },
    )
