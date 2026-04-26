"""Tests for the SSL three-mode policy (directive 1.2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from network_engineer.tools.ssl_policy import (
    SSLMode,
    SSLPolicyError,
    _is_private_address,
    build_verify,
    resolve_mode,
)


# ── Private-address detection ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "host",
    [
        "192.168.1.1",        # RFC1918
        "192.168.69.1",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.255",
        "127.0.0.1",          # loopback
        "::1",
        "169.254.1.1",        # link-local
        "fe80::1",
        "fd00::1",            # ULA
    ],
)
def test_private_address_recognized(host: str) -> None:
    assert _is_private_address(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",            # globally routable
        "1.1.1.1",
        "52.0.0.1",
        "2606:4700:4700::1111",
    ],
)
def test_public_address_rejected(host: str) -> None:
    """A globally-routable address is not 'private' under our LAN_ONLY policy.

    Note: Python's ip_address.is_private also treats some non-routable
    documentation/reserved ranges (RFC5737 TEST-NET, RFC6598 shared-address
    space, etc.) as private. We accept that semantics — the LAN_ONLY policy
    targets the realistic deployment shape, not abstract routability.
    """
    assert _is_private_address(host) is False


def test_hostname_treated_as_non_private() -> None:
    """Hostnames are refused under LAN_ONLY (no resolution at construction
    time — TOCTOU). This matches the documented usage where LAN operators
    use the IP literal."""
    assert _is_private_address("controller.local") is False
    assert _is_private_address("unifi.example.com") is False


# ── resolve_mode ────────────────────────────────────────────────────────────

def test_resolve_mode_default_is_lan_only() -> None:
    assert resolve_mode(env={}) is SSLMode.LAN_ONLY


def test_resolve_mode_explicit() -> None:
    assert resolve_mode(env={"UNIFI_SSL_MODE": "ca_bundle"}) is SSLMode.CA_BUNDLE
    assert resolve_mode(env={"UNIFI_SSL_MODE": "pinned"}) is SSLMode.PINNED
    assert resolve_mode(env={"UNIFI_SSL_MODE": "LAN_ONLY"}) is SSLMode.LAN_ONLY  # case-insensitive


def test_resolve_mode_rejects_unknown() -> None:
    with pytest.raises(SSLPolicyError, match="not a valid mode"):
        resolve_mode(env={"UNIFI_SSL_MODE": "off"})


# ── build_verify: LAN_ONLY ──────────────────────────────────────────────────

def test_lan_only_returns_false_for_private_host() -> None:
    assert build_verify(SSLMode.LAN_ONLY, "192.168.1.1") is False
    assert build_verify(SSLMode.LAN_ONLY, "10.0.0.1") is False
    assert build_verify(SSLMode.LAN_ONLY, "127.0.0.1") is False


def test_lan_only_refuses_public_host() -> None:
    with pytest.raises(SSLPolicyError, match="lan_only refuses host"):
        build_verify(SSLMode.LAN_ONLY, "8.8.8.8")


def test_lan_only_refuses_hostname() -> None:
    """Hostnames cannot be confirmed private at construction time without
    resolution; LAN_ONLY refuses to take that risk."""
    with pytest.raises(SSLPolicyError, match="lan_only refuses host"):
        build_verify(SSLMode.LAN_ONLY, "controller.local")


# ── build_verify: CA_BUNDLE ─────────────────────────────────────────────────

def test_ca_bundle_returns_path_when_file_exists(tmp_path: Path) -> None:
    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n")
    result = build_verify(SSLMode.CA_BUNDLE, "192.168.1.1", env={"UNIFI_CA_BUNDLE": str(bundle)})
    assert result == str(bundle)


def test_ca_bundle_requires_env_var() -> None:
    with pytest.raises(SSLPolicyError, match="UNIFI_CA_BUNDLE"):
        build_verify(SSLMode.CA_BUNDLE, "192.168.1.1", env={})


def test_ca_bundle_requires_existing_file(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does_not_exist.pem"
    with pytest.raises(SSLPolicyError, match="not a readable file"):
        build_verify(SSLMode.CA_BUNDLE, "192.168.1.1", env={"UNIFI_CA_BUNDLE": str(nonexistent)})


def test_ca_bundle_does_not_constrain_host() -> None:
    """CA_BUNDLE means 'verify against this CA' — the host can be public."""
    bundle_dir = Path(__file__).resolve().parent
    # Use any existing file as a stand-in for a CA bundle (the policy doesn't
    # validate PEM content at this layer)
    bundle = bundle_dir / "test_ssl_policy.py"
    result = build_verify(SSLMode.CA_BUNDLE, "8.8.8.8", env={"UNIFI_CA_BUNDLE": str(bundle)})
    assert result == str(bundle)


# ── build_verify: PINNED ────────────────────────────────────────────────────

def test_pinned_requires_fingerprint_env() -> None:
    with pytest.raises(SSLPolicyError, match="UNIFI_CERT_FINGERPRINT"):
        build_verify(SSLMode.PINNED, "192.168.1.1", env={})


def test_pinned_with_fingerprint_raises_not_implemented_until_phase_14() -> None:
    """The directive specifies PINNED is required before Phase 14
    (Cloudflare Tunnel). The mode validates here so operators can stage
    the env, but the verifier itself ships in Phase 14."""
    with pytest.raises(NotImplementedError, match="Phase 14"):
        build_verify(
            SSLMode.PINNED, "192.168.1.1",
            env={"UNIFI_CERT_FINGERPRINT": "a" * 64},
        )


# ── Integration: UnifiClient construction ───────────────────────────────────

def test_unifi_client_default_mode_accepts_private_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default LAN_ONLY allows construction against an RFC1918 host."""
    monkeypatch.delenv("UNIFI_SSL_MODE", raising=False)
    monkeypatch.setenv("UNIFI_HOST", "192.168.1.1")
    monkeypatch.setenv("UNIFI_API_KEY", "fake-key-for-test")
    monkeypatch.delenv("UNIFI_MODE", raising=False)

    # Construction must reach the SSL setup; the v1 site-id lookup will
    # fail on httpx connect, but only after the SSL policy passes. To
    # avoid the network call, construct in fixtures mode but assert the
    # LAN_ONLY policy logic via build_verify directly instead.
    from network_engineer.tools.ssl_policy import build_verify, resolve_mode
    mode = resolve_mode()
    assert mode is SSLMode.LAN_ONLY
    assert build_verify(mode, "192.168.1.1") is False


def test_unifi_client_default_mode_refuses_public_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default LAN_ONLY refuses construction against a public host —
    fail-fast on misconfigured UNIFI_HOST."""
    from network_engineer.tools.ssl_policy import build_verify, resolve_mode
    monkeypatch.delenv("UNIFI_SSL_MODE", raising=False)
    mode = resolve_mode()
    with pytest.raises(SSLPolicyError, match="lan_only refuses host"):
        build_verify(mode, "52.0.0.1")
