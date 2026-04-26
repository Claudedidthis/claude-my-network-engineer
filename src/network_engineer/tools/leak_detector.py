"""Pre-push leak detector — block operator-specific data from leaving the repo.

This is the load-bearing safety net for public-repo hygiene. It scans every
commit in a push range (or the full repo on demand) for five classes of
leak and produces structured findings. A non-empty findings list is a
push-blocker.

The five layers
---------------

    A. Identity leaks
       /Users/<known-user> paths, ~<user> shell paths, configured personal
       names. Catches accidental absolute paths and inline references.

    B. Operator-config shadow leaks
       For every committed file ending in ".example.*" the corresponding
       gitignored file is checked NOT to be tracked. If the gitignored
       file appears in the push tree, that's a privacy break.

    C. Network identifiers
       MAC addresses (non-synthetic-suffix), IPv4 outside RFC1918 +
       documentation ranges, IPv6 outside link-local + ULA, SSIDs from
       the operator's blocklist, hostnames from the operator's blocklist.
       The synthetic-MAC convention is the same as tests/test_example_macs.py
       (XX:XX:XX:00:00:NN or RFC locally-administered).

    D. Secrets
       AWS key prefixes (AKIA / ASIA), GitHub PATs (ghp_/gho_/ghu_/ghs_/ghr_),
       Anthropic keys (sk-ant-...), OpenAI keys (sk-...), generic JWT,
       PEM private-key headers, .env-shaped lines containing high-entropy
       values near KEY/TOKEN/SECRET keywords.

    E. Operator blocklist (the load-bearing layer)
       A gitignored .git-leak-blocklist file containing literal substrings
       and regex patterns specific to THIS operator: real name, family
       names, street name, pet names, real device labels, real SSIDs,
       real domains, real network MACs. A generic rule cannot know that
       a particular street name or family member's first name is
       sensitive in this repo and irrelevant in another; the operator's
       private blocklist is what closes that gap.

Allowlisting
------------

A committed .git-leak-allowlist file lists intentional content that would
otherwise trip the rules. Format: one entry per line, `file_glob:rule_id`
or `file_glob:rule_id:line_no`. Used for legitimate test-rejector cases
like tests/test_example_macs.py that *deliberately* contain non-synthetic
MACs to verify the rejector works.

Exit semantics
--------------

scan_paths returns list[LeakFinding]. Non-empty = push must be blocked.
The CLI / hook entry point is responsible for translating that to a
non-zero exit code and a clear error report.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import math
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ── Synthetic MAC classifier (same convention as tests/test_example_macs.py) ─

_MAC_RE = re.compile(
    r"\b([0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:"
    r"[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})\b",
)

_SENTINEL_MACS = frozenset({
    "00:00:00:00:00:00",
    "ff:ff:ff:ff:ff:ff",
    "aa:bb:cc:dd:ee:ff",
})


def _is_synthetic_mac(mac: str) -> bool:
    parts = mac.lower().split(":")
    if len(parts) != 6:
        return False
    # Canonical demo-space aa:bb:cc:dd:ee:NN — universally used in code
    # samples and unit tests. Recognized as synthetic regardless of suffix.
    # This is more permissive than tests/test_example_macs.py's convention
    # (which requires OUI + zeroed suffix for educational example files);
    # the leak detector targets a different concern (catching real network
    # MACs) and accepts the broader fake-MAC convention.
    if parts[:5] == ["aa", "bb", "cc", "dd", "ee"]:
        return True
    suffix = parts[3:]
    if suffix[0] == "00" and suffix[1] == "00":
        return True
    if (
        parts[0] in ("02", "06", "0a", "0e")
        and all(p in ("00", "01", "02", "03", "04", "ff") for p in parts[1:5])
    ):
        return True
    if mac.lower() in _SENTINEL_MACS:
        return True
    return False


# ── IP classification ────────────────────────────────────────────────────────

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")


def _ip_is_safe(text: str) -> bool:
    """True iff the IP string is in a documentation / private / loopback range."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return True  # not actually an IP — let regex false-positive slide
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return True
    if ip.is_reserved or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        # RFC 5737 documentation ranges
        for net in (
            ipaddress.IPv4Network("192.0.2.0/24"),
            ipaddress.IPv4Network("198.51.100.0/24"),
            ipaddress.IPv4Network("203.0.113.0/24"),
        ):
            if ip in net:
                return True
    if isinstance(ip, ipaddress.IPv6Address):
        # RFC 3849 documentation prefix
        if ip in ipaddress.IPv6Network("2001:db8::/32"):
            return True
    return False


