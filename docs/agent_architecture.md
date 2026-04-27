# Agent Architecture

> **Status:** Draft — discussion document, not yet committed contract.
> Replace prior assumptions in `docs/architecture.md` once accepted.

This document defines what an "agent" is in this project, how it differs from a tool or a service, and how the operator-facing surface is built. It supersedes the implicit "10 agents in `src/network_engineer/agents/`" framing of earlier phases — that framing produced 10 form runners with the word *agent* in the filename. This document corrects course.

---

## 1. Definitions

| Term | Meaning |
|---|---|
| **Agent** | A runtime loop where an LLM chooses the next action from a set of tools. The agent owns a goal, observes state, decides, executes, observes the result, and repeats. It can ask the operator a question — but only when no tool can answer it. |
| **Tool** | A deterministic function. Same inputs always produce the same outputs. No LLM in the call path. Tools are what agents *do things with*. |
| **Service** | A stateless RPC-style wrapper around an LLM call (e.g. "given this snapshot, return a structured security analysis"). Called once, returns once. The current `AIRuntime` is a service. |

This project has **one agent** (the Conductor), **many tools**, and **one service** (`AIRuntime`).

---

## 2. The Conductor

### 2.1 What the Conductor is

The single operator-facing conversational agent. The operator never talks to anything else. Every interaction — first-run setup, day-to-day questions, audit walkthroughs, security planning, change approvals — flows through the same Conductor process.

The Conductor's job is **the conversation**, not the network operation. Network operations happen in tools; the Conductor decides which tool to call, when, and why.

### 2.2 What the Conductor is not

- It is not a script that walks a fixed list of probes.
- It does not own write permissions on the controller. Writes happen through `tools/change_executor.py` (the renamed Optimizer) which still requires an `ApprovedAction` artifact. The Conductor can *propose* a change and ask the operator to approve it; the executor tool is what actually applies it.
- It does not bypass the permission model. NEVER-tier actions are still NEVER, regardless of what the operator asks for in conversation.

### 2.3 Process model

A single long-lived process: `nye chat`.

```
operator runs:                nye chat
process loads:                durable memory (Tier 3)
process initializes:          working memory (Tier 1, empty)
process opens conversation:   prints greeting based on what it knows
process runs loop:            until operator quits or signals end
process exits:                writes session digest, persists durable memory
```

This is option (a) from the prior conversation: a REPL-shaped session the operator stays in. It is *not* a daemon, *not* a one-shot CLI command, *not* the FastAPI server. The FastAPI server (Phase 10) will eventually wrap the same Conductor logic for non-CLI clients (iOS app, web) — but the Conductor's loop primitive is identical in both shapes; only the I/O changes.

---

## 3. Memory architecture (mixed-tier)

Modeled after how Claude Code itself works. Three tiers; each promotes upward when the lower fills.

### Tier 1 — Working memory (10–12 turns)

A rolling window of the most recent operator/Conductor exchanges, verbatim. Holds the linguistic context for follow-up references ("the one we just talked about," "the second option"). Bounded; oldest pairs roll off into Tier 2.

- **Size**: 12 operator turns + 12 Conductor responses
- **Lifetime**: in-process only
- **Promotion**: when full, oldest 2 pairs are summarized and appended to the session digest

### Tier 2 — Session state

The current `nye chat` session's accumulated working state.

- Discovered facts (with confidence + evidence per fact)
- Decisions taken this session
- Pending follow-ups ("circle back to bandwidth concerns")
- Recent tool calls + results
- Session digest (compressed Tier 1 overflow)

- **Lifetime**: in-process for active session; serialized to `runs/<session_id>/state.json` on graceful exit and at periodic checkpoints
- **Promotion**: at session end, salient parts merge into Tier 3 (durable). Routine chatter does not promote.

### Tier 3 — Durable memory (interrogable)

Survives across sessions. The Conductor's long-term knowledge of the operator and the network.

