# Corpus Curation Plan

> **Status:** Draft — planning document, requires sign-off before any corpus content is bundled.
> **Purpose:** decide what goes into `data/corpus/`, how it's licensed for redistribution, and how the Conductor cites from it.

This document is the precondition for migration task #51 (Layer 0 retrieval). The architecture (`docs/agent_architecture.md` §3.5) requires the agent's authority to come from cited canonical sources. This doc decides which sources, in what form, with what license posture.

The hard constraint that shaped this plan: **most vendor documentation and networking curriculum the architecture doc named (Cisco docs, CCNA/CCNP curriculum, vendor user guides) is proprietary and not freely redistributable.** A naive "bundle everything" approach would create license violations the moment the package ships publicly. This plan addresses that constraint head-on.

---

## 1. License posture — what we can and cannot bundle

### 1.1 Freely redistributable (bundle in full)

| Source | License | Bundling stance |
|---|---|---|
| **IETF RFCs** | BCP 78 / RFC 5378 — text is freely usable, modifiable, redistributable | Bundle full text |
| **NIST publications** | US Government work, public domain (17 U.S.C. § 105) | Bundle full text |
| **CIS Controls v8** + selected Benchmarks | Creative Commons Attribution-NonCommercial-ShareAlike 4.0 (CC BY-NC-SA 4.0) | Bundle full text with attribution; **NonCommercial clause requires this project's distribution to remain non-commercial** — flagged as a constraint to keep an eye on |
| **Authored summaries** (this project) | MIT (the project license) | Bundle freely; cite proprietary sources by name + URL where the summary distills their guidance |

### 1.2 Reference-only (cite, do not bundle)

| Source | License | Citation strategy |
|---|---|---|
| **Ubiquiti UniFi documentation** (help.ui.com) | Proprietary; redistribution unclear | Cite by URL with version pin. Authored summaries quote sparingly under fair-use for commentary/criticism. |
| **Cisco IOS/NX-OS documentation** | Proprietary, cannot redistribute | Cite by URL only |
| **Cisco CCNA / CCNP curriculum** (NetAcad, Cisco Press) | Proprietary | Cite as a category ("per CCNA fundamentals") in authored summaries; do not quote |
| **CompTIA Network+ / Security+** | Proprietary | Same as Cisco curriculum |
| **IEEE 802 standards** (802.11, 802.1Q, 802.3, 802.1X) | Free download via IEEE GET Program (6 months post-publication, personal use) but **redistribution prohibited** | Cite by section number + URL; authored summaries paraphrase |
| **Vendor docs** — Lutron, Philips Hue, Sonos, Crestron, Apple HomeKit, Amazon Alexa, Google Home, SmartThings, Reolink | Proprietary | Cite by URL only; authored summaries cover canonical security guidance |

### 1.3 Operator-supplied (out of scope for v1)

A future enhancement: operator drops vendor PDFs they have legitimately acquired into a local `~/.config/network-engineer/local-corpus/` directory (gitignored, never uploaded). The agent indexes their personal copies for their own use. Out of scope for the initial corpus build.

---

## 2. The bundled corpus — concrete v1 list

### 2.1 NIST (full-text bundle)

- **NIST CSF 2.0** — Cybersecurity Framework. The canonical home/SMB cybersecurity reference.
- **NIST SP 800-53 Rev 5** — Security and Privacy Controls. Source of *"per SC-7 boundary protection"* citations.
- **NIST SP 800-171 Rev 3** — Controlled Unclassified Information protection. Less directly home-relevant but referenced for some controls.
- **NIST SP 800-115** — Technical Guide to Information Security Testing.
- **NIST SP 800-207** — Zero Trust Architecture. Key reference for IoT segmentation guidance.
- **NIST SP 800-46 Rev 2** — Enterprise Telework Security. Referenced for WFH-related caution rules.
- **NISTIR 8259** — IoT Device Cybersecurity Capability Baseline. Direct relevance to home IoT.

Total: ~7 documents, ~4-8 MB compressed (markdown).

### 2.2 IETF RFCs (full-text bundle, selected)

The "agent needs to know" subset:

