"""Alembic chain integrity tests (Céluma 1.3, Phase 2 and Phase 3 closures).

Céluma 1.3 was developed as ten revisions on the `celuma-1.3` branch and
ships as one. Two squashes got it there, and both folded their work into the
same contractual revision, `v1_3_0`:

  - **Phase 2 closure** folded in `v1_3_0 … v1_9_0` (Blocks A–E plus five
    post-Phase-2 remediation rounds) — see
    docs/celuma-1.3/phase-2-closure/alembic-squash-inventory.md.
  - **Phase 3 closure** folded in the notification chain that reused three of
    the identifiers Phase 2 had freed, `v1_4_0 → v1_5_0 → v1_6_0` (Blocks B,
    D and F) — see
    docs/celuma-1.3/phase-3-closure/phase-3-alembic-squash-inventory.md.

Development history and release history are therefore different, and the
per-block documents under docs/celuma-1.3/ record the former on purpose:

    development history:  v1_3_0 → v1_4_0 → v1_5_0 → v1_6_0
    release history:      v1_3_0 only

These tests are the regression net for both decisions:

  - the static ones guarantee the chain stays single-headed and linear, that
    the head is the release revision, and that no superseded 1.3 revision id
    can creep back into executable code;
  - the DB-backed ones guarantee the release migration still upgrades a clean
    pre-1.3 database, downgrades without residue — including from a
    *populated* database — and re-upgrades.

`TestNotificationDomain` replaces the three per-revision classes Phase 3
Blocks B, D and F each added (`TestNotificationsRevision`,
`TestDeliveryLifecycleRevision`, `TestNotificationLocaleRevision`). Their
assertions are kept, not dropped; what changed is that they now describe a
single revision's finished state rather than three successive ones, because
the intermediate states no longer exist to assert against. Two of them became
anti-assertions: the address-keyed delivery constraint and the locale-less
`notification` table are states the release migration must never pass
through.

The DB-backed tests use the same ephemeral-Postgres pattern as
tests/http/conftest.py: a database that is always dropped and recreated by
name, never the tenant's real `celumadb`.
"""
import ast
import io
import os
import pathlib
import subprocess
import tokenize
import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url

from app.core.config import settings


BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSIONS_DIR = BACKEND_ROOT / "alembic" / "versions"

#: The last revision belonging to the release *before* Céluma 1.3 — the
#: revision `main` and tag v1.2.0 carry.
LAST_PRE_1_3_REVISION = "v1_2_0"

#: The single consolidated Céluma 1.3 release revision, and the head.
RELEASE_REVISION = "v1_3_0"

#: Revision ids that existed only on the unreleased `celuma-1.3` branch and
#: were folded into RELEASE_REVISION. Nothing executable may reference them.
#:
#: The tuple has grown and shrunk with the development history and is now
#: closed. Phase 2 closure retired `v1_4_0 … v1_9_0`; Phase 3 Blocks B, D and
#: F each removed one entry when they reused a freed id for a live revision
#: (`v1_4_0`, `v1_5_0`, `v1_6_0`); the Phase 3 closure squash put all three
#: back, permanently, because those revisions are now folded into
#: RELEASE_REVISION as well. Every id here belonged to a revision that never
#: reached production, staging or a customer database, so no `alembic_version`
#: row anywhere carries one.
SUPERSEDED_REVISIONS = (
    "v1_4_0",
    "v1_5_0",
    "v1_6_0",
    "v1_7_0",
    "v1_8_0",
    "v1_9_0",
)

#: Every table the release migration introduces on top of `v1_2_0` — the
#: Phase 2 objects and the Phase 3 notification domain together.
RELEASE_TABLES = {
    "report_template_version",
    "report_letterhead",
    "report_letterhead_version",
    "notification",
    "notification_recipient",
    "notification_delivery",
    "notification_preference",
}

#: The four notification-domain tables, absorbed from the Phase 3 chain.
NOTIFICATION_TABLES = {
    "notification",
    "notification_recipient",
    "notification_delivery",
    "notification_preference",
}

#: The delivery uniqueness constraint Phase 3 Block B created and Block D
#: dropped. The consolidated migration must never create it: it assumed one
#: address belongs to one person, which silently denied email to the second
#: user sharing a mailbox.
SUPERSEDED_DELIVERY_CONSTRAINT = "uq_notification_delivery_notification_channel_address"

_MIGRATION_TEST_DB = "celuma_migration_test"


