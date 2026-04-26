# ClaudeMyNetworkEngineer — Build Plan

**Status:** Pre-Phase 0 (prep complete, build not yet started)
**Owner:** David Vickers
**Last updated:** 2026-04-24

This is the working document for how we build the system described in `ClaudeMyNetworkEngineer_ProjectBrief.md`. It supplements the brief — the brief is the *what* and *why*, this is the *how* and *in what order*.

---

## Guiding principles

1. **Laptop now, Mac Studio later.** Nothing hardcoded to a hostname or absolute path beyond what's in `.env`. The migration step at the end is: copy repo → copy `.env` → `pip install` → install launchd plist → install `cloudflared`. Nothing else.
2. **Read before write, always.** Every phase that touches the network starts read-only against the live UDM (or canned fixtures when off-LAN), then introduces writes only after the safety primitives (snapshot, permission gate, log, rollback) are proven.
3. **Fixture-first dev loop.** The laptop won't always be on the home LAN. Capture a real network snapshot early; from then on, every agent runs against fixtures during dev and against the live UDM only when home.
4. **One model ID source of truth.** As of 2026-04-24 the latest Claude family is 4.X (Opus 4.7, Sonnet 4.6, Haiku 4.5). The brief's references to `claude-sonnet-4-5` are pre-4.6/4.7 — defaults in `config/ai_runtime_config.yaml` use the current model IDs.
5. **Cloud is async, fire-and-forget.** Supabase/R2 writes never block a local operation. Local logs and snapshots are authoritative; cloud is the durability layer.
6. **Modular by design — open-sourceable.** Hard separation between core and optional subsystems. Each optional subsystem is a `pyproject.toml` extra. A fork that wants only the read-only Auditor `pip install`s `[core]` and is done. No user-specific config in git; that goes to `config/example.*.json` with real config gitignored.

---

## Modularity & open-source goals

The project is built to be forkable. Someone with their own UniFi network, their own opinions about cloud, and no Apple Developer account should be able to run a useful subset out of the box.

### Core (always required)

- Orchestrator + permission model
- UniFi client wrapper + snapshot/rollback primitives
- Auditor, Reporter, Monitor, Optimizer (with full safety pipeline)
- Local JSON-line logs + on-disk snapshots
- CLI entry points for every agent

The core has zero hard dependency on Anthropic, Supabase, R2, FastAPI, Cloudflare, or Apple. It runs offline against the local UDM.

### Optional layers

| Extra | Brings | Phases | If skipped, fork uses |
|---|---|---|---|
| `[ai]` | Anthropic AI Runtime — narrative analysis, security posture, monthly reports | 7 | Deterministic findings only (no narrative) |
| `[cloud]` | Supabase + Cloudflare R2 durability | 11 | Local-only logs and snapshots |
| `[server]` | FastAPI HTTP/WebSocket server, scheduler | 10 | Pure-CLI invocation; cron for scheduling |
| `[notifications]` | APNs push notifications via `aioapns` | 12 | Logs/webhooks for alerting |
| `[ios]` | SwiftUI companion app (separate Xcode project) | 13 | CLI / web dashboard / direct UDM UI |
| `[tunnel]` | Cloudflare Tunnel scaffolding for remote access | 14, 15 | LAN-only, WireGuard, or alternative VPN |

### Configuration layers (separation of concerns)

- `config/permission_model.yaml` — framework defaults; users may override
- `config/alert_thresholds.yaml` — framework defaults; users may override
- `config/network_profile.json` — **user-specific** (gitignored). Ships only as `config/example.network_profile.json` with David's home as a worked example
- `config/agent_personas.yaml` — framework defaults
- `config/upgrade_catalog.yaml` — framework data; community-contributable
- `.env` — credentials and per-deployment toggles (always gitignored)

### Files added in Phase 0 to make this OSS-ready

- `LICENSE` — MIT
- `pyproject.toml` with `[project.optional-dependencies]` per the table above
- `README.md` — explains what it is and the four canonical deployment shapes (audit-only, local-with-AI, full-cloud, full-with-iOS)
- `CONTRIBUTING.md` — how to add a check, an alert threshold, an upgrade catalog entry, or a sub-agent
- `examples/` — sample configs for each deployment shape
- `.github/workflows/` (optional, deferred) — CI for tests + lint

