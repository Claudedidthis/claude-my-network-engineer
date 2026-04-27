---
source_id: red-014-port-forward-databases
title: Database ports forwarded to WAN
severity_band: RED
related_caution_codes:
  - "PORT_FORWARD_MSSQL_WAN"
  - "PORT_FORWARD_MYSQL_WAN"
  - "PORT_FORWARD_POSTGRES_WAN"
  - "PORT_FORWARD_MONGODB_WAN"
  - "PORT_FORWARD_REDIS_WAN"
sources_cited:
  - "NIST SP 800-53 Rev 5, SC-7 'Boundary Protection' (referenced)"
  - "NIST SP 800-53 Rev 5, AC-3 'Access Enforcement' (referenced)"
  - "CIS Controls v8, Control 4 'Secure Configuration of Enterprise Assets and Software' (referenced)"
license: MIT
url: null
last_updated: "2026-04-27"
---

## Why this is RED

Database ports — MS-SQL (1433), MySQL (3306), PostgreSQL (5432), MongoDB (27017), Redis (6379), Memcached (11211) — should never be reachable from the public internet. The threat is not theoretical: every one of these protocols has had RCE-class vulnerabilities discovered in shipping versions over the last decade, and most have authentication models designed for trusted-network deployment that do not survive contact with the open internet.

Redis and Memcached are the worst because they shipped historically with **no authentication required by default**. Operators following old tutorials still configure them this way. A Redis instance reachable on the WAN with no auth is a fully writable key-value store handed to whoever finds it first.

## What canonical sources say

**NIST SP 800-53 Rev 5, SC-7 "Boundary Protection"**: information systems must monitor and control communications at external boundaries. A database port reachable from the public internet has no boundary; the database engine itself is the boundary.

**NIST SP 800-53 Rev 5, AC-3 "Access Enforcement"**: access decisions must enforce approved authorizations. Database protocols' authentication is not designed as the sole boundary against unbounded internet attack volume.

**CIS Controls v8, Control 4.5 "Implement and Manage a Firewall on Servers"** and **Control 4.7 "Manage Default Accounts on Enterprise Assets and Software"**: explicitly require database servers to be firewalled from non-trusted networks and that default accounts (e.g., `sa`, `root@%`) be disabled.

## Standard alternative

The right pattern: the database stays on the LAN; the application that uses it is also on the LAN; remote access to the application happens via VPN.

If a service genuinely needs to expose a query interface to the internet, the architecture should be:

```
internet → reverse proxy / API gateway (HTTPS) → application server → database
```

Not:

```
internet → database (whatever protocol) — NEVER
```

The reverse proxy / API gateway terminates TLS, performs authentication, validates input, and translates to internal-network database queries. The database itself is firewalled from WAN.

## Self-hosted services that need remote DB access

If the operator runs a self-hosted service that legitimately needs database access from outside the LAN (e.g., a hosted application that connects from a cloud server back to the home database):

- **Better**: a VPN tunnel from the cloud server back to the home network. The cloud server connects to the database over the VPN; no WAN exposure.
- **Acceptable for personal use**: a tightly-firewalled rule limiting database-port access to a single source IP (the cloud server's static IP). This is still risky if the cloud server is compromised, but reduces the attack surface from "the entire internet" to "one specific IP."
- **Never**: an unrestricted port forward.

Even the firewalled-to-source-IP case warrants an AMBER caution marker, not RED.

## When the operator may legitimately override

There is no legitimate home-network case for a wide-open database port forward to WAN. If an operator is running a service that requires this, they should be using one of the alternatives above. The Conductor records a RED operator_override marker only when the operator insists despite hearing the alternatives.

## How the Conductor uses this

When the auditor surfaces a port forward to any of the database ports (1433, 3306, 5432, 27017, 6379, 11211, etc.):

1. Cite this summary explicitly.
2. Identify the specific port and ask the operator what service is using it.
3. Offer the reverse-proxy / VPN architectures appropriate to the operator's use case.
4. If the operator overrides, record a RED operator_override marker with the canonical_source citation and the operator's rationale.
