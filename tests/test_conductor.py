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
    _cite_corpus,
    _evaluate_against_corpus,
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
    assert any(
        "loop-bootstrap" in str(m.get("content", ""))
        for m in msgs
    )


def test_messages_fold_user_turns_from_working_memory() -> None:
    """User-role turns in working memory get added to api_messages on the
    next decide() call. Assistant turns DO NOT get replayed — they're
    added from API responses directly."""
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="done_for_now", input={}),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    wm = [
        Turn(role="user", content="Hi"),
        Turn(role="user", content="What's my IP?"),
    ]
    llm.decide(
        system_prompt="(test)", working_memory=wm,
        session_summary="", durable_subset="", tools={},
    )
    msgs = runtime._client.messages.last_kwargs["messages"]
    contents = [str(m["content"]) for m in msgs if m["role"] == "user"]
    assert any("Hi" in c for c in contents)
    assert any("What's my IP?" in c for c in contents)


def test_tool_use_correlates_with_tool_result_on_next_call() -> None:
    """When the model emits a tool_use with id 'toolu_xyz', the next turn's
    tool_observation must land as a tool_result block referencing 'toolu_xyz'.
    This is the bug that made the agent retry read_snapshot 13 times."""
    # Sequence two API responses: first emits tool_use, second is done.
    class _SequencedMessages:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0
            self.last_kwargs = None

        def create(self, **kwargs):
            self.last_kwargs = kwargs
            r = self.responses[min(self.calls, len(self.responses) - 1)]
            self.calls += 1
            return r

    seq = _SequencedMessages([
        _FakeMessage([
            _FakeBlock(type="tool_use", name="audit_network", id="toolu_audit_1", input={}),
        ]),
        _FakeMessage([
            _FakeBlock(type="tool_use", name="done_for_now", input={}),
        ]),
    ])
    fake_anthropic = type("X", (), {"messages": seq})()
    from network_engineer.agents.ai_runtime import AIRuntime
    runtime = AIRuntime(enabled=True, client=fake_anthropic)
    llm = AIRuntimeAgentLLM(runtime)

    # First call: model emits tool_use; we receive it as CallToolDecision.
    d1 = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d1, CallToolDecision)
    assert d1.tool == "audit_network"

    # Loop now runs the tool and adds a tool_observation to working memory.
    wm_after_tool = [
        Turn(role="tool_observation", content="audit_network(...) → 5 findings"),
    ]
    # Second call: the adapter must include a tool_result block correlated
    # with toolu_audit_1.
    d2 = llm.decide(
        system_prompt="(test)", working_memory=wm_after_tool,
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d2, DoneDecision)

    # Inspect the messages sent on the second call
    msgs = seq.last_kwargs["messages"]
    # Find a user-role message whose content contains a tool_result block
    tool_result_blocks = []
    for m in msgs:
        if m["role"] != "user":
            continue
        content = m["content"]
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_result_blocks.append(block)
    assert tool_result_blocks, "expected a tool_result block on the second API call"
    assert tool_result_blocks[0]["tool_use_id"] == "toolu_audit_1"
    assert "5 findings" in tool_result_blocks[0]["content"]


def test_two_tool_uses_in_one_response_correlate_via_fifo_queue() -> None:
    """REGRESSION (caught by code-review 2026-04-27): when the model emits
    multiple real tool_uses in one response, EACH must correlate with its
    matching tool_observation by tool_use_id. The single-slot
    _pending_tool_use_id design dropped the second correlation, recreating
    the runaway-tool-call bug (read_snapshot 13 times). FIFO queue fix."""
    class _SequencedMessages:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0
            self.last_kwargs = None

        def create(self, **kwargs):
            self.last_kwargs = kwargs
            r = self.responses[min(self.calls, len(self.responses) - 1)]
            self.calls += 1
            return r

    # First response: TWO real tool_uses (audit_network + identify_smart_home_brands)
    seq = _SequencedMessages([
        _FakeMessage([
            _FakeBlock(type="tool_use", name="audit_network", id="toolu_audit_1", input={}),
            _FakeBlock(type="tool_use", name="identify_smart_home_brands", id="toolu_brands_1", input={}),
        ]),
        _FakeMessage([
            _FakeBlock(type="tool_use", name="done_for_now", input={}),
        ]),
    ])
    fake_anthropic = type("X", (), {"messages": seq})()
    from network_engineer.agents.ai_runtime import AIRuntime
    runtime = AIRuntime(enabled=True, client=fake_anthropic)
    llm = AIRuntimeAgentLLM(runtime)

    # First decide(): returns the first CallToolDecision; the second is queued.
    d1 = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d1, CallToolDecision)
    assert d1.tool == "audit_network"

    # Second decide(): drains the queue without an API call — second tool.
    d2 = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d2, CallToolDecision)
    assert d2.tool == "identify_smart_home_brands"

    # The loop now runs both tools in sequence; observations land in working
    # memory in tool-call order.
    wm_after_both = [
        Turn(role="tool_observation", content="audit_network(...) → 5 findings"),
        Turn(role="tool_observation", content="identify_smart_home_brands(...) → ['lutron']"),
    ]
    # Third decide(): triggers a real API call. Both tool_results must be
    # in the messages, each correlated with its respective tool_use_id.
    d3 = llm.decide(
        system_prompt="(test)", working_memory=wm_after_both,
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d3, DoneDecision)

    # Inspect the messages sent on the second API call (calls=1 was the first;
    # calls=2 is this one's reply).
    msgs = seq.last_kwargs["messages"]
    tool_results = []
    for m in msgs:
        if m["role"] != "user":
            continue
        content = m["content"]
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_results.append(block)

    assert len(tool_results) == 2, (
        f"expected 2 tool_result blocks (one per tool_use), got {len(tool_results)}"
    )
    # FIFO ordering: first tool_use_id pairs with first tool_observation
    assert tool_results[0]["tool_use_id"] == "toolu_audit_1"
    assert "5 findings" in tool_results[0]["content"]
    assert tool_results[1]["tool_use_id"] == "toolu_brands_1"
    assert "lutron" in tool_results[1]["content"]