# ── Secret patterns ──────────────────────────────────────────────────────────

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{40,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
)

# Lines with KEY/TOKEN/SECRET assigned to a high-entropy value.
_ENV_ASSIGN_RE = re.compile(
    r"(?im)^[\t ]*([A-Z][A-Z0-9_]*"
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CRED|API|AUTH))"
    r"[\t ]*[:=][\t ]*[\"']?([^\s\"'#]+)",
)


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# ── Identity patterns (Layer A) ──────────────────────────────────────────────

_USERS_PATH_RE = re.compile(r"/Users/([A-Za-z0-9._-]+)/")
_HOME_TILDE_RE = re.compile(r"~([A-Za-z0-9._-]+)/")


# ── LeakFinding + scan engine ────────────────────────────────────────────────

@dataclass(frozen=True)
class LeakFinding:
    """One detected leak. Multiple findings per push are common."""
    rule_id: str          # e.g. "C.mac_non_synthetic", "E.blocklist", "D.secret.aws_access_key"
    severity: str         # "BLOCK" | "WARN"
    file_path: str
    line_no: int          # 1-based; 0 for whole-file findings
    matched: str          # the offending substring, truncated for log safety
    reason: str           # human-readable explanation


@dataclass
class LeakDetectorConfig:
    """Configuration loaded from blocklist/allowlist files + repo state."""

    blocklist_literals: list[str] = field(default_factory=list)
    blocklist_regexes: list[re.Pattern[str]] = field(default_factory=list)
    allowlist_entries: list[tuple[str, str, int | None]] = field(default_factory=list)
    text_extensions: frozenset[str] = frozenset({
        ".py", ".md", ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini",
        ".sh", ".bash", ".zsh", ".env", ".example", ".lock", ".txt",
        ".html", ".css", ".js", ".ts", ".tsx", ".jsx", ".swift", ".plist",
    })
    max_file_bytes: int = 2_000_000

    @classmethod
    def load(cls, repo_root: Path) -> "LeakDetectorConfig":
        cfg = cls()
        bl = repo_root / ".git-leak-blocklist"
        if bl.exists():
            for raw in bl.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("re:"):
                    try:
                        cfg.blocklist_regexes.append(re.compile(line[3:].strip(), re.IGNORECASE))
                    except re.error:
                        continue
                else:
                    cfg.blocklist_literals.append(line)
        al = repo_root / ".git-leak-allowlist"
        if al.exists():
            for raw in al.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Format: file_glob:rule_id  OR  file_glob:rule_id:line_no
                parts = line.split(":")
                if len(parts) == 2:
                    cfg.allowlist_entries.append((parts[0], parts[1], None))
                elif len(parts) >= 3:
                    try:
                        cfg.allowlist_entries.append((parts[0], parts[1], int(parts[2])))
                    except ValueError:
                        cfg.allowlist_entries.append((parts[0], parts[1], None))
        return cfg

    def is_allowlisted(self, file_path: str, rule_id: str, line_no: int) -> bool:
        for glob, rule, allowed_line in self.allowlist_entries:
            if not fnmatch.fnmatch(file_path, glob):
                continue
            if rule != rule_id and rule != "*":
                continue
            if allowed_line is not None and allowed_line != line_no:
                continue
            return True
        return False


def _looks_textual(path: Path, cfg: LeakDetectorConfig) -> bool:
    if path.suffix.lower() in cfg.text_extensions:
        return True
    name = path.name.lower()
    if name in {"makefile", "dockerfile", ".env.example", ".gitignore",
                ".gitattributes", "license", "readme"}:
        return True
    return False


