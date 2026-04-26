"""Pydantic models shared across all agents.

These map 1:1 to Supabase tables added in Phase 11. Keeping the shapes here
means the agent layer never imports from cloud_sync and the core works offline.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ── Severity / tier enums ─────────────────────────────────────────────────────

class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class ActionTier(StrEnum):
    AUTO = "AUTO"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    NEVER = "NEVER"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DISMISSED = "DISMISSED"


class SecurityTier(StrEnum):
    """Security classification tier for clients (used by Security Agent + Registry)."""
    TRUST = "TRUST"
    IOT = "IOT"
    CAMERA = "CAMERA"
    GUEST = "GUEST"
    UNKNOWN = "UNKNOWN"


# ── Core agent models ─────────────────────────────────────────────────────────

class Finding(BaseModel):
    """A single audit finding returned by the Auditor agent."""

    severity: Severity
    code: str                   # e.g. "WIFI_CHANNEL_CONFLICT"
    title: str                  # short human-readable label
    detail: str                 # one-paragraph explanation with context
    evidence: dict[str, Any] = Field(default_factory=dict)
    agent: str = "auditor"
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_actionable(self) -> bool:
        return self.severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)


class Recommendation(BaseModel):
    """A proposed change that requires human approval (REQUIRES_APPROVAL tier)."""

    action: str
    title: str
    rationale: str
    current_state: dict[str, Any] = Field(default_factory=dict)
    proposed_change: dict[str, Any] = Field(default_factory=dict)
    rollback_plan: str = ""
    risk: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    agent: str = "orchestrator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NetworkEvent(BaseModel):
    """A monitoring event emitted by the Monitor agent (Phase 5)."""

    event_type: str             # e.g. "WAN_LATENCY_HIGH"
    severity: Severity
    message: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    agent: str = "monitor"
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentAction(BaseModel):
    """A write action that was actually applied (AUTO tier, post-verification)."""

    action: str
    tier: ActionTier
    agent: str
    params: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    snapshot_path: str = ""     # path to the pre-change snapshot
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UpgradeRecommendation(BaseModel):
    """A hardware EOL or firmware upgrade suggestion (Phase 9)."""

    device_id: str
    device_name: str
    device_model: str
    current_firmware: str = ""
    recommendation: str         # "upgrade_firmware" | "replace_device" | "monitor"
    reason: str
    urgency: Severity
    score: int = 0              # 0-100 deterministic score from the catalog
    factors: dict[str, int] = Field(default_factory=dict)   # individual score contributions
    successor_model: str | None = None
    successor_msrp_usd: int | None = None
    narrative: str = ""         # Optional AI narrative (Haiku) — empty when AI is disabled
    agent: str = "upgrade"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PendingApproval(BaseModel):
    """A REQUIRES_APPROVAL item waiting for human sign-off (surfaces in Phase 10 API)."""

    recommendation: Recommendation
    status: ApprovalStatus = ApprovalStatus.PENDING
    reviewed_at: datetime | None = None
    reviewer_note: str = ""


# ── AI Runtime models (Phase 7) ───────────────────────────────────────────────

class SecurityIssue(BaseModel):
    """One specific security issue identified by AI security analysis."""

    severity: Severity
    code: str                       # e.g. "IOT_ON_TRUSTED_VLAN"
    title: str
    description: str
    affected: list[str] = Field(default_factory=list)   # device/network/client names
    recommendation: str = ""


class SecurityAnalysis(BaseModel):
    """Output of `AIRuntime.analyze_security_posture`."""

    overall_posture: str            # "weak" | "moderate" | "strong" | "unknown"
    score: int                      # 0-100; 0 when generated_by == "deterministic_fallback"
    issues: list[SecurityIssue] = Field(default_factory=list)
    summary: str
    generated_by: str               # "ai" | "deterministic_fallback"
    model_used: str | None = None
    token_usage: dict[str, int] | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Registry models (Phase 9.5) ───────────────────────────────────────────────
#
# Operator-supplied annotations for devices (UniFi APs/switches/gateway) and
# clients (stations). Stored as YAML on disk now; mirrored to Supabase in
# Phase 11 as `device_registry` and `client_registry` tables.
#
# Supabase DDL (Phase 11 — keep in sync with these models):
#
#   CREATE TABLE device_registry (
#     mac            TEXT PRIMARY KEY,
#     name_hint      TEXT,
#     location       TEXT,
#     rationale      TEXT,
#     role           TEXT,
#     criticality    TEXT,
#     notes          TEXT NOT NULL DEFAULT '',
#     deployed_at    TIMESTAMPTZ,
#     last_inspected TIMESTAMPTZ,
#     source         TEXT NOT NULL DEFAULT 'manual',
#     created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
#     updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
#     deleted_at     TIMESTAMPTZ
#   );
#   CREATE INDEX idx_device_registry_updated_at ON device_registry(updated_at);
#
#   CREATE TABLE client_registry (
#     mac            TEXT PRIMARY KEY,
#     name_hint      TEXT,
#     tier_override  TEXT,
#     owner          TEXT,
#     location       TEXT,
#     role           TEXT,
#     criticality    TEXT,
#     notes          TEXT NOT NULL DEFAULT '',
#     source         TEXT NOT NULL DEFAULT 'manual',
#     created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
#     updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
#     deleted_at     TIMESTAMPTZ
#   );
#   CREATE INDEX idx_client_registry_updated_at ON client_registry(updated_at);

class DeviceRegistryEntry(BaseModel):
    """Operator annotations for a UniFi-managed device (AP/switch/gateway)."""

    mac: str                                  # PRIMARY KEY (lowercase, colon-separated)
    name_hint: str | None = None              # Display name from controller at last write
    location: str | None = None               # "master bedroom", "garage", etc.
    rationale: str | None = None              # "Coverage gap from main AP"
    role: str | None = None                   # primary | secondary | mesh | wired | gateway
    criticality: Severity | None = None       # CRITICAL/HIGH/MEDIUM/LOW (INFO unused)
    notes: str = ""
    deployed_at: datetime | None = None
    last_inspected: datetime | None = None
    # Sync metadata
    source: str = "manual"                    # manual | ios_app | imported | auto
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None        # soft-delete tombstone (sync correctness)


class ClientRegistryEntry(BaseModel):
    """Operator annotations for a station/client device."""

    mac: str                                  # PRIMARY KEY
    name_hint: str | None = None
    tier_override: SecurityTier | None = None
    owner: str | None = None                  # free-form: e.g. "alex", "shared", "kid", "guest"
    location: str | None = None
    role: str | None = None                   # "primary phone", "smart-home hub", etc.
    criticality: Severity | None = None
    notes: str = ""
    source: str = "manual"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None


# ── Operator context: profile, origin stories, dismissals (Phase 9.6) ────────
#
# These are the "Level 4 contextual signal architecture" from the Waiter paper:
# situated knowledge captured from the operator that lets agents stop running
# generic playbooks and start running playbooks against a specific household.
#
# All three are YAML-on-disk now; mirror to Supabase in Phase 11. Pydantic
# fields are 1:1 with the table columns. Suggested DDL:
#
#   CREATE TABLE household_profile (
#     id              TEXT PRIMARY KEY DEFAULT 'default',  -- single-row table per fork
#     use_case        TEXT, concerns TEXT[], motivations TEXT[],
#     layout          TEXT, material TEXT, square_footage INT,
#     work_from_home  TEXT, kids TEXT, workload_profile TEXT,
#     existing_concerns TEXT NOT NULL DEFAULT '',
#     mode            TEXT NOT NULL DEFAULT 'unknown',  -- heritage | greenfield | unknown
#     created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
#     updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
#   );
#
#   CREATE TABLE origin_stories (
#     subject_kind  TEXT NOT NULL,  -- network | port_forward | firewall_rule | vlan | device
#     subject_key   TEXT NOT NULL,  -- network name, rule _id, port, MAC, etc.
#     rationale     TEXT NOT NULL,
#     do_not_touch  BOOLEAN NOT NULL DEFAULT false,
#     created_by    TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
#     updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
#     PRIMARY KEY (subject_kind, subject_key)
#   );
#
#   CREATE TABLE dismissals (
#     finding_code  TEXT NOT NULL,
#     match_key     TEXT NOT NULL,  -- SSID, MAC, rule name, etc. that matches evidence
#     match_field   TEXT NOT NULL,  -- which evidence field to match against
#     reason        TEXT NOT NULL,
#     scope         TEXT NOT NULL DEFAULT 'permanent',  -- permanent | until-date | n-days
#     expires_at    TIMESTAMPTZ,
#     created_by    TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
#     PRIMARY KEY (finding_code, match_field, match_key)
#   );

# ── HouseholdProfile decomposed into 11 themed submodels ─────────────────────
#
# Each submodel covers one of the 11 dimensions that materially shape network
# recommendations. Every field is Optional so submodels can be partially
# populated; the onboarding agent fills them incrementally via probe-driven
# conversation. Lists default to [] for safe append.
#
# Design rule for adding fields here: only add what informs at least one
# downstream agent decision. Each field should be answerable to "what would
# the auditor / monitor / security agent / optimizer / upgrade agent do
# differently if it knew this?"


class BuildingProfile(BaseModel):
    """Physical reality of the building — RF planning + cable feasibility."""

    home_type: str | None = None                # house | apartment | condo | townhouse | converted | rv
    construction_era: str | None = None         # pre_1950 | 1950_1980 | 1980_2000 | 2000_2010 | post_2010
    num_floors: int | None = None
    has_basement: bool | None = None
    has_attic: bool | None = None
    has_crawlspace: bool | None = None
    attic_accessible_for_cable: bool | None = None
    basement_accessible_for_cable: bool | None = None
    construction_material: list[str] = Field(default_factory=list)   # drywall, plaster, brick, concrete, metal_lath
    floor_plan: str | None = None               # open | traditional | mixed
    square_footage: int | None = None
    outbuildings: list[str] = Field(default_factory=list)            # shed, workshop, adu, pool_house, etc.
    garage_type: str | None = None              # attached | detached | none
    isp_entry_location: str | None = None
    core_location: str | None = None            # where the rack / network core lives
    existing_cable: list[str] = Field(default_factory=list)          # ethernet, coax, phone, fiber_in_walls
    can_run_new_cable: str | None = None        # yes_diy | yes_hire | no | unsure
    rental: bool | None = None
    partner_tolerance_holes: str | None = None  # high | medium | low | n/a


class ISPProfile(BaseModel):
    """ISP situation — affects WAN reliability planning + IPv6 + failover."""

    isp_type: str | None = None                 # fiber | cable | dsl | fixed_wireless | starlink | 5g_home
    isp_name: str | None = None
    contracted_down_mbps: int | None = None
    contracted_up_mbps: int | None = None
    delivered_speeds_acceptable: bool | None = None
    ipv6_supported: bool | None = None
    modem_router_situation: str | None = None   # clean_handoff | combo_unit | isp_required_router
    failover_concern: bool | None = None
    second_isp_available: bool | None = None
    static_ip: bool | None = None
    data_cap_gb: int | None = None              # 0 means unlimited; None means unknown


class HouseholdComposition(BaseModel):
    """Who lives there — drives parental controls, accessibility, guest needs."""

    num_residents: int | None = None
    age_bands: list[str] = Field(default_factory=list)               # adult, teen, school_age, infant, senior
    tech_literacy_levels: list[str] = Field(default_factory=list)    # expert, comfortable, basic, novice
    accessibility_needs: str = ""               # free text — medical / mobility / cognitive
    has_kids: bool | None = None
    kid_age_band: str | None = None             # none | infants | school_age | teens | mixed
    kids_savvy_with_workarounds: bool | None = None
    guest_frequency: str | None = None          # rarely | occasional | frequent | constant
    medical_devices_on_network: str = ""        # free text — CPAPs, CGMs, etc.


class WorkProfile(BaseModel):
    """Work and professional use — drives WAN priority, VPN, conferencing."""

    work_from_home: str | None = None           # none | occasional | daily | heavy_meetings
    multiple_wfh_simultaneous: bool | None = None
    work_types: list[str] = Field(default_factory=list)              # video_calls, vdi, email, dev, creative
    corporate_vpn: bool | None = None
    corporate_vpn_split_tunnel: bool | None = None
    self_hosted_services: list[str] = Field(default_factory=list)    # nas, homelab, web, gameserver
    conferencing_quality_critical: bool | None = None
    managed_corporate_devices: bool | None = None


class DeviceEcosystem(BaseModel):
    """Device ecosystem — the longest section because it's where complexity lives."""

    approx_device_count: int | None = None
    primary_platforms: list[str] = Field(default_factory=list)       # apple, windows, linux, android
    smart_home_ecosystems: list[str] = Field(default_factory=list)   # homekit, google_home, alexa, smartthings, ha
    streaming_devices: list[str] = Field(default_factory=list)
    smart_tvs_count: int | None = None
    gaming: str | None = None                   # none | casual_console | competitive | pc_gaming | mixed
    camera_architecture: str | None = None      # none | local_nvr | cloud | hybrid
    has_video_doorbell: bool | None = None
    has_smart_locks: bool | None = None
    smart_lighting_protocols: list[str] = Field(default_factory=list)  # zigbee, zwave, wifi, thread
    smart_thermostat: bool | None = None
    smart_smoke_detectors: bool | None = None
    has_solar: bool | None = None
    solar_third_party_iot: bool | None = None   # solar installer's gateway on your network
    has_battery_storage: bool | None = None
    ev_charger: str | None = None               # none | present | planned
    has_nas: bool | None = None
    has_printers: bool | None = None
    has_voip: bool | None = None
    has_medical_iot: bool | None = None
    has_baby_monitor: bool | None = None
    has_kid_devices: bool | None = None


