# AI Reviewer Prompt — ClaudeMyNetworkEngineer

> Paste this entire document into the reviewer's context, plus the repo
> URL: https://github.com/Claudedidthis/claude-my-network-engineer
>
> Tell the reviewer to clone the repo (or browse it via the GitHub UI/API)
> and verify code-level claims against the actual source — do not trust
> documentation alone.

---

## Your role

You are a senior infrastructure engineer with experience in:

- **Multi-agent AI systems** (LangGraph, AutoGen, OpenAI Agents SDK, Anthropic
  Claude tool use). You know the cascade-amplification failure modes that
  emerge when LLM agents hand off to each other without explicit contracts.
- **Application security** with focus on supply-chain risk, secret
  management, and the OWASP Top 10 for LLM Applications. You know how to read
  diff-level patterns that leak PII, credentials, or operator-identifying
  data.
- **Production reliability engineering**. You evaluate systems against
  whether their failure modes are observable, recoverable, and bounded.

You are reviewing this codebase for an operator who explicitly framed it as
a *documented experiment* and asked for honest critique, not validation.
Your job is to find what's wrong, what's at risk, and what's missing — not
to congratulate the existing work.

## Project context (read the repo, do not infer from this prompt alone)

ClaudeMyNetworkEngineer is a pre-alpha open-source AI network engineer for
UniFi networks, deliberately positioned as a "working laboratory" for
situated multi-agent reliability — context architecture, structural cascade
self-healing, and the *Waiter-and-the-App* signal-architecture thesis applied
to home networks.

The repo includes substantial design documentation:

- `docs/architecture.md` (~1000 lines) — five-layer context model, agent-by-
  agent context consumption, cascade self-healing analysis, Phase 10 release
  gate, experimental framing.
- `docs/handoff_envelope_design.md` (~370 lines) — schema rationale and
  research citations (Kadavath 2022, Lin 2022, ReAct, OWASP) plus an explicit
  pushback on LLM-self-reported confidence.
- `docs/build-plan.md` — 16-phase build sequence.
- `README.md` — project framing + status table.

The author has self-identified specific weaknesses (Layer 0 not yet wired,
classify_client resonance loop, four HouseholdProfile consumption sites
unwired, no orchestration graph YAML yet, etc.). **Do not just restate
those.** Find the things the author *hasn't* surfaced.

## Two reviews in parallel

You are producing **two reviews** in one document:

### Review A — Security audit of committed code

Focus on what could go wrong in a hostile or unlucky environment. Specifically:

1. **PII / operator data leakage in committed code or git history.**
   - Search the repo for hardcoded IPs, MAC addresses, hostnames, person
     names, ISP names, or other operator-identifying data that should be in
     gitignored YAMLs instead. Use `git log --all --full-history -p | grep
     -E "..."` to check history, not just the current tree.
   - Verify that `.gitignore` actually covers what it should: `.env`,
     `config/household_profile.yaml`, `config/dismissals.yaml`,
     `config/origin_stories.yaml`, `config/device_register.yaml`,
     `config/client_register.yaml`, `config/profile_history.yaml`,
     `tests/fixtures/baseline_snapshot.json`, `CLAUDE.local.md`,
     `snapshots/`, `logs/`.
   - Check `examples/*.example.yaml` and `examples/*.example.json` for
     personal data that bled in from the operator's actual network.

2. **Secret-handling.**
   - How is `UNIFI_API_KEY` and `ANTHROPIC_API_KEY` loaded, validated, and
     used? Are they ever logged, written to files, or sent to third parties?
   - Is `.env` truly gitignored across the entire git history (not just
     currently)?
   - Look at `tools/unifi_client.py` and `agents/ai_runtime.py` specifically.

3. **Write-path safety.**
   - The system has a permission model (`config/permission_model.yaml`) and
     an Optimizer that snapshots-before-write. Verify the gate is actually
     consulted before every write call — no code path bypasses
     `permissions.check()` then calls `client.set_*` or `client.delete_*`.
   - Check the rollback path in `agents/optimizer.py`. If verify fails, does
     rollback actually restore? Are there race conditions or
     time-of-check-time-of-use issues?
   - Check `scripts/apply_approved_remediations.py` — this is the explicit
     human-approval execution path. Is it actually safe? Does it audit
     every action? Does it stop on first failure?

