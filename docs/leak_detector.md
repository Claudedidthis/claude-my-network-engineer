# Pre-push leak detector

**Why this exists:** the open-source repo lives next to a populated operator config tree (gitignored). Twice during early development, operator-specific data leaked into committed files — a hardcoded `/Users/<name>` path, real device labels from a UniFi controller, real network MAC addresses used as "rejected" test fixtures. Each leak passed manual review and the project's `.gitignore`. The leak detector is what catches the next one *before* it reaches the public remote.

## Five detection layers

| Layer | Catches |
|---|---|
| **A. Identity** | `/Users/<username>/...` and `~user/...` paths in tracked files |
| **B. Shadow pair** | A `*.example.*` file's gitignored companion (e.g. `config/foo.yaml` next to `examples/foo.example.yaml`) appearing in the tracked tree |
| **C. Network identifiers** | Non-synthetic MAC addresses, public IPv4/IPv6 outside RFC1918/loopback/link-local/documentation ranges |
| **D. Secrets** | AWS / GitHub / Anthropic / OpenAI / Slack key prefixes, JWTs, PEM private-key headers, high-entropy values assigned to KEY/TOKEN/SECRET-named env vars |
| **E. Operator blocklist** | Substring + regex matches against `.git-leak-blocklist` (gitignored, per-fork) — the load-bearing layer for operator-specific identifiers no generic rule can know about |

Layer E is the most important one. A generic rule cannot know that "Wagon Wheel" is sensitive in this repo and innocuous in another. The operator's private blocklist closes that gap.

## Installation (every clone, every operator)

```bash
bash scripts/install_pre_push_hook.sh
```

This installs `.git/hooks/pre-push`. Every subsequent `git push` runs the detector against the about-to-push commit range. Findings block the push with a structured error report.

Then create your private blocklist:

```bash
cp .git-leak-blocklist.example .git-leak-blocklist
# Edit with your real names, street, domains, device labels, real MACs
```

The example file is committed; the real one is gitignored.

## CI gate

`.github/workflows/leak-gate.yml` runs the detector against the full tree on every push and PR. CI cannot accept the `GIT_LEAK_OVERRIDE` bypass — it's the belt to the pre-push hook's suspenders.

## Manual scan

```bash
# Scan the working tree (modified + staged files)
python -m network_engineer.tools.leak_detector_cli

# Scan the entire tracked tree
python -m network_engineer.tools.leak_detector_cli --all

# Scan a specific commit range
python -m network_engineer.tools.leak_detector_cli --range origin/main..HEAD
```

Useful before a manual `git push` if you want to verify before the hook runs.

## Allowlist

Some test files deliberately contain content the detector would otherwise flag — for example, `tests/test_example_macs.py` lists fabricated non-synthetic MACs as the *negative* test cases for its rejector. The committed `.git-leak-allowlist` file documents these intentional exceptions:

```
# Format: file_glob:rule_id        OR
#         file_glob:rule_id:line_no  OR
#         file_glob:*               (allow all rules for matching files)

tests/test_example_macs.py:C.mac_non_synthetic
tests/test_ssl_policy.py:C.ipv4_public
tests/test_leak_detector.py:*
```

Adding allowlist entries requires a code-review-visible diff — silent suppression is not possible.

## Bypass (rare, logged)

For an emergency push when the gate is wrong AND there is no time to fix it:

```bash
GIT_LEAK_OVERRIDE=I_ACCEPT_THE_RISK git push origin main
```

The bypass is logged to `.git/leak_overrides.log` (gitignored). A bypass that turns out to have been needed for a real reason should be followed by an allowlist entry; a bypass that turns out to have shipped a real leak should be followed by a force-push history rewrite.

## Detection-layer detail

### A. Identity

- `/Users/<u>/` where `<u>` is not in `{"<your-user>", "<user>", "<username>", "you"}`
- `~<u>/` where `<u>` is non-empty and not a placeholder

### B. Shadow pair

For every tracked file matching `*.example.*`, the detector checks whether the corresponding non-example file is also tracked. If so:
- `examples/foo.example.yaml` + `examples/foo.yaml` → flag the latter
- `examples/foo.example.yaml` + `config/foo.yaml` → flag the latter

These are the two common patterns for example→real shadow.

### C. Network identifiers

**MAC addresses** are accepted as synthetic when:
- Suffix bytes are `00:00:NN` (the educational example-files convention)
- Whole MAC is in the RFC locally-administered range (`02|06|0a|0e:00:00:00:00:NN`)
- Whole MAC is in the canonical demo-space `aa:bb:cc:dd:ee:NN`
- Whole MAC is one of `00:00:00:00:00:00`, `ff:ff:ff:ff:ff:ff`, `aa:bb:cc:dd:ee:ff`

Anything else is flagged as `C.mac_non_synthetic`.

**IPv4** is accepted as safe when in:
- Private (RFC1918): 10/8, 172.16/12, 192.168/16
- Loopback: 127/8
- Link-local: 169.254/16
- Multicast/reserved/unspecified
- Documentation: 192.0.2/24, 198.51.100/24, 203.0.113/24

Anything else is flagged as `C.ipv4_public`.

**IPv6** is accepted in link-local (fe80::/10), ULA (fc00::/7), loopback (::1), and the documentation prefix 2001:db8::/32.

### D. Secrets

| Rule | Pattern |
|---|---|
| `D.secret.aws_access_key` | `AKIA...` / `ASIA...` (20 chars total) |
| `D.secret.github_pat` | `ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_` + ≥36 chars |
| `D.secret.anthropic_key` | `sk-ant-` + ≥40 chars |
| `D.secret.openai_key` | `sk-` (optionally `sk-proj-`) + ≥32 chars |
| `D.secret.slack_token` | `xoxb-` / `xoxp-` / `xoxa-` / `xoxr-` / `xoxs-` |
| `D.secret.jwt` | `eyJ...` (three base64 segments, dot-separated) |
| `D.secret.pem_private_key` | `-----BEGIN ... PRIVATE KEY-----` |
| `D.env_assign_high_entropy` | `*KEY=...`, `*TOKEN=...`, `*SECRET=...` etc. assigned a value with Shannon entropy ≥ 4.0 and length ≥ 16 |

The high-entropy heuristic is the catch-all for keys that don't match a vendor-specific prefix. Placeholders (`<your-key>`, `${VAR}`, empty values) are exempt.

### E. Operator blocklist

`.git-leak-blocklist` is per-fork, gitignored. One entry per line:

- A literal substring match (case-insensitive)
- `re:<regex>` for regex matches (case-insensitive)

The regex form is useful for OUI + non-zeroed-suffix patterns like `re:^60:64:05:(?!00:00:)` — matches a Lutron OUI that *isn't* followed by the zeroed-suffix synthetic convention.

## What the detector does NOT catch

- **Semantic** leaks. A device named "Pet's water bowl" doesn't match any pattern. Add it to the operator blocklist if it's identifying.
- **Image/binary** content. Files outside the configured text-extension list are skipped.
- **Encoded** secrets. Base64-wrapped JWTs are caught; double-encoded or split-across-lines secrets are not.
- **Branch protection**. The detector blocks pushes; it does not configure GitHub branch protection rules.

For each of these, the operator's blocklist + manual review are the remaining defenses.
