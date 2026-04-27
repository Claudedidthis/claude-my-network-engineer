"""Tests for the Upgrade Agent."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from network_engineer.agents.ai_runtime import AIRuntime
from network_engineer.tools.upgrade_agent import (
    _scan_with_data,
    load_catalog,
    render_markdown,
    scan,
    score_device,
    to_json_log_format,
)
from network_engineer.tools.schemas import Severity, UpgradeRecommendation

# ── Catalog loading ───────────────────────────────────────────────────────────

def test_catalog_loads_from_default_path() -> None:
    cat = load_catalog()
    assert "catalog" in cat
    assert "weights" in cat
    assert "severity_bands" in cat


def test_catalog_has_known_devices() -> None:
    cat = load_catalog()
    models = [e["model"] for e in cat["catalog"]]
    assert "UAP-AC-Lite" in models
    assert "FlexHD" in models
    assert "U6 IW" in models
    assert "USW Pro Max 16" in models


# ── Scoring ───────────────────────────────────────────────────────────────────

def _device(model: str, name: str = "test", mac: str = "aa:bb:cc:dd:ee:ff") -> dict[str, Any]:
    return {"model": model, "name": name, "macAddress": mac, "state": "ONLINE"}


def test_score_eol_device_high() -> None:
    cat = load_catalog()
    score, factors, entry = score_device(_device("UAP-AC-Lite"), cat)
    assert score >= 70   # eol(40) + aging(15) + has_successor(20) = 75
    assert "eol" in factors
    assert entry is not None


def test_score_current_gen_device_zero() -> None:
    cat = load_catalog()
    score, factors, entry = score_device(_device("U6 IW"), cat)
    assert score == 0
    assert factors == {}
    assert entry is not None


def test_score_aging_switch_medium() -> None:
    cat = load_catalog()
    score, factors, _ = score_device(_device("US 8 PoE 150W"), cat)
    assert 30 <= score <= 50  # aging(15) + has_successor(20) = 35
    assert "aging" in factors
    assert "has_successor" in factors


def test_score_flex_hd_low() -> None:
    cat = load_catalog()
    score, factors, _ = score_device(_device("FlexHD"), cat)
    assert 15 <= score < 40   # has_successor(20) only
    assert "has_successor" in factors


def test_score_unknown_device_returns_none_entry() -> None:
    cat = load_catalog()
    score, factors, entry = score_device(_device("Unknown-Model-9000"), cat)
    assert score == 0
    assert factors == {}
    assert entry is None


def test_score_high_traffic_multiplier_applied() -> None:
    cat = load_catalog()
    score_low, _, _ = score_device(_device("FlexHD"), cat, client_count=5)
    score_high, factors_high, _ = score_device(_device("FlexHD"), cat, client_count=15)
    assert score_high > score_low
    assert "high_traffic_multiplier_pct" in factors_high


def test_score_capped_at_100() -> None:
    cat = load_catalog()
    # Force a high-traffic UAP-AC-Lite scenario
    score, _, _ = score_device(_device("UAP-AC-Lite"), cat, client_count=50)
    assert score <= 100


def test_match_models_alternate_form() -> None:
    cat = load_catalog()
    score_a, _, _ = score_device(_device("FlexHD"), cat)
    score_b, _, _ = score_device(_device("UAP-FlexHD"), cat)
    assert score_a == score_b


# ── Severity bands ────────────────────────────────────────────────────────────

def test_severity_high_at_70() -> None:
    cat = load_catalog()
    devices = [_device("UAP-AC-Lite", mac="aa:00:00:00:00:01")]
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(devices, [], cat, runtime=runtime)
    assert recs
    assert recs[0].urgency == Severity.HIGH


def test_severity_medium_at_35() -> None:
    cat = load_catalog()
    devices = [_device("US 8 PoE 150W")]
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(devices, [], cat, runtime=runtime)
    assert recs[0].urgency == Severity.LOW or recs[0].urgency == Severity.MEDIUM


def test_severity_low_at_20() -> None:
    cat = load_catalog()
    devices = [_device("FlexHD")]
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(devices, [], cat, runtime=runtime)
    assert recs[0].urgency == Severity.LOW


# ── Threshold filtering ───────────────────────────────────────────────────────

def test_current_gen_devices_emit_no_recommendation() -> None:
    cat = load_catalog()
    devices = [_device("U6 IW"), _device("USW Pro Max 16"), _device("U6 LR")]
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(devices, [], cat, runtime=runtime)
    assert recs == []


def test_unknown_device_emits_no_recommendation() -> None:
    cat = load_catalog()
    devices = [_device("Some-Mystery-Model")]
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(devices, [], cat, runtime=runtime)
    assert recs == []


# ── Done-when: full sweep against fixture devices ─────────────────────────────

def test_full_sweep_against_fixture_produces_four_candidates() -> None:
    """Phase 9 done-when criterion: 4 candidates with scores in the right ballpark."""
    fixture = json.loads(
        Path(__file__).parent.joinpath("fixtures/baseline_snapshot.json").read_text()
    )
    cat = load_catalog()
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(fixture["devices"], fixture.get("clients", []), cat, runtime=runtime)

    candidate_models = [r.device_model for r in recs]
    assert "UAP-AC-Lite" in candidate_models
    assert "FlexHD" in candidate_models
    assert "US 8 PoE 150W" in candidate_models
    assert "US 24 PoE 250W" in candidate_models
    assert len(recs) == 4

    # UAP-AC-Lite should be the highest-scored (EOL)
    by_model = {r.device_model: r for r in recs}
    assert by_model["UAP-AC-Lite"].score >= 70
    assert by_model["UAP-AC-Lite"].urgency == Severity.HIGH
    # FlexHD should be the lowest-scored
    assert by_model["FlexHD"].score < by_model["UAP-AC-Lite"].score


def test_recommendations_sorted_by_score_descending() -> None:
    fixture = json.loads(
        Path(__file__).parent.joinpath("fixtures/baseline_snapshot.json").read_text()
    )
    cat = load_catalog()
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(fixture["devices"], fixture.get("clients", []), cat, runtime=runtime)
    scores = [r.score for r in recs]
    assert scores == sorted(scores, reverse=True)


# ── UpgradeRecommendation structure ───────────────────────────────────────────

def test_recommendation_has_all_required_fields() -> None:
    cat = load_catalog()
    devices = [_device("UAP-AC-Lite")]
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(devices, [], cat, runtime=runtime)
    r = recs[0]
    assert isinstance(r, UpgradeRecommendation)
    assert r.device_name == "test"
    assert r.device_model == "UAP-AC-Lite"
    assert r.recommendation == "replace_device"
    assert r.successor_model == "U7 Pro Wall"
    assert r.successor_msrp_usd == 179
    assert r.score > 0
    assert r.factors
    assert r.urgency == Severity.HIGH


def test_recommendation_reason_contains_key_phrases() -> None:
    cat = load_catalog()
    devices = [_device("UAP-AC-Lite")]
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(devices, [], cat, runtime=runtime)
    assert "end-of-life" in recs[0].reason.lower()


def test_no_ai_narrative_when_runtime_disabled() -> None:
    cat = load_catalog()
    devices = [_device("UAP-AC-Lite")]
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(devices, [], cat, runtime=runtime)
    assert recs[0].narrative == ""


# ── Mocked AI narrative ───────────────────────────────────────────────────────

class _FakeUsage:
    input_tokens = 200
    output_tokens = 80
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, response: str) -> None:
        self.response = response
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.last_kwargs = kwargs
        return _FakeMessage(self.response)


class _FakeAnthropic:
    def __init__(self, response: str) -> None:
        self.messages = _FakeMessages(response)


def test_ai_narrative_attached_when_runtime_enabled() -> None:
    response = json.dumps({
        "score": 78,
        "narrative": "EOL Wi-Fi 5 AP serving the living room — replace soon.",
    })
    fake = _FakeAnthropic(response)
    runtime = AIRuntime(enabled=True, client=fake)

    cat = load_catalog()
    devices = [_device("UAP-AC-Lite")]
    recs = _scan_with_data(devices, [], cat, runtime=runtime)
    assert "EOL Wi-Fi 5" in recs[0].narrative


def test_ai_uses_haiku_model_for_upgrade_scoring() -> None:
    response = json.dumps({"score": 50, "narrative": "ok"})
    fake = _FakeAnthropic(response)
    runtime = AIRuntime(enabled=True, client=fake)
    cat = load_catalog()
    _scan_with_data([_device("UAP-AC-Lite")], [], cat, runtime=runtime)
    assert fake.messages.last_kwargs["model"] == "claude-haiku-4-5-20251001"


def test_ai_failure_does_not_break_sweep() -> None:
    class BrokenAnthropic:
        class Messages:
            def create(self, **kwargs: Any) -> Any:
                raise RuntimeError("network down")
        messages = Messages()

    runtime = AIRuntime(enabled=True, client=BrokenAnthropic())
    cat = load_catalog()
    recs = _scan_with_data([_device("UAP-AC-Lite")], [], cat, runtime=runtime)
    # Sweep still produces the recommendation; narrative is empty
    assert len(recs) == 1
    assert recs[0].narrative == ""


# ── Markdown / JSON rendering ─────────────────────────────────────────────────

def test_render_markdown_empty() -> None:
    assert "No upgrade candidates" in render_markdown([])


def test_render_markdown_includes_table_and_details() -> None:
    cat = load_catalog()
    runtime = AIRuntime(enabled=False)
    fixture = json.loads(
        Path(__file__).parent.joinpath("fixtures/baseline_snapshot.json").read_text()
    )
    recs = _scan_with_data(fixture["devices"], fixture.get("clients", []), cat, runtime=runtime)
    md = render_markdown(recs)
    assert "# Upgrade Recommendations" in md
    assert "UAP-AC-Lite" in md
    assert "U7 Pro Wall" in md
    assert "$179" in md
    assert "Score factors" in md


def test_to_json_log_format_returns_serializable_list() -> None:
    cat = load_catalog()
    devices = [_device("UAP-AC-Lite")]
    runtime = AIRuntime(enabled=False)
    recs = _scan_with_data(devices, [], cat, runtime=runtime)
    out = to_json_log_format(recs)
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["device_model"] == "UAP-AC-Lite"
    assert out[0]["urgency"] == "HIGH"
    # Roundtrip-safe
    json.dumps(out)


# ── High-level scan() with real client mock ──────────────────────────────────

class _MockUnifi:
    def __init__(self, devices: list[dict[str, Any]], clients: list[dict[str, Any]]) -> None:
        self._devices = devices
        self._clients = clients

    def get_devices(self) -> list[dict[str, Any]]:
        return self._devices

    def get_clients(self) -> list[dict[str, Any]]:
        return self._clients


def test_scan_with_mock_client_produces_recommendations() -> None:
    devices = [_device("UAP-AC-Lite"), _device("U6 IW", "Modern AP")]
    client = _MockUnifi(devices, [])
    recs = scan(client)
    assert len(recs) == 1
    assert recs[0].device_model == "UAP-AC-Lite"


# ── Live integration ──────────────────────────────────────────────────────────

_LIVE = pytest.mark.skipif(
    not os.getenv("UNIFI_HOST"), reason="UNIFI_HOST not set — live tests require home LAN"
)


@_LIVE
def test_live_upgrade_scan() -> None:
    from network_engineer.tools.unifi_client import UnifiClient

    client = UnifiClient(use_fixtures=False)
    recs = scan(client)
    # Live network has UAP-AC-Lite → at least one HIGH recommendation expected
    assert any(r.urgency == Severity.HIGH for r in recs), "Expected HIGH on UAP-AC-Lite"
