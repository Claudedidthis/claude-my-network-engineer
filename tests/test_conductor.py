"""Tests for the Conductor — agent assembly + LLM adapter + tool registry.

The Conductor itself is a thin composition layer; most of the heavy
behavior is tested in test_agent_loop.py and test_durable_memory.py.
This file covers:
  - Conductor instantiation and run() with a fake AIRuntime
  - The conductor_llm tool-use parsing
  - Tool registry construction (corpus stubs, optional dependencies)
  - Session digest writing
  - Bootstrap behavior (no working memory yet)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from network_engineer.agents.conductor import Conductor, ConductorConfig
from network_engineer.agents.conductor_llm import (
    AIRuntimeAgentLLM,
    _VIRTUAL_TOOLS,
)
from network_engineer.agents.conductor_tools import (
    _stub_cite_corpus,
    _stub_evaluate_against_corpus,
    build_conductor_tools,
)
from network_engineer.tools.agent_loop import (
    AskDecision,
    CallToolDecision,
    DoneDecision,
    LogDecisionDecision,
    SaveFactDecision,
    SpeakDecision,
    Turn,
)
from network_engineer.tools.durable_memory import DurableMemory


# ── Test doubles ─────────────────────────────────────────────────────────────


class _FakeBlock:
    """Mimics Anthropic content blocks."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeMessage:
    """Mimics Anthropic Message response."""

    def __init__(self, content: list[Any], stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, response: _FakeMessage) -> None:
        self.response = response
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.last_kwargs = kwargs
        return self.response


class _FakeAnthropic:
    def __init__(self, response: _FakeMessage) -> None:
        self.messages = _FakeMessages(response)


def _make_runtime(content: list[Any]) -> Any:
    """Build a minimal AIRuntime-like stub that conductor_llm can use."""
    from network_engineer.agents.ai_runtime import AIRuntime
    fake = _FakeAnthropic(_FakeMessage(content))
    return AIRuntime(enabled=True, client=fake)


# ── conductor_llm: virtual tools advertised correctly ──────────────────────


def test_virtual_tools_include_all_six_decision_kinds() -> None:
    names = {t["name"] for t in _VIRTUAL_TOOLS}
    assert names == {"speak", "ask_operator", "save_fact", "log_decision", "done_for_now"}


def test_save_fact_virtual_tool_requires_field_path_value_confidence_evidence() -> None:
    save_tool = next(t for t in _VIRTUAL_TOOLS if t["name"] == "save_fact")
    required = set(save_tool["input_schema"]["required"])
    assert required == {"field_path", "value", "confidence", "evidence"}


# ── conductor_llm: tool_use parsing ─────────────────────────────────────────


def test_parse_speak_tool_use_returns_speak_decision() -> None:
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="speak", input={"text": "Hello operator"}),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    decision = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(decision, SpeakDecision)
    assert decision.text == "Hello operator"


def test_parse_ask_tool_use_returns_ask_decision() -> None:
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="ask_operator",
                   input={"question": "Use case?"}),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    d = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d, AskDecision)
    assert d.question == "Use case?"


def test_parse_save_fact_tool_use_returns_save_fact_decision() -> None:
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="save_fact", input={
            "field_path": "household_profile.use_case",
            "value": "home office",
            "confidence": 0.9,
            "evidence": ["operator turn 1"],
        }),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    d = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d, SaveFactDecision)
    assert d.field_path == "household_profile.use_case"
    assert d.confidence == 0.9


def test_parse_done_tool_use_returns_done_decision() -> None:
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="done_for_now",
                   input={"reason": "operator signed off"}),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    d = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d, DoneDecision)
    assert d.reason == "operator signed off"


def test_parse_log_decision_tool_use_returns_log_decision() -> None:
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="log_decision",
                   input={"entry": {"action": "save_origin_story"}}),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    d = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d, LogDecisionDecision)
    assert d.entry == {"action": "save_origin_story"}


def test_parse_real_tool_use_returns_call_tool_decision() -> None:
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="lookup_oui_vendor",
                   input={"mac": "60:64:05:00:00:01"}),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    d = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d, CallToolDecision)
    assert d.tool == "lookup_oui_vendor"
    assert d.args == {"mac": "60:64:05:00:00:01"}


