"""Tests for the agent loop primitive.

Six decision kinds + failure modes (unknown tool, exception in tool,
loop exhaustion, working-memory overflow). The LLM is scripted — every
test pre-loads a queue of decisions; the loop pops them one per turn.
"""
from __future__ import annotations

from typing import Any

import pytest

from network_engineer.tools.agent_loop import (
    AgentDecision,
    AgentLoopExhausted,
    AskDecision,
    CallToolDecision,
    DoneDecision,
    LogDecisionDecision,
    SaveFactDecision,
    SessionState,
    SpeakDecision,
    ToolCallRecord,
    ToolSpec,
    Turn,
    WorkingMemory,
    run_agent,
)


# ── Test doubles ─────────────────────────────────────────────────────────────


class ScriptedLLM:
    """LLM that pops AgentDecisions from a pre-loaded queue.

    Captures every kwargs dict it was called with so tests can assert
    what context the loop assembled per turn.
    """

    def __init__(self, decisions: list[AgentDecision]) -> None:
        self._decisions = list(decisions)
        self.calls: list[dict[str, Any]] = []

    def decide(self, **kwargs: Any) -> AgentDecision:
        self.calls.append(kwargs)
        if not self._decisions:
            raise AssertionError(
                "ScriptedLLM exhausted — test queued fewer decisions than the loop ran",
            )
        return self._decisions.pop(0)


class FakeDurableMemory:
    """Stand-in for the real DurableMemory (lands in task #53)."""

    def __init__(self) -> None:
        self.facts: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.relevance_lookups: list[str] = []

    def upsert_fact(
        self, *, field: str, value: Any, confidence: float,
        evidence: list[str], source_turn_id: str,
    ) -> None:
        self.facts.append({
            "field": field, "value": value, "confidence": confidence,
            "evidence": evidence, "source_turn_id": source_turn_id,
        })

    def append_decision(self, entry: dict[str, Any]) -> None:
        self.decisions.append(entry)

    def relevant_to(self, query: str) -> str:
        self.relevance_lookups.append(query)
        return ""


def _harness(
    decisions: list[AgentDecision],
    tools: dict[str, ToolSpec] | None = None,
    user_inputs: list[str] | None = None,
    max_turns: int = 100,
):
    """Build a fully-wired test environment. Returns (said, llm, durable, session)."""
    said: list[str] = []
    inputs = list(user_inputs or [])

    def on_say(text: str) -> None:
        said.append(text)

    def on_user_input(prompt: str) -> str:
        if not inputs:
            return ""
        return inputs.pop(0)

    llm = ScriptedLLM(decisions)
    durable = FakeDurableMemory()
    session = SessionState()
    wm = WorkingMemory()

    session_after = run_agent(
        system_prompt="(test)",
        durable_memory=durable,
        session_state=session,
        working_memory=wm,
        tools=tools or {},
        llm=llm,
        on_say=on_say,
        on_user_input=on_user_input,
        max_turns=max_turns,
    )
    return said, llm, durable, session_after


# ── Decision kinds — happy paths ────────────────────────────────────────────


def test_speak_decision_emits_text_and_records_assistant_turn() -> None:
    said, llm, _, _ = _harness([
        SpeakDecision(text="Hello operator"),
        DoneDecision(),
    ])
    assert said == ["Hello operator"]
    assert len(llm.calls) == 2  # one for speak, one for done
    # Second call's working_memory should contain the assistant turn from the first
    second_wm = llm.calls[1]["working_memory"]
    assert any(t.role == "assistant" and t.content == "Hello operator" for t in second_wm)


def test_speak_decision_allows_operator_interjection() -> None:
    """When the operator types something after a speak (rather than just
    hitting Enter), the typed input becomes a user turn the LLM sees on
    the next decision call. This prevents the agent from monologuing past
    an implicit question."""
    said, llm, _, _ = _harness(
        [SpeakDecision(text="Pretty solid setup."), DoneDecision()],
        user_inputs=["wait, before we go on, what about the camera?"],
    )
    assert said == ["Pretty solid setup."]
    # The interjection should land in working memory before the LLM's
    # second decide() call
    second_wm = llm.calls[1]["working_memory"]
    user_turns = [t for t in second_wm if t.role == "user"]
    assert len(user_turns) == 1
    assert "wait, before we go on" in user_turns[0].content


def test_speak_decision_empty_interjection_does_not_create_user_turn() -> None:
    """Empty input (just Enter) means 'continue' — no user turn added."""
    said, llm, _, _ = _harness(
        [SpeakDecision(text="Continuing."), DoneDecision()],
        user_inputs=[""],  # operator just presses Enter
    )
    assert said == ["Continuing."]
    second_wm = llm.calls[1]["working_memory"]
    user_turns = [t for t in second_wm if t.role == "user"]
    assert user_turns == []


