import os
from pathlib import Path

import pytest

from knife.cli.main import build_parser, main


def test_help_runs():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])


def test_status_command(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("KNIFE_CONFIG", str(tmp_path / "config.json"))
    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "knife" in out


def test_list_command(capsys):
    rc = main(["list", "--top", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PID" in out


def test_policy_update(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("KNIFE_CONFIG", str(tmp_path / "config.json"))
    main(["block", "torrent"])
    main(["policy", "--mode", "block", "--action", "warn"])
    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "torrent" in out
    assert "block" in out