- **RFC 1918** — Address Allocation for Private Internets (RFC1918 ranges)
- **RFC 5737** — IPv4 Address Blocks Reserved for Documentation
- **RFC 6598** — IANA-Reserved IPv4 Prefix for Shared Address Space
- **RFC 3849** — IPv6 Address Prefix Reserved for Documentation
- **RFC 4291** — IP Version 6 Addressing Architecture
- **RFC 1122** — Requirements for Internet Hosts
- **RFC 6762** — Multicast DNS (mDNS — load-bearing for the cross-VLAN reflector caution)
- **RFC 6763** — DNS-Based Service Discovery
- **RFC 7858** — Specification for DNS over Transport Layer Security (DoT)
- **RFC 8484** — DNS Queries over HTTPS (DoH)
- **RFC 4787** — NAT Behavioral Requirements for Unicast UDP
- **RFC 6886** — NAT Port Mapping Protocol (NAT-PMP)
- **RFC 6970** — UPnP IGD
- **RFC 1305 / RFC 5905** — Network Time Protocol
- **RFC 7159 / 8259** — JSON (because operators paste config snippets)
- **RFC 791 / 8200** — IPv4 / IPv6 base specifications

Total: ~16 RFCs, ~3-5 MB compressed (text).

### 2.3 CIS Controls v8 + selected Benchmarks

- **CIS Controls v8** — full document with all 18 control families.
- **CIS Benchmark — Cisco IOS** (relevant subset for switch-config commentary).
- **CIS Benchmark — generic Linux** (covers UDM Linux base where applicable).

License flag (§1.1): CIS BY-NC-SA 4.0 requires non-commercial use. The project is currently MIT-licensed open-source, intended for personal use. If any future fork or derivative becomes commercial, the CIS content must be removed from that fork. **Document this constraint in the project README.**

Total: ~3 documents, ~3-6 MB compressed.

### 2.4 Authored summaries (this project's original content)

Original content the project owns and bundles freely. Each summary is markdown with frontmatter (title, last_updated, severity_band, related_caution_codes, sources_cited).

Initial set, one per RED/AMBER classification case from architecture §3.4:

**RED tier authored summaries** (~25 documents, ~50 KB each):
- `red-001-open-wifi-primary-ssid.md`
- `red-002-default-admin-credentials.md`
- `red-003-management-interface-wan-exposed.md`
- `red-004-wps-enabled.md`
- `red-005-ssh-telnet-wan-exposed.md`
- `red-010-port-forward-smb.md`
- `red-011-port-forward-rdp.md`
- `red-012-port-forward-telnet.md`
- `red-013-port-forward-ftp.md`
- `red-014-port-forward-databases.md` (covers MySQL, PostgreSQL, MongoDB, Redis, MS-SQL)
- `red-015-port-forward-snmp.md`
- `red-016-port-forward-memcached.md`
- `red-020-wep-encryption.md`
- `red-021-wpa-tkip.md`
- `red-022-plaintext-dns.md`
- `red-023-http-only-management.md`
- `red-030-firewall-disabled.md`
- `red-031-any-any-allow.md`
- `red-032-flat-vlan-with-iot.md`
- `red-040-camera-wan-direct.md`
- `red-041-default-camera-credentials.md`

**AMBER tier authored summaries** (~15 documents, ~50 KB each):
- `amber-001-port-forward-http-https.md`
- `amber-002-port-forward-plex-jellyfin.md`
- `amber-003-port-forward-game-consoles.md`
- `amber-004-iot-on-trusted-vlan.md`
- `amber-005-single-flat-24.md`
- `amber-006-no-guest-network.md`
- `amber-007-hidden-ssid.md`
- `amber-008-wpa2-vs-wpa3.md`
- `amber-009-2-4ghz-only.md`
- `amber-010-auto-firmware-disabled.md`
- `amber-011-non-pi-hole-dns.md`
- `amber-012-no-ups.md`
- `amber-013-firmware-stability-stance.md`

**Foundational summaries** (~10 documents, framing material the agent cites):
- `foundation-001-home-vs-corporate.md` — "this is a home-network agent, not corporate"
- `foundation-002-segmentation-rationale.md` — when to VLAN, when not to
- `foundation-003-defense-in-depth.md` — layered approach
- `foundation-004-permission-tiers.md` — AUTO / REQUIRES_APPROVAL / NEVER mapping to canonical guidance
- `foundation-005-iot-pragmatism.md` — what "paranoid" looks like in practice
- `foundation-006-encryption-state-of-the-art.md` — current Wi-Fi encryption recommendations
- `foundation-007-mdns-cross-vlan.md` — the AirPlay/HomeKit/Sonos cross-VLAN problem
- `foundation-008-vpn-vs-port-forward.md` — why VPN beats port forwarding
- `foundation-009-firmware-updates.md` — the security/stability tradeoff
- `foundation-010-camera-isolation.md` — defense against firmware-stage exfil