### Source layout deviation from the brief

The brief shows top-level `agents/`, `tools/`, `server/` directories. For OSS-ability (clean import namespace, pip-installability without name collisions), we use the modern **`src/` layout** instead:

```
src/network_engineer/
    agents/
    tools/
    server/
```

Imports become `from network_engineer.agents.auditor import Auditor`. The CLI is exposed as a single `nye` command (subcommands: `audit`, `monitor`, `optimize`, `report`, `serve`) via `[project.scripts]` so end users never type the full path. Throughout this plan, when we refer to e.g. `agents/auditor.py`, the actual path is `src/network_engineer/agents/auditor.py`.

---

## Phase 0 — Repository bootstrap & safety rails  *(laptop, ~45 min)*

A clean, version-controlled, OSS-ready scaffold where committing a credential is impossible.

1. `git init` in the project root.
2. Write `.gitignore`: `.env`, `.env.local`, `logs/`, `snapshots/`, `__pycache__/`, `*.pyc`, `.venv/`, `dist/`, `build/`, `*.egg-info/`, `ios_app/build/`, `ios_app/DerivedData/`, `*.xcuserstate`, `config/network_profile.json` (the real one — example version stays tracked).
3. Write `LICENSE` — MIT, with David Vickers as copyright holder (OSS-friendly, most adoptable).
4. Write `pyproject.toml` with `[project.optional-dependencies]`:
   - `core`: `unifi-sm-api`, `httpx`, `python-dotenv`, `pyyaml`, `pydantic>=2`, `rich`, `tenacity`
   - `ai`: `anthropic`
   - `cloud`: `supabase`, `boto3`
   - `server`: `fastapi`, `uvicorn[standard]`, `apscheduler`, `websockets`
   - `notifications`: `aioapns`
   - `tunnel`: (none — `cloudflared` is a system binary)
   - `dev`: `pytest`, `pytest-asyncio`, `ruff`, `mypy`, `black`, `pip-tools`
5. Write `requirements-dev.txt` pinned via `pip-compile` for reproducible dev (run after step 7).
6. Create the directory skeleton from §2 of the brief, with empty `__init__.py` in `agents/`, `tools/`, `server/`, `tests/`. Add `examples/` and `docs/research/` (already exists).
7. Create venv: `python3 -m venv .venv`, activate, then `pip install -e ".[core,dev]"` first (this is what most contributors will run). Layer additional extras as we hit later phases.
8. Pre-commit hook (shell, no extra dep) that fails if `.env` is staged or any tracked file matches obvious key patterns (`UNIFI_API_KEY=[A-Za-z0-9]`, `sk-ant-`, etc.). Uses `git diff --cached`.
9. Write `CLAUDE.md` at root using brief §3 verbatim, plus a "Build mode vs. Live mode" section and a "Working on a fork" section explaining the modular extras.
10. Write `README.md` — short, project-style: what it is, why someone might want it, the four canonical deployment shapes (audit-only, local-with-AI, full-cloud, full-with-iOS), 3-line quickstart for each.
11. Write `CONTRIBUTING.md` — how to add a check, alert threshold, upgrade catalog entry, or sub-agent.
12. Move user-specific seed data: David's network details that lived in the brief land in `examples/network_profile.example.json` rather than `config/network_profile.json`. The real config is gitignored.
13. First commit (after pre-commit hook is in place — it should pass on this commit).

**Done when:** `git status` is clean; pre-commit hook blocks staging `.env`; `pip install -e ".[core,dev]"` succeeds in a fresh venv; `pytest -q` passes (zero tests is fine — confirms the package imports).

---

## Phase 1 — UniFi client (read-only) + fixture capture  *(laptop, on home LAN, ~2–3 hr)*

Talk to the UDM, capture a real network snapshot, never need to be on-LAN again for agent dev. Per the prior-art research, `unifi_client.py` is a **thin wrapper** around `unifi-sm-api` (for v1 Integration paths) plus a small `httpx`-based classic-API helper (for endpoints v1 omits — firewall rules, traffic rules, WLANs, port forwards). Wrapper retains ownership of snapshots, audit logging, permission gating.

