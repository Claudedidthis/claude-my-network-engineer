<!-- ATP_DESCRIPTION: A work-in-progress open-source AI network engineer for UniFi, built as a working laboratory for situated multi-agent reliability and structural cascade self-healing. -->
<!-- ATP_LIVE_URL:  -->

# ClaudeMyNetworkEngineer

> **🚧 Work in progress — pre-alpha. Architecture is the point; the agent is the experiment.**
>
> This is published partly as a usable tool and partly as a documented experiment in
> what enterprise-grade situated multi-agent reliability could look like at personal scale.
> Some of it works today. Some of it is scaffolded for the next phase. Read [`docs/architecture.md`](docs/architecture.md) before forming opinions on the design.

An open-source AI network engineer for [UniFi](https://ui.com/) networks. It audits your configuration, applies safe optimisations through a verified write pipeline, surfaces risky changes for human approval, and refuses categorically to do things that should never be automated. It also serves as a working laboratory for **situated multi-agent reliability** — context architecture, structural cascade self-healing, and the *Waiter-and-the-App* signal-architecture thesis applied to home networks.

## Two things this project is at the same time

**1. A practical tool.** If you have a UniFi gateway, this can already give you a structured audit of your network — sensitive port forwards, weakly-protected SSIDs, channel conflicts, EOL hardware, IoT segmentation gaps. With the AI runtime enabled it can produce a security posture analysis and review proposed configuration changes. With future phases it will run continuously, schedule sweeps, and surface findings to an iOS app.

**2. An experiment in agentic AI architecture.** Every architectural choice is documented as a *bet* — what evidence supports it, what evidence questions it, what would falsify it. The project is built around the thesis that the dominant factor in agentic reliability is not model size but **signal architecture** — what context the agent has at decision time, what contracts agents satisfy when they hand off to each other, and whether the cascade between agents is structurally damped or structurally amplifying. Concrete mechanisms include a five-layer context model, typed handoff envelopes with structural invariants, six negative-feedback patterns that make multi-agent chains self-healing rather than self-amplifying, and an explicit treatment of where LLM-self-reported confidence is a poor signal.

The point is not just to build a network tool. The point is to build a network tool whose architecture would scale to higher-stakes domains — to ask *what would it take for an agent system to be trustworthy enough to run autonomously?* and document the answer as code, not slides.

## Status

| Area | Status |
|---|---|
| Permission-model gated write pipeline (snapshot → apply → verify → log → rollback) | ✅ shipped |
| Probe-driven onboarding (94 probes across 11 themes, conversational engine) | ✅ shipped |
| Operator-knowledge registers (devices, clients, origin stories, dismissals) | ✅ shipped |
| Auditor + Monitor + Optimizer + Security Agent + Upgrade Agent + Reporter | ✅ shipped |
| AI Runtime with prompt caching + per-job model routing + LLM-confidence cap | ✅ shipped |
| HandoffEnvelope structural negative-feedback contract | ✅ shipped |
| Layer 0 domain-knowledge retrieval | 🚧 designed, not implemented |
| HouseholdProfile time-series + `nye reassess` | 🚧 designed, not implemented |
| Orchestration graph + cascade validators between every agent pair | 🚧 designed, in flight |
| FastAPI server + APScheduler-driven autonomous runtime | ⏳ next phase |
| iOS companion app | ⏳ deferred |
| Cloudflare Tunnel + Mac Studio migration | ⏳ deferred |

The full phase plan lives in [`docs/build-plan.md`](docs/build-plan.md). The Phase 10 release gate (the point at which the system can responsibly run autonomously) is documented in [`docs/architecture.md`](docs/architecture.md) Part 8.

## Quickstart (audit-only)

```bash
git clone https://github.com/Claudedidthis/claude-my-network-engineer
cd claude-my-network-engineer
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[core,dev]"
cp .env.example .env
# Edit .env — set UNIFI_HOST and UNIFI_API_KEY at minimum.
nye onboard            # Captures your household profile (probe-driven conversation).
nye audit              # Runs the read-only audit.
```

Get a UniFi local API key at: **UniFi UI → Settings → Control Plane → Integrations → Create API Key**. The key works against the local controller only; it is not the cloud Site Manager API key.

## Deployment shapes

| Shape | Install | What you get |
|---|---|---|
| **Audit-only** | `pip install -e ".[core]"` | Read-only insights, deterministic checks, no AI, no cloud |
| **Local with AI** | `pip install -e ".[core,ai]"` | Adds Anthropic-powered narrative analysis + change review |
| **Local + server** | `pip install -e ".[core,ai,server]"` | Run as a daemon with FastAPI; access via curl/browser on LAN |
| **Full stack** | `pip install -e ".[core,ai,cloud,server,notifications]"` | Always-on with cloud durability + push notifications. iOS app + Cloudflare Tunnel are extra |

## Permission model — the safety bedrock

Every write action is classified into one of three tiers in [`config/permission_model.yaml`](config/permission_model.yaml):

| Tier | Examples |
|---|---|
| **AUTO** | Renames, channel changes, TX power, band steering, restarting an offline AP, blocking/unblocking known clients |
| **REQUIRES_APPROVAL** | VLANs, firewall rules, port forwards, Wi-Fi networks, DHCP, switch port profiles, anything affecting cameras |
| **NEVER** | Deleting devices, factory resets, disabling WAN, exposing management to the internet, plain-text credential storage |

Default for unlisted actions: **`REQUIRES_APPROVAL`** (default-deny). Firewall, VLAN, and camera changes are **always** REQUIRES_APPROVAL regardless of how obvious they look. The Optimizer cannot apply a non-AUTO action even when explicitly told to — REQUIRES_APPROVAL actions go through an explicit human-approval execution path.

## What's different about this design

If you've used or built multi-agent systems before, the architecturally interesting parts are:

- **Five-layer context model.** Layer 0 (canonical domain knowledge — UniFi docs, CIS, NIST), Layer 1 (operator's household profile), Layer 2 (per-network operator knowledge), Layer 3 (live state), Layer 4 (telemetry baselines). Each layer feeds different agents differently. Triangulation across *different* layers is required for high-severity claims.
- **HandoffEnvelope contract** with five structural invariants (I1–I5) enforced at construction time. The most important is I4: an LLM-self-reported confidence cannot escalate severity above MEDIUM. The rationale (and the research evidence on LLM calibration) is in [`docs/handoff_envelope_design.md`](docs/handoff_envelope_design.md).
- **Six structural negative-feedback mechanisms** for cascade self-healing — re-grounding, triangulation, disagreement-as-required-output, TTL/auto-revocation, system-level invariants, bidirectional flow. None alone is enough; the combination makes the cascade behave more like a homeostatic system than an open-loop amplifier.
- **Stochastic systems are stochastic.** The architecture is explicit about what cannot be guaranteed (no recommendation can be promised correct) and what can (every recommendation is generated by a documented process, with specific evidence cited, against a snapshot we can replay). This is the honest contract the system makes with its operator.

Read [`docs/architecture.md`](docs/architecture.md) for the full map.

## Working on a fork

The project is built to run anywhere with a UniFi gateway, not just on the author's network. Operator-specific facts (gateway IP, household composition, dismissal allowlists, origin stories, device annotations) are gitignored and operator-supplied via:

- `nye onboard` — interactive probe-driven conversation that captures your household profile.
- `nye registry walkthrough` — interactive walkthrough that helps you identify unknown MAC addresses with manufacturer-specific tips.
- `examples/*.example.yaml` — checked-in templates if you prefer hand-editing.
- Optionally a `CLAUDE.local.md` next to `CLAUDE.md` — if you use Claude Code, drop free-form operator context here and it will be loaded automatically via the `@CLAUDE.local.md` import.

If anything in the agent code looks operator-specific (a hardcoded IP, SSID, MAC, or device name), that is a bug — please open an issue.

## Contributing

The probe library, identification tips, OUI database, upgrade catalog, and Layer 0 domain knowledge entries are all designed as community contribution surfaces. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the schema and review process for each.

## License

MIT. See [`LICENSE`](LICENSE).

## Further reading

- [`docs/architecture.md`](docs/architecture.md) — full architectural map (~1000 lines, structured for audit)
- [`docs/handoff_envelope_design.md`](docs/handoff_envelope_design.md) — schema rationale + research citations + LLM-calibration pushback
- [`docs/build-plan.md`](docs/build-plan.md) — the 16-phase build sequence
- [`docs/research/prior-art.md`](docs/research/prior-art.md) — landscape evaluation
