"""The monthly-numbering section (§3) of `v1_4_0` (Céluma 1.4, issue #8).

Real Alembic chain on a fresh, synthetic, disposable database
(`celuma_numbering_mig_test`, dropped before and after); never a developer,
customer or shared database, never a stamp: every scenario upgrades from
`v1_3_1` explicitly. Contract: the migration docstring (§3) and
`celuma-engineering/specs/laboratory/monthly-numbering.md`.
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401  registers every table
from app.core.config import settings
from sqlmodel import SQLModel
from tests.test_alembic_migrations import (
    HOTFIX_REVISION,
    RELEASE_1_4_REVISION,
    _admin_engine,
    _alembic,
    _current_revision,
)
from tests.test_sample_type_migration import insert

DB = "celuma_numbering_mig_test"
TABLES = ("numbering_config", "numbering_counter", "numbering_allocation")
INDEXES = ("ix_order_tenant_order_code", "ix_sample_tenant_sample_code")


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


def up(target=RELEASE_1_4_REVISION):
    _alembic(target, database=DB)


def down(target):
    _alembic(target, command="downgrade", database=DB)


def seed_legacy(conn) -> dict:
    """A 1.3.1 laboratory with old-style and look-alike codes, plus a duplicate
    sample code on two orders (legal before 1.4: unique only per order)."""
    tenant = insert(conn, "tenant")["id"]
    branch = insert(conn, "branch", tenant_id=tenant)["id"]
    orders = {}
    for code in ("CU-1", "CU-011026", "CU011026", "BX-7"):
        orders[code] = insert(conn, '"order"', tenant_id=tenant, branch_id=branch, order_code=code)["id"]
    for order_code, sample_code in (("CU-1", "S-1"), ("CU-011026", "S-1"), ("CU011026", "BX011026")):
        insert(conn, "sample", tenant_id=tenant, branch_id=branch, order_id=orders[order_code],
               sample_code=sample_code, type="BIOPSIA")
    return {"tenant": tenant, "branch": branch, "orders": orders}


def snapshot(conn, table: str) -> list:
    order_by = "id"
    return conn.execute(text(f"SELECT * FROM {table} ORDER BY {order_by}")).all()


class TestUpgrade:
    def test_existing_orders_and_samples_are_untouched_and_the_tables_are_empty(self, mdb):
        up(HOTFIX_REVISION)
        with mdb.begin() as conn:
            seed_legacy(conn)
            orders_before, samples_before = snapshot(conn, '"order"'), snapshot(conn, "sample")
            tenants_before = snapshot(conn, "tenant")
        up()
        assert _current_revision(mdb) == RELEASE_1_4_REVISION
        inspector = inspect(mdb)
        assert set(TABLES) <= set(inspector.get_table_names())
        with mdb.connect() as conn:
            assert snapshot(conn, '"order"') == orders_before
            # `sample` lost `type` and gained `sample_type_id` (§2); every other column is identical
            after = conn.execute(text(
                "SELECT id, tenant_id, branch_id, order_id, sample_code, state::text, notes FROM sample ORDER BY id")).all()
            assert [r[0] for r in after] == [r[0] for r in samples_before]
            assert snapshot(conn, "tenant") == tenants_before
            for table in TABLES:
                assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0, table
            codes = conn.execute(text('SELECT order_code FROM "order"')).scalars().all()
            assert sorted(codes) == sorted(["BX-7", "CU-1", "CU-011026", "CU011026"])  # punctuation and look-alikes preserved

    def test_lookup_indexes_exist_and_are_not_unique(self, mdb):
        up()
        for table, name in (("order", "ix_order_tenant_order_code"), ("sample", "ix_sample_tenant_sample_code")):
            index = {i["name"]: i for i in inspect(mdb).get_indexes(table)}[name]
            assert index["unique"] is False
        with mdb.begin() as conn:  # duplicates stay legal (legacy data may contain them)
            seeded = seed_legacy_head(conn)
            assert seeded

    def test_constraints_and_isolation_of_the_ledger(self, mdb):
        up()
        inspector = inspect(mdb)
        for table in ("numbering_counter", "numbering_allocation"):
            fks = {fk["referred_table"] for fk in inspector.get_foreign_keys(table)}
            assert fks == {"tenant"}, f"{table} must not reference orders, samples or catalogs"
        uniques = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("numbering_allocation")}
        assert uniques["uq_numbering_allocation_code"] == ["tenant_id", "entity_kind", "code"]
        with mdb.begin() as conn:
            tenant = insert(conn, "tenant")["id"]
        scope = "00000000-0000-0000-0000-000000000001"
        bad_rows = [
            ("INSERT INTO numbering_config VALUES (:t, 'patient', 'automatic', 'x', 2, now(), NULL)", {"t": tenant}),
            ("INSERT INTO numbering_config VALUES (:t, 'order', 'bogus', 'x', 2, now(), NULL)", {"t": tenant}),
            ("INSERT INTO numbering_config VALUES (:t, 'order', 'automatic', 'x', 7, now(), NULL)", {"t": tenant}),
            ("INSERT INTO numbering_counter VALUES (:t, 'order', :s, 2026, 13, 1, now())", {"t": tenant, "s": scope}),
            ("INSERT INTO numbering_counter VALUES (:t, 'order', :s, 2026, 10, -1, now())", {"t": tenant, "s": scope}),
        ]
        for sql, params in bad_rows:
            with pytest.raises(IntegrityError):
                with mdb.begin() as conn:
                    conn.execute(text(sql), params)
        ledger = ("INSERT INTO numbering_allocation (id, tenant_id, entity_kind, scope_id, period_year, period_month, "
                  "sequence_value, code, format, sequence_min_width, created_at) VALUES "
                  "(gen_random_uuid(), :t, :k, :s, 2026, 10, :v, :c, 'f', 2, now())")
        with mdb.begin() as conn:
            conn.execute(text(ledger), {"t": tenant, "k": "order", "s": scope, "v": 1, "c": "CU011026"})
            conn.execute(text(ledger), {"t": tenant, "k": "sample", "s": scope, "v": 1, "c": "CU011026"})  # other kind: allowed
        for params in ({"k": "order", "v": 2, "c": "CU011026"}, {"k": "order", "v": 1, "c": "CU021026"}):
            with pytest.raises(IntegrityError):
                with mdb.begin() as conn:
                    conn.execute(text(ledger), {"t": tenant, "s": scope, **params})

    def test_models_match_the_migrated_columns(self, mdb):
        up()
        inspector = inspect(mdb)
        for name in TABLES:
            migrated = {c["name"]: c["nullable"] for c in inspector.get_columns(name)}
            model = {c.name: c.nullable for c in SQLModel.metadata.tables[name].columns}
            assert migrated == model, name


def seed_legacy_head(conn) -> bool:
    tenant = insert(conn, "tenant")["id"]
    branch = insert(conn, "branch", tenant_id=tenant)["id"]
    for _ in range(2):  # the same order code twice: no unique index may reject legacy duplicates
        insert(conn, '"order"', tenant_id=tenant, branch_id=branch, order_code="DUP-1")
    return True


class TestDowngrade:
    def test_downgrade_drops_only_section_three_and_keeps_business_data(self, mdb):
        up(HOTFIX_REVISION)
        with mdb.begin() as conn:
            seed_legacy(conn)
            orders_before = snapshot(conn, '"order"')
        up()
        down(HOTFIX_REVISION)
        assert _current_revision(mdb) == HOTFIX_REVISION
        inspector = inspect(mdb)
        assert not set(TABLES) & set(inspector.get_table_names())
        assert not set(INDEXES) & {i["name"] for t in ("order", "sample") for i in inspector.get_indexes(t)}
        with mdb.connect() as conn:
            assert snapshot(conn, '"order"') == orders_before

    def test_upgrade_downgrade_upgrade_cycle(self, mdb):
        up()
        down(HOTFIX_REVISION)
        up()
        assert set(TABLES) <= set(inspect(mdb).get_table_names())


MIGRATIONS_RUNBOOK = pathlib.Path(
    os.environ.get("CELUMA_ENGINEERING_DIR") or pathlib.Path(__file__).resolve().parents[2] / "celuma-engineering"
) / "runbooks" / "migrations.md"


class TestRunbookSql:
    @pytest.mark.skipif(not MIGRATIONS_RUNBOOK.exists(), reason="celuma-engineering is not available")
    def test_numbering_checks_run_and_say_what_the_runbook_says(self, mdb):
        blocks = re.findall(r"```sql\n(.*?)```", MIGRATIONS_RUNBOOK.read_text(encoding="utf-8"), flags=re.S)
        sql = next(b for b in blocks if b.lstrip().startswith("-- post-upgrade, numbering objects"))
        up(HOTFIX_REVISION)
        with mdb.connect() as conn:  # on v1_3_1 the objects are absent
            assert tuple(conn.execute(text(sql)).one()) == (False, False, False)
        up()
        with mdb.connect() as conn:
            assert tuple(conn.execute(text(sql)).one()) == (True, True, True)