| Store | Contents | Path |
|---|---|---|
| **HouseholdProfile** | Operator's situated context — use case, household, work patterns, security philosophy | `config/household_profile.yaml` |
| **Registry** | Per-device + per-client annotations (tier, owner, role, criticality) | `config/device_register.yaml`, `config/client_register.yaml` |
| **Origin stories** | Heritage rationale — why each non-default config artifact exists | `config/origin_stories.yaml` |
| **Dismissals** | Operator-confirmed finding suppressions (with TTL + fingerprint) | `config/dismissals.yaml` |
| **Caution markers** | Persistent RED/AMBER warnings the operator can see in the UI; never auto-extinguish | `runs/caution_markers.jsonl` |
| **Decision log** | Append-only record of significant Conductor decisions + rationale | `runs/decisions.jsonl` |
| **Architecture history** | Network changes over time — what changed, when, by whom (operator approval), why | `runs/architecture.jsonl` |
| **Findings history** | What audits surfaced + what was done | `runs/findings.jsonl` |
| **Session digests** | Compressed prior sessions, retrievable on demand | `runs/<session_id>/digest.md` |
| **Profile history** | Time-series of HouseholdProfile (Waiter §3 — profile is a time series, not a fact) | `config/profile_history.yaml` |
| **External corpus (Layer 0)** | Vendor docs + official networking curriculum, bundled at install-time | `data/corpus/` (in package) |

All operator-config files are gitignored. The decision/architecture/findings/caution logs are local-only too (under `runs/`, also gitignored). The external corpus is bundled with the package — operators have the agent's expertise from install-time-zero, no DNS-resolution dependency at first run.

### Caution markers — first-class persistent warnings

When the Conductor counsels the operator against an action, or when the auditor finds something the operator hasn't addressed, a `CautionMarker` is written to durable memory. Markers are visible in the UI as red or amber lights; they do not auto-extinguish.

```python
class CautionMarker(BaseModel):
    marker_id: str
    severity: Literal["RED", "AMBER"]                  # see severity threshold below
    origin: Literal["operator_override", "audit_finding"]
    target_kind: str                                    # port_forward | firewall_rule | wifi_network | device | network
    target_key: str                                     # the specific subject
    canonical_source: str                               # "NIST 800-53 SC-7" / "UniFi Hardening Guide §3.2" / "CIS Controls v8 §11.2"
    counsel_text: str                                   # what the agent said at decision time
    counseled_at: datetime
    counseled_in_session: str
    state: Literal["active", "acknowledged", "resolved"]
    operator_rationale: str | None                      # set when origin=operator_override
    finding_id: str | None                              # link to underlying Finding when origin=audit_finding
```

**Two origins, distinct conversational language:**

- `operator_override` — *"You asked me to open port 22 on 2026-04-26. I counseled against per NIST 800-53 SC-7. You confirmed and stated reason: 'temporary weekend access.' Filed."*
- `audit_finding` — *"I noticed your guest SSID is open with no captive portal. Per the UniFi hardening guide and CIS Controls v8, this is generally not done. Want to discuss?"*

**Three states, no expiry:**

- `active` — counseled but not opened/acknowledged. UI shows full red/amber. Default for new markers.
- `acknowledged` — operator has explicitly opened the marker, read the counsel, and accepted. UI shows muted "you've seen this." Cannot disappear.
- `resolved` — the underlying state has been reversed (port closed, encryption added, finding remediated). Marker auto-resolves on the next audit pass that confirms remediation. UI shows greyed out with resolution date.

**Severity threshold (operator-defined, 2026-04-26):**

