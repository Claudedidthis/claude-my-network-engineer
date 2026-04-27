"""AIRuntime → AgentLLM adapter.

The agent loop primitive (tools/agent_loop.py) declares a thin AgentLLM
Protocol: `decide(...)` returns an AgentDecision given the loop state.
This module wraps the existing AIRuntime (Anthropic API) to satisfy
that protocol.

Stateful design (real-use bug fix 2026-04-27)
---------------------------------------------

The adapter maintains its OWN Anthropic-shaped message history across
decide() calls in one session. The first version reconstructed messages
from the loop's working memory each turn, which broke Anthropic's
tool_use/tool_result correlation convention — the model emits tool_use
with an `id`, and the next message must include a matching tool_result
block with `tool_use_id=<that id>`. Without correlation, the model
ignores the result and retries the tool — observed in the wild as the
agent calling read_snapshot 13 times in 40 seconds.

Two state variables make this work:

  • _api_messages — the conversation as Anthropic sees it. user/assistant
    role messages with proper tool_use and tool_result content blocks.
  • _pending_tool_use_id — the most recent tool_use_id we received from
    the model. The loop's next tool_observation turn becomes a
    tool_result block referencing this id.

Multi-decision response handling
--------------------------------

When the model emits BOTH text AND tool_use in one response (common —
"I'll check your network." + tool_use read_snapshot), we now produce
multiple AgentDecisions: SpeakDecision for the text, then CallToolDecision
for the tool. The loop consumes them in order; the operator sees the
narration before the tool runs.

A `_decision_queue` holds decisions extracted from the response that
haven't been returned yet. decide() drains the queue before making a new
API call.

What this module does
---------------------

  1. Builds messages.create payload using stateful _api_messages plus
     freshly-collected user-role updates from working_memory delta
     (tool_observations land here as proper tool_result blocks).
  2. Calls AIRuntime, captures the response in _api_messages.
  3. Parses response into one or more AgentDecisions; queues them.

Five virtual tools (speak, ask_operator, save_fact, log_decision,
done_for_now) are advertised alongside caller-supplied real tools.
The model emits these as tool_use blocks; the parser maps them to
the corresponding AgentDecision shape.
"""
from __future__ import annotations