Each authored summary has the structure:

```markdown
---
source_id: red-005-ssh-telnet-wan-exposed
title: SSH or Telnet exposed to WAN
last_updated: 2026-04-26
severity_band: RED
related_caution_codes: ["PORT_FORWARD_SSH_WAN", "PORT_FORWARD_TELNET_WAN"]
sources_cited:
  - "NIST SP 800-53 Rev 5 SC-7 (bundled)"
  - "CIS Controls v8 §11.2 (bundled)"
  - "UniFi Network Security Best Practices Guide v3.2 (referenced, https://...)"
license: MIT
---

## Why this is RED

[1-2 paragraph rationale, with citations to the bundled sources where exact quotes are available, and named references to proprietary sources.]

## What canonical sources say

- **NIST SP 800-53 SC-7**: [exact quote from bundled NIST]
- **CIS Controls v8 §11.2**: [exact quote from bundled CIS]
- **UniFi Hardening Guide**: [paraphrase, with URL]

## Standard alternative

[The recommended approach — VPN, etc.]

## When the operator may legitimately override

[Edge cases, with caveats. The agent uses this section to decide whether
to escalate counsel or accept a justified override.]
```

Total: ~50 authored summaries × ~50 KB each ≈ 2.5 MB.

### 2.5 Reference index (URL pointers, no content)

A single `data/corpus/references.yaml` with URL + version pins for proprietary sources the agent cites by reference. The Conductor uses this to construct citations like *"per the UniFi Network Security Best Practices Guide v3.2, section 4 (operator can verify at https://...)"*. The URL is shown in counsel; the operator can click through.

---

## 3. Storage layout

```
data/corpus/
├── README.md                     # license + attribution summary
├── manifest.json                 # full bundle index — what's here, what's referenced
├── nist/
│   ├── csf-2.0.md
│   ├── sp-800-53-rev5.md
│   ├── sp-800-171-rev3.md
│   └── ...
├── ietf/
│   ├── rfc-1918.md
│   ├── rfc-5737.md
│   └── ...
├── cis/
│   ├── controls-v8.md
│   ├── benchmark-cisco-ios.md
│   └── benchmark-linux.md
├── authored/
│   ├── red-005-ssh-telnet-wan-exposed.md
│   ├── amber-001-port-forward-http-https.md
│   ├── foundation-001-home-vs-corporate.md
│   └── ...
├── references.yaml               # URL pointers, no content
└── index/                        # search index built at install time
    ├── full_text.idx             # BM25 index over all bundled markdown
    └── citations.idx             # source_id → file_path mapping
```

Each markdown file has a frontmatter block. The `manifest.json` summarizes the whole bundle (counts, total size, license summary, version timestamps).

**Total bundle size estimate: ~15-25 MB compressed in the package.** Acceptable for a Python package.

---

## 4. Retrieval architecture

The Conductor's `evaluate_against_corpus` and `cite_corpus` tools (per architecture §5) need:

1. **Query → ranked candidates.** Given a free-text query like *"is opening port 22 to WAN safe?"*, return the top N corpus entries by relevance.
2. **Source-id → exact excerpt.** Given a `source_id` (e.g. `red-005-ssh-telnet-wan-exposed`), return the full content + frontmatter for the agent to quote.

**Implementation choice for v1: BM25 full-text search over the bundled markdown.**

- Simple, deterministic, no external dependencies (use `rank-bm25` or build trivially in-house)
- Corpus is small enough (~15-25 MB) that index fits in memory
- No embedding model required — keeps install footprint small
- Reproducible — same query produces same ranking

**Deferred to v2:** embedding-based retrieval. Useful when corpus grows or when query semantics matter more (e.g. operator says *"my old Apple Watch keeps disconnecting"* and we need to match RFC sections about 802.11 power-save). v2 work, not v1.

**`tools/corpus.py` API:**

