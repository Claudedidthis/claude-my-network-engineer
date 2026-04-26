"""Probe library — the conversational substrate of the onboarding agent.

A probe is a single question the agent might ask, paired with metadata about
how to interpret the answer, when it's relevant, and what follow-ups it
unlocks. The library is YAML-on-disk in `config/probes/<theme>.yaml` so the
open-source community can contribute probes for use cases the core team
hasn't imagined (the medical-device probe, the third-party-vendor probe,
the rental-tolerance probe — all natural community contributions).

Probe schema (per entry):
  id: unique stable identifier  ── building.construction_era
  theme: which submodel — building | isp | household | work | devices |
                          security | usage | infrastructure | preferences |
                          future | origin
  prompt: the question text — conversational, not form-like
  kind: how to interpret the answer
        - free_text: any string
        - choice: one of a fixed list
        - choice_multi: comma-separated subset of a fixed list
        - integer / boolean
        - structured: free text + AI extraction (advanced; later phase)
  choices: optional list — surfaces hints, but operator can free-form
  field_path: dotted path into the profile model where the answer lives
              e.g. building.construction_era, devices.has_solar
              For structured probes, may be a list of paths or a path prefix
              that the AI extractor populates.
  triggers_when: optional dict of {field_path: expected_value}
                 Probe is only offered if all conditions match the current
                 profile state. Empty means always-eligible.
  priority: 1-10 baseline information-gain estimate (higher = ask sooner)
  surfaces_concern: tag for what kind of decision this informs —
                    rf_planning, vlan_design, parental_controls,
                    third_party_isolation, conferencing_priority, etc.
  follow_ups: list of probe IDs to elevate priority on after this answer
              (or, conditionally, only when a specific value is given)

Loading: the engine reads `config/probes/*.yaml`, concatenates entries, and
builds an index by id. Duplicates raise. Operators can override a core probe
by writing the same id in a higher-precedence directory (TODO).

Contribution-friendly: a single YAML file with one or more probes is a
complete unit. PRs can add, edit, or remove probes without touching code.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PROBE_DIR = _REPO_ROOT / "config" / "probes"


def _to_str(v: Any) -> str:
    """Coerce a YAML-parsed value to a string. yes→'yes', True→'yes', etc."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


@dataclass
class Probe:
    id: str
    theme: str
    prompt: str
    kind: str = "free_text"
    field_path: str | list[str] = ""
    choices: list[str] = field(default_factory=list)
    triggers_when: dict[str, Any] = field(default_factory=dict)
    priority: int = 5
    surfaces_concern: str = ""
    follow_ups: list[str | dict[str, Any]] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Probe:
        # YAML parses bare `yes`/`no`/`true`/`false` as booleans — coerce
        # all choice tokens to strings so display + comparison stay consistent.
        choices = [_to_str(c) for c in data.get("choices", [])]
        return cls(
            id=data["id"],
            theme=data["theme"],
            prompt=data["prompt"],
            kind=data.get("kind", "free_text"),
            field_path=data.get("field_path", ""),
            choices=choices,
            triggers_when=dict(data.get("triggers_when", {})),
            priority=int(data.get("priority", 5)),
            surfaces_concern=data.get("surfaces_concern", ""),
            follow_ups=list(data.get("follow_ups", [])),
            notes=data.get("notes", ""),
        )


# ── Library loading ──────────────────────────────────────────────────────────

def load_probes(probe_dir: Path | None = None) -> dict[str, Probe]:
    """Load every probe YAML file in *probe_dir*; returns id → Probe."""
    d = probe_dir or _DEFAULT_PROBE_DIR
    probes: dict[str, Probe] = {}
    if not d.exists():
        return probes
    for path in sorted(d.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text()) or {}
        for entry in raw.get("probes", []):
            probe = Probe.from_dict(entry)
            if probe.id in probes:
                raise ValueError(
                    f"Duplicate probe id {probe.id!r} (already loaded from earlier file)",
                )
            probes[probe.id] = probe
    return probes


# ── Profile state lookup helpers ─────────────────────────────────────────────

def get_field(profile: Any, path: str) -> Any:
    """Read a dotted-path field out of a profile pydantic model."""
    if not path:
        return None
    obj: Any = profile
    for part in path.split("."):
        if obj is None:
            return None
        obj = getattr(obj, part, None)
    return obj


def set_field(profile: Any, path: str, value: Any) -> None:
    """Set a dotted-path field on a profile pydantic model.

    The model is mutable (Pydantic v2 default). Walks the path, calling
    setattr on the final segment.
    """
    if not path:
        return
    parts = path.split(".")
    obj: Any = profile
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


def field_is_unset(profile: Any, path: str) -> bool:
    """True if the field at *path* is empty/None/empty-list."""
    val = get_field(profile, path)
    if val is None:
        return True
    if isinstance(val, (list, str)) and len(val) == 0:
        return True
    return False


# ── Trigger evaluation ───────────────────────────────────────────────────────

def triggers_satisfied(probe: Probe, profile: Any) -> bool:
    """True if the probe's preconditions are all met in the current profile."""
    for path, expected in probe.triggers_when.items():
        actual = get_field(profile, path)
        # Special tokens
        if expected == "__set__":
            if actual is None or (isinstance(actual, (list, str)) and len(actual) == 0):
                return False
            continue
        if expected == "__unset__":
            if actual is not None and not (isinstance(actual, (list, str)) and len(actual) == 0):
                return False
            continue
        if isinstance(expected, list):
            # any-of match for lists
            if actual not in expected:
                return False
            continue
        if expected != actual:
            return False
    return True


