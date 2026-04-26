#!/usr/bin/env bash
# install_pre_push_hook.sh — install the leak-detector pre-push gate.
#
# Run once per clone:
#   bash scripts/install_pre_push_hook.sh
#
# After this, every `git push` automatically runs the leak detector on
# the about-to-push commits. A finding blocks the push (non-zero exit).
# Override (logged): GIT_LEAK_OVERRIDE=I_ACCEPT_THE_RISK git push ...
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_PATH="$REPO_ROOT/.git/hooks/pre-push"

if [[ ! -d "$REPO_ROOT/.git/hooks" ]]; then
    echo "error: $REPO_ROOT/.git/hooks not found — is this a git repo?" >&2
    exit 1
fi

if [[ -e "$HOOK_PATH" && ! -L "$HOOK_PATH" ]]; then
    backup="$HOOK_PATH.backup-$(date +%Y%m%d-%H%M%S)"
    echo "Backing up existing pre-push hook to: $backup"
    mv "$HOOK_PATH" "$backup"
fi

cat > "$HOOK_PATH" <<'HOOK'
#!/usr/bin/env bash
# Auto-installed by scripts/install_pre_push_hook.sh.
# Runs the leak detector on every git push. Findings block the push.
#
# To bypass for an emergency push (logged):
#   GIT_LEAK_OVERRIDE=I_ACCEPT_THE_RISK git push ...
set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Prefer the project venv; fall back to system python.
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "pre-push hook: no python interpreter found; refusing push" >&2
    exit 1
fi

exec "$PYTHON" -m network_engineer.tools.leak_detector_hook
HOOK

chmod +x "$HOOK_PATH"
echo "Installed pre-push hook at: $HOOK_PATH"
echo
echo "To verify:  bash $REPO_ROOT/scripts/install_pre_push_hook.sh && \\"
echo "             git push --dry-run origin main"
echo
echo "To bypass for an emergency push (logged):"
echo "  GIT_LEAK_OVERRIDE=I_ACCEPT_THE_RISK git push ..."