def test_ask_decision_prompts_and_collects_user_reply() -> None:
    said, llm, _, _ = _harness(
        [AskDecision(question="What's your favorite color?"), DoneDecision()],
        user_inputs=["blue"],
    )
    assert said == ["What's your favorite color?"]
    second_wm = llm.calls[1]["working_memory"]
    user_turns = [t for t in second_wm if t.role == "user"]
    assert len(user_turns) == 1
    assert user_turns[0].content == "blue"


def test_call_tool_decision_invokes_tool_and_records_result() -> None:
    captured_args: list[dict[str, Any]] = []

    def my_tool(*, x: int) -> int:
        captured_args.append({"x": x})
        return x * 2

    tools = {
        "double": ToolSpec(
            name="double", description="x -> 2x", fn=my_tool, schema={},
        ),
    }
    said, llm, _, session = _harness(
        [CallToolDecision(tool="double", args={"x": 21}), DoneDecision()],
        tools=tools,
    )
    assert captured_args == [{"x": 21}]
    assert len(session.tool_calls) == 1
    assert session.tool_calls[0].tool == "double"
    assert session.tool_calls[0].result == 42
    # Tool observation lands in working memory for the next LLM turn
    second_wm = llm.calls[1]["working_memory"]
    obs = [t for t in second_wm if t.role == "tool_observation"]
    assert len(obs) == 1
    assert "double" in obs[0].content
    assert "42" in obs[0].content


def test_save_fact_decision_writes_to_durable_memory() -> None:
    _, _, durable, _ = _harness([
        SaveFactDecision(
            field_path="household_profile.use_case",
            value="home office",
            confidence=0.85,
            evidence=["operator turn 1: 'I work from home'"],
        ),
        DoneDecision(),
    ])
    assert len(durable.facts) == 1
    assert durable.facts[0]["field"] == "household_profile.use_case"
    assert durable.facts[0]["value"] == "home office"
    assert durable.facts[0]["confidence"] == 0.85
    assert "operator turn 1" in durable.facts[0]["evidence"][0]


def test_log_decision_decision_appends_to_durable_log() -> None:
    _, _, durable, _ = _harness([
        LogDecisionDecision(entry={
            "action": "save_origin_story",
            "subject": "DMZ",
            "rationale_summary": "solar installer",
        }),
        DoneDecision(),
    ])
    assert len(durable.decisions) == 1
    assert durable.decisions[0]["action"] == "save_origin_story"


def test_done_decision_exits_loop_cleanly() -> None:
    said, llm, _, session = _harness([
        SpeakDecision(text="bye"),
        DoneDecision(reason="operator quit"),
        # Anything after Done shouldn't be reached
        SpeakDecision(text="should not appear"),
    ])
    assert said == ["bye"]
    assert len(llm.calls) == 2  # speak + done; the trailing speak never runs
    assert isinstance(session, SessionState)


# ── Failure modes ───────────────────────────────────────────────────────────


def test_unknown_tool_records_error_and_continues() -> None:
    """Unknown tool must not crash the loop — it lands as a tool_observation
    error so the LLM can recover or change strategy on the next turn."""
    said, llm, _, session = _harness(
        [
            CallToolDecision(tool="nonexistent", args={}),
            SpeakDecision(text="recovered"),
            DoneDecision(),
        ],
    )
    assert "recovered" in said
    assert len(session.tool_calls) == 1
    assert "error" in session.tool_calls[0].result
    assert "unknown tool" in session.tool_calls[0].result["error"]


def test_tool_exception_captured_not_propagated() -> None:
    """A tool that raises must yield an error result, not crash the loop."""
    def angry_tool(**_: Any) -> Any:
        raise ValueError("kaboom")

    tools = {
        "angry": ToolSpec(name="angry", description="raises", fn=angry_tool),
    }
    _, _, _, session = _harness(
        [CallToolDecision(tool="angry"), DoneDecision()],
        tools=tools,
    )
    assert len(session.tool_calls) == 1
    result = session.tool_calls[0].result
    assert result["error"] == "kaboom"
    assert result["exception_type"] == "ValueError"


def test_max_turns_exhaustion_raises() -> None:
    """If the LLM never emits done_for_now, the loop fails loudly rather
    than spinning forever."""
    decisions = [SpeakDecision(text=f"turn {i}") for i in range(10)]
    with pytest.raises(AgentLoopExhausted, match="max_turns=5"):
        _harness(decisions, max_turns=5)


# ── Schema invariants ───────────────────────────────────────────────────────


def test_save_fact_confidence_must_be_in_range() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SaveFactDecision(
            field_path="x", value=1, confidence=1.5, evidence=[],
        )
    with pytest.raises(ValidationError):
        SaveFactDecision(
            field_path="x", value=1, confidence=-0.1, evidence=[],
        )


def test_decisions_carry_optional_rationale() -> None:
    d = SpeakDecision(text="hi", rationale="warming up the operator")
    assert d.rationale == "warming up the operator"