# ── Conversational engine — pick next probe ──────────────────────────────────

def pick_next_probe(
    probes: dict[str, Probe],
    profile: Any,
    *,
    boost_ids: set[str] | None = None,
    asked_ids: set[str] | None = None,
    theme_filter: str | None = None,
) -> Probe | None:
    """Return the next-most-informative probe given current state, or None.

    Scoring (higher wins):
      base = probe.priority (1-10)
      +3 if probe.id is in boost_ids (recently elevated by a follow-up)
      +2 if probe's theme has 0 fields filled (cold-start theme)
      -100 if probe.id has already been asked
      -∞ (filtered out) if triggers_satisfied is False
      -∞ (filtered out) if the target field_path is already populated
    """
    asked = asked_ids or set()
    boost = boost_ids or set()
    candidates: list[tuple[int, Probe]] = []

    # Theme-coverage bonus: count themes with no probes answered yet
    cold_themes = _cold_themes(probes, asked)

    for probe in probes.values():
        if probe.id in asked:
            continue
        if theme_filter and probe.theme != theme_filter:
            continue
        if not triggers_satisfied(probe, profile):
            continue
        # Skip probes whose target field is already populated (only for
        # single-path probes; structured probes may have empty field_path)
        if isinstance(probe.field_path, str) and probe.field_path:
            if not field_is_unset(profile, probe.field_path):
                continue

        score = probe.priority
        if probe.id in boost:
            score += 3
        if probe.theme in cold_themes:
            score += 2
        candidates.append((score, probe))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], x[1].id))
    return candidates[0][1]


def _cold_themes(probes: dict[str, Probe], asked: set[str]) -> set[str]:
    """Return themes that have had zero probes answered yet."""
    by_theme: dict[str, set[str]] = {}
    for p in probes.values():
        by_theme.setdefault(p.theme, set()).add(p.id)
    cold: set[str] = set()
    for theme, ids in by_theme.items():
        if not (ids & asked):
            cold.add(theme)
    return cold


# ── Answer interpretation ────────────────────────────────────────────────────

_BOOL_TRUE = {"y", "yes", "true", "t", "1", "yeah", "yep", "yup", "sure"}
_BOOL_FALSE = {"n", "no", "false", "f", "0", "nope", "nah"}


def interpret_answer(probe: Probe, raw: str) -> tuple[Any, list[str]]:
    """Convert the raw operator answer to the appropriate Python value.

    Returns (value, warnings). Empty raw input returns (None, []) so the
    probe is left unanswered.
    """
    s = raw.strip()
    if not s:
        return None, []

    warnings: list[str] = []

    if probe.kind == "boolean":
        low = s.lower()
        if low in _BOOL_TRUE:
            return True, warnings
        if low in _BOOL_FALSE:
            return False, warnings
        warnings.append(f"didn't recognize {raw!r} as yes/no — treating as unset")
        return None, warnings

    if probe.kind == "integer":
        digits = "".join(c for c in s if c.isdigit())
        if not digits:
            warnings.append(f"no digits in {raw!r} — treating as unset")
            return None, warnings
        return int(digits), warnings

    if probe.kind == "choice":
        # match against choices case-insensitively; allow free-form override
        lower_choices = {c.lower(): c for c in probe.choices}
        if s.lower() in lower_choices:
            return lower_choices[s.lower()], warnings
        if probe.choices:
            warnings.append(f"{raw!r} not in suggested choices — accepting as free text")
        return s, warnings

    if probe.kind == "choice_multi":
        items = [piece.strip() for piece in s.split(",") if piece.strip()]
        return items, warnings

    # free_text or structured
    return s, warnings


# ── Resolve follow-up boosts ─────────────────────────────────────────────────

def resolve_follow_ups(probe: Probe, answer: Any) -> set[str]:
    """Return the set of probe IDs whose priority should be boosted.

    follow_ups can be:
      - a string (probe id) — always boost
      - a dict {when: <value>, ids: [<ids>]} — boost only when answer matches
    """
    boosts: set[str] = set()
    for f in probe.follow_ups:
        if isinstance(f, str):
            boosts.add(f)
        elif isinstance(f, dict):
            when = f.get("when")
            ids = f.get("ids", [])
            if when is None:
                boosts.update(ids)
            else:
                if _matches(when, answer):
                    boosts.update(ids)
    return boosts


def _matches(when: Any, answer: Any) -> bool:
    if isinstance(when, list):
        return answer in when
    if isinstance(when, dict):
        if "in" in when:
            return answer in when["in"]
        if "contains" in when and isinstance(answer, list):
            return when["contains"] in answer
        if "is" in when:
            return answer == when["is"]
    return when == answer


# ── Theme display + counts ──────────────────────────────────────────────────

def asked_per_theme(
    probes: dict[str, Probe], asked: set[str],
) -> dict[str, tuple[int, int]]:
    """For each theme, return (asked_count, total_count)."""
    counts: dict[str, list[int]] = {}
    for p in probes.values():
        bucket = counts.setdefault(p.theme, [0, 0])
        bucket[1] += 1
        if p.id in asked:
            bucket[0] += 1
    return {t: (c[0], c[1]) for t, c in counts.items()}


def remaining_in_theme(
    probes: dict[str, Probe], theme: str, profile: Any, asked: set[str],
) -> int:
    """How many probes are still actually askable for *theme*?"""
    return sum(
        1 for p in probes.values()
        if p.theme == theme
        and p.id not in asked
        and triggers_satisfied(p, profile)
    )


# Type alias used by the conversational engine's caller hooks
AnswerInterpreter = Callable[[Probe, str], tuple[Any, list[str]]]