1. `tools/unifi_client.py`: a `UnifiClient` class.
   - Reads `UNIFI_HOST`, `UNIFI_API_KEY`, etc. from env (via `python-dotenv`).
   - Resolves the **site UUID** on init via `GET /sites` and caches it (the v1 API requires UUID in paths; the legacy `default` string returns 400).
   - V1 reads delegated to `unifi-sm-api`: `get_devices`, `get_clients`, `get_sites`, `get_networks`, `get_stats`.
   - Classic-API reads via internal `httpx.Client` for endpoints v1 doesn't expose: `get_firewall_rules`, `get_traffic_rules`, `get_wifi_networks`, `get_port_forwards`. Endpoint paths and payload shapes mined from `sirkirby/unifi-network-rules` source (see `docs/research/prior-art.md`).
   - Pagination handled internally (defaults to `limit=25`; loop until `totalCount` reached).
   - Protect API access via internal helper — `get_protect_cameras`, `get_protect_alerts`.
2. `--test` CLI returns device count, client count, UniFi Network app version, Protect app version. Exit non-zero on failure.
3. `snapshot()` fetches every read endpoint into a single dated JSON in `snapshots/`. Used before every write later. Schema versioned (`"schema_version": 1`).
4. Run `--test`, run `snapshot()` against the live UDM (already smoke-tested in pre-Phase-0 work — site UUID, device count = 9, clients = 36, networks = 4 confirmed).
5. Copy the freshest snapshot into `tests/fixtures/baseline_snapshot.json` (full + redacted variant — MACs/IPs scrambled).
6. `UnifiClient(use_fixtures=True)` mode loads from `tests/fixtures/`. Toggle via `UNIFI_MODE=fixtures|live`.
7. `tests/test_unifi_client.py` — verify fixture loading, Pydantic typing, pagination loop, site-UUID caching.

**Done when:** `UNIFI_MODE=fixtures python tools/unifi_client.py --test` works from a coffee shop and against `live` returns real data.

---

## Phase 2 — Permission model + Orchestrator skeleton  *(laptop, ~2 hr)*

A permission gate everything else routes through, with tests.

1. `config/permission_model.yaml` — copy §4 of the brief verbatim.
2. `tools/permissions.py` — `check(action_name) → AUTO | REQUIRES_APPROVAL | NEVER`. Default to `REQUIRES_APPROVAL` for unlisted actions.
3. `agents/orchestrator.py` — accepts a task dict, dispatches to the right sub-agent, gates writes through `tools/permissions.py`. Sub-agents stubbed.
4. `tools/logging_setup.py` — JSON-line structured logging to `logs/agent_actions.log`, `logs/recommendations.log`, `logs/upgrade_recommendations.log`, `logs/errors.log`.
5. `tests/test_permission_model.py` — table of (action, expected_tier) covering every line in §4 plus unlisted-action cases.

**Done when:** Tests green; orchestrator refuses any unlisted write.

---

## Phase 3 — Auditor (read-only) + first findings report  *(laptop, ~3–4 hr)*

First real deliverable.

1. `agents/auditor.py` — pulls a snapshot, runs §5.2 checks, returns `Finding(severity, code, title, detail, evidence)` objects.
2. §7 priority-queue checks baked in as named functions: `check_channel_conflict`, `check_dns_success_rate`, `check_flat_topology`, `check_iot_isolation`, `check_camera_isolation`, `check_g4_pro_offline_expected`, etc.
3. Pre-seed expected-offline list (G4 Pro) so it logs INFO not WARNING.
4. CLI: `python -m agents.auditor --output stdout|json|markdown`.
5. `tests/test_auditor.py` — assert channel-conflict and flat-topology findings appear when run against the fixture.

**Done when:** `python -m agents.auditor --output markdown` produces a readable report matching §7.

---

## Phase 4 — Reporter + canonical log/event schema  *(laptop, ~1–2 hr)*

Lock in data shapes before more agents start producing data.

1. `agents/reporter.py` — reads logs + latest snapshot, produces daily summary, change log, on-demand audit report (markdown).
2. Pydantic models in `tools/schemas.py`: `NetworkEvent`, `AgentAction`, `Finding`, `Recommendation`, `UpgradeRecommendation`, `PendingApproval` — these map 1:1 to the Supabase tables in Phase 11.
3. CLI: `python -m agents.reporter daily|changes|audit`.

