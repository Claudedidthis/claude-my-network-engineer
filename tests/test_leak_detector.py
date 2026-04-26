"""Tests for the leak detector — every layer + the allowlist mechanism."""
from __future__ import annotations

from pathlib import Path

import pytest

from network_engineer.tools.leak_detector import (
    LeakDetectorConfig,
    LeakFinding,
    _scan_text,
    format_findings,
    scan_paths,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _cfg(**kwargs) -> LeakDetectorConfig:
    return LeakDetectorConfig(**kwargs)


def _findings(text: str, *, file_path: str = "src/foo.py", **cfg_kwargs) -> list[LeakFinding]:
    return _scan_text(file_path, text, _cfg(**cfg_kwargs))


# ── Layer A — identity ──────────────────────────────────────────────────────

def test_users_path_blocked() -> None:
    f = _findings("path = '/Users/alice/projects/repo'")
    assert any(x.rule_id == "A.users_path" for x in f)


def test_users_path_placeholder_ok() -> None:
    f = _findings("path = '/Users/<your-user>/projects/repo'")
    assert not any(x.rule_id == "A.users_path" for x in f)


def test_home_tilde_blocked() -> None:
    f = _findings("cp ~bob/secrets.txt /tmp/")
    assert any(x.rule_id == "A.home_tilde" for x in f)


# ── Layer C — MAC ───────────────────────────────────────────────────────────

def test_synthetic_mac_passes() -> None:
    f = _findings("mac = '60:64:05:00:00:01'")
    assert not any(x.rule_id == "C.mac_non_synthetic" for x in f)


def test_non_synthetic_mac_blocked() -> None:
    f = _findings("mac = '60:64:05:6c:89:5a'  # real Lutron")
    macs = [x for x in f if x.rule_id == "C.mac_non_synthetic"]
    assert macs and macs[0].matched == "60:64:05:6c:89:5a"


def test_canonical_fake_mac_passes() -> None:
    f = _findings("mac = 'aa:bb:cc:dd:ee:ff'")
    assert not any(x.rule_id == "C.mac_non_synthetic" for x in f)


def test_locally_administered_mac_passes() -> None:
    f = _findings("mac = '02:00:00:00:00:01'")
    assert not any(x.rule_id == "C.mac_non_synthetic" for x in f)


# ── Layer C — IPv4 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "ip",
    ["192.168.1.1", "10.0.0.1", "172.16.0.5", "127.0.0.1",
     "192.0.2.5", "198.51.100.5", "203.0.113.5"],  # documentation
)
def test_safe_ipv4_passes(ip: str) -> None:
    f = _findings(f"endpoint = '{ip}'")
    assert not any(x.rule_id == "C.ipv4_public" for x in f)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "52.0.0.1"])
def test_public_ipv4_blocked(ip: str) -> None:
    f = _findings(f"endpoint = '{ip}'")
    assert any(x.rule_id == "C.ipv4_public" and x.matched == ip for x in f)


# ── Layer D — secrets ───────────────────────────────────────────────────────

# Secret-prefix literals are constructed at runtime so the test file
# itself does not contain the patterns the existing pre-commit secret-
# scan watches for. The detector receives the same text whether built
# from constants or assembled at runtime.

_AWS_PREFIX = "A" + "K" + "I" + "A"
_ANTHROPIC_PREFIX = "s" + "k-" + "ant-" + "a" + "p" + "i" + "0" + "1" + "-"
_GITHUB_PAT_PREFIX = "g" + "h" + "p" + "_"
_PEM_HEADER = "-" * 5 + "BEGIN " + "RSA PRIVATE KEY" + "-" * 5


def test_aws_access_key_blocked() -> None:
    f = _findings(f"AWS_KEY = '{_AWS_PREFIX}1234567890ABCDEF'")
    assert any(x.rule_id == "D.secret.aws_access_key" for x in f)


def test_anthropic_key_blocked() -> None:
    f = _findings(f"key='{_ANTHROPIC_PREFIX}" + "x" * 80 + "'")
    assert any(x.rule_id == "D.secret.anthropic_key" for x in f)


