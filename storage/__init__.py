"""Storage backends for Hermes state (enterprise: PostgreSQL).

Default mode is local: :func:`get_backend` returns ``None`` and callers keep
the existing file/sqlite paths unchanged. ``storage.backend: postgres`` in
config.yaml yields a per-tenant :class:`storage.postgres.PostgresBackend`
(needs the ``[postgres]`` extra).

Profile scope (HX002): the cache key is the tenant NAME the caller resolved
from an explicit home/profile — never an ambient default.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from storage.base import Document, StorageBackend

if TYPE_CHECKING:
    from storage.postgres import PostgresBackend

__all__ = ["Document", "StorageBackend", "get_backend", "clear_backend_cache"]

_backends: dict[str, "PostgresBackend"] = {}


def get_backend(tenant: Optional[str] = None) -> Optional[StorageBackend]:
    """The configured backend for *tenant*, or None in local mode.

    *tenant* is the profile name owning this data; defaults to the tenant in
    the storage config (which itself defaults to the active profile).
    """
    from storage.config import get_storage_config

    cfg = get_storage_config()
    if cfg is None:
        return None
    name = tenant or cfg.tenant
    backend = _backends.get(name)
    if backend is None:
        from storage.postgres import PostgresBackend
        backend = _backends[name] = PostgresBackend(dsn=cfg.dsn, tenant=name)
    return backend


def clear_backend_cache() -> None:
    """Drop cached engines (tests, config changes)."""
    for backend in _backends.values():
        backend.close()
    _backends.clear()
