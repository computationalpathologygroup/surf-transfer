"""
conftest.py — Pytest fixtures for the FileSender downloader's state manifest tests.
"""

import sys
from pathlib import Path

_PIPELINE_DIR = Path(__file__).parent.parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import pytest


@pytest.fixture
def output_dir(tmp_path) -> Path:
    """A fresh directory standing in for --output-dir."""
    d = tmp_path / "output"
    d.mkdir()
    return d


@pytest.fixture
def state_path(tmp_path) -> Path:
    """Path to a not-yet-created state manifest."""
    return tmp_path / ".filesender_state.json"


def write_file(path: Path, content: bytes) -> None:
    """Write bytes to path, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
