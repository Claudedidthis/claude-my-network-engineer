# ClaudeMyNetworkEngineer — Architecture & Audit Map

> _A reviewable map of how this system gathers context, what contracts the
> agents satisfy when they hand off to each other, what makes the cascade
> structurally self-healing rather than self-amplifying, and how the whole
> thing is orchestrated. Read for an audit pass, or jump to a specific
> subsystem. Last updated: 2026-04-25._

---

## Preface — what this document is, and is not

This is an architectural thesis, partially implemented, deliberately
positioned as a documented experiment. It is **not** a description of a
solved system. Three things to internalise before reading further:

**Stochastic systems are stochastic.** Every agentic system that uses an
LLM at any layer carries irreducible uncertainty. We can constrain its
shape, narrow its blast radius, make its failures visible, and force the
high-confidence claims to be backed by evidence — but we cannot make it
deterministic. People building or reviewing systems like this routinely
forget that fact and then are surprised when the system behaves
unexpectedly. The right framing is: this is a system designed to *fail
gracefully* and *fail visibly*, not a system that won't fail.

**This is open source for UniFi enthusiasts of all skill levels.** The
target user ranges from someone who just bought a UniFi gateway and has no
idea where to start, to someone with a heritage network they built over
years and now want to bring under structured management. Nothing in the
agent code is operator-specific to the project author. Every project-
specific signal is operator-supplied (via `nye onboard`, the registries,
or the dismissals YAML) or discovery-driven from live state — never
hardcoded.

**Architectural choices are documented as bets, not facts.** Every
significant design choice in this document carries: (a) what evidence
supports it, (b) what evidence questions it, (c) what we are betting on
by adopting it, (d) how we'll know if we got it wrong. This format is
deliberate — it reflects that we are early in the empirical study of
multi-agent reliability and that we are reasoning under uncertainty.
The cited research (Waiter-and-the-App architectural thesis; LangGraph,
ADK, Microsoft Agent Framework patterns; Kadavath et al. 2022 on LLM
calibration; Lin et al. 2022 on uncertainty verbalisation; OWASP Top 10
for LLM Applications; Yao et al. 2023 ReAct; broader RAG literature) is
directionally informative but not collectively conclusive. Knowledge
cutoff for cited work is August 2025; later work may shift these
recommendations.

How to read this document:

- **First-time auditor**: read Preface → Part 1 → Part 2 → Part 5 →
  Part 11. ~30 minutes for the conceptual frame.
- **Reviewing for a specific subsystem**: jump straight to that part.
- **Verifying claims against the implementation**: Part 7 (audit
  checklist) is grep-able against the repo.

---

## Part 1 — Architectural premise

This system applies the Waiter-and-the-App thesis to home networks: the
gap between a generic recommendation engine and one that actually fits
*this* operator's network is not a model-capability gap, it's a **signal
architecture gap**. Agents do not get smarter by using a bigger model; they
get smarter by being given richer situated context at decision time, and
by being constrained from acting on stochastic claims that aren't backed
by deterministic anchors.

**Five operational consequences:**

1. **Every project-specific fact is operator-supplied or discovery-driven.**
   Nothing operator-specific is hardcoded in agent code. The previous
   hardcoded allowlists that previously lived in agent code (an offline-device set, an open-SSID allowlist)
   constants were tech debt; they have been migrated to
   `config/dismissals.yaml`. New operator facts go through the dismissal
   registry, the device/client registers, or the origin stories — never
   the agent code path.

2. **Onboarding is a probe-driven conversation, not a form.** Operators do
   not know what context to volunteer until asked. The probe library
   (`config/probes/<theme>.yaml`) is the surface for both initial capture
   and community contribution.

3. **The household profile is a time series, not a fact.** Operator
   context drifts as life changes — kids age out of parental controls,
   work patterns shift, roommates leave, dogs get bitter. The system must
   support periodic reassessment with append-only history; recommendations
   must record which profile snapshot informed them.

4. **Recommendations must be backed by authoritative sources, not
   training weights.** A system that tells you to configure your VLAN a
   certain way must be able to point at *why* — UniFi documentation, CIS
   benchmarks, NIST publications — not "the AI thinks." This is the
   Layer 0 domain knowledge requirement (Part 2).

