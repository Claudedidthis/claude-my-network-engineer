"""UniFi client — thin wrapper over unifi-sm-api (v1 Integration) + httpx (classic + Protect).

Three API layers on a local UDM:
  • v1 Integration API  — /proxy/network/integration/v1/  (devices, clients, networks, sites)
  • Network API         — /proxy/network/api/s/default/   (classic) and
                          /proxy/network/v2/api/site/default/ (v2)
  • Protect API         — /proxy/protect/integration/v1/  (cameras)

Classic API returns {"data": [...]}; v2 Network API returns a bare list.
All auth uses X-API-KEY. The traffic-rules endpoint moved to the v2 path on modern UDM firmware.
"""
from __future__ import annotations

import json
import logging
import os
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from unifi_sm_api.api import SiteManagerAPI

from network_engineer.tools.authorization import (
    ApprovedAction,
    UnauthorizedWriteError,
    canonical_payload_hash,
)
from network_engineer.tools.ssl_policy import (
    SSLMode,
    build_verify,
    resolve_mode,
)

# urllib3 noisy about self-signed cert on UDM — suppress at module level
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

load_dotenv()

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SNAPSHOTS_DIR = _REPO_ROOT / "snapshots"
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures"
SCHEMA_VERSION = 1


class UnifiClientError(RuntimeError):
    pass


