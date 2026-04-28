"""nye — the ClaudeMyNetworkEngineer command-line entry point.

Per docs/agent_architecture.md §12.8 (decided 2026-04-26): bare `nye` drops
into the Conductor REPL — the LLM-driven conversational agent. Existing
subcommands (audit, monitor, optimize, security, etc.) survive as
ergonomic shortcuts but the primary interaction surface is the Conductor.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from network_engineer import __version__
from network_engineer.tools.logging_setup import configure_logging

_SEVERITY_ICON = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵", "INFO": "⚪"}


def _cmd_audit(args: argparse.Namespace) -> int:
    from network_engineer.tools.auditor import run_from_client
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    try:
        client = UnifiClient()
        findings = run_from_client(client)
    except UnifiClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([f.model_dump(mode="json") for f in findings], indent=2))
        return 0

    if not findings:
        print("No findings — network looks clean.")
        return 0

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    summary = "  ".join(f"{_SEVERITY_ICON.get(k, k)} {k}: {v}" for k, v in counts.items())
    print(f"\nAudit complete — {len(findings)} finding(s)   {summary}\n")

    for f in findings:
        icon = _SEVERITY_ICON.get(f.severity, "")
        print(f"{icon} [{f.severity}] {f.code}")
        print(f"   {f.title}")
        print(f"   {f.detail}")
        print()

    return 1 if counts.get("CRITICAL") or counts.get("HIGH") else 0


def _cmd_report(args: argparse.Namespace) -> int:
    from network_engineer.tools.auditor import run_from_client
    from network_engineer.tools.reporter import audit_report, changes_report, daily_report
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    kind = args.type

    try:
        if kind == "changes":
            text = changes_report(days=args.days)
        else:
            client = UnifiClient()
            findings = run_from_client(client)
            if kind == "audit":
                text = audit_report(findings)
            else:  # daily
                info = client.test_connection()
                text = daily_report(findings, info)
    except UnifiClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(text)
        print(f"Report written to {args.out}")
    else:
        print(text)
    return 0


def _cmd_upgrade(args: argparse.Namespace) -> int:
    from network_engineer.tools.upgrade_agent import render_markdown, scan, to_json_log_format
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    try:
        client = UnifiClient()
        if args.upgrade_cmd == "scan":
            recs = scan(client)
            if args.json:
                print(json.dumps(to_json_log_format(recs), indent=2, default=str))
            else:
                print(render_markdown(recs))
            return 0
    except UnifiClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_onboard(args: argparse.Namespace) -> int:
    from network_engineer.agents.onboarding_agent import onboard
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    client = None
    if not args.profile_only:
        try:
            client = UnifiClient()
        except UnifiClientError as exc:
            print(f"WARN: live UDM unreachable ({exc}). Running profile-only.",
                  file=sys.stderr)
    onboard(client)
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    from network_engineer.tools.profile import has_profile, load_profile

    if not has_profile():
        print("No profile yet. Run `nye onboard` to capture one.", file=sys.stderr)
        return 1
    profile = load_profile()
    print(json.dumps(profile.model_dump(mode="json", exclude_none=True),
                     indent=2, default=str))
    return 0


def _cmd_registry(args: argparse.Namespace) -> int:
    from network_engineer.agents.registry_agent import bootstrap, walkthrough
    from network_engineer.tools.registry import (
        Registry,
        manufacturer_for_mac,
        normalize_mac,
    )
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    if args.registry_cmd == "init":
        try:
            client = UnifiClient()
        except UnifiClientError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        registry, counts = bootstrap(client)
        print("Bootstrapped registry:")
        print(f"  • {counts['devices_added']} device(s) added")
        print(f"  • {counts['clients_added']} client(s) added "
              f"({counts['clients_auto_classified']} auto-classified)")
        print(f"  → {registry.device_path}")
        print(f"  → {registry.client_path}")
        print("\nRun `nye registry walkthrough` to fill in operator details.")
        return 0

    if args.registry_cmd == "walkthrough":
        try:
            client = UnifiClient()
        except UnifiClientError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        walkthrough(client)
        return 0

    if args.registry_cmd == "list":
        registry = Registry.load()
        print(f"Devices ({len(registry.devices)}):")
        for entry in sorted(registry.devices.values(), key=lambda e: e.mac):
            loc = entry.location or "(no location)"
            crit = entry.criticality or "—"
            print(f"  {entry.mac}  {entry.name_hint or '?':25s}  {loc:25s}  {crit}")
        print(f"\nClients ({len(registry.clients)}):")
        for entry in sorted(registry.clients.values(), key=lambda e: e.mac):
            tier = entry.tier_override or "?"
            owner = entry.owner or "?"
            loc = entry.location or "?"
            print(f"  {entry.mac}  {entry.name_hint or '?':25s}  "
                  f"{tier:8s}  {owner:8s}  {loc}")
        return 0

    if args.registry_cmd == "show":
        registry = Registry.load()
        mac = normalize_mac(args.mac)
        entry = registry.get_device(mac) or registry.get_client(mac)
        if entry is None:
            print(f"No registry entry for {mac}", file=sys.stderr)
            return 1
        print(json.dumps(entry.model_dump(mode="json"), indent=2, default=str))
        print(f"\nManufacturer (from OUI): {manufacturer_for_mac(mac)}")
        return 0

    return 0


def _cmd_security(args: argparse.Namespace) -> int:
    from network_engineer.agents.security_agent import propose_vlans, render_markdown
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    try:
        client = UnifiClient()
        if args.security_cmd == "propose-vlans":
            rec = propose_vlans(client)
            if args.json:
                print(json.dumps(rec.model_dump(mode="json"), indent=2, default=str))
            else:
                print(render_markdown(rec))
            return 0
    except UnifiClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_ai(args: argparse.Namespace) -> int:
    from network_engineer.agents.ai_runtime import AIRuntime, AIRuntimeError
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    try:
        runtime = AIRuntime()

        if args.ai_cmd == "analyze":
            client = UnifiClient()
            snapshot = {
                "networks": client.get_networks(),
                "wifi_networks": client.get_wifi_networks(),
                "clients": client.get_clients(),
                "firewall_rules": client.get_firewall_rules(),
                "port_forwards": client.get_port_forwards(),
                "devices": client.get_devices(),
            }
            analysis = runtime.analyze_security_posture(snapshot)
            print(json.dumps(analysis.model_dump(mode="json"), indent=2, default=str))
            return 0

        if args.ai_cmd == "review":
            try:
                proposed = json.loads(args.proposed)
            except json.JSONDecodeError as exc:
                print(f"ERROR: --proposed must be JSON: {exc}", file=sys.stderr)
                return 2
            current = json.loads(args.current) if args.current else {}
            review = runtime.review_config_change(proposed, current, action=args.action)
            print(json.dumps(review.model_dump(mode="json"), indent=2, default=str))
            return 0

    except (AIRuntimeError, UnifiClientError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


def _cmd_optimize(args: argparse.Namespace) -> int:
    from network_engineer.tools.optimizer import (
        OptimizerError,
        rename_device,
        resolve_channel_conflicts,
    )
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    try:
        client = UnifiClient()

        if args.optimize_cmd == "channel-fix":
            results = resolve_channel_conflicts(client)
            if not results:
                print("No channel conflicts detected — nothing to fix.")
                return 0
            for r in results:
                icon = "✅" if r.status == "applied" else "⚠️"
                print(f"{icon} {r.action}: {r.status}  — {r.detail.get('verify_note', '')}")
                if r.snapshot_after:
                    print(f"   snapshots: {r.snapshot_before.name} → {r.snapshot_after.name}")
            failed = sum(1 for r in results if r.status != "applied")
            return 1 if failed else 0

        if args.optimize_cmd == "rename":
            result = rename_device(client, args.device, args.new_name)
            icon = "✅" if result.status == "applied" else "⚠️"
            note = result.detail.get("verify_note", "")
            print(f"{icon} {result.action}: {result.status}  — {note}")
            if result.snapshot_after:
                print(f"   snapshots: {result.snapshot_before.name} → {result.snapshot_after.name}")
            return 0 if result.status == "applied" else 1

    except (UnifiClientError, OptimizerError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


def _cmd_monitor(args: argparse.Namespace) -> int:
    from network_engineer.tools.monitor import _print_sweep, run_from_client, watch
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    try:
        client = UnifiClient()
        if args.watch:
            watch(client, interval=args.interval)
            return 0
        events = run_from_client(client)
        _print_sweep(events)
        critical = sum(1 for e in events if e.severity == "CRITICAL")
        high = sum(1 for e in events if e.severity == "HIGH")
        return 1 if critical or high else 0
    except UnifiClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_test(args: argparse.Namespace) -> int:
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    try:
        client = UnifiClient()
        info = client.test_connection()
        if args.snapshot:
            snap = client.snapshot()
            info["snapshot"] = str(snap)
        print(json.dumps(info, indent=2))
        return 0
    except UnifiClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    """Start the FastAPI server hosting the Conductor web UI.

    Stage 1 scaffold: server boots, browser-side single-page UI loads,
    WebSocket echoes. Stage 2 wires the Conductor through the WS so the
    conversation lands in the browser as message bubbles.
    """
    try:
        from network_engineer.ui.server import serve
    except ImportError as exc:
        print(
            "nye serve requires the [server] extra:\n"
            "  pip install '.[server]'\n"
            f"(import failed: {exc})",
            file=__import__("sys").stderr,
        )
        return 1
    serve(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    """Drop into the Conductor REPL. Bare `nye` invokes this implicitly."""
    from network_engineer.agents.conductor import Conductor, ConductorConfig
    from network_engineer.agents.ai_runtime import AIRuntime
    from network_engineer.tools.unifi_client import UnifiClient, UnifiClientError

    # Optional UnifiClient — Conductor handles None gracefully (will ask
    # the operator about connection in the bootstrap conversation).
    try:
        client = UnifiClient()
    except (UnifiClientError, KeyError, RuntimeError):
        client = None

    ai = AIRuntime()
    config = ConductorConfig(
        model_alias=getattr(args, "model", "sonnet") or "sonnet",
        max_turns=getattr(args, "max_turns", 100),
    )
    conductor = Conductor(config=config, ai_runtime=ai, unifi_client=client)
    conductor.run()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="nye",
        description="ClaudeMyNetworkEngineer — an AI-powered network engineer for UniFi.\n\n"
                    "Run with no subcommand to drop into the Conductor REPL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"nye {__version__}")
    # Bare `nye` drops into Conductor — subcommand no longer required.
    subparsers = parser.add_subparsers(dest="command", required=False)

    # ── chat — explicit Conductor entry point ────────────────────────────
    chat_p = subparsers.add_parser(
        "chat",
        help="Drop into the Conductor REPL (the default for bare `nye`)",
    )
    chat_p.add_argument(
        "--model", choices=["sonnet", "opus", "haiku"], default="sonnet",
        help="Model alias to use for agent turns (default: sonnet)",
    )
    chat_p.add_argument(
        "--max-turns", type=int, default=100,
        help="Maximum turns before the loop exhausts (default: 100)",
    )
    chat_p.set_defaults(func=_cmd_chat)

    test_p = subparsers.add_parser("test", help="Verify UniFi connectivity and print a summary")
    test_p.add_argument(
        "--snapshot", action="store_true", help="Also capture a full snapshot to snapshots/"
    )
    test_p.set_defaults(func=_cmd_test)

    audit_p = subparsers.add_parser("audit", help="Run a read-only network audit")
    audit_p.add_argument("--json", action="store_true", help="Output findings as JSON")
    audit_p.set_defaults(func=_cmd_audit)
    report_p = subparsers.add_parser("report", help="Generate a human-readable report")
    report_p.add_argument(
        "type", choices=["audit", "daily", "changes"], default="daily", nargs="?",
        help="Report type (default: daily)",
    )
    report_p.add_argument("--out", metavar="FILE", help="Write report to FILE instead of stdout")
    report_p.add_argument("--days", type=int, default=7, help="Days of history for changes report")
    report_p.set_defaults(func=_cmd_report)

    monitor_p = subparsers.add_parser("monitor", help="Run a monitor sweep or watch loop")
    monitor_p.add_argument(
        "--watch", action="store_true", help="Poll continuously (Ctrl-C to stop)"
    )
    monitor_p.add_argument(
        "--interval", type=int, default=300, metavar="SECS",
        help="Seconds between sweeps in watch mode (default: 300)",
    )
    monitor_p.set_defaults(func=_cmd_monitor)

    upgrade_p = subparsers.add_parser("upgrade", help="Upgrade Agent — score devices vs catalog")
    upgrade_sub = upgrade_p.add_subparsers(dest="upgrade_cmd", required=True)
    scan_p = upgrade_sub.add_parser("scan", help="Run a sweep against the live device list")
    scan_p.add_argument("--json", action="store_true", help="Output JSON instead of markdown")
    upgrade_p.set_defaults(func=_cmd_upgrade)

    onboard_p = subparsers.add_parser(
        "onboard",
        help="Interactive probe-driven onboarding (household profile + heritage walkthrough)",
    )
    onboard_p.add_argument(
        "--profile-only", action="store_true",
        help="Capture profile only; skip the live-UDM heritage walkthrough",
    )
    onboard_p.set_defaults(func=_cmd_onboard)

    profile_p = subparsers.add_parser(
        "profile", help="Show the captured household profile as JSON",
    )
    profile_p.set_defaults(func=_cmd_profile)

    reg_p = subparsers.add_parser("registry", help="Device + client registry (operator knowledge)")
    reg_sub = reg_p.add_subparsers(dest="registry_cmd", required=True)
    reg_sub.add_parser("init", help="Bootstrap registry from live network (auto-classify)")
    reg_sub.add_parser("walkthrough", help="Interactive walkthrough — fill in unknown devices")
    reg_sub.add_parser("list", help="List every device + client annotation")
    show_p = reg_sub.add_parser("show", help="Show one entry by MAC")
    show_p.add_argument("mac", help="MAC address (any common format)")
    reg_p.set_defaults(func=_cmd_registry)

    sec_p = subparsers.add_parser("security", help="Security Agent — emits VLAN/firewall proposals")
    sec_sub = sec_p.add_subparsers(dest="security_cmd", required=True)
    propose_p = sec_sub.add_parser(
        "propose-vlans", help="Generate a complete VLAN architecture proposal",
    )
    propose_p.add_argument("--json", action="store_true", help="Output JSON instead of markdown")
    sec_p.set_defaults(func=_cmd_security)

    ai_p = subparsers.add_parser("ai", help="AI runtime jobs (security analysis, change review)")
    ai_sub = ai_p.add_subparsers(dest="ai_cmd", required=True)
    ai_sub.add_parser("analyze", help="Run AI security posture analysis on the live snapshot")
    review_p = ai_sub.add_parser("review", help="AI review of a proposed config change (JSON)")
    review_p.add_argument(
        "--action", default="",
        help="Action name (escalates to Opus when sensitive)",
    )
    review_p.add_argument("--proposed", required=True, help="Proposed change as JSON")
    review_p.add_argument("--current", default="", help="Current state as JSON (optional)")
    ai_p.set_defaults(func=_cmd_ai)

    optimize_p = subparsers.add_parser("optimize", help="Apply AUTO-tier network optimizations")
    optimize_sub = optimize_p.add_subparsers(dest="optimize_cmd", required=True)
    optimize_sub.add_parser("channel-fix", help="Detect and resolve Wi-Fi channel conflicts")
    rename_p = optimize_sub.add_parser("rename", help="Rename a device")
    rename_p.add_argument("device", help="Current device display name")
    rename_p.add_argument("new_name", help="New display name")
    optimize_p.set_defaults(func=_cmd_optimize)

    serve_p = subparsers.add_parser(
        "serve",
        help="Run the Conductor web UI ([server] extra required)",
    )
    serve_p.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address. Defaults to 127.0.0.1 (localhost-only). The "
             "server has NO authentication — using a non-localhost host "
             "exposes the Conductor to anyone on that network.",
    )
    serve_p.add_argument(
        "--port", type=int, default=8088,
        help="Port to bind on (default: 8088)",
    )
    serve_p.add_argument(
        "--no-browser", action="store_true",
        help="Do not auto-open a browser window",
    )
    serve_p.set_defaults(func=_cmd_serve)

    args = parser.parse_args()
    configure_logging()

    # Bare `nye` (no subcommand) drops into the Conductor REPL.
    if args.command is None:
        return _cmd_chat(args)

    if hasattr(args, "func"):
        return args.func(args)

    print(f"nye {args.command}: not yet implemented (Phase 0 scaffold)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
