# Directive compliance log

Per directives §6 (Reporting Contract). One file per directive when complete.

| Directive | Title | Tier | Status | Doc |
|---|---|---|---|---|
| 1.1 | Move permission enforcement into UnifiClient | 1 | complete (stronger superset) | [1.1.md](1.1.md) |
| 1.2 | SSL verification hardening | 2 | complete (PINNED stub) | [1.2.md](1.2.md) |
| 1.3 | Operator-YAML prompt-injection sanitization | 1 | complete | [1.3.md](1.3.md) |
| 1.4 | Dismissals TTL & auto-revocation | 1 | complete | [1.4.md](1.4.md) |
| 1.5 | Path traversal guards | 2 | pending | — |
| 1.6 | Redact AI response previews in logs | 2 | complete | [1.6.md](1.6.md) |
| 2.1 | Cascade instrumentation | 1 | pending | — |
| 2.2 | I6 confidence-basis validator | 1 | pending | — |
| 2.3 | Layer-independence matrix | 1 | pending | — |
| 2.4 | Profile-aware threshold builder | 1 | pending | — |
| 2.5 | Damping class analysis | 1 | pending | — |
| 2.6 | Split UnifiClient by API regime | 1 | pending | — |
| 2.7 | Cross-YAML config validator | 2 | pending | — |
| 2.8 | Async boundary | 2 | pending | — |
| 3.1 | Stab measurement harness | 1 | pending (substantial — see notes) | — |
| 3.2 | Drift quantification | 2 | pending | — |
| 3.3 | ρ measurement | 3 | pending | — |
| 3.4 | Nyquist-aware scheduler cadence | 4 | pending | — |
| 3.5 | Grey-region registry | 3 | pending | — |
| 3.6 | Resonance audit checklist for Layer 0 | 3 | pending | — |
| 3.7 | 30-day supervised dry-run | 4 | pending (operational milestone, not code) | — |
| 4.1 | Remove fallback sprawl | — | pending | — |
| 4.2 | Type the envelope source_id | — | pending | — |
| 4.3 | Reverse-engineered endpoint contract tests | — | pending | — |
| 4.4 | README honesty audit | — | ongoing | — |

## Tier ordering

Per directives §5, execution proceeds in topological order. Tier 1 items in flight or complete before Tier 2 begins.

## Outstanding methodology questions

- **3.1 corpus construction** — directive requires 200+ paraphrases × 5+ intent classes hand-curated. Acceptance language in this repo will scope §3.1 as "ship harness skeleton + one fully-populated intent class as a worked example" with the remaining intent classes treated as a research backlog. The reviewer should confirm or push back.
- **3.7 30-day supervised dry-run** — operational milestone, not code. Cannot be "implemented." Will start when the system is otherwise feature-complete and the live network is instrumented.

## Conventions

- Each compliance doc has frontmatter (directive id, title, status, acceptance items with pass/fail/pending), followed by free-form prose explaining what landed, divergences from the directive's literal spec (when applicable), and outstanding work.
- `commits` field lists the hash(es) where the directive's work landed. Useful for the reviewer doing a clean second-pass.
- Status values: `complete`, `complete (stronger superset)`, `partial`, `in_progress`, `pending`, `blocked`. Anything other than `complete` requires explicit reasoning.