**Done when:** Reporter against today's fixture-based audit produces a readable markdown report.

---

## Phase 5 — Monitor + alert thresholds  *(laptop, ~2 hr)*

Continuous polling that emits events but does not act.

1. `config/alert_thresholds.yaml` — copy §8 of the brief verbatim.
2. `agents/monitor.py` — pulls metrics, evaluates thresholds, emits `NetworkEvent` to `agent_actions.log`.
3. CLI: `python -m agents.monitor --once` (single sweep), `--watch` (loop).
4. `tests/test_monitor.py` — feed a fixture with breached thresholds, assert the right events.

**Done when:** `--once` against the fixture produces the expected WARNING for the 93% DNS success rate.

---

## Phase 6 — Optimizer with full write-path safety  *(laptop, on home LAN, ~3–4 hr)*

First real write path, exercising the full safety pipeline.

1. `tools/config_diff.py` — diff two snapshot JSONs into a human-readable patch.
2. `tools/rollback.py` — restores from snapshot (REQUIRES_APPROVAL).
3. Add write methods to `UnifiClient`: `set_ap_channel`, `set_ap_tx_power`, `set_device_name`, `set_client_tag`, `restart_device`.
4. `agents/optimizer.py` — for each proposed change runs the §9 checklist: snapshot → permission check (AUTO?) → apply ONE change → wait → re-snapshot → verify → log → success. On failure: log + auto-rollback + halt.
5. **Live test #1:** Rename one device (AUTO, low risk). Verify snapshot, log entry, change in UniFi UI. Rename back via the same path.
6. **Live test #2:** Resolve the FlexHD/U6 IW Ch. 48 conflict (§7 priority 1). Verify retry rates improve in next monitor sweep.

**Done when:** Snapshots exist for both renames + the channel change; `agent_actions.log` is clean; channel conflict resolved.

---

## Phase 7 — AI Runtime  *(laptop, ~2–3 hr)*  ·  **Optional layer: `[ai]`**

One wrapper for all Anthropic API calls. Two concrete jobs to start. **Every agent that calls AIRuntime checks `AI_RUNTIME_ENABLED` first and falls back to deterministic output if disabled** — this keeps the core fork usable without an Anthropic key.

1. `agents/ai_runtime.py` — `AIRuntime` class with **prompt caching enabled** (system prompt + network context block both cached; per-call question uncached). Use the `claude-api` skill at this step.
2. Job → model mapping in `config/ai_runtime_config.yaml`:
   - `analyze_security_posture` (daily) → **Opus 4.7**
   - `generate_monthly_report` (monthly) → **Opus 4.7**
   - `review_config_change` (default) → **Sonnet 4.6**; **Opus 4.7** when change touches firewall/VLAN/cameras
   - `explain_anomaly` (on alert) → **Sonnet 4.6**
   - `natural_language_query` (iOS on-demand) → **Sonnet 4.6**, route obvious-simple queries to **Haiku 4.5**
   - `score_upgrade_recommendation` (weekly) → **Haiku 4.5**
3. First two jobs: `analyze_security_posture(snapshot)` and `review_config_change(proposed, current)`.
4. `config/ai_runtime_config.yaml` — budgets per §13 of the brief. Token spend logged on every call to `agent_actions.log`.
5. Stub fallback path: when `AI_RUNTIME_ENABLED=false`, every method returns a structured "AI disabled" result with deterministic fields populated. No exceptions thrown.

**Done when:** `analyze_security_posture` against the fixture returns a structured `SecurityAnalysis` JSON that names the IoT-on-trusted-VLAN issue. With `AI_RUNTIME_ENABLED=false`, the same call returns a deterministic placeholder without erroring.

---

## Phase 8 — Security Agent (recommendations only, no writes)  *(laptop, ~2 hr)*

Generate the VLAN proposal as a `Recommendation`. No writes — every action this agent could take is REQUIRES_APPROVAL.

1. `agents/security_agent.py` — runs §5.4 read-only audits, emits the §5.4 VLAN architecture as a structured recommendation, includes risk + rollback.
2. Pipes the recommendation through `AIRuntime.review_config_change` for a sanity-check narrative.
3. CLI: `python -m agents.security_agent propose-vlans`.

