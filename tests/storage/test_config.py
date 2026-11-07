"""storage.config: the config.yaml ``storage:`` section (no I/O, no DB)."""
import pytest

from storage.config import POSTGRES, StorageConfigError, get_storage_config, storage_config_from_dict


def test_absent_section_is_local():
    assert storage_config_from_dict(None) is None
    assert storage_config_from_dict({}) is None


def test_local_mode():
    assert storage_config_from_dict({"storage": {"backend": "local"}}) is None
    assert storage_config_from_dict({"storage": {}}) is None


def test_unknown_backend_rejected():
    with pytest.raises(StorageConfigError):
        storage_config_from_dict({"storage": {"backend": "mysql"}})


def test_section_must_be_mapping():
    with pytest.raises(StorageConfigError):
        storage_config_from_dict({"storage": "postgres"})


def test_postgres_requires_dsn(monkeypatch):
    monkeypatch.delenv("STORAGE_TEST_DSN_ENV", raising=False)
    with pytest.raises(StorageConfigError):
        storage_config_from_dict({"storage": {"backend": "postgres"}})


def test_dsn_env_must_resolve(monkeypatch):
    monkeypatch.delenv("STORAGE_TEST_DSN_ENV", raising=False)
    with pytest.raises(StorageConfigError):
        storage_config_from_dict({"storage": {"backend": "postgres",
                                              "dsn_env": "STORAGE_TEST_DSN_ENV"}})


def test_inline_dsn_normalized(monkeypatch):
    monkeypatch.delenv("STORAGE_TEST_DSN_ENV", raising=False)
    cfg = storage_config_from_dict({"storage": {"backend": "postgres",
                                                "dsn": "postgresql://u:p@h:5432/db",
                                                "tenant": "work"}})
    assert cfg is not None
    assert cfg.backend == POSTGRES
    assert cfg.dsn == "postgresql+psycopg://u:p@h:5432/db"
    assert cfg.tenant == "work"


def test_postgres_scheme_normalized(monkeypatch):
    monkeypatch.delenv("STORAGE_TEST_DSN_ENV", raising=False)
    cfg = storage_config_from_dict({"storage": {"backend": "postgres",
                                                "dsn": "postgres://u@h/db",
                                                "tenant": "t"}})
    assert cfg.dsn == "postgresql+psycopg://u@h/db"


def test_dsn_env_resolution(monkeypatch):
    monkeypatch.setenv("STORAGE_TEST_DSN_ENV", "postgres://u@h/db")
    cfg = storage_config_from_dict({"storage": {"backend": "postgres",
                                                "dsn_env": "STORAGE_TEST_DSN_ENV",
                                                "tenant": "t"}})
    assert cfg is not None
    assert cfg.dsn == "postgresql+psycopg://u@h/db"


def test_inline_dsn_wins_over_env(monkeypatch):
    monkeypatch.setenv("STORAGE_TEST_DSN_ENV", "postgres://env@h/db")
    cfg = storage_config_from_dict({"storage": {"backend": "postgres",
                                                "dsn": "postgres://inline@h/db",
                                                "dsn_env": "STORAGE_TEST_DSN_ENV",
                                                "tenant": "t"}})
    assert cfg.dsn == "postgresql+psycopg://inline@h/db"


def test_tenant_defaults_to_active_profile(monkeypatch):
    monkeypatch.setenv("STORAGE_TEST_DSN_ENV", "postgres://u@h/db")
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "ops")
    cfg = storage_config_from_dict({"storage": {"backend": "postgres",
                                                "dsn_env": "STORAGE_TEST_DSN_ENV"}})
    assert cfg.tenant == "ops"


def test_tenant_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("STORAGE_TEST_DSN_ENV", "postgres://u@h/db")
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: None)
    cfg = storage_config_from_dict({"storage": {"backend": "postgres",
                                                "dsn_env": "STORAGE_TEST_DSN_ENV"}})
    assert cfg.tenant == "default"


def _point_at_config(tmp_path, monkeypatch, content: str | None):
    path = tmp_path / "config.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr("hermes_cli.config.get_config_path", lambda: path)


def test_get_storage_config_missing_file_is_local(tmp_path, monkeypatch):
    _point_at_config(tmp_path, monkeypatch, None)
    assert get_storage_config() is None


def test_get_storage_config_reads_section(tmp_path, monkeypatch):
    _point_at_config(tmp_path, monkeypatch,
                     "storage:\n  backend: postgres\n  dsn: postgres://u@h/db\n  tenant: t\n")
    cfg = get_storage_config()
    assert cfg is not None
    assert cfg.dsn == "postgresql+psycopg://u@h/db" and cfg.tenant == "t"


def test_get_storage_config_malformed_section_raises(tmp_path, monkeypatch):
    _point_at_config(tmp_path, monkeypatch, "storage:\n  backend: mysql\n")
    with pytest.raises(StorageConfigError):
        get_storage_config()


def test_get_storage_config_unparseable_file_is_local(tmp_path, monkeypatch):
    _point_at_config(tmp_path, monkeypatch, "storage: [unclosed\n  bad: : yaml\n")
    assert get_storage_config() is None


POSTGRES_SECTION_FILE = "storage:\n  backend: postgres\n  dsn: postgres://file@h/db\n  tenant: file\n"


def test_get_storage_config_prefers_worker_snapshot(tmp_path, monkeypatch):
    """A bound worker reads ONLY the frozen snapshot — never the file (audit contract)."""
    _point_at_config(tmp_path, monkeypatch, POSTGRES_SECTION_FILE)
    monkeypatch.setattr(
        "hermes_cli.config._worker_config_snapshot",
        lambda: {"storage": {"backend": "postgres",
                             "dsn": "postgres://snap@h/db", "tenant": "snap"}})
    cfg = get_storage_config()
    assert cfg is not None
    assert cfg.tenant == "snap" and cfg.dsn == "postgresql+psycopg://snap@h/db"


def test_get_storage_config_snapshot_without_section_is_local(tmp_path, monkeypatch):
    _point_at_config(tmp_path, monkeypatch, POSTGRES_SECTION_FILE)
    monkeypatch.setattr("hermes_cli.config._worker_config_snapshot",
                        lambda: {"agent": {}})
    assert get_storage_config() is None