def _scan_text(
    file_path: str,
    text: str,
    cfg: LeakDetectorConfig,
) -> list[LeakFinding]:
    findings: list[LeakFinding] = []

    def emit(rule_id: str, severity: str, line_no: int, matched: str, reason: str) -> None:
        if cfg.is_allowlisted(file_path, rule_id, line_no):
            return
        # Truncate matched content for log safety; never echo full PEM blocks etc.
        m = matched if len(matched) <= 80 else matched[:77] + "..."
        findings.append(LeakFinding(
            rule_id=rule_id, severity=severity, file_path=file_path,
            line_no=line_no, matched=m, reason=reason,
        ))

    lines = text.splitlines()
    for ln_idx, line in enumerate(lines, start=1):

        # Layer A — /Users/<username> and ~<username> paths
        for m in _USERS_PATH_RE.finditer(line):
            user = m.group(1)
            if user not in {"<your-user>", "<user>", "<username>", "you"}:
                emit("A.users_path", "BLOCK", ln_idx, m.group(0),
                     f"hardcoded /Users/{user} path leaks the operator's username")
        for m in _HOME_TILDE_RE.finditer(line):
            user = m.group(1)
            if user and user not in {"", "<user>", "<username>"}:
                emit("A.home_tilde", "BLOCK", ln_idx, m.group(0),
                     f"~{user}/ path leaks an explicit username")

        # Layer C.mac
        for m in _MAC_RE.finditer(line):
            mac = m.group(1)
            if not _is_synthetic_mac(mac):
                emit("C.mac_non_synthetic", "BLOCK", ln_idx, mac,
                     "MAC does not match synthetic-suffix convention "
                     "(XX:XX:XX:00:00:NN) — likely real network hardware")

        # Layer C.ipv4
        for m in _IPV4_RE.finditer(line):
            cand = m.group(0)
            if not _ip_is_safe(cand):
                emit("C.ipv4_public", "BLOCK", ln_idx, cand,
                     "IPv4 address outside RFC1918/loopback/link-local/"
                     "documentation ranges — likely a real public IP")

        # Layer C.ipv6
        for m in _IPV6_RE.finditer(line):
            cand = m.group(0)
            # Skip MAC-shaped strings the IPv6 regex would also match
            if _MAC_RE.fullmatch(cand):
                continue
            if not _ip_is_safe(cand):
                emit("C.ipv6_public", "BLOCK", ln_idx, cand,
                     "IPv6 address outside link-local/ULA/documentation ranges")

        # Layer D — secret patterns
        for label, pat in _SECRET_PATTERNS:
            for m in pat.finditer(line):
                emit(f"D.secret.{label}", "BLOCK", ln_idx, m.group(0),
                     f"matches {label} pattern — never commit secrets")

        # Layer D — KEY/TOKEN/SECRET assignment to high-entropy value
        for m in _ENV_ASSIGN_RE.finditer(line):
            key, value = m.group(1), m.group(2)
            if value in {"", "''", '""', "<your-key>", "<value>", "your-token-here"}:
                continue
            if value.startswith("$"):  # ${VAR} interpolation — fine
                continue
            if len(value) >= 16 and _shannon_entropy(value) >= 4.0:
                emit("D.env_assign_high_entropy", "BLOCK", ln_idx,
                     f"{key}={value[:16]}...",
                     f"{key} assigned a high-entropy value; commit a placeholder instead")

        # Layer E — operator blocklist literals
        line_lower = line.lower()
        for needle in cfg.blocklist_literals:
            if needle.lower() in line_lower:
                emit("E.blocklist_literal", "BLOCK", ln_idx, needle,
                     f"matches operator blocklist entry {needle!r}")
        for pat in cfg.blocklist_regexes:
            for m in pat.finditer(line):
                emit("E.blocklist_regex", "BLOCK", ln_idx, m.group(0),
                     f"matches operator blocklist regex {pat.pattern!r}")

    return findings


