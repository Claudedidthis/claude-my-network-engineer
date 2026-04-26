"""Onboarding agent — probe-driven conversation that builds the household profile.

Replaces the earlier linear-form walkthrough with a proper conversational
engine. Probes are picked one at a time by the engine in tools/probes.py based
on:
  • baseline priority (higher = more informative)
  • cold-theme bonus (themes with zero answers get nudged early)
  • follow-up boosts (probes triggered by recent answers get +3)
  • field-already-populated filter (don't re-ask)
  • triggers_satisfied filter (skip probes whose preconditions aren't met)

The conversation has natural rhythm:
  • Origin probes go first (sets framing)
  • Every 5 probes the agent asks "want to keep going / switch theme / stop?"
  • Operator can switch themes or quit at any prompt
  • Progress is saved after every answer — fully resumable across sessions

This is designed to feel like a conversation, not a form. When the operator
volunteers something the agent isn't asking about (e.g., mentions solar in an
unrelated answer), follow-up probes get boosted. The probe library itself is
the contribution surface: PRs add `config/probes/<theme>.yaml` files.

Heritage walkthrough (asking about existing config artifacts) lives in this
module too — it runs after profile capture for operators in heritage mode.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from network_engineer.tools.logging_setup import get_logger
from network_engineer.tools.origin_stories import OriginStoryRegistry
from network_engineer.tools.probes import (
    Probe,
    asked_per_theme,
    interpret_answer,
    load_probes,
    pick_next_probe,
    resolve_follow_ups,
    set_field,
)
from network_engineer.tools.profile import has_profile, load_profile, save_profile
from network_engineer.tools.schemas import HouseholdProfile, OriginStory

log = get_logger("agents.onboarding")

_DEFAULT_INPUT: Callable[[str], str] = input


# ── Public entry point ────────────────────────────────────────────────────────

def onboard(
    client: Any | None = None,
    *,
    input_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] | None = None,
    max_probes_per_session: int = 50,
    check_in_every: int = 5,
) -> dict[str, int]:
    """Run the onboarding conversation. Returns counts of what was captured."""
    ask = input_fn or _DEFAULT_INPUT
    say = print_fn or print

    say("\n" + "=" * 64)
    say("ClaudeMyNetworkEngineer — onboarding")
    say("=" * 64)
    say("This is a conversation, not a form. The agent will ask probes that")
    say("inform downstream recommendations. You can:")
    say("  • answer normally")
    say("  • type 'skip' to skip a probe (it'll come back later)")
    say("  • type 'theme' to switch to a different theme")
    say("  • type 'q' or 'quit' to stop and save progress")
    say("Progress is saved after every answer — you can re-run anytime.\n")

    profile = load_profile() if has_profile() else HouseholdProfile()
    probes = load_probes()
    if not probes:
        say("ERROR: no probes loaded — config/probes/ is empty.")
        return {"probes_answered": 0, "origin_stories": 0}

    asked: set[str] = set(profile.probes_answered)
    boost_ids: set[str] = set()
    counts = {"probes_answered": 0, "origin_stories": 0, "skipped": 0}

    if asked:
        say(f"Resuming previous session — {len(asked)} probe(s) already answered.\n")

    theme_filter: str | None = None
    asked_this_session = 0

    try:
        while asked_this_session < max_probes_per_session:
            probe = pick_next_probe(
                probes, profile,
                boost_ids=boost_ids, asked_ids=asked,
                theme_filter=theme_filter,
            )
            if probe is None:
                if theme_filter is not None:
                    say(f"\n  No more probes available for theme {theme_filter!r}.")
                    theme_filter = None
                    continue
                say("\n  ✅  No more applicable probes — profile is as complete as it gets.")
                break

            say("\n" + "─" * 64)
            say(f"[{probe.theme}]  ({probe.id})")
            say("─" * 64)
            for line in probe.prompt.strip().split("\n"):
                say("  " + line)
            if probe.choices:
                hint = " / ".join(probe.choices[:6])
                more = "" if len(probe.choices) <= 6 else f" (+{len(probe.choices)-6} more)"
                say(f"\n  Suggested choices: {hint}{more}")

            raw = ask("\n> ").strip()

            # Meta-commands
            if raw.lower() in ("q", "quit", "exit"):
                raise _QuitError
            if raw.lower() == "skip":
                counts["skipped"] += 1
                say("  (skipped — will come back later)")
                continue
            if raw.lower() == "theme":
                theme_filter = _pick_theme(probes, asked, profile, ask, say)
                continue

            value, warnings = interpret_answer(probe, raw)
            for w in warnings:
                say(f"  ⚠  {w}")

            # Apply the answer to the profile
            if value is not None and isinstance(probe.field_path, str) and probe.field_path:
                set_field(profile, probe.field_path, value)

            # Mark answered + propagate follow-up boosts
            asked.add(probe.id)
            boost_ids |= resolve_follow_ups(probe, value)
            profile.probes_answered = sorted(asked)
            save_profile(profile)
            counts["probes_answered"] += 1
            asked_this_session += 1

            say("  → saved.")

            # Check-in every N probes
            if asked_this_session % check_in_every == 0:
                if not _check_in(probes, asked, profile, ask, say):
                    raise _QuitError

    except _QuitError:
        save_profile(profile)
        say("\n  Saving and exiting. Run `nye onboard` again any time.")

    # Heritage walkthrough only if mode == heritage and a client is available
    if profile.mode == "heritage" and client is not None:
        try:
            origins = _heritage_walkthrough(client, ask, say)
            counts["origin_stories"] += origins
        except _QuitError:
            pass

    say("\n" + "=" * 64)
    say(_progress_line(probes, asked))
    say("=" * 64)
    return counts


# ── Helpers ──────────────────────────────────────────────────────────────────

class _QuitError(Exception):
    """Operator typed 'q' — exit cleanly, saving progress."""


def _check_in(
    probes: dict[str, Probe], asked: set[str], profile: HouseholdProfile,
    ask: Callable[[str], str], say: Callable[[str], None],
) -> bool:
    """Periodic check-in. Returns False if operator wants to stop."""
    say("\n" + "─" * 64)
    say(_progress_line(probes, asked))
    answer = ask(
        "\n  Keep going? [Enter=yes, t=switch theme, q=stop]: ",
    ).strip().lower()
    if answer in ("q", "quit", "stop", "n", "no"):
        return False
    if answer == "t":
        # Caller will see theme_filter set on next iteration via separate path.
        # (Implementation detail: this current function can't set the closure
        # variable; instead we just print the theme list and let the operator
        # type 'theme' again at the next prompt.)
        say("  At the next prompt, type 'theme' to choose one.")
    return True


def _pick_theme(
    probes: dict[str, Probe], asked: set[str], profile: HouseholdProfile,
    ask: Callable[[str], str], say: Callable[[str], None],
) -> str | None:
    """Show theme menu, return selected theme name or None for 'all'."""
    counts = asked_per_theme(probes, asked)
    say("\n  Themes:")
    themes = sorted(counts.keys())
    for i, t in enumerate(themes, start=1):
        ans, total = counts[t]
        say(f"    {i:2d}. {t}  ({ans}/{total} answered)")
    say(f"    {len(themes) + 1:2d}. all themes (default — return to mixed mode)")
    raw = ask("  > ").strip()
    if not raw:
        return None
    try:
        n = int(raw)
        if 1 <= n <= len(themes):
            return themes[n - 1]
    except ValueError:
        # match by name prefix
        for t in themes:
            if t.startswith(raw.lower()):
                return t
    return None


def _progress_line(probes: dict[str, Probe], asked: set[str]) -> str:
    counts = asked_per_theme(probes, asked)
    parts = []
    for theme in sorted(counts.keys()):
        a, t = counts[theme]
        parts.append(f"{theme}: {a}/{t}")
    total_a = sum(c[0] for c in counts.values())
    total_t = sum(c[1] for c in counts.values())
    return f"Progress — {total_a}/{total_t} probes overall · " + " · ".join(parts)


# ── Heritage walkthrough (unchanged from prior implementation) ──────────────

def _heritage_walkthrough(
    client: Any, ask: Callable[[str], str], say: Callable[[str], None],
) -> int:
    """Walk through every non-default config artifact, capturing origin stories."""
    say("\n" + "=" * 64)
    say("Heritage walkthrough — existing config artifacts")
    say("=" * 64)
    say("For each non-default network/port-forward/firewall-rule, you'll be")
    say("asked 'why does this exist?' Your answer is recorded as an origin")
    say("story so the agents respect it instead of recommending it for removal.\n")

    registry = OriginStoryRegistry.load()
    added = 0

    for name, items, kind, label_fn in [
        (
            "Networks", _safe_call(client.get_networks),
            "network", lambda n: f"{n.get('name', '?')}  (VLAN {n.get('vlan', '?')})",
        ),
        (
            "Port forwards", _safe_call(client.get_port_forwards),
            "port_forward",
            lambda f: (f"{f.get('name', '?')}  "
                       f"(port {f.get('dst_port', '?')} → "
                       f"{f.get('fwd', '?')}:{f.get('fwd_port', '?')})"),
        ),
        (
            "Firewall rules", _safe_call(client.get_firewall_rules),
            "firewall_rule", lambda r: r.get("name", "?"),
        ),
    ]:
        if name == "Networks":
            items = [n for n in items if (n.get("name") or "").lower() != "default"]
        if not items:
            continue
        say(f"\n--- {name}: {len(items)} ---\n")
        for item in items:
            key = (
                item.get("name", "?") if kind in ("network", "port_forward")
                else (item.get("_id") or item.get("name", "?"))
            )
            if registry.has(kind, key):
                continue
            say("─" * 60)
            say(label_fn(item))
            say("─" * 60)
            answer = ask("  Annotate this? [y/N/q]: ").strip().lower()
            if answer in ("q", "quit"):
                raise _QuitError
            if answer not in ("y", "yes"):
                continue
            rationale = ask("  Why does this exist? ").strip()
            if not rationale:
                continue
            dnt = ask("  Should agents NEVER recommend modifying this? [y/N]: ").strip().lower()
            registry.upsert(OriginStory(
                subject_kind=kind,
                subject_key=key,
                rationale=rationale,
                do_not_touch=dnt in ("y", "yes"),
            ))
            registry.save()
            added += 1
            say("  → saved.")

    if added == 0:
        say("\nNo new origin stories captured — all artifacts already annotated or skipped.")
    else:
        say(f"\nCaptured {added} origin story/stories in config/origin_stories.yaml.")
    return added


def _safe_call(fn: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Run a UnifiClient getter, returning [] on any exception."""
    try:
        return fn()
    except Exception:
        return []
