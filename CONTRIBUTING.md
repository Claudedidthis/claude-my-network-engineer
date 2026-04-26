# Contributing

This project is built phase-by-phase per [`docs/build-plan.md`](docs/build-plan.md). Bug reports, feature requests, and PRs are welcome. Before opening a PR, please read [`CLAUDE.md`](CLAUDE.md) and the build plan.

## Ground rules

1. **The permission model is sacred.** Any new write action must be classified in `config/permission_model.yaml`. Default to `REQUIRES_APPROVAL` unless you have a strong reason to put it in `AUTO`. Firewall, VLAN, and camera changes are `NEVER` autonomous.
2. **Snapshot before write, always.** Every write path must call `tools/unifi_client.snapshot()` first, with the snapshot path captured in the audit log entry.
3. **One change per operation.** No batched writes. Even logically related changes get sequenced.
4. **Tests against fixtures, not the live network.** CI runs against canned snapshots in `tests/fixtures/`. Capture your own with `nye audit --capture-fixture` (Phase 1+).

## Adding a check to the Auditor

Auditor checks live in `src/network_engineer/agents/auditor.py` as named functions returning a `Finding(severity, code, title, detail, evidence)`. Add a function, register it in the auditor's check list, add a test in `tests/test_auditor.py` against the fixture.

## Adding an alert threshold

Edit `config/alert_thresholds.yaml`. The Monitor evaluates these on each poll. Add a test in `tests/test_monitor.py` if the threshold introduces new logic.

## Adding an upgrade catalog entry

Edit `config/upgrade_catalog.yaml` (lands in Phase 9). Each entry: model, EOL date, successor, notes. Vendor-specific data; community-contributable.

## Adding onboarding probes (high-leverage community contribution)

The onboarding agent learns about an operator's situated context through **probes** — single conversational questions paired with metadata about how to interpret the answer, when it's relevant, and what follow-ups it triggers. The probe library lives in `config/probes/<theme>.yaml` and is the single biggest community-contribution surface in this project.

Anything you've encountered in your own UniFi setup that the existing probes wouldn't have caught is worth a PR. Examples we've already seen:

- A solar installer putting their Zigbee gateway on the operator's network → probe `devices.solar_third_party_iot`
- Pre-1950 metal-lath plaster walls turning a house into a Faraday cage → probe `building.construction_era`
- A CPAP that loses cloud reporting if WiFi drops → probe `household.medical_devices_on_network`

To add a probe:

1. Pick (or add) a theme file in `config/probes/`. Themes: `origin`, `building`, `isp`, `household`, `work`, `devices`, `security`, `usage`, `infrastructure`, `preferences`, `future`.
2. Add an entry with `id`, `theme`, `prompt`, `kind`, `field_path`, `priority`, `surfaces_concern`. See `tools/probes.py` for the full schema and existing files for examples.
3. Probe IDs are namespaced by theme (`devices.has_solar`, `building.attic_accessible_for_cable`). Pick one that's stable — it lands in the operator's `probes_answered` list and changing it invalidates resume state.
4. **Probes should feel like a conversation, not a form.** The best probes surface latent context the operator wouldn't volunteer (the solar example is canonical). Prompt phrasing matters; PRs that simply add fields without thoughtful prompts will get pushback.
5. If your probe targets a profile field that doesn't exist yet, add it to the relevant submodel in `tools/schemas.py`.
6. Add a test only if you've added new logic — pure probe additions are picked up automatically by `test_real_probe_library_loads_cleanly`.

## Adding a sub-agent

Sub-agents live in `src/network_engineer/agents/`. Every sub-agent must:

1. Receive tasks only from the Orchestrator (no peer-to-peer routing).
2. Pass every write through `tools/permissions.check()` before touching `tools/unifi_client`.
3. Snapshot before any write.
4. Log every action to the appropriate log file (`logs/agent_actions.log`, `logs/recommendations.log`, etc.).
5. Have a fixture-mode test in `tests/`.

## Code style

- **Ruff** for linting: `ruff check .`
- **Black** for formatting: `black src/ tests/`
- **mypy** for types: `mypy src/`
- **pytest** for tests: `pytest`

Run `ruff check . && mypy src/ && pytest` before pushing.

## Optional layers

Each optional subsystem (`[ai]`, `[cloud]`, `[server]`, `[notifications]`, `[ios]`, `[tunnel]`) must be skippable. Do not introduce hard dependencies between layers. The core path must work with `pip install -e ".[core]"` only.

## Reporting security issues

Do not open a public issue for a security vulnerability. Email the project maintainer directly. The threat model assumes the agent operates a real network with cameras and a home office — please treat findings accordingly.