```python
class CitationCandidate(BaseModel):
    source_id: str
    title: str
    severity_band: Literal["RED", "AMBER", "INFO"] | None  # None for foundational
    relevance_score: float
    excerpt_preview: str  # first ~300 chars

class CitationExcerpt(BaseModel):
    source_id: str
    title: str
    full_text: str
    frontmatter: dict
    file_path: str  # for transparency / debugging

def query(text: str, top_k: int = 5) -> list[CitationCandidate]: ...
def cite_by_id(source_id: str) -> CitationExcerpt: ...
def list_red_codes() -> list[str]: ...   # for the auditor's caution-code mapping
def list_amber_codes() -> list[str]: ...
```

---

## 5. Update cadence

The corpus is bundled per package release. Each release captures:

- A frozen snapshot of NIST/RFCs/CIS at the package's build time (sources don't change often — quarterly checks)
- Authored summaries updated as the project learns what operators encounter

`manifest.json` records the snapshot date. The Conductor surfaces the corpus age in conversation if it's >180 days old (*"my canonical sources are 6 months old; you may want a newer release"*) — this is the directive 2.5 stale-context rule applied to the agent's own knowledge base.

---

## 6. Acceptance criteria for migration task #51

The corpus build is complete when:

1. `data/corpus/` is populated with at minimum the v1 list above (NIST 7 docs + RFC 16 docs + CIS 3 docs + authored ~50 summaries + references.yaml).
2. `tools/corpus.py` implements `query()`, `cite_by_id()`, `list_red_codes()`, `list_amber_codes()`.
3. `tests/test_corpus.py` covers: query relevance, citation lookup, frontmatter parsing, severity-band filtering, manifest integrity (every authored summary's `sources_cited` references either a bundled source_id or a URL in `references.yaml`).
4. `data/corpus/README.md` documents what's bundled, what's referenced, license posture per source.
5. Project README adds a "Corpus and licensing" section noting the CIS BY-NC-SA constraint.
6. Leak detector clean — corpus content has no operator-data leakage paths.
7. Bundle size verified <30 MB compressed in the wheel.

---

## 7. What's deferred / not in v1

Explicitly listed so we don't pretend otherwise:

- **Embedding-based retrieval** — BM25 first; embeddings v2.
- **Operator-supplied local corpus** — `~/.config/network-engineer/local-corpus/` is a future enhancement.
- **Multi-language support** — corpus is English-only initially.
- **Live corpus refresh** — corpus is bundled per release; no online updates.
- **Vendor-specific deep coverage** — initial vendor coverage is via authored summaries that paraphrase canonical practices; operators wanting vendor-specific deep dives need to bring their own copies.
- **Cisco/CompTIA curriculum content** — referenced by category in authored summaries; not bundled.
- **IEEE 802 standards full text** — referenced; not bundled.

---

## 8. Sign-off questions for the operator

Before any corpus content lands, decide:

1. **CIS NonCommercial constraint** — do we accept it for v1? Implication: if the project ever takes a commercial path (paid SaaS hosting, etc.), the CIS portion needs removal. My lean: accept for v1, document the constraint, revisit if commercial path emerges.

2. **Authored summaries as primary citation source** — comfortable that the agent cites *our* authored summary that paraphrases the proprietary source, rather than the proprietary source directly? My lean: yes — this is honest, legally clean, and the operator can always verify against the URL.

3. **Bundle size budget** — ~15-25 MB compressed in the wheel. Acceptable, or do we need the corpus to be a separate downloadable artifact (e.g. `pip install network-engineer[corpus]`)? My lean: bundle in the main wheel for first-run guarantee. Operators with bandwidth concerns can be addressed in a future release with the optional-extra pattern.

4. **Authored summary count** — ~50 summaries for the initial RED/AMBER + foundational coverage. That's a substantial writing effort (probably 30-50 hours of focused work to write properly). Two options:
   - (a) **All 50 in one pass** before the Conductor lands. Slower start; more comprehensive day-one citations.
   - (b) **Top 15-20 highest-leverage cases** for v1; the rest as the project encounters real operator situations that warrant new summaries. Faster start; the corpus grows organically with operator demand.
   My lean: (b). The Conductor can fall back to citing NIST/RFC/CIS directly when an authored summary doesn't yet exist for a specific case.

5. **CCNA/CompTIA paraphrasing legality** — the authored summaries draw on canonical networking knowledge that ultimately comes from textbooks and courses. Where the summary distills "what every CCNA-trained engineer knows" without copying specific text, this is fact, not protected expression. I want explicit acknowledgment that this posture is what we're taking, and that we're prepared to revise if a specific summary draws too closely from a copyrighted source.

Once these five are answered, task #51 (corpus build) can begin.
