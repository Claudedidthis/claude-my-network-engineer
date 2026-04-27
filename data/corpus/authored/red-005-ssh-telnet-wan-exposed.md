---
source_id: red-005-ssh-telnet-wan-exposed
title: SSH or Telnet exposed directly to WAN
severity_band: RED
related_caution_codes:
  - "PORT_FORWARD_SSH_WAN"
  - "PORT_FORWARD_TELNET_WAN"
  - "MGMT_INTERFACE_WAN_EXPOSED"
sources_cited:
  - "NIST SP 800-53 Rev 5, SC-7 'Boundary Protection' (referenced)"
  - "CIS Controls v8, Control 11 'Data Recovery' and Control 12 'Network Infrastructure Management' (referenced)"
  - "UniFi Network Security Best Practices (referenced — proprietary, see https://help.ui.com)"
license: MIT
url: null
last_updated: "2026-04-27"
---

## Why this is RED

Opening port 22 (SSH) or port 23 (Telnet) on the WAN interface — whether via port forward to an internal host, or by exposing the gateway's management interface itself — is one of the highest-volume attack surfaces on the public internet. Within minutes of exposure, automated scanners begin password-spray and known-vulnerability probes. CCNA-level networking education and every modern security framework treats this as categorical: don't.

Telnet is worse than SSH because credentials and session traffic are transmitted in plaintext. But even SSH on the public WAN is dangerous: SSH-on-22 to anything still reveals the existence of a host accepting administrative connections, allows username enumeration through error timing, and forces the operator to rely on password / key strength as the only line of defense against unbounded automated attack volume.

## What canonical sources say

**NIST SP 800-53 Rev 5, SC-7 "Boundary Protection"**: Information systems must monitor and control communications at external boundaries. Direct exposure of administrative protocols to the public internet violates the spirit of SC-7 — there is no boundary, the management interface IS the boundary.

**CIS Controls v8, Control 12.4 "Establish and Maintain Architecture Diagram(s)"** and **12.6 "Use of Secure Network Management and Communication Protocols"**: explicitly call for management traffic to flow over secure, authenticated, and isolated paths — not directly over the public internet.

**UniFi Network Security Best Practices** documents recommend remote access via the official UniFi Identity / WireGuard / L2TP VPN integrations rather than via direct port forwards.

## Standard alternative

The right answer is a VPN endpoint. Three home-grade options, in order of operator effort:

1. **Tailscale or similar mesh VPN** (lowest effort) — install on the device the operator wants to reach, install on the operator's phone/laptop, done. Works behind NAT, no port forward needed. Authentication via the operator's existing identity provider (Google / Microsoft / GitHub).

2. **WireGuard server on the UDM** (medium effort) — UniFi supports WireGuard server natively in modern firmware. One UDP port forward (for WireGuard's listening port) is required, but WireGuard responds only to authenticated peers — there is no service banner for opportunistic scanners to find.

3. **L2TP/IPsec VPN on the UDM** (highest compatibility, more setup) — built into UniFi for many years. Works with native iOS / macOS / Windows VPN clients.

Once the VPN is up, the operator SSHes (or uses any management protocol) over the VPN tunnel. The WAN side shows nothing exposed beyond the VPN endpoint itself.

## When the operator may legitimately override

Few legitimate cases for SSH-on-WAN in a home context:

- **Temporary, time-bounded access** for a remote diagnostic or vendor support session, where the operator commits to closing the port within hours. Even then, the alternative is "give the vendor a temporary VPN credential," which is almost always feasible.
- **A specific service requires SSH from a fixed external IP** AND the operator has narrowed the firewall rule to that single source IP. This isn't WAN exposure; it's SSH-from-one-IP. Still warrants an AMBER marker, not RED.
- **Honeypot research** — the operator deliberately wants the SSH service exposed to study attack patterns. This is unusual but legitimate; the marker should be acknowledged with operator_rationale stating the research intent.

In all override cases the marker stays as a RED active caution until the port is closed. The operator's stated reason is captured in the marker; future audits remind the operator the temporary thing is now permanent.

## How the Conductor uses this

When the auditor surfaces a port-forward finding for ports 22 or 23 on the WAN interface, the Conductor:

1. Cites this summary explicitly in the conversation.
2. Quotes the NIST SC-7 / CIS 12.4 references when the operator asks "but why?"
3. Offers the VPN alternative with a sentence each on Tailscale / WireGuard / L2TP-IPsec.
4. If the operator chooses to keep the port forward despite counsel, records a RED operator_override CautionMarker with `canonical_source = "NIST SP 800-53 SC-7"` and `operator_rationale` set to the operator's stated reason.
