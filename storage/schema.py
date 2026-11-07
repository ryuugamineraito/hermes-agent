"""Schema DDL and the versioned migration runner (per-tenant schemas, design D5).

``public`` holds the shared catalog (tenant registry + per-schema migration
state); each tenant gets ``tenant_<name>`` with its tables. The runner is
idempotent: safe to run on every backend construction (stateless containers
boot with an empty scratch home and self-provision).
"""
from __future__ import annotations

import re

SCHEMA_VERSION = 1
PUBLIC_SCHEMA = "public"

PUBLIC_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS public.tenants (
               schema_name text PRIMARY KEY,
               name text UNIQUE NOT NULL,
               created_at timestamptz NOT NULL DEFAULT now()
           )""",
        """CREATE TABLE IF NOT EXISTS public.migration_state (
               schema_name text PRIMARY KEY,
               version integer NOT NULL,
               applied_at timestamptz NOT NULL DEFAULT now()
           )""",
    ],
}

TENANT_MIGRATIONS: dict[int, list[str]] = {
    1: [
        # Versioned content (SOUL/skills/memory/config in later increments).
        # Head lookups ORDER BY version DESC on the PK (kind, key, version).
        """CREATE TABLE IF NOT EXISTS documents (
               kind text NOT NULL,
               key text NOT NULL,
               version integer NOT NULL,
               content text NOT NULL,
               meta jsonb NOT NULL DEFAULT '{}',
               checksum text NOT NULL,
               created_by text NOT NULL DEFAULT 'agent',
               created_at timestamptz NOT NULL DEFAULT now(),
               PRIMARY KEY (kind, key, version)
           )""",
    ],
}

_SAFE = re.compile(r"^[a-z0-9_]+$")
# Postgres truncates identifiers at 63 bytes; keep the full name inside that.
_MAX_PAYLOAD = 63 - len("tenant_")


def schema_name_for(tenant: str) -> str:
    """Sanitize a tenant (profile) name into a safe ``tenant_<x>`` schema name."""
    name = re.sub(r"[^a-z0-9_]", "_", str(tenant).lower())
    if not any(c.isalnum() for c in name):
        name = "default"
    name = name[:_MAX_PAYLOAD]
    schema = f"tenant_{name}"
    if not _SAFE.match(schema):  # belt: _SAFE is total after the sub, but fail closed
        raise ValueError(f"unsafe tenant schema name: {schema!r}")
    return schema


def apply_migrations(conn, tenant_schema: str, tenant_name: str) -> int:
    """Apply pending migrations on a transactional SQLAlchemy connection.

    Runs inside the caller's transaction (``engine.begin()``); returns the
    tenant schema version after the call. Idempotent.
    """
    from sqlalchemy import text

    for stmt in PUBLIC_MIGRATIONS[1]:
        conn.execute(text(stmt))
    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {tenant_schema}"))
    current = conn.execute(
        text(f"SELECT version FROM {PUBLIC_SCHEMA}.migration_state WHERE schema_name = :s"),
        {"s": tenant_schema},
    ).scalar() or 0
    if current < SCHEMA_VERSION:
        # Tenant DDL is unqualified; pin the search path for this transaction only.
        conn.execute(text(f"SET LOCAL search_path TO {tenant_schema}, {PUBLIC_SCHEMA}"))
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for stmt in TENANT_MIGRATIONS[version]:
                conn.execute(text(stmt))
            conn.execute(
                text(
                    f"INSERT INTO {PUBLIC_SCHEMA}.migration_state (schema_name, version) "
                    "VALUES (:s, :v) ON CONFLICT (schema_name) DO UPDATE "
                    "SET version = EXCLUDED.version, applied_at = now()"
                ),
                {"s": tenant_schema, "v": version},
            )
            current = version
    conn.execute(
        text(
            f"INSERT INTO {PUBLIC_SCHEMA}.tenants (schema_name, name) VALUES (:s, :n) "
            "ON CONFLICT (schema_name) DO NOTHING"
        ),
        {"s": tenant_schema, "n": tenant_name},
    )
    return current
