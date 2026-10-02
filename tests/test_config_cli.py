import csv
import json

import pytest

from fakes import FakeRunner, FakeSource
from surf_transfer import cli, report
from surf_transfer.config import (
    load_env,
    load_filesender_config,
    load_surfdrive_configs,
    parse_dotenv,
    parse_filesender_token,
    parse_surfdrive_folder,
    parse_surfdrive_link,
)
from surf_transfer.manifest import Manifest

# --- config ---------------------------------------------------------------


def test_parse_surfdrive_public_link():
    assert parse_surfdrive_link("https://surfdrive.surf.nl/s/JgJpQDsQabtspZE") == (
        "https://surfdrive.surf.nl",
        "JgJpQDsQabtspZE",
    )
    assert parse_surfdrive_link("https://surfdrive.surf.nl/index.php/s/abc123/")[1] == "abc123"


def test_parse_surfdrive_folder_from_link_dir_parameter():
    base = "https://surfdrive.surf.nl/s/abc123"
    assert parse_surfdrive_folder(base) == ""
    assert parse_surfdrive_folder(f"{base}?dir=/40x-scans/Symbiant_218_compleet") == (
        "40x-scans/Symbiant_218_compleet"
    )
    assert parse_surfdrive_folder(f"{base}?dir=%2FA%20B%2Fc&path=/x") == "A B/c"


@pytest.mark.parametrize("bad", ["http://surfdrive.surf.nl/s/abc", "https://x/y", "abc"])
def test_parse_surfdrive_link_rejects_non_share_or_non_https(bad):
    with pytest.raises(ValueError):
        parse_surfdrive_link(bad)


def test_parse_filesender_token_from_link_or_bare():
    token = "8bb6d51a-3de8-4681-aff1-847a073497f6"
    assert parse_filesender_token(f"https://filesender.surf.nl/?s=download&token={token}") == token
    assert parse_filesender_token(f" {token} ") == token


def test_parse_dotenv():
    text = "# c\nAPI_SECRET='abc'\nexport FILESENDER_USERNAME=\"u@x.nl\"\n\nBAD LINE\nK=v=w\n"
    assert parse_dotenv(text) == {"API_SECRET": "abc", "FILESENDER_USERNAME": "u@x.nl", "K": "v=w"}


def test_environment_overrides_dotenv(tmp_path):
    (tmp_path / ".env").write_text("API_SECRET=file\nFILESENDER_USERNAME=u\n")
    env = load_env({"API_SECRET": "env", "UNRELATED": "x"}, tmp_path / ".env")
    assert (
        env["API_SECRET"] == "env" and env["FILESENDER_USERNAME"] == "u" and "UNRELATED" not in env
    )


def test_filesender_config_from_env(tmp_path):
    cfg = load_filesender_config(tmp_path, {"FILESENDER_USERNAME": "u@x.nl", "API_SECRET": "s"})
    assert (cfg.username, cfg.apikey) == ("u@x.nl", "s")
    assert cfg.base_url == "https://filesender.surf.nl/rest.php"


def test_filesender_config_missing_credentials_is_none(tmp_path):
    assert load_filesender_config(tmp_path, {}) is None


def test_filesender_config_from_ini_and_cli_override(tmp_path):
    ini = tmp_path / ".filesender"
    ini.mkdir()
    (ini / "filesender.py.ini").write_text(
        "[system]\nbase_url = https://fs.example/rest.php\n[user]\nusername = a\napikey = k\n"
        "[filesender]\ncentre = Centre A\n"
    )
    cfg = load_filesender_config(tmp_path, {})
    assert (cfg.base_url, cfg.username, cfg.apikey, cfg.centre) == (
        "https://fs.example/rest.php",
        "a",
        "k",
        "Centre A",
    )
    assert load_filesender_config(tmp_path, {}, {"username": "b"}).username == "b"


def test_surfdrive_config_both_modes(tmp_path):
    d = tmp_path / ".surfdrive"
    d.mkdir()
    (d / "surfdrive.ini").write_text(
        "[surfdrive-public]\ntoken = TOK\npassword = pw\ncentre = A\n"
        "[surfdrive-user]\nusername = me@x.nl\napp_password = app\nfolder = /Slides/in\n"
        "[surfdrive-public:second]\ntoken = T2\n"
    )
    pub, user, second = load_surfdrive_configs(tmp_path)
    assert (pub.mode, pub.username, pub.password, pub.centre) == ("public", "TOK", "pw", "A")
    assert (user.mode, user.username, user.password, user.remote_folder) == (
        "user",
        "me@x.nl",
        "app",
        "Slides/in",
    )
    assert second.name == "second" and second.password == ""


def test_surfdrive_config_validates(tmp_path):
    (tmp_path / ".surfdrive").mkdir()
    (tmp_path / ".surfdrive" / "surfdrive.ini").write_text("[surfdrive-public]\npassword = x\n")
    with pytest.raises(ValueError, match="token"):
        load_surfdrive_configs(tmp_path)