class SecurityPhilosophy(BaseModel):
    """Security posture and operator philosophy — drives VLAN architecture aggressiveness."""

    iot_isolation_appetite: str | None = None   # none | light | strict | paranoid
    third_party_devices_present: bool | None = None
    third_party_devices_description: str = ""   # solar Zigbee, alarm, HVAC remote, ISP-provided
    vpn_usage: list[str] = Field(default_factory=list)               # privacy, remote_in, remote_out
    remote_access_required: bool | None = None
    iot_phone_home_concern: str | None = None   # none | mild | high
    dns_filtering_interest: bool | None = None
    content_filter_for_kids: bool | None = None
    has_been_compromised_before: bool | None = None
    breach_disclosure: str = ""                 # what happened, free text


class UsagePatterns(BaseModel):
    """Network load characterization — drives bandwidth + radio planning."""

    peak_usage_window: str | None = None        # "weekday evenings 6-10pm" free text
    simultaneous_4k_streams_typical: int | None = None
    simultaneous_4k_streams_peak: int | None = None
    backup_jobs: list[str] = Field(default_factory=list)             # time_machine, backblaze, photos
    bandwidth_intensive_extras: str = ""        # free text — podcast uploads, video editing, etc.
    automation_depth: str | None = None         # none | light | medium | heavy
    zigbee_device_count: int | None = None      # affects radio environment if many


