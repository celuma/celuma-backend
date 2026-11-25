"""The Céluma 1.5 release revision `v1_5_0` (#64): an EMPTY `numbering_type_config` on top of `v1_4_1`.

Real Alembic chain on a fresh, synthetic, disposable database (`celuma_type_numbering_mig_test`, dropped
before and after); never a developer, customer or shared database, never a stamp.
Contract: the migration docstring and `celuma-engineering/specs/laboratory/type-numbering.md`.
"""
from __future__ import annotations

import uuid

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
