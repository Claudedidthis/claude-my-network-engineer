"""SSL verification policy for the UniFi controller transport (directive 1.2).

Three modes, defaulting to the most permissive that is safe on a LAN:

    LAN_ONLY    — verify=False, but the host MUST be RFC1918 / loopback /
                  link-local. Refuses construction if UNIFI_HOST resolves
                  to a public address. Suitable for the default home-LAN
                  deployment shape.

    CA_BUNDLE   — verify against a caller-supplied CA file (UNIFI_CA_BUNDLE).
                  The expected use case: operator generated their own CA,
                  signed the UDM cert, distributed the CA bundle.

    PINNED      — verify against a specific certificate fingerprint
                  (UNIFI_CERT_FINGERPRINT). Strictest. Required posture
                  before Phase 14 (Cloudflare Tunnel) which exposes the
                  controller to the public internet — there, MITM with a
                  valid cert is the relevant threat and CA-bundle
                  verification is insufficient on its own.

Why three modes
---------------
A single `verify=False` flag conflates two very different threat models:

    On the LAN, the only realistic attacker is one who has already
    compromised the LAN — at which point cert verification is not the
    operative defence. `verify=False` is acceptable.

    Once the controller is reachable beyond the LAN (Cloudflare Tunnel,
    public DNS, etc.), `verify=False` aligns autonomous actuation with
    a MITM attacker's capabilities — Waiter §5.3 calls this an
    architectural resonance condition. The MITM rewrites a legitimate
    "no" to a "yes", or vice versa. No defensive prompt fixes that.

The directive specifies the configuration posture must be correct *before*
the deployment topology changes — i.e. now, while the system is still
LAN-only, but knowing Phase 14 is on the roadmap.
"""
from __future__ import annotations

import ipaddress
import os
from enum import StrEnum
from pathlib import Path


class SSLPolicyError(RuntimeError):
    """Raised when the SSL configuration is internally inconsistent or
    when the chosen mode forbids the target host."""


class SSLMode(StrEnum):
    PINNED = "pinned"
    CA_BUNDLE = "ca_bundle"
    LAN_ONLY = "lan_only"


def _resolve_to_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Best-effort: parse host as an IP literal; return None if it's a hostname.

    We do NOT resolve hostnames here — that would mean network I/O at
    construction time and create a TOCTOU window. A hostname is treated
    as "could be anything" and refused under LAN_ONLY (operators on a LAN
    use the IP directly, which is the documented shape).
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_private_address(host: str) -> bool:
    """True iff host is an RFC1918 / loopback / link-local IP literal.

    Hostnames return False — see _resolve_to_ip rationale.
    """
    ip = _resolve_to_ip(host)
    if ip is None:
        return False
    return (
        ip.is_private          # RFC1918, ULA, ...
        or ip.is_loopback      # 127/8, ::1
        or ip.is_link_local    # 169.254/16, fe80::/10
    )


def resolve_mode(env: dict[str, str] | None = None) -> SSLMode:
    """Read UNIFI_SSL_MODE from the environment; default LAN_ONLY."""
    e = env if env is not None else os.environ
    raw = (e.get("UNIFI_SSL_MODE") or "lan_only").strip().lower()
    try:
        return SSLMode(raw)
    except ValueError as exc:
        raise SSLPolicyError(
            f"UNIFI_SSL_MODE={raw!r} is not a valid mode. "
            f"Use one of: {[m.value for m in SSLMode]}"
        ) from exc


def build_verify(
    mode: SSLMode,
    host: str,
    *,
    env: dict[str, str] | None = None,
) -> bool | str:
    """Return the value to pass to httpx's `verify=` for the given mode.

    Raises SSLPolicyError on misconfiguration:
      - LAN_ONLY  + non-private host
      - CA_BUNDLE + missing UNIFI_CA_BUNDLE or non-existent path
      - PINNED    + missing UNIFI_CERT_FINGERPRINT
      - PINNED                                        (currently NotImplementedError —
                                                       fingerprint pinning ships in Phase 14)
    """
    e = env if env is not None else os.environ

    if mode is SSLMode.LAN_ONLY:
        if not _is_private_address(host):
            raise SSLPolicyError(
                f"UNIFI_SSL_MODE=lan_only refuses host {host!r}: not an "
                f"RFC1918/loopback/link-local IP literal. If you genuinely "
                "need to reach this host, switch to UNIFI_SSL_MODE=ca_bundle "
                "or pinned (Phase 14)."
            )
        return False

    if mode is SSLMode.CA_BUNDLE:
        bundle = e.get("UNIFI_CA_BUNDLE")
        if not bundle:
            raise SSLPolicyError(
                "UNIFI_SSL_MODE=ca_bundle requires UNIFI_CA_BUNDLE to point "
                "to a CA bundle (PEM) file."
            )
        bundle_path = Path(bundle)
        if not bundle_path.is_file():
            raise SSLPolicyError(
                f"UNIFI_CA_BUNDLE={bundle!r} is not a readable file."
            )
        return str(bundle_path)

    if mode is SSLMode.PINNED:
        fingerprint = e.get("UNIFI_CERT_FINGERPRINT")
        if not fingerprint:
            raise SSLPolicyError(
                "UNIFI_SSL_MODE=pinned requires UNIFI_CERT_FINGERPRINT "
                "(SHA-256 hex of the controller's leaf certificate)."
            )
        # Fingerprint pinning is planned for Phase 14 (Cloudflare Tunnel
        # deployment) where it is required posture. The current LAN
        # deployment does not need it; mode is accepted as a configuration
        # forward-declaration but raises until the verifier is implemented.
        raise NotImplementedError(
            "UNIFI_SSL_MODE=pinned is reserved for Phase 14 deployment. "
            "Use ca_bundle until then; the configuration validates here so "
            "you can stage the env without surprises later."
        )

    # Unreachable given SSLMode is closed
    raise SSLPolicyError(f"unhandled SSL mode {mode!r}")
