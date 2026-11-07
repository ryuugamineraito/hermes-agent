"""``hermes storage``: dispatch, reply shapes and exit codes (local mode + no-server paths).

Live Postgres behavior is covered in tests/storage/test_postgres_live.py
(HERMES_STORAGE_TEST_DSN).
"""
import json

import pytest

from hermes_cli.main import _build_cli_parser


def _run(capsys, *argv):
    """Run the command in-process; SystemExit becomes the (int) exit code."""
    parser, _ = _build_cli_parser()
    args = parser.parse_args(list(argv))
    try:
        code = args.func(args)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    out, err = capsys.readouterr()
    return code, out, err


def _force_local(monkeypatch):
    """No storage section, whatever the sandbox config says."""
    monkeypatch.setattr("storage.config.get_storage_config", lambda: None)


def test_status_local_mode(capsys, monkeypatch):
    _force_local(monkeypatch)
    code, out, err = _run(capsys, "storage", "status")
    assert code == 0
    assert "backend: local" in out


def test_status_local_json(capsys, monkeypatch):
    _force_local(monkeypatch)
    code, out, err = _run(capsys, "storage", "status", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["backend"] == "local"


def test_provision_requires_postgres(capsys, monkeypatch):
    _force_local(monkeypatch)
    code, out, err = _run(capsys, "storage", "provision")
    assert code == 1
    assert "local" in out


def test_soul_set_requires_postgres(capsys, monkeypatch):
    _force_local(monkeypatch)
    code, out, err = _run(capsys, "storage", "soul", "set", "--text", "x")
    assert code == 1


def test_soul_show_reads_home_file(tmp_path, monkeypatch, capsys):
    (tmp_path / "SOUL.md").write_text("local soul", encoding="utf-8")
    _force_local(monkeypatch)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    code, out, err = _run(capsys, "storage", "soul", "show")
    assert code == 0
    assert out.strip() == "local soul"


def test_soul_show_missing_home_file(tmp_path, monkeypatch, capsys):
    _force_local(monkeypatch)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    code, out, err = _run(capsys, "storage", "soul", "show")
    assert code == 1
    assert "no SOUL.md" in err


def test_config_error_is_reported_not_crashed(capsys, monkeypatch):
    from storage.config import StorageConfigError

    def _boom():
        raise StorageConfigError("storage: backend=postgres needs dsn or dsn_env")

    monkeypatch.setattr("storage.config.get_storage_config", _boom)
    code, out, err = _run(capsys, "storage", "status")
    assert code == 2
    assert "storage:" in err