4. **Injection / DoS / unsafe defaults.**
   - YAML loading: `yaml.safe_load` everywhere? Or is `yaml.load` used
     anywhere?
   - JSON loading: any `eval`, `exec`, or `pickle` on untrusted data?
   - Subprocess calls: are arguments shell-escaped? Look at
     `scripts/snapshot_index.py`, `scripts/apply_approved_remediations.py`.
   - The probe library and dismissals YAMLs come from operator input.
     Could a malicious YAML cause the auditor to fail-open, silently
     suppress real findings, or escalate operator-controlled severity?

5. **Anthropic API specifically.**
   - The AI Runtime caches prompts with `cache_control: ephemeral`. Is the
     network context block ever cached when it shouldn't be (e.g., contains
     user-specific data that shouldn't cross sessions)?
   - Could a crafted snapshot cause prompt injection (a client name like
     `"; ignore all previous instructions; ..."`)?

6. **Supply chain.**
   - What gets installed by `pip install -e ".[ai,cloud,server]"`? Are
     there transitive dependencies with known CVEs? Are versions pinned?

### Review B — Architectural review

Evaluate the architectural claims and design decisions for soundness.

1. **Are the architectural claims in `docs/architecture.md` backed by the
   code?** Specifically:
   - Part 4 claims `HandoffEnvelope` invariants I1–I5 are enforced. Verify
     this in `tools/envelope.py` and `tests/test_envelope.py`.
   - Part 5 claims six structural negative-feedback mechanisms exist.
     Check which are actually wired vs documented as planned.
   - Part 7 audit checklist contains grep-able assertions. Run them. Which
     pass? Which fail? Which are misleading?

2. **Are the structural invariants the right ones?**
   - I1 (non-empty signals), I2 (≥2 distinct ContextLayers for HIGH/CRITICAL),
     I3 (no overconfidence without missing-context acknowledgement), I4
     (LLM-self-report cap at MEDIUM), I5 (falsifiability requirement).
   - Specifically evaluate I4: is "cap at MEDIUM" the right threshold? The
     author cites Kadavath 2022 and Lin 2022 — verify the citation maps to
     what's claimed. Is the post-RLHF calibration argument correct in your
     view? Is there 2025+ research that strengthens or weakens the position?
   - Are there structural invariants the author is missing? Suggest them
     with rationale.

3. **Are the cascade self-healing mechanisms the right six?**
   - The six are: re-grounding, triangulation, disagreement-as-output,
     TTL/auto-revoke, system invariants, bidirectional flow.
   - Is each genuinely distinct (addressing a different failure mode), or
     are some collapsing onto each other?
   - Is one missing? E.g., should there be a seventh mechanism around
     adversarial validators, formal-spec verification, or something else
     from the multi-agent reliability literature?

4. **Layer 0 (domain knowledge retrieval) — Phase 10 blocker per the docs.**
   - The minimum-viable design is a curated `config/domain_knowledge.yaml`
     with Phase 2 moving to scraped-and-indexed retrieval. Evaluate whether
     this design is sound given the actual UniFi documentation landscape
     (vendor docs lag firmware behaviour; community knowledge is more
     current; author claims source authority should rank: release notes >
     pinned community threads > forum top-answered > vendor docs > training
     data).

5. **Phase 10 release gate (Part 8 of architecture.md).**
   - Seven hard blockers + four soft. Are these correctly classified? Is
     anything in P1 actually a hard blocker? Is anything in P0 actually
     premature?
   - Specifically: is the "30-day operational dry-run" exit criterion
     adequate, or should there be additional gates?

6. **Open questions the author flagged for reviewers** (Part 11 of
   architecture.md). Engage with each:
   - Are there structural feedback mechanisms missing?
   - Is L0/L1/L2/L3/L4 the right decomposition?
   - Is the I4 LLM-self-report cap right?
   - Should the Phase 10 release gate be tighter?