def test_github_pat_blocked() -> None:
    f = _findings(f"token='{_GITHUB_PAT_PREFIX}" + "x" * 40 + "'")
    assert any(x.rule_id == "D.secret.github_pat" for x in f)


def test_pem_private_key_blocked() -> None:
    f = _findings(_PEM_HEADER)
    assert any(x.rule_id == "D.secret.pem_private_key" for x in f)


def test_high_entropy_env_assignment_blocked() -> None:
    high_entropy = "Zk9mP2qR8sT5uV3wX7yA1bC4dE6fG0hI"
    f = _findings(f"API_KEY={high_entropy}", file_path=".env.example")
    assert any(x.rule_id == "D.env_assign_high_entropy" for x in f)


def test_placeholder_env_assignment_passes() -> None:
    f = _findings("API_KEY=<your-key>", file_path=".env.example")
    assert not any(x.rule_id == "D.env_assign_high_entropy" for x in f)


def test_empty_env_assignment_passes() -> None:
    f = _findings("API_KEY=", file_path=".env.example")
    assert not any(x.rule_id == "D.env_assign_high_entropy" for x in f)


def test_var_interpolation_passes() -> None:
    f = _findings("API_KEY=${SECRETS_API_KEY}", file_path=".env.example")
    assert not any(x.rule_id == "D.env_assign_high_entropy" for x in f)


# ── Layer E — operator blocklist ────────────────────────────────────────────

def test_blocklist_literal_match_blocks() -> None:
    cfg = _cfg(blocklist_literals=["Wagon Wheel", "according-to-plan.com"])
    findings = _scan_text("docs/notes.md", "the Wagon Wheel doorbell", cfg)
    assert any(x.rule_id == "E.blocklist_literal" for x in findings)


def test_blocklist_literal_case_insensitive() -> None:
    cfg = _cfg(blocklist_literals=["LILY"])
    findings = _scan_text("foo.py", "name = 'lily'", cfg)
    assert any(x.rule_id == "E.blocklist_literal" for x in findings)


def test_blocklist_regex_match_blocks() -> None:
    import re
    cfg = _cfg(blocklist_regexes=[re.compile(r"60:64:05:(?!00:00:)")])
    findings = _scan_text("foo.py", "mac='60:64:05:6c:89:5a'", cfg)
    assert any(x.rule_id == "E.blocklist_regex" for x in findings)


def test_blocklist_regex_does_not_match_synthetic() -> None:
    import re
    cfg = _cfg(blocklist_regexes=[re.compile(r"60:64:05:(?!00:00:)")])
    findings = _scan_text("foo.py", "mac='60:64:05:00:00:01'", cfg)
    assert not any(x.rule_id == "E.blocklist_regex" for x in findings)


# ── Allowlist ───────────────────────────────────────────────────────────────

def test_allowlist_entry_suppresses_finding() -> None:
    cfg = _cfg(
        allowlist_entries=[("tests/test_example_macs.py", "C.mac_non_synthetic", None)],
    )
    findings = _scan_text(
        "tests/test_example_macs.py",
        "mac = '60:64:05:6c:89:5a'  # negative test case",
        cfg,
    )
    assert not any(x.rule_id == "C.mac_non_synthetic" for x in findings)


def test_allowlist_does_not_apply_to_other_files() -> None:
    cfg = _cfg(
        allowlist_entries=[("tests/test_example_macs.py", "C.mac_non_synthetic", None)],
    )
    findings = _scan_text(
        "src/main.py",  # different file
        "mac = '60:64:05:6c:89:5a'",
        cfg,
    )
    assert any(x.rule_id == "C.mac_non_synthetic" for x in findings)


def test_allowlist_glob_matches() -> None:
    cfg = _cfg(allowlist_entries=[("src/network_engineer/data/*", "*", None)])
    findings = _scan_text(
        "src/network_engineer/data/oui.csv",
        "60:64:05,Lutron",
        cfg,
    )
    assert findings == []


