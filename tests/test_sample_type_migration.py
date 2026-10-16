"""The sample-type catalog section of `v1_4_0` (Céluma 1.4, issue #17).

Runs the real Alembic chain on a fresh, synthetic, disposable database
(`celuma_sample_type_mig_test`, dropped before and after). It never touches a
developer, customer or shared database, and it never relies on an
`alembic_version` stamp: every scenario upgrades from `v1_3_1` explicitly.

Contract: the migration docstring (§2) and
`celuma-engineering/runbooks/migrations.md`, "Sample-type catalog".
"""
from __future__ import annotations

import os
import pathlib
import re
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url

from app.core.config import settings
from tests.test_alembic_migrations import (
    HOTFIX_REVISION,
    RELEASE_1_4_REVISION,
    _admin_engine,
    _alembic,
    _current_revision,
)

DB = "celuma_sample_type_mig_test"
# The identifiers come from the chain-integrity module: only that module may spell
# the reused `v1_4_0` id (see TestTheReusedV140Identifier).
PRE = HOTFIX_REVISION
HEAD = RELEASE_1_4_REVISION
ENUM_VALUES = ["SANGRE", "BIOPSIA", "LAMINILLA", "TEJIDO", "OTRO"]


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


def up(target=HEAD):
    _alembic(target, database=DB)


def down(target):
    _alembic(target, command="downgrade", database=DB)


def insert(conn, table: str, **given):
    """Insert one synthetic row, filling every other NOT NULL column without a
    default from its type — so the test does not depend on a hand-copied
    column list of a frozen schema."""
    cols = conn.execute(text(
        "SELECT column_name, data_type, udt_name, character_maximum_length FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=:t AND is_nullable='NO' AND column_default IS NULL"
    ), {"t": table.strip('"')}).all()
    values = dict(given)
    for name, data_type, udt, length in cols:
        if name in values:
            continue
        if data_type == "uuid":
            values[name] = uuid.uuid4()
        elif data_type in ("character varying", "text"):
            values[name] = f"x-{uuid.uuid4().hex[:8]}"[: length or 99]
        elif data_type.startswith("timestamp"):
            values[name] = datetime(2026, 1, 5, 9, 0, 0)
        elif data_type == "boolean":
            values[name] = False
        elif data_type in ("integer", "bigint", "numeric", "smallint"):
            values[name] = 0
        elif data_type == "date":
            values[name] = datetime(2026, 1, 5).date()
        elif data_type in ("json", "jsonb"):
            values[name] = "{}"
        elif data_type == "USER-DEFINED":
            values[name] = conn.execute(text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid "
                "WHERE t.typname=:u ORDER BY e.enumsortorder LIMIT 1"), {"u": udt}).scalar_one()
        else:  # pragma: no cover - a new NOT NULL type needs a deliberate value
            raise AssertionError(f"no synthetic value for {table}.{name} ({data_type})")
    quoted = ", ".join(f'"{c}"' for c in values)
    conn.execute(text(f'INSERT INTO {table} ({quoted}) VALUES ({", ".join(":" + c for c in values)})'), values)
    return values


def seed_1_3_1(conn) -> dict:
    """A tiny 1.3.1 laboratory: enum-typed samples, labels, notes, an order."""
    tenant = insert(conn, "tenant")["id"]
    branch = insert(conn, "branch", tenant_id=tenant)["id"]
    order = insert(conn, '"order"', tenant_id=tenant, branch_id=branch, order_code="ORD-1")["id"]
    label = insert(conn, "label", tenant_id=tenant, name="Urgente")["id"]
    samples = {}
    for code, enum_value in zip(("S-1", "S-2", "S-3", "S-4", "S-5"), ENUM_VALUES):
        row = insert(conn, "sample", tenant_id=tenant, branch_id=branch, order_id=order, sample_code=code,
                     type=enum_value, state="PROCESSING" if code == "S-2" else "RECEIVED", notes=f"nota {code}")
        samples[code] = row["id"]
    conn.execute(text("INSERT INTO sample_labels (sample_id, label_id) VALUES (:s, :l)"),
                 {"s": samples["S-1"], "l": label})
    return {"tenant": tenant, "branch": branch, "order": order, "label": label, "samples": samples}


