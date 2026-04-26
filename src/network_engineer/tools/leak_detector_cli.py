"""Manual CLI for the leak detector — `python -m ...leak_detector_cli`."""
from __future__ import annotations

import sys

from network_engineer.tools.leak_detector import main_cli


if __name__ == "__main__":
    sys.exit(main_cli())
