"""The Céluma 1.5 release revision `v1_5_0` (#64): an EMPTY `numbering_type_config` on top of `v1_4_1`.

Real Alembic chain on a fresh, synthetic, disposable database (`celuma_type_numbering_mig_test`, dropped
before and after); never a developer, customer or shared database, never a stamp.
Contract: the migration docstring and `celuma-engineering/specs/laboratory/type-numbering.md`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from tests.test_alembic_migrations import (
    HOTFIX_1_4_1_REVISION,
    RELEASE_1_5_REVISION,
    _admin_engine,
    _alembic,
    _current_revision,
)
from tests.test_sample_type_migration import insert

DB = "celuma_type_numbering_mig_test"


@pytest.fixture(name="mdb")
def mdb_fixture():
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{DB}"'))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"Postgres not reachable for migration tests: {exc}")
    finally:
        admin.dispose()
    engine = create_engine(make_url(settings.database_url).set(database=DB))
    yield engine
    engine.dispose()
    admin = _admin_engine()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)'))
    admin.dispose()


def exists(conn) -> bool:
    return conn.execute(text("SELECT to_regclass('public.numbering_type_config') IS NOT NULL")).scalar_one()


def test_upgrade_from_1_4_1_creates_an_empty_table_and_rewrites_nothing(mdb):
    _alembic(HOTFIX_1_4_1_REVISION, database=DB)
    with mdb.begin() as conn:
        tenant = insert(conn, "tenant")["id"]
        branch = insert(conn, "branch", tenant_id=tenant)["id"]
        study = insert(conn, "study_type", tenant_id=tenant, code="CU", name="Citología", is_active=True)["id"]
        insert(conn, '"order"', tenant_id=tenant, branch_id=branch, order_code="CU-1", study_type_id=study)
        conn.execute(text(
            "INSERT INTO numbering_config (tenant_id, entity_kind, active, manual_policy, format, sequence_min_width, "
            "start_value, scope, timezone, updated_at) VALUES (:t, 'order', true, 'allowed', '{BASE}{SEQ}{MM}{YY}', 2, 1, "
            "'tenant', 'America/Mexico_City', now())"), {"t": tenant})
        assert not exists(conn)
    _alembic(RELEASE_1_5_REVISION, database=DB)
    with mdb.connect() as conn:
        assert _current_revision(mdb) == RELEASE_1_5_REVISION
        assert exists(conn)
        assert conn.execute(text("SELECT count(*) FROM numbering_type_config")).scalar_one() == 0
        assert conn.execute(text('SELECT order_code FROM "order"')).scalar_one() == "CU-1"
        assert conn.execute(text("SELECT format FROM numbering_config")).scalar_one() == "{BASE}{SEQ}{MM}{YY}"


@pytest.mark.parametrize("column,value", [
    ("entity_kind", "invoice"), ("sequence_min_width", 0), ("sequence_min_width", 7),
    ("start_value", 0), ("start_value", 100_000_000),
])
def test_constraints_refuse_out_of_contract_rows(mdb, column, value):
    _alembic(RELEASE_1_5_REVISION, database=DB)
    with mdb.begin() as conn:
        tenant = insert(conn, "tenant")["id"]
    row = {"t": tenant, "k": "order", "i": str(uuid.uuid4()), "f": "{BASE}-{SEQ}{MM}{YY}", "w": 2, "s": 1}
    row[{"entity_kind": "k", "sequence_min_width": "w", "start_value": "s"}[column]] = value
    with pytest.raises(IntegrityError), mdb.begin() as conn:
        conn.execute(text(
            "INSERT INTO numbering_type_config (tenant_id, entity_kind, type_id, format, sequence_min_width, start_value, "
            "created_at, updated_at) VALUES (:t, :k, :i, :f, :w, :s, now(), now())"), row)


def test_one_row_per_tenant_kind_and_type(mdb):
    _alembic(RELEASE_1_5_REVISION, database=DB)
    with mdb.begin() as conn:
        tenant = insert(conn, "tenant")["id"]
    sql = text("INSERT INTO numbering_type_config (tenant_id, entity_kind, type_id, format, sequence_min_width, "
               "start_value, created_at, updated_at) VALUES (:t, 'sample', :i, '{BASE}{SEQ}{MM}{YY}', 2, 1, now(), now())")
    type_id = str(uuid.uuid4())
    with mdb.begin() as conn:
        conn.execute(sql, {"t": tenant, "i": type_id})
    with pytest.raises(IntegrityError), mdb.begin() as conn:
        conn.execute(sql, {"t": tenant, "i": type_id})


def test_downgrade_drops_only_the_table(mdb):
    _alembic(RELEASE_1_5_REVISION, database=DB)
    _alembic(HOTFIX_1_4_1_REVISION, command="downgrade", database=DB)
    with mdb.connect() as conn:
        assert _current_revision(mdb) == HOTFIX_1_4_1_REVISION
        assert not exists(conn)
        assert conn.execute(text("SELECT to_regclass('public.numbering_config') IS NOT NULL")).scalar_one()


# ---------------------------------------------------------------------------- §2: catalog type removal (#94)

REMOVAL_COLUMNS = {"removed_at", "removed_by", "removal_reason", "code_released_at"}


def removal_columns(conn, table: str) -> set:
    return {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t "
        "AND column_name IN ('removed_at', 'removed_by', 'removal_reason', 'code_released_at')"), {"t": table})}


def seed_catalogs(conn) -> dict:
    tenant = insert(conn, "tenant")["id"]
    branch = insert(conn, "branch", tenant_id=tenant)["id"]
    study = insert(conn, "study_type", tenant_id=tenant, code="CU", name="Citología", is_active=True)["id"]
    inactive = insert(conn, "study_type", tenant_id=tenant, code="OLD", name="Anterior", is_active=False)["id"]
    sample_type = insert(conn, "sample_type", tenant_id=tenant, code="BX", name="Biopsia", is_active=True)["id"]
    order = insert(conn, '"order"', tenant_id=tenant, branch_id=branch, order_code="CU-1", study_type_id=study)["id"]
    insert(conn, "sample", tenant_id=tenant, branch_id=branch, order_id=order, sample_code="BX-1",
           sample_type_id=sample_type)
    return {"tenant": tenant, "study": study, "inactive": inactive, "sample_type": sample_type}


def test_upgrade_adds_an_empty_removal_marker_to_both_catalogs(mdb):
    _alembic(HOTFIX_1_4_1_REVISION, database=DB)
    with mdb.begin() as conn:
        seeded = seed_catalogs(conn)
        assert removal_columns(conn, "study_type") == set() and removal_columns(conn, "sample_type") == set()
    _alembic(RELEASE_1_5_REVISION, database=DB)
    with mdb.connect() as conn:
        for table in ("study_type", "sample_type"):
            assert removal_columns(conn, table) == REMOVAL_COLUMNS
            assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE removed_at IS NOT NULL OR removed_by IS NOT "
                                     "NULL OR removal_reason IS NOT NULL OR code_released_at IS NOT NULL")).scalar_one() == 0
        # sample-type codes: unique among code-reserving rows (was the full uq_sample_type_tenant_code)
        uniques = conn.execute(text("SELECT conname FROM pg_constraint WHERE conrelid = 'public.sample_type'::regclass "
                                    "AND contype = 'u'")).scalars().all()
        assert "uq_sample_type_tenant_code" not in uniques
        index = conn.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_sample_type_tenant_code_reserved'"
                                  )).scalar_one()
        assert "UNIQUE" in index and "code_released_at IS NULL" in index
        # nothing is removed or reactivated by the upgrade
        actives = dict(conn.execute(text("SELECT code, is_active FROM study_type")).all())
        assert actives == {"CU": True, "OLD": False}
        assert conn.execute(text("SELECT sample_type_id FROM sample")).scalar_one() == seeded["sample_type"]


@pytest.mark.parametrize("table", ["study_type", "sample_type"])
def test_a_removed_type_can_never_be_active(mdb, table):
    _alembic(RELEASE_1_5_REVISION, database=DB)
    with mdb.begin() as conn:
        tenant = insert(conn, "tenant")["id"]
    with pytest.raises(IntegrityError, match=f"ck_{table}_removed_inactive"), mdb.begin() as conn:
        insert(conn, table, tenant_id=tenant, code="X", name="X", is_active=True, removed_at=datetime(2026, 11, 25))
    with mdb.begin() as conn:
        row = insert(conn, table, tenant_id=tenant, code="Y", name="Y", is_active=False,
                     removed_at=datetime(2026, 11, 25), removal_reason="duplicado")["id"]
    with pytest.raises(IntegrityError, match=f"ck_{table}_removed_inactive"), mdb.begin() as conn:
        conn.execute(text(f"UPDATE {table} SET is_active = true WHERE id = :i"), {"i": row})


def test_downgrade_drops_the_marker_and_leaves_removed_types_inactive(mdb):
    _alembic(RELEASE_1_5_REVISION, database=DB)
    with mdb.begin() as conn:
        seeded = seed_catalogs(conn)
        conn.execute(text("UPDATE sample_type SET is_active = false, removed_at = now(), removal_reason = 'x' "
                          "WHERE id = :i"), {"i": seeded["sample_type"]})
    _alembic(HOTFIX_1_4_1_REVISION, command="downgrade", database=DB)
    with mdb.connect() as conn:
        assert _current_revision(mdb) == HOTFIX_1_4_1_REVISION
        assert removal_columns(conn, "study_type") == set() and removal_columns(conn, "sample_type") == set()
        # the removed type survives as an ordinary INACTIVE type, with its id, code and references
        row = conn.execute(text("SELECT code, is_active FROM sample_type WHERE id = :i"),
                           {"i": seeded["sample_type"]}).one()
        assert tuple(row) == ("BX", False)
        assert conn.execute(text("SELECT sample_type_id FROM sample")).scalar_one() == seeded["sample_type"]
    # and the cycle is repeatable on the same database
    _alembic(RELEASE_1_5_REVISION, database=DB)
    with mdb.connect() as conn:
        assert removal_columns(conn, "sample_type") == REMOVAL_COLUMNS


def test_one_reserving_row_per_code_and_only_removed_types_release(mdb):
    _alembic(RELEASE_1_5_REVISION, database=DB)
    with mdb.begin() as conn:
        tenant = insert(conn, "tenant")["id"]
        insert(conn, "sample_type", tenant_id=tenant, code="BX", name="A", is_active=False,
               removed_at=datetime(2026, 11, 25), code_released_at=datetime(2026, 11, 25))
        insert(conn, "sample_type", tenant_id=tenant, code="BX", name="B", is_active=True)  # reuse is allowed
    with pytest.raises(IntegrityError, match="uq_sample_type_tenant_code_reserved"), mdb.begin() as conn:
        insert(conn, "sample_type", tenant_id=tenant, code="BX", name="C", is_active=True)
    with pytest.raises(IntegrityError, match="ck_study_type_code_release_removed"), mdb.begin() as conn:
        insert(conn, "study_type", tenant_id=tenant, code="CU", name="X", is_active=False,
               code_released_at=datetime(2026, 11, 25))


@pytest.mark.parametrize("table", ["study_type", "sample_type"])
def test_downgrade_refuses_when_a_released_code_was_reused_and_changes_nothing(mdb, table):
    _alembic(RELEASE_1_5_REVISION, database=DB)
    with mdb.begin() as conn:
        tenant = insert(conn, "tenant")["id"]
        old = insert(conn, table, tenant_id=tenant, code="BX", name="Anterior", is_active=False,
                     removed_at=datetime(2026, 11, 25), code_released_at=datetime(2026, 11, 25))["id"]
        insert(conn, table, tenant_id=tenant, code="BX", name="Nuevo", is_active=True)
    with pytest.raises(Exception):
        _alembic(HOTFIX_1_4_1_REVISION, command="downgrade", database=DB)
    with mdb.connect() as conn:
        assert _current_revision(mdb) == RELEASE_1_5_REVISION
        assert conn.execute(text(f"SELECT code, code_released_at IS NOT NULL FROM {table} WHERE id = :i"),
                            {"i": old}).one() == ("BX", True)
        assert removal_columns(conn, table) == REMOVAL_COLUMNS


def test_downgrade_without_reuse_restores_the_full_unique_constraint(mdb):
    _alembic(RELEASE_1_5_REVISION, database=DB)
    with mdb.begin() as conn:
        tenant = insert(conn, "tenant")["id"]
        insert(conn, "sample_type", tenant_id=tenant, code="BX", name="Liberado", is_active=False,
               removed_at=datetime(2026, 11, 25), code_released_at=datetime(2026, 11, 25))
    _alembic(HOTFIX_1_4_1_REVISION, command="downgrade", database=DB)
    with mdb.connect() as conn:
        uniques = conn.execute(text("SELECT conname FROM pg_constraint WHERE conrelid = 'public.sample_type'::regclass "
                                    "AND contype = 'u'")).scalars().all()
        assert "uq_sample_type_tenant_code" in uniques
        assert conn.execute(text("SELECT code, is_active FROM sample_type")).one() == ("BX", False)
