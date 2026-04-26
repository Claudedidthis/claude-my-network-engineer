"""Orchestrator — single entry point for all agent tasks.

Every request passes through here before any sub-agent runs. The orchestrator:
  1. Checks the action against the permission model (AUTO / REQUIRES_APPROVAL / NEVER)
  2. For NEVER: logs the refusal and raises PermissionDenied
  3. For REQUIRES_APPROVAL: logs the proposal to recommendations.log and returns
     a pending result — the agent does NOT proceed until a human explicitly approves
  4. For AUTO: delegates to the correct sub-agent, which must snapshot → apply →
     verify → log

Sub-agents are stubbed in Phase 2 and wired up in later phases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from network_engineer.tools.logging_setup import (
    get_logger,
    log_action,
    log_recommendation,
    log_refused,
)
from network_engineer.tools.permissions import Tier, check

log = get_logger("agents.orchestrator")


class PermissionDeniedError(RuntimeError):
    """Raised when an action is in the NEVER tier."""


class ApprovalRequiredError(RuntimeError):
    """Raised when an action requires human approval before proceeding."""

    def __init__(self, action: str, proposal: dict[str, Any]) -> None:
        super().__init__(f"Action '{action}' requires human approval.")
        self.action = action
        self.proposal = proposal


@dataclass
class TaskResult:
    action: str
    tier: Tier
    status: str                          # "applied" | "pending_approval" | "refused" | "stubbed"
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


# ── Sub-agent dispatch stubs (wired up in later phases) ──────────────────────

_AGENT_ROUTES: dict[str, str] = {
    # action_prefix → agent name (for routing logic in later phases)
    "rename_device": "optimizer",
    "update_device_description": "optimizer",
    "set_ap_channel": "optimizer",
    "set_ap_tx_power": "optimizer",
    "set_band_steering": "optimizer",
    "restart_offline_ap": "optimizer",
    "block_known_client": "security",
    "unblock_known_client": "security",
    "update_dns_assignment": "optimizer",
    "tag_client": "auditor",
}


def _route_agent(action: str) -> str:
    for prefix, agent in _AGENT_ROUTES.items():
        if action.startswith(prefix):
            return agent
    return "orchestrator"


def _dispatch_stub(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Placeholder dispatch — returns a stub result until agents are implemented."""
    agent = _route_agent(action)
    log.info(
        "stub_dispatch",
        extra={"action": action, "routed_to": agent, "params": params},
    )
    return {"stub": True, "routed_to": agent, "params": params}


# ── Main entry point ──────────────────────────────────────────────────────────

def run(task: dict[str, Any]) -> TaskResult:
    """Execute *task* through the full permission + dispatch pipeline.

    task must contain:
      "action"  — the action name (must match permission_model.yaml)
      "params"  — dict of parameters for the sub-agent (may be empty)
      "agent"   — optional, name of the requesting agent (for audit log)

    Raises:
      PermissionDenied   — action is in the NEVER tier
      ApprovalRequired   — action requires human sign-off (also logged)
      ValueError         — task dict is malformed
    """
    action = task.get("action")
    if not action:
        raise ValueError("task must have an 'action' key")

    params: dict[str, Any] = task.get("params", {})
    requesting_agent: str = task.get("agent", "unknown")

    tier = check(action)
    log.debug("permission_check", extra={"action": action, "tier": tier, "agent": requesting_agent})

    if tier is Tier.NEVER:
        log_refused(requesting_agent, action)
        raise PermissionDeniedError(
            f"Action '{action}' is in the NEVER tier and cannot be executed. "
            "See config/permission_model.yaml."
        )

    if tier is Tier.REQUIRES_APPROVAL:
        proposal = {
            "action": action,
            "params": params,
            "requested_by": requesting_agent,
            "requested_at": datetime.now(UTC).isoformat(),
            "current_state": task.get("current_state"),
            "rationale": task.get("rationale", ""),
            "rollback_plan": task.get("rollback_plan", ""),
        }
        log_recommendation(requesting_agent, action, proposal)
        raise ApprovalRequiredError(action, proposal)

    # AUTO — dispatch to sub-agent
    detail = _dispatch_stub(action, params)
    log_action(requesting_agent, action, detail, tier=tier.value)

    return TaskResult(action=action, tier=tier, status="stubbed", detail=detail)


def run_approved(task: dict[str, Any]) -> TaskResult:
    """Execute a task that has already received human approval.

    Same as run() but skips the REQUIRES_APPROVAL gate. The action must still
    not be in the NEVER tier. Used by the approval workflow in Phase 10+.
    """
    action = task.get("action")
    if not action:
        raise ValueError("task must have an 'action' key")

    params: dict[str, Any] = task.get("params", {})
    requesting_agent: str = task.get("agent", "unknown")

    tier = check(action)

    if tier is Tier.NEVER:
        log_refused(requesting_agent, action, reason="NEVER tier — even with approval")
        raise PermissionDeniedError(
            f"Action '{action}' is in the NEVER tier. Human approval cannot override this."
        )

    detail = _dispatch_stub(action, params)
    log_action(requesting_agent, action, detail, tier=tier.value)
    return TaskResult(action=action, tier=tier, status="stubbed", detail=detail)
