"""Agents — LLM-driven runtime loops.

After the architectural redesign on 2026-04-26 (see docs/agent_architecture.md),
this package contains only modules where an LLM is in the driver's seat:

  • ai_runtime    — service (RPC wrapper around Anthropic). Stays.
  • orchestrator  — placeholder for the Conductor (lands in migration step 5).
  • onboarding_agent — current form-runner. Deprecated when Conductor lands.
  • registry_agent  — current form-runner. Deprecated when Conductor lands.
  • security_agent  — hybrid (tool data + LLM reasoning). Refactored when
                      Conductor lands.

Plus backward-compat shims (auditor.py / optimizer.py / monitor.py /
reporter.py / upgrade_agent.py) that re-export from network_engineer.tools
with a DeprecationWarning. These will be removed in a future release.
"""
