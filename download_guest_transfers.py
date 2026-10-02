#!/usr/bin/env python3
"""Compatibility entry point: the downloader now lives in src/surf_transfer.

All the previous flags still work; see `python3 download_guest_transfers.py --help`
(or `surf-transfer --help` once installed with `pip install -e .`).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from surf_transfer.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
