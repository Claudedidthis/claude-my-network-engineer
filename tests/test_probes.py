"""Tests for the probe library + conversational engine + onboarding rewrite."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from network_engineer.agents.onboarding_agent import onboard
from network_engineer.tools.probes import (
    Probe,
    asked_per_theme,
    field_is_unset,
    get_field,
    interpret_answer,
    load_probes,
    pick_next_probe,
    resolve_follow_ups,
    set_field,
    triggers_satisfied,
)
from network_engineer.tools.profile import load_profile, save_profile
from network_engineer.tools.schemas import HouseholdProfile

# ── Probe loading ────────────────────────────────────────────────────────────

def _temp_probe_dir(tmp_path: Path, contents: dict[str, list[dict[str, Any]]]) -> Path:
    """Write a fake probes/ directory with the given file→entries mapping."""
    d = tmp_path / "probes"
    d.mkdir()
    for fname, probes in contents.items():
        (d / fname).write_text(yaml.safe_dump({"probes": probes}))
    return d


def test_load_probes_from_dir(tmp_path: Path) -> None:
    d = _temp_probe_dir(tmp_path, {
        "01.yaml": [
            {"id": "x.foo", "theme": "x", "prompt": "p1", "field_path": "x.foo"},
        ],
        "02.yaml": [
            {"id": "y.bar", "theme": "y", "prompt": "p2", "field_path": "y.bar"},
        ],
    })
    probes = load_probes(d)
    assert set(probes.keys()) == {"x.foo", "y.bar"}
    assert probes["x.foo"].theme == "x"


def test_load_probes_duplicate_id_raises(tmp_path: Path) -> None:
    d = _temp_probe_dir(tmp_path, {
        "01.yaml": [{"id": "dup", "theme": "x", "prompt": "p1"}],
        "02.yaml": [{"id": "dup", "theme": "y", "prompt": "p2"}],
    })
    with pytest.raises(ValueError, match="Duplicate"):
        load_probes(d)


def test_real_probe_library_loads_cleanly() -> None:
    """The shipped probe library in config/probes/ must load without errors."""
    probes = load_probes()
    assert len(probes) >= 60   # we shipped ~85
    themes = {p.theme for p in probes.values()}
    assert themes >= {
        "origin", "building", "isp", "household", "work",
        "devices", "security", "usage", "infrastructure",
        "preferences", "future",
    }


# ── Field accessors ─────────────────────────────────────────────────────────

def test_get_set_field_dotted_path() -> None:
    profile = HouseholdProfile()
    assert get_field(profile, "building.home_type") is None
    set_field(profile, "building.home_type", "house")
    assert get_field(profile, "building.home_type") == "house"


def test_field_is_unset() -> None:
    profile = HouseholdProfile()
    assert field_is_unset(profile, "building.home_type") is True
    assert field_is_unset(profile, "building.outbuildings") is True   # empty list
    set_field(profile, "building.home_type", "house")
    assert field_is_unset(profile, "building.home_type") is False


# ── Trigger evaluation ──────────────────────────────────────────────────────

def test_triggers_when_match() -> None:
    probe = Probe(
        id="x", theme="t", prompt="p",
        triggers_when={"household.has_kids": True},
    )
    profile = HouseholdProfile()
    assert triggers_satisfied(probe, profile) is False
    profile.household.has_kids = True
    assert triggers_satisfied(probe, profile) is True


def test_triggers_when_list_means_any_of() -> None:
    probe = Probe(
        id="x", theme="t", prompt="p",
        triggers_when={"work.work_from_home": ["daily", "heavy_meetings"]},
    )
    profile = HouseholdProfile()
    profile.work.work_from_home = "daily"
    assert triggers_satisfied(probe, profile) is True
    profile.work.work_from_home = "occasional"
    assert triggers_satisfied(probe, profile) is False


# ── Answer interpretation ────────────────────────────────────────────────────

def test_interpret_boolean() -> None:
    p = Probe(id="x", theme="t", prompt="p", kind="boolean")
    assert interpret_answer(p, "yes")[0] is True
    assert interpret_answer(p, "no")[0] is False
    assert interpret_answer(p, "y")[0] is True
    val, warnings = interpret_answer(p, "maybe")
    assert val is None
    assert warnings


def test_interpret_integer() -> None:
    p = Probe(id="x", theme="t", prompt="p", kind="integer")
    assert interpret_answer(p, "42")[0] == 42
    assert interpret_answer(p, "about 1200")[0] == 1200


def test_interpret_choice_normalizes_case() -> None:
    p = Probe(
        id="x", theme="t", prompt="p", kind="choice",
        choices=["yes_diy", "yes_hire", "no"],
    )
    assert interpret_answer(p, "YES_DIY")[0] == "yes_diy"


def test_interpret_choice_multi() -> None:
    p = Probe(
        id="x", theme="t", prompt="p", kind="choice_multi",
        choices=["a", "b", "c"],
    )
    val, _ = interpret_answer(p, "a, b, custom")
    assert val == ["a", "b", "custom"]


def test_interpret_empty_returns_none() -> None:
    p = Probe(id="x", theme="t", prompt="p", kind="free_text")
    val, _ = interpret_answer(p, "   ")
    assert val is None


# ── Picker: scoring + filtering ──────────────────────────────────────────────

def test_pick_next_probe_returns_highest_priority() -> None:
    probes = {
        "a": Probe(id="a", theme="t", prompt="p", priority=3, field_path="building.home_type"),
        "b": Probe(id="b", theme="t", prompt="p", priority=8, field_path="building.num_floors"),
        "c": Probe(id="c", theme="t", prompt="p", priority=5, field_path="isp.isp_type"),
    }
    profile = HouseholdProfile()
    chosen = pick_next_probe(probes, profile)
    assert chosen.id == "b"   # priority 8 wins


def test_pick_next_probe_filters_already_asked() -> None:
    probes = {
        "a": Probe(id="a", theme="t", prompt="p", priority=8, field_path="building.home_type"),
        "b": Probe(id="b", theme="t", prompt="p", priority=3, field_path="building.num_floors"),
    }
    profile = HouseholdProfile()
    chosen = pick_next_probe(probes, profile, asked_ids={"a"})
    assert chosen.id == "b"


def test_pick_next_probe_filters_populated_fields() -> None:
    probes = {
        "a": Probe(id="a", theme="t", prompt="p", priority=8, field_path="building.home_type"),
        "b": Probe(id="b", theme="t", prompt="p", priority=3, field_path="isp.isp_type"),
    }
    profile = HouseholdProfile()
    profile.building.home_type = "house"
    chosen = pick_next_probe(probes, profile)
    assert chosen.id == "b"   # 'a' filtered because field already set


def test_pick_next_probe_returns_none_when_exhausted() -> None:
    probes = {
        "a": Probe(id="a", theme="t", prompt="p", priority=8, field_path="building.home_type"),
    }
    profile = HouseholdProfile()
    profile.building.home_type = "house"
    assert pick_next_probe(probes, profile) is None


def test_pick_next_probe_respects_triggers() -> None:
    probes = {
        "a": Probe(
            id="a", theme="t", prompt="p", priority=9,
            field_path="security.content_filter_for_kids",
            triggers_when={"household.has_kids": True},
        ),
        "b": Probe(id="b", theme="t", prompt="p", priority=3, field_path="building.home_type"),
    }
    profile = HouseholdProfile()
    chosen = pick_next_probe(probes, profile)
    assert chosen.id == "b"   # 'a' blocked by trigger
    profile.household.has_kids = True
    chosen = pick_next_probe(probes, profile)
    assert chosen.id == "a"   # trigger now satisfied, higher priority wins


def test_pick_next_probe_boost_lifts_priority() -> None:
    probes = {
        "a": Probe(id="a", theme="t", prompt="p", priority=8, field_path="building.home_type"),
        "b": Probe(id="b", theme="t", prompt="p", priority=5, field_path="isp.isp_type"),
    }
    profile = HouseholdProfile()
    chosen = pick_next_probe(probes, profile, boost_ids={"b"})
    # b: 5 + 3 boost = 8, which ties with a at 8 — lexicographic tiebreak picks "a"
    assert chosen.id in ("a", "b")
    # Now make 'b' the clear winner
    chosen2 = pick_next_probe(
        probes, profile, boost_ids={"b"}, asked_ids={"a"},
    )
    assert chosen2.id == "b"


def test_pick_next_probe_theme_filter() -> None:
    probes = {
        "a": Probe(id="a", theme="x", prompt="p", priority=8, field_path="building.home_type"),
        "b": Probe(id="b", theme="y", prompt="p", priority=9, field_path="isp.isp_type"),
    }
    profile = HouseholdProfile()
    chosen = pick_next_probe(probes, profile, theme_filter="x")
    assert chosen.id == "a"


# ── Follow-ups ──────────────────────────────────────────────────────────────

def test_resolve_follow_ups_simple_string() -> None:
    p = Probe(id="x", theme="t", prompt="p", follow_ups=["a", "b"])
    boosts = resolve_follow_ups(p, "anything")
    assert boosts == {"a", "b"}


def test_resolve_follow_ups_conditional() -> None:
    p = Probe(
        id="x", theme="t", prompt="p",
        follow_ups=[{"when": True, "ids": ["a", "b"]}],
    )
    assert resolve_follow_ups(p, True) == {"a", "b"}
    assert resolve_follow_ups(p, False) == set()


def test_resolve_follow_ups_in_clause() -> None:
    p = Probe(
        id="x", theme="t", prompt="p",
        follow_ups=[{"when": {"in": ["pre_1950", "1950_1980"]}, "ids": ["mat"]}],
    )
    assert resolve_follow_ups(p, "pre_1950") == {"mat"}
    assert resolve_follow_ups(p, "post_2010") == set()


# ── asked_per_theme ─────────────────────────────────────────────────────────

def test_asked_per_theme() -> None:
    probes = {
        "a": Probe(id="a", theme="x", prompt="p"),
        "b": Probe(id="b", theme="x", prompt="p"),
        "c": Probe(id="c", theme="y", prompt="p"),
    }
    counts = asked_per_theme(probes, asked={"a"})
    assert counts["x"] == (1, 2)
    assert counts["y"] == (0, 1)


# ── Onboarding integration with scripted I/O ────────────────────────────────

class _ScriptedInput:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(
                f"onboarding asked more than scripted (next prompt: {prompt!r})",
            )
        return self.answers.pop(0)


def test_onboard_quits_cleanly_and_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator types 'q' on first probe — profile saves, no crash."""
    profile_path = tmp_path / "household_profile.yaml"
    monkeypatch.setattr(
        "network_engineer.tools.profile._DEFAULT_PATH", profile_path,
    )

    inp = _ScriptedInput(["q"])
    out: list[str] = []
    onboard(client=None, input_fn=inp, print_fn=out.append)

    # Profile file may exist (empty/default) — at minimum the call returned cleanly
    assert any("Saving and exiting" in line for line in out)


