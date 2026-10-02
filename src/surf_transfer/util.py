"""Small pure helpers plus sha256_file (the only disk read here)."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

_SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
}

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: str | os.PathLike[str], chunk_size: int = 8 * 1024 * 1024) -> str:
    """Stream a file from disk and return its hex sha256 digest."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_size(text: str | int | float) -> int:
    """Parse a human size like '200G', '1.5T', '500M', or a plain byte count."""
    if isinstance(text, (int, float)):
        return int(text)
    match = _SIZE_RE.match(str(text))
    if not match:
        raise ValueError(f"Invalid size: {text!r}")
    number, suffix = match.group(1), match.group(2).upper()
    if suffix not in _SIZE_UNITS:
        raise ValueError(f"Unknown size suffix {suffix!r} in {text!r}")
    return int(float(number) * _SIZE_UNITS[suffix])


def format_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0:
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} PB"


def sanitize_folder(label: str | None, fallback: str) -> str:
    """Turn a transfer subject / share name into a single safe folder name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (label or "").strip()).strip(". ")
    return name or fallback


def safe_relpath(name: str) -> str:
    """Normalise a remote path to a relative '/'-separated path that cannot
    escape its folder. Raises ValueError for absolute paths or '..' segments."""
    cleaned = name.replace("\\", "/")
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        raise ValueError(f"absolute path not allowed: {name!r}")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"unsafe path: {name!r}")
    return str(PurePosixPath(*parts))


def fits_free_space(free_bytes: int, needed_bytes: int, reserve_bytes: int = 0) -> bool:
    """Whether needed_bytes can be written while leaving reserve_bytes free."""
    return needed_bytes + reserve_bytes <= free_bytes
