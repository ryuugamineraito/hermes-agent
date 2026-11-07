"""Storage config: the ``storage:`` section of config.yaml.

Behavior (backend choice) lives in config.yaml per repo policy; the DSN is a
secret, so ``dsn_env`` names the environment variable (typically from ``.env``
or container env) holding it.

    storage:
      backend: postgres                # local (default) | postgres
      dsn: postgresql://user:***@host:5432/hermes   # or:
      dsn_env: HERMES_PG_DSN
      tenant: work                     # defaults to the active profile name
"""
from __future__ import annotations

import os
from dataclasses import dataclass

LOCAL = "local"
POSTGRES = "postgres"


class StorageConfigError(ValueError):
    """Malformed ``storage:`` section."""


@dataclass(frozen=True)
class StorageConfig:
    backend: str
    dsn: str
    tenant: str


def storage_config_from_dict(config: dict | None) -> StorageConfig | None:
    """Parse the ``storage:`` section. None = local mode (callers keep file paths)."""
    section = (config or {}).get("storage") or {}
    if not isinstance(section, dict):
        raise StorageConfigError("storage: must be a mapping")
    backend = str(section.get("backend", LOCAL)).strip().lower()
    if not section or backend == LOCAL:
        return None
    if backend != POSTGRES:
        raise StorageConfigError(f"storage.backend: unknown {backend!r} (expected local|postgres)")
    dsn_env = section.get("dsn_env")
    dsn = section.get("dsn") or (os.environ.get(str(dsn_env)) if dsn_env else None)
    if not dsn:
        raise StorageConfigError("storage: backend=postgres needs dsn or dsn_env (secret env var)")
    tenant = str(section.get("tenant") or _active_profile()).strip()
    return StorageConfig(backend=backend, dsn=normalize_dsn(str(dsn)), tenant=tenant or "default")


def get_storage_config() -> StorageConfig | None:
    """The section from config.yaml (None in local mode; raises StorageConfigError).

    A bound worker (safe/config/managed) gets the frozen session config snapshot —
    the same source ``load_config`` uses there; opening config.yaml would violate
    the safe-worker no-file-read audit contract. Otherwise the file is read via
    ``read_config_doc`` directly — never via ``load_config()``: that path runs home
    initialization, which itself asks for the storage backend, and the config lock
    is not reentrant, so the reentrant ``load_config`` would deadlock (first-run
    SOUL seeding hit exactly this). Unreadable/missing files fall back to local
    mode; the main config loader reports the real error.
    """
    from hermes_cli.config import _worker_config_snapshot
    snapshot = _worker_config_snapshot()
    if snapshot is not None:
        return storage_config_from_dict(snapshot)
    from hermes_cli.config import get_config_path
    from hermes_cli.config_backend import read_config_doc
    try:
        data = read_config_doc(get_config_path())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    section = data.get("storage")
    if section is None:
        return None
    return storage_config_from_dict({"storage": section})


def normalize_dsn(dsn: str) -> str:
    """``postgresql://`` / ``postgres://`` → SQLAlchemy's explicit psycopg dialect."""
    for scheme in ("postgresql://", "postgres://"):
        if dsn.startswith(scheme):
            return dsn.replace(scheme, "postgresql+psycopg://", 1)
    return dsn


def _active_profile() -> str:
    from hermes_cli.profiles import get_active_profile_name
    try:
        return get_active_profile_name() or "default"
    except Exception:
        return "default"
