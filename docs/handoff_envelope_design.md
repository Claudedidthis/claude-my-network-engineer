# HandoffEnvelope Design — Rationale, Evidence, and Honest Caveats

> _This is an architectural hypothesis with mixed evidence support. We are
> running an experiment: that explicit structural negative-feedback contracts
> on inter-agent handoffs reduce cascade error amplification. Some of the
> mechanisms are well-evidenced; others are reasonable but unproven; one
> (LLM-self-reported confidence) is contested by recent research and we have
> deliberately constrained how it's allowed to flow. Readers are invited to
> treat this as a documented experiment, not settled architecture._

> _Last updated: 2026-04-25, paired with `src/network_engineer/tools/envelope.py`._

---

## Why this document exists

The Waiter-and-the-App paper argues that pure agent-to-agent cascades are
positive-feedback systems: each agent treats the previous agent's output as
ground truth and amplifies any errors that came before. Multi-agent
orchestration frameworks (LangGraph, ADK, Microsoft Agent Framework) all
converged on roughly the same set of contracts to break that loop. This
project is adopting a subset of those contracts, with two specific structural
mechanisms baked into the envelope schema:

1. **Triangulation requirement** — every claim must cite ≥2 independent
   context layers to be eligible for HIGH/CRITICAL severity.
2. **Uncertainty declarations** — every artifact must populate confidence,
   confidence basis, known missing context, and falsifiability conditions;
   overconfidence is rejected at envelope-construction time.

This document explains, per design choice, what evidence supports it, what
evidence questions it, and what we are betting on by adopting it.

---

## Mechanism 1: Triangulation requirement

### What it does

`HandoffEnvelope.supporting_signals` is a non-empty list of `SignalRef`
objects, each tagged with the `ContextLayer` it came from. The envelope
validator I2 rejects any HIGH/CRITICAL artifact whose supporting signals
collectively span fewer than 2 distinct layers.

A signal from L3 (live state) plus a signal from L0 (domain knowledge)
counts as 2. Two signals from L3 (two readings of the same snapshot)
count as 1.

### Why this exists

A single-source claim is a hypothesis. Two converging sources, drawn from
independent contexts, become evidence. This is the basic structure of
defense-in-depth in security engineering, and of ensemble methods in
machine learning — both of which have decades of robust empirical support.

The specific failure mode we are trying to prevent is the **classify_client
resonance loop** (OWASP "Cascading Hallucination Attack"): a single
heuristic produces a classification, downstream agents ratify it, the
operator's UI fixes propagate the same heuristic, and the wrong
classification compounds confidence over iterations. Triangulation makes
this structurally impossible: the heuristic alone cannot escalate severity,
so a wrong classification cannot drive an aggressive recommendation
without a corroborating signal.

### What evidence supports this

- **Defense-in-depth** is a foundational principle in security engineering
  (NIST SP 800-53 SI-13 — system component diversity; CIS Controls v8 in
  multiple sections). Independent overlapping controls with different
  failure modes empirically reduce single-point failures.

