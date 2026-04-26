"""Enforce that example YAMLs use only synthetic MAC addresses.

A reviewer surfaced that committed example files leak real operator MACs
when the device-specific suffix bytes aren't sanitized. This test grep-walks
the example YAMLs and asserts that every MAC-shaped string follows one of
two synthetic-suffix conventions:

  • OUI prefix preserved + suffix zeroed: AA:BB:CC:00:00:NN
    (educational — operator can OUI-lookup the prefix and learn the vendor)
  • Fully synthetic prefix: AA:BB:CC:DD:EE:FF or RFC-style 02:00:... locally
    administered ranges

Anything else is rejected — almost certainly a leaked real MAC.

If you legitimately need a different MAC pattern in an example, add an
explicit allowlist entry below with rationale rather than disabling the test.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLES_DIR = _REPO_ROOT / "examples"

_MAC_RE = re.compile(r"\b([0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2})\b", re.IGNORECASE)


def _is_synthetic_mac(mac: str) -> bool:
    """True if the MAC follows a recognised synthetic convention.

    Conventions accepted:
      • Suffix bytes (last 3 octets) are 00:00:NN (NN any).
      • Whole MAC is in the locally-administered RFC range 02:00:00:00:00:NN.
      • Whole MAC is the all-zero or all-FF sentinel.
    """
    parts = mac.lower().split(":")
    if len(parts) != 6:
        return False
    suffix = parts[3:]
    # Accept zeroed-suffix convention (XX:XX:XX:00:00:NN).
    if suffix[0] == "00" and suffix[1] == "00":
        return True
    # Accept locally-administered prefix convention.
    if parts[0] in ("02", "06", "0a", "0e") and all(p in ("00", "01", "02", "03", "04", "ff") for p in parts[1:5]):
        return True
    # Accept sentinels.
    if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "aa:bb:cc:dd:ee:ff"):
        return True
    return False


def _collect_macs() -> list[tuple[Path, int, str]]:
    """Walk examples/ and return every MAC found with file + line context."""
    found: list[tuple[Path, int, str]] = []
    for path in _EXAMPLES_DIR.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".yaml", ".yml", ".json", ".md"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for match in _MAC_RE.finditer(line):
                found.append((path, line_no, match.group(1).lower()))
    return found


def test_no_real_macs_in_examples() -> None:
    """Every MAC in examples/ must follow a synthetic-suffix convention."""
    leaks = []
    for path, line_no, mac in _collect_macs():
        if not _is_synthetic_mac(mac):
            leaks.append(
                f"  {path.relative_to(_REPO_ROOT)}:{line_no} contains non-synthetic MAC {mac!r}",
            )
    assert not leaks, (
        "Real-looking MACs found in example files (must use synthetic suffix "
        "convention 'XX:XX:XX:00:00:NN' or '02:00:...'):\n" + "\n".join(leaks)
    )


@pytest.mark.parametrize(
    ("mac", "expected"),
    [
        ("60:64:05:00:00:01", True),         # Lutron OUI + zeroed suffix
        ("ec:b5:fa:00:00:02", True),         # Hue OUI + zeroed suffix
        ("78:8a:20:00:00:00", True),         # Ubiquiti OUI + zeroed suffix
        ("60:64:05:42:7a:18", False),        # fabricated Lutron-OUI + non-zeroed suffix (rejected)
        ("ec:b5:fa:42:7a:18", False),        # fabricated Hue-OUI + non-zeroed suffix (rejected)
        ("aa:bb:cc:dd:ee:ff", True),         # canonical fake
        ("02:00:00:00:00:01", True),         # RFC locally-administered
        ("00:00:00:00:00:00", True),         # sentinel
    ],
)
def test_synthetic_mac_classifier(mac: str, expected: bool) -> None:
    assert _is_synthetic_mac(mac) is expected