**Done when:** `recommendations.log` contains a complete VLAN proposal you'd be willing to look at on your phone and tap "Approve."

---

## Phase 9 — Upgrade Agent + catalog  *(laptop, ~2 hr)*

1. `config/upgrade_catalog.yaml` — UAP-AC-Lite, UP Chime PoE, U6 IW, FlexHD, USW Pro Max 16, etc. EOL/firmware/successor for each.
2. `agents/upgrade_agent.py` — weekly-sweep logic from §14, scoring formula from the table, narrative via `AIRuntime.score_upgrade_recommendation` (Haiku).
3. Output to `logs/upgrade_recommendations.log` matching the JSON format in §14.

**Done when:** Sweep produces the four pre-seeded candidates with scores in the right ballpark.

---

## Phase 10 — FastAPI server (LAN-only)  *(laptop, ~3 hr)*  ·  **Optional layer: `[server]`**

HTTP surface that the iOS app will eventually talk to. Test with `curl` for now.

1. `server/api_server.py` — every endpoint in §12. Bind to `127.0.0.1:8765`.
2. Bearer-token auth (token in `.env`, easily rotated).
3. CORS allowlist: the operator's LAN subnet (e.g. `192.168.1.0/24`); Cloudflare-tunnel origin added in Phase 14.
4. `server/scheduler.py` — APScheduler in-process: Monitor every 5 min, Auditor hourly, full sweep daily. Lifespan-managed by FastAPI.
5. **Smoke test with curl:** every endpoint, including approve/dismiss against a fake pending recommendation.

**Done when:** `curl -H "Authorization: Bearer …" http://127.0.0.1:8765/status` returns live data and the scheduler ticks Monitor on a 5-minute cadence.

---

## Phase 11 — Supabase + R2 cloud sync  *(laptop, ~3 hr)*  ·  **Optional layer: `[cloud]`**

Forensic durability. Critical events survive a Mac Studio outage.

1. Create the Supabase project, run the §17 schema. Enable RLS. Service key for Mac Studio; anon key with read-only RLS for iOS (write only on `pending_approvals.status`).
2. Create the R2 bucket `network-engineer-backups`; lifecycle rules per §17 (90/30/365).
3. `tools/cloud_sync.py` — async, fire-and-forget. `log_event`, `log_action`, `push_approval_request`, `daily_backup`. Wraps every call in `asyncio.create_task` + 5s timeout. Failure → `errors.log`, never re-raise.
4. `tools/r2_client.py` — boto3 against the R2 S3 endpoint. `upload_snapshot`, `upload_daily_backup`, `list_backups`, `download_backup`.
5. Wire `cloud_sync.log_event` into Monitor's WARNING+ path; `cloud_sync.log_action` into Optimizer's success path.
6. Wire snapshot writer to also push to R2 under `snapshots/YYYY-MM-DD/HHMMSS_pre-<action>.json`.
7. Daily 03:00 job in `scheduler.py`: full snapshot → R2 `backups/daily/`.

**Done when:** Apply a tiny AUTO change → Supabase row exists → R2 snapshot exists. Pull the network cable on the laptop, verify local op still succeeds and only cloud writes errored.

---

## Phase 12 — WebSocket + APNs scaffolding  *(laptop, ~2 hr — partial; APNs cert defers to migration)*  ·  **Optional layer: `[notifications]`**

1. `server/websocket_server.py` — `/live` channel pushing event + state diffs as JSON. Same auth token.
2. `server/apns_dispatcher.py` — APNs HTTP/2 token-based auth. Sandbox endpoint for dev.
3. Notification categories from §15 registered.
4. **Defer to Phase 13/15:** real device-token registration (needs the iOS app); production APNs (needs production-signed iOS build).

**Done when:** WebSocket clients can subscribe and see Monitor events arrive in real time.

---

## Phase 13 — iOS companion app  *(laptop, ~1–2 days)*  ·  **Optional layer: `[ios]`**