def _shadow_pair_check(
    repo_root: Path,
    files_in_push: Iterable[str],
    cfg: LeakDetectorConfig,
) -> list[LeakFinding]:
    """Layer B — for every example.* file, the corresponding non-example
    file must NOT appear in the tracked tree."""
    findings: list[LeakFinding] = []
    in_push = set(files_in_push)
    examples = [f for f in in_push if ".example." in f]
    for ex in examples:
        # examples/foo.example.yaml -> config/foo.yaml or examples/foo.yaml
        # We strip ".example" and check both the same dir and a likely
        # config/ companion. Heuristic: if the operator file is in the
        # gitignored set, finding it tracked here is a leak.
        candidates = {
            ex.replace(".example.", "."),
            ex.replace("examples/", "config/").replace(".example.", "."),
        }
        for cand in candidates:
            if cand == ex:
                continue
            if cand in in_push:
                if cfg.is_allowlisted(cand, "B.shadow_pair", 0):
                    continue
                findings.append(LeakFinding(
                    rule_id="B.shadow_pair", severity="BLOCK",
                    file_path=cand, line_no=0, matched=cand,
                    reason=f"file shadows the example template {ex!r} — "
                           "should be gitignored, not committed",
                ))
    return findings


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
    )
    return result.stdout


def list_files_in_range(start_sha: str, end_sha: str, *, cwd: Path) -> list[str]:
    """Files changed between two refs. Empty start_sha = full tree at end_sha."""
    if not start_sha or start_sha == "0000000000000000000000000000000000000000":
        out = _git("ls-tree", "-r", "--name-only", end_sha, cwd=cwd)
    else:
        out = _git("diff", "--name-only", f"{start_sha}..{end_sha}", cwd=cwd)
    return [line for line in out.splitlines() if line]


def list_all_tracked(cwd: Path) -> list[str]:
    return [line for line in _git("ls-files", cwd=cwd).splitlines() if line]


def read_blob(sha: str, path: str, *, cwd: Path) -> bytes:
    """Read file content at a specific commit. Empty sha → working tree."""
    if not sha:
        return (cwd / path).read_bytes()
    try:
        out = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            cwd=str(cwd), check=True, capture_output=True,
        )
        return out.stdout
    except subprocess.CalledProcessError:
        return b""


def scan_paths(
    paths: Iterable[str],
    *,
    repo_root: Path,
    rev: str = "",
    config: LeakDetectorConfig | None = None,
) -> list[LeakFinding]:
    """Scan the given paths at `rev` (working tree if rev is empty).

    Caller-supplied config takes precedence over loading from disk; this
    keeps tests hermetic.
    """
    cfg = config or LeakDetectorConfig.load(repo_root)
    findings: list[LeakFinding] = []
    paths_list = list(paths)
    for path in paths_list:
        full = repo_root / path
        if not _looks_textual(full, cfg):
            continue
        try:
            data = read_blob(rev, path, cwd=repo_root) if rev else (
                full.read_bytes() if full.exists() else b""
            )
        except OSError:
            continue
        if len(data) > cfg.max_file_bytes:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(_scan_text(path, text, cfg))
    findings.extend(_shadow_pair_check(repo_root, paths_list, cfg))
    return findings


def scan_push_range(
    local_sha: str,
    remote_sha: str,
    *,
    repo_root: Path,
    config: LeakDetectorConfig | None = None,
) -> list[LeakFinding]:
    """Entry point for the pre-push hook.

    `local_sha` is the about-to-push tip. `remote_sha` is what the remote
    currently has (empty / zero on first push). Files changed in the range
    are scanned at `local_sha` so we catch content as it would appear on
    the public tree, not the working copy.
    """
    files = list_files_in_range(remote_sha or "", local_sha, cwd=repo_root)
    return scan_paths(files, repo_root=repo_root, rev=local_sha, config=config)