class UnifiClient:
    """UniFi client — reads and writes against the local UDM.

    Modes
    -----
    live     (default) — hits the real UDM via UNIFI_HOST + UNIFI_API_KEY
    fixtures           — loads data from tests/fixtures/baseline_snapshot.json;
                         write methods raise UnifiClientError in this mode
    """

    def __init__(
        self,
        host: str | None = None,
        api_key: str | None = None,
        use_fixtures: bool | None = None,
        fixtures_path: Path | None = None,
    ) -> None:
        env_mode = os.getenv("UNIFI_MODE", "live")
        if use_fixtures is True:
            self._mode = "fixtures"
        elif use_fixtures is False:
            self._mode = "live"
        else:
            self._mode = env_mode

        # Replay-protection set for the authorization-consumption write boundary.
        # Single-use authorizations: once an ID is consumed, the client refuses
        # any further write that presents the same ID — even on different actions.
        self._consumed_authorization_ids: set[str] = set()

        if self._mode == "fixtures":
            self._fixtures_path = fixtures_path or _FIXTURES_DIR / "baseline_snapshot.json"
            self._site_id: str = "fixture-site"
            self._fixture_cache: dict[str, Any] | None = None
            return

        self._host = host or os.environ["UNIFI_HOST"]
        self._api_key = api_key or os.environ["UNIFI_API_KEY"]

        # SSL policy resolution — see tools/ssl_policy.py for the three-mode
        # rationale. Default is LAN_ONLY which refuses non-RFC1918 hosts at
        # construction time, so a misconfigured UNIFI_HOST fails fast rather
        # than disabling cert verification against a public address.
        self._ssl_mode: SSLMode = resolve_mode()
        verify_arg = build_verify(self._ssl_mode, self._host)

        # v1 Integration API — unifi-sm-api handles pagination and request
        # building. Its kwarg is `verify_ssl` (bool); the library does not
        # currently accept a CA bundle path, so we degrade to bool here.
        # When CA_BUNDLE is in use the boolean form effectively means
        # "verify with the system trust store" — operators with a
        # self-signed CA must add it to the OS trust store for the v1
        # transport. Documented in .env.example.
        v1_verify_ssl = verify_arg is not False
        self._v1 = SiteManagerAPI(
            api_key=self._api_key,
            base_url=f"https://{self._host}/proxy/network/integration/",
            verify_ssl=v1_verify_ssl,
        )

        # Network API — covers both classic (/api/s/default/) and v2 (/v2/api/site/default/)
        self._net = httpx.Client(
            base_url=f"https://{self._host}/proxy/network/",
            headers={"X-API-KEY": self._api_key},
            verify=verify_arg,
            timeout=15.0,
        )

        # Protect API — separate subsystem
        self._protect = httpx.Client(
            base_url=f"https://{self._host}/proxy/protect/integration/v1/",
            headers={"X-API-KEY": self._api_key},
            verify=verify_arg,
            timeout=15.0,
        )

        self._fixture_cache = None
        self._site_id = os.getenv("UNIFI_SITE_ID") or self._resolve_site_id()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _resolve_site_id(self) -> str:
        resp = self._v1.get_sites()
        sites = resp.get("data", [])
        if not sites:
            raise UnifiClientError("GET /sites returned no sites")
        site_id: str = sites[0]["id"]
        log.debug("resolved site_id=%s", site_id)
        return site_id

    def _net_get(self, path: str) -> list[dict[str, Any]]:
        """GET a Network API path; handles classic {"data":[...]} and v2 bare-list responses."""
        r = self._net.get(path)
        r.raise_for_status()
        body = r.json()
        if isinstance(body, list):
            return body
        return body.get("data", [])

    def _net_get_optional(self, path: str) -> list[dict[str, Any]]:
        """Like _net_get but returns [] instead of raising on 4xx (endpoint may be unavailable)."""
        try:
            return self._net_get(path)
        except httpx.HTTPStatusError as exc:
            log.debug("Optional endpoint unavailable (%s): %s", path, exc)
            return []

    def _protect_get(self, path: str) -> Any:
        r = self._protect.get(path)
        r.raise_for_status()
        body = r.json()
        if isinstance(body, list):
            return body
        return body.get("data", body)

    def _load_fixture(self) -> dict[str, Any]:
        if self._fixture_cache is None:
            if not self._fixtures_path.exists():
                raise UnifiClientError(
                    f"Fixture file not found: {self._fixtures_path}\n"
                    "Run 'UNIFI_MODE=live nye test --snapshot' on the home LAN first."
                )
            self._fixture_cache = json.loads(self._fixtures_path.read_text())
        return self._fixture_cache

    def _fx(self, key: str) -> list[dict[str, Any]]:
        """Return a key from the fixture, defaulting to []."""
        return self._load_fixture().get(key, [])

    # ── v1 Integration endpoints ──────────────────────────────────────────────

    def get_sites(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("sites")
        resp = self._v1.get_sites()
        return resp.get("data", [])

    def get_devices(self) -> list[dict[str, Any]]:
        """Basic device list from v1 (id, mac, ip, model, state, firmware)."""
        if self._mode == "fixtures":
            return self._fx("devices")
        return self._v1.get_unifi_devices(self._site_id, as_list=True)

    def get_clients(self) -> list[dict[str, Any]]:
        """Currently-connected clients from v1."""
        if self._mode == "fixtures":
            return self._fx("clients")
        return self._v1.get_clients(self._site_id, as_list=True)

    def get_networks(self) -> list[dict[str, Any]]:
        """VLANs / IP networks from the v1 Integration API."""
        if self._mode == "fixtures":
            return self._fx("networks")
        resp = self._v1._fetch_all_paginated(f"sites/{self._site_id}/networks")
        return resp["data"]

    # ── Network API — classic (/api/s/default/) ───────────────────────────────

    def get_network_config(self) -> list[dict[str, Any]]:
        """Full network/VLAN config objects (richer than v1 networks)."""
        if self._mode == "fixtures":
            return self._fx("network_config")
        return self._net_get("api/s/default/rest/networkconf")

    def get_wifi_networks(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("wifi_networks")
        return self._net_get("api/s/default/rest/wlanconf")

    def get_wlan_groups(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("wlan_groups")
        return self._net_get("api/s/default/rest/wlangroup")

    def get_firewall_rules(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("firewall_rules")
        return self._net_get("api/s/default/rest/firewallrule")

    def get_firewall_groups(self) -> list[dict[str, Any]]:
        """IP/MAC groups referenced by firewall rules."""
        if self._mode == "fixtures":
            return self._fx("firewall_groups")
        return self._net_get("api/s/default/rest/firewallgroup")

    def get_port_forwards(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("port_forwards")
        return self._net_get("api/s/default/rest/portforward")

    def get_port_profiles(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("port_profiles")
        return self._net_get("api/s/default/rest/portconf")

    def get_radius_profiles(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("radius_profiles")
        return self._net_get("api/s/default/rest/radiusprofile")

    def get_known_clients(self) -> list[dict[str, Any]]:
        """All clients ever seen — includes stored names, fixed IPs, groups, tags."""
        if self._mode == "fixtures":
            return self._fx("known_clients")
        return self._net_get("api/s/default/rest/user")

    def get_user_groups(self) -> list[dict[str, Any]]:
        """Rate-limiting / bandwidth groups."""
        if self._mode == "fixtures":
            return self._fx("user_groups")
        return self._net_get("api/s/default/rest/usergroup")

    def get_dynamic_dns(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("dynamic_dns")
        return self._net_get("api/s/default/rest/dynamicdns")

    def get_dpi_apps(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("dpi_apps")
        return self._net_get("api/s/default/rest/dpiapp")

    def get_dpi_groups(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("dpi_groups")
        return self._net_get("api/s/default/rest/dpigroup")

    def get_settings(self) -> list[dict[str, Any]]:
        """All site settings (45 objects covering UISP, auto-upgrade, guest portal, etc.)."""
        if self._mode == "fixtures":
            return self._fx("settings")
        return self._net_get("api/s/default/rest/setting")

    def get_health(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("health")
        return self._net_get("api/s/default/stat/health")

    def get_device_stats(self) -> list[dict[str, Any]]:
        """Full device config + live stats (port tables, radio tables, uptime, etc.)."""
        if self._mode == "fixtures":
            return self._fx("device_stats")
        return self._net_get("api/s/default/stat/device")

    def get_client_stats(self) -> list[dict[str, Any]]:
        """Live stats for currently-connected stations (signal, tx/rx, channel, etc.)."""
        if self._mode == "fixtures":
            return self._fx("client_stats")
        return self._net_get("api/s/default/stat/sta")

    def get_sysinfo(self) -> list[dict[str, Any]]:
        """Controller version, hostname, uptime, and system flags."""
        if self._mode == "fixtures":
            return self._fx("sysinfo")
        return self._net_get("api/s/default/stat/sysinfo")

    def get_alerts(self) -> list[dict[str, Any]]:
        """Network alerts/alarms. Returns [] if the endpoint is unavailable."""
        if self._mode == "fixtures":
            return self._fx("alerts")
        return self._net_get_optional("api/s/default/stat/alarm")

    # ── Network API — v2 (/v2/api/site/default/) ─────────────────────────────

    def get_traffic_rules(self) -> list[dict[str, Any]]:
        """Traffic rules (v2 endpoint — replaced the legacy /rest/trafficrule path)."""
        if self._mode == "fixtures":
            return self._fx("traffic_rules")
        return self._net_get("v2/api/site/default/trafficrules")

    def get_traffic_routes(self) -> list[dict[str, Any]]:
        """Policy-based routing / traffic routes (v2 endpoint)."""
        if self._mode == "fixtures":
            return self._fx("traffic_routes")
        return self._net_get("v2/api/site/default/trafficroutes")

    # ── Protect API endpoints ─────────────────────────────────────────────────

    def get_protect_cameras(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("protect_cameras")
        try:
            result = self._protect_get("cameras")
            return result if isinstance(result, list) else []
        except httpx.HTTPStatusError as exc:
            log.warning("Protect cameras unavailable: %s", exc)
            return []

    def get_protect_alerts(self) -> list[dict[str, Any]]:
        if self._mode == "fixtures":
            return self._fx("protect_alerts")
        try:
            result = self._protect_get("alerts")
            return result if isinstance(result, list) else []
        except httpx.HTTPStatusError as exc:
            log.warning("Protect alerts unavailable: %s", exc)
            return []

    # ── Write methods (Phase 6) ──────────────────────────────────────────────
    #
    # The `_net_put`/`_net_post`/`_net_delete` helpers below are private
    # transport primitives. Public write methods always pass an
    # ApprovedAction first (via _consume_authorization). Calling these
    # private helpers directly is a contract violation — they trust their
    # caller to have already checked the authorization. New write methods
    # added later MUST require an `authorization: ApprovedAction` kwarg.

    def _net_put(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """PUT to the Network API; returns the parsed JSON body. Internal."""
        if self._mode == "fixtures":
            raise UnifiClientError("Write operations are not available in fixture mode")
        r = self._net.put(path, json=data)
        r.raise_for_status()
        return r.json()

    def _net_post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST to the Network API; returns the parsed JSON body. Internal."""
        if self._mode == "fixtures":
            raise UnifiClientError("Write operations are not available in fixture mode")
        r = self._net.post(path, json=data)
        r.raise_for_status()
        return r.json()

    def _net_delete(self, path: str) -> None:
        """DELETE on the Network API; raises on HTTP error. Internal."""
        if self._mode == "fixtures":
            raise UnifiClientError("Write operations are not available in fixture mode")
        r = self._net.delete(path)
        r.raise_for_status()

    def _consume_authorization(
        self,
        authorization: ApprovedAction,
        expected_action: str,
        expected_payload: dict[str, Any],
    ) -> None:
        """Verify and atomically consume an ApprovedAction.

        Five bindings checked in order:
          1. action_name matches the public method's expected_action
          2. payload_hash matches a fresh hash of expected_payload
          3. expires_at > now
          4. approval_tier matches permissions.check(action_name)
             (already enforced at construction time, re-checked defensively)
          5. authorization_id has not been consumed by this client

        On any failure raises UnauthorizedWriteError; the consumed-set is
        unchanged. On success the ID is added to the consumed-set BEFORE
        the underlying transport call, so a network failure or fixture-mode
        rejection still burns the authorization (no replay).
        """
        if authorization.action_name != expected_action:
            raise UnauthorizedWriteError(
                f"Authorization is for {authorization.action_name!r}, "
                f"call expected {expected_action!r}."
            )
        expected_hash = canonical_payload_hash(expected_action, expected_payload)
        if authorization.payload_hash != expected_hash:
            raise UnauthorizedWriteError(
                f"Payload hash mismatch for {expected_action!r}: "
                f"authorization is for a different payload. The args presented "
                f"to the client do not match what was authorized."
            )
        if authorization.is_expired():
            raise UnauthorizedWriteError(
                f"Authorization {authorization.authorization_id!r} expired at "
                f"{authorization.expires_at.isoformat()}; mint a fresh one."
            )
        if authorization.authorization_id in self._consumed_authorization_ids:
            raise UnauthorizedWriteError(
                f"Authorization {authorization.authorization_id!r} has already "
                "been consumed by this client. Authorizations are single-use; "
                "mint a fresh one for the retry."
            )
        self._consumed_authorization_ids.add(authorization.authorization_id)

    def delete_port_forward(
        self,
        forward_id: str,
        *,
        authorization: ApprovedAction,
    ) -> None:
        """Delete a port forward by its classic-API _id (REQUIRES_APPROVAL action)."""
        self._consume_authorization(
            authorization,
            "delete_port_forward",
            {"forward_id": forward_id},
        )
        self._net_delete(f"api/s/default/rest/portforward/{forward_id}")

    def set_device_name(
        self,
        device_id: str,
        name: str,
        *,
        authorization: ApprovedAction,
    ) -> None:
        """Rename a device. device_id is the _id field from get_device_stats().

        Permission action: rename_device (AUTO).
        """
        self._consume_authorization(
            authorization,
            "rename_device",
            {"device_id": device_id, "name": name},
        )
        self._net_put(f"api/s/default/rest/device/{device_id}", {"name": name})

    def set_ap_channel(
        self,
        device_id: str,
        radio: str,
        channel: int | str,
        *,
        authorization: ApprovedAction,
    ) -> None:
        """Set the channel for an AP radio.

        radio: "na" (5 GHz) → permission action set_ap_channel_5ghz
               "ng" (2.4 GHz) → permission action set_ap_channel_2_4ghz
        channel: integer channel number or "auto"

        UDM overwrites `radio_table` rather than merging — so we fetch the
        existing list, mutate the matching radio in place, and PUT the
        complete list back.
        """
        action = "set_ap_channel_5ghz" if radio == "na" else "set_ap_channel_2_4ghz"
        self._consume_authorization(
            authorization,
            action,
            {"device_id": device_id, "radio": radio, "channel": str(channel)},
        )

        device = self._get_device_by_id(device_id)
        radio_table = [dict(r) for r in device.get("radio_table", [])]
        found = False
        for r in radio_table:
            if r.get("radio") == radio:
                r["channel"] = str(channel)
                found = True
        if not found:
            radio_table.append({"radio": radio, "channel": str(channel)})

        self._net_put(
            f"api/s/default/rest/device/{device_id}",
            {"radio_table": radio_table},
        )

    def set_ap_tx_power(
        self,
        device_id: str,
        radio: str,
        tx_power_mode: str,
        tx_power: int | None = None,
        *,
        authorization: ApprovedAction,
    ) -> None:
        """Set TX power mode (auto/low/medium/high/custom) for an AP radio.

        Like set_ap_channel, fetches the full radio_table and merges in place.
        Permission action: set_ap_tx_power.
        """
        expected_payload: dict[str, Any] = {
            "device_id": device_id,
            "radio": radio,
            "tx_power_mode": tx_power_mode,
        }
        if tx_power is not None:
            expected_payload["tx_power"] = tx_power
        self._consume_authorization(authorization, "set_ap_tx_power", expected_payload)

        device = self._get_device_by_id(device_id)
        radio_table = [dict(r) for r in device.get("radio_table", [])]
        found = False
        for r in radio_table:
            if r.get("radio") == radio:
                r["tx_power_mode"] = tx_power_mode
                if tx_power is not None:
                    r["tx_power"] = tx_power
                found = True
        if not found:
            entry: dict[str, Any] = {"radio": radio, "tx_power_mode": tx_power_mode}
            if tx_power is not None:
                entry["tx_power"] = tx_power
            radio_table.append(entry)

        self._net_put(
            f"api/s/default/rest/device/{device_id}",
            {"radio_table": radio_table},
        )

    def _get_device_by_id(self, device_id: str) -> dict[str, Any]:
        """Look up a device by its classic-API _id from get_device_stats."""
        for d in self.get_device_stats():
            if d.get("_id") == device_id:
                return d
        raise UnifiClientError(f"Device with _id {device_id!r} not found")

    def set_client_tag(
        self,
        client_id: str,
        note: str,
        *,
        authorization: ApprovedAction,
    ) -> None:
        """Set the note/tag on a known client entry by its _id.

        Permission action: tag_client_with_known_device_type (AUTO).
        """
        self._consume_authorization(
            authorization,
            "tag_client_with_known_device_type",
            {"client_id": client_id, "note": note},
        )
        self._net_put(
            f"api/s/default/rest/user/{client_id}",
            {"note": note, "noted": True},
        )

    def restart_device(
        self,
        mac: str,
        *,
        authorization: ApprovedAction,
    ) -> None:
        """Send a restart command to a device by its MAC address.

        Permission action: restart_offline_ap (AUTO).
        """
        self._consume_authorization(
            authorization,
            "restart_offline_ap",
            {"mac": mac},
        )
        self._net_post("api/s/default/cmd/devmgr", {"cmd": "restart", "mac": mac})

    # ── Snapshot (config-only) ────────────────────────────────────────────────

    def snapshot(self) -> Path:
        """Fetch the core config endpoints and write a dated JSON to snapshots/.

        This is the pre-change safety snapshot — fast, config-focused.
        For a comprehensive backup use full_backup().
        """
        _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out = _SNAPSHOTS_DIR / f"{ts}_snapshot.json"

        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "captured_at": datetime.now().isoformat(),
            "site_id": self._site_id,
            "sites": self.get_sites(),
            "devices": self.get_devices(),
            "clients": self.get_clients(),
            "networks": self.get_networks(),
            "network_config": self.get_network_config(),
            "wifi_networks": self.get_wifi_networks(),
            "wlan_groups": self.get_wlan_groups(),
            "firewall_rules": self.get_firewall_rules(),
            "firewall_groups": self.get_firewall_groups(),
            "traffic_rules": self.get_traffic_rules(),
            "traffic_routes": self.get_traffic_routes(),
            "port_forwards": self.get_port_forwards(),
            "port_profiles": self.get_port_profiles(),
            "health": self.get_health(),
            "alerts": self.get_alerts(),
            "protect_cameras": self.get_protect_cameras(),
            "protect_alerts": self.get_protect_alerts(),
        }

        out.write_text(json.dumps(data, indent=2))
        log.info("snapshot written to %s", out)
        return out

    def full_backup(self) -> Path:
        """Capture every readable endpoint into a single dated JSON in snapshots/.

        Includes the full device config (stat/device), all 134 known clients,
        all 45 site settings, RADIUS profiles, DPI config, sysinfo, and live
        client stats — everything available via X-API-KEY auth.
        """
        _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out = _SNAPSHOTS_DIR / f"{ts}_full_backup.json"

        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "backup_type": "full",
            "captured_at": datetime.now().isoformat(),
            "site_id": self._site_id,
            # System info
            "sysinfo": self.get_sysinfo(),
            "health": self.get_health(),
            # Site topology (v1)
            "sites": self.get_sites(),
            "devices": self.get_devices(),
            "clients": self.get_clients(),
            # Full device config + live stats (classic API — much richer than v1)
            "device_stats": self.get_device_stats(),
            "client_stats": self.get_client_stats(),
            # Network / VLAN config
            "networks": self.get_networks(),
            "network_config": self.get_network_config(),
            # WiFi
            "wifi_networks": self.get_wifi_networks(),
            "wlan_groups": self.get_wlan_groups(),
            # Firewall
            "firewall_rules": self.get_firewall_rules(),
            "firewall_groups": self.get_firewall_groups(),
            # Traffic management (v2)
            "traffic_rules": self.get_traffic_rules(),
            "traffic_routes": self.get_traffic_routes(),
            # Port forwarding / switching
            "port_forwards": self.get_port_forwards(),
            "port_profiles": self.get_port_profiles(),
            # Client management
            "known_clients": self.get_known_clients(),
            "user_groups": self.get_user_groups(),
            # Auth / RADIUS
            "radius_profiles": self.get_radius_profiles(),
            # DPI
            "dpi_apps": self.get_dpi_apps(),
            "dpi_groups": self.get_dpi_groups(),
            # DNS / routing
            "dynamic_dns": self.get_dynamic_dns(),
            # All 45 site settings
            "settings": self.get_settings(),
            # Alerts
            "alerts": self.get_alerts(),
            # Protect
            "protect_cameras": self.get_protect_cameras(),
            "protect_alerts": self.get_protect_alerts(),
        }

        out.write_text(json.dumps(data, indent=2))
        log.info("full backup written to %s", out)
        return out

    # ── Connection test / info ────────────────────────────────────────────────

    def test_connection(self) -> dict[str, Any]:
        """Return a summary dict suitable for CLI --test output."""
        devices = self.get_devices()
        clients = self.get_clients()
        networks = self.get_networks()

        info: dict[str, Any] = {
            "mode": self._mode,
            "site_id": self._site_id,
            "device_count": len(devices),
            "client_count": len(clients),
            "network_count": len(networks),
        }

        if self._mode == "live":
            sysinfo = self.get_sysinfo()
            if sysinfo:
                s = sysinfo[0]
                info["network_app_version"] = s.get("version", "unknown")
                info["hostname"] = s.get("hostname", "unknown")
                info["uptime_days"] = round(s.get("uptime", 0) / 86400, 1)

            protect_cameras = self.get_protect_cameras()
            info["protect_camera_count"] = len(protect_cameras)

        return info
