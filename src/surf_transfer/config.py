"""Frozen configuration, loaded once at the edge and passed down explicitly."""

from __future__ import annotations

import configparser
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

DEFAULT_FILESENDER_URL = "https://filesender.surf.nl/rest.php"
DEFAULT_SURFDRIVE_URL = "https://surfdrive.surf.nl"


@dataclass(frozen=True)
class FileSenderConfig:
    base_url: str
    username: str
    apikey: str = ""
    insecure: bool = False
    centre: str | None = None


@dataclass(frozen=True)
class SurfDriveConfig:
    mode: str  # "public" | "user"
    base_url: str
    name: str  # section label, used for the output folder and --source selection
    username: str  # public: the share token; user: the account name
    password: str = ""  # public: optional share password; user: app password
    remote_folder: str = ""
    centre: str | None = None
    insecure: bool = False


@dataclass(frozen=True)
class RunOptions:
    output_dir: Path
    state_file: Path
    verbose: bool = False
    force: bool = False
    dry_run: bool = False
    recheck_hashes: bool = False
    max_files: int | None = None
    max_bytes: int | None = None
    archive_dir: Path | None = None
    validate_workers: int = 2
    sample_tiles: int = 64
    deep_check: bool = False
    validate_timeout: float = 900.0
    seed: int | None = None
    min_free_bytes: int = 1024**3
    accept_flagged: bool = False
    keep_zips: bool = False
    allow_source_change: bool = False
    slide_filter: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    run: RunOptions
    filesender: FileSenderConfig | None = None
    surfdrive: tuple[SurfDriveConfig, ...] = ()


def _candidate_paths(filename: str, dirname: str, home: Path) -> list[Path]:
    return [home / dirname / filename, Path(dirname) / filename, Path(filename)]


def parse_dotenv(text: str) -> dict[str, str]:
    """Minimal KEY=VALUE parser for a .env file (quotes stripped, # comments)."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        values[key] = value.strip().strip("'\"")
    return values


def load_env(environ: Mapping[str, str], dotenv_path: Path | None = None) -> dict[str, str]:
    """Process environment over a .env file (the environment wins)."""
    values: dict[str, str] = {}
    path = dotenv_path if dotenv_path is not None else Path(".env")
    if path.is_file():
        values.update(parse_dotenv(path.read_text(encoding="utf-8")))
    values.update(
        {
            k: v
            for k, v in environ.items()
            if k.startswith(("FILESENDER_", "SURFDRIVE_")) or k == "API_SECRET"
        }
    )
    return values


def load_filesender_config(
    home: Path,
    env: Mapping[str, str],
    overrides: Mapping[str, str | None] | None = None,
) -> FileSenderConfig | None:
    """filesender.py.ini (same discovery as filesender.py), then env/.env
    (FILESENDER_USERNAME, API_SECRET, FILESENDER_BASE_URL), then CLI overrides."""
    overrides = overrides or {}
    ini = configparser.ConfigParser()
    for path in _candidate_paths("filesender.py.ini", ".filesender", home):
        if path.exists():
            ini.read(path)
            break
    base_url = (
        overrides.get("base_url")
        or env.get("FILESENDER_BASE_URL")
        or (ini["system"].get("base_url") if "system" in ini else None)
        or DEFAULT_FILESENDER_URL
    )
    username = (
        overrides.get("username")
        or env.get("FILESENDER_USERNAME")
        or (ini["user"].get("username") if "user" in ini else None)
    )
    apikey = (
        overrides.get("apikey")
        or env.get("API_SECRET")
        or env.get("FILESENDER_APIKEY")
        or (ini["user"].get("apikey") if "user" in ini else None)
    )
    if not (username and apikey):
        return None
    centre = ini["filesender"].get("centre") if "filesender" in ini else None
    return FileSenderConfig(
        base_url=base_url.rstrip("/"),
        username=username,
        apikey=apikey,
        centre=overrides.get("centre") or centre,
    )


def load_surfdrive_configs(home: Path, insecure: bool = False) -> tuple[SurfDriveConfig, ...]:
    """surfdrive.ini, discovered like filesender.py.ini. Sections:

        [surfdrive-public]            token = <share token>   password = <optional>
        [surfdrive-user]              username = ...  app_password = ...  folder = ...
        [surfdrive-public:<name>]     further named shares (same for -user:<name>)

    Each section may set `centre` and `base_url`."""
    ini = configparser.ConfigParser()
    for path in _candidate_paths("surfdrive.ini", ".surfdrive", home):
        if path.exists():
            ini.read(path)
            break
    configs: list[SurfDriveConfig] = []
    for section in ini.sections():
        kind, _, label = section.partition(":")
        if kind not in ("surfdrive-public", "surfdrive-user"):
            continue
        s = ini[section]
        base_url = s.get("base_url", DEFAULT_SURFDRIVE_URL).rstrip("/")
        if kind == "surfdrive-public":
            token = s.get("token", "").strip()
            if not token:
                raise ValueError(f"[{section}] needs a share token (token = ...)")
            configs.append(
                SurfDriveConfig(
                    mode="public",
                    base_url=base_url,
                    name=label or "public",
                    username=token,
                    password=s.get("password", ""),
                    remote_folder=s.get("folder", "").strip("/"),
                    centre=s.get("centre"),
                    insecure=insecure,
                )
            )
        else:
            user, password = s.get("username", ""), s.get("app_password", "")
            if not (user and password):
                raise ValueError(f"[{section}] needs username and app_password")
            configs.append(
                SurfDriveConfig(
                    mode="user",
                    base_url=base_url,
                    name=label or "user",
                    username=user,
                    password=password,
                    remote_folder=s.get("folder", "").strip("/"),
                    centre=s.get("centre"),
                    insecure=insecure,
                )
            )
    return tuple(configs)


_SURFDRIVE_LINK = re.compile(r"^(https://[^/]+)(?:/index\.php)?/s/([A-Za-z0-9]+)/?(?:[?#].*)?$")


def parse_surfdrive_link(url: str) -> tuple[str, str]:
    """('https://surfdrive.surf.nl', '<token>') from a public share link."""
    match = _SURFDRIVE_LINK.match(url.strip())
    if not match:
        raise ValueError(f"not a SurfDrive/ownCloud public share link: {url!r}")
    return match.group(1), match.group(2)


def parse_surfdrive_folder(url: str) -> str:
    """The subfolder a share link points into (`?dir=/a/b`), '' for the share root."""
    values = parse_qs(urlsplit(url.strip()).query).get("dir", [])
    return unquote(values[0]).strip("/") if values else ""


_FILESENDER_TOKEN = re.compile(r"token=([0-9a-fA-F-]{8,})")


def parse_filesender_token(value: str) -> str:
    """A bare download token, or the token out of a FileSender download link."""
    match = _FILESENDER_TOKEN.search(value)
    return match.group(1) if match else value.strip()


def default_home() -> Path:
    return Path(os.path.expanduser("~"))
