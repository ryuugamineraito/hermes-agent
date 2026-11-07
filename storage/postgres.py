"""PostgreSQL backend: SQLAlchemy Core + psycopg (the [postgres] extra).

System of record for enterprise deployments (design/enterprise-storage-plan.md).
The tenant schema is created and migrated on construction — stateless containers
self-provision at boot; ``hermes storage provision`` exists for operators who
want to pre-create tenants.
"""
from __future__ import annotations

import atexit
import hashlib
import json

from storage import schema as _schema
from storage.base import Document, StorageBackend
from storage.config import normalize_dsn


class PostgresBackend(StorageBackend):
    name = "postgres"

    def __init__(self, dsn: str, tenant: str):
        try:
            from sqlalchemy import create_engine, event, text
        except ImportError as e:  # pragma: no cover - extra not installed
            raise RuntimeError(
                "storage.backend=postgres requires the [postgres] extra "
                "(sqlalchemy, psycopg): install with `hermes-agent[postgres]`"
            ) from e
        self.tenant = tenant
        self._schema = _schema.schema_name_for(tenant)
        self._disposed = False
        self._engine = create_engine(normalize_dsn(dsn), pool_pre_ping=True,
                                     pool_size=5, max_overflow=10)
        # Deterministic teardown: without it the pool is torn down by GC during
        # interpreter finalization, racing psycopg's C extension (SIGSEGV at exit).
        atexit.register(self.close)

        # Capture only strings: a closure capturing `self` would make the engine ->
        # listener -> backend -> engine cycle, keeping the engine alive into late
        # interpreter finalization (SIGSEGV at exit on CPython 3.13+).
        search_path_sql = f"SET search_path TO {self._schema}, {_schema.PUBLIC_SCHEMA}"

        @event.listens_for(self._engine, "connect")
        def _pin_search_path(dbapi_conn, _record):  # every pooled conn sees this tenant first
            cur = dbapi_conn.cursor()
            cur.execute(search_path_sql)
            cur.close()

        with self._engine.begin() as conn:
            _schema.apply_migrations(conn, self._schema, tenant)

    @property
    def schema_name(self) -> str:
        return self._schema

    def get_document(self, kind: str, key: str, version: int | None = None) -> Document | None:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT kind, key, version, content, meta, checksum, created_by, created_at "
                    "FROM documents WHERE kind = :k AND key = :key "
                    "AND (CAST(:v AS integer) IS NULL OR version = :v) "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"k": kind, "key": key, "v": version},
            ).first()
        if row is None:
            return None
        meta = row.meta if isinstance(row.meta, dict) else json.loads(row.meta)
        created_at = row.created_at
        return Document(kind=row.kind, key=row.key, version=row.version, content=row.content,
                        meta=meta, checksum=row.checksum, created_by=row.created_by,
                        created_at=created_at.isoformat() if hasattr(created_at, "isoformat")
                        else str(created_at))

    def put_document(self, kind: str, key: str, content: str, *,
                     meta: dict | None = None, created_by: str = "agent") -> Document:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        params = {"k": kind, "key": key, "content": content,
                  "meta": json.dumps(meta or {}), "checksum": checksum, "by": created_by}
        row = None
        for attempt in (1, 2):
            try:
                with self._engine.begin() as conn:
                    row = conn.execute(
                        text(
                            "INSERT INTO documents (kind, key, version, content, meta, checksum, created_by) "
                            "VALUES (:k, :key, COALESCE((SELECT max(version) FROM documents "
                            "WHERE kind = :k AND key = :key), 0) + 1, :content, CAST(:meta AS jsonb), :checksum, :by) "
                            "RETURNING version, created_at"
                        ),
                        params,
                    ).one()
                break
            except IntegrityError:
                # A concurrent writer took the same (kind, key) version; recompute max+1 once.
                # shortcut: single retry -- a hot key with >2 simultaneous writers can still
                # collide; move to an advisory lock if that ever shows up in audit_log.
                if attempt == 2:
                    raise
        created_at = row[1]
        return Document(kind, key, row[0], content, meta or {}, checksum, created_by,
                        created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at))

    def document_counts(self) -> list[tuple[str, int]]:
        """Inventory: (kind, version count) rows, for ``hermes storage status``."""
        from sqlalchemy import text
        with self._engine.connect() as conn:
            return [(r[0], r[1]) for r in conn.execute(
                text("SELECT kind, count(*) FROM documents GROUP BY kind ORDER BY kind"))]

    def is_ready(self) -> bool:
        from sqlalchemy import text
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._engine.dispose()