5. **The cascade is structurally damped.** A pure agent-to-agent cascade
   without human checkpoints is a positive-feedback amplifier (Waiter
   paper §4.3). The architecture inserts structural negative-feedback
   mechanisms at every emit→consume boundary, so that when the human is
   no longer in between (Phase 10's autonomous scheduler), errors do not
   compound silently. Mechanisms detailed in Part 5.

The **single hard safety property** that everything else supports:
*stochastic claims (LLM-derived content) must never autonomously drive
high-blast-radius write actions without a deterministic anchor*. This is
a structural invariant, not a runtime preference. It is enforced at the
HandoffEnvelope schema layer (Part 4), at the permission model gate, and
at the Phase 10 approval boundary.

---

## Part 2 — Context-gathering strategy: the five layers

The system gathers context in **five layers**, ordered by stability — most
stable first. Each agent's recommendation quality is determined by which
layers it consults; auditing a finding means tracing which layers
informed it.

| # | Layer | Stability | Where it lives | How captured |
|--:|-------|-----------|----------------|--------------|
| **0** | **Domain Knowledge** | Slow-changing (months) | `config/domain_knowledge.yaml` (Phase 1) → indexed retrieval (Phase 2) | Curated + scraped from UniFi docs, community forum, release notes, CIS, NIST |
| **1** | **Household Profile** | Slow-changing (years) | `config/household_profile.yaml` + `config/profile_history.yaml` | Probe-driven `nye onboard` + periodic `nye reassess` |
| **2** | **Operator Knowledge** | Per-network slow-changing | 4 YAML files (device + client registers, origin stories, dismissals) | Heritage walkthrough + `nye registry walkthrough` + ad-hoc edits |
| **3** | **Live Network State** | Pulled fresh per invocation | UniFi API + `snapshots/*.json` for diffs | `tools/unifi_client.py`, snapshot before every write |
| **4** | **Agent Telemetry** | Append-only history | `logs/*.log`, rolled up by `tools/baseline.py` | All agents emit; baselines computed on demand |

The key architectural insight: **agents must be allowed (and required) to
draw signals from multiple layers simultaneously, AND triangulation
discipline requires that significant claims be supported by signals from
*different* layers.** Two signals from L3 alone are not independent — they
are two readings of the same data. A signal from L3 plus a signal from L0
*are* independent — they are different sources with different failure
modes.

### Layer 0 — Domain Knowledge (the new layer)

**What it contains:** canonical, authoritative networking and security
guidance — UniFi product documentation, community forum top-answered
threads, release notes, CIS network device benchmarks, NIST 800-series
publications (particularly 800-153 for wireless security), SANS
publications on home/SMB segmentation.

**Why it exists:** without it, every recommendation the system emits is
only as good as Claude's training data on UniFi configuration — which
has a knowledge cutoff (August 2025 in the current model), was never
authoritative on firmware-specific behaviour, and cannot incorporate
community-discovered patterns from after the cutoff. This is a
confidence-laundering pattern: the system speaks with apparent AI
authority but the basis is hallucinated rather than retrieved.

**Capture strategy:**

- **Phase 1 (minimum viable, ships before Phase 10):** a curated
  `config/domain_knowledge.yaml` keyed by `(finding_code, hardware_class,
  firmware_band)`. Each entry: `source_citation`, `excerpt`, `applies_to`,
  `last_verified_date`. Community-contributable. Probably 50-100 entries
  to start, focused on the highest-frequency findings.

- **Phase 2 (post-launch):** scraped + indexed view of the canonical
  sources, refreshed on a schedule. RAG over UniFi docs + community top
  threads + release notes. Vector embeddings for semantic retrieval keyed
  by hardware model + firmware band.

**Retrieval mechanism, not static context block.** Most domain knowledge
is irrelevant to any given finding; stuffing the entire UniFi docs into
every prompt is wrong both architecturally and on token cost. The AI
Runtime calls a `retrieve_domain_knowledge(finding_code, hardware_model,
firmware_version, profile_constraints)` tool that returns top-k chunks.
The retrieved chunks are injected into the recommendation generation call
*and* the citations are recorded on the resulting HandoffEnvelope as
`sources_consulted` (Part 4).

**Source authority ranking:** vendor release notes > pinned community
threads > community top-answered > vendor docs > general training data.
Per the operator's domain expertise, UniFi's own documentation is not
always current with firmware behaviour — community knowledge often is.
The retrieval layer must reflect that reality.

### Layer 1 — Household Profile (the time-indexed foundation)

**Schema:** `tools/schemas.py` → `HouseholdProfile`, composed of 11 themed
submodels:

| Submodel | Captures | Drives downstream decisions |
|----------|----------|------------------------------|
| `BuildingProfile` | Construction era, materials, floors, basement/attic, cable feasibility | RF planning, cable feasibility |
| `ISPProfile` | Type, speeds, IPv6, modem situation, failover, data caps | WAN priority, failover design |
| `HouseholdComposition` | Residents, ages, tech literacy, accessibility, kids' workaround savvy | Parental controls, criticality classification, recovery design |
| `WorkProfile` | WFH cadence, work types, corporate VPN, conferencing criticality | WAN priority during business hours, VPN handling |
| `DeviceEcosystem` | Device count, platforms, smart-home, gaming, **third-party-installer IoT** | VLAN architecture, capacity planning |
| `SecurityPhilosophy` | IoT isolation appetite, third-party devices, VPN usage, DNS filtering | VLAN aggressiveness, DNS filter recommendations |
| `UsagePatterns` | Peak windows, simultaneous streams, automation depth, Zigbee count | Bandwidth planning, RF environment |
| `InfrastructureProfile` | UPS, PoE budget, willingness to run power, rack space | Hardware recommendations, AP placement |
| `PreferencesProfile` | Cloud-vs-local, privacy orientation, DIY comfort, maintenance tolerance | Recommendation aggressiveness, monitoring cadence |
| `FutureStateProfile` | Renovations, moving plans, planned devices | Investment horizon |
| `OriginStoryProfile` | Trigger event, previous setup, biggest frustration | Framing — sets aggressiveness for everything else |

**The temporal correctness problem.** A profile captured three years ago
that says `kids: school_age` is silently producing wrong recommendations
once the kids leave for college. The system *cannot* notice this
drift through any other layer (the kids' devices may still appear in the
client list as inherited names; usage patterns shift gradually). The
operator must trigger reassessment, and the profile must record what was
true *when*.

**Implementation pattern (in flight, P0 for Phase 10):**

- `config/profile_history.yaml` — append-only log of every field
  mutation: `(timestamp, field_path, old_value, new_value, source)`. Never
  rewrite history.
- `nye reassess` — does NOT overwrite the current profile. Walks the
  operator through life-event-tagged probes, shows the previous answer,
  asks "is this still true?" Each confirmed answer extends validity;
  each changed answer creates a new history entry and triggers downstream
  re-evaluation.
- Probes get a `life_event_relevant: true` flag in YAML when their
  answer is the kind that changes with life events (kids' ages,
  work-from-home, accessibility needs, third-party device additions).
- Reassessment triggers: scheduled annual prompt, operator-pulled,
  drift-detected (telemetry shows usage pattern radically different from
  profile assumptions). Surfaced in the next report.
- Every Recommendation records `source_profile_version` in its
  HandoffEnvelope. Recommendations whose source profile is older than a
  configurable threshold are re-run before being acted on.

**Capture mechanism:** `agents/onboarding_agent.py` runs a probe-driven
conversation. The conversational engine in `tools/probes.py` picks the
next-most-informative probe each turn:

```
score = probe.priority                          # 1-10 baseline
        + 3  if probe.id in boost_ids           # follow-up of recent answer
        + 2  if probe.theme has 0 fields filled # cold-theme bonus
        - filtered out if triggers_satisfied is False
        - filtered out if target field already populated
```

The probe library lives in `config/probes/<theme>.yaml` — 11 files, ~94
probes, designed for community PRs. Each probe specifies prompt text,
interpretation kind, target profile field path, optional triggers,
priority, and conditional follow-ups.

### Layer 2 — Operator Knowledge

Four flat YAML files, all operator-supplied, all gitignored, all with
checked-in `examples/*.example.yaml` templates:

| File | Content | Loader | Phase 11 Supabase mirror |
|------|---------|--------|---------|
| `config/device_register.yaml` | Per-MAC notes on UniFi-managed devices: location, role, criticality, deployment rationale | `tools/registry.py` | `device_registry` table |
| `config/client_register.yaml` | Per-MAC notes on stations: tier override, owner, location, criticality | `tools/registry.py` | `client_registry` table |
| `config/origin_stories.yaml` | Per-artifact "why does this exist?" notes for networks, port forwards, firewall rules | `tools/origin_stories.py` | `origin_stories` table |
| `config/dismissals.yaml` | Per-(finding_code, evidence_field, evidence_value) suppressions with operator reason | `tools/dismissals.py` | `dismissals` table |

OUI lookup + identification tips (`config/oui_common.yaml`,
`config/identification_tips.yaml`) make the registry walkthrough
educational rather than a guess-and-check exercise.

### Layer 3 — Live Network State

Pulled fresh from the UniFi API on every invocation; no local cache except
diff snapshots. `tools/unifi_client.py` wraps three UniFi API surfaces (v1
Integration, classic, Protect). Read methods are cheap; write methods
(`set_ap_channel`, `set_device_name`, `delete_port_forward`, etc.) are
gated by the permission model. `client.snapshot()` writes a config-focused
JSON to `snapshots/<ts>_snapshot.json` before any write operation.

### Layer 4 — Agent Telemetry / Baselines

Append-only history that the system reads to compute *trajectory* signals.
`tools/logging_setup.py` configures four log files with consistent
JSON-line shape. `tools/baseline.py` rolls forward the action log into
per-(device, band, metric) rolling baselines (default 7-day window). The
monitor consults these so "8 packet drops" becomes "anomalous vs baseline
of 1.2" instead of triggering a static threshold.

---

## Part 3 — How each agent consumes context

Every agent's recommendation quality is determined by which context layers
it reads. Below: each agent's read profile, what it emits, and (for
partial wiring) what's still TODO.