| Marker | Definition |
|---|---|
| **RED** | Things any networking curriculum (CCNA / Network+ / CIS / NIST) categorically calls a security or operational mistake for any home-network deployment. Examples: open Wi-Fi on primary SSID, default admin credentials, management interfaces exposed to WAN, dangerous port forwards (SMB/RDP/Telnet/FTP/databases/SNMP/Memcached/Redis), WEP or WPA-TKIP encryption, firewall disabled, "any → any → allow" rules, cameras directly reachable from WAN. |
| **AMBER** | Things canonical sources advise against but with legitimate operator use cases: HTTP(S) port forwards for self-hosted services, gaming console NAT ports, Plex/Jellyfin/Emby ports, IoT-on-trusted-VLAN for minimal-IoT households, single flat /24 for very small networks, hidden SSIDs, WPA2-Personal vs WPA3, auto-firmware-updates disabled. |
| **(no marker)** | Informational concerns: suboptimal channel selection, AP placement gaps, legacy 802.11n device pulling down 5GHz radios, mixed-vendor switching. Mentioned once in conversation, not tracked as cautions. |

Corporate-networking patterns (dual-homed switches, MLAG, HSRP/VRRP, BGP/OSPF, 802.1X, RADIUS for Wi-Fi, SIEM aggregation) are explicitly **not** in scope for marker classification. This is a home-network agent.

### External corpus (Layer 0) — agent's source of authority

Bundled with the package at install-time. Two tiers of source material:

**Vendor documentation** for any device class an operator is likely to own:
- Networking: Ubiquiti UniFi, Cisco, HP/Aruba, Meraki, MikroTik
- Smart home: Lutron Caseta, Philips Hue, Sonos, Crestron, Control4
- Platforms: Apple HomeKit, Amazon Alexa, Google Home, SmartThings
- Security cameras: UniFi Protect, Reolink, Arlo, Wyze
- ISP / modem manufacturers as relevant

**Official networking curriculum:**
- Cisco CCNA + CCNP routing/switching/security curriculum (home-relevant subset)
- CompTIA Network+ and Security+ knowledge bases
- IETF RFC standards (relevant subset — TCP, UDP, IP, DNS, DHCP, etc.)
- IEEE specifications: 802.11 (Wi-Fi), 802.1Q (VLANs), 802.3 (Ethernet), 802.1X (port auth)
- NIST: Cybersecurity Framework, SP 800-53, SP 800-171, SP 800-115, SP 800-207
- CIS Controls v8 + CIS Benchmarks for relevant device categories

**Explicitly NOT in corpus:**
- Blog posts, forums, Reddit, Stack Exchange (useful for diagnostics, not authoritative)
- Marketing material from vendors
- YouTube / video content
- Influencer / opinion publications

The agent's authority comes from naming a specific source. *"Per IEEE 802.11-2020 §11.4.4..."* / *"Per the UniFi Network Security Best Practices Guide..."* / *"Per NIST 800-53 SC-7 'Boundary Protection'..."* If the agent cannot cite a specific corpus source, it does not get to invoke counsel-against — it can express concern but cannot trigger a caution marker.

### Interrogation

The Conductor has a `query_history` tool. Operator can ask:

- *"Why did we set up the DMZ?"* → returns origin story for the DMZ network
- *"When did we last change a channel?"* → searches architecture history for `set_ap_channel_*` events
- *"What did I tell you about my work-from-home pattern?"* → searches profile history + session digests
- *"Show me everything we've ever flagged about this Hue bridge"* → searches findings history filtered by MAC

The tool returns timestamped entries; the Conductor synthesizes a natural-language summary from them.

### What goes where (the promotion rules)

```
operator types something               → Tier 1 (working)
Conductor extracts a fact              → Tier 3 (durable, with confidence + evidence)
Conductor calls a tool                 → Tier 2 (session, for back-reference)
Conductor makes a non-trivial decision → Tier 3 (decision log)
Tool applies a network change          → Tier 3 (architecture log)
Tool emits a finding                   → Tier 3 (findings log)
Session ends                           → Tier 1+2 collapse to Tier 3 digest
```

---

## 4. The loop primitive

