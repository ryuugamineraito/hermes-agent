"""storage.schema: schema naming + migration table sanity (no live DB needed)."""
import pytest

from storage import schema as S


def test_schema_name_sanitized():
    assert S.schema_name_for("Work Profile") == "tenant_work_profile"
    assert S.schema_name_for("abc/def") == "tenant_abc_def"
    assert S.schema_name_for("Default") == "tenant_default"


def test_schema_name_never_blank_or_unsafe():
    for tenant in ("__", "///", "Ünïcode", "", "a" * 500):
        name = S.schema_name_for(tenant)
        assert S._SAFE.match(name), tenant
        assert name.startswith("tenant_")
        assert len(name) <= 63  # Postgres identifier limit


def test_schema_name_collision_truncation_is_deterministic():
    assert S.schema_name_for("a" * 500) == S.schema_name_for("a" * 500)


def test_migrations_contiguous_from_one():
    assert sorted(S.TENANT_MIGRATIONS) == list(range(1, S.SCHEMA_VERSION + 1))
    assert sorted(S.PUBLIC_MIGRATIONS) == list(range(1, S.SCHEMA_VERSION + 1))


def test_ddl_idempotent_markers():
    for statements in (*S.PUBLIC_MIGRATIONS.values(), *S.TENANT_MIGRATIONS.values()):
        for stmt in statements:
            head = " ".join(stmt.split()).lower()
            assert "if not exists" in head, stmt[:60]


def test_documents_pk_covers_head_lookup():
    ddl = " ".join(S.TENANT_MIGRATIONS[1]).lower()
    assert "primary key (kind, key, version)" in " ".join(ddl.split())


@pytest.mark.parametrize("bad", ("drop schema", "tenant; --", "x\"y"))
def test_schema_name_never_echoes_injection(bad):
    name = S.schema_name_for(bad)
    assert ";" not in name and '"' not in name and " " not in name
