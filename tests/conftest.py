"""pytest configuration shared across all tests."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

# Ensure src/ is importable even when not pip-installed.
# pyproject.toml also sets pythonpath = ["src"]; this is belt-and-suspenders.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Load .env BEFORE pytest collects tests, so pytest.mark.skipif decorators
# guarded on env vars (UNIFI_HOST, ANTHROPIC_API_KEY) see the right values
# at collection time. Without this, live integration tests skip even when
# UNIFI_HOST is set in .env, because load_dotenv() in tools/unifi_client.py
# runs only when that module is first imported — too late for collection-time
# decorator evaluation.
load_dotenv(PROJECT_ROOT / ".env")
