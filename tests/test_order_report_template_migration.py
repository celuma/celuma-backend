"""The Céluma 1.4.1 hotfix revision: `order.report_template_id` on top of the released `v1_4_0`.

Real Alembic chain on a fresh, synthetic, disposable database (`celuma_order_template_mig_test`, dropped before
and after); never a developer, customer or shared database, never a stamp: every scenario upgrades from the
released revision explicitly with business data in place. Contract: the migration docstring and
`celuma-engineering/runbooks/migrations.md` (order report template, Céluma 1.4.1).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from tests.test_alembic_migrations import (
    HOTFIX_1_4_1_REVISION,
    RELEASE_1_4_REVISION,
    _admin_engine,
    _alembic,
    _current_revision,
    _script_directory,
)
from tests.test_sample_type_migration import insert

DB = "celuma_order_template_mig_test"


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


def up(target=HOTFIX_1_4_1_REVISION):
    _alembic(target, database=DB)


def down(target):
    _alembic(target, command="downgrade", database=DB)


def seed_1_4_0(conn) -> dict:
    """A 1.4.0 laboratory: templates linked to a study type AND to a sample type (the ambassador's case), two
    orders with typed and legacy samples."""
    tenant = insert(conn, "tenant")["id"]
    branch = insert(conn, "branch", tenant_id=tenant)["id"]
    template = insert(conn, "report_template", tenant_id=tenant, name="Biopsia", is_active=True)["id"]
    study = insert(conn, "study_type", tenant_id=tenant, code="BX", name="Biopsia", is_active=True,
                   default_report_template_id=template)["id"]
    sample_type = insert(conn, "sample_type", tenant_id=tenant, code="BX", name="Biopsia", is_active=True,
                         default_report_template_id=template)["id"]
    orders = [insert(conn, '"order"', tenant_id=tenant, branch_id=branch, order_code=f"BX-{n}",
                     study_type_id=study)["id"] for n in (1, 2)]
    insert(conn, "sample", tenant_id=tenant, branch_id=branch, order_id=orders[0], sample_code="S-1",
           sample_type_id=sample_type)
    insert(conn, "sample", tenant_id=tenant, branch_id=branch, order_id=orders[1], sample_code="S-2")
    return {"tenant": tenant, "template": template, "orders": orders}


def rows(conn, sql: str) -> list:
    return [tuple(r) for r in conn.execute(text(sql)).all()]


class TestUpgradeFromTheRelease:
    def test_business_data_is_preserved_and_every_order_has_no_explicit_choice(self, mdb):
        up(RELEASE_1_4_REVISION)
        with mdb.begin() as conn:
            seed = seed_1_4_0(conn)
            before = {t: rows(conn, f"SELECT * FROM {t} ORDER BY id") for t in
                      ('"order"', "sample", "study_type", "sample_type", "report_template")}
        up()
        assert _current_revision(mdb) == HOTFIX_1_4_1_REVISION
        with mdb.connect() as conn:
            orders = rows(conn, 'SELECT * FROM "order" ORDER BY id')
            width = len(before['"order"'][0])
            # The column is appended at the END and is NULL everywhere; every pre-existing value is identical.
            assert [r[:width] for r in orders] == before['"order"']
            assert all(r[width:] == (None,) for r in orders)
            for table in ("sample", "study_type", "sample_type", "report_template"):
                assert rows(conn, f"SELECT * FROM {table} ORDER BY id") == before[table], table
            assert conn.execute(text('SELECT count(*) FROM "order" WHERE report_template_id IS NOT NULL')).scalar() == 0
        assert len(seed["orders"]) == 2

    def test_the_reference_is_a_validated_foreign_key_with_an_index(self, mdb):
        up()
        inspector = inspect(mdb)
        fks = {fk["name"]: fk for fk in inspector.get_foreign_keys("order")}
        assert fks["fk_order_report_template"]["referred_table"] == "report_template"
        assert fks["fk_order_report_template"]["constrained_columns"] == ["report_template_id"]
        assert fks["fk_order_report_template"]["options"].get("ondelete") is None
        indexes = {ix["name"]: ix["column_names"] for ix in inspector.get_indexes("order")}
        assert indexes["ix_order_report_template_id"] == ["report_template_id"]
        with mdb.connect() as conn:
            assert conn.execute(text(
                "SELECT convalidated FROM pg_constraint WHERE conname = 'fk_order_report_template'")).scalar() is True
        column = next(c for c in inspector.get_columns("order") if c["name"] == "report_template_id")
        assert column["nullable"] is True and column["default"] is None

    def test_the_foreign_key_refuses_a_missing_template_and_protects_a_referenced_one(self, mdb):
        import uuid

        up(RELEASE_1_4_REVISION)
        with mdb.begin() as conn:
            seed = seed_1_4_0(conn)
        up()
        with pytest.raises(IntegrityError), mdb.begin() as conn:
            conn.execute(text('UPDATE "order" SET report_template_id = :t WHERE id = :o'),
                         {"t": uuid.uuid4(), "o": seed["orders"][0]})
        with mdb.begin() as conn:
            conn.execute(text('UPDATE "order" SET report_template_id = :t WHERE id = :o'),
                         {"t": seed["template"], "o": seed["orders"][0]})
        with pytest.raises(IntegrityError), mdb.begin() as conn:
            conn.execute(text("UPDATE study_type SET default_report_template_id = NULL"))
            conn.execute(text("UPDATE sample_type SET default_report_template_id = NULL"))
            conn.execute(text("DELETE FROM report_template WHERE id = :t"), {"t": seed["template"]})

    def test_downgrade_drops_only_the_new_objects_and_re_upgrade_works(self, mdb):
        up(RELEASE_1_4_REVISION)
        with mdb.begin() as conn:
            seed = seed_1_4_0(conn)
            before = rows(conn, 'SELECT * FROM "order" ORDER BY id')
        up()
        with mdb.begin() as conn:
            conn.execute(text('UPDATE "order" SET report_template_id = :t WHERE id = :o'),
                         {"t": seed["template"], "o": seed["orders"][0]})
        down(RELEASE_1_4_REVISION)
        assert _current_revision(mdb) == RELEASE_1_4_REVISION
        inspector = inspect(mdb)
        assert "report_template_id" not in {c["name"] for c in inspector.get_columns("order")}
        assert "ix_order_report_template_id" not in {ix["name"] for ix in inspector.get_indexes("order")}
        with mdb.connect() as conn:
            # Lossy for the explicit choice only (documented); every 1.4.0 value is back exactly.
            assert rows(conn, 'SELECT * FROM "order" ORDER BY id') == before
        up()
        assert _current_revision(mdb) == HOTFIX_1_4_1_REVISION

    def test_single_head(self):
        assert _script_directory().get_heads() == [HOTFIX_1_4_1_REVISION]
