"""Conductor debug logging — JSONL trace of every API boundary, decision, fold.

The Conductor's interaction with Anthropic + the operator is multi-step
and tightly coupled. When something goes wrong (a 400, an unexpected
parse, a runaway tool loop), the only durable record I can use to debug
is what landed in this log.

Path: logs/conductor_debug.jsonl  (gitignored, local-only)

Events captured:
  session_start          session_id, env (model_alias, max_turns)
  api_request_pre_call   full messages payload + tools count, just before
                          .messages.create. If this is the LAST entry in
                          a session that crashed, the next event would
                          have been the response — so the request itself
                          is what was malformed.
  api_response_received  full response content blocks parsed
  api_request_failed     the request payload + Anthropic's error body so
                          we can see exactly what was rejected and why
  decision_emitted       each AgentDecision returned to the loop
  fold_appended          what was added to api_messages on a fold
  queue_drained          when a queued decision is returned without an API call
  tool_call              tool name, args (truncated), result (truncated)

Default ON. Disable with `CONDUCTOR_DEBUG=0` env var if logs get too
large. No PII protection — these logs are LOCAL-ONLY (logs/ is
gitignored). The pre-push leak detector verifies nothing in logs/ ever
reaches a public commit.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOG_PATH = _REPO_ROOT / "logs" / "conductor_debug.jsonl"

# Session id is set once per Conductor.run() and used by every log_event call
# from that point until the next session_start.
_session_id_holder: dict[str, str | None] = {"id": None}


def set_session_id(session_id: str) -> None:
    _session_id_holder["id"] = session_id


def is_enabled() -> bool:
    """Default ON (debug-friendly). Disable with CONDUCTOR_DEBUG=0."""
    val = os.getenv("CONDUCTOR_DEBUG", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def log_event(event: str, payload: dict[str, Any] | None = None) -> None:
    """Append one event to logs/conductor_debug.jsonl. Never raises."""
    if not is_enabled():
        return
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "session_id": _session_id_holder["id"],
        }
        if payload:
            entry.update(payload)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=_safe_default) + "\n")
    except Exception:
        # Debug logging must never break the agent. Swallow.
        pass


def _safe_default(obj: Any) -> Any:
    """JSON fallback for non-serializable objects (datetimes, Pydantic, etc.)."""
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json")
        except Exception:
            pass
    return repr(obj)[:500]


def truncate_messages_for_log(
    messages: list[dict[str, Any]],
    max_block_chars: int = 800,
) -> list[dict[str, Any]]:
    """Shrink messages list for log entry — full structure, truncated content.

    Each text/tool_result content gets capped at max_block_chars. Structure
    (roles, tool_use_ids, block types) is preserved intact so we can see
    exactly what shape was sent.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            shortened = content if len(content) <= max_block_chars else content[:max_block_chars] + f"...[+{len(content) - max_block_chars} chars]"
            out.append({"role": m.get("role"), "content": shortened})
            continue
        if isinstance(content, list):
            out_blocks: list[dict[str, Any]] = []
            for b in content:
                if not isinstance(b, dict):
                    out_blocks.append({"non_dict_block": repr(b)[:200]})
                    continue
                block = dict(b)
                # Truncate text content blocks
                for key in ("text", "content"):
                    val = block.get(key)
                    if isinstance(val, str) and len(val) > max_block_chars:
                        block[key] = val[:max_block_chars] + f"...[+{len(val) - max_block_chars} chars]"
                out_blocks.append(block)
            out.append({"role": m.get("role"), "content": out_blocks})
            continue
        out.append({"role": m.get("role"), "content": repr(content)[:200]})
    return out


def get_log_path() -> Path:
    return _LOG_PATH