```python
# tools/agent_loop.py — written once, used by the Conductor

def run_conductor(
    *,
    durable_memory: DurableMemory,
    session_state: SessionState,
    working_memory: RollingWindow,
    tools: dict[str, ToolSpec],
    llm: AIRuntime,
    on_user_input: Callable[[str], str],   # gets next operator turn
    on_say: Callable[[str], None],         # says something to operator
) -> None:
    on_say(opening_greeting(durable_memory, session_state))
    while True:
        decision = llm.decide(
            system=CONDUCTOR_SYSTEM_PROMPT,
            working_memory=working_memory.recent(),
            session_summary=session_state.summary(),
            durable_subset=durable_memory.relevant_to(working_memory.recent()),
            tools=tools,
        )
        if decision.kind == "speak":
            on_say(decision.text)
            working_memory.add_assistant(decision.text)
        elif decision.kind == "ask":
            on_say(decision.question)
            answer = on_user_input("> ")
            working_memory.add_user(answer)
        elif decision.kind == "call_tool":
            result = tools[decision.tool].fn(**decision.args)
            session_state.record_tool_call(decision.tool, decision.args, result)
        elif decision.kind == "save_fact":
            durable_memory.upsert_fact(
                field=decision.field,
                value=decision.value,
                confidence=decision.confidence,
                evidence=decision.evidence,
                source=decision.source_turn_id,
            )
        elif decision.kind == "log_decision":
            durable_memory.decision_log.append(decision.entry)
        elif decision.kind == "done_for_now":
            session_state.checkpoint()
            return
```

That is the entire agent. ~100 lines. Everything else is tools.

---

## 5. Tool inventory (proposed)

| Tool | Determinism | Wraps |
|---|---|---|
| `read_snapshot` | deterministic | `UnifiClient.snapshot()` |
| `count_devices_by_role` | deterministic | snapshot projection |
| `lookup_oui_vendor` | deterministic | OUI database |
| `identify_smart_home_brands` | deterministic | OUI scan over clients |
| `derive_isp_from_wan` | deterministic | WHOIS lookup on WAN IP |
| `audit_network` | deterministic | the existing auditor checks |
| `propose_change` | deterministic | builds an `ApprovedAction` candidate (does NOT execute) |
| `apply_approved_change` | deterministic + permission gate | the existing change executor — requires `ApprovedAction` |
| `monitor_status` | deterministic | snapshot of current monitor events |
| `query_history` | deterministic | searches durable memory |
| `propose_segmentation` | service call | wraps the security planner sub-agent |
| `analyze_security_posture` | service call | wraps `AIRuntime` |
| `review_change` | service call | wraps `AIRuntime` |
| `save_household_profile_field` | deterministic | writes to `household_profile.yaml` with confidence/evidence |
| `save_registry_entry` | deterministic | writes to `device_register.yaml` / `client_register.yaml` |
| `save_origin_story` | deterministic | writes to `origin_stories.yaml` |
| `save_dismissal` | deterministic | writes to `dismissals.yaml` |
| `ask_operator_to_approve` | deterministic | presents an `ApprovedAction` proposal, captures yes/no, writes to durable memory |
| `evaluate_against_corpus` | service call | given (action, current_state, household_profile), consults `data/corpus/`, returns severity + canonical citation + counsel text |
| `cite_corpus` | deterministic | retrieves a specific corpus excerpt by source ID for the Conductor to quote when explaining counsel |
| `record_caution_marker` | deterministic | writes a `CautionMarker` to `runs/caution_markers.jsonl` (origin=operator_override after override) |
| `record_audit_caution` | deterministic | writes a `CautionMarker` (origin=audit_finding) when an auditor finding warrants persistent visibility |
| `list_cautions` | deterministic | returns active + acknowledged markers; CLI surface for "show me my caution lights" |
| `acknowledge_caution` | deterministic | operator-initiated state transition active → acknowledged |
| `recheck_caution_resolution` | deterministic | run on each audit pass; transitions acknowledged → resolved when underlying state has been remediated |

Notes:

- The Conductor cannot call `apply_approved_change` without an `ApprovedAction` artifact. The flow is always: Conductor proposes → operator approves (`ask_operator_to_approve`) → Conductor calls `apply_approved_change` with the resulting authorization. No autonomous writes.
- `propose_segmentation` is a sub-agent because VLAN topology requires multi-step reasoning over the snapshot, household profile, and origin stories. It runs the same loop primitive but with a tighter goal and a smaller toolset, and returns a recommendation to the Conductor.
- All save tools write *with* confidence and evidence. The HouseholdProfile becomes time-indexed (`profile_history.yaml`), not overwritten.

---

## 6. Bootstrap conversation (first-run, nothing configured)

```
$ nye chat

Conductor:  Hi. Looks like I haven't been here before — no operator profile,
            no UniFi connection. Two options before we go any further:

              1. You have a UniFi network and I'll connect to it.
              2. You're designing one from scratch — we'll plan the
                 topology together and capture it as a target profile.

            Which one?

Operator:   I have a UniFi network but I haven't given you any keys yet.

Conductor:  Got it. Two things I need:

              - the host or IP of your UniFi controller (often something
                like 192.168.1.1 or 192.168.69.1 — your gateway)
              - an API key from the controller

            For the key: open https://<your-controller>/network/default/settings/control-plane/admins-and-users → click your admin → API → Create API Key. Give it a name like "ClaudeMyNetworkEngineer" so you can revoke it later.

            Want me to wait while you grab those, or paste them now?

Operator:   192.168.69.1 and the key is ABC...

Conductor:  Saving those to your local .env (gitignored — never leaves your
            machine). Trying to connect... [calls test_connection tool]

            Reachable. I see 4 access points, a UDM gateway, 3 switches,
            36 active clients, and 5 Protect cameras. Let me look around
            for a moment. [calls read_snapshot, audit_network, identify_smart_home_brands]

            Quick read of what I see:

              - Trust-tier devices: Macs, iPhones, iPads
              - IoT brands present: Lutron, Hue, Sonos, Apple HomeKit,
                Amazon Alexa
              - Cameras: 4 online, 1 offline (G4 Pro — is that intentional?)
              - One DMZ network — non-default, probably has a story behind it
              - Three port forwards open to WAN — we should look at those

            Does that match your mental model? And: what brought you to
            this project — what are you trying to fix or improve?

Operator:   The G4 is intentionally off, the DMZ is for the solar installer's
            zigbee box. I'm mostly worried about IoT being on the same VLAN
            as my work laptop, and overall network speed feels slow.

Conductor:  [save_dismissal: G4 Pro intentionally offline]
            [save_origin_story: DMZ exists for solar installer's zigbee — do_not_touch]
            [save_household_profile_field: security.iot_isolation_appetite ≈ paranoid]
            [save_household_profile_field: origin.biggest_frustration ≈ slow throughput]

            Filed all of that. The IoT-on-trusted-VLAN concern is real and
            actionable — I can sketch a segmentation strategy when you're
            ready. The throughput question is a different shape — could be
            channel selection, AP placement, ISP delivery, or device-side.
            Want to dig into one of those now, or finish the
            getting-to-know-you conversation first?
```

This is a real conversation. No 94-question form. No "⚠ not in suggested choices" warnings. The operator says things naturally; the Conductor extracts multiple structured facts per turn, reads back its understanding, and asks intelligent follow-ups.

---

## 7. Steady-state conversation (returning operator)

```
$ nye chat

Conductor:  Welcome back. Last time we talked about IoT isolation and you
            wanted me to propose a segmentation strategy. I haven't done
            that yet — should we?

            Also: there's been one new finding since you were last here —
            an AP went offline yesterday at 14:32 and came back at 14:34.
            Probably a power blip. Worth noting?

Operator:   Yes do the segmentation thing. Skip the AP blip.

Conductor:  [calls propose_segmentation sub-agent]
            ...
```

The Conductor opens by reading from durable memory: what was pending, what's new since last session, what the operator was working on. It does not re-introduce itself or re-ask anything.

---

## 8. Trust boundaries (what the Conductor can and cannot do)