# --- cli ------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.chdir(tmp_path)
    for var in ("API_SECRET", "FILESENDER_USERNAME", "SURFDRIVE_SHARE_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    return h


def run_cli(*args):
    return cli.main(list(args))


def test_no_source_and_no_action_is_a_usage_error(home, tmp_path, capsys):
    assert run_cli("-o", str(tmp_path / "out")) == report.EXIT_USAGE
    assert "specify a source" in capsys.readouterr().err


def test_filesender_without_credentials_is_a_usage_error(home, tmp_path, capsys):
    assert run_cli("-o", str(tmp_path / "out"), "-e", "g@x.nl") == report.EXIT_USAGE
    assert "API_SECRET" in capsys.readouterr().err


def test_unknown_surfdrive_section_is_a_usage_error(home, tmp_path, capsys):
    assert run_cli("-o", str(tmp_path / "out"), "--surfdrive", "user") == report.EXIT_USAGE
    assert "surfdrive.ini" in capsys.readouterr().err


def test_mark_complete_requires_a_note_and_a_known_source(home, tmp_path):
    out = tmp_path / "out"
    assert run_cli("-o", str(out), "--mark-complete", "A") == report.EXIT_USAGE
    assert run_cli("-o", str(out), "--mark-complete", "A", "--note", "n") == report.EXIT_USAGE


def test_status_json_report_and_mark_complete_round_trip(home, tmp_path, capsys):
    out = tmp_path / "out"
    state = out / ".filesender_state.json"
    m = Manifest(state)
    m.sync_listing(FakeSource({"a.svs": b"x" * 10}).list(), centre="Centre A")
    m.sync_slides()
    m.save()

    assert run_cli("-o", str(out), "--mark-complete", "Centre A", "--note", "mail 2026-10-01") == 0
    capsys.readouterr()
    code = run_cli("-o", str(out), "--status", "--json")
    data = json.loads(capsys.readouterr().out)
    assert code == report.EXIT_FAILED  # a queued file is outstanding
    assert data["sources"][0]["complete"]["note"] == "mail 2026-10-01"
    assert data["sources"][0]["state"].startswith("complete, but 1 of 1 slides not verified")

    csv_path = tmp_path / "slides.csv"
    assert run_cli("-o", str(out), "--report", str(csv_path)) == 0
    rows = list(csv.DictReader(csv_path.open()))
    assert rows[0]["slide"] == "a" and rows[0]["centre"] == "Centre A"
    assert rows[0]["status"] == "queued" and rows[0]["member_files"].endswith("a.svs")


def test_main_runs_a_source_end_to_end_with_exit_codes(home, tmp_path, monkeypatch, capsys):
    source = FakeSource({"a.svs": b"A" * 10, "b.svs": b"B" * 10})
    monkeypatch.setattr(cli, "build_sources", lambda args, config: [source])
    monkeypatch.setattr(cli, "SlideValidator", lambda: FakeRunner())
    out = tmp_path / "out"
    assert run_cli("-o", str(out), "-e", "g@x.nl", "--min-free", "0", "--max-files", "1") == (
        report.EXIT_BUDGET
    )
    text = capsys.readouterr().out
    assert "Run budget reached" in text and "SAFE TO MOVE" in text
    assert run_cli("-o", str(out), "-e", "g@x.nl", "--min-free", "0") == report.EXIT_OK
    assert (out / "surf_transfer.log").exists()  # log file next to the manifest


def test_main_dry_run_returns_zero_and_downloads_nothing(home, tmp_path, monkeypatch):
    source = FakeSource({"a.svs": b"A" * 10})
    monkeypatch.setattr(cli, "build_sources", lambda args, config: [source])
    assert run_cli("-o", str(tmp_path / "out"), "-e", "g", "--dry-run") == 0
    assert source.opened == []


def test_verify_archive_standalone(home, tmp_path, capsys):
    out, archive = tmp_path / "out", tmp_path / "archive"
    archive.mkdir()
    assert run_cli("-o", str(out), "--verify-archive", str(archive)) == 0
    assert "0 file(s) verified" in capsys.readouterr().out


def test_existing_flags_are_all_still_accepted():
    parser = cli.build_arg_parser()
    args = parser.parse_args(
        [
            "-e",
            "g@x.nl",
            "--guest-id",
            "3",
            "-o",
            "d",
            "--limit",
            "2",
            "-v",
            "--insecure",
            "--dry-run",
            "--force",
            "-u",
            "u",
            "-a",
            "k",
            "-b",
            "https://x/rest.php",
            "--state-file",
            "s.json",
            "--max-files",
            "5",
            "--max-bytes",
            "1G",
            "--archive-dir",
            "a",
            "--verify-archive",
            "v",
            "--status",
            "--recheck-hashes",
            "--list-guests",
        ]
    )
    assert args.max_bytes == "1G" and args.recheck_hashes and args.force and args.dry_run


def test_surfdrive_link_dir_becomes_the_remote_folder(tmp_path):
    args = cli.build_arg_parser().parse_args(
        ["--surfdrive-link", "https://surfdrive.surf.nl/s/abc123?dir=/40x-scans/Sym", "-o", "out"]
    )
    config = cli.build_config(args, {}, tmp_path)
    (drive,) = config.surfdrive
    assert drive.username == "abc123" and drive.remote_folder == "40x-scans/Sym"