# ── WorkingMemory ───────────────────────────────────────────────────────────


def test_working_memory_default_max_turns_is_12() -> None:
    wm = WorkingMemory()
    assert wm.max_turns == 12


def test_working_memory_overflow_calls_callback() -> None:
    overflow_received: list[list[Turn]] = []
    wm = WorkingMemory(
        max_turns=3,
        overflow_callback=lambda batch: overflow_received.append(list(batch)),
    )
    wm.add_user("a")
    wm.add_assistant("b")
    wm.add_user("c")
    wm.add_assistant("d")
    wm.add_user("e")
    # 5 added, max=3, so 2 should have rolled off
    assert len(wm.recent()) == 3
    assert sum(len(b) for b in overflow_received) == 2


def test_working_memory_current_turn_id_returns_last() -> None:
    wm = WorkingMemory()
    assert wm.current_turn_id == ""
    t = wm.add_user("hello")
    assert wm.current_turn_id == t.turn_id


# ── SessionState ────────────────────────────────────────────────────────────


def test_session_state_summary_empty_when_no_activity() -> None:
    s = SessionState()
    assert s.summary() == ""


def test_session_state_summary_includes_tool_calls() -> None:
    s = SessionState()
    s.record_tool_call("foo", {"x": 1}, {"ok": True})
    summary = s.summary()
    assert "foo" in summary
    assert "Tool calls" in summary


def test_session_state_absorb_overflow_compresses_into_digest() -> None:
    s = SessionState()
    overflow = [
        Turn(role="user", content="q1"),
        Turn(role="assistant", content="a1"),
        Turn(role="user", content="q2"),
    ]
    s.absorb_overflow(overflow)
    assert len(s.digest_lines) == 1
    assert "2 user" in s.digest_lines[0] or "user" in s.digest_lines[0]


def test_loop_wires_session_overflow_callback_automatically() -> None:
    """If the caller doesn't set WorkingMemory.overflow_callback, the loop
    wires it to session_state.absorb_overflow so overflow doesn't disappear."""
    # Setup: working memory with very low max_turns + many speak decisions
    decisions = [SpeakDecision(text=f"turn {i}") for i in range(15)] + [DoneDecision()]
    said: list[str] = []
    llm = ScriptedLLM(decisions)
    durable = FakeDurableMemory()
    session = SessionState()
    wm = WorkingMemory(max_turns=3)  # no overflow_callback

    run_agent(
        system_prompt="(test)",
        durable_memory=durable,
        session_state=session,
        working_memory=wm,
        tools={},
        llm=llm,
        on_say=said.append,
        on_user_input=lambda _: "",
    )
    assert wm.overflow_callback is not None
    # Some overflow should have landed in session digest_lines
    assert len(session.digest_lines) > 0


# ── Loop integration: realistic multi-turn scenario ─────────────────────────


def test_realistic_onboarding_like_scenario() -> None:
    """Simulate a realistic short conversation: agent greets, asks something,
    saves the answer as a fact, calls a tool, logs a decision, exits.

    This is the kind of trace the Conductor will produce (with a real LLM
    instead of ScriptedLLM)."""
    seen_oui: list[str] = []

    def lookup_oui(*, mac: str) -> dict[str, str]:
        seen_oui.append(mac)
        return {"vendor": "Lutron"}

    tools = {
        "lookup_oui": ToolSpec(
            name="lookup_oui", description="MAC → vendor",
            fn=lookup_oui, schema={"mac": "string"},
        ),
    }

    decisions = [
        SpeakDecision(text="Welcome. Quick question first."),
        AskDecision(question="What's your primary use case for this network?"),
        SaveFactDecision(
            field_path="household_profile.use_case",
            value="work-from-home",
            confidence=0.9,
            evidence=["operator answered: 'mostly home office'"],
        ),
        CallToolDecision(tool="lookup_oui", args={"mac": "60:64:05:00:00:01"}),
        LogDecisionDecision(entry={
            "action": "identified_vendor",
            "mac": "60:64:05:00:00:01",
            "vendor": "Lutron",
        }),
        SpeakDecision(text="Got it — that's a Lutron device on your network."),
        DoneDecision(reason="initial intake complete"),
    ]

    said, _, durable, session = _harness(
        decisions, tools=tools, user_inputs=["mostly home office"],
    )

    assert said == [
        "Welcome. Quick question first.",
        "What's your primary use case for this network?",
        "Got it — that's a Lutron device on your network.",
    ]
    assert len(durable.facts) == 1
    assert durable.facts[0]["field"] == "household_profile.use_case"
    assert len(durable.decisions) == 1
    assert durable.decisions[0]["vendor"] == "Lutron"
    assert seen_oui == ["60:64:05:00:00:01"]
    assert len(session.tool_calls) == 1