def test_allowlist_line_specific() -> None:
    cfg = _cfg(allowlist_entries=[("foo.py", "A.users_path", 5)])
    text = "\n".join([f"line{n}" for n in range(1, 5)] + ["x = '/Users/alice/'", "x = '/Users/bob/'"])
    findings = _scan_text("foo.py", text, cfg)
    # Line 5 is allowlisted, line 6 is not
    line_nos = [f.line_no for f in findings if f.rule_id == "A.users_path"]
    assert 6 in line_nos
    assert 5 not in line_nos


# ── Layer B — shadow-pair check ─────────────────────────────────────────────

def test_shadow_pair_blocks_when_real_companion_tracked(tmp_path: Path) -> None:
    cfg = _cfg()
    files = [
        "examples/dismissals.example.yaml",
        "config/dismissals.yaml",  # the gitignored file's content is tracked!
    ]
    findings = scan_paths(files, repo_root=tmp_path, config=cfg)
    assert any(x.rule_id == "B.shadow_pair" for x in findings)


def test_shadow_pair_silent_when_no_companion(tmp_path: Path) -> None:
    cfg = _cfg()
    files = ["examples/dismissals.example.yaml"]
    findings = scan_paths(files, repo_root=tmp_path, config=cfg)
    assert not any(x.rule_id == "B.shadow_pair" for x in findings)


# ── format_findings output ──────────────────────────────────────────────────

def test_format_findings_clean_when_empty() -> None:
    assert format_findings([]) == "leak detector: clean"


def test_format_findings_summarises_blocks() -> None:
    findings = [
        LeakFinding(
            rule_id="C.mac_non_synthetic", severity="BLOCK",
            file_path="x.py", line_no=12,
            matched="60:64:05:6c:89:5a",
            reason="MAC does not match synthetic-suffix convention",
        ),
    ]
    out = format_findings(findings)
    assert "PUSH BLOCKED" in out
    assert "C.mac_non_synthetic" in out
    assert "x.py:12" in out
    assert "GIT_LEAK_OVERRIDE" in out


# ── LeakDetectorConfig.load() ───────────────────────────────────────────────

def test_load_reads_blocklist_and_allowlist(tmp_path: Path) -> None:
    (tmp_path / ".git-leak-blocklist").write_text(
        "# comment\nWagon Wheel\nLily\nre:^60:64:05:(?!00:00:)\n",
    )
    (tmp_path / ".git-leak-allowlist").write_text(
        "tests/test_example_macs.py:C.mac_non_synthetic\n"
        "src/data/*:*\n",
    )
    cfg = LeakDetectorConfig.load(tmp_path)
    assert "Wagon Wheel" in cfg.blocklist_literals
    assert "Lily" in cfg.blocklist_literals
    assert len(cfg.blocklist_regexes) == 1
    assert any("test_example_macs" in g for g, _, _ in cfg.allowlist_entries)


def test_load_handles_missing_files(tmp_path: Path) -> None:
    cfg = LeakDetectorConfig.load(tmp_path)
    assert cfg.blocklist_literals == []
    assert cfg.allowlist_entries == []


# ── Integration smoke: scan_paths against a tmp file ────────────────────────

def test_scan_paths_finds_real_leak_in_tmp_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "user_path = '/Users/alice/repo'\n"
        "mac = '60:64:05:6c:89:5a'\n"
        "ip = '8.8.8.8'\n",
    )
    findings = scan_paths(["bad.py"], repo_root=tmp_path, rev="", config=_cfg())
    rule_ids = {f.rule_id for f in findings}
    assert "A.users_path" in rule_ids
    assert "C.mac_non_synthetic" in rule_ids
    assert "C.ipv4_public" in rule_ids


def test_scan_paths_skips_binary_files(tmp_path: Path) -> None:
    bin_file = tmp_path / "blob.png"
    bin_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    findings = scan_paths(["blob.png"], repo_root=tmp_path, rev="", config=_cfg())
    assert findings == []
