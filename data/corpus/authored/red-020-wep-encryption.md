---
source_id: red-020-wep-encryption
title: WEP or WPA-TKIP encryption on a primary SSID
severity_band: RED
related_caution_codes:
  - "WIFI_WEP_ENCRYPTION"
  - "WIFI_WPA_TKIP"
sources_cited:
  - "IEEE 802.11-2020 (deprecates WEP and WPA-TKIP) (referenced)"
  - "NIST SP 800-53 Rev 5, SC-8 'Transmission Confidentiality and Integrity' (referenced)"
  - "Wi-Fi Alliance — WPA3 specification, WPA2 transition guidance (referenced)"
license: MIT
url: null
last_updated: "2026-04-27"
---

## Why this is RED

WEP (Wired Equivalent Privacy) was broken in 2001. Practical attacks recover the key in minutes from passive traffic capture. WPA-TKIP has been broken since 2017 (KRACK and predecessors). Both are deprecated by the IEEE 802.11 standard and removed from modern Wi-Fi Alliance certification programs.

A network running WEP or WPA-TKIP today is not encrypted in any meaningful sense. The cryptographic protection these standards purported to provide does not survive a single laptop with a directional antenna.

## What canonical sources say

**IEEE 802.11-2020**: WEP is removed from the standard's mandatory functionality. WPA-TKIP is preserved for backward compatibility but flagged as cryptographically weak. WPA2-CCMP (AES) and WPA3-SAE are the modern requirements.

**NIST SP 800-53 Rev 5, SC-8 "Transmission Confidentiality and Integrity"**: cryptographic mechanisms must provide confidentiality during transmission. Broken cryptography does not satisfy the control.

**Wi-Fi Alliance**: WPA3 is the current state of the art. WPA2-Personal (with CCMP/AES) remains acceptable. WPA-TKIP and WEP are deprecated.

## Standard alternative

WPA3-Personal where every client device supports it (modern Wi-Fi 6/6E gear universally does). Older devices may need WPA2-Personal, in which case "WPA2/WPA3 Transition Mode" (UniFi terminology) gracefully falls back per-client.

A passphrase of 12+ characters is sufficient with modern key-derivation; entropy matters more than complexity. The Conductor recommends a passphrase rather than a random string — operators rotate random strings less frequently because they're harder to share with family.

## Common reasons operators get stuck on WEP/TKIP

- **A legacy device** (printer from 2008, Roomba from 2012, IoT thermostat that hasn't been updated since shipping). The right answer is a separate IoT SSID with appropriate security; the modern primary SSID should not be downgraded for one device.
- **The operator's WiFi setup memory** is from when WPA-TKIP was current. Modern equipment defaults to WPA2/WPA3; a setup screen showing TKIP is suspicious.
- **Forced compatibility mode** in older controller firmware. Upgrade the controller / AP firmware.

In every case the right resolution is moving the legacy device to its own SSID, not keeping the primary SSID on broken crypto.

## When the operator may legitimately override

No legitimate long-term override. The IoT-segregation pattern (separate SSID for the legacy device, primary SSID stays modern) handles every edge case the operator is likely to encounter.

If the operator says "I have one device that only supports WEP, I can't replace it, I have no other option" — the Conductor's response is the IoT-segregation walkthrough, not accepting the operator-override.

## How the Conductor uses this

When the auditor flags a SSID with WEP or WPA-TKIP encryption:

1. Cite this summary.
2. Offer the IoT-segregation pattern as the standard fix (separate SSID for legacy device, primary stays modern).
3. If the operator hasn't tried it: walk them through creating an additional UniFi WLAN with the right security level for the legacy device.
4. Record an AMBER caution marker (not RED operator_override — the situation is "in transition, fix is known") until the segregation is complete.
