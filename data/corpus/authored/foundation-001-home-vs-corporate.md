---
source_id: foundation-001-home-vs-corporate
title: This is a home-network agent, not a corporate-networking tool
severity_band: INFO
related_caution_codes: []
sources_cited:
  - "Cisco CCNA / CCNP curriculum (referenced — proprietary)"
  - "NIST SP 800-46 Rev 2 'Guide to Enterprise Telework, Remote Access, and BYOD Security'"
  - "CIS Controls v8 (referenced — CC BY-NC-SA 4.0)"
license: MIT
url: null
last_updated: "2026-04-27"
---

## Scope

ClaudeMyNetworkEngineer is built for one operator running one home network on UniFi gear. The agent's reasoning, recommendations, and caution markers are calibrated to that context.

## What this means in practice

Several patterns that are mandatory in corporate environments are explicitly **out of scope** for this agent's RED/AMBER classification:

- **Switch redundancy** — dual-homed switches, MLAG, stacking. Home networks rarely justify the added complexity; a single switch failure is a recoverable inconvenience, not a business outage.
- **Gateway redundancy protocols** — HSRP, VRRP, GLBP. Home gateways fail; the operator reboots them. Sub-second failover is corporate-class engineering with corporate-class operational cost.
- **Dynamic routing protocols** — BGP, OSPF, EIGRP, RIP. Home networks have one upstream gateway. Static defaults are the right answer.
- **802.1X port authentication** — RADIUS-backed wired-port auth. Useful when an operator has an MDM-managed device fleet; overkill for a household.
- **SIEM aggregation** — centralized log pipelines. The Conductor's runs/ and snapshots/ directories are sufficient for one-operator audit trails.

## Why we are explicit about this

A naive agent that flags the absence of HSRP as a high-severity finding produces noise the operator correctly ignores. Worse, that noise teaches the operator to dismiss findings categorically — which is exactly the failure mode that compromises real RED-tier flags later.

The Conductor's RED/AMBER classifications are tied to home-realistic threat models:
- Could a script kiddie / opportunistic scanner exploit this?
- Could a compromised IoT device pivot to higher-trust devices?
- Could a misconfigured port forward leak personal data?
- Could a misbehaving family member's device take down the network?

These are real concerns. The absence of corporate-class redundancy is not, for most homes.

## How the operator can override

If a specific operator has corporate-adjacent needs (small home business, video studio, server rack, etc.), the HouseholdProfile captures it (use_case, concerns) and the Conductor adjusts. The default is "this is a home agent"; the operator opts into stricter posture per-concern.