def sample_snapshot(conn):
    return conn.execute(text(
        "SELECT id, tenant_id, branch_id, order_id, sample_code, state::text, notes, collected_at, received_at "
        "FROM sample ORDER BY sample_code")).all()


class TestUpgrade:
    def test_legacy_samples_become_untyped_and_nothing_else_changes(self, mdb):
        up(PRE)
        assert _current_revision(mdb) == PRE
        with mdb.begin() as conn:
            seeded = seed_1_3_1(conn)
            before = sample_snapshot(conn)
            assert conn.execute(text("SELECT DISTINCT type::text FROM sample ORDER BY 1")).scalars().all() == sorted(ENUM_VALUES)

        up()
        assert _current_revision(mdb) == HEAD
        with mdb.connect() as conn:
            assert sample_snapshot(conn) == before  # identity, state, notes, order links: identical
            assert conn.execute(text("SELECT count(*) FROM sample WHERE sample_type_id IS NULL")).scalar_one() == 5
            # the enum values are NOT seeded as catalog entries
            assert conn.execute(text("SELECT count(*) FROM sample_type")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM sample_labels")).scalar_one() == 1
            assert conn.execute(text('SELECT count(*) FROM "order" WHERE id = :o'), {"o": seeded["order"]}).scalar_one() == 1
        columns = {c["name"] for c in inspect(mdb).get_columns("sample")}
        assert "type" not in columns and "sample_type_id" in columns
        with mdb.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM pg_type WHERE typname = 'sampletype'")).scalar_one() == 0

    def test_catalog_table_parity_and_constraints(self, mdb):
        up()
        inspector = inspect(mdb)
        study = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns("study_type")}
        sample = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns("sample_type")}
        assert sample == study  # exact persisted-field parity with StudyType
        uniques = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("sample_type")}
        assert uniques == {"uq_sample_type_tenant_code": ["tenant_id", "code"]}
        fks = {fk["constrained_columns"][0]: fk["referred_table"] for fk in inspector.get_foreign_keys("sample_type")}
        assert fks == {"tenant_id": "tenant", "default_report_template_id": "report_template"}
        sample_fk = [fk for fk in inspector.get_foreign_keys("sample") if fk["constrained_columns"] == ["sample_type_id"]]
        assert sample_fk[0]["referred_table"] == "sample_type"
        assert sample_fk[0]["options"].get("ondelete") == "RESTRICT"
        assert {i["name"] for i in inspector.get_indexes("sample")} >= {"ix_sample_sample_type_id"}

    def test_the_new_nullable_column_does_not_let_a_referenced_type_be_deleted(self, mdb):
        up()
        with mdb.begin() as conn:
            seeded = seed_1_3_1_head(conn)
        with pytest.raises(Exception, match="foreign key|violates"):
            with mdb.begin() as conn:
                conn.execute(text("DELETE FROM sample_type WHERE id = :i"), {"i": seeded["type"]})


def seed_1_3_1_head(conn) -> dict:
    tenant = insert(conn, "tenant")["id"]
    branch = insert(conn, "branch", tenant_id=tenant)["id"]
    order = insert(conn, '"order"', tenant_id=tenant, branch_id=branch, order_code="ORD-1")["id"]
    stype = insert(conn, "sample_type", tenant_id=tenant, code="BIO", name="Biopsia", is_active=True)["id"]
    insert(conn, "sample", tenant_id=tenant, branch_id=branch, order_id=order, sample_code="S-1", sample_type_id=stype)
    return {"tenant": tenant, "type": stype}


