---
source_id: amber-001-port-forward-http-https
title: HTTP / HTTPS port forwards to WAN for self-hosted services
severity_band: AMBER
related_caution_codes:
  - "PORT_FORWARD_HTTP_WAN"
  - "PORT_FORWARD_HTTPS_WAN"
sources_cited:
  - "NIST SP 800-53 Rev 5, SC-7 'Boundary Protection' (referenced)"
  - "OWASP Application Security Verification Standard (referenced)"
  - "Cloudflare Tunnel documentation (referenced — proprietary)"
license: MIT
url: null
last_updated: "2026-04-27"
---

## Why this is AMBER (not RED)

Forwarding ports 80 (HTTP) and 443 (HTTPS) to a self-hosted service on the LAN is something a thoughtful operator can do safely. Many home operators run legitimate services this way — personal websites, photo galleries, status pages, home dashboards, bookmark managers. Unlike database ports or SSH-on-WAN, web traffic on standard ports is **expected** by every internet stakeholder, and the application server is generally designed to handle hostile inputs.

That said, the operator is now responsible for everything an application security posture demands. Most operators don't realize what they signed up for.

## What canonical sources say

**NIST SP 800-53 Rev 5, SC-7 "Boundary Protection"**: explicitly permits boundary protection through application-layer firewalls and reverse proxies. HTTP/HTTPS forwarding to a hardened application server is the canonical pattern.

**OWASP Application Security Verification Standard (ASVS)**: defines the security requirements an exposed web application must meet. Operators with a port-80/443 forward are now responsible for ASVS-class concerns: input validation, session management, dependency vulnerability monitoring, etc.

## Standard architecture for safe HTTP/HTTPS exposure

In order from "least operator effort" to "highest control":

1. **Cloudflare Tunnel** — no port forward at all. The tunnel agent runs on the home server and creates an outbound connection to Cloudflare's edge; Cloudflare handles TLS, DDoS, WAF, and access policies. The home network has zero inbound exposure.

2. **Tailscale Funnel** — similar concept: the Tailscale node exposes the service via Tailscale's edge, no port forward on the home gateway.

3. **Home gateway port forward + Cloudflare or Caddy reverse proxy on the LAN** — the port forward terminates at a reverse proxy (Caddy is the lowest-friction option for home use) which handles TLS, rate limiting, and routing to the actual application. Application is not exposed directly.

4. **Direct port forward to the application** — the application is responsible for everything. Acceptable only when:
   - The application is well-maintained (active upstream development, recent CVE responses)
   - TLS is configured with current ciphers and a valid certificate
   - The application has authentication (the service isn't anonymously accessible by accident)
   - The operator has a vulnerability-update plan (auto-update, monitoring, etc.)

## What raises this from AMBER to RED

- **Plain HTTP (port 80) only, no HTTPS** — sniffable plaintext for anything sensitive. RED if the application handles authentication or personal data; AMBER if it's a static read-only site.
- **The application has known unpatched RCE-class CVEs** — different problem, but the port forward is the attack vector. Conductor escalates.
- **Default admin credentials on the application** — separate concern (red-002), but the port forward enables it. Escalate.
- **A "fancy router" / consumer-grade gateway with auto-UPnP** opening unintended ports — the operator should disable UPnP and explicitly enumerate forwards.

## When the operator should resolve the AMBER marker

The marker stays active until either:

- The operator switches to Cloudflare Tunnel / Tailscale Funnel (zero exposure), at which point the marker auto-resolves on the next audit pass.
- The operator confirms they've put a hardened reverse proxy in front of the application AND the application itself meets the OWASP ASVS basics. The Conductor doesn't auto-resolve here — operator-acknowledge transitions the marker to acknowledged-not-resolved.

## How the Conductor uses this

When the auditor flags a port-80 or port-443 forward:

1. Identify the service behind the forward (the operator may have given it a name; the Conductor asks if not).
2. Cite this summary; offer the Cloudflare Tunnel / Tailscale Funnel alternatives if the operator hasn't considered them.
3. Record an AMBER audit_finding marker so the dashboard reflects the exposure.
4. If the service has known vulnerabilities (operator says "yes I haven't updated in two years"), escalate to RED.
