---
source_id: red-002-default-admin-credentials
title: Default admin credentials still active on network gear
severity_band: RED
related_caution_codes:
  - "DEFAULT_ADMIN_CREDENTIALS"
sources_cited:
  - "NIST SP 800-53 Rev 5, IA-2 'Identification and Authentication (Organizational Users)' (referenced)"
  - "NIST SP 800-63B Digital Identity Guidelines (referenced)"
  - "CIS Controls v8, Control 5 'Account Management' (referenced)"
  - "NISTIR 8259 IoT Device Cybersecurity Capability Baseline (referenced)"
license: MIT
url: null
last_updated: "2026-04-27"
---

## Why this is RED

Default credentials on any network device — gateway, switch, AP, camera, printer, NAS, IoT bridge — are public knowledge. Vendor manuals are searchable; default-credential lookup tables exist for every product class; Shodan and Censys index devices accepting their factory defaults globally. A device on the network with default credentials is not protected by authentication at all; the only protection is that nobody has guessed it's there yet.

This is categorical in every networking curriculum and IoT-security baseline. Change them.

## What canonical sources say

**NIST SP 800-53 Rev 5, IA-2 "Identification and Authentication (Organizational Users)"**: each user/account requires unique identification and authenticator. Defaults satisfy neither uniqueness nor authentication-as-a-control.

**NIST SP 800-63B Digital Identity Guidelines**: factory-default authenticators provide no entropy; they're equivalent to no authenticator at all from the framework's perspective.

**NISTIR 8259 IoT Device Cybersecurity Capability Baseline**: explicitly calls out factory-default credentials as a baseline failure for IoT devices intended for network connection.

**CIS Controls v8, Control 5.4**: default accounts and passwords must be removed or disabled on every device class.

## Standard alternative

For each device class:

1. **UniFi gear** — controller / APs / switches / cameras: change the admin password during initial setup. UniFi prompts during adoption; the prompt should never be deferred.
2. **ISP-provided gear** — modem, combo router (when used in bridge mode): admin credentials are often required for configuration. Change them.
3. **IoT devices** — anything that accepts a configurable login (smart camera, printer, NAS, smart-home bridge). Default-credential exposure is the #1 IoT compromise vector. Change them or disable remote management entirely.
4. **Wi-Fi access points other than UniFi** — Eero, Nest WiFi, Google WiFi, etc.: each has admin credentials. Change.

The operator should also document the credentials somewhere durable but local — a password manager, not a Post-It on the desk.

## What "default" actually means here

A device has default credentials if:

- The username/password is the factory-shipped value (`admin`/`admin`, `root`/`(blank)`, etc.)
- The username/password is a documented "first-time-setup" value the operator never changed
- The credentials are the same across multiple devices the operator owns ("I use the same password on all my UniFi gear" — this is not default in the strict sense, but is bad practice for a different reason)

## How the Conductor detects this

Direct detection of default credentials is not always possible (the agent can't generally try logins). Indirect signals:

- New device adopted recently → ask the operator if they changed default credentials during adoption
- UniFi controller has default site / network / admin names → likely default password too
- Device explicitly broadcasts default-credential markers (some IoT devices include the literal string "default" or "admin" in mDNS / SSDP advertisements)

When the Conductor cannot directly verify, it asks the operator: *"Have you changed the admin credentials on the [device] from its factory defaults?"* and saves the answer with confidence.

## When the operator may legitimately override

There is no defensible operator-override here. If the operator says "yes, I'm intentionally leaving defaults," the Conductor should still record a RED marker — this isn't a contextual judgment call, it's a categorical security failure. The marker stays acknowledged-not-resolved until the operator changes the credentials.

## How the Conductor uses this

When the auditor flags a default-credential signal, or the operator's heritage walkthrough surfaces a never-configured-since-purchase device:

1. Cite this summary.
2. Offer device-class-specific remediation guidance.
3. If the operator declares "I'll leave them," still record the RED marker. The agent does not enable categorical security failures by accepting operator dismissal.
