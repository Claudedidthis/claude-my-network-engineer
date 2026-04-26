"""Operator-YAML prompt-injection sanitization (per directive 1.3).

Operator-supplied free text — household_profile fields, registry notes,
origin stories, dismissal reasons, custom probes — flows into the LLM
system prompt via json.dumps(context_blob). The agents/ai_runtime.py
defensive preamble (directive 1.6 / task #42) tells the model to treat
NETWORK CONTEXT as untrusted data, but defence in depth requires *also*
catching obvious injection attempts at the YAML→prompt boundary and
failing closed before the API call.

Trust boundary
--------------
Three classes of string land in the prompt context:

    1. Auto-discovered (UDM-supplied)  — device names, hostnames, SSIDs.
       Trustworthy in shape; operator can rename devices but not the
       transport. Sanitised but with a more permissive policy.
    2. Operator-supplied (YAML)        — fields the operator typed into
       household_profile / registers / origin_stories / dismissals.
       Trusted-but-validated.
    3. Community-supplied (vendor data, OUI db) — community-reviewed,
       sanitised on ingest.

This module enforces (2). For (1) the same patterns apply but the failure
mode is a warning, not a hard raise (an operator could legitimately rename
their TV "ignore-previous-instructions" as a joke; we don't want to nuke
audits over that). The distinction is left to the caller via the strict=
flag on sanitize_operator_string.

Failure semantics
-----------------
sanitize_operator_string raises OperatorInputError, never silently strips.
The reasoning: a string that looks like injection is either a real attempt
(in which case stripping leaves the operator unaware) or a false positive
(in which case the operator wants to know so they can adjust the YAML).
Both cases are surfaced; the LLM call is aborted.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from network_engineer.tools.logging_setup import get_logger

log = get_logger("tools.prompt_safety")


class OperatorInputError(ValueError):
    """Raised when operator-supplied input matches a prompt-injection
    pattern, exceeds size limits, or contains a control-character class
    that would alter prompt parsing."""


# ── Pattern catalog ─────────────────────────────────────────────────────────
#
# Each pattern targets a known attack family. The list is conservative on
# purpose — false positives are operator-visible (the YAML edit is rejected),
# false negatives slip through to the LLM where the directive 1.6 system
# preamble is the second line of defence.

_IGNORE_INSTRUCTIONS = re.compile(
    # Allow up to ~3 intervening words ("the", "all the", "all of the", etc.)
    r"(?i)ignore\s+(?:\S+\s+){0,3}(?:previous|prior|above|earlier)\s+instructions",
)
_NEW_PERSONA = re.compile(
    r"(?i)you are\s+(?:now\s+)?(?:a |an )?(?:different|new)\s+(?:assistant|model|agent|system|persona)",
)
_OVERRIDE_SYSTEM = re.compile(
    r"(?i)(?:override|disregard|forget)\s+(?:\S+\s+){0,2}(?:system|developer|previous)\s+(?:prompt|instructions|message)",
)
# Closing/opening tags used by various LLM tool-use chat templates. Any of
# these inside operator content is a strong signal of an attempt to break
# out of the data block and inject a directive.
_CHAT_TEMPLATE_TAGS = re.compile(
    r"</?(?:system|user|assistant|tool_use|tool_result|function_calls?|invoke|parameter)\b",
    re.IGNORECASE,
)
_CHAT_TEMPLATE_TOKENS = re.compile(r"<\|[^|]{1,40}\|>")
# Common Anthropic / OpenAI / generic "human:" turn markers
_TURN_MARKERS = re.compile(
    r"(?im)^\s*(?:human|assistant|system)\s*:\s*",
)

_SUSPICIOUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous_instructions", _IGNORE_INSTRUCTIONS),
    ("new_persona", _NEW_PERSONA),
    ("override_system", _OVERRIDE_SYSTEM),
    ("chat_template_tag", _CHAT_TEMPLATE_TAGS),
    ("chat_template_token", _CHAT_TEMPLATE_TOKENS),
    ("turn_marker", _TURN_MARKERS),
)


_MAX_FIELD_LEN = 2000  # bytes after NFC normalization
_MAX_BLOB_TOTAL = 200_000  # safety on the whole context blob

# Control characters: ASCII 0-31 except whitespace (\t \n \r) and
# unicode separator/format classes that can hide content from operator
# review while still landing in the LLM context. Bidi overrides are the
# canonical example.
_BIDI_OVERRIDES = ("‪", "‫", "‬", "‭", "‮",
                   "⁦", "⁧", "⁨", "⁩")


def sanitize_operator_string(
    value: str,
    *,
    field_path: str,
    strict: bool = True,
) -> str:
    """Validate one operator-supplied string. Returns the input unchanged
    on success; raises OperatorInputError on any policy violation.

    strict=True   — operator-YAML route. Hard-rejects on any pattern match.
    strict=False  — auto-discovered route (device/SSID names). Warns and
                    returns the value unchanged. The LLM-side preamble is
                    the remaining line of defence.

    The function never silently strips or transforms content.
    """
    if not isinstance(value, str):
        raise OperatorInputError(
            f"{field_path}: expected str, got {type(value).__name__}"
        )

    normalized = unicodedata.normalize("NFKC", value)
    if any(ch in normalized for ch in _BIDI_OVERRIDES):
        raise OperatorInputError(
            f"{field_path}: contains bidi override character — refusing "
            "(directional overrides can hide content from operator review)."
        )

    # Strip zero-width / format control characters (Cf category) before
    # pattern matching. They are invisible to operator review and are a
    # common way to bypass naive substring filters: i​gnore previous
    # instructions reads as "ignore..." but the regex sees a ZWSP between
    # the i and g. Bidi overrides are also Cf but caught above for a more
    # specific error message.
    pattern_normalized = "".join(
        ch for ch in normalized if unicodedata.category(ch) != "Cf"
    )

    encoded_len = len(normalized.encode("utf-8"))
    if encoded_len > _MAX_FIELD_LEN:
        raise OperatorInputError(
            f"{field_path}: length {encoded_len} bytes exceeds "
            f"{_MAX_FIELD_LEN}. Trim the field or split it across multiple "
            "structured entries."
        )

    for pattern_name, pattern in _SUSPICIOUS_PATTERNS:
        if pattern.search(pattern_normalized):
            log.warning(
                "prompt_injection_pattern_detected",
                extra={
                    "agent": "prompt_safety",
                    "field_path": field_path,
                    "pattern_name": pattern_name,
                    "strict": strict,
                },
            )
            if strict:
                raise OperatorInputError(
                    f"{field_path}: matches pattern {pattern_name!r}; "
                    "this looks like a prompt-injection attempt. Edit the "
                    "operator YAML to remove the offending phrasing. If "
                    "this is a legitimate value (uncommon), file an issue "
                    "and tag the offending field — the pattern catalog is "
                    "deliberately conservative."
                )
    return normalized


def sanitize_context_blob(
    blob: Any,
    *,
    path: str = "",
    strict: bool = True,
) -> Any:
    """Recursively sanitize every string leaf in a JSON-shaped value.

    Returns a structurally-identical object with each string passed through
    sanitize_operator_string. Raises on the first violation. The total
    encoded size is checked at the end to bound the prompt blast radius.
    """
    sanitized = _walk_and_sanitize(blob, path=path or "(root)", strict=strict)

    # Bound total context size as a safety net — a 100k-line YAML poured
    # into a single LLM call is probably either an accident or hostile.
    import json
    encoded_len = len(json.dumps(sanitized, default=str).encode("utf-8"))
    if encoded_len > _MAX_BLOB_TOTAL:
        raise OperatorInputError(
            f"context blob is {encoded_len} bytes (limit {_MAX_BLOB_TOTAL}). "
            "Trim the snapshot or split the analysis."
        )
    return sanitized


def _walk_and_sanitize(value: Any, *, path: str, strict: bool) -> Any:
    if isinstance(value, str):
        return sanitize_operator_string(value, field_path=path, strict=strict)
    if isinstance(value, dict):
        return {
            k: _walk_and_sanitize(v, path=f"{path}.{k}", strict=strict)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _walk_and_sanitize(v, path=f"{path}[{i}]", strict=strict)
            for i, v in enumerate(value)
        ]
    if isinstance(value, tuple):
        return tuple(
            _walk_and_sanitize(v, path=f"{path}[{i}]", strict=strict)
            for i, v in enumerate(value)
        )
    return value


# ── Operator vs auto-discovered field classification ────────────────────────
#
# Some context_blob keys are operator-typed YAML; others are auto-discovered
# UDM fields. The routing isn't currently introspectable on the blob, so the
# default is strict (treat all string content as operator-supplied). Callers
# that have a richer view — e.g. _security_context which knows which keys
# came from the snapshot vs the household_profile — may pass strict=False
# for the auto-discovered subtrees.

_DEFAULT_STRICT_KEYS = frozenset({
    "household_profile",
    "registry",
    "origin_stories",
    "dismissals",
    "user_notes",
    "operator_notes",
    "concerns",
    "use_case",
    "rationale",
    "reason",
})

_DEFAULT_PERMISSIVE_KEYS = frozenset({
    "networks",
    "wifi_networks",
    "clients",
    "devices",
    "device_stats",
    "client_stats",
    "firewall_rules",
    "port_forwards",
    "settings",
    "health",
    "alerts",
    "protect_cameras",
})


def sanitize_context_blob_partitioned(blob: dict[str, Any]) -> dict[str, Any]:
    """Apply strict sanitization to operator-supplied subtrees and a
    permissive (warning-only) pass to auto-discovered subtrees.

    Keys not in either default set are treated as strict (fail-closed bias).
    """
    out: dict[str, Any] = {}
    for key, value in blob.items():
        if key in _DEFAULT_PERMISSIVE_KEYS:
            out[key] = sanitize_context_blob(value, path=key, strict=False)
        else:
            out[key] = sanitize_context_blob(value, path=key, strict=True)
    return out