### Auditor (`agents/auditor.py`) — read-only, deterministic

**Reads:** L3 snapshot (devices/clients/networks/wifi/firewall/port-forwards/settings)
+ L2 dismissals (downgrade/suppress findings the operator marked
intentional) + L2 origin stories (downgrade port-forward findings when
operator rationale exists; suppress entirely when `do_not_touch=true`).

**Planned wiring (Phase 10 blocker):** L1 household profile to nudge
severity bands — security-focused operators get +1 on security findings,
reliability-focused get +1 on uptime findings; L0 domain knowledge to
cite *why* a finding is severe (link to the relevant CIS control or
UniFi docs section).

**Emits:** `Finding` objects wrapped in `HandoffEnvelope` with
`confidence_basis = DETERMINISTIC_AGGREGATE`. Sorted by severity.

### Monitor (`agents/monitor.py`) — read-only, deterministic

**Reads:** L3 snapshot health/device-stats/client-stats.

**Planned wiring (Phase 10 blocker):** L4 baseline (so threshold
classification becomes anomaly classification); L1 household profile
(escalate WAN drops to HIGH during business hours when
`work.conferencing_quality_critical == 'mission_critical'`).

**Emits:** `NetworkEvent` objects wrapped in `HandoffEnvelope` with
`confidence_basis = DETERMINISTIC_AGGREGATE`.

### Optimizer (`agents/optimizer.py`) — write path with full safety pipeline

**Reads:** L3 snapshot (before + after every write — the rollback anchor)
+ permission model (`config/permission_model.yaml`).

**The §9 pipeline (from `apply_change()`):**

```
1. permissions.check(action) → must be Tier.AUTO
2. client.snapshot()         → snapshot_before
3. _do_apply(client, action, params)   ← the one write
4. time.sleep(verify_wait_s)            ← let UDM propagate
5. _do_verify(client, action, params)   ← read back, confirm
6. on verify failure: _do_rollback + log + return 'rolled_back'
7. on success: client.snapshot() again, log_action, return 'applied'
```

This is the Waiter paper §4.3 negative-feedback configuration applied to
a single action. Verification breaks the positive-feedback loop
structurally. **REQUIRES_APPROVAL actions are gated by an explicit
`ApprovedRecommendation` artifact** (Part 6 — interrupt/resume pattern,
Phase 10 design).

**Emits:** `OptimizerResult` with status `applied | rolled_back | failed`.

### Security Agent (`agents/security_agent.py`) — recommendations only

**Reads:** L3 networks/wifi/clients/firewall + L2 client register
(`tier_override` wins over heuristic) + AI Runtime
(`AIRuntime.review_config_change`, auto-escalates to Opus when the change
touches firewall/VLAN/cameras).

**Planned wiring (Phase 10 blocker):** L2 origin stories (preserve any
network with a `do_not_touch` story rather than collapsing into the
4-tier proposal — the canonical solar-DMZ case); L1 household profile
(`security.iot_isolation_appetite` to set VLAN aggressiveness;
`household.kids_savvy_with_workarounds` to choose content-filter
strategy); L0 domain knowledge (cite the actual recommendation source
for every VLAN proposal element).

