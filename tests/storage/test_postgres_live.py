"""Live-Postgres integration tests for the storage backend.

Skipped unless HERMES_STORAGE_TEST_DSN is set, e.g.::

    HERMES_STORAGE_TEST_DSN=postgresql://user:***@localhost:5432/hermes_storage_test \
        scripts/run_tests.sh tests/storage/test_postgres_live.py

Requires the [postgres] extra (sqlalchemy, psycopg) installed in the test env.
"""
import hashlib
import os
import uuid

import pytest

DSN = os.environ.get("HERMES_STORAGE_TEST_DSN", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="HERMES_STORAGE_TEST_DSN not set")

from storage.postgres import PostgresBackend  # noqa: E402


def _tenant(tag: str) -> str:
    """Unique tenant per run so a shared test DB never mixes data between runs."""
    return f"test_{tag}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


def test_provision_and_roundtrip():
    backend = PostgresBackend(DSN, _tenant("round"))
    try:
        assert backend.is_ready()
        doc = backend.put_document("SOUL", "SOUL.md", "hello", created_by="test")
        assert doc.version == 1
        assert doc.checksum == hashlib.sha256(b"hello").hexdigest()
        got = backend.get_document("SOUL", "SOUL.md")
        assert got is not None
        assert got.content == "hello" and got.version == 1 and got.created_by == "test"
        assert backend.get_document("SOUL", "nope") is None
    finally:
        backend.close()


def test_versions_append_only():
    backend = PostgresBackend(DSN, _tenant("versions"))
    try:
        v1 = backend.put_document("SOUL", "SOUL.md", "one")
        v2 = backend.put_document("SOUL", "SOUL.md", "two")
        assert (v1.version, v2.version) == (1, 2)
        assert backend.get_document("SOUL", "SOUL.md").content == "two"
        assert backend.get_document("SOUL", "SOUL.md", version=1).content == "one"
        assert backend.get_document("SOUL", "SOUL.md", version=9) is None
    finally:
        backend.close()


def test_tenant_isolation():
    a = PostgresBackend(DSN, _tenant("iso_a"))
    b = PostgresBackend(DSN, _tenant("iso_b"))
    try:
        a.put_document("SOUL", "SOUL.md", "tenant-a")
        assert b.get_document("SOUL", "SOUL.md") is None
        assert a.get_document("SOUL", "SOUL.md").content == "tenant-a"
    finally:
        a.close()
        b.close()


def test_reprovision_is_idempotent():
    tenant = _tenant("idempotent")
    first = PostgresBackend(DSN, tenant)
    try:
        first.put_document("SOUL", "SOUL.md", "keep me")
        # A second backend on the same tenant re-runs migrations on construction.
        second = PostgresBackend(DSN, tenant)
        try:
            doc = second.get_document("SOUL", "SOUL.md")
            assert doc is not None and doc.content == "keep me"
            assert second.put_document("SOUL", "SOUL.md", "next").version == 2
        finally:
            second.close()
    finally:
        first.close()


def test_document_counts_inventory():
    backend = PostgresBackend(DSN, _tenant("inventory"))
    try:
        backend.put_document("SOUL", "SOUL.md", "a")
        backend.put_document("SOUL", "SOUL.md", "b")
        backend.put_document("MEMORY", "MEMORY.md", "c")
        counts = dict(backend.document_counts())
        assert counts == {"SOUL": 2, "MEMORY": 1}
    finally:
        backend.close()
