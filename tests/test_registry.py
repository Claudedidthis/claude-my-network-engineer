"""Tests for the device + client registry and the Registry Agent walkthrough."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from network_engineer.agents.registry_agent import (
    bootstrap,
    walkthrough,
)
from network_engineer.tools.registry import (
    Registry,
    identification_tips,
    manufacturer_for_mac,
    normalize_mac,
    oui_for_mac,
)
from network_engineer.tools.schemas import (
    ClientRegistryEntry,
    DeviceRegistryEntry,
    SecurityTier,
    Severity,
)

# ── MAC normalization & OUI lookup ───────────────────────────────────────────

@pytest.mark.parametrize(
    ("mac_in", "expected"),
    [
        ("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"),
        ("aa-bb-cc-dd-ee-ff", "aa:bb:cc:dd:ee:ff"),
        ("aabb.ccdd.eeff", "aa:bb:cc:dd:ee:ff"),
        ("aabbccddeeff", "aa:bb:cc:dd:ee:ff"),
        ("aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff"),
        ("", ""),
    ],
)
def test_normalize_mac(mac_in: str, expected: str) -> None:
    assert normalize_mac(mac_in) == expected


def test_oui_for_mac() -> None:
    assert oui_for_mac("aa:bb:cc:dd:ee:ff") == "aabbcc"
    assert oui_for_mac("AA:BB:CC:DD:EE:FF") == "aabbcc"


def test_manufacturer_for_known_apple_oui() -> None:
    # 3c:07:54 is one of Apple's OUIs in the bundled list
    assert manufacturer_for_mac("3c:07:54:00:00:01") == "Apple"


def test_manufacturer_for_unknown_returns_unknown() -> None:
    assert manufacturer_for_mac("00:00:00:00:00:00") == "Unknown"


def test_manufacturer_for_ubiquiti() -> None:
    assert "Ubiquiti" in manufacturer_for_mac("f0:9f:c2:00:00:01")


def test_identification_tips_apple_includes_settings_path() -> None:
    tips = identification_tips("Apple")
    assert any("Settings" in t for t in tips)


def test_identification_tips_unknown_falls_back() -> None:
    tips = identification_tips("Some-Mystery-Brand")
    assert tips
    assert any("arp" in t.lower() or "macvendors" in t.lower() for t in tips)


# ── Registry: load / save / lookup / upsert ─────────────────────────────────

def _temp_registry(tmp_path: Path) -> Registry:
    return Registry.load(
        device_path=tmp_path / "device_register.yaml",
        client_path=tmp_path / "client_register.yaml",
    )


def test_load_empty_when_files_missing(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    assert reg.devices == {}
    assert reg.clients == {}


def test_upsert_and_lookup_device(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    entry = DeviceRegistryEntry(
        mac="AA:BB:CC:DD:EE:FF", name_hint="Test AP", location="garage",
    )
    reg.upsert_device(entry)
    looked = reg.get_device("aa-bb-cc-dd-ee-ff")
    assert looked is not None
    assert looked.location == "garage"


def test_upsert_and_lookup_client(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    entry = ClientRegistryEntry(
        mac="aa:bb:cc:dd:ee:ff", tier_override=SecurityTier.IOT, owner="alex",
    )
    reg.upsert_client(entry)
    looked = reg.get_client("AABBCCDDEEFF")
    assert looked is not None
    assert looked.tier_override == SecurityTier.IOT


def test_save_and_reload_device(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    reg.upsert_device(DeviceRegistryEntry(
        mac="aa:bb:cc:dd:ee:ff", name_hint="U6 IW",
        location="master bedroom", criticality=Severity.HIGH,
    ))
    reg.save()
    reloaded = _temp_registry(tmp_path)
    assert len(reloaded.devices) == 1
    entry = reloaded.get_device("aa:bb:cc:dd:ee:ff")
    assert entry is not None
    assert entry.location == "master bedroom"
    assert entry.criticality == Severity.HIGH


def test_save_and_reload_client(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    reg.upsert_client(ClientRegistryEntry(
        mac="aa:bb:cc:dd:ee:ff", tier_override=SecurityTier.IOT,
        owner="shared", location="kitchen", notes="Lutron hub",
    ))
    reg.save()
    reloaded = _temp_registry(tmp_path)
    entry = reloaded.get_client("aa:bb:cc:dd:ee:ff")
    assert entry is not None
    assert entry.tier_override == SecurityTier.IOT
    assert entry.owner == "shared"
    assert entry.notes == "Lutron hub"


def test_upsert_bumps_updated_at(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    old = datetime(2020, 1, 1, tzinfo=UTC)
    entry = ClientRegistryEntry(mac="aa:bb:cc:dd:ee:ff", updated_at=old)
    reg.upsert_client(entry)
    assert reg.get_client("aa:bb:cc:dd:ee:ff").updated_at > old


def test_unannotated_device_filter(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    reg.upsert_device(DeviceRegistryEntry(mac="aa:00:00:00:00:01", location="garage"))
    reg.upsert_device(DeviceRegistryEntry(mac="aa:00:00:00:00:02"))
    pending = reg.unannotated_devices()
    assert len(pending) == 1
    assert pending[0].mac == "aa:00:00:00:00:02"


def test_unannotated_client_filter(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    reg.upsert_client(ClientRegistryEntry(
        mac="aa:00:00:00:00:01", tier_override=SecurityTier.IOT,
    ))
    reg.upsert_client(ClientRegistryEntry(mac="aa:00:00:00:00:02"))
    pending = reg.unannotated_clients()
    assert len(pending) == 1
    assert pending[0].mac == "aa:00:00:00:00:02"


# ── classify_client honors registry override ────────────────────────────────

def test_classify_client_uses_registry_override(tmp_path: Path) -> None:
    from network_engineer.agents.security_agent import classify_client

    reg = _temp_registry(tmp_path)
    reg.upsert_client(ClientRegistryEntry(
        mac="aa:bb:cc:dd:ee:ff", tier_override=SecurityTier.TRUST,
    ))
    # Heuristic would say IOT (camera keyword), but override wins
    client = {"name": "Kitchen camera", "macAddress": "AA:BB:CC:DD:EE:FF"}
    assert classify_client(client, registry=reg) == SecurityTier.TRUST


def test_classify_client_falls_back_to_heuristic_when_no_override(tmp_path: Path) -> None:
    from network_engineer.agents.security_agent import classify_client

    reg = _temp_registry(tmp_path)
    client = {"name": "Philips Hue", "macAddress": "aa:bb:cc:dd:ee:ff"}
    assert classify_client(client, registry=reg) == SecurityTier.IOT


def test_classify_client_works_with_empty_registry() -> None:
    from network_engineer.agents.security_agent import classify_client

    client = {"name": "Macbook Pro", "macAddress": "aa:bb:cc:dd:ee:ff"}
    assert classify_client(client, registry=Registry()) == SecurityTier.TRUST


# ── Bootstrap (non-interactive) ──────────────────────────────────────────────

class _MockUnifi:
    def __init__(self, devices: list[dict[str, Any]], clients: list[dict[str, Any]]) -> None:
        self._devices = devices
        self._clients = clients

    def get_device_stats(self) -> list[dict[str, Any]]:
        return self._devices

    def get_clients(self) -> list[dict[str, Any]]:
        return self._clients


def test_bootstrap_adds_devices(tmp_path: Path) -> None:
    client = _MockUnifi(
        devices=[
            {"mac": "aa:00:00:00:00:01", "name": "AP-1", "model": "U6 IW"},
            {"mac": "aa:00:00:00:00:02", "name": "Switch-1", "model": "USW Pro"},
        ],
        clients=[],
    )
    reg = _temp_registry(tmp_path)
    reg, counts = bootstrap(client, registry=reg)
    assert counts["devices_added"] == 2
    assert reg.get_device("aa:00:00:00:00:01") is not None


def test_bootstrap_classifies_known_clients(tmp_path: Path) -> None:
    client = _MockUnifi(
        devices=[],
        clients=[
            {"macAddress": "aa:00:00:00:00:01", "name": "Macbook Pro"},
            {"macAddress": "aa:00:00:00:00:02", "name": "Philips Hue"},
            {"macAddress": "aa:00:00:00:00:03", "name": "Random"},
        ],
    )
    reg = _temp_registry(tmp_path)
    reg, counts = bootstrap(client, registry=reg)
    assert counts["clients_added"] == 3
    assert counts["clients_auto_classified"] == 2  # Mac + Hue
    assert reg.get_client("aa:00:00:00:00:01").tier_override == SecurityTier.TRUST
    assert reg.get_client("aa:00:00:00:00:02").tier_override == SecurityTier.IOT
    # Unknown name → no override
    assert reg.get_client("aa:00:00:00:00:03").tier_override is None


def test_bootstrap_does_not_overwrite_existing(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    reg.upsert_client(ClientRegistryEntry(
        mac="aa:00:00:00:00:01", tier_override=SecurityTier.GUEST,
        owner="set by operator",
    ))
    client = _MockUnifi(
        devices=[],
        clients=[{"macAddress": "aa:00:00:00:00:01", "name": "Philips Hue"}],
    )
    reg, counts = bootstrap(client, registry=reg)
    assert counts["clients_added"] == 0
    # Operator-set values intact
    entry = reg.get_client("aa:00:00:00:00:01")
    assert entry.owner == "set by operator"
    assert entry.tier_override == SecurityTier.GUEST


# ── Interactive walkthrough (scripted) ───────────────────────────────────────

class _ScriptedInput:
    """Replay a list of canned answers; raises StopIteration when exhausted."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"Walkthrough asked more than scripted: {prompt!r}")
        return self.answers.pop(0)


