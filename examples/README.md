# Examples

Sample configurations and deployment patterns.

## `network_profile.example.json`

Worked example of `config/network_profile.json` — the per-deployment file describing your specific network. The real `config/network_profile.json` is **gitignored**; only `network_profile.example.json` lives in version control.

To use:

```bash
cp examples/network_profile.example.json config/network_profile.json
# Edit config/network_profile.json for your network
```

## Deployment patterns

### Audit-only (simplest)

```bash
pip install -e ".[core]"
cp .env.example .env
# Set UNIFI_HOST, UNIFI_API_KEY in .env
nye audit                 # Read-only findings report
```

### Local with AI

```bash
pip install -e ".[core,ai]"
# Set ANTHROPIC_API_KEY in .env in addition to UniFi vars
nye audit --with-ai       # Findings + Claude-narrated security posture
```

### Local + always-on server

```bash
pip install -e ".[core,ai,server]"
nye serve                 # FastAPI on 127.0.0.1:8765, scheduler running
curl -H "Authorization: Bearer $API_BEARER_TOKEN" http://127.0.0.1:8765/status
```

### Full stack (cloud durability + push)

```bash
pip install -e ".[core,ai,cloud,server,notifications]"
# Set Supabase + R2 + APNs credentials in .env
nye serve
# Events durably written to Supabase. Snapshots to R2.
# CRITICAL alerts pushed to iOS via APNs.
```

### Adding remote access (Cloudflare Tunnel)

Requires `cloudflared` system binary (install via `brew install cloudflared`). See `docs/build-plan.md` Phase 14/15 for the full setup runbook.
