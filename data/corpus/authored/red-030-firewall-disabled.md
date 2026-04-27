---
source_id: red-030-firewall-disabled
title: Stateful firewall disabled on the gateway
severity_band: RED
related_caution_codes:
  - "FIREWALL_DISABLED"
  - "FIREWALL_ANY_ANY_ALLOW"
sources_cited:
  - "NIST SP 800-53 Rev 5, SC-7 'Boundary Protection' (referenced)"
  - "NIST SP 800-41 Rev 1 'Guidelines on Firewalls and Firewall Policy' (referenced)"
  - "CIS Controls v8, Control 12.4 'Establish and Maintain Architecture Diagram(s)' (referenced)"
license: MIT
url: null
last_updated: "2026-04-27"
---

## Why this is RED

The stateful firewall on the home gateway is the single load-bearing security control between the household network and the public internet. Without it (or with an effectively-disabled "any → any → allow" rule that has the same effect), every device on the LAN is directly addressable from the WAN by anyone who knows or guesses the public IP. The default UniFi posture — stateful firewall enabled, deny-by-default on WAN-to-LAN — is what makes a home network safe by default.

A firewall in this state is not a misconfiguration to debug; it's a categorical security failure. CCNA-level networking education and every modern security framework treats this as never-acceptable.

## What canonical sources say

**NIST SP 800-53 Rev 5, SC-7 "Boundary Protection"**: information systems must monitor and control communications at external boundaries. The home gateway IS the external boundary; disabling its enforcement removes the control.

**NIST SP 800-41 Rev 1 "Guidelines on Firewalls and Firewall Policy"**: the entire publication is dedicated to this control's design and operation. Disabled is not a position the document considers.

**CIS Controls v8, Control 12**: explicitly requires firewall-class boundary protection.

## How a home firewall ends up disabled

A handful of patterns:

- **Operator was debugging connectivity** ("nothing works, disable the firewall, check if THAT's the problem") and never re-enabled. This is the most common cause.
- **Operator was setting up a service that needed unusual inbound** and disabled the firewall as a shortcut instead of writing the specific rule.
- **A well-meaning rule like "allow inbound from my friend's IP for gaming" that became "allow inbound any" via a typo or copy-paste accident.**
- **An ISP combo unit in passthrough where the upstream firewall was assumed but isn't actually filtering.**
- **A development environment that never got tightened up.**

In every case, the resolution is to re-enable the default stateful firewall and explicitly write the specific allow-rules the operator's services need.

## Standard alternative

The default UniFi gateway firewall is the right baseline:

- WAN → LAN: deny by default (only stateful return traffic to LAN-initiated connections is allowed)
- LAN → WAN: allow (with optional content-filtering)
- Specific services that need inbound: explicit port forwards with narrow source rules where possible

Specific services that need inbound are documented per-port, per-source. UniFi's Threat Management features (IDS/IPS) are layered on top of the firewall, not in place of it.

## When the operator may legitimately override

There is no legitimate long-term override. The Conductor does not record an operator_override RED marker for "operator chose to disable the firewall" — this isn't a contextual judgment call. The operator's agency stops short of decisions that have categorical answers; the Conductor's response is to refuse to participate in the misconfiguration and offer to walk through proper rule construction instead.

If the operator's underlying concern is a specific service that needs inbound, the Conductor's job is the rule-construction walkthrough, not the firewall-disable acquiescence.

## How the Conductor uses this

When the auditor flags a disabled firewall or a wide-open allow rule:

1. Cite this summary explicitly.
2. Refuse to apply changes that would worsen the state (no AUTO-tier action could disable the firewall even if asked; the permission_model already covers this).
3. Offer to walk the operator through reconstructing the proper firewall posture.
4. Record an AMBER audit_finding marker (not RED — this is the auditor flagging something for the operator's attention, not the operator overriding counsel). Once the operator confirms the situation and either fixes it or knowingly leaves it, the Conductor escalates the marker to RED if the situation persists past one session.