**Emits:** `Recommendation` objects with full audit envelope, written to
`recommendations.log`.

### Upgrade Agent (`agents/upgrade_agent.py`) — recommendations only

**Reads:** L3 device list + `config/upgrade_catalog.yaml` + AI Runtime
(`AIRuntime.score_upgrade_recommendation`, Haiku) for narrative.

**Planned wiring:** L2 device register's criticality field (a HIGH-urgency
EOL on a critical AP is more pressing than the same EOL on a backup unit);
L0 domain knowledge (Phase 2 replaces the manual catalog with an indexed
view of UniFi release notes — Part 9).

### Reporter (`agents/reporter.py`) — narrative

**Reads:** L3 current snapshot + L4 action log.

**Planned wiring (Phase 10 blocker):** L1 household profile so narratives
reference operator context naturally ("This week your work-from-home WAN
was stable; the conferencing-critical link saw 0 drops during business
hours").

### AI Runtime (`agents/ai_runtime.py`) — model orchestration

Single wrapper for all Anthropic API calls. Per-job model routing in
`config/ai_runtime_config.yaml`:

| Job | Model | Cadence |
|-----|-------|---------|
| `analyze_security_posture` | Opus 4.7 | daily |
| `generate_monthly_report` | Opus 4.7 | monthly |
| `review_config_change` | Sonnet 4.6 (escalates to Opus on firewall/VLAN/camera/admin) | per-change |
| `explain_anomaly` | Sonnet 4.6 | per-event |
| `natural_language_query` | Sonnet 4.6 (Haiku for simple) | on-demand |
| `score_upgrade_recommendation` | Haiku 4.5 | weekly |

**Context architecture per call:**
- System prompt — cached (1.25× input rate, 90% discount on cache hit)
- Network context block — cached (full snapshot, trimmed via
  `_security_context()`)
- Optional `previous_snapshot` diff — separate cached block, the
  trajectory signal
- Optional `household_profile` — cached, the situated-context signal
- L0 domain knowledge retrieval results — cached per query
- User question — uncached (changes per call)

**Fallback path:** when `AI_RUNTIME_ENABLED=false`, every method returns
a deterministic placeholder rather than raising. Core agents must function
without an Anthropic key. This is non-negotiable for the open-source
distribution — most users may not configure an Anthropic key on first
install.

### Onboarding Agent (`agents/onboarding_agent.py`) — meta-agent

Probe-driven conversation that fills L1. Heritage mode extends with the
existing-config walkthrough that captures L2 origin stories.

### Registry Agent (`agents/registry_agent.py`) — populates L2

Two modes: `bootstrap(client)` (non-interactive seed from live state with
auto-classification) and `walkthrough(client)` (interactive per-MAC
annotation with OUI-driven identification tips for unknown devices).

---

## Part 4 — Inter-agent contract: HandoffEnvelope

Every artifact passing between agents is wrapped in `HandoffEnvelope`
(`tools/envelope.py`). The envelope is the **structural negative-feedback
contract** — constraints on artifact shape that make several classes of
cascade failure impossible at construction time, regardless of agent
behaviour.

### Five structural invariants (I1–I5)

Enforced as Pydantic validators that fire at envelope construction:

| # | Invariant | Why |
|---|-----------|-----|
| **I1** | `supporting_signals` is non-empty | A claim with no evidence is not a claim. |
| **I2** | HIGH/CRITICAL severity requires ≥2 distinct ContextLayers in supporting_signals | Triangulation. Single-source escalation is the OWASP "Cascading Hallucination Attack" pattern — disallowed structurally. |
| **I3** | confidence > 0.85 with empty `known_missing_context` is rejected — UNLESS `confidence_basis = DETERMINISTIC_AGGREGATE` | Overconfidence guard. Deterministic agents with complete coverage may legitimately claim 0.95+; LLM-derived content may not. |
| **I4** | `confidence_basis = LLM_SELF_REPORT` cannot escalate severity above MEDIUM | The hard safety property. LLM-self-rated confidence is poorly calibrated (Kadavath 2022, Lin 2022, post-2024 RLHF literature) and cannot drive HIGH/CRITICAL alarms alone. |
| **I5** | `signals_that_would_invalidate` is non-empty for any artifact with confidence > 0.5 and severity above LOW | Falsifiability. An unfalsifiable claim cannot self-heal. |

### The `ConfidenceBasis` distinction (the load-bearing field)

Five-valued enum. **This is the most important field on the envelope when
LLM-derived content is in flight.**

| Basis | Trust level | Used by |
|-------|------------|---------|
| `DETERMINISTIC_AGGREGATE` | High | Auditor, Monitor, Optimizer verify steps |
| `RETRIEVAL_GROUNDED` | Medium-high | AI Runtime when L0 sources are cited |
| `CROSS_AGENT_AGREEMENT` | Medium-high | Outputs that multiple independent agents agree on |
| `LLM_SELF_REPORT` | Low | AI Runtime narrative without citations |
| `UNCERTAIN` | Honest unknown | Any agent declaring it doesn't know |

The schema design rationale, evidence base, and the explicit pushback on
"ask the LLM to rate its confidence" is documented at length in
`docs/handoff_envelope_design.md`. Short version: LLMs are poorly
calibrated self-raters; the envelope makes that fact visible by tagging
confidence with how it was derived, and validator I4 caps the blast
radius of self-reported confidence.

### What envelopes do NOT solve

The envelope is necessary but not sufficient. It enforces shape, not
semantics. An agent can produce a structurally-valid envelope with
nonsense content — two unrelated supporting signals from different layers
satisfy I2 syntactically even if neither actually supports the claim.
The runtime validators in Part 5 are the second line of defense.

---

## Part 5 — Cascade self-healing: six structural negative-feedback mechanisms

The Waiter paper's argument is that pure agent-to-agent cascades are
positive-feedback systems — each agent treats the previous agent's output
as ground truth and amplifies any errors. The classical control-theory
remedy is *negative feedback*: a reference signal, a comparison, and a
correction that reduces the error in subsequent iterations.

A naive cascade has none of those. The architecture introduces **six
distinct structural mechanisms**, each addressing a distinct failure mode.
Combined, they make the cascade behave more like a homeostatic system
than an open-loop amplifier.

### 1. Re-grounding at every step

Each agent re-pulls the relevant slice of live state and verifies upstream
claims before acting on them. The Auditor saying "FlexHD on ch6" is
treated as a hypothesis; the Security Agent re-checks the radio table and
aborts if the world has changed. **This is negative feedback in the strict
sense:** comparison against ground truth produces an error signal that
drives correction.

**Status:** partially implemented (Optimizer re-snapshots before every
write). Phase 10 work: extend to Security Agent and Reporter so every
agent in the chain re-grounds before acting on upstream claims.

### 2. Triangulation requirement

Every Finding/Recommendation must be supported by at least two
independent signals from *different* ContextLayers before it can carry a
severity above LOW. **One signal is a hypothesis; two converging signals
are evidence.**

**Status:** shipped at the schema layer (envelope I2). Wiring Phase 10
work: agents must populate `supporting_signals` with appropriate L0/L1/L2/L3/L4
references when emitting envelopes.

### 3. Disagreement-as-required-output

Every artifact must populate `confidence`, `confidence_basis`,
`known_missing_context`, and `signals_that_would_invalidate`. Empty values
on actionable artifacts are rejected (envelope I3, I5). **This forces
uncertainty to propagate through the cascade visibly rather than be
absorbed silently.**

**Status:** shipped at the schema layer. Phase 10 work: agent code must
populate these fields meaningfully (the most likely failure mode here is
boilerplate "if any input is wrong" in the invalidation field — a
quality issue we'll need to monitor in early operation).

### 4. TTL + automatic revocation

Every Recommendation has a built-in expiration and a re-validation path.
After N hours, the test that generated the Recommendation runs again
against fresh state. If the underlying condition no longer holds, the
Recommendation is auto-revoked. **The system forgets stale claims rather
than letting them sit indefinitely waiting for human action.**

**Status:** not yet shipped. Phase 10 design must include this; without
it, an approved-but-not-yet-executed Recommendation can become wrong if
the network changes between approval and execution.

### 5. System-level invariants — safety interlocks

A small set of invariants in `config/invariants.yaml` that must always
hold regardless of any agent's recommendations. Examples:

- "Camera VLAN must always be no-internet."
- "Total firewall rules cannot increase by more than 5 per day without
  explicit operator override."
- "Total port forwards cannot increase without a corresponding origin
  story."

Any agent recommendation that would violate an invariant is rejected by
the invariant checker — not by the receiving agent. **These are hard
guardrails, the negative-feedback equivalent of a safety interlock in
industrial control.**

**Status:** not yet shipped. The permission model (`permission_model.yaml`)
is the closest existing mechanism but operates per-action; invariants
operate cross-action over time.

### 6. Bidirectional flow / closed-loop adaptation

The Reporter (end of the chain) computes meta-signals about cascade
behaviour — severity distribution drift, finding-novelty score,
recommendation acceptance rate — and these signals flow *back* into the
next cycle as priors. If 80% of last week's HIGH findings were dismissed
by the operator, the Auditor's severity calibration shifts down for
similar findings next run.

**Status:** not yet shipped. Most architecturally novel of the six;
deferred to post-Phase-10 once we have enough operational data to compute
meta-signals against.

### Tension to be aware of: overdamping

The Waiter paper §5.2 warns that too many feedback loops produce sluggish
systems — every recommendation requires triangulation + invariant check +
re-grounding + TTL renewal, and nothing happens. Each mechanism above
must address a *distinct* failure mode (and they do — see the per-row
"Why" in the table at top of Part 4). If two mechanisms collapse into
the same defense, drop one.

The endgame: a cascade that converges on stable correct states
automatically, where errors are visible at every handoff (rather than
absorbed), stale claims expire (rather than persist), boundary violations
are rejected (rather than negotiated), and the human is needed only for
genuinely novel decisions.

---

## Part 6 — Orchestration

The CLI is the human-facing orchestrator today. There is **no central
runtime event bus yet** — coordination is operator-driven. Phase 10 adds
the in-process scheduler (Monitor every 5 min, Auditor hourly, full sweep
daily, weekly upgrade scan).

### The orchestration graph (planned: `config/orchestration_graph.yaml`)

Phase-10-prerequisite. A machine-readable contract: nodes (agents) with
`emits`, `consumes`, `writes`; edges (artifact flows) with per-edge
requirements (`requires_validator`, `requires_evidence_bundle`,
`requires_permission_check`, `requires_snapshot_before_write`). Every
agent's emit/consume sites are testable against this graph; violations
caught in CI.

### Permission model — the single hard gate

`config/permission_model.yaml` classifies every write action into:

- **AUTO** — reversible, low blast-radius. Optimizer applies through full
  safety pipeline.
- **REQUIRES_APPROVAL** — human approves before execution.
- **NEVER** — refused categorically with logged refusal.

Default for unlisted actions: `REQUIRES_APPROVAL` (default-deny).
Firewall, VLAN, and camera changes are **always** REQUIRES_APPROVAL
regardless of how obvious they look — per `CLAUDE.md` Critical Rule #5.

The Optimizer cannot apply a non-AUTO action even when explicitly told
to; the operator must use the explicit human-approval execution path
(Phase 10: iOS approve-and-execute via the interrupt/resume pattern).

### Phase 10 approval flow: interrupt/resume, not polling

LangGraph's `interrupt()` pattern is the model. REQUIRES_APPROVAL action
encountered → scheduler pauses the thread, persists the interrupt payload
(full Recommendation + envelope), marks the thread as waiting, resumes
cleanly when iOS sends `Command(resume=approve|reject)` keyed by
thread_id. **Approval payload includes `exact_action_payload_hash` and
`expires_at`** — the operator approves *the exact action that gets
executed*, not a mutable recommendation that can drift between approval
and execution.

### Cascade risk callouts

**The classify_client resonance loop (the canonical example).** Today the
keyword-heuristic client classifier feeds the Security Agent's VLAN
proposal which the operator approves which feeds re-classification on the
next sweep. This is the OWASP-named "Cascading Hallucination Attack"
pattern: false information propagates, embeds, and amplifies across
interconnected systems. **Currently the human breaks the loop by being
between every step. Phase 10 removes that.**

The fix (Phase 10 blocker): cross-check classification against a *second*
signal (DPI fingerprint, port-usage profile, OUI manufacturer). When
heuristic and second signal disagree, return `SecurityTier.UNCERTAIN`
with the conflict surfaced. **Disagreement is information; flag it,
don't silently overwrite.**

### What is NOT coordinated today

The latent cascade chain is `Auditor finding → Security Agent VLAN
proposal → Optimizer write → Reporter narrative`. Today the human breaks
the loop. With Phase 10's scheduler, the cascade gets longer and the
operator moves further from each step. **The validator pattern
(`AIRuntime.review_config_change` between every agent's emit step and any
consumer that acts on it) is the planned mitigation; today it is wired
only inside the Security Agent.** This is a Phase 10 blocker — not a
roadmap item.

---

## Part 7 — Audit checklist

Use this section to verify the implementation against the architectural
intent. Items are grep-able / runnable / measurable.

### Context discipline

- [ ] **No operator-specific facts in agent code.** Grep
  `src/network_engineer/agents/` for hardcoded SSIDs, MACs, device names,
  IP subnets. Expected: zero. Migrated examples: `_KNOWN_OFFLINE` and
  `_KNOWN_CAPTIVE_PORTAL_SSIDS` (both removed).
- [ ] **All operator knowledge in YAML.** `config/*.yaml` (gitignored
  except the catalog/probes/identification YAMLs that are
  community-contributable). Examples checked into
  `examples/*.example.yaml`.
- [ ] **Probes feel conversational, not form-like.** Read 5 random probe
  prompts. Each should explain *why* the question matters.

### Layer 0 domain knowledge discipline (NEW — to be checked once Phase 1 ships)

- [ ] **Every Recommendation carries `sources_consulted`** in its
  envelope. Recommendations without citations get a visible "unverified —
  training-weight only" badge in Reporter output.
- [ ] **Every catalog entry has `last_verified_date`.** Stale entries
  (>180 days) trigger a re-verification prompt.
- [ ] **AI Runtime calls retrieve_domain_knowledge before generating
  recommendations.** Verify by inspecting the action log: every AI call
  should have a paired retrieval call.

### Cascade discipline

- [ ] **Optimizer always snapshots before write.** `apply_change()` line
  ordering: tier check → snapshot_before → apply → wait → verify →
  snapshot_after → log. Single function, easy to audit.
- [ ] **Cascade validators between every emit→consume pair.** Today only
  Security Agent runs its own output through
  `AIRuntime.review_config_change`. **All other emit→consume pairs are
  still bare** — Phase 10 blocker.
- [ ] **Permission model is consulted, not bypassed.** Grep for any code
  path that calls `client.set_*` or `client.delete_*` without
  `permissions.check()` first. Expected: only
  `scripts/apply_approved_remediations.py`, the explicit
  human-approval execution path.

### Envelope discipline (NEW)

- [ ] **Every inter-agent artifact wrapped in `HandoffEnvelope`.** Grep
  for direct `Finding(...)` / `Recommendation(...)` / `OptimizerResult(...)`
  emissions outside the envelope. Expected: zero (after task #38).
- [ ] **All five invariants I1–I5 fire on bad envelopes.** `tests/test_envelope.py`
  has 19 tests covering positive and negative cases.
- [ ] **`confidence_basis` reflects actual derivation.** AI Runtime
  emissions with empty `sources_consulted` should be `LLM_SELF_REPORT`,
  not `RETRIEVAL_GROUNDED`.

### Profile time-series discipline (NEW — to be checked once #20 ships)

- [ ] **`profile_history.yaml` is append-only.** No code path overwrites
  history entries. The file should grow monotonically.
- [ ] **Every Recommendation records `source_profile_version`.**
  Recommendations against stale profiles trigger re-evaluation.
- [ ] **`nye reassess` does NOT overwrite the current profile.** It
  proposes changes; operator confirms each one; new history entries are
  created.

### Layer-1 wiring (Phase 10 blockers — checking these specifically)

- [ ] Auditor severity bands tuned by
  `household_profile.security.iot_phone_home_concern`
- [ ] Monitor escalates WAN drops during business hours when
  `work.conferencing_quality_critical == 'mission_critical'`
- [ ] Security Agent uses `security.iot_isolation_appetite` to choose
  VLAN aggressiveness
- [ ] Security Agent preserves DMZ networks with `do_not_touch` origin
  stories rather than collapsing into the four-tier proposal
- [ ] Reporter narratives reference profile context (work-from-home,
  kids, etc.)
- [ ] Upgrade Agent urgency adjusted by
  `device_register.criticality`
- [ ] Monitor consults `tools/baseline.py` for anomaly framing

These are wire-up tasks — the *signals* exist (Layers 0–4 are all
captured); the *consumption sites* in agent code are still TODO. The four
trust-destroying-if-missing-on-first-run sites (Monitor WAN, Security
isolation appetite, Security DMZ preservation, Reporter narrative
references) are the Phase 10 blocker subset.

### Open-source readiness

- [ ] Greenfield + heritage modes both work end-to-end with no
  operator-specific assumptions.
- [ ] `nye onboard` runs cleanly when no profile, no registers, no
  dismissals exist (the cold-start case).
- [ ] CONTRIBUTING.md documents probe-library contribution surface +
  L0 domain-knowledge contribution surface.
- [ ] Examples cover every operator knowledge file so a forker can
  populate by hand if they prefer.
- [ ] Discovery layer (Phase 10 work, task #32) gracefully handles
  non-UDM controllers (USG, CloudKey, Network App).

---

## Part 8 — Phase 10 release gate

Phase 10 is **not done when FastAPI + APScheduler are running.** It is
done when the system can responsibly run agents in scheduled sequence
without a human between every step.

### Hard release gate (must hold before Phase 10 ships)

1. **HandoffEnvelope shipped + every agent emits envelopes (tasks #21,
   #38).** Required for the rest to be enforceable.
2. **Orchestration graph YAML exists + tested (task #37).** Declares
   legal flows; tests verify agents conform.
3. **Cascade validators wired between every emit→consume pair (task
   #22).** Each pair has either a deterministic validator or an
   `AIRuntime.review_config_change` pass.
4. **Layer 0 domain knowledge minimum-viable retrieval (task #35).**
   Phase-1 curated YAML with retrieval tool. Recommendations carry
   `sources_consulted` populated from L0.
5. **Four minimum HouseholdProfile consumption sites wired (task #25).**
   Monitor WAN escalation, Security isolation appetite, DMZ origin-story
   preservation, Reporter narrative references.
6. **classify_client resonance fix (task #24).** Cross-check + UNCERTAIN
   tier on disagreement.
7. **Approval as interrupt/resume, not polling (task #26).** Persistent
   interrupt payloads keyed by thread_id; `exact_action_payload_hash`
   prevents drift between approval and execution.

### Soft gate (can ship in flight during Phase 10 v1)

8. **Profile time-series (task #20).** Operator can re-run `nye onboard`
   in the interim.
9. **Validator tiers framework (task #23).** Can be retrofitted onto
   envelopes after launch.
10. **Stale-context protection (task #28).** Add max-age rules when an
    actual stale-context incident teaches us where they're needed.
11. **Durable run records (task #27).** Crash-recovery is desirable but
    not blocking if Phase 10 v1 is operator-supervised initially.

### P1 (post-launch additive)

Tracing (#29), Stab/Drift regression suite (#30), context contracts
(#31), discovery layer for non-UDM (#32), artifact-handle pattern (#33),
task ledger (#34), architecture doc updates (#36).

### Done-when criterion

Auditor → Security Agent → Optimizer → Reporter run end-to-end through
HandoffEnvelopes; validators fire on violations; every Recommendation
carries citations from L0; the scheduler can pause/resume on
REQUIRES_APPROVAL via the interrupt pattern; a 30-day operational dry-run
on the home LAN produces no I1–I5 invariant violations and no operator-
override-against-recommendation incidents that the soft-gate items would
have caught.

---

## Part 9 — Phase roadmap as it relates to context architecture

| Phase | What it adds to context architecture |
|-------|--------------------------------------|
| 0–6 | Layers 3 and 4 captured; agents emit findings/recommendations |
| 7 | AI Runtime — model orchestration layer |
| 8 | Security Agent — first agent emitting structured Recommendations consuming L2 |
| 9 | Upgrade Agent — first agent with a community-contributable catalog |
| 9.5 | Device + client registry — per-MAC operator knowledge surface |
| 9.6 | Dismissals + origin stories — finding-level operator overrides |
| 9.7 | Probe-driven onboarding — L1 capture |
| **9.8 (current)** | **HandoffEnvelope + cascade self-healing analysis** |
| 10 | **Layer 0 domain knowledge + scheduler + interrupt/resume + remaining wire-up** |
| 11 | Supabase mirror — Layers 0/1/2/4 sync to cloud; cross-device + iOS read/write |
| 12 | WebSocket + APNs — events streamed to operator in real time |
| 13 | iOS app — probe-driven onboarding **as a mobile UI** |
| 14–15 | Cloudflare tunnel + Mac Studio migration — runtime relocation |
| 16 | Hardening — agent-watchdogs, sanity-check cron jobs |

The probe library (Phase 9.7) feeds directly into the iOS UI (Phase 13):
the same probe definitions drive the conversational engine in CLI today
and the guided-questionnaire UI on iOS later. The data model is the
contract; the surface changes.

---

## Part 10 — Where to start auditing

If you are auditing this for the first time, read in this order:

1. **`config/permission_model.yaml`** — the safety bedrock. Five
   minutes.
2. **`tools/schemas.py`** — every shape the system uses. Skim the
   section headers; the 11 profile submodels and the four registry models
   are the data contract.
3. **`tools/envelope.py` + `docs/handoff_envelope_design.md`** — the
   inter-agent contract. ~20 minutes.
4. **`tools/probes.py` + `agents/onboarding_agent.py`** — how Layer 1 is
   captured. The conversational engine's `pick_next_probe` is the most
   architecturally interesting piece.
5. **`agents/auditor.py` → `agents/security_agent.py` →
   `agents/optimizer.py`** — read in this order to see how findings flow
   into recommendations into write actions, and where the cascade-damping
   happens.
6. **`agents/ai_runtime.py`** — last because it's the model orchestration
   layer; the deterministic agents above are the system's spine, and AI
   adds narrative without taking over decisions.

A reasonable audit pass takes ~90 minutes if you have the source open
and test the assertions against real `grep`s.

---

## Part 11 — Experimental framing

> Reviewers: this is the most important section. Skim everything else if
> you must, but do not skip this.

This system is a documented experiment. We are betting on a specific set
of architectural choices, derived from a thesis (the Waiter-and-the-App
signal-architecture argument), informed by parallel work (LangGraph, ADK,
Microsoft Agent Framework patterns; the LLM calibration literature; the
OWASP threat model for agentic systems). The bets are reasonable; they
are not settled.

### What we are betting on

1. **That signal architecture, not model size, is the dominant factor in
   agentic reliability for this class of system.** Evidence: directional
   support from the Waiter paper, the multi-agent debate literature, the
   tool-grounded-reasoning work (ReAct, Toolformer). Not yet
   experimentally confirmed for personal-scale home network agents.

2. **That structural negative-feedback contracts (the HandoffEnvelope
   I1–I5, the six cascade self-healing mechanisms) are sufficient to
   keep the multi-agent cascade stable under autonomous scheduling.**
   Evidence: control-theoretic reasoning + multi-agent framework
   convergence. Not yet validated under months of production load.

3. **That LLM-self-reported confidence is poorly calibrated and must be
   structurally bounded.** Evidence: Kadavath et al. 2022, Lin et al.
   2022, post-2024 RLHF calibration literature. Reasonably well-supported.

4. **That triangulation (≥2 independent context layers for high-severity
   claims) approximates "independent error patterns" closely enough to be
   useful.** Evidence: ensemble-method literature in ML;
   defense-in-depth literature in security. The "different layer ⇒
   independent" heuristic is approximate and could be wrong in cases
   where layers share root data.

5. **That Layer 0 domain knowledge retrieval, with proper citations,
   produces materially more trustworthy recommendations than training-
   weight-only generation.** Evidence: the broader RAG literature. Open
   question: source authority for *this* specific domain (UniFi
   configuration) varies; vendor docs sometimes lag firmware reality.

### What would falsify each bet

For a reviewer evaluating whether the thesis is right or wrong, here is
what evidence would change my mind on each bet:

- **Bet 1** (signal architecture dominates): if doubling model size on a
  fixed signal architecture produces dramatically better recommendations
  than enriching the signal architecture on the same model, the thesis
  is wrong. Testable as Stab measurement at fixed context level across
  model tiers.

- **Bet 2** (structural contracts sufficient): if the system, run
  autonomously for 30+ days, produces cascade failures the contracts did
  not catch — and those failures occur at rates close to a control
  configuration without the contracts — the contracts aren't doing what
  we think.

- **Bet 3** (LLM calibration cap is necessary): if envelopes with
  `RETRIEVAL_GROUNDED` confidence basis produce wrong recommendations at
  rates similar to envelopes with `LLM_SELF_REPORT` basis, the
  citations aren't actually adding signal and the cap is over-engineered.

- **Bet 4** (triangulation approximates independence): if the
  triangulation requirement systematically rejects legitimate single-
  source HIGH findings AND those rejected findings were correct, the
  approximation is too strict.

- **Bet 5** (Layer 0 is materially better): if the rate of correct
  recommendations does not improve when Layer 0 is wired vs. when it is
  disabled, retrieval isn't paying its cost.

### How operational data feeds back into design

The system is being built with instrumentation to test these bets. The
durable run records (task #27), tracing (task #29), and Stab/Drift
regression suite (task #30) are designed not just to debug failures but
to produce data that confirms or refutes the architectural choices.
This is deliberate. A passion project that ships and tells us nothing
about whether the architecture works is less valuable than one that
ships, runs imperfectly, and tells us *exactly* where the architecture
needs adjustment.

### Stochastic systems are stochastic — the blanket caveat

Every recommendation this system makes carries irreducible uncertainty
because somewhere in the chain an LLM is involved (in the AI Runtime
narrative pass, in the heuristic classifier's ambiguous cases, in the
operator's interpretation of probes). We cannot make the system
deterministic. We can:

- make the deterministic agents (Auditor, Monitor, Optimizer) the spine
- bound LLM blast radius via the envelope's `confidence_basis` constraint
- require LLM claims to be backed by retrievable sources (Layer 0) before
  escalating
- require multi-signal triangulation before high-severity action
- snapshot before every write so any failure is reversible
- log everything so we can learn from failures

What we cannot do is promise that a recommendation is correct. We can
only promise that it was generated by a documented process, with
specific evidence cited, against a snapshot we can replay. That is the
honest contract this system makes with its operator.

---

## Provenance + open questions

This document is co-authored: the project's code and structural choices
were made by the development sessions logged in the project history;
the reviews that surfaced the gaps it now reflects came from independent
external feedback. Three reviews have shaped the current shape:

- The Waiter-and-the-App architectural thesis (operator-authored)
- An "orchestration patterns" review citing LangGraph, AutoGen,
  CrewAI, OpenAI Agents SDK, ADK, Pydantic AI patterns
- A LangGraph/ADK/Microsoft Agent Framework comparative review with
  specific Phase 10 blockers

Plus the operator's own contributions on:
- The temporal/life-stage requirement (HouseholdProfile is a time
  series)
- The Layer 0 domain knowledge gap
- The reframe from personal tool to open-source UniFi-enthusiast
  project

Open questions a reviewer should flag if they see them:

- **Have we missed any structural feedback mechanisms** beyond the six in
  Part 5? Bidirectional flow is the most speculative; are there others
  we should consider?
- **Is the L0/L1/L2/L3/L4 layer model the right decomposition?** Could
  there be a Layer 5 we haven't identified — physical-world signals
  (RF measurements, neighbour networks, ambient noise) that no other
  layer captures?
- **Are the I1–I5 invariants the right set?** Are there additional
  structural constraints we should enforce at envelope construction?
  The currently-listed invariants are necessary; are they sufficient?
- **Is the LLM-self-report cap (I4) too strict, too loose, or right?**
  We are betting on "cap at MEDIUM"; reasonable arguments exist for
  "cap at LOW" or "no cap, but must be triangulated."
- **Should the Phase 10 release gate be tighter?** We have 7 hard
  blockers and 4 soft. Reviewers may want all 11 to be hard.

These are open. Feedback that changes the answers is welcome and
expected.