def test_walkthrough_recognized_client_captures_all_fields(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    reg.upsert_client(ClientRegistryEntry(
        mac="3c:07:54:00:00:01", name_hint="Family iPhone",
    ))
    client = _MockUnifi(
        devices=[],
        clients=[{
            "macAddress": "3c:07:54:00:00:01", "name": "Family iPhone",
            "ipAddress": "192.168.1.42", "signal": -55,
        }],
    )
    answers = [
        "y",            # recognize?
        "TRUST",        # tier
        "alex",        # owner
        "everywhere",   # location
        "primary phone",# role
        "HIGH",         # criticality
        "kid's phone",  # notes
    ]
    walkthrough(client, registry=reg, input_fn=_ScriptedInput(answers), print_fn=lambda _: None)
    entry = reg.get_client("3c:07:54:00:00:01")
    assert entry.tier_override == SecurityTier.TRUST
    assert entry.owner == "alex"
    assert entry.location == "everywhere"
    assert entry.role == "primary phone"
    assert entry.criticality == Severity.HIGH
    assert entry.notes == "kid's phone"
    assert entry.source == "manual"


def test_walkthrough_unidentified_client_marked_unknown(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    reg.upsert_client(ClientRegistryEntry(
        mac="00:00:00:11:22:33", name_hint="?",
    ))
    client = _MockUnifi(
        devices=[],
        clients=[{"macAddress": "00:00:00:11:22:33"}],
    )
    answers = ["n", "n"]   # don't recognize, still don't recognize after tips
    walkthrough(client, registry=reg, input_fn=_ScriptedInput(answers), print_fn=lambda _: None)
    entry = reg.get_client("00:00:00:11:22:33")
    assert entry.tier_override == SecurityTier.UNKNOWN
    assert "could not identify" in entry.notes.lower()


def test_walkthrough_skip_does_not_modify(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    reg.upsert_client(ClientRegistryEntry(mac="aa:bb:cc:dd:ee:ff"))
    client = _MockUnifi(
        devices=[],
        clients=[{"macAddress": "aa:bb:cc:dd:ee:ff"}],
    )
    walkthrough(
        client, registry=reg,
        input_fn=_ScriptedInput(["skip"]), print_fn=lambda _: None,
    )
    entry = reg.get_client("aa:bb:cc:dd:ee:ff")
    assert entry.tier_override is None  # still unannotated


def test_walkthrough_quit_saves_progress(tmp_path: Path) -> None:
    reg = _temp_registry(tmp_path)
    reg.upsert_client(ClientRegistryEntry(mac="aa:bb:cc:dd:ee:01"))
    reg.upsert_client(ClientRegistryEntry(mac="aa:bb:cc:dd:ee:02"))
    client = _MockUnifi(
        devices=[],
        clients=[
            {"macAddress": "aa:bb:cc:dd:ee:01"},
            {"macAddress": "aa:bb:cc:dd:ee:02"},
        ],
    )
    answers = [
        "y",      # recognize first
        "IOT",    # tier
        "shared", # owner
        "q",      # quit during location prompt — exits before second device
    ]
    walkthrough(client, registry=reg, input_fn=_ScriptedInput(answers), print_fn=lambda _: None)
    # First entry annotated and saved
    e1 = reg.get_client("aa:bb:cc:dd:ee:01")
    assert e1.tier_override == SecurityTier.IOT
    assert e1.owner == "shared"
    # Second entry untouched
    e2 = reg.get_client("aa:bb:cc:dd:ee:02")
    assert e2.tier_override is None


def test_walkthrough_devices_first(tmp_path: Path) -> None:
    """Devices are walked before clients (because there are usually fewer)."""
    reg = _temp_registry(tmp_path)
    reg.upsert_device(DeviceRegistryEntry(mac="aa:bb:cc:dd:ee:01"))
    reg.upsert_client(ClientRegistryEntry(mac="aa:bb:cc:dd:ee:02"))
    client = _MockUnifi(
        devices=[{"mac": "aa:bb:cc:dd:ee:01", "name": "AP-1", "model": "U6"}],
        clients=[{"macAddress": "aa:bb:cc:dd:ee:02"}],
    )
    # Skip device first, then skip client — confirms devices come first
    scripted = _ScriptedInput(["n", "skip"])
    walkthrough(client, registry=reg, input_fn=scripted, print_fn=lambda _: None)
    # First prompt should mention "Annotate this device" (device walk)
    assert any("device" in p.lower() for p in scripted.prompts[:2])