class TestDowngrade:
    def test_downgrade_is_lossy_by_design_and_documented(self, mdb):
        """Every sample returns as OTRO: the retired enum assignments are gone
        for good and cannot be inferred from NULL."""
        up(PRE)
        with mdb.begin() as conn:
            seed_1_3_1(conn)
            before = sample_snapshot(conn)
        up()
        with mdb.begin() as conn:  # a catalog assignment made after the upgrade
            conn.execute(text("UPDATE sample SET sample_type_id = NULL"))
            tenant = conn.execute(text("SELECT tenant_id FROM sample LIMIT 1")).scalar_one()
            stype = insert(conn, "sample_type", tenant_id=tenant, code="BIO", name="Biopsia", is_active=True)["id"]
            conn.execute(text("UPDATE sample SET sample_type_id = :t WHERE sample_code = 'S-1'"), {"t": stype})

        down(PRE)
        assert _current_revision(mdb) == PRE
        inspector = inspect(mdb)
        assert "sample_type" not in inspector.get_table_names()
        columns = {c["name"]: c for c in inspector.get_columns("sample")}
        assert "sample_type_id" not in columns and columns["type"]["nullable"] is False
        with mdb.connect() as conn:
            assert conn.execute(text("SELECT DISTINCT type::text FROM sample")).scalars().all() == ["OTRO"]
            assert conn.execute(text("SELECT enum_range(NULL::sampletype)::text")).scalar_one() == "{SANGRE,BIOPSIA,LAMINILLA,TEJIDO,OTRO}"
            assert sample_snapshot(conn) == before  # nothing else about the samples moved

    def test_upgrade_downgrade_upgrade_cycle_leaves_a_clean_schema(self, mdb):
        up(PRE)
        with mdb.begin() as conn:
            seed_1_3_1(conn)
        up()
        down(PRE)
        up()
        assert _current_revision(mdb) == HEAD
        with mdb.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM sample WHERE sample_type_id IS NULL")).scalar_one() == 5
            assert conn.execute(text("SELECT count(*) FROM sample_type")).scalar_one() == 0
        down(PRE)
        assert "sample_type" not in inspect(mdb).get_table_names()

# ---------------------------------------------------------------------------
# The operator runbook's SQL, executed
# ---------------------------------------------------------------------------

MIGRATIONS_RUNBOOK = pathlib.Path(
    os.environ.get("CELUMA_ENGINEERING_DIR") or pathlib.Path(__file__).resolve().parents[2] / "celuma-engineering"
) / "runbooks" / "migrations.md"


def runbook_sql(marker: str) -> str:
    text_ = MIGRATIONS_RUNBOOK.read_text(encoding="utf-8")
    for block in re.findall(r"```sql\n(.*?)```", text_, flags=re.S):
        if block.lstrip().startswith(f"-- {marker}"):
            return block
    raise AssertionError(f"no sql block marked '{marker}' in the migrations runbook")


class TestRunbookSql:
    """The read-only SQL of `celuma-engineering/runbooks/migrations.md`
    ("Sample-type catalog"), executed. Skipped when that repository is not
    checked out beside the backend (or at `CELUMA_ENGINEERING_DIR`)."""

    @pytest.mark.skipif(not MIGRATIONS_RUNBOOK.exists(), reason="celuma-engineering is not available")
    def test_pre_and_post_upgrade_queries_run_and_say_what_the_runbook_says(self, mdb):
        migrate = up
        migrate(PRE)
        with mdb.begin() as conn:
            seed_1_3_1(conn)
            pre = conn.execute(text(runbook_sql("pre-upgrade (revision v1_3_1)"))).all()
        assert dict(pre) == {"SANGRE": 1, "BIOPSIA": 1, "LAMINILLA": 1, "TEJIDO": 1, "OTRO": 1}
        total = sum(n for _, n in pre)

        migrate()
        with mdb.connect() as conn:
            objects = conn.execute(text(runbook_sql("post-upgrade, objects"))).one()
            counts = conn.execute(text(runbook_sql("post-upgrade, reconciliation"))).one()
        assert tuple(objects) == (True, True, True)
        assert tuple(counts) == (total, total, 0)