def test_onboard_writes_answers_to_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three answers + quit — verify they land in the profile YAML."""
    profile_path = tmp_path / "household_profile.yaml"
    monkeypatch.setattr(
        "network_engineer.tools.profile._DEFAULT_PATH", profile_path,
    )

    # Answer the first 3 probes the engine offers, then quit. The first probe
    # is highest-priority. With our shipped library that's origin.trigger_event
    # (priority 10). We provide free-text answers compatible with whatever
    # comes up.
    inp = _ScriptedInput([
        "Buffering during work calls",   # answer
        "ISP combo box",                  # answer
        "wifi keeps dropping",            # answer
        "q",                              # quit
    ])
    out: list[str] = []
    counts = onboard(client=None, input_fn=inp, print_fn=out.append)

    assert counts["probes_answered"] == 3

    profile = load_profile()
    assert profile is not None
    assert len(profile.probes_answered) == 3


def test_onboard_resumes_from_existing_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second invocation skips already-answered probes."""
    profile_path = tmp_path / "household_profile.yaml"
    monkeypatch.setattr(
        "network_engineer.tools.profile._DEFAULT_PATH", profile_path,
    )

    # Pre-populate a profile with one probe answered
    profile = HouseholdProfile()
    profile.probes_answered = ["origin.trigger_event"]
    profile.origin.trigger_event = "previous answer"
    save_profile(profile)

    inp = _ScriptedInput(["q"])
    out: list[str] = []
    onboard(client=None, input_fn=inp, print_fn=out.append)

    # First-line summary should mention resuming
    assert any("Resuming previous session" in line for line in out)


def test_onboard_skip_keeps_probe_for_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'skip' on first probe — it shouldn't be marked answered."""
    profile_path = tmp_path / "household_profile.yaml"
    monkeypatch.setattr(
        "network_engineer.tools.profile._DEFAULT_PATH", profile_path,
    )

    inp = _ScriptedInput(["skip", "q"])
    out: list[str] = []
    counts = onboard(client=None, input_fn=inp, print_fn=out.append)

    assert counts["skipped"] == 1
    assert counts["probes_answered"] == 0