def _executable_source(path: pathlib.Path) -> str:
    """Return a module's source stripped of its module docstring and comments.

    The release migration documents its own provenance in prose — the module
    docstring names the revisions it consolidates, and each section of
    `upgrade()` carries an `ex-v1_x_0` comment so a reader can trace any DDL
    statement back to the block that introduced it. Both are inert. What must
    never come back is a revision id that some code path actually resolves,
    stamps, or branches on, so the search runs against code only.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - not our concern here
        return source

    if ast.get_docstring(tree, clean=False) is not None:
        lines = source.splitlines(keepends=True)
        source = "".join(lines[tree.body[0].end_lineno :])

    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return "\n".join(
        token.string for token in tokens if token.type != tokenize.COMMENT
    )


def _script_directory() -> ScriptDirectory:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


class TestChainShape:
    """Static assertions — no database required."""

    def test_exactly_one_head(self):
        assert len(_script_directory().get_heads()) == 1

    def test_head_is_the_release_revision(self):
        """Phase 3 closure: the head moved back from the notification-locale
        revision to the release revision that now contains it."""
        assert _script_directory().get_current_head() == RELEASE_REVISION

    def test_release_revision_sits_directly_on_the_last_pre_1_3_revision(self):
        revision = _script_directory().get_revision(RELEASE_REVISION)
        assert revision.down_revision == LAST_PRE_1_3_REVISION

    def test_chain_is_linear_from_base_to_head(self):
        script = _script_directory()
        revisions = list(script.walk_revisions())
        assert [r.revision for r in revisions] == [
            RELEASE_REVISION,
            "v1_2_0",
            "v1_1_0",
            "v1_0_0",
        ]

    def test_no_merge_revision_exists(self):
        """A squash, not a merge: no revision may have more than one parent."""
        for revision in _script_directory().walk_revisions():
            parents = revision.down_revision
            assert not isinstance(parents, (tuple, list)) or len(parents) <= 1, (
                f"{revision.revision} is a merge revision"
            )

    def test_no_superseded_revision_file_remains_in_the_versions_directory(self):
        script = _script_directory()
        known = {r.revision for r in script.walk_revisions()}
        assert known.isdisjoint(SUPERSEDED_REVISIONS)

    @pytest.mark.parametrize("stale", SUPERSEDED_REVISIONS)
    def test_no_superseded_revision_file_is_on_disk(self, stale):
        """Not merely absent from the chain — absent from the directory, so a
        stray file cannot be revived by editing one `down_revision`."""
        offenders = [
            path.name
            for path in VERSIONS_DIR.glob("*.py")
            if path.name.startswith(f"{stale}_")
        ]
        assert offenders == []

    @pytest.mark.parametrize("stale", SUPERSEDED_REVISIONS)
    def test_no_executable_code_references_a_superseded_revision(self, stale):
        """Documentation intentionally keeps the historical ids as a record
        of how the release was built — docs/celuma-1.3/, and the release
        migration's own module docstring, which names the revisions it
        consolidates. Executable code must not reference them: nothing may
        resolve, stamp, or branch on a revision that no longer exists.
        """
        offenders = []
        for path in list((BACKEND_ROOT / "app").rglob("*.py")) + list(
            (BACKEND_ROOT / "tests").rglob("*.py")
        ) + list(VERSIONS_DIR.rglob("*.py")):
            if path == pathlib.Path(__file__):
                continue
            if stale in _executable_source(path):
                offenders.append(str(path.relative_to(BACKEND_ROOT)))
        assert offenders == []


def _admin_engine():
    return create_engine(
        make_url(settings.database_url).set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )


def _alembic(target: str, *, command: str = "upgrade") -> None:
    url = make_url(settings.database_url).set(database=_MIGRATION_TEST_DB)
    subprocess.run(
        ["alembic", command, target],
        cwd=str(BACKEND_ROOT),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
    )


@pytest.fixture(name="migration_db")
def migration_db_fixture():
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(
                text(f'DROP DATABASE IF EXISTS "{_MIGRATION_TEST_DB}" WITH (FORCE)')
            )
            conn.execute(text(f'CREATE DATABASE "{_MIGRATION_TEST_DB}"'))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"Postgres not reachable for migration tests: {exc}")
    finally:
        admin.dispose()

    engine = create_engine(make_url(settings.database_url).set(database=_MIGRATION_TEST_DB))
    yield engine
    engine.dispose()

    admin = _admin_engine()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{_MIGRATION_TEST_DB}" WITH (FORCE)'))
    admin.dispose()


def _current_revision(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


class TestReleaseMigration:
    """Path A and Path B of the closure verification matrix."""

    def test_clean_upgrade_from_last_pre_1_3_revision(self, migration_db):
        _alembic(LAST_PRE_1_3_REVISION)
        assert _current_revision(migration_db) == LAST_PRE_1_3_REVISION

        _alembic("head")
        assert _current_revision(migration_db) == RELEASE_REVISION

    def test_downgrade_then_re_upgrade(self, migration_db):
        _alembic("head")
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
        assert _current_revision(migration_db) == LAST_PRE_1_3_REVISION

        _alembic("head")
        assert _current_revision(migration_db) == RELEASE_REVISION

    def test_release_introduces_exactly_the_expected_tables(self, migration_db):
        """The release migration's table footprint, pinned. Replaces Block B's
        `test_notifications_revision_modifies_no_existing_table`, which could
        only be expressed while the notification tables arrived in a separate
        revision: there is no longer an intermediate state to diff against, so
        the guard moves to the `v1_2_0 → v1_3_0` boundary instead."""
        _alembic(LAST_PRE_1_3_REVISION)
        before = set(inspect(migration_db).get_table_names())

        _alembic("head")
        after = set(inspect(migration_db).get_table_names())

        assert after - before == RELEASE_TABLES
        assert before - after == set()

    def test_downgrade_removes_every_object_the_release_introduced(self, migration_db):
        _alembic("head")
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")

        inspector = inspect(migration_db)
        tables = set(inspector.get_table_names())
        assert RELEASE_TABLES.isdisjoint(tables)

        report_version_columns = {c["name"] for c in inspector.get_columns("report_version")}
        assert report_version_columns.isdisjoint(
            {
                "schema_version",
                "template_version_id",
                "generated_by_renderer_version",
                "pdf_generation_status",
                "pdf_sha256",
                "letterhead_version_id",
                "publish_started_at",
                "publish_started_by",
            }
        )
        tenant_columns = {c["name"] for c in inspector.get_columns("tenant")}
        assert "reports_v2_enabled" not in tenant_columns

    def test_downgrade_works_on_a_populated_database(self, migration_db):
        """The notification tables now originate inside the release migration,
        so its downgrade has to drop tables that hold rows and foreign keys —
        not merely empty ones. Dependency order is what makes that possible:
        `notification_recipient` and `notification_delivery` both reference
        `notification`, so `notification` goes last of the four.
        """
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id, notification_id, user_a, user_b = _seed_delivery_context(conn)
            _insert_recipient(conn, tenant_id=tenant_id, notification_id=notification_id,
                              user_id=user_a)
            # Two users sharing one mailbox — the shape the final uniqueness
            # model exists to allow, and the one most likely to obstruct a drop.
            _insert_delivery(conn, tenant_id=tenant_id, notification_id=notification_id,
                             recipient_user_id=user_a, address="shared@lab.test")
            _insert_delivery(conn, tenant_id=tenant_id, notification_id=notification_id,
                             recipient_user_id=user_b, address="shared@lab.test")
            _insert_preference(conn, tenant_id=tenant_id, user_id=user_a)

        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")

        assert _current_revision(migration_db) == LAST_PRE_1_3_REVISION
        assert RELEASE_TABLES.isdisjoint(set(inspect(migration_db).get_table_names()))

    def test_upgraded_schema_matches_the_models(self, migration_db):
        """Every table and column the SQLModel metadata declares must exist
        in the database the release migration produces."""
        import app.models  # noqa: F401  registers every table
        from app.models.base import BaseModel

        _alembic("head")
        inspector = inspect(migration_db)
        actual_tables = set(inspector.get_table_names())

        for table in BaseModel.metadata.sorted_tables:
            assert table.name in actual_tables, f"missing table {table.name}"
            actual_columns = {c["name"] for c in inspector.get_columns(table.name)}
            declared = {c.name for c in table.columns}
            assert declared <= actual_columns, (
                f"{table.name} missing columns: {sorted(declared - actual_columns)}"
            )

    def test_release_migration_creates_the_partial_unique_indexes(self, migration_db):
        _alembic("head")
        with migration_db.connect() as conn:
            predicates = dict(
                conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname = 'public' AND indexname IN "
                        "('ix_report_template_version_one_active', "
                        " 'ix_report_letterhead_version_one_active', "
                        " 'ix_report_letterhead_one_default')"
                    )
                ).all()
            )

        assert set(predicates) == {
            "ix_report_template_version_one_active",
            "ix_report_letterhead_version_one_active",
            "ix_report_letterhead_one_default",
        }
        assert "WHERE ((status)::text = 'ACTIVE'::text)" in (
            predicates["ix_report_template_version_one_active"]
        )
        assert "WHERE ((status)::text = 'ACTIVE'::text)" in (
            predicates["ix_report_letterhead_version_one_active"]
        )
        assert "WHERE (is_default = true)" in predicates["ix_report_letterhead_one_default"]

    def test_release_migration_performs_no_backfill_on_nullable_columns(self, migration_db):
        """The compatibility decisions the remediation rounds made must
        survive the squash: consolidating migrations into one is not a
        licence to populate columns that were deliberately left empty."""
        _alembic("head")
        inspector = inspect(migration_db)

        nullable_by_design = {
            "report_version": [
                "schema_version",
                "template_version_id",
                "generated_by_renderer_version",
                "pdf_generation_status",
                "pdf_sha256",
                "pdf_page_count",
                "pdf_size_bytes",
                "letterhead_version_id",
                "publish_started_at",
                "publish_started_by",
            ],
            "report_template": [
                "preferred_letterhead_version_id",
                "preferred_letterhead_id",
            ],
            "storage_object": ["tenant_id"],
        }
        for table, columns in nullable_by_design.items():
            actual = {c["name"]: c for c in inspector.get_columns(table)}
            for column in columns:
                assert actual[column]["nullable"] is True, (
                    f"{table}.{column} must stay nullable for historical rows"
                )

        # The single exception: the tenant flag is NOT NULL, backfilled to
        # false by its own server_default inside the same ALTER TABLE.
        tenant_columns = {c["name"]: c for c in inspector.get_columns("tenant")}
        assert tenant_columns["reports_v2_enabled"]["nullable"] is False
        assert "false" in str(tenant_columns["reports_v2_enabled"]["default"]).lower()


class TestNotificationDomain:
    """The notification domain, as the release migration now delivers it.

    Carries forward every assertion the three Phase 3 revision classes made,
    retargeted from their own revisions to the single head. The chain tests
    above prove the revision is reachable and reversible; these prove it
    created the *right* objects — the constraints and indexes the domain's
    correctness actually rests on, rather than four tables with the right
    names — and, for delivery uniqueness, the behaviour those objects exist
    for.
    """

    USER_INDEX = "uq_notification_delivery_recipient_user"
    ADDRESS_INDEX = "uq_notification_delivery_recipient_address"

    def _index_map(self, migration_db, table):
        with migration_db.connect() as conn:
            return dict(
                conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = :t"
                    ),
                    {"t": table},
                ).all()
            )

    def test_upgrade_creates_the_four_notification_tables(self, migration_db):
        _alembic(LAST_PRE_1_3_REVISION)
        assert NOTIFICATION_TABLES.isdisjoint(
            set(inspect(migration_db).get_table_names())
        )

        _alembic("head")
        assert NOTIFICATION_TABLES <= set(inspect(migration_db).get_table_names())

    def test_downgrade_drops_every_notification_table(self, migration_db):
        _alembic("head")
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")

        assert _current_revision(migration_db) == LAST_PRE_1_3_REVISION
        assert NOTIFICATION_TABLES.isdisjoint(
            set(inspect(migration_db).get_table_names())
        )

    def test_downgrade_then_re_upgrade_restores_the_tables(self, migration_db):
        _alembic("head")
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
        _alembic("head")

        assert _current_revision(migration_db) == RELEASE_REVISION
        assert NOTIFICATION_TABLES <= set(inspect(migration_db).get_table_names())

    def test_unique_constraints_exist(self, migration_db):
        _alembic("head")
        inspector = inspect(migration_db)

        def constraint(table, name):
            found = [
                c
                for c in inspector.get_unique_constraints(table)
                if c["name"] == name
            ]
            assert found, f"missing unique constraint {name} on {table}"
            return found[0]

        assert constraint("notification", "uq_notification_tenant_idempotency_key")[
            "column_names"
        ] == ["tenant_id", "idempotency_key"]
        assert constraint(
            "notification_recipient", "uq_notification_recipient_notification_user"
        )["column_names"] == ["notification_id", "user_id"]
        assert constraint(
            "notification_preference", "uq_notification_preference_user_type"
        )["column_names"] == ["user_id", "notification_type"]

    def test_the_superseded_address_constraint_is_never_created(self, migration_db):
        """An anti-assertion, and the reason the consolidated migration is not
        a concatenation of the three Phase 3 revisions. Block B's
        `UNIQUE (notification_id, channel, recipient_address)` encoded the
        assumption that one address belongs to one person; Block D dropped it.
        Creating it here only to drop it would replay a defect, so it must not
        exist at any point — including immediately after the upgrade.
        """
        _alembic("head")
        names = {
            c["name"]
            for c in inspect(migration_db).get_unique_constraints("notification_delivery")
        }
        assert SUPERSEDED_DELIVERY_CONSTRAINT not in names
        assert SUPERSEDED_DELIVERY_CONSTRAINT not in self._index_map(
            migration_db, "notification_delivery"
        )

    def test_both_delivery_indexes_are_unique_and_partial(self, migration_db):
        """Partial is the whole point: each index covers exactly the half of
        the table the other cannot key on, so every row is guarded once."""
        _alembic("head")
        indexes = self._index_map(migration_db, "notification_delivery")

        user_index = indexes[self.USER_INDEX]
        assert "CREATE UNIQUE INDEX" in user_index
        assert "recipient_user_id IS NOT NULL" in user_index
        assert "notification_id" in user_index and "channel" in user_index

        address_index = indexes[self.ADDRESS_INDEX]
        assert "CREATE UNIQUE INDEX" in address_index
        assert "recipient_user_id IS NULL" in address_index
        assert "recipient_address" in address_index

    def test_two_users_sharing_one_mailbox_each_get_a_delivery_row(self, migration_db):
        """The delivery model's defining behaviour, asserted as behaviour
        rather than as DDL: a shared `recepcion@` must not mean the second
        person silently receives nothing."""
        _alembic("head")
        with migration_db.connect() as conn:
            tenant_id, notification_id, user_a, user_b = _seed_delivery_context(conn)

            _insert_delivery(conn, tenant_id=tenant_id, notification_id=notification_id,
                             recipient_user_id=user_a, address="shared@lab.test")
            _insert_delivery(conn, tenant_id=tenant_id, notification_id=notification_id,
                             recipient_user_id=user_b, address="shared@lab.test")
            conn.commit()

            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM notification_delivery "
                    "WHERE notification_id = :n"
                ),
                {"n": notification_id},
            ).scalar_one()
        assert count == 2

    def test_one_user_still_cannot_get_two_rows_for_the_same_event(self, migration_db):
        """Allowing a shared address must not relax the per-recipient rule:
        the duplicate defence moved, it did not disappear."""
        _alembic("head")
        with migration_db.connect() as conn:
            tenant_id, notification_id, user_a, _ = _seed_delivery_context(conn)
            _insert_delivery(conn, tenant_id=tenant_id, notification_id=notification_id,
                             recipient_user_id=user_a, address="a@lab.test")
            conn.commit()

            with pytest.raises(Exception):
                # A different address, same user: still one delivery per
                # (event, channel, recipient).
                _insert_delivery(conn, tenant_id=tenant_id,
                                 notification_id=notification_id,
                                 recipient_user_id=user_a, address="a.alias@lab.test")
                conn.commit()

    def test_account_less_recipients_keep_the_address_guarantee(self, migration_db):
        """A physician resolved straight to an address has no user id to key
        on, so the original address constraint must still hold for them."""
        _alembic("head")
        with migration_db.connect() as conn:
            tenant_id, notification_id, _, _ = _seed_delivery_context(conn)
            _insert_delivery(conn, tenant_id=tenant_id, notification_id=notification_id,
                             recipient_user_id=None, address="physician@practice.test")
            conn.commit()

            with pytest.raises(Exception):
                _insert_delivery(conn, tenant_id=tenant_id,
                                 notification_id=notification_id,
                                 recipient_user_id=None,
                                 address="physician@practice.test")
                conn.commit()

    def test_indexes_supporting_the_hot_query_paths_exist(self, migration_db):
        _alembic("head")
        inspector = inspect(migration_db)

        def columns_of(table, index_name):
            found = [i for i in inspector.get_indexes(table) if i["name"] == index_name]
            assert found, f"missing index {index_name} on {table}"
            return found[0]["column_names"]

        assert columns_of("notification", "ix_notification_tenant_resource") == [
            "tenant_id",
            "resource_type",
            "resource_id",
        ]
        assert columns_of("notification", "ix_notification_tenant_type_created_at") == [
            "tenant_id",
            "type",
            "created_at",
        ]
        # Unread-count query.
        assert columns_of(
            "notification_recipient", "ix_notification_recipient_inbox_status"
        ) == ["tenant_id", "user_id", "status"]
        # Inbox list/pagination query.
        assert columns_of(
            "notification_recipient", "ix_notification_recipient_inbox_created_at"
        ) == ["tenant_id", "user_id", "created_at"]
        # The delivery poller's claim query.
        assert columns_of("notification_delivery", "ix_notification_delivery_poller") == [
            "status",
            "next_attempt_at",
        ]

    def test_check_constraints_pin_the_enum_values(self, migration_db):
        """Enums are VARCHAR + CHECK, not native PostgreSQL ENUM types (see
        the release migration's module docstring). Assert the constraints
        exist and that no native enum type was created behind our back."""
        _alembic("head")
        with migration_db.connect() as conn:
            checks = dict(
                conn.execute(
                    text(
                        "SELECT con.conname, pg_get_constraintdef(con.oid) "
                        "FROM pg_constraint con "
                        "JOIN pg_class rel ON rel.oid = con.conrelid "
                        "WHERE con.contype = 'c' AND rel.relname LIKE 'notification%'"
                    )
                ).all()
            )

        assert "REPORT_SUBMITTED" in checks["ck_notification_type"]
        assert "SAMPLE_STATUS_CHANGED" in checks["ck_notification_type"]
        assert "ACTION_REQUIRED" in checks["ck_notification_severity"]
        assert "DISMISSED" in checks["ck_notification_recipient_status"]
        # EMAIL is the only channel — PUSH/SMS must NOT be pre-declared.
        assert "EMAIL" in checks["ck_notification_delivery_channel"]
        assert "PUSH" not in checks["ck_notification_delivery_channel"]
        assert "SMS" not in checks["ck_notification_delivery_channel"]
        assert "SENDING" in checks["ck_notification_delivery_status"]
        assert "attempts" in checks["ck_notification_delivery_attempts_non_negative"]
        assert "read_at" in checks["ck_notification_recipient_read_requires_timestamp"]
        assert "REPORT_PDF_READY" in checks["ck_notification_preference_type"]

        with migration_db.connect() as conn:
            enum_types = conn.execute(
                text(
                    "SELECT typname FROM pg_type WHERE typtype = 'e' "
                    "AND typname LIKE 'notification%'"
                )
            ).all()
        assert enum_types == []

    def test_nullability_matches_the_contract(self, migration_db):
        _alembic("head")
        inspector = inspect(migration_db)

        def nullable(table):
            return {c["name"]: c["nullable"] for c in inspector.get_columns(table)}

        notification = nullable("notification")
        assert notification["title"] is False
        assert notification["idempotency_key"] is False
        assert notification["resource_type"] is False
        assert notification["resource_id"] is False
        # Title-only notifications are legal; system-generated ones have no
        # actor.
        assert notification["body"] is True
        assert notification["created_by"] is True
        assert notification["notification_metadata"] is True

        recipient = nullable("notification_recipient")
        assert recipient["read_at"] is True
        assert recipient["status"] is False
        # The redundant delivered_at Block A proposed was deliberately not
        # created — see the architecture decision.
        assert "delivered_at" not in recipient

        delivery = nullable("notification_delivery")
        # NOT NULL is what makes the account-less delivery guarantee real.
        assert delivery["recipient_address"] is False
        assert delivery["recipient_user_id"] is True
        assert delivery["attempts"] is False

    def test_locale_is_created_with_the_table_not_added_afterwards(self, migration_db):
        """Phase 3 Block F's column, absorbed into `CREATE TABLE
        notification`. It must be non-null with the `es-MX` server default the
        moment the release migration finishes — there is no later revision to
        add it, and on a clean database there is no row to backfill.

        `locale` is also asserted to be the table's *last* column: that is
        where the superseded `ALTER TABLE ... ADD COLUMN` left it, and keeping
        the physical order is what makes the consolidated schema identical to
        the former `v1_6_0` schema at the `pg_dump` level rather than merely
        semantically.
        """
        _alembic("head")
        columns = inspect(migration_db).get_columns("notification")
        by_name = {c["name"]: c for c in columns}

        assert "locale" in by_name
        assert by_name["locale"]["nullable"] is False
        assert "es-MX" in str(by_name["locale"]["default"])
        assert columns[-1]["name"] == "locale"

    def test_notification_foreign_keys_have_no_destructive_cascade(self, migration_db):
        """Deleting a user or tenant must not silently erase notification
        history — no FK may carry ON DELETE CASCADE/SET NULL."""
        _alembic("head")
        with migration_db.connect() as conn:
            definitions = [
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(con.oid) FROM pg_constraint con "
                        "JOIN pg_class rel ON rel.oid = con.conrelid "
                        "WHERE con.contype = 'f' AND rel.relname LIKE 'notification%'"
                    )
                ).all()
            ]

        assert definitions, "expected foreign keys on the notification tables"
        for definition in definitions:
            assert "ON DELETE" not in definition.upper(), definition

    def test_release_creates_no_notification_or_preference_rows(self, migration_db):
        """Additive, no backfill: the tables arrive empty. In particular no
        preference row is seeded per user or per type — absence of a row is
        what 'use the default' means, and the preference contract depends on
        that staying true."""
        _alembic("head")
        with migration_db.connect() as conn:
            for table in sorted(NOTIFICATION_TABLES):
                count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                assert count == 0, f"{table} is not empty after upgrade"


def _seed_delivery_context(conn):
    """Minimal tenant/users/notification needed to insert a delivery row
    directly, without importing the application's models — these tests assert
    what the *migration* produced, not what SQLModel thinks it did."""
    tenant_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO tenant (id, name, created_at) "
            "VALUES (:id, :name, now())"
        ),
        {"id": tenant_id, "name": f"T-{tenant_id.hex[:8]}"},
    )

    user_ids = []
    for index in range(2):
        user_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO app_user (id, tenant_id, email, full_name, first_name, "
                "last_name, hashed_password, is_active, created_at) "
                "VALUES (:id, :tenant_id, :email, 'U', 'U', 'U', 'x', true, now())"
            ),
            {
                "id": user_id,
                "tenant_id": tenant_id,
                "email": f"u{index}-{user_id.hex[:8]}@lab.test",
            },
        )
        user_ids.append(user_id)

    notification_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO notification (id, tenant_id, type, severity, title, "
            "resource_type, resource_id, idempotency_key, created_at) "
            "VALUES (:id, :tenant_id, 'REPORT_PUBLISHED', 'INFO', 'T', 'report', "
            ":resource_id, :key, now())"
        ),
        {
            "id": notification_id,
            "tenant_id": tenant_id,
            "resource_id": uuid.uuid4(),
            "key": f"k-{notification_id.hex}",
        },
    )
    return tenant_id, notification_id, user_ids[0], user_ids[1]


def _insert_recipient(conn, *, tenant_id, notification_id, user_id):
    conn.execute(
        text(
            "INSERT INTO notification_recipient (id, notification_id, tenant_id, "
            "user_id, status, created_at) "
            "VALUES (:id, :notification_id, :tenant_id, :user_id, 'UNREAD', now())"
        ),
        {
            "id": uuid.uuid4(),
            "notification_id": notification_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
        },
    )


def _insert_delivery(conn, *, tenant_id, notification_id, recipient_user_id, address):
    conn.execute(
        text(
            "INSERT INTO notification_delivery (id, notification_id, tenant_id, "
            "recipient_user_id, recipient_address, channel, status, attempts, "
            "created_at, updated_at) "
            "VALUES (:id, :notification_id, :tenant_id, :recipient_user_id, "
            ":address, 'EMAIL', 'PENDING', 0, now(), now())"
        ),
        {
            "id": uuid.uuid4(),
            "notification_id": notification_id,
            "tenant_id": tenant_id,
            "recipient_user_id": recipient_user_id,
            "address": address,
        },
    )


def _insert_preference(conn, *, tenant_id, user_id):
    conn.execute(
        text(
            "INSERT INTO notification_preference (id, tenant_id, user_id, "
            "notification_type, in_app_enabled, email_enabled, updated_at) "
            "VALUES (:id, :tenant_id, :user_id, 'REPORT_PUBLISHED', true, false, now())"
        ),
        {"id": uuid.uuid4(), "tenant_id": tenant_id, "user_id": user_id},
    )