def test_api_messages_are_capped_at_max() -> None:
    """REGRESSION (caught by code-review 2026-04-27): _api_messages was
    unbounded. Long sessions would blow the model's context window. The
    buffer is now capped; the oldest non-anchor pair is dropped when
    over the limit."""
    runtime = _make_runtime([
        _FakeBlock(type="tool_use", name="speak", input={"text": "ack"}),
    ])
    llm = AIRuntimeAgentLLM(runtime)
    # Force-fill _api_messages well past the cap
    llm._api_messages = [{"role": "user", "content": "anchor"}]
    for i in range(120):
        role = "user" if i % 2 == 0 else "assistant"
        llm._api_messages.append({"role": role, "content": f"turn-{i}"})

    llm._enforce_message_cap()

    # Should be at-or-under the cap
    assert len(llm._api_messages) <= AIRuntimeAgentLLM._MAX_API_MESSAGES
    # The anchor (first message) must be preserved
    assert llm._api_messages[0]["content"] == "anchor"
    # New conversation start must be a user-role message (Anthropic invariant)
    if len(llm._api_messages) > 1:
        assert llm._api_messages[1]["role"] == "user"


def test_multi_block_response_emits_speak_then_call_tool() -> None:
    """When the model emits [text + tool_use] in one response, the loop
    should see a SpeakDecision FIRST (so the operator hears the
    narration), then the CallToolDecision on the next decide() call."""
    runtime = _make_runtime([
        _FakeBlock(type="text", text="I'll check your network now."),
        _FakeBlock(type="tool_use", name="audit_network", id="toolu_1", input={}),
    ])
    llm = AIRuntimeAgentLLM(runtime)

    d1 = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d1, SpeakDecision)
    assert "check your network" in d1.text

    # Second decide() drains the queue without making another API call
    d2 = llm.decide(
        system_prompt="(test)", working_memory=[],
        session_summary="", durable_subset="", tools={},
    )
    assert isinstance(d2, CallToolDecision)
    assert d2.tool == "audit_network"


# ── conductor_tools: tool registry ──────────────────────────────────────────


def test_record_caution_rejects_state_transition_fields(tmp_path: Path) -> None:
    """REGRESSION (caught by code-review 2026-04-27): _record_caution
    accepted **kwargs unfiltered, letting the LLM set state="resolved"
    and acknowledged_at directly. That bypasses the architecture §3.4
    asymmetry where only operator-acknowledge or system-resolution can
    transition a marker."""
    from network_engineer.agents.conductor_tools import _record_caution
    from network_engineer.tools.durable_memory import DurableMemory

    durable = DurableMemory(runs_dir=tmp_path / "runs", session_id="s")

    # LLM tries to inject a state transition into the new-marker call
    result = _record_caution(
        durable, "s",
        severity="RED", origin="audit_finding",
        target_kind="port_forward", target_key="x",
        canonical_source="src", counsel_text="t",
        state="resolved",  # ← injection attempt
        acknowledged_at="2026-04-26T00:00:00",  # ← injection attempt
    )

    # Should return the rejection error, not write a marker
    assert "error" in result
    assert "rejected_fields" in result
    assert "state" in result["rejected_fields"]
    assert "acknowledged_at" in result["rejected_fields"]
    # No marker should have been recorded
    assert durable.list_cautions() == []


def test_record_caution_with_whitelisted_fields_succeeds(tmp_path: Path) -> None:
    """Sanity: the happy-path with allowed fields still works."""
    from network_engineer.agents.conductor_tools import _record_caution
    from network_engineer.tools.durable_memory import DurableMemory

    durable = DurableMemory(runs_dir=tmp_path / "runs", session_id="s")
    result = _record_caution(
        durable, "s",
        severity="RED", origin="operator_override",
        target_kind="port_forward", target_key="ssh-22",
        canonical_source="NIST 800-53 SC-7",
        counsel_text="SSH on WAN is high severity per NIST.",
        operator_rationale="weekend access for vendor",
    )
    assert "error" not in result
    assert result["state"] == "active"  # always starts active
    assert result["severity"] == "RED"
    assert len(durable.list_cautions()) == 1


def test_corpus_tools_query_real_corpus() -> None:
    """The corpus tools now hit tools/corpus.py. With the v0.1 starter
    bundle landed (8 authored summaries), querying "ssh wan" should
    return red-005-ssh-telnet-wan-exposed with RED severity."""
    eval_result = _evaluate_against_corpus(
        action="port_forward",
        current_state={"port": 22, "destination": "WAN"},
    )
    assert eval_result["corpus_loaded"] is True
    # Best match should be the SSH-on-WAN summary
    assert "ssh" in eval_result["canonical_source"].lower()
    assert eval_result["severity"] == "RED"


def test_cite_corpus_returns_full_text() -> None:
    cite_result = _cite_corpus(source_id="red-005-ssh-telnet-wan-exposed")
    assert cite_result["corpus_loaded"] is True
    assert cite_result["source_id"] == "red-005-ssh-telnet-wan-exposed"
    assert "WAN" in cite_result["full_text"]


def test_cite_corpus_unknown_source_id_returns_message() -> None:
    cite_result = _cite_corpus(source_id="nonexistent-source-id")
    assert cite_result["corpus_loaded"] is True
    assert cite_result["source_id"] == "nonexistent-source-id"
    assert "No corpus entry found" in cite_result["excerpt"]


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
