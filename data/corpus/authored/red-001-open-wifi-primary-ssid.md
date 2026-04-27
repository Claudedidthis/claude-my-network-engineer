---
source_id: red-001-open-wifi-primary-ssid
title: Open Wi-Fi (no encryption) on a primary SSID
severity_band: RED
related_caution_codes:
  - "WIFI_NO_ENCRYPTION"
sources_cited:
  - "IEEE 802.11-2020 §11 (referenced)"
  - "NIST SP 800-53 Rev 5, SC-8 'Transmission Confidentiality and Integrity' (referenced)"
  - "Wi-Fi Alliance — WPA3 specification (referenced)"
license: MIT
url: null
last_updated: "2026-04-27"
---

## Why this is RED

A Wi-Fi SSID with no encryption broadcasts every device's traffic in plaintext to anyone within range. Passwords typed over HTTPS are still encrypted, but every DNS lookup, every metadata flow, every cookie that wasn't issued with `Secure` flag, every smart-device telemetry that happens to be plain HTTP — all of it is readable by any attacker with a directional antenna across the street.

This is categorical in every modern networking curriculum and security framework. Open Wi-Fi on a *primary* household SSID is never the right answer.

## What canonical sources say

**IEEE 802.11-2020 §11 "MAC Service Set Procedures"**: defines authentication and confidentiality services (WPA2 / WPA3) as the foundation of secure wireless operation. Open authentication exists in the spec for legacy and captive-portal use cases, not for primary network use.

**NIST SP 800-53 Rev 5, SC-8 "Transmission Confidentiality and Integrity"**: requires cryptographic protection of information in transmission. Open Wi-Fi violates this by design.

**Wi-Fi Alliance WPA3 specification**: WPA3-Personal (Simultaneous Authentication of Equals, SAE) is the current state of the art. WPA2-Personal remains acceptable. Open networks are not.

## Standard alternative

For a primary SSID: **WPA2-Personal at minimum, WPA3-Personal preferred**. Modern UniFi controllers support WPA3-Personal (sometimes labeled "WPA2/WPA3 Transition Mode") which falls back to WPA2 for older clients. Pre-shared key length 12+ characters, ideally a passphrase rather than a random string for memorability without sacrificing entropy.

For a guest network where the operator wants frictionless access: a **captive-portal SSID** with a voucher / one-click splash. UniFi calls this a "Hotspot" or "Guest Portal." The technical SSID is still encryption-less from the 802.11 standpoint, but the captive portal is the access control. The Conductor distinguishes captive-portal SSIDs (intentional, accept) from genuinely-open SSIDs (RED).

## The captive-portal case (AMBER, not RED)

When the operator legitimately runs an open SSID as a captive-portal guest network:

- The SSID name itself often signals the use ("Guest", "Visitors", "*-Guest", or a custom voucher-system name)
- The UniFi WLAN config has `is_guest: true` AND a hotspot/captive-portal configuration attached
- The expected flow is: guest connects → DHCP assigns IP → first HTTPS request is intercepted by captive portal → guest sees splash / accepts terms / enters voucher

In this case the agent should NOT flag WIFI_NO_ENCRYPTION as a finding. The dismissals registry covers this (per directive 1.4 / docs/agent_architecture.md): the operator stores a permanent dismissal for the captive-portal SSID, and the auditor consults it.

## When the operator may legitimately override

Even narrower than the captive-portal case:

- **Short-term diagnostic open SSID** the operator stands up to test a misbehaving device, intending to remove it within minutes. Even here the operator should use a deliberately-named test SSID, not the primary household one.

There is no defensible long-term operator-override for an open primary SSID. The Conductor should not record a RED operator_override CautionMarker here — the right resolution is moving the device to a captive-portal guest network, not accepting plaintext on the trusted network.

## How the Conductor uses this

When the auditor surfaces a `WIFI_NO_ENCRYPTION` finding:

1. Check the dismissals registry — operator may have already declared this SSID is a captive-portal voucher network.
2. If no dismissal: cite this summary, explain the difference between primary-SSID-open (RED, fix it) vs guest-captive-portal-SSID (AMBER, configure portal or downgrade to dismissal).
3. Offer the operator the captive-portal configuration walkthrough if they want to keep an open guest network.