def test_parse_text_only_response_returns_speak_decision() -> None:
    """When the LLM emits text without a tool_use, treat it as a speak decision."""
    runtime = _make_runtime([
        _FakeBlock(type="text", text="Just a short narrative response."),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    d = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d, SpeakDecision)
    assert d.text == "Just a short narrative response."


def test_parse_empty_response_returns_done() -> None:
    """Defensive: empty content → end the session rather than spin."""
    runtime = _make_runtime([])
    llm = AIRuntimeAgentLLM(runtime)
    d = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d, DoneDecision)


def test_disabled_ai_runtime_emits_speak_explanation() -> None:
    """When AIRuntime is disabled, the adapter explains rather than calling."""
    from network_engineer.agents.ai_runtime import AIRuntime
    llm = AIRuntimeAgentLLM(AIRuntime(enabled=False))
    d = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d, SpeakDecision)
    assert "disabled" in d.text.lower()


# ── conductor_llm: messages assembly ────────────────────────────────────────


def test_messages_with_no_working_memory_includes_bootstrap_nudge() -> None:
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="done_for_now", input={}),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    msgs = runtime._client.messages.last_kwargs["messages"]
    # Should have a bootstrap user message
    assert any("loop-bootstrap" in m["content"] for m in msgs)


def test_messages_replay_working_memory() -> None:
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="done_for_now", input={}),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    wm = [
        Turn(role="user", content="Hi"),
        Turn(role="assistant", content="Hello"),
        Turn(role="user", content="What's my IP?"),
    ]
    llm.decide(
        system_prompt="(test)", working_memory=wm,
        session_summary="", durable_subset="", tools={},
    )
    msgs = runtime._client.messages.last_kwargs["messages"]
    contents = [m["content"] for m in msgs]
    assert "Hi" in contents
    assert "Hello" in contents
    assert "What's my IP?" in contents


def test_messages_end_on_user_role() -> None:
    """Anthropic requires conversations end on user-role; a trailing
    assistant turn gets a continuation nudge."""
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="done_for_now", input={}),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    wm = [
        Turn(role="user", content="Hi"),
        Turn(role="assistant", content="Hello"),
    ]
    llm.decide(
        system_prompt="(test)", working_memory=wm,
        session_summary="", durable_subset="", tools={},
    )
    msgs = runtime._client.messages.last_kwargs["messages"]
    assert msgs[-1]["role"] == "user"


# ── conductor_tools: tool registry ──────────────────────────────────────────


def test_corpus_tools_are_stubbed() -> None:
    eval_result = _stub_evaluate_against_corpus(
        action="open_port_22_wan",
        current_state={},
    )
    assert eval_result["corpus_loaded"] is False
    assert eval_result["severity"] is None

    cite_result = _stub_cite_corpus(source_id="red-005-ssh-telnet-wan-exposed")
    assert cite_result["corpus_loaded"] is False


def test_build_conductor_tools_includes_corpus_stubs(tmp_path: Path) -> None:
    durable = DurableMemory(runs_dir=tmp_path / "runs", session_id="s")
    tools = build_conductor_tools(
        durable_memory=durable,
        unifi_client=None,
        ai_runtime=None,
        session_id="s",
    )
    assert "evaluate_against_corpus" in tools
    assert "cite_corpus" in tools


def test_build_conductor_tools_omits_unifi_tools_when_no_client(tmp_path: Path) -> None:
    durable = DurableMemory(runs_dir=tmp_path / "runs", session_id="s")
    tools = build_conductor_tools(
        durable_memory=durable, unifi_client=None,
        ai_runtime=None, session_id="s",
    )
    # Unifi-dependent tools should not be present
    assert "read_snapshot" not in tools
    assert "audit_network" not in tools
    # Memory-only tools always present
    assert "query_history" in tools
    assert "list_cautions" in tools