import logging
from typing import Any

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
from network_engineer.tools.conductor_debug import (
    log_event as _debug_log,
    truncate_messages_for_log,
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
                "value": {},
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
    """Stateful Anthropic-conversation adapter satisfying AgentLLM Protocol.

    State across decide() calls within one session:
      _api_messages           Anthropic-shaped conversation history.
      _pending_tool_uses      Document-ordered list of (id, name, kind)
                              tuples for every tool_use in the most
                              recent assistant response. kind is
                              "virtual" (speak/ask/save/log/done) or
                              "real" (snapshot/audit/etc). EVERY entry
                              MUST get a tool_result on the next user
                              message — virtual ones get a synthesized
                              "ok"; real ones get the actual tool
                              observation content. Anthropic enforces
                              this at the API layer (returns 400 with
                              "tool_use ids were found without tool_result
                              blocks immediately after").
      _decision_queue         Multi-block responses (text + tool_use)
                              produce multiple decisions; the loop drains
                              them one per call.
      _processed_turn_count   Working-memory delta tracker.

    Memory-bound invariant: _api_messages is capped at _MAX_API_MESSAGES
    entries; once exceeded, the oldest non-bootstrap pair is dropped.
    Long sessions don't blow the model's context window.
    """

    # Soft cap on the conversation buffer. Each pair is one user + one
    # assistant message; ~50 pairs at average 1KB each is ~100KB which
    # fits comfortably under any current model's context window plus
    # leaves room for the system prompt + durable subset.
    _MAX_API_MESSAGES = 100

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

        # Anthropic-shaped conversation. Each entry: {"role": ..., "content": ...}
        self._api_messages: list[dict[str, Any]] = []
        # Document-ordered list of pending (tool_use_id, name, kind) for
        # every tool_use in the most recent assistant response. ALL of
        # them need a tool_result on the next user message. Cleared
        # entirely on each fold.
        self._pending_tool_uses: list[tuple[str, str, str]] = []
        # Decisions extracted from a multi-block response, queued for the
        # loop to consume one at a time.
        self._decision_queue: list[AgentDecision] = []
        # How many turns of working_memory we've already folded into
        # _api_messages — incremented as we consume the delta.
        self._processed_turn_count: int = 0

    def decide(
        self,
        *,
        system_prompt: str,
        working_memory: list[Turn],
        session_summary: str,
        durable_subset: str,
        tools: dict[str, ToolSpec],
    ) -> AgentDecision:
        """Build payload, call Anthropic, parse one AgentDecision.

        Drains the decision queue first; only calls Anthropic when the
        queue is empty.
        """
        if not self.ai.enabled:
            if not self._disabled_message_emitted:
                self._disabled_message_emitted = True
                return SpeakDecision(
                    text="AI runtime is disabled (set AI_RUNTIME_ENABLED=true and "
                         "ANTHROPIC_API_KEY to enable). Without an LLM I can't drive "
                         "this conversation. Exiting.",
                )
            return DoneDecision(reason="AI runtime disabled")

        # If we have queued decisions from a multi-block response, return
        # the next one without making a new API call.
        if self._decision_queue:
            return self._decision_queue.pop(0)

        # Fold any new turns from working_memory into _api_messages.
        self._fold_working_memory_delta(working_memory)

        # Bootstrap if we have no messages yet.
        if not self._api_messages:
            self._api_messages.append({
                "role": "user",
                "content": (
                    "[loop-bootstrap] Opening turn — no working memory yet. "
                    "Greet the operator appropriately for their situation "
                    "(use durable memory + session summary to determine if "
                    "this is a first-meet or a return). Emit a tool_use "
                    "(speak / ask_operator / call_tool / etc)."
                ),
            })

        # Anthropic requires conversations end on a user-role message
        # before the next assistant turn. If somehow the last message is
        # assistant (shouldn't happen but defensive), append a nudge.
        if self._api_messages[-1]["role"] == "assistant":
            self._api_messages.append({
                "role": "user",
                "content": "[loop-tick] Continue.",
            })

        anthropic_tools = self._build_anthropic_tools(tools)
        model_id = self.ai._config["models"].get(self.model_alias)
        if model_id is None:
            log.warning(
                "conductor_llm_unknown_model_alias",
                extra={"alias": self.model_alias},
            )
            model_id = self.ai._config["models"].get("sonnet", "claude-sonnet-4-6")

        # Log the full request payload BEFORE the call. If the API rejects,
        # this is the trace I read to diagnose. After every successful
        # call this also gets logged so I can see what the model saw.
        _debug_log("api_request_pre_call", {
            "model": model_id,
            "messages_count": len(self._api_messages),
            "messages": truncate_messages_for_log(self._api_messages),
            "tools_count": len(anthropic_tools),
            "tool_names": [t.get("name") for t in anthropic_tools],
            "max_tokens": self.max_tokens,
            "pending_tool_uses": list(self._pending_tool_uses),
        })

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
                        "text": (
                            "DURABLE MEMORY SUBSET:\n\n" + (durable_subset or "(none)")
                            + "\n\nSESSION SUMMARY:\n\n" + (session_summary or "(none)")
                        ),
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                messages=self._api_messages,
                tools=anthropic_tools,
            )
        except Exception as exc:
            # Capture EVERYTHING about the failure — request payload, the
            # exception class, the response body if Anthropic gave us one
            # (BadRequestError exposes .body and .response on most SDK
            # versions). This is the data I need to debug the next 400.
            error_body = None
            response_text = None
            response_status = None
            try:
                body = getattr(exc, "body", None)
                if body is not None:
                    error_body = body if isinstance(body, (dict, str)) else repr(body)[:2000]
            except Exception:
                pass
            try:
                resp = getattr(exc, "response", None)
                if resp is not None:
                    response_text = getattr(resp, "text", None)
                    if response_text and len(response_text) > 4000:
                        response_text = response_text[:4000] + "...[truncated]"
                    response_status = getattr(resp, "status_code", None)
            except Exception:
                pass

            _debug_log("api_request_failed", {
                "model": model_id,
                "error_type": exc.__class__.__name__,
                "error_str": str(exc)[:2000],
                "response_status": response_status,
                "response_text": response_text,
                "error_body": error_body,
                "messages_count": len(self._api_messages),
                "messages": truncate_messages_for_log(self._api_messages),
                "pending_tool_uses": list(self._pending_tool_uses),
            })

            log.error(
                "conductor_llm_api_error",
                extra={
                    "agent": "conductor_llm",
                    "model": model_id,
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                    "debug_log": "logs/conductor_debug.jsonl",
                },
            )
            return DoneDecision(reason=f"LLM API error: {exc.__class__.__name__}")

        # Append the assistant response to our message history (full
        # content blocks so tool_use is preserved for next turn's
        # correlation).
        assistant_content = self._content_to_dicts(message.content)
        self._api_messages.append({
            "role": "assistant",
            "content": assistant_content,
        })

        _debug_log("api_response_received", {
            "model": model_id,
            "stop_reason": getattr(message, "stop_reason", "?"),
            "assistant_content": assistant_content,
            "input_tokens": getattr(getattr(message, "usage", None), "input_tokens", None),
            "output_tokens": getattr(getattr(message, "usage", None), "output_tokens", None),
        })

        # Parse the response into one or more AgentDecisions.
        decisions = self._parse_response_to_decisions(message)
        if not decisions:
            # Empty response (model emitted no text + no tool_use). Rather
            # than immediately ending the session, append a nudge and
            # retry once. If the second attempt is also empty, we exit.
            stop_reason = getattr(message, "stop_reason", "unknown")
            log.warning(
                "conductor_llm_empty_response",
                extra={
                    "agent": "conductor_llm",
                    "stop_reason": stop_reason,
                    "input_tokens": getattr(getattr(message, "usage", None), "input_tokens", None),
                    "output_tokens": getattr(getattr(message, "usage", None), "output_tokens", None),
                },
            )
            _debug_log("empty_response_retry_attempt", {
                "stop_reason": stop_reason,
            })
            # Append a nudge to the conversation and try once more.
            self._api_messages.append({
                "role": "user",
                "content": (
                    "[loop-nudge] Your previous response had no content. "
                    "If you were waiting for the operator, use ask_operator. "
                    "If you were done, emit done_for_now. Otherwise, continue "
                    "with the conversation — speak, call a tool, or save a fact."
                ),
            })
            try:
                retry_message = self.ai._client.messages.create(
                    model=model_id,
                    max_tokens=self.max_tokens,
                    system=[
                        {"type": "text", "text": system_prompt,
                         "cache_control": {"type": "ephemeral"}},
                        {"type": "text",
                         "text": "DURABLE MEMORY SUBSET:\n\n" + (durable_subset or "(none)")
                                 + "\n\nSESSION SUMMARY:\n\n" + (session_summary or "(none)"),
                         "cache_control": {"type": "ephemeral"}},
                    ],
                    messages=self._api_messages,
                    tools=anthropic_tools,
                )
            except Exception as exc:
                _debug_log("empty_response_retry_failed", {
                    "error_type": exc.__class__.__name__,
                    "error_str": str(exc)[:1000],
                })
                return DoneDecision(reason=f"empty response + retry failed: {exc.__class__.__name__}")

            retry_content = self._content_to_dicts(retry_message.content)
            self._api_messages.append({
                "role": "assistant",
                "content": retry_content,
            })
            for block in retry_message.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                use_id = getattr(block, "id", None)
                if not use_id:
                    continue
                bname = getattr(block, "name", "")
                kind = "virtual" if bname in _VIRTUAL_TOOL_NAMES else "real"
                self._pending_tool_uses.append((use_id, bname, kind))

            _debug_log("empty_response_retry_response", {
                "stop_reason": getattr(retry_message, "stop_reason", "?"),
                "assistant_content": retry_content,
            })

            decisions = self._parse_response_to_decisions(retry_message)
            if not decisions:
                # Second time empty — really done.
                return DoneDecision(reason="LLM returned empty content twice in a row")

        # Capture EVERY tool_use (real AND virtual) in document order.
        # Anthropic's API requires tool_result blocks for ALL tool_uses
        # in the immediately-following user message. Virtual tools
        # (speak/ask/save/log/done) get synthesized "ok" results; real
        # tools get the actual tool_observation content via _fold.
        for block in message.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            use_id = getattr(block, "id", None)
            if not use_id:
                continue
            block_name = getattr(block, "name", "")
            kind = "virtual" if block_name in _VIRTUAL_TOOL_NAMES else "real"
            self._pending_tool_uses.append((use_id, block_name, kind))

        # Trim the conversation buffer if it's grown past the cap.
        self._enforce_message_cap()

        # First decision goes back immediately; the rest queue.
        first, *rest = decisions
        self._decision_queue.extend(rest)
        return first

    def _enforce_message_cap(self) -> None:
        """Trim the api_messages buffer when it grows past the cap.

        Per code-review 2026-04-27: the buffer was unbounded, which would
        eventually overflow the model's context window in long sessions.
        When over the cap, drop the oldest user/assistant pair from the
        front. The first message is preserved as a coarse "session
        opener" anchor.
        """
        if len(self._api_messages) <= self._MAX_API_MESSAGES:
            return
        # Always keep the first message (bootstrap or first operator turn)
        # as session-opener context. Drop the next-oldest pair.
        # If the very-second message is assistant, drop the assistant first
        # then the user to keep parity.
        excess = len(self._api_messages) - self._MAX_API_MESSAGES
        # Drop pairs (2 messages) until under the cap, starting after index 0.
        head = self._api_messages[:1]
        rest = self._api_messages[1:]
        # Skip-by-2 pattern from the front of `rest` until under cap.
        drop_count = excess
        # Round up to even so we don't leave a dangling assistant-first.
        if drop_count % 2 == 1:
            drop_count += 1
        rest = rest[drop_count:]
        # Re-anchor: ensure the new conversation still starts with a
        # user-role message after the head.
        while rest and rest[0]["role"] != "user":
            rest = rest[1:]
        self._api_messages = head + rest

    # ── Internals ────────────────────────────────────────────────────────

    def _fold_working_memory_delta(self, working_memory: list[Turn]) -> None:
        """Walk new turns since last call; emit AT MOST ONE user message
        with all required tool_result blocks plus any operator text.

        Anthropic constraints this satisfies:
          1. Strict user/assistant alternation. One user message per fold.
          2. tool_result for EVERY tool_use in the prior assistant message,
             in document order. This includes virtual tools (speak,
             ask_operator, save_fact, log_decision, done_for_now) — the
             API treats them as regular tool_uses and rejects with 400 if
             they don't have matching tool_results.
          3. Mixed content blocks (tool_result + text) in one user message
             are valid and let us include operator interjections /
             ask-replies alongside tool results.

        Block order in the emitted user message:
          1. tool_result blocks for every pending tool_use (in document
             order). Virtual tools get synthesized "ok" content; real
             tools get the corresponding tool_observation content.
          2. Text blocks for any operator user turns (interjections,
             ask replies).
        """
        new_turns = working_memory[self._processed_turn_count:]
        self._processed_turn_count = len(working_memory)

        # Separate user turns and tool observations from the delta.
        observations = [t for t in new_turns if t.role == "tool_observation"]
        user_turns = [t for t in new_turns if t.role == "user"]
        observation_iter = iter(observations)

        # ask_operator is BLOCKING — every ask in pending corresponds 1:1
        # to a user turn in the delta. We pair them by reverse order: the
        # last ask_operator gets the last user turn, etc. Why reverse:
        # interjections from speaks come FIRST in document order, ask
        # replies come AFTER. Reverse-matching lets ask replies bind
        # correctly while interjections fall through to text blocks.
        ask_pending_indices = [
            i for i, (_, name, kind) in enumerate(self._pending_tool_uses)
            if kind == "virtual" and name == "ask_operator"
        ]
        ask_replies: dict[str, str] = {}
        unconsumed_user_turns = list(user_turns)
        for idx in reversed(ask_pending_indices):
            if not unconsumed_user_turns:
                break
            use_id = self._pending_tool_uses[idx][0]
            ask_replies[use_id] = unconsumed_user_turns.pop().content

        blocks: list[dict[str, Any]] = []

        # Pass 1: tool_result blocks for EVERY pending tool_use, in
        # document order. Virtual ask_operator carries the operator's
        # actual reply as content (cleaner for the model than a synth
        # placeholder + separate text block). Other virtual tools get
        # minimal "ok". Real tools get the corresponding observation.
        for use_id, name, kind in self._pending_tool_uses:
            if name == "ask_operator" and use_id in ask_replies:
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": use_id,
                    "content": ask_replies[use_id][:8000],
                })
            elif kind == "virtual":
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": use_id,
                    "content": _virtual_synth_result(name),
                })
            else:
                obs = next(observation_iter, None)
                if obs is not None:
                    blocks.append({
                        "type": "tool_result",
                        "tool_use_id": use_id,
                        "content": obs.content[:8000],
                    })
                else:
                    # Defensive fallback — every CallToolDecision adds a
                    # tool_observation; this shouldn't happen.
                    blocks.append({
                        "type": "tool_result",
                        "tool_use_id": use_id,
                        "content": "(tool result missing)",
                    })
        # All pending tool_uses are now satisfied.
        self._pending_tool_uses = []

        # Pass 2: any user turns NOT consumed as ask replies become text
        # blocks (these are interjections from speak windows).
        for turn in unconsumed_user_turns:
            blocks.append({
                "type": "text",
                "text": turn.content,
            })

        if not blocks:
            return

        if len(blocks) == 1 and blocks[0]["type"] == "text":
            self._api_messages.append({
                "role": "user",
                "content": blocks[0]["text"],
            })
        else:
            self._api_messages.append({
                "role": "user",
                "content": blocks,
            })

    def _build_anthropic_tools(
        self, tools: dict[str, ToolSpec],
    ) -> list[dict[str, Any]]:
        """Combine virtual tools (speak/ask/save/log/done) with real tools."""
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

    @staticmethod
    def _content_to_dicts(content_blocks: Any) -> list[dict[str, Any]]:
        """Convert Anthropic SDK content blocks to JSON-safe dicts so we
        can stash them in api_messages and round-trip them on subsequent
        API calls."""
        out: list[dict[str, Any]] = []
        for block in content_blocks or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = getattr(block, "text", "") or ""
                if text:
                    out.append({"type": "text", "text": text})
            elif block_type == "tool_use":
                out.append({
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}) or {},
                })
            # Other types (thinking, image, etc.) are dropped — the
            # Conductor doesn't use them.
        return out

    def _parse_response_to_decisions(self, message: Any) -> list[AgentDecision]:
        """Extract zero or more AgentDecisions from a model response.

        Order: text blocks first (as SpeakDecision), then tool_uses.
        This matches Anthropic's content-block ordering convention where
        the model narrates BEFORE invoking tools.
        """
        decisions: list[AgentDecision] = []
        content_blocks = list(getattr(message, "content", []) or [])

        # Collect all text first, then all tool_uses, in the order they
        # appeared. We emit text blocks AS SpeakDecisions only when they
        # have non-trivial content.
        for block in content_blocks:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = (getattr(block, "text", "") or "").strip()
                if text:
                    decisions.append(SpeakDecision(text=text))
            elif block_type == "tool_use":
                tool_name = getattr(block, "name", "") or ""
                tool_input = getattr(block, "input", {}) or {}
                d = self._tool_use_to_decision(tool_name, tool_input)
                if d is not None:
                    decisions.append(d)

        return decisions

    def _tool_use_to_decision(
        self, tool_name: str, tool_input: dict[str, Any],
    ) -> AgentDecision | None:
        """Map a tool_use block to one of the AgentDecision shapes."""
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

        # Real tool call (anything not in _VIRTUAL_TOOL_NAMES)
        if tool_name and tool_name not in _VIRTUAL_TOOL_NAMES:
            return CallToolDecision(
                tool=tool_name,
                args={k: v for k, v in tool_input.items() if k != "rationale"},
                rationale=rationale,
            )
        return None


def _virtual_synth_result(tool_name: str) -> str:
    """Synthesized tool_result content for a virtual tool that's already
    been processed by the loop. Anthropic's API requires SOME tool_result
    content per tool_use; we keep it minimal to avoid confusing the model
    with meta-narration about what the loop did. Operator replies (for
    ask_operator) are placed in the tool_result content directly via the
    fold logic, not as a synth string here."""
    return "ok"