class InfrastructureProfile(BaseModel):
    """Existing wiring + power infrastructure."""

    has_ups: bool | None = None
    ups_capacity_va: int | None = None
    ups_protected_devices: str = ""             # free text
    existing_switches: str = ""                 # what they have
    poe_budget_w: int | None = None
    willing_to_run_power_to_aps: bool | None = None
    rack_space: str | None = None               # full_rack | half_rack | shelf | closet | none
    outdoor_coverage_required: bool | None = None
    outdoor_areas: list[str] = Field(default_factory=list)           # backyard, front_porch, garage, shed


class PreferencesProfile(BaseModel):
    """Operator preferences — drives recommendation tone + maintenance posture."""

    cloud_vs_local_control: str | None = None   # cloud_ok | prefer_local | local_only
    privacy_orientation: str | None = None      # low | medium | high
    diy_comfort: str | None = None              # high (run cable) | medium (keystones) | low (plug_and_play)
    maintenance_tolerance: str | None = None    # zero_touch | occasional | regular_tinkering
    aesthetic_constraints: str = ""             # free text — "no AP visible in living room"
    budget_initial_usd: int | None = None
    budget_ongoing_monthly_usd: int | None = None
    firmware_stance: str | None = None          # lts_only | mainstream | willing_to_run_rc