| Action | Who decides | Who executes |
|---|---|---|
| Read network state | Conductor (autonomous) | snapshot tool |
| Run a deterministic check | Conductor (autonomous) | auditor tool |
| Generate a finding | auditor tool | tool returns to Conductor |
| Save a fact to operator config | Conductor (autonomous, with confidence + evidence) | save tools |
| Consult external corpus | Conductor (autonomous) | `evaluate_against_corpus` |
| Counsel against an action | Conductor (autonomous, only when corpus citation is available) | `cite_corpus` + conversational turn |
| Propose a network change | Conductor (autonomous) | builds candidate ApprovedAction |
| Approve a change | Operator (always) | `ask_operator_to_approve` tool |
| Override counsel-against | Operator (explicit) | recorded via `record_caution_marker` (origin=operator_override) |
| Execute an approved change | Conductor (with ApprovedAction) | `apply_approved_change` tool |
| Acknowledge a caution marker | Operator only | `acknowledge_caution` tool |
| Resolve a caution marker | System on next audit pass when remediation confirmed | `recheck_caution_resolution` |
| Extinguish a caution marker | NEVER | markers persist until resolved or operator-acknowledged (and acknowledged still shows in UI) |
| Execute an unapproved change | NEVER | n/a |
| Bypass permission tier | NEVER | n/a |
| Write to `.env` or credentials | only with explicit operator turn | save tool |
| Counsel without a corpus citation | NEVER | the agent can express concern but cannot invoke the counsel-against mechanism |

The HandoffEnvelope contract still applies to anything crossing a trust boundary. The directive set's I1–I5 invariants still apply to anything the Conductor delegates to a sub-agent or service. The corpus-citation requirement (`Counsel without a corpus citation`) is an additional structural invariant: it ensures the agent's authority comes from cited sources rather than training-weight opinions.

---

## 9. Migration path

1. **Adopt this document** — once you've read it and pushed back where you disagree, it becomes `docs/agent_architecture.md` for real (currently a draft).
2. **Rename the deterministic modules.** `agents/auditor.py` → `tools/auditor.py`, etc. Backward-compat shim for one release. The honest naming change matters for contributors.
3. **Build `tools/agent_loop.py`** — the loop primitive. Tested standalone.
4. **Build `tools/durable_memory.py`** — the Tier 3 reader/writer with the `query_history` tool.
5. **Build `agents/conductor.py`** — uses the loop primitive + memory + tool inventory. The first real agent.
6. **Make bare `nye` drop into the Conductor REPL.** The default invocation with no subcommand becomes the conversational entry point. Existing subcommands (`nye onboard`, `nye registry walkthrough`, `nye audit`, `nye optimize`, `nye security propose-vlans`, etc.) become legacy aliases — they invoke the Conductor with a specific opening prompt rather than running the old form-runner code paths.
7. **Deprecate the form-runner code paths.** The probe-driven `_cmd_onboard` and the per-device `walkthrough` in `registry_agent.py` get removed once the Conductor handles those flows. Subcommand aliases survive as ergonomic shortcuts, but the underlying behavior is the Conductor with a tailored opening prompt.
8. **Sub-agents come later.** `propose_segmentation` becomes the second agent (smaller goal, smaller toolset). Other sub-agents only as needed.
9. **Re-scope the directive set.** Most P0 BLOCKERs assumed 10 agents handing off. They apply to 1 agent + 1 sub-agent + many tools, which is a thinner contract. See §11.

---

## 10. What this design does NOT solve yet

Explicitly listed so we don't pretend otherwise:

- **Concurrency.** One Conductor process at a time. If the operator opens two `nye chat` sessions simultaneously, behavior is undefined. Lock file or session multiplexer is future work.
- **Long absences.** If the operator hasn't run `nye chat` for 6 months, the snapshot is stale. The Conductor needs stale-context detection (directive 2.5 / task #28) before claiming durable memory is current.
- **Hostile prompts in durable memory.** Operator-supplied YAML already goes through `tools/prompt_safety.py` (directive 1.3 / task #46). Decision log + architecture log entries are LLM-written — they need the same sanitization on save AND on retrieval, or a hostile reflected payload could land back in a future LLM call. Open question.
- **Multi-network.** The Conductor today assumes one UniFi controller. Operators with multi-site deployments need either one Conductor per site or a site-switching tool. Defer.
- **iOS / web client.** The Conductor's loop primitive is shape-agnostic, but the I/O wrapper for non-CLI clients is Phase 10 server work. The CLI implementation is what we build first.

---

## 11. Implications for the directive set

The directives in `DIRECTIVES.md` were written assuming ~10 agents needing inter-agent contracts. With one Conductor + sub-agents, most of that work shrinks.

Re-scoped:

- **1.1, 1.2, 1.3, 1.4, 1.5, 1.6** — apply unchanged. Permission boundary, SSL hardening, prompt-injection, dismissals TTL, path traversal, log redaction. All still real.
- **2.1 cascade instrumentation** — applies to the Conductor → sub-agent → tool path, not to 10 agents. Smaller surface, simpler contract.
- **2.2 I6 confidence-basis validator** — applies to the field-saver tool's confidence + evidence semantics. Still real, smaller scope.
- **2.3 layer-independence matrix** — applies. Fewer signals to triangulate.
- **2.4 profile-aware threshold builder** — becomes a tool the Conductor calls. Still real.
- **2.5 damping class doc** — applies to tools, not agents. Still real.
- **2.6 split UnifiClient** — applies. Independent.
- **2.7 cross-YAML config validator** — applies. Now also validates conductor-written YAML.
- **2.8 async boundary** — applies for Phase 10 server. Independent.
- **3.1 Stab harness** — re-scoped. Was about agent classification reliability across context levels. The Conductor's classification *is* the project's classification surface; one harness rather than five.
- **3.2 Drift quantification** — applies to the change-executor tool (drift = blast radius if a change goes wrong). Tool-level concern.
- **3.3 ρ measurement** — measures cascade error correlation. With one agent, "cascade" largely becomes "agent → sub-agent → tool"; measurement still meaningful, scope smaller.
- **3.4 Nyquist cadence** — applies to the monitor tool's polling interval. Tool concern.
- **3.5 Grey-region registry** — applies to the Conductor's "I don't know — should I ask the operator?" decisions.
- **3.6 Layer 0 resonance audit + corpus retrieval** — **promoted to immediate next-build prerequisite**. The Conductor's authority to counsel-against depends on naming a specific corpus source (per §8 trust boundary). Without Layer 0 the agent has no canonical citations and cannot invoke the caution-marker mechanism — it would be an opinionated chatbot rather than an advisor. Acceptance criteria for 3.6 now include the corpus-curation list defined in §3.5: vendor docs (UniFi/Cisco/Aruba/Lutron/Hue/Sonos/Crestron/Apple/Amazon/Google/SmartThings/Reolink/etc.) + official curriculum (CCNA/CCNP/Network+/Security+/IETF RFCs/IEEE/NIST/CIS). Bundled at install-time, not download-on-first-run.
- **3.7 30-day supervised dry-run** — applies and is even more important. The operator approves every Conductor-proposed change for 30 days; we measure the false-positive rate, the operator-override rate, and (new metric) the caution-marker accuracy: how many RED markers were operator-resolved vs. how many became long-lived "this was actually fine" cases.

---

## 12. Open questions

### Decided

1. ~~**Tool naming**~~ — *still open*, see below.
2. ~~**Single Conductor or per-flow Conductors?**~~ **DECIDED 2026-04-26**: single Conductor + sub-agents for narrow specialized tasks. Sub-agents (e.g. `propose_segmentation`) get focused goals and narrow toolsets but are invoked from the single operator-facing Conductor — operator only ever sees one continuous conversation. Tonal differentiation across modes (warm-and-curious in onboarding, terse-and-precise in approval) is handled inside the single Conductor's system prompt as a state variable, not as separate agent classes.
3. ~~**Hostile-content sanitization on durable memory retrieval**~~ **DECIDED 2026-04-26**: four layers + provenance tagging + caution markers + bundled corpus.
   - Layer 1: sanitize at write time (hygiene only — Cf/bidi rejection, oversize caps; the operator hacking themselves is not the threat).
   - Layer 2: wrap durable memory in untrusted-data tags on retrieval — `<operator_quote>`, `<conductor_rendered>`, `<tool_output>`, `<external_corpus>`.
   - Layer 3: provenance tagging on every entry; default-on-failure is `operator` (most-suspicious posture).
   - Layer 4: size caps + NFKC normalization + Cf-stripping at write time.
   - **Plus** counsel-against mechanism (§3.4 caution markers, §8 trust boundaries) that supersedes prior triple-warn-with-30-day-re-counsel design. Markers persist visually in UI; agent does not re-counsel.
   - **Plus** bundled external corpus (§3.5) so the agent has cited authority for counsel from install-time-zero.
4. ~~**Markers on findings vs. on actions**~~ **DECIDED 2026-04-26**: distinguish via `origin` field on `CautionMarker`. `operator_override` for "you asked me to do this, I counseled against, you confirmed, here's the record." `audit_finding` for "I noticed this and I think you should do something about it." Same UI surface, different conversational language.
5. ~~**Layer 0 corpus delivery**~~ **DECIDED 2026-04-26**: bundled at install-time. Operators have agent expertise from second one of installation; no DNS-resolution dependency at first run.
6. ~~**Severity threshold for RED vs AMBER**~~ **DECIDED 2026-04-26**: RED = anything CCNA/Network+/CIS/NIST categorically calls a security/operational mistake for any home-network deployment (excluding corporate-networking patterns like switch redundancy). AMBER = frowned upon but legitimate operator use cases exist. See §3.4 for full classification list.

### Still open

*(none — all four resolved 2026-04-26)*

### Also decided 2026-04-26

7. ~~**Tool naming**~~ **DECIDED**: `tools/auditor.py` (and `tools/optimizer.py`, `tools/monitor.py`, etc. — preserve existing noun-form module names). Reasons: preserves existing module names so the rename is a `git mv` rather than a code rewrite; the noun form reads naturally at call sites (`tools.auditor.run(...)`); lower migration churn for downstream imports.

8. ~~**CLI shape**~~ **DECIDED**: bare `nye` drops into the Conductor. Existing subcommands (`nye onboard`, `nye registry walkthrough`, `nye audit`, `nye optimize`, `nye security propose-vlans`, `nye ai analyze`, etc.) become legacy aliases that map to the Conductor with a specific opening prompt — useful as shortcuts for power users but no longer the primary path. The "talk to my agent" experience is `nye` with no arguments.

9. ~~**History interrogation depth**~~ **DECIDED**: configurable. Default `query_history(days_back=None)` returns all relevant matches across all time. Operator can scope inline ("just the last 30 days") and the Conductor passes that through as `days_back=30`. Power-user CLI flag also available. History is small (text logs); the cost of all-time default is negligible and the value of unrestricted recall is real ("why did we set up the DMZ?" should find the answer regardless of when the origin story was captured).

10. ~~**Session digest writer**~~ **DECIDED**: hybrid — deterministic structured facts + LLM-generated narrative summary. The structured part captures the audit trail (which tools were called, what was saved, what state changed) and is reproducible. The narrative paragraph is LLM-generated and gives the *story* of what happened in the session ("operator was concerned about IoT isolation; we walked the heritage artifacts and captured DMZ origin story; identified two AMBER markers around port forwards"). Both go into the digest. The narrative gets wrapped in `<conductor_rendered>` provenance tags on retrieval per §3 rules. The structured facts get `<tool_output>` tags. This way the next session reads the rich narrative for context but the model knows it's reading a Conductor-rendered summary, not authoritative directives.
