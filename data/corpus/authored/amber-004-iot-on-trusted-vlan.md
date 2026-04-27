---
source_id: amber-004-iot-on-trusted-vlan
title: IoT devices on the same VLAN as trusted endpoints
severity_band: AMBER
related_caution_codes:
  - "IOT_ON_TRUSTED_VLAN"
sources_cited:
  - "NIST SP 800-207 'Zero Trust Architecture' (referenced)"
  - "NISTIR 8259 IoT Device Cybersecurity Capability Baseline (referenced)"
  - "CIS Controls v8, Control 12 'Network Infrastructure Management' (referenced)"
license: MIT
url: null
last_updated: "2026-04-27"
---

## Why this is AMBER (and why it's not RED)

IoT devices — smart bulbs, smart speakers, robot vacuums, smart cameras, IoT bridges — are the largest category of unmaintained / poorly-patched devices in the modern home. They ship with proprietary firmware that often goes unsupported within 2-3 years of release. They make outbound connections to vendor cloud services that change without notice. They occasionally have RCE-class vulnerabilities that take months to patch.

Putting them on the same VLAN as trusted endpoints (laptops, phones, workstations, NAS) means a compromised IoT device has Layer-2 visibility to those trusted devices: it can ARP-spoof, do mDNS discovery, attempt internal lateral attacks. The blast radius of a single compromised IoT device is the entire trusted LAN.

That said: most home networks **are** flat /24s with IoT mixed in, and the operator hasn't been compromised. The realistic threat is bounded by the IoT-vendor population's willingness to compromise specific household devices, which is still rare. This is AMBER not RED because:

1. The architectural concern is real but the realized risk for any specific household is low.
2. The remediation (segmentation) requires real configuration effort and operator commitment.
3. Operators who have explicitly considered the trade-off and chose flat-VLAN-with-IoT are making a defensible call; the Conductor should respect that without nagging.

## What canonical sources say

**NIST SP 800-207 "Zero Trust Architecture"**: explicitly recommends micro-segmentation as a foundational control. The "perimeter" should not be the front door of the network — every device-to-device flow should be evaluated.

**NISTIR 8259 IoT Device Cybersecurity Capability Baseline**: identifies "the device should not have unrestricted local network access" as a baseline expectation that most consumer IoT fails to meet — implying the network must enforce the restriction the device lacks.

**CIS Controls v8, Control 12.4 "Establish and Maintain Architecture Diagram(s)"** and **12.6 "Use of Secure Network Management and Communication Protocols"**: support the segmentation principle without mandating a specific topology for residential use.

## Standard pattern: IoT VLAN + inter-VLAN firewall

The minimum viable segmentation:

```
Trust VLAN  → laptops, phones, workstations, NAS
IoT VLAN    → smart bulbs, speakers, vacuum, cameras (non-Protect)
Cameras VLAN → UniFi Protect cameras (separate, Protect controller blocks WAN)
Guest VLAN  → guest SSID
```

Inter-VLAN policy:
- Trust → all (initiator) — the operator's laptop can manage the IoT network
- IoT → Trust: drop (compromised IoT cannot pivot)
- IoT → Cameras: drop
- IoT → Guest: drop
- IoT → WAN: allow (so devices can reach vendor cloud)
- Cameras → WAN: drop (cameras stay on-prem)
- Guest → all internal: drop

The mDNS reflector caveat (per RFC 6762): cross-VLAN mDNS discovery is needed for AirPlay, HomeKit-over-IP, Sonos cross-room, Chromecast. UniFi has a built-in mDNS reflector toggle; enable it on the relevant VLANs.

## Why operators legitimately don't segment

- **Sonos / AirPlay / HomeKit operations are flaky across VLANs** — even with mDNS reflection. The operator decides one-network simplicity beats the marginal security improvement.
- **Family non-experts can't troubleshoot** — when a smart bulb stops working, the operator wants to debug at the device, not the firewall.
- **The IoT population is small** — three smart bulbs and a Sonos. The blast radius is small enough that segmentation is overengineering.
- **The operator has a paranoid IoT philosophy AND uses Apple HomeKit** which expects local-network multicast: the trade-off favors flat for them.

The Conductor respects all of these as legitimate. The marker stays AMBER (informational, not blocking), and the operator can acknowledge it.

## When the IoT count crosses the threshold

The Conductor escalates the urgency of segmentation discussion when:

- IoT count >15 (the realized blast radius is now meaningful)
- The household profile says `security.iot_isolation_appetite = paranoid` AND no segmentation exists (mismatch between stated philosophy and practice)
- A specific IoT device's vendor has had recent compromise reports

## How the Conductor uses this

When the auditor surfaces IoT-on-trusted-VLAN:

1. Cite this summary.
2. Surface the operator's stated `iot_isolation_appetite` from the profile if known.
3. Offer the segmentation walkthrough if the operator wants it. The walkthrough is multi-phase and gradual (per the existing security_agent's migration phases).
4. Record an AMBER audit_finding marker. Operator can acknowledge if they're choosing flat-network.