def test_build_conductor_tools_includes_unifi_tools_when_client_present(tmp_path: Path) -> None:
    from network_engineer.tools.unifi_client import UnifiClient
    durable = DurableMemory(runs_dir=tmp_path / "runs", session_id="s")
    client = UnifiClient(use_fixtures=True)
    tools = build_conductor_tools(
        durable_memory=durable, unifi_client=client,
        ai_runtime=None, session_id="s",
    )
    assert "read_snapshot" in tools
    assert "audit_network" in tools
    assert "lookup_oui_vendor" in tools


# ── Conductor: end-to-end with scripted LLM ─────────────────────────────────


def test_conductor_runs_with_scripted_llm_to_done(tmp_path: Path) -> None:
    """Run a complete (very short) Conductor session against a scripted LLM
    that emits a speak then a done. Verifies wiring works end-to-end."""
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="speak", input={"text": "Hi"}),
        _FakeBlock(type="tool_use", name="done_for_now", input={}),
    ])

    # Two-step: first decide() returns speak (consumes the response), but
    # our fake messages.create always returns the same response. We need
    # a multi-response fake.
    class _SequencedMessages:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0

        def create(self, **kwargs):
            r = self.responses[min(self.calls, len(self.responses) - 1)]
            self.calls += 1
            return r

    seq_messages = _SequencedMessages([
        _FakeMessage([_FakeBlock(type="tool_use", name="speak", input={"text": "Hi"})]),
        _FakeMessage([_FakeBlock(type="tool_use", name="done_for_now", input={})]),
    ])
    seq_anthropic = type("X", (), {"messages": seq_messages})()
    from network_engineer.agents.ai_runtime import AIRuntime
    runtime = AIRuntime(enabled=True, client=seq_anthropic)

    durable = DurableMemory(runs_dir=tmp_path / "runs", session_id="sess-test")
    config = ConductorConfig(runs_dir=tmp_path / "runs", max_turns=10)

    said: list[str] = []
    inputs: list[str] = []

    conductor = Conductor(
        config=config, ai_runtime=runtime,
        unifi_client=None, durable_memory=durable,
    )
    conductor.run(on_say=said.append, on_user_input=lambda _: inputs.pop(0) if inputs else "")

    assert said == ["Hi"]
    # A session digest should have been written
    assert (tmp_path / "runs" / "session_digests").exists()


def test_conductor_writes_digest_even_on_loop_exhaustion(tmp_path: Path) -> None:
    """If the loop blows past max_turns, the digest still gets written
    so the session leaves an audit trail."""
    response = _FakeMessage([
        _FakeBlock(type="tool_use", name="speak", input={"text": "loop"}),
    ])

    class _AlwaysSpeak:
        def create(self, **kwargs):
            return response

    fake = type("X", (), {"messages": _AlwaysSpeak()})()
    from network_engineer.agents.ai_runtime import AIRuntime
    runtime = AIRuntime(enabled=True, client=fake)

    durable = DurableMemory(runs_dir=tmp_path / "runs", session_id="s")
    config = ConductorConfig(runs_dir=tmp_path / "runs", max_turns=3)

    conductor = Conductor(
        config=config, ai_runtime=runtime,
        unifi_client=None, durable_memory=durable,
    )

    with pytest.raises(Exception):  # AgentLoopExhausted
        conductor.run(
            on_say=lambda _: None,
            on_user_input=lambda _: "",
        )

    # Digest should have been written before the exception propagated
    assert any((tmp_path / "runs" / "session_digests").glob("*.md"))


def test_conductor_with_disabled_ai_runtime_exits_gracefully(tmp_path: Path) -> None:
    """When AI runtime is disabled, the adapter emits a speak with an
    explanation and the session ends."""
    from network_engineer.agents.ai_runtime import AIRuntime
    runtime = AIRuntime(enabled=False)
    durable = DurableMemory(runs_dir=tmp_path / "runs", session_id="s")
    config = ConductorConfig(runs_dir=tmp_path / "runs", max_turns=10)

    said: list[str] = []
    conductor = Conductor(
        config=config, ai_runtime=runtime,
        unifi_client=None, durable_memory=durable,
    )
    conductor.run(on_say=said.append, on_user_input=lambda _: "")
    # Should have emitted the disabled-AI message and exited
    assert any("disabled" in s.lower() for s in said)