class FutureStateProfile(BaseModel):
    """What's likely to change — affects investment calculus."""

    planned_renovation: bool | None = None
    renovation_scope: str = ""                  # free text
    moving_within_2_years: bool | None = None
    household_growth_planned: bool | None = None
    planned_devices: list[str] = Field(default_factory=list)         # ev, solar, more_cameras, home_theater
    homelab_plans: bool | None = None


class OriginStoryProfile(BaseModel):
    """Why the operator is here — the trigger event that brought them to this tool.

    Distinct from per-artifact OriginStory entries: this is the personal
    journey, not the rationale for a specific port forward. Shapes how
    aggressive vs. conservative recommendations should be."""

    trigger_event: str = ""                     # free text — "buffering during Zoom"
    pain_points: list[str] = Field(default_factory=list)
    previous_setup: str = ""                    # what they had before UniFi
    biggest_frustration: str = ""               # free text


class HouseholdProfile(BaseModel):
    """Top-level operator profile — composition of 11 themed submodels.

    Captured by the onboarding agent through probe-driven conversation, not a
    flat form. Every other agent consults this to tune thresholds, severity
    bands, recommendation aggressiveness, and narrative tone.

    Field naming: submodel field names map to YAML keys (building, isp, ...)
    so the on-disk profile is readable and contributable as plain YAML.
    """

    mode: str = "unknown"                       # heritage | greenfield | unknown
    use_case_summary: str | None = None         # one-line summary captured from the operator
    building: BuildingProfile = Field(default_factory=BuildingProfile)
    isp: ISPProfile = Field(default_factory=ISPProfile)
    household: HouseholdComposition = Field(default_factory=HouseholdComposition)
    work: WorkProfile = Field(default_factory=WorkProfile)
    devices: DeviceEcosystem = Field(default_factory=DeviceEcosystem)
    security: SecurityPhilosophy = Field(default_factory=SecurityPhilosophy)
    usage: UsagePatterns = Field(default_factory=UsagePatterns)
    infrastructure: InfrastructureProfile = Field(default_factory=InfrastructureProfile)
    preferences: PreferencesProfile = Field(default_factory=PreferencesProfile)
    future: FutureStateProfile = Field(default_factory=FutureStateProfile)
    origin: OriginStoryProfile = Field(default_factory=OriginStoryProfile)

    # Probe progress tracking — which probe IDs have been answered, allowing
    # resumability across multiple onboarding sessions.
    probes_answered: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OriginStory(BaseModel):
    """Operator-supplied 'why does this exist?' note attached to a config artifact.

    Critical for heritage networks: the auditor must treat a port forward with
    a recorded rationale very differently from one with none. The DMZ that
    exists because of a solar installer's Zigbee gateway is not the same as a
    DMZ nobody can explain.
    """

    subject_kind: str                          # network | port_forward | firewall_rule | vlan | device | wifi
    subject_key: str                           # the identifying key for this kind (network name, rule _id, etc.)
    rationale: str                             # operator's explanation
    do_not_touch: bool = False                 # if true, agents must never recommend modifying this
    created_by: str = "operator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Dismissal(BaseModel):
    """Operator-confirmed suppression of a specific finding pattern.

    Generalizes the hardcoded `_KNOWN_OFFLINE` and `_KNOWN_CAPTIVE_PORTAL_SSIDS`
    allowlists into per-fork operator-controlled config. When the auditor
    produces a Finding, it consults the dismissals registry: if any active
    dismissal matches the finding's code and evidence, the finding is
    suppressed (or downgraded to INFO with the dismissal reason attached).

    TTL + auto-revocation (directive 1.4)
    -------------------------------------
    A dismissal is a claim about network state ("camera is intentionally
    offline") that ages out of truth. Every dismissal therefore has an
    expiry: the on-disk schema permits `expires_at` to be absent, but
    `DismissalRegistry.load()` assigns a default 90-day TTL to any entry
    missing one and surfaces a warning. After that, expired entries no
    longer suppress findings (matches() filters them) AND are surfaced
    by `stale_dismissals()` for the auditor to flag as INFO findings —
    so the operator is prompted to re-confirm rather than silently losing
    the suppression.

    `reconfirm_on_change=True` enables fingerprint-based auto-revocation:
    if the live state's stable attributes for the target hash differently
    than the captured `target_fingerprint`, the dismissal is treated as
    inactive and a "dismissal stale — target attributes changed"
    notification is emitted. Aliasing defence per Waiter §5.4.
    """

    finding_code: str                          # e.g. WIFI_NO_ENCRYPTION, DEVICE_OFFLINE_UNEXPECTED
    match_field: str                           # which evidence field to match (ssid, name, mac, rule_name, ...)
    match_key: str                             # the value that evidence[match_field] must equal
    reason: str                                # operator's rationale — surfaced in INFO downgrade
    scope: str = "permanent"                   # permanent | until-date | n-days  (informational; expires_at is authoritative)
    expires_at: datetime | None = None         # effectively mandatory; legacy entries get a default at load time
    target_fingerprint: str | None = None      # sha256 of stable attributes — set when reconfirm_on_change=True
    target_fingerprint_alg: str = "sha256-v1"  # versioned so the algorithm can evolve without invalidating older entries
    reconfirm_on_change: bool = False          # auto-revoke when fingerprint diverges
    created_by: str = "operator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChangeReview(BaseModel):
    """Output of `AIRuntime.review_config_change` — independent assessment of a proposed change."""

    verdict: str                    # "safe" | "risky" | "block"
    reasoning: str
    concerns: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    suggested_alternatives: list[str] = Field(default_factory=list)
    generated_by: str
    model_used: str | None = None
    token_usage: dict[str, int] | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