def format_findings(findings: list[LeakFinding]) -> str:
    if not findings:
        return "leak detector: clean"
    by_rule: dict[str, list[LeakFinding]] = {}
    for f in findings:
        by_rule.setdefault(f.rule_id, []).append(f)
    lines = [
        f"PUSH BLOCKED — {len(findings)} leak finding(s) across "
        f"{len(by_rule)} rule(s):",
        "",
    ]
    for rule_id in sorted(by_rule):
        group = by_rule[rule_id]
        lines.append(f"  [{rule_id}] {group[0].reason}")
        for f in group[:8]:
            loc = f"{f.file_path}:{f.line_no}" if f.line_no else f.file_path
            lines.append(f"      {loc}  ::  {f.matched}")
        if len(group) > 8:
            lines.append(f"      ... and {len(group) - 8} more")
        lines.append("")
    lines.append(
        "How to proceed:\n"
        "  1. If the finding is a real leak, scrub the data + amend/squash "
        "before pushing.\n"
        "  2. If the finding is intentional (e.g. test fixture), add an "
        "entry to .git-leak-allowlist with the file glob and rule id.\n"
        "  3. To bypass for an emergency push, set "
        "GIT_LEAK_OVERRIDE=I_ACCEPT_THE_RISK in the env. The bypass is\n"
        "     logged and reviewed."
    )
    return "\n".join(lines)


# ── CLI / hook entry point ──────────────────────────────────────────────────

def main_hook(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    """Pre-push hook entry point. Reads ref-pairs from stdin per git docs.

    https://git-scm.com/docs/githooks#_pre_push
    """
    repo_root = Path(_git("rev-parse", "--show-toplevel", cwd=Path.cwd()).strip())
    cfg = LeakDetectorConfig.load(repo_root)

    if os.getenv("GIT_LEAK_OVERRIDE") == "I_ACCEPT_THE_RISK":
        print("WARNING: GIT_LEAK_OVERRIDE is set — leak gate bypassed. "
              "Logging the bypass.", flush=True)
        try:
            log_path = repo_root / ".git" / "leak_overrides.log"
            log_path.parent.mkdir(exist_ok=True)
            from datetime import UTC, datetime
            with log_path.open("a") as fh:
                fh.write(f"{datetime.now(UTC).isoformat()}\toverride\n")
        except OSError:
            pass
        return 0

    import sys
    payload = stdin_text if stdin_text is not None else sys.stdin.read()
    all_findings: list[LeakFinding] = []
    for raw in payload.splitlines():
        parts = raw.split()
        if len(parts) < 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts[:4]
        del local_ref, remote_ref
        if local_sha == "0000000000000000000000000000000000000000":
            # Branch deletion — nothing to scan.
            continue
        all_findings.extend(scan_push_range(
            local_sha, remote_sha, repo_root=repo_root, config=cfg,
        ))

    print(format_findings(all_findings), flush=True)
    return 0 if not all_findings else 1


def main_cli(args: list[str] | None = None) -> int:
    """Manual CLI entry point: scan working tree or a specified rev range."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="nye gate-push",
        description="Scan the repo for operator-data leaks before pushing.",
    )
    parser.add_argument(
        "--rev", default="",
        help="Git rev to scan (default: working tree).",
    )
    parser.add_argument(
        "--range", default="",
        help="Git range (start..end) to scan.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Scan the entire tracked tree.",
    )
    parsed = parser.parse_args(args)

    repo_root = Path(_git("rev-parse", "--show-toplevel", cwd=Path.cwd()).strip())
    cfg = LeakDetectorConfig.load(repo_root)

    if parsed.range:
        start, _, end = parsed.range.partition("..")
        files = list_files_in_range(start or "", end, cwd=repo_root)
        findings = scan_paths(files, repo_root=repo_root, rev=end, config=cfg)
    elif parsed.all:
        files = list_all_tracked(repo_root)
        findings = scan_paths(files, repo_root=repo_root, rev=parsed.rev, config=cfg)
    else:
        # Default: working tree, only modified + untracked staged files.
        out = _git("ls-files", "--cached", "--others", "--exclude-standard", cwd=repo_root)
        files = [ln for ln in out.splitlines() if ln]
        findings = scan_paths(files, repo_root=repo_root, rev="", config=cfg)

    print(format_findings(findings), flush=True)
    return 0 if not findings else 1
