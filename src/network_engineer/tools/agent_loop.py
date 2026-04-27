"""Agent loop primitive — the runtime behavior of every LLM-driven agent.

Per docs/agent_architecture.md §4. Single function, six decision kinds.
The Conductor (step 5) and any sub-agents (e.g. propose_segmentation)
both call `run_agent` with their own config: their own system prompt,
their own toolset, their own goal-bounded `max_turns`.

This module is the *primitive*. It does not import a specific LLM client,
a specific memory backend, or any agent-specific logic. The contract is:
the caller supplies an `AgentLLM` (anything that returns an `AgentDecision`
given the current state), a `WorkingMemory`, a `SessionState`, a
`DurableMemoryProtocol`, a tool dict, and I/O callbacks. The loop
turns the crank until the LLM emits `done_for_now`.

What this module deliberately does NOT do:
  • Load or persist durable memory — that's task #53.
  • Provide a real LLM — `AgentLLM` is a protocol; AIRuntime wraps Anthropic.
  • Wrap durable memory in untrusted-data tags on retrieval — the
    `DurableMemoryProtocol.relevant_to()` implementation does that
    (per architecture §3 layer 2).
  • Sanitize hostile content — `tools/prompt_safety.py` does that at
    write time; `DurableMemoryProtocol` enforces it.

Tests in `tests/test_agent_loop.py` use a `ScriptedLLM` that pops
decisions from a queue. Each decision kind is exercised; failure
modes (unknown tool, exhausted decisions, max_turns reached) are
covered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

# ── Turn (message in working memory) ─────────────────────────────────────────


class Turn(BaseModel):
    """One operator/agent/tool-observation entry in working memory."""

    turn_id: str = Field(default_factory=lambda: f"t-{uuid4().hex[:12]}")
    role: Literal["user", "assistant", "tool_observation"]
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── ToolSpec ─────────────────────────────────────────────────────────────────


@dataclass
class ToolSpec:
    """Describes a tool the agent can invoke.

    `schema` is a JSON Schema for `args`, used to populate the LLM's
    tool-use prompt. `fn` is the deterministic callable. The loop never
    does anything with the schema except pass it to the LLM via
    `AgentLLM.decide`; the LLM is responsible for producing args that
    conform.
    """

    name: str
    description: str
    fn: Callable[..., Any]
    schema: dict[str, Any] = field(default_factory=dict)


# ── AgentDecision — six kinds ────────────────────────────────────────────────
#
# Each decision kind is a separate Pydantic model so callers can isinstance()
# without surprises. The LLM-side path (step 5) will parse tool-use JSON into
# one of these via a discriminated union; that parsing lives in the AIRuntime
# wrapper, not here.


class _DecisionBase(BaseModel):
    rationale: str | None = None  # optional internal note for traces


class SpeakDecision(_DecisionBase):
    """Say something to the operator. No question, no waiting for a reply."""
    kind: Literal["speak"] = "speak"
    text: str


class AskDecision(_DecisionBase):
    """Ask the operator something and wait for the reply."""
    kind: Literal["ask"] = "ask"
    question: str


class CallToolDecision(_DecisionBase):
    """Invoke one of the available tools with `args`. Result lands in
    working memory as a tool_observation turn and in session_state."""
    kind: Literal["call_tool"] = "call_tool"
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class SaveFactDecision(_DecisionBase):
    """Write a fact to durable memory with confidence + evidence trail.

    `field_path` (not `field` — that name shadows Pydantic's Field) is the
    dotted path into the operator profile / registry / etc. that the
    durable memory implementation knows how to address.
    """
    kind: Literal["save_fact"] = "save_fact"
    field_path: str
    value: Any
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class LogDecisionDecision(_DecisionBase):
    """Append a structured entry to the durable decision log."""
    kind: Literal["log_decision"] = "log_decision"
    entry: dict[str, Any]


class DoneDecision(_DecisionBase):
    """Exit the loop cleanly. Loop calls session_state.checkpoint() before returning."""
    kind: Literal["done_for_now"] = "done_for_now"
    reason: str | None = None


AgentDecision = (
    SpeakDecision
    | AskDecision
    | CallToolDecision
    | SaveFactDecision
    | LogDecisionDecision
    | DoneDecision
)


# ── WorkingMemory (Tier 1) ───────────────────────────────────────────────────


@dataclass
class WorkingMemory:
    """Tier 1 — rolling window of the most recent turns. Verbatim. Bounded.

    When the window overflows, oldest turns are passed to `overflow_callback`
    (which the SessionState typically wires to its digest_lines so the
    overflow is summarized into Tier 2 rather than discarded).
    """

    max_turns: int = 12
    overflow_callback: Callable[[list[Turn]], None] | None = None
    _turns: list[Turn] = field(default_factory=list)

    def add_user(self, content: str) -> Turn:
        return self._add(Turn(role="user", content=content))

    def add_assistant(self, content: str) -> Turn:
        return self._add(Turn(role="assistant", content=content))

    def add_tool_observation(self, content: str) -> Turn:
        return self._add(Turn(role="tool_observation", content=content))

    def _add(self, turn: Turn) -> Turn:
        self._turns.append(turn)
        if len(self._turns) > self.max_turns:
            overflow = self._turns[: len(self._turns) - self.max_turns]
            self._turns = self._turns[len(self._turns) - self.max_turns:]
            if self.overflow_callback:
                self.overflow_callback(overflow)
        return turn

    def recent(self) -> list[Turn]:
        return list(self._turns)

    @property
    def current_turn_id(self) -> str:
        return self._turns[-1].turn_id if self._turns else ""


# ── SessionState (Tier 2 — in-process for now) ──────────────────────────────


@dataclass
class ToolCallRecord:
    """One tool invocation captured in the session log for back-reference."""
    tool: str
    args: dict[str, Any]
    result: Any
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SessionState:
    """Tier 2 — accumulated session state.

    Tool calls, working-memory overflow digests, pending follow-ups.
    Real disk persistence lands in migration step 4 (task #53) — for now,
    `checkpoint()` is a no-op so the loop primitive can be tested
    without a backend.
    """

    session_id: str = field(default_factory=lambda: f"sess-{uuid4().hex[:12]}")
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    digest_lines: list[str] = field(default_factory=list)

    def record_tool_call(self, tool: str, args: dict[str, Any], result: Any) -> None:
        self.tool_calls.append(ToolCallRecord(tool=tool, args=args, result=result))

    def absorb_overflow(self, overflow: list[Turn]) -> None:
        """Receive working-memory overflow and compress it into a digest line.

        Called by WorkingMemory.overflow_callback. The compression is
        deliberately mechanical here (count + role summary) — the rich
        LLM-generated narrative summary at session end is the
        Conductor-level concern (per §12 question 10).
        """
        if not overflow:
            return
        roles = {}
        for t in overflow:
            roles[t.role] = roles.get(t.role, 0) + 1
        parts = ", ".join(f"{n} {r}" for r, n in sorted(roles.items()))
        self.digest_lines.append(
            f"[t-overflow @ {overflow[0].timestamp.isoformat()}] {parts}",
        )

    def summary(self) -> str:
        """Compact representation for the LLM's session-summary slot."""
        if not self.tool_calls and not self.digest_lines:
            return ""
        lines: list[str] = []
        if self.digest_lines:
            lines.append("Earlier in this session:")
            lines.extend(f"  {d}" for d in self.digest_lines[-5:])
        if self.tool_calls:
            lines.append(f"Tool calls so far: {len(self.tool_calls)}")
            for c in self.tool_calls[-3:]:
                lines.append(f"  {c.tool}({c.args}) → {repr(c.result)[:80]}")
        return "\n".join(lines)

    def checkpoint(self) -> None:
        """Snapshot session state. No-op until task #53 wires durable persistence."""
        pass


# ── Protocols (DurableMemory comes in task #53) ─────────────────────────────


class DurableMemoryProtocol(Protocol):
    """Tier 3 — durable memory. The loop primitive only declares what it calls.

    Real implementation in `tools/durable_memory.py` (migration step 4 / #53)
    handles: provenance tagging, untrusted-data wrapping on retrieval,
    sanitization on write, caution markers, decision log, query_history.
    """

    def upsert_fact(
        self,
        *,
        field: str,
        value: Any,
        confidence: float,
        evidence: list[str],
        source_turn_id: str,
    ) -> None: ...

    def append_decision(self, entry: dict[str, Any]) -> None: ...

    def relevant_to(self, query: str) -> str: ...


class AgentLLM(Protocol):
    """Whatever the loop calls to produce the next AgentDecision.

    Real implementation wraps AIRuntime + tool-use JSON parsing in
    migration step 5 (task #54). Tests use a ScriptedLLM that pops
    decisions from a queue.
    """

    def decide(
        self,
        *,
        system_prompt: str,
        working_memory: list[Turn],
        session_summary: str,
        durable_subset: str,
        tools: dict[str, ToolSpec],
    ) -> AgentDecision: ...


# ── The loop ────────────────────────────────────────────────────────────────


class AgentLoopExhausted(RuntimeError):
    """Raised when max_turns elapses without a done_for_now decision."""


def run_agent(
    *,
    system_prompt: str,
    durable_memory: DurableMemoryProtocol,
    session_state: SessionState,
    working_memory: WorkingMemory,
    tools: dict[str, ToolSpec],
    llm: AgentLLM,
    on_say: Callable[[str], None],
    on_user_input: Callable[[str], str],
    max_turns: int = 100,
) -> SessionState:
    """Run the agent loop until done_for_now or max_turns exhaustion.

    The Conductor and any sub-agent both call this with their own config.
    Returns the SessionState (now containing the full tool-call log and
    digest) so the caller can serialize it for the next session.
    """
    # Wire WorkingMemory overflow into the session's digest if not already set.
    if working_memory.overflow_callback is None:
        working_memory.overflow_callback = session_state.absorb_overflow

    for _ in range(max_turns):
        decision = llm.decide(
            system_prompt=system_prompt,
            working_memory=working_memory.recent(),
            session_summary=session_state.summary(),
            durable_subset=durable_memory.relevant_to(
                _query_from_recent(working_memory.recent()),
            ),
            tools=tools,
        )

        if isinstance(decision, SpeakDecision):
            on_say(decision.text)
            working_memory.add_assistant(decision.text)
            # After a speak, give the operator a chance to interject. Empty
            # input (just Enter) means "continue, no interjection" and the
            # loop proceeds to the next LLM turn without a user message.
            # Non-empty input becomes a user turn the LLM sees on its next
            # decision call. This prevents the agent from monologuing past
            # an implicit question; it also makes conversation feel natural
            # — when someone says something to you, you can reply.
            interjection = on_user_input("> ")
            if interjection:
                working_memory.add_user(interjection)

        elif isinstance(decision, AskDecision):
            on_say(decision.question)
            working_memory.add_assistant(decision.question)
            answer = on_user_input("> ")
            working_memory.add_user(answer)

        elif isinstance(decision, CallToolDecision):
            tool = tools.get(decision.tool)
            if tool is None:
                err_msg = f"unknown tool: {decision.tool!r}"
                working_memory.add_tool_observation(err_msg)
                session_state.record_tool_call(
                    decision.tool, decision.args, {"error": err_msg},
                )
                continue
            try:
                result = tool.fn(**decision.args)
            except Exception as exc:
                result = {
                    "error": str(exc),
                    "exception_type": exc.__class__.__name__,
                }
            session_state.record_tool_call(decision.tool, decision.args, result)
            working_memory.add_tool_observation(
                f"{decision.tool}(...) → {repr(result)[:120]}",
            )

        elif isinstance(decision, SaveFactDecision):
            durable_memory.upsert_fact(
                field=decision.field_path,
                value=decision.value,
                confidence=decision.confidence,
                evidence=decision.evidence,
                source_turn_id=working_memory.current_turn_id,
            )

        elif isinstance(decision, LogDecisionDecision):
            durable_memory.append_decision(decision.entry)

        elif isinstance(decision, DoneDecision):
            session_state.checkpoint()
            return session_state

    raise AgentLoopExhausted(
        f"Agent loop exceeded max_turns={max_turns} without done_for_now",
    )


def _query_from_recent(turns: list[Turn]) -> str:
    """Compact a few recent turns into a query string for memory retrieval.

    Defensive: each turn's content is truncated to 200 chars to keep the
    query bounded. The actual durable memory implementation may do its
    own deeper retrieval; this is just the seed query.
    """
    return " ".join(t.content[:200] for t in turns[-3:])
