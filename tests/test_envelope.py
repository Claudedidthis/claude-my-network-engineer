"""Tests for HandoffEnvelope structural invariants I1-I5.

Each invariant has a positive test (passes when conditions met) and a
negative test (raises when conditions violated). Together they verify the
schema delivers the structural negative-feedback contract documented in
docs/handoff_envelope_design.md.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from network_engineer.tools.envelope import (
    ConfidenceBasis,
    ContextLayer,
    HandoffEnvelope,
    SignalRef,
    deterministic_envelope,
    llm_envelope,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _signal(layer: ContextLayer, source_id: str = "test") -> SignalRef:
    return SignalRef(
        layer=layer,
        source_id=source_id,
        evidence={"checked": True},
        measured_at=datetime.now(UTC),
    )


def _base_kwargs(**overrides):
    base = {
        "envelope_id": "test:envelope:1",
        "source_agent": "auditor",
        "artifact_type": "Finding",
        "supporting_signals": [_signal(ContextLayer.L3_LIVE_STATE)],
        "confidence": 0.5,
        "confidence_basis": ConfidenceBasis.UNCERTAIN,
        "approval_tier": "INFORMATIONAL",
        "payload": {"severity": "INFO", "code": "TEST"},
    }
    base.update(overrides)
    return base


# ── I1: supporting_signals is non-empty ──────────────────────────────────────

def test_i1_empty_supporting_signals_rejected() -> None:
    """Pydantic min_length=1 enforces I1 directly."""
    with pytest.raises(ValidationError, match="at least 1 item"):
        HandoffEnvelope(**_base_kwargs(supporting_signals=[]))


def test_i1_one_signal_accepted() -> None:
    env = HandoffEnvelope(**_base_kwargs())
    assert len(env.supporting_signals) == 1


# ── I2: HIGH/CRITICAL requires ≥2 distinct layers ───────────────────────────

def test_i2_high_severity_with_one_layer_rejected() -> None:
    with pytest.raises(ValidationError, match="Triangulation violation"):
        HandoffEnvelope(**_base_kwargs(
            payload={"severity": "HIGH", "code": "WIFI_X"},
            confidence=0.6,
            signals_that_would_invalidate=["radio config changed"],
        ))


def test_i2_critical_severity_with_one_layer_rejected() -> None:
    with pytest.raises(ValidationError, match="Triangulation"):
        HandoffEnvelope(**_base_kwargs(
            payload={"severity": "CRITICAL", "code": "OPEN_WIFI"},
            confidence=0.6,
            signals_that_would_invalidate=["portal active"],
        ))


def test_i2_high_severity_with_two_distinct_layers_accepted() -> None:
    env = HandoffEnvelope(**_base_kwargs(
        supporting_signals=[
            _signal(ContextLayer.L3_LIVE_STATE),
            _signal(ContextLayer.L0_DOMAIN_KNOWLEDGE),
        ],
        payload={"severity": "HIGH", "code": "WIFI_X"},
        confidence=0.7,
        confidence_basis=ConfidenceBasis.RETRIEVAL_GROUNDED,
        signals_that_would_invalidate=["channel changed"],
    ))
    assert env.independent_layer_count == 2


def test_i2_two_signals_same_layer_does_not_satisfy() -> None:
    """Two signals from L3 alone do NOT count as triangulation."""
    with pytest.raises(ValidationError, match="Triangulation"):
        HandoffEnvelope(**_base_kwargs(
            supporting_signals=[
                _signal(ContextLayer.L3_LIVE_STATE, "check_a"),
                _signal(ContextLayer.L3_LIVE_STATE, "check_b"),
            ],
            payload={"severity": "HIGH", "code": "X"},
            confidence=0.7,
            signals_that_would_invalidate=["x"],
        ))


def test_i2_low_severity_can_have_one_layer() -> None:
    """Single-source LOW findings are legitimate (e.g., vendor EOL fact)."""
    env = HandoffEnvelope(**_base_kwargs(
        payload={"severity": "LOW", "code": "VENDOR_EOL"},
        confidence=0.4,
    ))
    assert env.independent_layer_count == 1


# ── I3: overconfidence rejection ─────────────────────────────────────────────

def test_i3_high_confidence_no_missing_context_rejected_for_llm() -> None:
    with pytest.raises(ValidationError, match="Overconfidence violation"):
        HandoffEnvelope(**_base_kwargs(
            confidence=0.95,
            confidence_basis=ConfidenceBasis.LLM_SELF_REPORT,
            known_missing_context=[],   # empty → suspicious for LLM_SELF_REPORT
        ))


def test_i3_high_confidence_no_missing_context_allowed_for_deterministic() -> None:
    """Deterministic agents with complete coverage may legitimately claim 0.95+."""
    env = HandoffEnvelope(**_base_kwargs(
        confidence=0.95,
        confidence_basis=ConfidenceBasis.DETERMINISTIC_AGGREGATE,
        known_missing_context=[],
    ))
    assert env.confidence == 0.95


def test_i3_high_confidence_with_acknowledged_missing_context_allowed() -> None:
    env = HandoffEnvelope(**_base_kwargs(
        confidence=0.92,
        confidence_basis=ConfidenceBasis.RETRIEVAL_GROUNDED,
        known_missing_context=["firmware version not verified"],
    ))
    assert env.confidence == 0.92


# ── I4: LLM-self-report cannot escalate severity alone ───────────────────────

def test_i4_llm_self_report_high_severity_rejected() -> None:
    """LLM-self-report alone cannot drive HIGH severity."""
    with pytest.raises(ValidationError, match="LLM-self-report severity cap"):
        HandoffEnvelope(**_base_kwargs(
            supporting_signals=[
                _signal(ContextLayer.L3_LIVE_STATE),
                _signal(ContextLayer.L0_DOMAIN_KNOWLEDGE),
            ],
            payload={"severity": "HIGH", "code": "X"},
            confidence=0.7,
            confidence_basis=ConfidenceBasis.LLM_SELF_REPORT,
            known_missing_context=["did not retrieve sources"],
            signals_that_would_invalidate=["citation found"],
        ))


def test_i4_llm_self_report_medium_severity_allowed() -> None:
    """LLM-self-report can drive MEDIUM severity (the cap is HIGH)."""
    env = HandoffEnvelope(**_base_kwargs(
        supporting_signals=[
            _signal(ContextLayer.L3_LIVE_STATE),
            _signal(ContextLayer.L0_DOMAIN_KNOWLEDGE),
        ],
        payload={"severity": "MEDIUM", "code": "X"},
        confidence=0.6,
        confidence_basis=ConfidenceBasis.LLM_SELF_REPORT,
        known_missing_context=["did not retrieve sources"],
        signals_that_would_invalidate=["citation found"],
    ))
    assert env.severity == "MEDIUM"


# ── I5: falsifiability requirement ───────────────────────────────────────────

def test_i5_actionable_claim_without_invalidation_signals_rejected() -> None:
    with pytest.raises(ValidationError, match="Falsifiability violation"):
        HandoffEnvelope(**_base_kwargs(
            supporting_signals=[
                _signal(ContextLayer.L3_LIVE_STATE),
                _signal(ContextLayer.L0_DOMAIN_KNOWLEDGE),
            ],
            payload={"severity": "HIGH", "code": "X"},
            confidence=0.7,
            confidence_basis=ConfidenceBasis.RETRIEVAL_GROUNDED,
            known_missing_context=["x"],
            signals_that_would_invalidate=[],   # empty — refuses falsifiability
        ))


def test_i5_low_severity_does_not_require_invalidation_signals() -> None:
    env = HandoffEnvelope(**_base_kwargs(
        payload={"severity": "LOW", "code": "X"},
        confidence=0.4,
        signals_that_would_invalidate=[],
    ))
    assert env.signals_that_would_invalidate == []


# ── Factory helpers ──────────────────────────────────────────────────────────

def test_deterministic_envelope_factory_high_confidence_with_two_layers() -> None:
    env = deterministic_envelope(
        source_agent="auditor",
        artifact_type="Finding",
        payload={"severity": "INFO", "code": "X"},
        supporting_signals=[
            _signal(ContextLayer.L3_LIVE_STATE),
            _signal(ContextLayer.L0_DOMAIN_KNOWLEDGE),
        ],
        snapshot_id="snap_1",
        coverage_complete=True,
    )
    assert env.confidence == 0.95
    assert env.confidence_basis == ConfidenceBasis.DETERMINISTIC_AGGREGATE


def test_deterministic_envelope_lower_confidence_with_one_layer() -> None:
    env = deterministic_envelope(
        source_agent="auditor",
        artifact_type="Finding",
        payload={"severity": "INFO", "code": "X"},
        supporting_signals=[_signal(ContextLayer.L3_LIVE_STATE)],
        snapshot_id="snap_1",
        coverage_complete=True,
    )
    assert env.confidence == 0.75


def test_deterministic_envelope_uncertain_when_coverage_incomplete() -> None:
    env = deterministic_envelope(
        source_agent="auditor",
        artifact_type="Finding",
        payload={"severity": "INFO", "code": "X"},
        supporting_signals=[_signal(ContextLayer.L3_LIVE_STATE)],
        snapshot_id="snap_1",
        coverage_complete=False,
    )
    assert env.confidence_basis == ConfidenceBasis.UNCERTAIN


def test_llm_envelope_with_citations_is_retrieval_grounded() -> None:
    env = llm_envelope(
        source_agent="ai_runtime",
        artifact_type="Recommendation",
        payload={"severity": "INFO", "verdict": "safe"},
        supporting_signals=[_signal(ContextLayer.L0_DOMAIN_KNOWLEDGE)],
        snapshot_id="snap_1",
        citations=["unifi_docs#vlan_iot", "cis_benchmark#network"],
        known_missing_context=["firmware version"],
        invalidating_signals=["firmware advisory"],
    )
    assert env.confidence_basis == ConfidenceBasis.RETRIEVAL_GROUNDED
    assert env.confidence == 0.7   # 0.5 + 0.1 * 2 citations


def test_llm_envelope_without_citations_is_self_report() -> None:
    env = llm_envelope(
        source_agent="ai_runtime",
        artifact_type="Recommendation",
        payload={"severity": "INFO", "verdict": "safe"},
        supporting_signals=[_signal(ContextLayer.L3_LIVE_STATE)],
        snapshot_id="snap_1",
        citations=[],
        known_missing_context=["no sources retrieved"],
        invalidating_signals=["citation found"],
    )
    assert env.confidence_basis == ConfidenceBasis.LLM_SELF_REPORT
    assert env.confidence == 0.6
