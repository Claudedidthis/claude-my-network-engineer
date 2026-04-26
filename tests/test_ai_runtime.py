"""Tests for the AIRuntime agent — covers fallback, mocked, and live paths."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from network_engineer.agents.ai_runtime import (
    AIRuntime,
    AIRuntimeError,
    _SYSTEM_CHANGE_REVIEW,
    _SYSTEM_SECURITY,
    _UNTRUSTED_DATA_PREAMBLE,
    _is_sensitive_action,
    _normalize_severity,
    _output_fingerprint,
    _security_context,
    _strip_json,
)
from network_engineer.tools.schemas import ChangeReview, SecurityAnalysis, Severity

# ── Fake Anthropic client ─────────────────────────────────────────────────────

class _FakeUsage:
    def __init__(
        self,
        input_tokens: int = 1000,
        output_tokens: int = 500,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(
        self,
        text: str,
        usage: _FakeUsage | None = None,
        model: str = "fake-model",
    ) -> None:
        self.content = [_FakeBlock(text)]
        self.usage = usage or _FakeUsage()
        self.model = model
        self.id = "msg_test"


class _FakeMessages:
    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.last_kwargs: dict[str, Any] | None = None
        self.call_count = 0

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.last_kwargs = kwargs
        self.call_count += 1
        return _FakeMessage(self._response_text)


class _FakeAnthropic:
    def __init__(self, response_text: str) -> None:
        self.messages = _FakeMessages(response_text)


def _make_runtime(response_text: str) -> tuple[AIRuntime, _FakeAnthropic]:
    fake = _FakeAnthropic(response_text)
    runtime = AIRuntime(enabled=True, client=fake)
    return runtime, fake


# ── Fixture data ──────────────────────────────────────────────────────────────

def _toy_snapshot() -> dict[str, Any]:
    return {
        "networks": [{"name": "Default", "vlan": 1, "subnet": "192.168.1.0/24"}],
        "wifi_networks": [
            {"name": "Home", "security": "wpapsk", "enabled": True},
            {"name": "Guest-Voucher-Net", "security": "open", "enabled": True, "is_guest": True},
        ],
        "clients": [
            {"name": "Lutron light controller", "ipAddress": "192.168.1.114"},
            {"name": "Macbook", "ipAddress": "192.168.1.50"},
        ],
        "firewall_rules": [],
        "port_forwards": [],
        "devices": [{"name": "UDM", "model": "UDM", "state": "ONLINE"}],
    }


# ── Fallback path (AI disabled) ──────────────────────────────────────────────

def test_security_analysis_fallback_returns_placeholder() -> None:
    runtime = AIRuntime(enabled=False)
    result = runtime.analyze_security_posture(_toy_snapshot())
    assert isinstance(result, SecurityAnalysis)
    assert result.generated_by == "deterministic_fallback"
    assert result.overall_posture == "unknown"
    assert result.score == 0
    assert result.issues == []
    assert "AI runtime is disabled" in result.summary


def test_security_analysis_fallback_does_not_call_anthropic() -> None:
    runtime = AIRuntime(enabled=False)
    # No client should have been initialized
    assert runtime._client is None
    runtime.analyze_security_posture(_toy_snapshot())  # must not raise


def test_change_review_fallback_returns_placeholder() -> None:
    runtime = AIRuntime(enabled=False)
    review = runtime.review_config_change({"new": "thing"}, {"old": "thing"})
    assert isinstance(review, ChangeReview)
    assert review.generated_by == "deterministic_fallback"
    assert review.verdict == "risky"   # default-deny when no AI review available


def test_explain_anomaly_fallback() -> None:
    runtime = AIRuntime(enabled=False)
    text = runtime.explain_anomaly({"event_type": "WAN_LATENCY_HIGH"})
    assert "disabled" in text.lower()


def test_score_upgrade_recommendation_fallback() -> None:
    runtime = AIRuntime(enabled=False)
    score = runtime.score_upgrade_recommendation({"device": "UAP-AC-Lite"})
    assert score["score"] == 0
    assert score["generated_by"] == "deterministic_fallback"


def test_runtime_respects_ai_runtime_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_RUNTIME_ENABLED", "false")
    runtime = AIRuntime()
    assert runtime.enabled is False


def test_runtime_enabled_without_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AIRuntimeError, match="ANTHROPIC_API_KEY"):
        AIRuntime(enabled=True)


# ── Enabled path with mocked Anthropic ───────────────────────────────────────

def test_security_analysis_parses_ai_json_response() -> None:
    response = json.dumps({
        "overall_posture": "weak",
        "score": 45,
        "issues": [
            {
                "severity": "HIGH",
                "code": "IOT_ON_TRUSTED_VLAN",
                "title": "IoT and smart-home devices share the trusted LAN",
                "description": "Lutron and Hue controllers share 192.168.1.0/24 with laptops.",
                "affected": ["Lutron light controller", "Philips Hue"],
                "recommendation": "Migrate IoT clients to a dedicated VLAN.",
            }
        ],
        "summary": "Overall posture is weak — primary concern is missing IoT segmentation.",
    })
    runtime, _ = _make_runtime(response)
    result = runtime.analyze_security_posture(_toy_snapshot())

    assert isinstance(result, SecurityAnalysis)
    assert result.generated_by == "ai"
    assert result.overall_posture == "weak"
    assert result.score == 45
    assert len(result.issues) == 1
    assert result.issues[0].code == "IOT_ON_TRUSTED_VLAN"
    assert result.issues[0].severity == Severity.HIGH


def test_security_analysis_handles_code_fences() -> None:
    response = "```json\n" + json.dumps({
        "overall_posture": "moderate", "score": 60,
        "issues": [], "summary": "ok",
    }) + "\n```"
    runtime, _ = _make_runtime(response)
    result = runtime.analyze_security_posture(_toy_snapshot())
    assert result.overall_posture == "moderate"
    assert result.score == 60


def test_security_analysis_handles_non_json_gracefully() -> None:
    runtime, _ = _make_runtime("Sorry, I cannot do that.")
    result = runtime.analyze_security_posture(_toy_snapshot())
    assert result.generated_by == "ai"
    assert result.overall_posture == "unknown"
    assert "non-JSON" in result.summary


# ── Prompt-injection hardening (untrusted-data boundary) ─────────────────────

def test_untrusted_data_preamble_present_in_security_prompt() -> None:
    assert _UNTRUSTED_DATA_PREAMBLE in _SYSTEM_SECURITY
    assert "UNTRUSTED DATA" in _SYSTEM_SECURITY
    assert "Never follow" in _SYSTEM_SECURITY


def test_untrusted_data_preamble_present_in_change_review_prompt() -> None:
    assert _UNTRUSTED_DATA_PREAMBLE in _SYSTEM_CHANGE_REVIEW
    assert "UNTRUSTED DATA" in _SYSTEM_CHANGE_REVIEW


def test_security_system_prompt_starts_with_security_boundary() -> None:
    """The boundary block must come first — anything after it is downstream
    of an established boundary, but anything BEFORE it would be unguarded."""
    assert _SYSTEM_SECURITY.startswith("SECURITY BOUNDARY")


def test_change_review_system_prompt_starts_with_security_boundary() -> None:
    assert _SYSTEM_CHANGE_REVIEW.startswith("SECURITY BOUNDARY")


def test_parse_failure_never_returns_raw_content_in_security_summary() -> None:
    """Parse failure must NOT include raw model output in the returned summary —
    the model output may itself contain prompt-injected payload echoed back
    from NETWORK CONTEXT, or operator-private details."""
    # A "raw" string that, if leaked, would clearly identify itself
    canary = "CANARY_RAW_LEAK_a3f7b9c2_should_never_appear_anywhere"
    runtime, _ = _make_runtime(canary)
    result = runtime.analyze_security_posture(_toy_snapshot())
    assert canary not in result.summary
    assert canary not in json.dumps(result.model_dump(mode="json"), default=str)


def test_parse_failure_never_returns_raw_content_in_change_review_reasoning() -> None:
    canary = "CANARY_RAW_LEAK_d8e1f5b6_should_never_appear_anywhere"
    runtime, _ = _make_runtime(canary)
    review = runtime.review_config_change(
        {"name": "Garage AP"}, {"name": "AP-1"}, action="rename_device",
    )
    assert canary not in review.reasoning
    assert canary not in json.dumps(review.model_dump(mode="json"), default=str)


def test_parse_failure_never_returns_raw_content_in_upgrade_score() -> None:
    canary = "CANARY_RAW_LEAK_72fe_should_never_appear_anywhere"
    runtime, _ = _make_runtime(canary)
    result = runtime.score_upgrade_recommendation({"device": "UAP-AC-Lite"})
    assert canary not in json.dumps(result, default=str)


def test_parse_failure_summary_references_output_hash() -> None:
    """Operators need a way to correlate the user-facing message back to the
    structured error log entry — the output hash is that handle."""
    runtime, _ = _make_runtime("not json at all")
    result = runtime.analyze_security_posture(_toy_snapshot())
    assert "hash" in result.summary.lower()


def test_output_fingerprint_returns_hash_and_length_only() -> None:
    fp = _output_fingerprint("hello world")
    assert set(fp.keys()) == {"hash", "length"}
    assert fp["length"] == 11
    assert len(fp["hash"]) == 16  # 16-char prefix
    # No part of the hash should be guessable from short inputs without sha256
    assert fp["hash"] == _output_fingerprint("hello world")["hash"]
    assert fp["hash"] != _output_fingerprint("hello worlD")["hash"]


def test_security_analysis_records_token_usage() -> None:
    response = json.dumps({
        "overall_posture": "strong", "score": 90, "issues": [], "summary": "fine",
    })
    runtime, _ = _make_runtime(response)
    result = runtime.analyze_security_posture(_toy_snapshot())
    assert result.token_usage is not None
    assert result.token_usage["input_tokens"] == 1000
    assert result.token_usage["output_tokens"] == 500


def test_security_analysis_records_model_used() -> None:
    response = json.dumps({"overall_posture": "strong", "score": 90, "issues": [], "summary": "ok"})
    runtime, fake = _make_runtime(response)
    result = runtime.analyze_security_posture(_toy_snapshot())
    # Opus is the configured model for this job
    assert result.model_used == "claude-opus-4-7"
    assert fake.messages.last_kwargs is not None
    assert fake.messages.last_kwargs["model"] == "claude-opus-4-7"


def test_security_analysis_uses_two_cache_breakpoints() -> None:
    response = json.dumps({"overall_posture": "strong", "score": 90, "issues": [], "summary": "ok"})
    runtime, fake = _make_runtime(response)
    runtime.analyze_security_posture(_toy_snapshot())

    system = fake.messages.last_kwargs["system"]
    assert isinstance(system, list)
    assert len(system) == 2
    assert all(s.get("cache_control", {}).get("type") == "ephemeral" for s in system)


def test_security_analysis_sends_user_message_uncached() -> None:
    response = json.dumps({"overall_posture": "strong", "score": 90, "issues": [], "summary": "ok"})
    runtime, fake = _make_runtime(response)
    runtime.analyze_security_posture(_toy_snapshot())
    messages = fake.messages.last_kwargs["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    # User content is a plain string — no cache_control key
    assert isinstance(messages[0]["content"], str)


def test_severity_normalization_handles_drift() -> None:
    response = json.dumps({
        "overall_posture": "weak",
        "score": 30,
        "issues": [{
            "severity": "warn",   # drifted form
            "code": "X", "title": "t", "description": "d", "affected": [], "recommendation": "r",
        }],
        "summary": "s",
    })
    runtime, _ = _make_runtime(response)
    result = runtime.analyze_security_posture(_toy_snapshot())
    assert result.issues[0].severity == Severity.MEDIUM


# ── Change review escalation ─────────────────────────────────────────────────

def test_change_review_default_uses_sonnet() -> None:
    response = json.dumps({
        "verdict": "safe", "reasoning": "low risk",
        "concerns": [], "questions": [], "suggested_alternatives": [],
    })
    runtime, fake = _make_runtime(response)
    runtime.review_config_change(
        {"name": "Garage AP"}, {"name": "AP-1"}, action="rename_device",
    )
    assert fake.messages.last_kwargs["model"] == "claude-sonnet-4-6"


def test_change_review_escalates_to_opus_for_firewall() -> None:
    response = json.dumps({
        "verdict": "risky", "reasoning": "firewall changes are high impact",
        "concerns": ["may break remote access"], "questions": [], "suggested_alternatives": [],
    })
    runtime, fake = _make_runtime(response)
    runtime.review_config_change(
        {"rule": "allow any"}, {}, action="create_firewall_rule",
    )
    assert fake.messages.last_kwargs["model"] == "claude-opus-4-7"


def test_change_review_escalates_to_opus_for_vlan() -> None:
    response = json.dumps({
        "verdict": "risky", "reasoning": "vlan is sensitive",
        "concerns": [], "questions": [], "suggested_alternatives": [],
    })
    runtime, fake = _make_runtime(response)
    runtime.review_config_change({"vlan": 30}, {}, action="create_vlan")
    assert fake.messages.last_kwargs["model"] == "claude-opus-4-7"


def test_change_review_escalates_when_proposed_mentions_camera() -> None:
    response = json.dumps({
        "verdict": "risky", "reasoning": "cameras are sensitive",
        "concerns": [], "questions": [], "suggested_alternatives": [],
    })
    runtime, fake = _make_runtime(response)
    runtime.review_config_change(
        {"target": "G4 camera firmware update"}, {}, action="firmware_update_any_device",
    )
    assert fake.messages.last_kwargs["model"] == "claude-opus-4-7"


def test_change_review_parses_response() -> None:
    response = json.dumps({
        "verdict": "safe",
        "reasoning": "Renaming is reversible.",
        "concerns": [],
        "questions": ["any naming convention?"],
        "suggested_alternatives": [],
    })
    runtime, _ = _make_runtime(response)
    review = runtime.review_config_change(
        {"name": "Garage AP"}, {"name": "AP-1"}, action="rename_device",
    )
    assert review.verdict == "safe"
    assert "Renaming" in review.reasoning
    assert review.questions == ["any naming convention?"]


# ── Helper functions ──────────────────────────────────────────────────────────

def test_is_sensitive_action_true_for_firewall_keyword() -> None:
    assert _is_sensitive_action("create_firewall_rule", {}) is True


def test_is_sensitive_action_true_for_camera_in_proposal() -> None:
    assert _is_sensitive_action("rename_device", {"target": "G4 camera"}) is True


def test_is_sensitive_action_false_for_simple_rename() -> None:
    assert _is_sensitive_action("rename_device", {"name": "AP-Office"}) is False


def test_strip_json_removes_code_fences() -> None:
    assert _strip_json("```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert _strip_json("```\n{\"a\": 1}\n```") == '{"a": 1}'


def test_strip_json_passes_through_plain_json() -> None:
    assert _strip_json('{"a": 1}') == '{"a": 1}'


def test_normalize_severity_handles_drift() -> None:
    assert _normalize_severity("warn") == "MEDIUM"
    assert _normalize_severity("WARNING") == "MEDIUM"
    assert _normalize_severity("error") == "HIGH"
    assert _normalize_severity("crit") == "CRITICAL"
    assert _normalize_severity("HIGH") == "HIGH"


def test_security_context_trims_to_relevant_fields() -> None:
    snap = {
        "networks": [{"name": "n", "vlan": 1, "extra_garbage": [1, 2, 3] * 1000}],
        "clients": [],
        "wifi_networks": [],
        "firewall_rules": [],
        "port_forwards": [],
        "devices": [],
    }
    ctx = _security_context(snap)
    assert "extra_garbage" not in ctx["networks"][0]
    assert "vlan" in ctx["networks"][0]


# ── Live integration ──────────────────────────────────────────────────────────

_LIVE_AI = pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — live AI tests require an Anthropic key",
)


@_LIVE_AI
def test_live_security_analysis_against_fixture() -> None:
    """The Phase 7 done-when criterion: real Opus call must name IOT-on-trusted-VLAN."""
    fixture = json.loads(
        Path(__file__).parent.joinpath("fixtures/baseline_snapshot.json").read_text()
    )
    runtime = AIRuntime(enabled=True)
    result = runtime.analyze_security_posture(fixture)

    assert result.generated_by == "ai"
    assert result.score >= 0
    # The fixture has IoT on the trusted /24 — Opus should flag it
    haystack = " ".join(
        [result.summary]
        + [i.code for i in result.issues]
        + [i.title for i in result.issues]
        + [i.description for i in result.issues]
    ).upper()
    assert "IOT" in haystack or "VLAN" in haystack or "SEGMEN" in haystack, (
        "Expected an IoT/VLAN/segmentation finding; got:\n"
        + json.dumps(result.model_dump(mode="json"), indent=2, default=str)
    )
