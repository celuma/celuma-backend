"""The laboratory-content cleanup section (§7) of `v1_4_0` (Céluma 1.4, issue #39).

Real Alembic chain on a fresh, synthetic, disposable database (`celuma_cleanup_mig_test`, dropped before
and after); never a developer, customer or shared database, never a stamp: every scenario upgrades from
`v1_3_1` explicitly. Also pins the classification of EVERY table of the migrated schema, so a new table,
JSON column or storage reference cannot reach production without a decision about the cleanup.
Contract: the migration docstring (§7) and `celuma-engineering/specs/laboratory/laboratory-content-cleanup.md`.
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
from app.core.maintenance_guard import GUARDED_MODELS
from app.services import laboratory_cleanup_scope as scope
from sqlmodel import SQLModel
from tests.test_alembic_migrations import (
    HOTFIX_REVISION,
    RELEASE_1_4_REVISION,
    _admin_engine,
    _alembic,
    _current_revision,
)
from tests.test_sample_type_migration import insert

DB = "celuma_cleanup_mig_test"
TABLES = ("laboratory_cleanup_operation", "laboratory_cleanup_object", "laboratory_cleanup_retired_code")


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


def seed(conn) -> dict:
    """A 1.3.1 laboratory with an order, a sample and an invoice."""
    tenant = insert(conn, "tenant")["id"]
    branch = insert(conn, "branch", tenant_id=tenant)["id"]
    order = insert(conn, '"order"', tenant_id=tenant, branch_id=branch, order_code="CU-1")["id"]
    insert(conn, "sample", tenant_id=tenant, branch_id=branch, order_id=order, sample_code="S-1", type="BIOPSIA")
    user = insert(conn, "app_user", tenant_id=tenant)["id"]
    return {"tenant": tenant, "branch": branch, "order": order, "user": user}


def rows_text(conn, table: str) -> list:
    return conn.execute(text(f"SELECT t::text FROM {table} t ORDER BY 1")).scalars().all()


class TestUpgrade:
    def test_existing_data_is_untouched_and_the_new_tables_are_empty(self, mdb):
        up(HOTFIX_REVISION)
        key_columns = {
            '"order"': "id, tenant_id, branch_id, order_code, status, notes, report_id, invoice_id, created_at",
            "sample": "id, tenant_id, branch_id, order_id, sample_code",
            "tenant": "id, name, logo_storage_id, default_reviewer_id",
            "branch": "id, tenant_id, code",
            "app_user": "id, tenant_id, email, signature_storage_id",
        }

        def read(conn):
            return {t: conn.execute(text(f"SELECT {cols} FROM {t} ORDER BY id")).all() for t, cols in key_columns.items()}

        with mdb.begin() as conn:
            seed(conn)
            before = read(conn)
        up()
        assert _current_revision(mdb) == RELEASE_1_4_REVISION
        with mdb.connect() as conn:
            for name in TABLES:
                assert conn.execute(text(f"SELECT count(*) FROM {name}")).scalar_one() == 0, name
            # every existing laboratory has NO recorded creator: there is no reliable provenance, so it fails closed
            assert conn.execute(text("SELECT count(*) FROM tenant WHERE created_by_user_id IS NOT NULL")).scalar_one() == 0
            assert read(conn) == before

    def test_models_match_the_migrated_columns(self, mdb):
        up()
        inspector = inspect(mdb)
        for name in TABLES:
            migrated = {c["name"]: c["nullable"] for c in inspector.get_columns(name)}
            model = {c.name: c.nullable for c in SQLModel.metadata.tables[name].columns}
            assert migrated == model, name
        tenant_columns = {c["name"]: c["nullable"] for c in inspector.get_columns("tenant")}
        assert tenant_columns["created_by_user_id"] is True
        assert {c.name for c in SQLModel.metadata.tables["tenant"].columns} == set(tenant_columns)
        # the order table is untouched by this section: no tombstone marker exists any more
        assert "cleanup_operation_id" not in {c["name"] for c in inspector.get_columns("order")}

    def test_the_creator_binding_is_write_once_and_backfillable_exactly_once(self, mdb):
        up(HOTFIX_REVISION)
        with mdb.begin() as conn:
            ids = seed(conn)
            other = insert(conn, "app_user", tenant_id=ids["tenant"])["id"]
        up()
        set_creator = text("UPDATE tenant SET created_by_user_id = :u WHERE id = :t")
        with mdb.begin() as conn:   # the one allowed transition: NULL -> a user (registration, or the documented backfill)
            conn.execute(set_creator, {"u": ids["user"], "t": ids["tenant"]})
        with mdb.begin() as conn:   # idempotent re-assertion of the same value
            conn.execute(set_creator, {"u": ids["user"], "t": ids["tenant"]})
        for value in (other, None):
            with pytest.raises(Exception, match="write-once"):
                with mdb.begin() as conn:
                    conn.execute(set_creator, {"u": value, "t": ids["tenant"]})
        with mdb.connect() as conn:
            assert str(conn.execute(text("SELECT created_by_user_id FROM tenant WHERE id = :t"), {"t": ids["tenant"]}).scalar_one()) == str(ids["user"])
        with pytest.raises(IntegrityError):   # a real foreign key
            with mdb.begin() as conn:
                fresh = insert(conn, "tenant")["id"]
                conn.execute(text("UPDATE tenant SET created_by_user_id = gen_random_uuid() WHERE id = :t"), {"t": fresh})

    def test_constraints(self, mdb):
        up(HOTFIX_REVISION)
        with mdb.begin() as conn:
            ids = seed(conn)
        up()
        base = dict(tenant_id=ids["tenant"], actor_user_id=ids["user"], phase="QUIESCE", reason="r",
                    laboratory_name="n", scope_revision="a" * 64, confirmation_fingerprint="f", approved_counts="{}")
        insert_sql = (
            "INSERT INTO laboratory_cleanup_operation (id, tenant_id, actor_user_id, status, phase, reason, "
            "laboratory_name, scope_revision, confirmation_jti, confirmation_fingerprint, approved_counts, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :tenant_id, :actor_user_id, :status, :phase, :reason, :laboratory_name, "
            ":scope_revision, :jti, :confirmation_fingerprint, CAST(:approved_counts AS json), now(), now())"
        )
        with mdb.begin() as conn:
            conn.execute(text(insert_sql), {**base, "status": "COMPLETED", "jti": "j1"})
            conn.execute(text(insert_sql), {**base, "status": "COMPLETED", "jti": "j2"})  # many closed operations are fine
            conn.execute(text(insert_sql), {**base, "status": "FAILED", "jti": "j3"})       # one OPEN one is fine
        for params, why in (
            ({**base, "status": "QUEUED", "jti": "j4"}, "a second OPEN operation for the tenant"),
            ({**base, "status": "COMPLETED", "jti": "j1"}, "a reused confirmation id"),
            ({**base, "status": "BOGUS", "jti": "j5"}, "an unknown status"),
            ({**base, "status": "COMPLETED", "phase": "BOGUS", "jti": "j6"}, "an unknown phase"),
        ):
            with pytest.raises(IntegrityError, match=r"."):
                with mdb.begin() as conn:
                    conn.execute(text(insert_sql), params)


class TestDowngrade:
    def test_downgrade_drops_only_section_seven_and_keeps_business_data(self, mdb):
        up(HOTFIX_REVISION)
        with mdb.begin() as conn:
            seed(conn)
        up()
        with mdb.connect() as conn:
            orders = rows_text(conn, '"order"')
        down(HOTFIX_REVISION)
        assert _current_revision(mdb) == HOTFIX_REVISION
        inspector = inspect(mdb)
        assert not set(TABLES) & set(inspector.get_table_names())
        assert "created_by_user_id" not in {c["name"] for c in inspector.get_columns("tenant")}
        with mdb.connect() as conn:
            assert conn.execute(text('SELECT count(*) FROM "order"')).scalar_one() == 1
        up()
        assert set(TABLES) <= set(inspect(mdb).get_table_names())
        assert orders  # the cycle upgrade -> downgrade -> upgrade works

    def test_downgrade_is_refused_while_a_cleanup_operation_exists(self, mdb):
        up(HOTFIX_REVISION)
        with mdb.begin() as conn:
            ids = seed(conn)
        up()
        with mdb.begin() as conn:
            conn.execute(text(
                "INSERT INTO laboratory_cleanup_operation (id, tenant_id, actor_user_id, status, phase, reason, "
                "laboratory_name, scope_revision, confirmation_jti, confirmation_fingerprint, approved_counts, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :t, :u, 'COMPLETED', 'DONE', 'r', 'n', 'a', 'j', 'f', '{}', now(), now())"),
                {"t": ids["tenant"], "u": ids["user"]})
        with pytest.raises(Exception) as exc:
            down(HOTFIX_REVISION)
        assert "Downgrade refused" in (exc.value.stderr or "") + (exc.value.stdout or "")
        assert _current_revision(mdb) == RELEASE_1_4_REVISION  # nothing was changed
        assert set(TABLES) <= set(inspect(mdb).get_table_names())


MIGRATIONS_RUNBOOK = pathlib.Path(
    os.environ.get("CELUMA_ENGINEERING_DIR") or pathlib.Path(__file__).resolve().parents[2] / "celuma-engineering"
) / "runbooks" / "migrations.md"


class TestRunbookSql:
    @pytest.mark.skipif(not MIGRATIONS_RUNBOOK.exists(), reason="celuma-engineering is not available")
    def test_cleanup_object_check_runs_and_says_what_the_runbook_says(self, mdb):
        blocks = re.findall(r"```sql\n(.*?)```", MIGRATIONS_RUNBOOK.read_text(encoding="utf-8"), flags=re.S)
        sql = next(b for b in blocks if b.lstrip().startswith("-- post-upgrade, laboratory cleanup objects"))
        up(HOTFIX_REVISION)
        with mdb.connect() as conn:  # on v1_3_1 the objects are absent
            assert tuple(conn.execute(text(sql)).one()) == (False, False, False, False)
        up()
        with mdb.connect() as conn:
            assert tuple(conn.execute(text(sql)).one()) == (True, True, True, True)


# --------------------------------------------------------------------------- classification of the whole schema

#: Every table of the migrated schema and what the cleanup does with it (the spec's dependency matrix).
MATRIX = {
    # DELETE (scoped graph)
    "order": "DELETE/RETAIN", "sample": "DELETE", "sample_image": "DELETE", "sample_image_rendition": "DELETE",
    "sample_labels": "DELETE", "order_labels": "DELETE", "order_comment": "DELETE", "order_comment_mention": "DELETE",
    "order_event": "DELETE", "order_requesting_physician": "DELETE", "report": "DELETE", "report_version": "DELETE",
    "report_amendment": "DELETE", "report_review": "DELETE", "assignment": "DELETE", "notification": "DELETE",
    "notification_recipient": "DELETE", "notification_delivery": "DELETE", "invoice": "DELETE/RETAIN",
    "invoice_item": "DELETE/RETAIN", "payment": "DELETE/RETAIN",
    # storage rows: resolved from the graph, exclusively owned only
    "storage_object": "DELETE/PRESERVE",
    # RETAIN-AS-EVIDENCE
    "audit_log": "RETAIN", "idempotency_receipt": "RETAIN", "numbering_allocation": "PRESERVE",
    "sample_code_correction": "PRESERVE", "laboratory_cleanup_operation": "RETAIN", "laboratory_cleanup_object": "RETAIN",
    "laboratory_cleanup_retired_code": "RETAIN",
    # PRESERVE
    "tenant": "PRESERVE", "branch": "PRESERVE", "app_user": "PRESERVE", "user_branch": "PRESERVE", "user_role": "PRESERVE",
    "role": "PRESERVE", "role_permission": "PRESERVE", "permission": "PRESERVE", "user_invitation": "PRESERVE",
    "password_reset_token": "PRESERVE", "blacklisted_token": "PRESERVE", "patient": "PRESERVE",
    "requesting_physician": "PRESERVE", "study_type": "PRESERVE", "sample_type": "PRESERVE", "price_catalog": "PRESERVE",
    "label": "PRESERVE", "report_template": "PRESERVE", "report_template_version": "PRESERVE",
    "report_letterhead": "PRESERVE", "report_letterhead_version": "PRESERVE", "report_section": "PRESERVE",
    "numbering_config": "PRESERVE", "numbering_counter": "PRESERVE", "notification_preference": "PRESERVE",
    "tenant_limits": "PRESERVE", "tenant_usage_reconciliation": "PRESERVE", "alembic_version": "PRESERVE",
    # RECONCILE
    "tenant_usage": "RECONCILE", "tenant_usage_threshold_state": "RECONCILE",
}

#: JSON columns and why none of them can silently hold a clinical copy or an unprotected object reference.
JSON_COLUMNS = {
    ("audit_log", "old_values"): "redacted for scoped entities", ("audit_log", "new_values"): "redacted for scoped entities",
    ("audit_log", "event_metadata"): "redacted for scoped entities",
    ("idempotency_receipt", "result"): "ids and codes only", ("idempotency_receipt", "resources"): "ids only",
    ("laboratory_cleanup_operation", "approved_counts"): "numbers only", ("laboratory_cleanup_operation", "executed_counts"): "numbers only",
    ("notification", "notification_metadata"): "deleted with scoped notifications",
    ("order_comment", "comment_metadata"): "deleted with the order", ("order_event", "event_metadata"): "deleted with the order",
    ("report", "template"): "PRESERVED for other laboratories; scanned for object references", ("report_amendment", "reviews_at_opening"): "deleted with the report",
    ("report_letterhead_version", "configuration"): "PRESERVED; scanned for object references",
    ("report_template", "template_json"): "PRESERVED; scanned for object references",
    ("report_template_version", "configuration"): "PRESERVED; scanned for object references",
}

#: Foreign keys to storage_object: the three scoped referencers are resolved by the cleanup; the rest PROTECT the object.
STORAGE_REFERENCERS = {
    ("sample_image", "storage_id"), ("sample_image_rendition", "storage_id"),
    ("report_version", "pdf_storage_id"), ("report_version", "json_storage_id"), ("report_version", "html_storage_id"),
    ("app_user", "signature_storage_id"), ("tenant", "logo_storage_id"),
}


class TestEverySchemaObjectIsClassified:
    def test_every_table_is_in_the_dependency_matrix(self, mdb):
        up()
        tables = set(inspect(mdb).get_table_names())
        assert tables == set(MATRIX), {"unclassified": tables - set(MATRIX), "stale": set(MATRIX) - tables}

    def test_every_json_column_is_accounted_for(self, mdb):
        up()
        with mdb.connect() as conn:
            found = {(t, c) for t, c in conn.execute(text(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                "AND data_type IN ('json','jsonb')"))}
        assert found == set(JSON_COLUMNS), {"unclassified": found - set(JSON_COLUMNS), "stale": set(JSON_COLUMNS) - found}
        # the preserved configuration columns are exactly the ones the object-reference scan reads
        assert {(t, c) for t, c, _ in scope.CONFIG_JSON_COLUMNS} == {k for k, v in JSON_COLUMNS.items() if v.startswith("PRESERVED")}

    def test_every_storage_reference_is_known(self, mdb):
        up()
        assert set(scope._storage_references_from_metadata()) == STORAGE_REFERENCERS
        with mdb.connect() as conn:
            live = {(t, c) for t, c in conn.execute(text(
                "SELECT conrelid::regclass::text, a.attname FROM pg_constraint k "
                "JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY(k.conkey) "
                "WHERE k.contype = 'f' AND k.confrelid = 'storage_object'::regclass"))}
        assert live == STORAGE_REFERENCERS

    def test_every_table_that_references_the_scope_roots_is_deleted_or_classified(self, mdb):
        """A new table with a foreign key into order/sample/report/invoice/... must be a DELETE-scope table or be
        consciously retained; otherwise the cleanup's DELETE would fail (or worse, leave an orphan)."""
        up()
        roots = ("order", "sample", "report", "report_version", "report_amendment", "invoice", "sample_image",
                 "order_comment", "notification")
        with mdb.connect() as conn:
            referencing = {r for (r,) in conn.execute(text(
                "SELECT DISTINCT conrelid::regclass::text FROM pg_constraint WHERE contype = 'f' AND confrelid::regclass::text = ANY(:roots)"),
                {"roots": [f'"{r}"' if r == "order" else r for r in roots]})}
        referencing = {r.strip('"') for r in referencing}
        deleted = {st.table.strip('"') for st in scope.CORE_TABLES + scope.FINANCE_TABLES}
        evidence = {"audit_log"}  # FK columns are nulled and the payload redacted
        assert referencing - deleted - evidence == set(), referencing - deleted - evidence

    def test_the_gate_guards_every_deleted_operational_table_that_writers_touch(self):
        deleted = {st.table.strip('"') for st in scope.CORE_TABLES + scope.FINANCE_TABLES}
        guarded = {m.__tablename__ for m in GUARDED_MODELS}
        assert deleted - guarded == {"notification", "notification_recipient", "notification_delivery"}
        assert guarded - deleted == set()