1. Xcode project in `ios_app/`. Bundle ID `com.<your-org>.networkEngineer`, iOS 17.0+, SwiftUI.
2. `Services/APIClient.swift` — async/await, LAN-first base URL with Cloudflare tunnel URL fallback (cached LAN URL with 2s timeout → tunnel URL). Token in Keychain.
3. `Services/SupabaseClient.swift` — fallback when both LAN and tunnel fail. Realtime subscription to CRITICAL events.
4. `Services/NotificationManager.swift` — APNs registration on first launch, posts device token to Mac Studio.
5. Views: Dashboard, Alerts, Devices, Recommendations, Upgrades, Approvals, Ask.
6. Apple Developer setup: APNs Auth Key (.p8), drop into Mac Studio `.env` as `APNS_KEY_PATH`, `APNS_KEY_ID`, `APNS_TEAM_ID`.
7. Test on a physical iPhone on home WiFi.

**Done when:** Dashboard live; tap-to-approve on a real REQUIRES_APPROVAL item works end-to-end.

---

## Phase 14 — Cloudflare Tunnel for remote access  *(real install in Phase 15)*  ·  **Optional layer: `[tunnel]`**

**On laptop now:**
- Subdomain: `network.<your-domain>`.
- Draft `~/.cloudflared/config.yml` template in `config/cloudflared.config.yml` (version-controlled).
- Draft Cloudflare Access policy (IP-lock + email OTP fallback) as a runbook in `docs/cloudflare-access-setup.md`.

**Optional dev test:** `brew install cloudflared && cloudflared tunnel login && cloudflared tunnel create dev-test` against a throwaway subdomain. Tear down before migration.

---

## Phase 15 — Migration to Mac Studio  *(half a day)*

Laptop becomes the dev machine, Mac Studio becomes prod.

1. Mac Studio prereqs: Python 3.12+, Homebrew, `git`, `cloudflared`.
2. `git clone` (or `rsync`) repo to `/Users/<your-user>/<projects>/claude-my-network-engineer` on Mac Studio.
3. `python3 -m venv .venv && pip install -r requirements.txt`.
4. Recreate `.env` on Mac Studio (1Password or USB stick — never copy unencrypted across the network). New UniFi API key issued specifically for the Mac Studio (revoke the laptop key).
5. Install APNs Auth Key file at the path referenced by `.env`.
6. `brew install cloudflared`, `cloudflared tunnel login`, `cloudflared tunnel create network-engineer`. Drop credentials JSON into `~/.cloudflared/`. Use the version-controlled `config/cloudflared.config.yml`. Point `network.<your-domain>` DNS at the tunnel.
7. Cloudflare Access policy (IP-lock + email OTP) on the application.
8. Install `~/Library/LaunchAgents/com.<your-org>.networkEngineer.plist` for the API server. `cloudflared service install`.
9. `launchctl load` both. Verify `curl https://network.<your-domain>/status -H "Authorization: Bearer …"` works from cellular.
10. Switch laptop `.env` to `UNIFI_MODE=fixtures`; laptop is dev-only from here.
11. Add a runbook entry in `README.md`: "to run the system, do nothing — it's running on the Mac Studio."

**Done when:** Reboot the Mac Studio, log out, watch the iOS app come back to life on its own.

---

## Phase 16 — Hardening & monitoring-the-monitor  *(post-migration, ongoing)*

- Weekly job: download yesterday's R2 backup and validate JSON.
- `/_health` endpoint the Mac Studio polls every minute and writes to Supabase. If it stops writing for >10 min, the iOS app shows "Mac Studio appears offline."
- Snapshot retention enforcement (local + R2).
- Phase-2 AI runtime jobs from §13 (anomaly explanation, monthly narrative).
- Future capabilities backlog from §16 of the brief.

---

## Open questions before Phase 0

1. **Are you on the home LAN right now?** Phase 1 needs to talk to the UDM at least once to capture the fixture.
2. **Supabase + R2 accounts** — exist already on your Cloudflare account?
3. **APNs Auth Key** — already generated, or do we generate one when we hit Phase 13?
4. **Anthropic API key** — confirmed available for Phase 7? (And: revoked the one that leaked in chat?)
5. **Comfortable with the model-ID correction** (Opus 4.7 / Sonnet 4.6 / Haiku 4.5 instead of the brief's 4-5 references)?