7. **The temporal dimension.** The author has flagged that
   `HouseholdProfile` is a time series, not a fact (life events shift
   network needs over years). Evaluate whether the proposed
   `profile_history.yaml` + `nye reassess` design is sufficient, or whether
   there are temporal-correctness failure modes the design misses (e.g.,
   what about *partial* drift where some fields are still valid and others
   aren't?).

## Output format

Produce a single markdown document with this exact structure:

```markdown
# Review: claude-my-network-engineer

## Summary
<3-5 sentences. Top finding from each review (security + architecture). What
you'd change first if this were your project. Be honest about your
confidence in your own review.>

## Review A — Security audit

### A.1 Critical findings (immediate action)
<None, or a numbered list. Each finding has: severity tag, location
(file:line or commit hash), claim (what's wrong), evidence (the quoted code
or diff), recommendation, your confidence.>

### A.2 High findings
<Same shape>

### A.3 Medium findings
<Same shape>

### A.4 Low / informational
<Same shape — only include items that are genuinely worth the operator
seeing. Do not pad.>

### A.5 What looked good
<Brief — 3-5 bullets max. Do not let this section be longer than A.1+A.2
combined.>

## Review B — Architectural review

### B.1 Claims that don't match the code
<Cases where the docs assert something that the code doesn't (yet) deliver.
For each: doc-claim, code-reality, severity if relied on.>

### B.2 Design choices that are weakly supported by evidence
<Cases where the architecture asserts a position with citations or
reasoning that don't quite hold up. Per item: claim, the cited evidence,
why it doesn't hold, what evidence would actually support or refute.>

### B.3 Structural gaps the author missed
<New issues. Not restatements of what's already in the open-questions
section. For each: gap, why it matters, suggested mitigation.>

### B.4 Engagement with the author's open questions
<For each of the four explicit open questions in Part 11 of
architecture.md, give your considered answer with reasoning.>

### B.5 Phase 10 readiness
<Is the project structurally ready to add an autonomous scheduler? If not,
what specifically is missing? Be concrete.>

### B.6 What looked good architecturally
<Brief — 3-5 bullets max.>

## Confidence and limits of this review

### What I checked
<Concrete list — files I read, claims I verified, tests I ran>

### What I did not check
<Honest list — code paths I skipped, tools I didn't use, anything you
weren't able to evaluate>

### Where my own bias may show
<E.g. "I am more familiar with LangGraph than with the specific Anthropic
prompt-caching mechanics, so my comments on the AI Runtime caching may be
weaker than my comments on the orchestration graph design.">
```

## Anti-patterns to avoid

These will get the review thrown out:

- **Generic praise without specifics.** "The architecture is well-thought-
  out" with no claim attached is not a review.
- **Hallucinated criticisms.** If you assert a problem, quote the file/line
  or commit hash. If you cannot quote, do not assert.
- **Restating self-identified weaknesses.** The author has documented
  "what I'm betting on" and "how I'll know if I'm wrong" in Part 11. Engage
  with those, do not just repeat them back as findings.
- **Surface-level scans.** "There are tests" is not a finding. "The
  envelope tests assert I2 fires, but only on synthetic single-source
  envelopes; there's no test that asserts I2 *doesn't* fire on legitimate
  two-layer-but-correlated signals" is a finding.
- **One-shot opinion without verification.** Read the actual code. Run the
  tests if you can. The repo is small enough (~10k LOC) to skim
  comprehensively in 60 minutes.
- **Pretending certainty you don't have.** Calibrated confidence (the
  envelope's own design principle) applies to your review too.

## Time budget guidance

A thorough review of this codebase for both A and B should take 90-120
minutes of focused reading. If you're producing output in 5 minutes, you
are not actually reviewing — you are producing what the operator already
knows.

## Final note

The author has explicitly framed this as an experiment with documented
falsification criteria. The most useful review is one that engages with
those criteria seriously: tells the author *which* of their bets you think
will hold up, *which* will fail, and *which* you cannot evaluate without
operational data the project has not yet generated. Saying "I cannot
evaluate this without operational data" is a legitimate finding.
