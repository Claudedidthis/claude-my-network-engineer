"""HandoffEnvelope — the structural negative-feedback contract.

This module defines the contract every artifact must satisfy when one agent
hands off to another. It is the cheapest of the structural negative-feedback
mechanisms from the cascade self-healing analysis: it imposes constraints on
*shape* rather than runtime behaviour, but those constraints make several
classes of cascade failure structurally impossible.

The two mechanisms baked into this schema:

  (1) TRIANGULATION REQUIREMENT
      Every artifact must list `supporting_signals` from independent context
      layers. Severity above LOW requires ≥2 independent layers. Single-
      signal claims still flow but cannot escalate.

  (2) UNCERTAINTY DECLARATIONS
      Every artifact must populate `confidence`, `confidence_basis`,
      `known_missing_context`, and `signals_that_would_invalidate`.
      The framework rejects envelopes that show overconfidence (high
      confidence + no acknowledged missing context) — but the rejection
      is calibrated by `confidence_basis`: deterministic agents have a
      lower bar than LLM-generated content.

Why these specific shapes — see docs/handoff_envelope_design.md for the
research evidence, the dissenting argument on LLM self-assessment, and the
experimental framing (this is an architectural hypothesis, not a settled
result).
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

# ── Context layers (the architecture's five-layer model) ─────────────────────

class ContextLayer(StrEnum):
    """The five context layers each agent can draw signals from.

    Ordered most-stable → most-volatile. A signal's layer affects how it
    contributes to triangulation: signals from different layers count as
    independent; signals from the same layer (e.g., two findings from the
    same auditor check) are NOT independent and don't satisfy the
    triangulation requirement on their own.
    """

    # Most stable — canonical domain knowledge (UniFi docs, CIS, NIST, etc.)
    L0_DOMAIN_KNOWLEDGE = "L0_domain_knowledge"
    # Operator's situated profile — slow-changing, time-indexed
    L1_HOUSEHOLD_PROFILE = "L1_household_profile"
    # Per-MAC notes, origin stories, dismissals — slow-changing
    L2_OPERATOR_KNOWLEDGE = "L2_operator_knowledge"
    # Live network snapshot — refreshed each invocation
    L3_LIVE_STATE = "L3_live_state"
    # Rolling telemetry / baselines — temporal trajectory
    L4_TELEMETRY = "L4_telemetry"


# ── Confidence basis — the load-bearing distinction ─────────────────────────

class ConfidenceBasis(StrEnum):
    """How the `confidence` value was derived.

    This is the most important field on the envelope when triangulating
    LLM-emitted artifacts. The research evidence (Kadavath et al. 2022,
    Lin et al. 2022, and 2024-25 RLHF calibration work) shows LLM
    self-reported confidence is poorly calibrated and tends to
    over-confidence after RLHF. So the framework treats different
    confidence-bases very differently:

      - DETERMINISTIC_AGGREGATE: trustworthy. Derived from "fraction of
        checks that passed" or "signal coverage was complete." Used by
        deterministic agents (Auditor, Monitor, Optimizer's verify step).

      - RETRIEVAL_GROUNDED: trustworthy when sources are cited. Confidence
        is a function of citation count, source authority, and recency.
        Used by AI Runtime when retrieving from Layer 0 (domain knowledge).

      - CROSS_AGENT_AGREEMENT: trustworthy. Confidence is the fraction
        of independent agents reaching the same conclusion when asked the
        same question.

      - LLM_SELF_REPORT: low-trust. The LLM said it was confident.
        Allowed but flagged; the framework refuses to escalate severity
        on LLM-self-report alone.

      - UNCERTAIN: explicit "I do not know." Always allowed; downstream
        agents must treat as needing operator input.
    """

    DETERMINISTIC_AGGREGATE = "deterministic_aggregate"
    RETRIEVAL_GROUNDED = "retrieval_grounded"
    CROSS_AGENT_AGREEMENT = "cross_agent_agreement"
    LLM_SELF_REPORT = "llm_self_report"
    UNCERTAIN = "uncertain"


# ── SignalRef — one piece of evidence ────────────────────────────────────────

class SignalRef(BaseModel):
    """A single piece of evidence supporting (or refuting) a claim.

    Triangulation requires evidence to be cited explicitly. The envelope
    framework counts distinct ContextLayers across `supporting_signals`
    to determine whether a high-severity claim is sufficiently supported.
    Two signals from the same layer (e.g., two L3 readings) count as ONE
    independent layer for triangulation purposes.
    """

    layer: ContextLayer
    # Stable identifier for the source — e.g. "auditor.check_wifi_channel_conflicts"
    # or "unifi_docs.security_v4" — so downstream consumers can trace provenance.
    source_id: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    measured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # True when this signal *contradicts* the claim. Agents must surface
    # contradictions, not hide them — mechanism (2) of the cascade design.
    is_refuting: bool = False


# ── HandoffEnvelope — the artifact wrapper ───────────────────────────────────

class HandoffEnvelope(BaseModel):
    """Contract every artifact must satisfy on inter-agent handoff.

    Wraps Finding / NetworkEvent / Recommendation / OptimizerResult /
    UpgradeRecommendation. The wrapped artifact lives in `payload`;
    everything else is the contract that makes the cascade self-healing.

    Structural invariants enforced by validators:

      I1: supporting_signals is non-empty.
      I2: HIGH/CRITICAL severity requires ≥2 distinct ContextLayers in
          supporting_signals (triangulation requirement).
      I3: confidence > 0.85 with empty known_missing_context is rejected
          as suspicious overconfidence — UNLESS confidence_basis is
          DETERMINISTIC_AGGREGATE (where complete coverage is legitimate).
      I4: confidence_basis == LLM_SELF_REPORT cannot drive severity
          escalation alone — at least one signal must come from a
          non-LLM layer (L0/L1/L2/L3/L4 deterministic).
      I5: signals_that_would_invalidate is non-empty for any artifact
          with confidence > 0.5 and severity above LOW.

    These are *structural* — violations are caught at envelope construction
    time, before any downstream agent can act on them.
    """

    # ── Identity & provenance ──────────────────────────────────────────
    envelope_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_agent: str
    destination_agent: str | None = None       # None = broadcast
    artifact_type: Literal[
        "Finding", "NetworkEvent", "Recommendation",
        "OptimizerResult", "UpgradeRecommendation", "ReportInput",
    ]

    # ── Snapshot anchoring (so re-grounding can verify state hasn't drifted) ──
    source_snapshot_id: str | None = None
    previous_snapshot_id: str | None = None    # for diff-based artifacts

    # ── Triangulation (mechanism 1) ────────────────────────────────────
    supporting_signals: list[SignalRef] = Field(min_length=1)
    refuting_signals: list[SignalRef] = Field(default_factory=list)

    # ── Uncertainty declarations (mechanism 2) ─────────────────────────
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_basis: ConfidenceBasis
    known_missing_context: list[str] = Field(default_factory=list)
    signals_that_would_invalidate: list[str] = Field(default_factory=list)

    # ── Approval / permission tier ─────────────────────────────────────
    approval_tier: Literal["AUTO", "REQUIRES_APPROVAL", "NEVER", "INFORMATIONAL"]
    validator_status: Literal[
        "not_required", "pending", "passed", "failed", "human_required",
    ] = "not_required"

    # ── Domain knowledge citations (Layer 0 link) ──────────────────────
    sources_consulted: list[str] = Field(default_factory=list)   # e.g. "unifi_docs#vlan_iot"

    # ── The actual artifact ────────────────────────────────────────────
    payload: dict[str, Any]

    # ── Computed property: independent layer count ─────────────────────
    @property
    def independent_layer_count(self) -> int:
        """How many *distinct* ContextLayers are represented in supporting_signals.

        Used by I2: HIGH/CRITICAL severity requires this to be ≥ 2.
        Two signals from the same layer count as one independent source.
        """
        return len({s.layer for s in self.supporting_signals})

    @property
    def has_non_llm_signal(self) -> bool:
        """True if at least one supporting signal is non-LLM-derived.

        Used by I4: LLM-self-reported confidence cannot escalate severity
        unless there's a deterministic anchor.
        """
        # All ContextLayer values are non-LLM in this schema; LLM contributions
        # are tracked via confidence_basis, not as a layer.
        return len(self.supporting_signals) > 0

    @property
    def severity(self) -> str | None:
        """Best-effort extraction of severity from the payload."""
        if not isinstance(self.payload, dict):
            return None
        sev = self.payload.get("severity")
        return str(sev) if sev else None

    # ── Validators ─────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _i2_triangulation(self) -> Self:
        """I2: HIGH/CRITICAL severity requires ≥2 independent layers."""
        if self.severity in ("CRITICAL", "HIGH"):
            if self.independent_layer_count < 2:
                raise ValueError(
                    f"Triangulation violation (I2): {self.artifact_type} with "
                    f"severity {self.severity!r} requires supporting_signals from "
                    f"≥2 distinct ContextLayers; only "
                    f"{self.independent_layer_count} present "
                    f"({[s.layer for s in self.supporting_signals]!s}). "
                    "Either provide a second independent signal or downgrade severity."
                )
        return self

    @model_validator(mode="after")
    def _i3_overconfidence(self) -> Self:
        """I3: high confidence + no missing-context acknowledgement is suspicious.

        Exempts DETERMINISTIC_AGGREGATE because complete coverage of a
        deterministic check is a legitimate basis for high confidence with
        no missing context (every input was checked, every input was as
        expected).
        """
        if (
            self.confidence > 0.85
            and not self.known_missing_context
            and self.confidence_basis != ConfidenceBasis.DETERMINISTIC_AGGREGATE
        ):
            raise ValueError(
                f"Overconfidence violation (I3): confidence={self.confidence} "
                f"with empty known_missing_context. confidence_basis is "
                f"{self.confidence_basis!r} — only DETERMINISTIC_AGGREGATE "
                f"may claim near-perfect confidence with no missing context. "
                "Either populate known_missing_context, downgrade confidence, "
                "or upgrade confidence_basis."
            )
        return self

    @model_validator(mode="after")
    def _i4_llm_self_report_cannot_escalate(self) -> Self:
        """I4: LLM-self-reported confidence cannot drive severity escalation.

        Severity is capped at MEDIUM whenever `confidence_basis ==
        LLM_SELF_REPORT`, regardless of what supporting signals the
        envelope happens to cite. The semantic argument: if the agent
        had genuinely grounded its confidence in those signals, its
        confidence_basis would be RETRIEVAL_GROUNDED (Layer 0) or
        CROSS_AGENT_AGREEMENT (multi-agent overlap). LLM_SELF_REPORT
        means "the LLM said so" — that is allowed to flow but not allowed
        to escalate.
        """
        if (
            self.confidence_basis == ConfidenceBasis.LLM_SELF_REPORT
            and self.severity in ("CRITICAL", "HIGH")
        ):
            raise ValueError(
                f"LLM-self-report severity cap violation (I4): "
                f"{self.artifact_type} with severity {self.severity!r} but "
                f"confidence_basis is LLM_SELF_REPORT. LLM-only confidence "
                "cannot drive HIGH/CRITICAL. If supporting signals genuinely "
                "anchor the claim, set confidence_basis to RETRIEVAL_GROUNDED "
                "or CROSS_AGENT_AGREEMENT. Otherwise downgrade severity."
            )
        return self

    @model_validator(mode="after")
    def _i5_invalidation_signals(self) -> Self:
        """I5: actionable artifacts must articulate what would prove them wrong."""
        if self.confidence > 0.5 and self.severity in (
            "CRITICAL", "HIGH", "MEDIUM",
        ):
            if not self.signals_that_would_invalidate:
                raise ValueError(
                    f"Falsifiability violation (I5): {self.artifact_type} "
                    f"with confidence={self.confidence} and severity "
                    f"{self.severity!r} must enumerate at least one "
                    "signals_that_would_invalidate. An unfalsifiable claim "
                    "cannot be self-healing."
                )
        return self


# ── Factory helpers (deterministic agents) ───────────────────────────────────

def deterministic_envelope(
    *,
    source_agent: str,
    artifact_type: Literal[
        "Finding", "NetworkEvent", "Recommendation",
        "OptimizerResult", "UpgradeRecommendation", "ReportInput",
    ],
    payload: dict[str, Any],
    supporting_signals: list[SignalRef],
    snapshot_id: str | None,
    coverage_complete: bool,
    known_missing_context: list[str] | None = None,
    invalidating_signals: list[str] | None = None,
    approval_tier: Literal[
        "AUTO", "REQUIRES_APPROVAL", "NEVER", "INFORMATIONAL",
    ] = "INFORMATIONAL",
) -> HandoffEnvelope:
    """Construct an envelope from a deterministic agent.

    Confidence is derived from coverage_complete + signal count, NOT
    self-reported. This is the high-trust path.
    """
    n = len({s.layer for s in supporting_signals})
    if coverage_complete and n >= 2:
        confidence = 0.95
        basis = ConfidenceBasis.DETERMINISTIC_AGGREGATE
    elif coverage_complete:
        confidence = 0.75
        basis = ConfidenceBasis.DETERMINISTIC_AGGREGATE
    else:
        confidence = 0.5
        basis = ConfidenceBasis.UNCERTAIN

    return HandoffEnvelope(
        envelope_id=f"{source_agent}:{artifact_type}:{datetime.now(UTC).isoformat()}",
        source_agent=source_agent,
        artifact_type=artifact_type,
        source_snapshot_id=snapshot_id,
        supporting_signals=supporting_signals,
        confidence=confidence,
        confidence_basis=basis,
        known_missing_context=list(known_missing_context or []),
        signals_that_would_invalidate=list(invalidating_signals or []),
        approval_tier=approval_tier,
        payload=payload,
    )


def llm_envelope(
    *,
    source_agent: str,
    artifact_type: Literal[
        "Finding", "NetworkEvent", "Recommendation",
        "OptimizerResult", "UpgradeRecommendation", "ReportInput",
    ],
    payload: dict[str, Any],
    supporting_signals: list[SignalRef],
    snapshot_id: str | None,
    citations: list[str],
    known_missing_context: list[str],
    invalidating_signals: list[str],
    approval_tier: Literal[
        "AUTO", "REQUIRES_APPROVAL", "NEVER", "INFORMATIONAL",
    ] = "INFORMATIONAL",
) -> HandoffEnvelope:
    """Construct an envelope from an LLM-emitted artifact.

    Confidence is RETRIEVAL_GROUNDED if citations exist (Layer 0 sources),
    otherwise capped at LLM_SELF_REPORT. Forces the operator-knowledge
    distinction at envelope-construction time so downstream agents see
    immediately whether the claim is grounded or just generated.
    """
    if citations:
        # Retrieval-grounded: confidence scales with citation count up to 0.85
        confidence = min(0.85, 0.5 + 0.1 * len(citations))
        basis = ConfidenceBasis.RETRIEVAL_GROUNDED
    else:
        # No citations — pure LLM-self-report; capped at 0.7 by I4 in practice
        confidence = 0.6
        basis = ConfidenceBasis.LLM_SELF_REPORT

    return HandoffEnvelope(
        envelope_id=f"{source_agent}:{artifact_type}:{datetime.now(UTC).isoformat()}",
        source_agent=source_agent,
        artifact_type=artifact_type,
        source_snapshot_id=snapshot_id,
        supporting_signals=supporting_signals,
        confidence=confidence,
        confidence_basis=basis,
        sources_consulted=citations,
        known_missing_context=known_missing_context,
        signals_that_would_invalidate=invalidating_signals,
        approval_tier=approval_tier,
        payload=payload,
    )
