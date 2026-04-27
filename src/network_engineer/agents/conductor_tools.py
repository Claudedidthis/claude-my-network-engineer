"""Conductor tool registry — wraps existing modules as ToolSpecs the agent loop calls.

Per docs/agent_architecture.md §5. The Conductor exposes ~25 tools at any
given moment; this module is where they're declared with their schemas.

Tools fall into categories:

  Discovery (read-only)
    read_snapshot, count_devices_by_role, lookup_oui_vendor,
    identify_smart_home_brands, derive_isp_from_wan, audit_network,
    monitor_status, query_history, list_cautions

  Reasoning (LLM service-calls)
    analyze_security_posture, review_change, propose_segmentation,
    evaluate_against_corpus*, cite_corpus*

  Save (operator-config writes)
    save_household_profile_field, save_registry_entry, save_origin_story,
    save_dismissal, record_caution_marker, record_audit_caution

  State transitions
    acknowledge_caution, recheck_caution_resolution

  Operator-interaction gate
    ask_operator_to_approve

  Execute (requires ApprovedAction)
    apply_approved_change

* evaluate_against_corpus and cite_corpus are STUBBED until task #51
  (Layer 0 corpus) lands. The stubs return "corpus not loaded yet" so the
  Conductor knows it cannot invoke counsel-against without canonical
  citation. The system prompt teaches the Conductor to handle this case.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from network_engineer.tools.agent_loop import ToolSpec
from network_engineer.tools.durable_memory import (
    CautionMarker,
    DurableMemory,
)


# ── Stub-only functions (real impls land in #51 / step 5+) ─────────────────


def _stub_evaluate_against_corpus(
    *, action: str, current_state: dict, household_profile: dict | None = None,
) -> dict[str, Any]:
    """STUB — real implementation in task #51 (Layer 0 corpus retrieval).

    Returns a structured "corpus not loaded" response so the Conductor's
    system prompt can recognize the no-corpus state and avoid invoking
    counsel-against without canonical citations.
    """
    return {
        "corpus_loaded": False,
        "severity": None,
        "canonical_source": None,
        "counsel_text": (
            "Corpus is not loaded yet (task #51 pending). Cannot evaluate "
            "this action against canonical sources. Express concern in "
            "conversation if appropriate but do NOT call record_caution_marker."
        ),
    }


def _stub_cite_corpus(*, source_id: str) -> dict[str, Any]:
    return {
        "corpus_loaded": False,
        "source_id": source_id,
        "title": None,
        "excerpt": (
            "Corpus is not loaded yet (task #51 pending). The cite_corpus "
            "tool will return real excerpts once the bundle is built."
        ),
    }


# ── Tool builders — each returns a ToolSpec keyed by the tool name ──────────


def build_conductor_tools(
    *,
    durable_memory: DurableMemory,
    unifi_client: Any | None,
    ai_runtime: Any | None,
    session_id: str,
) -> dict[str, ToolSpec]:
    """Construct the full tool registry for one Conductor session.

    The registry is built fresh per session because tools close over the
    session_id (for caution-marker provenance) and the durable memory
    instance (for persistence). The unifi_client and ai_runtime are also
    closed over.
    """

    tools: dict[str, ToolSpec] = {}

    # ── Discovery ────────────────────────────────────────────────────────

    if unifi_client is not None:
        tools["read_snapshot"] = ToolSpec(
            name="read_snapshot",
            description=(
                "Capture a fresh snapshot of the live network and return a "
                "summary (device count, client count, network count, AP "
                "models, camera count). Use to ground reasoning in current "
                "state; results are also persisted to snapshots/ for audit."
            ),
            fn=lambda: _read_snapshot(unifi_client),
            schema={"type": "object", "properties": {}},
        )

        tools["count_devices_by_role"] = ToolSpec(
            name="count_devices_by_role",
            description=(
                "Return a breakdown of devices on the network by inferred role "
                "(AP / switch / gateway / camera / IoT / trusted endpoint / "
                "unknown). Useful for building a mental model of the network."
            ),
            fn=lambda: _count_devices_by_role(unifi_client),
            schema={"type": "object", "properties": {}},
        )

        tools["lookup_oui_vendor"] = ToolSpec(
            name="lookup_oui_vendor",
            description=(
                "Given a MAC address (or just an OUI prefix), return the IEEE-"
                "registered vendor. Useful for identifying mystery devices."
            ),
            fn=lambda mac: _lookup_oui(mac),
            schema={
                "type": "object",
                "properties": {"mac": {"type": "string"}},
                "required": ["mac"],
            },
        )

        tools["identify_smart_home_brands"] = ToolSpec(
            name="identify_smart_home_brands",
            description=(
                "Scan all clients on the network and return the smart-home "
                "ecosystems present (lutron, hue, sonos, homekit, alexa, "
                "google_home, ring, smartthings, etc.) inferred from MAC OUI."
            ),
            fn=lambda: _identify_smart_home_brands(unifi_client),
            schema={"type": "object", "properties": {}},
        )

        tools["audit_network"] = ToolSpec(
            name="audit_network",
            description=(
                "Run the deterministic auditor over the current snapshot and "
                "return all findings. Each finding has code, severity, "
                "evidence, and recommendation."
            ),
            fn=lambda: _audit_network(unifi_client),
            schema={"type": "object", "properties": {}},
        )

    # ── Durable memory access ───────────────────────────────────────────

    tools["query_history"] = ToolSpec(
        name="query_history",
        description=(
            "Search durable memory for entries relevant to a question. "
            "Returns provenance-tagged untrusted-data blocks. Optional "
            "days_back to scope the search; default is unrestricted."
        ),
        fn=lambda question, days_back=None: durable_memory.query_history(
            question, days_back=days_back,
        ),
        schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "days_back": {"type": ["integer", "null"]},
            },
            "required": ["question"],
        },
    )

    tools["list_cautions"] = ToolSpec(
        name="list_cautions",
        description=(
            "Return the operator's current caution markers. Filter by "
            "state (active/acknowledged/resolved) and severity (RED/AMBER)."
        ),
        fn=lambda state_filter=None, severity_filter=None: [
            m.model_dump(mode="json")
            for m in durable_memory.list_cautions(
                state_filter=state_filter,
                severity_filter=severity_filter,
            )
        ],
        schema={
            "type": "object",
            "properties": {
                "state_filter": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "severity_filter": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
            },
        },
    )

    # ── Save tools ───────────────────────────────────────────────────────

    tools["save_household_profile_field"] = ToolSpec(
        name="save_household_profile_field",
        description=(
            "Save a HouseholdProfile field with confidence + evidence. "
            "field_path is dotted (e.g. 'security.iot_isolation_appetite')."
        ),
        fn=lambda field_path, value, confidence, evidence: durable_memory.upsert_fact(
            field=f"household_profile.{field_path}" if not field_path.startswith("household_profile") else field_path,
            value=value,
            confidence=confidence,
            evidence=evidence,
            source_turn_id="(conductor)",
        ),
        schema={
            "type": "object",
            "properties": {
                "field_path": {"type": "string"},
                "value": {},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["field_path", "value", "confidence", "evidence"],
        },
    )

    tools["save_origin_story"] = ToolSpec(
        name="save_origin_story",
        description=(
            "Record why a non-default config artifact exists "
            "(network/port-forward/firewall-rule). Used when the operator "
            "explains the heritage of an existing artifact. Sets do_not_touch "
            "if operator says agents should never modify it."
        ),
        fn=lambda subject_kind, subject_key, rationale, do_not_touch=False: _save_origin_story(
            subject_kind=subject_kind, subject_key=subject_key,
            rationale=rationale, do_not_touch=do_not_touch,
        ),
        schema={
            "type": "object",
            "properties": {
                "subject_kind": {
                    "type": "string",
                    "enum": ["network", "port_forward", "firewall_rule", "device", "vlan"],
                },
                "subject_key": {"type": "string"},
                "rationale": {"type": "string"},
                "do_not_touch": {"type": "boolean"},
            },
            "required": ["subject_kind", "subject_key", "rationale"],
        },
    )

    tools["save_dismissal"] = ToolSpec(
        name="save_dismissal",
        description=(
            "Suppress a specific finding pattern with operator-confirmed "
            "rationale. Per directive 1.4, dismissals have TTLs; default "
            "expiry 90 days. Pass reconfirm_on_change=true to auto-revoke "
            "when target attributes drift."
        ),
        fn=lambda finding_code, match_field, match_key, reason, reconfirm_on_change=False: _save_dismissal(
            finding_code=finding_code, match_field=match_field,
            match_key=match_key, reason=reason,
            reconfirm_on_change=reconfirm_on_change,
        ),
        schema={
            "type": "object",
            "properties": {
                "finding_code": {"type": "string"},
                "match_field": {"type": "string"},
                "match_key": {"type": "string"},
                "reason": {"type": "string"},
                "reconfirm_on_change": {"type": "boolean"},
            },
            "required": ["finding_code", "match_field", "match_key", "reason"],
        },
    )

    # ── Caution markers ──────────────────────────────────────────────────

    tools["record_caution_marker"] = ToolSpec(
        name="record_caution_marker",
        description=(
            "Persist a RED or AMBER caution marker. Use ONLY when you have "
            "a corpus citation backing the counsel (evaluate_against_corpus "
            "returned a non-null severity). Origin must be 'operator_override' "
            "(operator chose this despite counsel) or 'audit_finding' (auditor "
            "surfaced something the operator hasn't acted on)."
        ),
        fn=lambda **kwargs: _record_caution(durable_memory, session_id, **kwargs),
        schema={
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["RED", "AMBER"]},
                "origin": {
                    "type": "string",
                    "enum": ["operator_override", "audit_finding"],
                },
                "target_kind": {"type": "string"},
                "target_key": {"type": "string"},
                "canonical_source": {"type": "string"},
                "counsel_text": {"type": "string"},
                "operator_rationale": {"type": ["string", "null"]},
                "finding_id": {"type": ["string", "null"]},
            },
            "required": [
                "severity", "origin", "target_kind", "target_key",
                "canonical_source", "counsel_text",
            ],
        },
    )

    tools["acknowledge_caution"] = ToolSpec(
        name="acknowledge_caution",
        description=(
            "Operator-initiated transition: active → acknowledged. Use "
            "ONLY after the operator has explicitly confirmed they have "
            "read and accepted the caution. Marker remains visible in UI."
        ),
        fn=lambda marker_id: durable_memory.acknowledge_caution(marker_id).model_dump(mode="json"),
        schema={
            "type": "object",
            "properties": {"marker_id": {"type": "string"}},
            "required": ["marker_id"],
        },
    )

    # ── Reasoning service-calls ──────────────────────────────────────────

    if ai_runtime is not None:
        tools["analyze_security_posture"] = ToolSpec(
            name="analyze_security_posture",
            description=(
                "Run AI security analysis over the current snapshot. Returns "
                "structured posture rating, list of issues with severity, "
                "summary. Calls Anthropic with caching."
            ),
            fn=lambda: _analyze_security_posture(ai_runtime, unifi_client),
            schema={"type": "object", "properties": {}},
        )

        tools["review_change"] = ToolSpec(
            name="review_change",
            description=(
                "Independent AI review of a proposed configuration change. "
                "Returns verdict (safe/risky/block), reasoning, concerns, "
                "questions, suggested alternatives."
            ),
            fn=lambda action, proposed, current=None: ai_runtime.review_config_change(
                proposed, current or {}, action=action,
            ).model_dump(mode="json"),
            schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "proposed": {"type": "object"},
                    "current": {"type": ["object", "null"]},
                },
                "required": ["action", "proposed"],
            },
        )

    # ── Corpus stubs (real impls land in #51) ───────────────────────────

    tools["evaluate_against_corpus"] = ToolSpec(
        name="evaluate_against_corpus",
        description=(
            "Evaluate a proposed action against canonical sources. Returns "
            "severity (RED/AMBER/null), canonical_source citation, counsel "
            "text. STUB until task #51 (Layer 0 corpus) lands; currently "
            "returns corpus_loaded=false. The Conductor must handle the "
            "no-corpus case gracefully — express concern but do NOT call "
            "record_caution_marker."
        ),
        fn=_stub_evaluate_against_corpus,
        schema={
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "current_state": {"type": "object"},
                "household_profile": {"type": ["object", "null"]},
            },
            "required": ["action", "current_state"],
        },
    )

    tools["cite_corpus"] = ToolSpec(
        name="cite_corpus",
        description=(
            "Retrieve a specific corpus excerpt by source_id (e.g. "
            "'red-005-ssh-telnet-wan-exposed', 'rfc-1918'). STUB until "
            "task #51 lands."
        ),
        fn=_stub_cite_corpus,
        schema={
            "type": "object",
            "properties": {"source_id": {"type": "string"}},
            "required": ["source_id"],
        },
    )

    return tools


# ── Concrete tool implementations ───────────────────────────────────────────


def _read_snapshot(client: Any) -> dict[str, Any]:
    """Capture snapshot summary; full data persisted by client.snapshot()."""
    info = client.test_connection()
    snapshot_path = client.snapshot()
    return {
        "summary": info,
        "snapshot_path": str(snapshot_path),
        "captured_at": datetime.now(UTC).isoformat(),
    }


def _count_devices_by_role(client: Any) -> dict[str, int]:
    """Group devices by inferred role."""
    devices = client.get_devices()
    out = {"ap": 0, "switch": 0, "gateway": 0, "camera": 0, "other": 0}
    for d in devices:
        model = str(d.get("model", "")).lower()
        if "udm" in model or "usg" in model or "gateway" in model:
            out["gateway"] += 1
        elif "ap" in model or "u6" in model or "uap" in model:
            out["ap"] += 1
        elif "sw" in model or "switch" in model or "usw" in model:
            out["switch"] += 1
        else:
            out["other"] += 1
    cameras = client.get_protect_cameras()
    out["camera"] = len(cameras)
    return out


def _lookup_oui(mac: str) -> dict[str, str | None]:
    from network_engineer.tools.registry import manufacturer_for_mac
    return {"mac": mac, "vendor": manufacturer_for_mac(mac)}


def _identify_smart_home_brands(client: Any) -> dict[str, list[str]]:
    """Walk clients, classify by OUI vendor heuristics."""
    from network_engineer.tools.registry import manufacturer_for_mac
    brand_keywords = {
        "lutron": "lutron",
        "philips": "hue",  # Philips Hue OUIs
        "signify": "hue",  # Signify (Philips Hue) OUIs
        "sonos": "sonos",
        "apple": "apple",
        "amazon": "amazon",
        "google": "google",
        "nest": "nest",
        "ring": "ring",
        "samsung": "samsung",
        "lifx": "lifx",
        "ecobee": "ecobee",
    }
    found: dict[str, list[str]] = {}
    for c in client.get_clients():
        mac = c.get("macAddress") or c.get("mac") or ""
        vendor = (manufacturer_for_mac(mac) or "").lower()
        for keyword, brand in brand_keywords.items():
            if keyword in vendor:
                found.setdefault(brand, []).append(c.get("name") or mac)
                break
    return found


def _audit_network(client: Any) -> list[dict[str, Any]]:
    from network_engineer.tools.auditor import run_from_client
    findings = run_from_client(client)
    return [f.model_dump(mode="json") for f in findings]


def _save_origin_story(
    *, subject_kind: str, subject_key: str, rationale: str, do_not_touch: bool,
) -> dict[str, str]:
    from network_engineer.tools.origin_stories import OriginStoryRegistry
    from network_engineer.tools.schemas import OriginStory
    registry = OriginStoryRegistry.load()
    story = OriginStory(
        subject_kind=subject_kind,
        subject_key=subject_key,
        rationale=rationale,
        do_not_touch=do_not_touch,
    )
    registry.upsert(story)
    registry.save()
    return {
        "status": "saved",
        "subject_kind": subject_kind,
        "subject_key": subject_key,
    }


def _save_dismissal(
    *, finding_code: str, match_field: str, match_key: str,
    reason: str, reconfirm_on_change: bool,
) -> dict[str, str]:
    from datetime import timedelta
    from network_engineer.tools.dismissals import DismissalRegistry
    from network_engineer.tools.schemas import Dismissal
    registry = DismissalRegistry.load()
    dismissal = Dismissal(
        finding_code=finding_code,
        match_field=match_field,
        match_key=match_key,
        reason=reason,
        expires_at=datetime.now(UTC) + timedelta(days=90),
        reconfirm_on_change=reconfirm_on_change,
    )
    registry.add(dismissal)
    registry.save()
    return {
        "status": "saved",
        "finding_code": finding_code,
        "match_key": match_key,
    }


def _record_caution(
    durable_memory: DurableMemory,
    session_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Construct a CautionMarker and persist it."""
    marker = CautionMarker(
        counseled_in_session=session_id,
        **kwargs,
    )
    durable_memory.record_caution_marker(marker)
    return marker.model_dump(mode="json")


def _analyze_security_posture(ai_runtime: Any, client: Any) -> dict[str, Any]:
    snapshot = {
        "networks": client.get_networks(),
        "wifi_networks": client.get_wifi_networks(),
        "clients": client.get_clients(),
        "firewall_rules": client.get_firewall_rules(),
        "port_forwards": client.get_port_forwards(),
        "devices": client.get_devices(),
    }
    result = ai_runtime.analyze_security_posture(snapshot)
    return result.model_dump(mode="json")