- **Ensemble methods in ML** (Dietterich 2000, "Ensemble Methods in
  Machine Learning"; later work on bagging, boosting, stacking) consistently
  show that combining classifiers with independent error patterns reduces
  total error rate. The condition "independent error patterns" is what we
  are operationalizing as "different ContextLayers."

- **Recent multi-agent work** — Du et al. 2023 ("Improving Factuality and
  Reasoning in Language Models through Multi-Agent Debate") and Liang et al.
  2023 ("Encouraging Divergent Thinking in Large Language Models through
  Multi-Agent Debate") both found that multi-signal verification reduced
  hallucination rates in tasks where single-agent output would have been
  confident-but-wrong.

### What evidence questions this

The **independence** assumption is fragile. Two signals from L3 and L0
sound independent, but if the L0 entry was originally written by reading
an L3 reading, they're not actually independent — same root data, two
hops. Genuine independence is hard to establish; we approximate it by
"different layers" which is a heuristic, not a guarantee.

There is also a real cost: some legitimate single-source findings (a
firmware version is EOL per the vendor, full stop) cannot be triangulated
because there's only one source for that claim. We accept this cost by
allowing single-source findings at LOW severity — a vendor EOL fact is
informational, not actionable on its own.

### What we are betting on

That the cost of capping single-source findings at LOW is smaller than the
benefit of preventing single-source resonance loops from escalating to
HIGH/CRITICAL. This is testable: track the rate of single-source findings
that *should* have been HIGH but couldn't be. If that's a meaningful
fraction, the threshold is wrong.

### How we'll know if we got it wrong

If, after some months of operation, we find that:
- the operator is regularly overriding the LOW-severity floor on
  single-source findings, AND
- those overrides correlate with genuinely actionable issues that
  triangulation missed,

then the triangulation cost outweighs the benefit, and we should adjust
either the layer-independence definition or the severity floor.

---

## Mechanism 2: Uncertainty declarations — and the LLM-confidence problem

### What it does

Every envelope must populate four uncertainty-related fields:

- `confidence: float` (0–1)
- `confidence_basis: ConfidenceBasis` — how the confidence was derived
- `known_missing_context: list[str]` — what the agent didn't check
- `signals_that_would_invalidate: list[str]` — falsifiability conditions

Validators I3 and I5 enforce that these fields cannot be silently empty
when the artifact carries actionable severity. I4 caps the severity an
LLM-self-report alone can drive.

### Why this exists

The cascade fails when each agent in the chain confidently restates the
previous agent's claim as fact, dropping the uncertainty that should have
flowed with it. Forcing every artifact to articulate "what I don't know"
and "what would prove me wrong" prevents confidence laundering — the
process by which a tentative observation becomes a confident assertion
through mere propagation.

### What evidence supports this

- **Falsifiability** as a quality criterion is a foundational scientific
  norm; an unfalsifiable claim has poor information content. Forcing
  agents to articulate `signals_that_would_invalidate` is operationalising
  this in software.

- **Calibration research** in machine learning (Guo et al. 2017, "On
  Calibration of Modern Neural Networks") established that classifier
  confidences should track empirical accuracy. Calibrated confidence
  estimates measurably improve downstream decision-making in cost-
  sensitive applications.

- **Tool-use grounding** (ReAct — Yao et al. 2023; Toolformer — Schick et
  al. 2023) showed that requiring agents to ground claims in retrievable
  tool outputs measurably reduces hallucination. Citing
  `sources_consulted` is the same principle.

### Where the evidence pushes back — the LLM self-report problem

> **This is the part where I'm pushing back on my own initial framing.**

Several lines of evidence suggest LLM-self-reported confidence is poorly
calibrated and tends toward overconfidence:

- **Kadavath et al. 2022 (Anthropic), "Language Models (Mostly) Know What
  They Know"** — found that LLMs do have *some* internal signal correlated
  with correctness, but the calibration is noisy and varies dramatically
  across domains. The honest summary: "better than chance, far from
  reliable."

- **Lin et al. 2022, "Teaching Models to Express Their Uncertainty in
  Words"** — verbalised uncertainty can be improved with training but
  expected calibration error remained substantial. Stated confidence
  correlates with empirical accuracy weakly in many task classes.

- **Post-RLHF calibration literature (2024–2025)** has documented that
  human-feedback-tuned models exhibit *increased* overconfidence: the
  training process rewards confident-sounding answers, which models learn
  to produce regardless of correctness. This means the most capable
  current production models are *more*, not less, susceptible to this
  failure mode than earlier models.

- **OWASP "Top 10 for LLM Applications"** (2024 release) explicitly
  identifies "overreliance on LLM output" and "improper output handling"
  as top categories — both rooted in the gap between stated and actual
  reliability.

The implication is clear: **we cannot trust an LLM's self-reported
confidence at face value.** The schema must distinguish trustworthy
confidence sources from untrustworthy ones, and treat them differently.

### How we resolve this — the `confidence_basis` field

Rather than ban LLM-emitted confidence (which would gut the AI Runtime),
we tag it. `ConfidenceBasis` is a five-valued enum:

| Basis | Trust level | Used by |
|-------|------------|---------|
| `DETERMINISTIC_AGGREGATE` | High | Auditor, Monitor, Optimizer verify steps |
| `RETRIEVAL_GROUNDED` | Medium-high | AI Runtime when Layer 0 sources are cited |
| `CROSS_AGENT_AGREEMENT` | Medium-high | Outputs that multiple independent agents agree on |
| `LLM_SELF_REPORT` | Low | AI Runtime narrative without citations |
| `UNCERTAIN` | Honest unknown | Any agent declaring it doesn't know |

Validator I4 makes the consequence explicit: an envelope with
`confidence_basis = LLM_SELF_REPORT` cannot escalate to HIGH/CRITICAL
severity unless at least one *non-LLM* supporting signal agrees with it.
This means the LLM can speak, but it cannot raise alarms alone — there
must be a deterministic anchor.

### What evidence supports this specific design

The `RETRIEVAL_GROUNDED` tier is well-supported: tool-grounded reasoning
reduces hallucination measurably (Yao 2023, Schick 2023, the broader RAG
literature). Citing concrete sources at the envelope level operationalises
that.

The `CROSS_AGENT_AGREEMENT` tier is supported by the multi-agent debate
work cited above and by classic ensemble theory.

The `LLM_SELF_REPORT` cap on severity is the most defensive choice —
it's an architectural concession that the calibration problem has not
been solved, and rather than wait for it to be solved we are gating the
blast radius until it is.

### What we are betting on

That the calibration gap on LLM self-report is real and persistent, AND
that retrieval-grounded confidence (with proper Layer 0 citations) is a
materially better signal than self-report. Both are reasonably evidenced
but not settled.

### How we'll know if we got it wrong

If, after operating with these constraints, we find that:
- LLM-emitted artifacts with valid citations still produce wrong
  recommendations at a rate similar to LLM-self-report artifacts,

then the `RETRIEVAL_GROUNDED` tier is over-trusted and we should treat all
LLM output more strictly — perhaps requiring CROSS_AGENT_AGREEMENT before
trusting any LLM-derived severity.

Conversely, if deterministic agents are systematically more cautious than
they should be (refusing to escalate when they're actually correct), the
DETERMINISTIC_AGGREGATE tier may need to permit LOWER confidence floors
in some cases.

---

## Validators I1–I5 — the structural invariants

### I1: supporting_signals is non-empty

**Why:** A claim with no evidence is not a claim. Forcing a signal list
prevents the "agent emits a finding from nothing" failure mode.

**Cost:** Trivial — every agent already has *some* basis for its findings;
this just makes the basis explicit.

### I2: HIGH/CRITICAL severity requires ≥2 distinct layers

**Why:** Triangulation. Documented above.

**Cost:** Some legitimately HIGH single-source findings get capped at LOW.
Acceptable if the cap is rare; problematic if it's common. Worth measuring.

### I3: Overconfidence rejection

**Why:** An agent that says "confidence 0.95, no missing context" is
either claiming omniscience or being intellectually lazy. Forcing the
agent to acknowledge what it didn't check is a structural antidote to
the confidence-laundering pattern.

**Important exemption:** `DETERMINISTIC_AGGREGATE` agents *can* legitimately
have high confidence and empty missing-context — if the deterministic
check covered every input and every input was as expected, there is no
missing context. The validator allows this.

**Cost:** LLM agents have to self-articulate uncertainty, which they're
imperfect at. Mitigated by I4.

### I4: LLM-self-report cannot escalate severity alone

**Why:** Documented at length above. The single most important constraint
in the schema.

**Cost:** AI Runtime cannot drive HIGH/CRITICAL findings on its own,
even when it might be right. Acceptable: the AI Runtime's role is
narrative and review, not autonomous escalation.

### I5: Falsifiability requirement

**Why:** An unfalsifiable claim cannot self-heal because no future
observation can refute it. Requiring `signals_that_would_invalidate`
ensures the claim has a built-in revocation path — paired with the
TTL/auto-revocation mechanism (cascade self-healing #4), this means
stale claims expire automatically.

**Cost:** Agents have to think about what would prove them wrong.
This is genuinely useful work; the cost is that it forces agents to do
it.

---

## What this is NOT

- It is **not** a theorem. The schema cannot guarantee correctness; it can
  only make certain classes of error structurally harder.
- It is **not** sufficient on its own. The other cascade self-healing
  mechanisms (re-grounding, TTL, invariants, bidirectional flow) are
  needed too. The Waiter paper §4.4's analogy is right: many overlapping
  feedback loops, none perfect alone.
- It is **not** evidence-free. Nor is it settled science. We are running
  an experiment — adopting reasonable architecture from the multi-agent
  framework lineage and the ML calibration literature, with the specific
  pieces called out above as more or less well-evidenced.

---

## How this wires into the build plan

This schema is a **prerequisite** for several existing P0 tasks rather than
a standalone task:

- Task #21 (orchestration graph + typed handoff envelopes) — this schema
  *is* the typed handoff envelope. Building task #21 means wiring every
  agent emit/consume site to use it.
- Task #22 (cascade validators) — validators I1–I5 are the structural
  validators; runtime LLM validators (`AIRuntime.review_config_change`)
  layer on top.
- Task #23 (validator tiers) — `confidence_basis` is the schema-level
  expression of where in the validator tier the artifact sits. Validator
  tiers (Tier 0 schema → Tier 6 human) are the runtime enforcement of
  what `confidence_basis` represents structurally.
- Task #24 (classify_client resonance fix) — the regression tests for
  this fix should produce envelopes with `confidence_basis =
  CROSS_AGENT_AGREEMENT` when the heuristic and second signal agree, and
  envelopes with severity=UNCERTAIN when they disagree.
- Task #35 (Layer 0 domain knowledge) — `sources_consulted` and the
  `RETRIEVAL_GROUNDED` confidence basis are the schema-level link to
  Layer 0. Building task #35 means populating these fields whenever an
  agent retrieves from domain knowledge.

In other words: ship the envelope first; the rest of the P0 set is what
populates and enforces it.

---

## Final caveat — this is an experiment

The user explicitly framed this project as a passion project that should
also serve as a proxy for what enterprise-grade agent architecture *could*
look like. The schema in `envelope.py` is our best current hypothesis
about that. It is informed by:

- the Waiter-and-the-App architectural thesis,
- the orchestration patterns from LangGraph / ADK / Microsoft Agent
  Framework / OpenAI Agents SDK,
- the calibration and tool-grounding literature in ML,
- the OWASP threat model for agentic systems,
- the foundational defense-in-depth and ensemble-method literature.

It is *not* validated by this project actually running for months under
production load. The honest framing for any reader is: this is a
documented experiment with an explicit theory of why it should work, with
specific pieces called out as well- or weakly-evidenced, and with
falsification criteria documented. If it works, we'll have evidence for
this kind of contract-based cascade architecture in personal-scale
agentic systems. If it doesn't, the documented falsification criteria
will tell us where the theory is wrong.

That second outcome is also useful. A well-instrumented failed experiment
is more valuable than a successful one with no instrumentation.
