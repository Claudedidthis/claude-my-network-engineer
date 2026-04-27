"""AIRuntime → AgentLLM adapter.

The agent loop primitive (tools/agent_loop.py) declares a thin AgentLLM
Protocol: `decide(...)` returns an AgentDecision given the loop state.
This module wraps the existing AIRuntime (Anthropic API) to satisfy
that protocol.

What the adapter does
---------------------

  1. Builds a messages.create payload with:
       - System block: Conductor system prompt + cached
       - Cached context block: durable memory subset (untrusted-data tagged)
       - Cached working memory: rolling 12-turn replay
       - Session summary as a plain text user-role message
       - Tools: every ToolSpec exposed via Anthropic tool-use shape

  2. Calls AIRuntime, parses the response.

  3. Translates the response to one AgentDecision:
       - tool_use(name="speak", text=...)            → SpeakDecision
       - tool_use(name="ask_operator", question=...) → AskDecision
       - tool_use(name="save_fact", ...)             → SaveFactDecision
       - tool_use(name="log_decision", entry=...)    → LogDecisionDecision
       - tool_use(name="done_for_now", ...)          → DoneDecision
       - tool_use(name=<other tool>, args=...)       → CallToolDecision
       - text-only (no tool_use)                     → SpeakDecision

We expose `speak`, `ask_operator`, `save_fact`, `log_decision`, and
`done_for_now` as virtual tools to the LLM (alongside the real tools).
This gives the model a uniform "I'm always emitting a tool_use" shape,
which is more reliable than mixing text+tool_use parsing.

What the adapter does NOT do
----------------------------

  • Implement specific tools — those live in conductor_tools.py.
  • Execute the chosen tool — the agent loop does that.
  • Manage memory — the loop owns Working/Session/Durable.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from network_engineer.agents.ai_runtime import AIRuntime
from network_engineer.tools.agent_loop import (
    AgentDecision,
    AskDecision,
    CallToolDecision,
    DoneDecision,
    LogDecisionDecision,
    SaveFactDecision,
    SpeakDecision,
    ToolSpec,
    Turn,
)

log = logging.getLogger("agents.conductor_llm")


# ── Virtual tools — the loop's decision kinds presented as Anthropic tools ──


_VIRTUAL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "speak",
        "description": "Say something to the operator that does NOT expect a reply. "
                       "Use for statements, summaries, explanations, opening greetings. "
                       "If your message contains a question mark or asks the operator "
                       "anything, use ask_operator instead — speak does not block for "
                       "a response, the loop proceeds to your next decision. After a "
                       "speak the operator may still interject (their typed input "
                       "becomes the next user turn); but never rely on that — questions "
                       "go via ask_operator, period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "ask_operator",
        "description": "Ask the operator a question and BLOCK until their reply lands. "
                       "Use for any message expecting a response — questions, "
                       "confirmations, follow-ups. The loop pauses; the operator's "
                       "answer becomes the next user turn the model sees.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "save_fact",
        "description": "Save a fact to durable memory with confidence and evidence. "
                       "Required: field_path (dotted path the router knows), value, "
                       "confidence (0-1), evidence (list of source citations).",
        "input_schema": {
            "type": "object",
            "properties": {
                "field_path": {"type": "string"},
                "value": {},  # any
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
            },
            "required": ["field_path", "value", "confidence", "evidence"],
        },
    },
    {
        "name": "log_decision",
        "description": "Append a structured entry to the durable decision log "
                       "(why-you-did-what trace). Used for non-trivial choices "
                       "the operator may want to revisit later.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entry": {"type": "object"},
                "rationale": {"type": "string"},
            },
            "required": ["entry"],
        },
    },
    {
        "name": "done_for_now",
        "description": "End the session. Session digest will be written, durable "
                       "memory checkpoints. Use when the operator signs off or "
                       "the discrete task is complete.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "rationale": {"type": "string"},
            },
        },
    },
]


_VIRTUAL_TOOL_NAMES = {t["name"] for t in _VIRTUAL_TOOLS}


# ── Adapter ─────────────────────────────────────────────────────────────────


class AIRuntimeAgentLLM:
    """Wraps AIRuntime to satisfy the AgentLLM protocol.

    The Conductor instantiates this with its AIRuntime + a `model_alias`
    ("opus" or "sonnet") indicating which model to use for agent turns.
    Per existing AIRuntime conventions, "opus" is reserved for high-stakes
    work — the Conductor uses "sonnet" by default and escalates per tool
    call as the existing review_change pattern does (sensitive actions
    bump to opus).
    """

    def __init__(
        self,
        ai_runtime: AIRuntime,
        *,
        model_alias: str = "sonnet",
        max_tokens: int = 2048,
    ) -> None:
        self.ai = ai_runtime
        self.model_alias = model_alias
        self.max_tokens = max_tokens
        self._disabled_message_emitted = False

    def decide(
        self,
        *,
        system_prompt: str,
        working_memory: list[Turn],
        session_summary: str,
        durable_subset: str,
        tools: dict[str, ToolSpec],
    ) -> AgentDecision:
        """Build payload, call Anthropic, parse one AgentDecision."""
        if not self.ai.enabled:
            # First disabled turn: explain to the operator. Second: exit.
            # Two turns total so the SpeakDecision lands via on_say before
            # DoneDecision tears down the loop.
            if not self._disabled_message_emitted:
                self._disabled_message_emitted = True
                return SpeakDecision(
                    text="AI runtime is disabled (set AI_RUNTIME_ENABLED=true and "
                         "ANTHROPIC_API_KEY to enable). Without an LLM I can't drive "
                         "this conversation. Exiting.",
                )
            return DoneDecision(reason="AI runtime disabled")

        anthropic_tools = self._build_anthropic_tools(tools)
        messages = self._build_messages(working_memory, session_summary)

        # Resolve model_id from AIRuntime config
        model_id = self.ai._config["models"].get(self.model_alias)
        if model_id is None:
            log.warning(
                "conductor_llm_unknown_model_alias",
                extra={"alias": self.model_alias},
            )
            # Fall back to default
            model_id = self.ai._config["models"].get("sonnet", "claude-sonnet-4-6")

        try:
            message = self.ai._client.messages.create(
                model=model_id,
                max_tokens=self.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": "DURABLE MEMORY SUBSET:\n\n" + (durable_subset or "(none)"),
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                messages=messages,
                tools=anthropic_tools,
            )
        except Exception as exc:
            log.error(
                "conductor_llm_api_error",
                extra={
                    "agent": "conductor_llm",
                    "model": model_id,
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                },
            )
            # Hard error → end the session rather than spin
            return DoneDecision(reason=f"LLM API error: {exc.__class__.__name__}")

        return self._parse_response(message)

    def _build_anthropic_tools(self, tools: dict[str, ToolSpec]) -> list[dict[str, Any]]:
        """Combine virtual tools (speak/ask/save/log/done) with caller-supplied tools."""
        out = list(_VIRTUAL_TOOLS)
        for spec in tools.values():
            out.append({
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.schema or {
                    "type": "object",
                    "properties": {},
                },
            })
        return out

    def _build_messages(
        self, working_memory: list[Turn], session_summary: str,
    ) -> list[dict[str, Any]]:
        """Construct the messages array for Anthropic.

        Strategy: replay working memory as alternating user/assistant turns;
        prepend session_summary as an initial user message context block;
        if there are no operator turns yet, emit a single bootstrap message.
        """
        msgs: list[dict[str, Any]] = []
        if session_summary:
            msgs.append({
                "role": "user",
                "content": "SESSION SUMMARY (so far):\n" + session_summary,
            })

        if not working_memory:
            # Bootstrap turn: ask the LLM to open the conversation.
            msgs.append({
                "role": "user",
                "content": (
                    "[loop-bootstrap] No working memory yet. Open the conversation "
                    "appropriately for this operator's situation (use durable memory "
                    "to determine if this is a first-meet or a return). Emit a tool_use."
                ),
            })
            return msgs

        # Replay working memory. The AgentLLM loop semantics: the loop has
        # already added the assistant's responses to working memory after
        # they're emitted, so a faithful replay recreates the conversation
        # on each turn.
        for turn in working_memory:
            if turn.role == "user":
                msgs.append({"role": "user", "content": turn.content})
            elif turn.role == "assistant":
                msgs.append({"role": "assistant", "content": turn.content})
            elif turn.role == "tool_observation":
                # Tool observations become user-role context (Anthropic
                # convention: tool_result blocks). Simplified to plain text
                # here; richer multi-block tool_result wiring is deferred.
                msgs.append({
                    "role": "user",
                    "content": f"[tool_observation] {turn.content}",
                })

        # Anthropic requires the conversation to end on a user-role message
        # before the next assistant turn. If the last working-memory turn
        # was assistant, append a nudge.
        if msgs and msgs[-1]["role"] == "assistant":
            msgs.append({
                "role": "user",
                "content": "[loop-tick] Continue with the next decision.",
            })
        return msgs

    def _parse_response(self, message: Any) -> AgentDecision:
        """Extract a single AgentDecision from the Anthropic message.

        Strategy: scan content blocks for the first tool_use; if none,
        fall back to combining text content as a SpeakDecision.
        """
        content_blocks = list(getattr(message, "content", []) or [])

        # Prefer tool_use blocks
        for block in content_blocks:
            block_type = getattr(block, "type", None)
            if block_type != "tool_use":
                continue
            tool_name = getattr(block, "name", "") or ""
            tool_input = getattr(block, "input", {}) or {}
            decision = self._tool_use_to_decision(tool_name, tool_input)
            if decision is not None:
                return decision

        # No tool_use: fall back to plain-text speak
        text_parts = [
            getattr(b, "text", "")
            for b in content_blocks
            if getattr(b, "type", "") == "text" and getattr(b, "text", "")
        ]
        if text_parts:
            return SpeakDecision(text="\n\n".join(text_parts).strip())

        # Nothing extractable — end session rather than loop
        log.warning(
            "conductor_llm_empty_response",
            extra={
                "agent": "conductor_llm",
                "stop_reason": getattr(message, "stop_reason", "unknown"),
            },
        )
        return DoneDecision(reason="LLM returned no actionable content")

    def _tool_use_to_decision(
        self, tool_name: str, tool_input: dict[str, Any],
    ) -> AgentDecision | None:
        """Map a tool_use to one of the agent_loop AgentDecision shapes."""
        rationale = tool_input.get("rationale")

        if tool_name == "speak":
            text = tool_input.get("text")
            if not text:
                return None
            return SpeakDecision(text=str(text), rationale=rationale)

        if tool_name == "ask_operator":
            question = tool_input.get("question")
            if not question:
                return None
            return AskDecision(question=str(question), rationale=rationale)

        if tool_name == "save_fact":
            try:
                return SaveFactDecision(
                    field_path=str(tool_input["field_path"]),
                    value=tool_input["value"],
                    confidence=float(tool_input["confidence"]),
                    evidence=list(tool_input.get("evidence") or []),
                    rationale=rationale,
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning(
                    "conductor_llm_malformed_save_fact",
                    extra={
                        "agent": "conductor_llm",
                        "input_keys": list(tool_input.keys()),
                        "error": str(exc),
                    },
                )
                return None

        if tool_name == "log_decision":
            entry = tool_input.get("entry")
            if not isinstance(entry, dict):
                return None
            return LogDecisionDecision(entry=entry, rationale=rationale)

        if tool_name == "done_for_now":
            return DoneDecision(reason=tool_input.get("reason"), rationale=rationale)

        # Anything else is a real tool call
        if tool_name and tool_name not in _VIRTUAL_TOOL_NAMES:
            return CallToolDecision(
                tool=tool_name,
                args={k: v for k, v in tool_input.items() if k != "rationale"},
                rationale=rationale,
            )
        return None
