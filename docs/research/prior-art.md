# Prior Art Survey — ClaudeMyNetworkEngineer

**Date:** 2026-04-24
**Researcher:** Claude Code research agent (Opus, web-search)
**Purpose:** Before writing code, evaluate existing OSS projects for things we should adopt, learn from, or avoid.

---

## 1. UniFi Python API clients

| Project | Stars | Last push | License | Notes | Verdict |
|---|---|---|---|---|---|
| [Kane610/aiounifi](https://github.com/Kane610/aiounifi) | 89 | 2026-04-23 | MIT | Async aiohttp client; powers Home Assistant. Targets the **classic controller API** (`/api/s/...`), not Integration v1. | Read source for patterns; **do not adopt as runtime dep** (forces async everywhere) |
| [unifi-sm-api](https://pypi.org/project/unifi-sm-api/) (`netalertx`) | low | 2026-02-19 | MIT | Specifically targets `/proxy/network/integration/v1/` with `X-API-KEY` auth. Exposes `get_sites/get_unifi_devices/get_clients`. | **Adopt** as the v1-Integration adapter underneath our wrapper |
| [unifi-official-api](https://pypi.org/project/unifi-official-api/) (`ruaan-deysel`) | low | 2026-03-30 | MIT | Async + Pydantic, covers Network and Protect. | Learn from (Pydantic typing pattern); adoption risk too high |
| [finish06/pyunifi](https://github.com/finish06/pyunifi) | 246 | 2024-05-02 | MIT | Sync `requests` wrapper. | Avoid — stale |
| [tnware/unifi-controller-api](https://github.com/tnware/unifi-controller-api) | 14 | 2025-12-27 | MIT | Classic API only. | Avoid — low adoption |
| `unificontrol` | — | 2020 | — | | Avoid — abandoned |

## 2. Home Assistant UniFi integration

- HA core `unifi` integration uses `aiounifi`. It pragmatically falls back to undocumented controller endpoints for things absent from v1.
- [sirkirby/unifi-network-rules](https://github.com/sirkirby/unifi-network-rules) — 52 stars, 2026-04-23, MIT. HACS custom integration; manages firewall policies, traffic rules, port forwards, routes via the legacy controller endpoints (requires Network app 9.0.92+).
  - **Verdict: Mine the source as the canonical reference for the firewall-rule and traffic-rule endpoints that v1 still doesn't expose.** Don't take a runtime dep on a HA component.

## 3. UniFi audit / monitoring tools

- [unpoller/unpoller](https://github.com/unpoller/unpoller) — 2,598 stars, 2026-04-14, MIT. Go daemon → Prometheus/InfluxDB/Loki, 12 Grafana dashboards.
  - **Verdict:** Defer adoption. Our Monitor agent suffices for now. We can layer `unpoller` later on the Mac Studio if we want long-horizon historical telemetry for AI analysis.
- No mature **Python-native** UniFi auditing tool exists. Channel-conflict / security-gap analysis is greenfield in Python.

## 4. AI / LLM-powered network engineering

- Survey articles (NetPilot, Networkers Home) and Anthropic's April 2026 "Glasswing/Mythos" preview confirm LLMs are being used for config review.
- **No open-source project applies an LLM to a live UniFi (or any home-network) controller with autonomous safe-apply semantics.** Closest adjacency is [enuno/unifi-mcp-server](https://github.com/enuno/unifi-mcp-server) (MCP wrapper, tiny scope).
- **Verdict: We are genuinely first-mover.** Build our own safety primitives (snapshot → permission gate → one change → verify → log → rollback). Borrow the governance pattern (validation → human approval → audit log) from the survey articles.

## 5. Multi-agent ops frameworks

- [LangGraph](https://www.langchain.com/langgraph) — best stateful orchestration for hierarchical conductor patterns.
  - **Verdict:** Defer. Our orchestrator is simple (task → permission check → sub-agent → log). LangGraph shines on complex branching/parallel state graphs, which we don't have. Migrate later if warranted.
- CrewAI, kyegomez/swarms — role-based / swarm patterns. Learn from role definitions; don't add a framework dep.
- [arXiv 2511.15755](https://arxiv.org/abs/2511.15755) — multi-agent incident response paper. Useful conceptual reference for hierarchical-vs-single-agent.

## 6. APNs Python libraries

| Library | Stars | Last push | License | Verdict |
|---|---|---|---|---|
| [Fatal1ty/aioapns](https://github.com/Fatal1ty/aioapns) | 159 | 2025-04-14 | Apache-2.0 | **Adopt.** Async HTTP/2, native `.p8` token auth, Python 3.10+ |
| [Pr0Ger/PyAPNs2](https://github.com/Pr0Ger/PyAPNs2) | 358 | 2024-04-19 | MIT | Avoid — sync, no Py3.10+ |

## 7. Cloudflare Tunnel + FastAPI

- [hyprchs/cloudflare-tunnel-template](https://github.com/hyprchs/cloudflare-tunnel-template) — small but exact-fit setup script for tunnel + Access app + One-time-PIN IdP + policies. **Adopt.**
- Cloudflare's official tutorial: [Validate Access JWT with FastAPI](https://developers.cloudflare.com/cloudflare-one/tutorials/fastapi/) — canonical JWT middleware. **Adopt verbatim.**
- [Wytamma/with-cloudflared](https://github.com/Wytamma/with-cloudflared) — context manager for dev. Learn from for local iteration only.

## 8. Open-source SwiftUI UniFi / home-network apps

- **No third-party SwiftUI UniFi client found.**
- Pull UI patterns from generic dashboard apps (Home Assistant Companion iOS, etc.) when we hit Phase 13.

---

## What this means for our build

We **layer two UniFi clients** under `tools/unifi_client.py`:

- `unifi-sm-api` for everything the **Integration v1 API** exposes (devices, clients, sites, stats, networks)
- A small **classic-API helper** (httpx-based) for endpoints v1 still omits — firewall rules, traffic rules, WLANs, port forwards. Endpoint paths and payload shapes mined from `sirkirby/unifi-network-rules` source.

Our wrapper retains ownership of the things no library should manage for us: snapshots, permission gating, one-change-per-operation discipline, audit logging.

From Home Assistant's playbook, steal the **single Controller object that owns the session and re-emits typed events** pattern.

For AI-driven network audit we are **genuinely first-mover** — design our own guardrails (dry-run → diff → policy gate → human-approval queue → audit log to Supabase). No reference implementation exists.

For Prometheus-grade telemetry, defer `unpoller` until we know we want long-horizon historical data.

For APNs, adopt **`aioapns`** — async-native, `.p8` token auth, Apache-2.0, fits our FastAPI/asyncio stack.

For the tunnel, use **Cloudflare's official FastAPI JWT-validation tutorial** plus the **`hyprchs/cloudflare-tunnel-template`** setup script.
