"""Pre-push hook entry point — see scripts/install_pre_push_hook.sh.

Thin wrapper around tools.leak_detector.main_hook so the installed git
hook can call `python -m network_engineer.tools.leak_detector_hook`
without depending on any specific CLI command being on $PATH.
"""
from __future__ import annotations

import sys

from network_engineer.tools.leak_detector import main_hook


if __name__ == "__main__":
    sys.exit(main_hook())
