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
from sqlalchemy.exc import IntegrityError

from app.core.config import settings


BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSIONS_DIR = BACKEND_ROOT / "alembic" / "versions"

#: The last revision belonging to the release *before* Céluma 1.3 — the
#: revision `main` and tag v1.2.0 carry.
LAST_PRE_1_3_REVISION = "v1_2_0"

#: The single consolidated Céluma 1.3 release revision. No longer the head
#: — Céluma 1.3, Phase 4, Block B adds the first post-release revision on
#: top of it — but it remains the fixed, closed boundary every pre-1.3
#: revision test still targets explicitly (never "head") so those tests
#: keep describing the release migration's own footprint, unaffected by
#: whatever lands on top of it in later phases.
RELEASE_REVISION = "v1_3_0"

#: Céluma 1.3, Phase 4, Block B — the usage domain model. The first revision
#: built on top of the closed v1_3_0 release. No longer the head — Block C
#: adds one more revision on top — but still the fixed boundary
#: `TestUsageDomainMigration` targets explicitly.
#: Not v1_4_0: that id (and v1_5_0 through v1_9_0) is permanently retired by
#: SUPERSEDED_REVISIONS below — the Phase 3 closure squash folded them into
#: v1_3_0 and forbids their ever being resolvable again. v1_10_0 is the
#: first id after v1_3_0 that chain never used.
USAGE_DOMAIN_REVISION = "v1_10_0"

#: Céluma 1.3, Phase 4, Block C — storage attribution & usage
#: initialization. Built directly on top of v1_10_0. No longer the head —
#: Block D adds one more revision on top. Data-only (no new table/column) —
#: see the revision's own module docstring for why it still deserves a
#: dedicated id rather than being folded into v1_10_0 (that revision is
#: closed, per the master spec's "do not rewrite v1_10_0/v1_3_0"
#: instruction).
STORAGE_ATTRIBUTION_REVISION = "v1_11_0"

#: Céluma 1.3, Phase 4, Block D — DB-scoped tenant logo (`tenant.
#: logo_storage_id` + backfill) and reconciliation hardening
#: (`tenant_usage_reconciliation.metadata_mismatches_found`, one-RUNNING-
#: per-tenant partial unique index). No longer the head — Block G adds one
#: more revision on top — but still the fixed boundary
#: `TestReconciliationHardeningMigration` targets explicitly.
BLOCK_D_REVISION = "v1_12_0"

#: Céluma 1.3, Phase 4, Block G — durable usage-threshold state
#: (`tenant_usage_threshold_state`). Schema only: the revision creates the
#: table and nothing else, and in particular seeds no baseline state and
#: creates no notification. The current alembic head.
BLOCK_G_REVISION = "v1_13_0"

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

#: The three tables Céluma 1.3, Phase 4, Block B introduces on top of the
#: release revision.
USAGE_DOMAIN_TABLES = {
    "tenant_usage",
    "tenant_limits",
    "tenant_usage_reconciliation",
}

#: The one table Céluma 1.3, Phase 4, Block G introduces on top of Block D's
#: revision.
THRESHOLD_STATE_TABLES = {"tenant_usage_threshold_state"}

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

    def test_head_is_the_block_g_revision(self):
        """Céluma 1.3, Phase 4, Block G: the head moves forward again, from
        Block D's revision to Block G's. Same reasoning as every previous
        move of this assertion — it names the newest revision, and is
        expected to be updated (not deleted) by the next block that adds one.
        """
        assert _script_directory().get_current_head() == BLOCK_G_REVISION

    def test_release_revision_sits_directly_on_the_last_pre_1_3_revision(self):
        revision = _script_directory().get_revision(RELEASE_REVISION)
        assert revision.down_revision == LAST_PRE_1_3_REVISION

    def test_usage_domain_revision_sits_directly_on_the_release_revision(self):
        """The new revision is additive on top of the closed release, not a
        rewrite of it: `v1_3_0` is untouched, and `v1_4_0` is its only
        child."""
        revision = _script_directory().get_revision(USAGE_DOMAIN_REVISION)
        assert revision.down_revision == RELEASE_REVISION

    def test_storage_attribution_revision_sits_directly_on_the_usage_domain_revision(self):
        """Block C is additive on top of the closed Block B revision, not a
        rewrite of it: `v1_10_0` is untouched, and `v1_11_0` is its only
        child."""
        revision = _script_directory().get_revision(STORAGE_ATTRIBUTION_REVISION)
        assert revision.down_revision == USAGE_DOMAIN_REVISION

    def test_block_d_revision_sits_directly_on_the_storage_attribution_revision(self):
        """Block D is additive on top of the closed Block C revision: after
        its own D0 determinism correction, `v1_11_0` is frozen and
        `v1_12_0` is its only child."""
        revision = _script_directory().get_revision(BLOCK_D_REVISION)
        assert revision.down_revision == STORAGE_ATTRIBUTION_REVISION

    def test_block_g_revision_sits_directly_on_the_block_d_revision(self):
        """Block G is additive on top of the closed Block D revision:
        `v1_12_0` is untouched and `v1_13_0` is its only child."""
        revision = _script_directory().get_revision(BLOCK_G_REVISION)
        assert revision.down_revision == BLOCK_D_REVISION

    def test_chain_is_linear_from_base_to_head(self):
        script = _script_directory()
        revisions = list(script.walk_revisions())
        assert [r.revision for r in revisions] == [
            BLOCK_G_REVISION,
            BLOCK_D_REVISION,
            STORAGE_ATTRIBUTION_REVISION,
            USAGE_DOMAIN_REVISION,
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


class TestMigrationHistoricalDeterminism:
    """Céluma 1.3, Phase 4, Block C remediation — a migration must produce
    the same result forever, independent of the application code around
    it. `app.services.*` modules carry evolvable business logic (a future
    change to `StorageBillingService`'s billable categories, for example);
    if a historical migration imported one, upgrading a fresh environment
    through the full chain later would silently apply *today's* rules to a
    *historical* revision's data, instead of the rules that revision
    actually shipped with. See v1_11_0's own module docstring
    ("Historical determinism") and docs/celuma-1.3/phase-4-block-c/
    block-c-remediation-report.md.

    A structural AST guard, not a DB-backed test — this is about what a
    migration file imports, not what it does once run.
    """

    def _imported_modules(self, path: pathlib.Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name)
        return modules

    def test_v1_11_0_does_not_import_application_services(self):
        path = VERSIONS_DIR / "v1_11_0_block_c_storage_attribution.py"
        modules = self._imported_modules(path)
        offenders = {
            m for m in modules if m == "app.services" or m.startswith("app.services.")
        }
        assert offenders == set(), (
            f"v1_11_0 must not import runtime business services; found {offenders}"
        )

    def test_v1_11_0_does_not_import_the_usage_service(self):
        path = VERSIONS_DIR / "v1_11_0_block_c_storage_attribution.py"
        modules = self._imported_modules(path)
        assert "app.services.usage" not in modules
        assert not any(m.startswith("app.services.usage") for m in modules)

    def test_v1_11_0_does_not_import_the_storage_billing_service(self):
        path = VERSIONS_DIR / "v1_11_0_block_c_storage_attribution.py"
        modules = self._imported_modules(path)
        assert "app.services.storage_billing" not in modules
        assert not any(m.startswith("app.services.storage_billing") for m in modules)

    def test_no_migration_file_imports_an_application_service(self):
        """The general rule this remediation establishes for every
        migration, not only v1_11_0 — a regression guard against the same
        mistake recurring in a later revision."""
        offenders = []
        for path in sorted(VERSIONS_DIR.glob("*.py")):
            modules = self._imported_modules(path)
            bad = {m for m in modules if m == "app.services" or m.startswith("app.services.")}
            if bad:
                offenders.append((path.name, sorted(bad)))
        assert offenders == []

    def test_v1_11_0_only_imports_stable_primitives(self):
        """Whitelist, not blacklist — proves the migration's import surface
        is exactly the small, stable set intended (`typing`, `alembic.op`,
        `sqlalchemy.text`), not merely "no app.services", which a
        differently-shaped business-logic import (e.g. a direct app.models
        import performing hidden computation) could technically satisfy
        while still reintroducing drift risk.

        `os` was on this list until Block D's D0 correction, because the
        tenant-logo baseline read `os.environ` for the CDN prefix. It is
        deliberately no longer allowed: that read is exactly what made the
        revision environment-dependent.
        """
        path = VERSIONS_DIR / "v1_11_0_block_c_storage_attribution.py"
        modules = self._imported_modules(path)
        allowed_prefixes = ("typing", "alembic", "sqlalchemy")
        offenders = {
            m for m in modules if not any(m == p or m.startswith(p + ".") for p in allowed_prefixes)
        }
        assert offenders == set(), f"unexpected import surface: {offenders}"

    @pytest.mark.parametrize(
        "setting",
        ["MEDIA_PUBLIC_BASE_URL", "S3_BUCKET_NAME", "AWS_REGION", "os.environ", "getenv"],
    )
    def test_v1_11_0_reads_no_environment_configuration(self, setting):
        """Céluma 1.3, Phase 4, Block D (D0). Historical determinism is not
        only "does not import evolvable code" — it is also "does not read
        mutable configuration". Until D0 this revision rebuilt the public-URL
        prefix from `MEDIA_PUBLIC_BASE_URL`/`S3_BUCKET_NAME`/`AWS_REGION` to
        interpret a persisted `Tenant.logo_url`, so the same rows and the
        same source could produce a different baseline in an environment
        whose CDN hostname had changed. The current-logo resolution is now
        purely relational, and this guard keeps those settings from
        returning to it.

        Runs against executable source only — the module docstring
        legitimately *discusses* these names.
        """
        source = _executable_source(VERSIONS_DIR / "v1_11_0_block_c_storage_attribution.py")
        assert setting not in source

    def test_no_migration_file_reads_the_cdn_base_url(self):
        """The standing rule D0 establishes for every revision, not only
        v1_11_0: a migration's result must not depend on which CDN happens
        to be configured when it runs."""
        offenders = [
            path.name
            for path in sorted(VERSIONS_DIR.glob("*.py"))
            if "MEDIA_PUBLIC_BASE_URL" in _executable_source(path)
        ]
        assert offenders == []


def _admin_engine():
    return create_engine(
        make_url(settings.database_url).set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )


def _alembic(
    target: str,
    *,
    command: str = "upgrade",
    database: str = _MIGRATION_TEST_DB,
    env: dict | None = None,
) -> None:
    """Run one alembic command against an ephemeral database.

    `database` and `env` exist for Céluma 1.3 Phase 4, Block D's
    historical-determinism proof, which runs the same revision against two
    separate databases under deliberately different `MEDIA_PUBLIC_BASE_URL`/
    `S3_BUCKET_NAME`/`AWS_REGION` values and asserts the results are
    identical.
    """
    url = make_url(settings.database_url).set(database=database)
    subprocess.run(
        ["alembic", command, target],
        cwd=str(BACKEND_ROOT),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            **(env or {}),
            "DATABASE_URL": url.render_as_string(hide_password=False),
        },
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
        """Pinned to RELEASE_REVISION, not "head": this test is about the
        v1_2_0 -> v1_3_0 transition specifically, isolated from whatever
        later phases (Block B's v1_4_0 onward) add on top."""
        _alembic(LAST_PRE_1_3_REVISION)
        assert _current_revision(migration_db) == LAST_PRE_1_3_REVISION

        _alembic(RELEASE_REVISION)
        assert _current_revision(migration_db) == RELEASE_REVISION

    def test_downgrade_then_re_upgrade(self, migration_db):
        _alembic(RELEASE_REVISION)
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
        assert _current_revision(migration_db) == LAST_PRE_1_3_REVISION

        _alembic(RELEASE_REVISION)
        assert _current_revision(migration_db) == RELEASE_REVISION

    def test_release_introduces_exactly_the_expected_tables(self, migration_db):
        """The release migration's table footprint, pinned. Replaces Block B's
        `test_notifications_revision_modifies_no_existing_table`, which could
        only be expressed while the notification tables arrived in a separate
        revision: there is no longer an intermediate state to diff against, so
        the guard moves to the `v1_2_0 → v1_3_0` boundary instead.

        Pinned to RELEASE_REVISION rather than "head": Céluma 1.3, Phase 4,
        Block B adds three more tables on top, and this test's whole point is
        an *exact* set match on what v1_3_0 alone introduced."""
        _alembic(LAST_PRE_1_3_REVISION)
        before = set(inspect(migration_db).get_table_names())

        _alembic(RELEASE_REVISION)
        after = set(inspect(migration_db).get_table_names())

        assert after - before == RELEASE_TABLES
        assert before - after == set()

    def test_downgrade_removes_every_object_the_release_introduced(self, migration_db):
        _alembic(RELEASE_REVISION)
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

        Pinned to RELEASE_REVISION: this is the release migration's own
        populated-downgrade guarantee, isolated from Block B's.
        """
        _alembic(RELEASE_REVISION)
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
        _alembic(RELEASE_REVISION)
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
        _alembic(RELEASE_REVISION)
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

        assert _current_revision(migration_db) == BLOCK_G_REVISION
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


class TestUsageDomainMigration:
    """Céluma 1.3, Phase 4, Block B — the usage domain revision (`v1_4_0`),
    additive on top of the closed release revision (`v1_3_0`). Mirrors the
    discipline `TestNotificationDomain` established: the chain tests above
    prove the revision is reachable and reversible; these prove it created
    the right tables, columns, constraints and indexes.
    """

    def test_upgrade_creates_the_three_usage_tables(self, migration_db):
        _alembic(RELEASE_REVISION)
        assert USAGE_DOMAIN_TABLES.isdisjoint(
            set(inspect(migration_db).get_table_names())
        )

        _alembic("head")
        assert USAGE_DOMAIN_TABLES <= set(inspect(migration_db).get_table_names())

    def test_introduces_exactly_the_expected_tables(self, migration_db):
        """Pinned to `USAGE_DOMAIN_REVISION`, not "head".

        This test is about `v1_10_0`'s own footprint — "these three tables and
        nothing else" — which is a statement about one closed revision.
        Running it to "head" made it silently also assert that no later
        revision ever adds a table, so Céluma 1.3, Phase 4, Block G's
        `tenant_usage_threshold_state` failed a test that was never meant to
        be about it. Block G's footprint has its own class below.
        """
        _alembic(RELEASE_REVISION)
        before = set(inspect(migration_db).get_table_names())

        _alembic(USAGE_DOMAIN_REVISION)
        after = set(inspect(migration_db).get_table_names())

        assert after - before == USAGE_DOMAIN_TABLES

    def test_downgrade_drops_every_usage_table(self, migration_db):
        _alembic("head")
        _alembic(RELEASE_REVISION, command="downgrade")

        assert _current_revision(migration_db) == RELEASE_REVISION
        assert USAGE_DOMAIN_TABLES.isdisjoint(
            set(inspect(migration_db).get_table_names())
        )

    def test_downgrade_then_re_upgrade_restores_the_tables(self, migration_db):
        _alembic("head")
        _alembic(RELEASE_REVISION, command="downgrade")
        _alembic("head")

        assert _current_revision(migration_db) == BLOCK_G_REVISION
        assert USAGE_DOMAIN_TABLES <= set(inspect(migration_db).get_table_names())

    def test_downgrade_removes_the_app_user_index(self, migration_db):
        """Confirmed absent before this revision (module docstring) — must
        also be confirmed absent again after a downgrade."""
        _alembic("head")
        indexes_before = {
            i["name"] for i in inspect(migration_db).get_indexes("app_user")
        }
        assert "ix_app_user_tenant_id_is_active" in indexes_before

        _alembic(RELEASE_REVISION, command="downgrade")
        indexes_after = {
            i["name"] for i in inspect(migration_db).get_indexes("app_user")
        }
        assert "ix_app_user_tenant_id_is_active" not in indexes_after

    def test_downgrade_works_on_a_populated_database(self, migration_db):
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            conn.execute(
                text(
                    "INSERT INTO tenant_usage "
                    "(tenant_id, billable_storage_bytes, last_updated) "
                    "VALUES (:tenant_id, 1024, now())"
                ),
                {"tenant_id": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO tenant_limits "
                    "(tenant_id, storage_limit_bytes, user_limit, updated_at) "
                    "VALUES (:tenant_id, 1073741824, 10, now())"
                ),
                {"tenant_id": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO tenant_usage_reconciliation "
                    "(id, tenant_id, status, started_at, completed_at) "
                    "VALUES (:id, :tenant_id, 'SUCCEEDED', now(), now())"
                ),
                {"id": uuid.uuid4(), "tenant_id": tenant_id},
            )

        _alembic(RELEASE_REVISION, command="downgrade")

        assert _current_revision(migration_db) == RELEASE_REVISION
        assert USAGE_DOMAIN_TABLES.isdisjoint(
            set(inspect(migration_db).get_table_names())
        )

    def test_columns_nullability_and_defaults(self, migration_db):
        _alembic("head")
        inspector = inspect(migration_db)

        usage = {c["name"]: c for c in inspector.get_columns("tenant_usage")}
        assert usage["tenant_id"]["nullable"] is False
        assert usage["billable_storage_bytes"]["nullable"] is False
        assert "0" in str(usage["billable_storage_bytes"]["default"])
        assert usage["last_updated"]["nullable"] is False

        limits = {c["name"]: c for c in inspector.get_columns("tenant_limits")}
        assert limits["tenant_id"]["nullable"] is False
        assert limits["storage_limit_bytes"]["nullable"] is True
        assert limits["user_limit"]["nullable"] is True
        assert limits["updated_at"]["nullable"] is False

        recon = {
            c["name"]: c
            for c in inspector.get_columns("tenant_usage_reconciliation")
        }
        assert recon["id"]["nullable"] is False
        assert recon["tenant_id"]["nullable"] is False
        assert recon["status"]["nullable"] is False
        assert "RUNNING" in str(recon["status"]["default"])
        assert recon["started_at"]["nullable"] is False
        for nullable_field in (
            "completed_at",
            "expected_storage_bytes",
            "actual_storage_bytes",
            "difference_bytes",
            "objects_checked",
            "orphans_found",
            "missing_objects_found",
            "repaired",
            "error_code",
        ):
            assert recon[nullable_field]["nullable"] is True, nullable_field

    def test_primary_keys_and_foreign_keys(self, migration_db):
        _alembic("head")
        inspector = inspect(migration_db)

        assert inspector.get_pk_constraint("tenant_usage")[
            "constrained_columns"
        ] == ["tenant_id"]
        assert inspector.get_pk_constraint("tenant_limits")[
            "constrained_columns"
        ] == ["tenant_id"]
        assert inspector.get_pk_constraint("tenant_usage_reconciliation")[
            "constrained_columns"
        ] == ["id"]

        for table in USAGE_DOMAIN_TABLES:
            fks = inspector.get_foreign_keys(table)
            assert any(
                fk["referred_table"] == "tenant"
                and fk["constrained_columns"] == ["tenant_id"]
                for fk in fks
            ), f"{table} missing tenant_id -> tenant.id foreign key"
            # No FK carries ON DELETE — same no-cascade convention as the
            # notification domain (deleting a tenant with usage/limits/
            # reconciliation history must be refused, not silently erased).
            for fk in fks:
                assert not fk.get("options", {}).get("ondelete")

    def test_check_constraints(self, migration_db):
        _alembic("head")
        with migration_db.connect() as conn:
            checks = dict(
                conn.execute(
                    text(
                        "SELECT con.conname, pg_get_constraintdef(con.oid) "
                        "FROM pg_constraint con "
                        "JOIN pg_class rel ON rel.oid = con.conrelid "
                        "WHERE con.contype = 'c' AND rel.relname IN "
                        "('tenant_usage', 'tenant_limits', "
                        "'tenant_usage_reconciliation')"
                    )
                ).all()
            )

        assert "billable_storage_bytes" in checks["ck_tenant_usage_storage_non_negative"]
        assert "storage_limit_bytes" in checks["ck_tenant_limits_storage_limit_positive"]
        assert "user_limit" in checks["ck_tenant_limits_user_limit_positive"]
        assert "RUNNING" in checks["ck_tenant_usage_reconciliation_status"]
        assert "SUCCEEDED" in checks["ck_tenant_usage_reconciliation_status"]
        assert "FAILED" in checks["ck_tenant_usage_reconciliation_status"]

        # Enums are VARCHAR + CHECK here too, not a native Postgres ENUM.
        with migration_db.connect() as conn:
            enum_types = conn.execute(
                text(
                    "SELECT typname FROM pg_type WHERE typtype = 'e' "
                    "AND typname LIKE 'tenant_usage%'"
                )
            ).all()
        assert enum_types == []

    def test_reconciliation_lifecycle_constraints_reject_invalid_rows(
        self, migration_db
    ):
        """RUNNING must have no completed_at; a terminal status must have
        one; SUCCEEDED must not carry an error_code; an unrecognized status
        is rejected outright."""
        _alembic("head")
        with migration_db.connect() as conn:
            tenant_id = _seed_tenant(conn)
            conn.commit()

        def _insert(status, completed_at_sql, error_code_sql):
            with migration_db.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO tenant_usage_reconciliation "
                        "(id, tenant_id, status, started_at, completed_at, "
                        "error_code) VALUES (:id, :tenant_id, :status, "
                        f"now(), {completed_at_sql}, {error_code_sql})"
                    ),
                    {"id": uuid.uuid4(), "tenant_id": tenant_id, "status": status},
                )
                conn.commit()

        # Valid combinations.
        _insert("RUNNING", "NULL", "NULL")
        _insert("SUCCEEDED", "now()", "NULL")
        _insert("FAILED", "now()", "'s3_timeout'")

        # Invalid: RUNNING with a completed_at already set.
        with pytest.raises(Exception):
            _insert("RUNNING", "now()", "NULL")
        # Invalid: SUCCEEDED with no completed_at.
        with pytest.raises(Exception):
            _insert("SUCCEEDED", "NULL", "NULL")
        # Invalid: SUCCEEDED carrying an error_code.
        with pytest.raises(Exception):
            _insert("SUCCEEDED", "now()", "'oops'")
        # Invalid: an unrecognized status value.
        with pytest.raises(Exception):
            _insert("CANCELLED", "now()", "NULL")

    def test_counters_reject_negative_values(self, migration_db):
        _alembic("head")
        with migration_db.connect() as conn:
            tenant_id = _seed_tenant(conn)
            conn.commit()

        with pytest.raises(Exception):
            with migration_db.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO tenant_usage_reconciliation "
                        "(id, tenant_id, status, started_at, completed_at, "
                        "objects_checked) "
                        "VALUES (:id, :tenant_id, 'SUCCEEDED', now(), now(), -1)"
                    ),
                    {"id": uuid.uuid4(), "tenant_id": tenant_id},
                )
                conn.commit()

    def test_tenant_usage_storage_rejects_negative(self, migration_db):
        _alembic("head")
        with migration_db.connect() as conn:
            tenant_id = _seed_tenant(conn)
            conn.commit()

        with pytest.raises(Exception):
            with migration_db.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO tenant_usage "
                        "(tenant_id, billable_storage_bytes, last_updated) "
                        "VALUES (:tenant_id, -1, now())"
                    ),
                    {"tenant_id": tenant_id},
                )
                conn.commit()

    def test_tenant_usage_default_is_zero(self, migration_db):
        _alembic("head")
        with migration_db.connect() as conn:
            tenant_id = _seed_tenant(conn)
            conn.execute(
                text(
                    "INSERT INTO tenant_usage (tenant_id, last_updated) "
                    "VALUES (:tenant_id, now())"
                ),
                {"tenant_id": tenant_id},
            )
            conn.commit()
            value = conn.execute(
                text(
                    "SELECT billable_storage_bytes FROM tenant_usage "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            ).scalar_one()
        assert value == 0

    def test_tenant_limits_rejects_zero_and_negative(self, migration_db):
        _alembic("head")
        with migration_db.connect() as conn:
            tenant_id = _seed_tenant(conn)
            conn.commit()

        for bad_value in (0, -1):
            with pytest.raises(Exception):
                with migration_db.connect() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO tenant_limits "
                            "(tenant_id, storage_limit_bytes, updated_at) "
                            "VALUES (:tenant_id, :value, now())"
                        ),
                        {"tenant_id": tenant_id, "value": bad_value},
                    )
                    conn.commit()

    def test_tenant_limits_accepts_null_limits(self, migration_db):
        """NULL means unlimited/unconfigured — must be accepted, not
        rejected by the positive-only check."""
        _alembic("head")
        with migration_db.connect() as conn:
            tenant_id = _seed_tenant(conn)
            conn.execute(
                text(
                    "INSERT INTO tenant_limits (tenant_id, updated_at) "
                    "VALUES (:tenant_id, now())"
                ),
                {"tenant_id": tenant_id},
            )
            conn.commit()

    def test_indexes_supporting_the_hot_query_paths_exist(self, migration_db):
        _alembic("head")
        inspector = inspect(migration_db)

        def columns_of(table, index_name):
            found = [
                i for i in inspector.get_indexes(table) if i["name"] == index_name
            ]
            assert found, f"missing index {index_name} on {table}"
            return found[0]["column_names"]

        assert columns_of(
            "tenant_usage_reconciliation",
            "ix_tenant_usage_reconciliation_tenant_started_at",
        ) == ["tenant_id", "started_at"]
        assert columns_of(
            "tenant_usage_reconciliation",
            "ix_tenant_usage_reconciliation_status_started_at",
        ) == ["status", "started_at"]
        assert columns_of("app_user", "ix_app_user_tenant_id_is_active") == [
            "tenant_id",
            "is_active",
        ]

    def test_no_backfill_or_seed_rows(self, migration_db):
        """Additive, no backfill: all three tables arrive empty."""
        _alembic("head")
        with migration_db.connect() as conn:
            for table in sorted(USAGE_DOMAIN_TABLES):
                count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                assert count == 0, f"{table} is not empty after upgrade"


class TestStorageAttributionMigration:
    """Céluma 1.3, Phase 4, Block C — `v1_11_0`, the storage-attribution and
    usage-initialization revision. Data-only: these tests exercise the
    backfill and initialization logic directly against a populated
    database, the same way `TestUsageDomainMigration` proves v1_10_0's
    schema rather than merely that it is reachable.
    """

    def test_backfills_tenant_id_for_the_four_gapped_categories(self, migration_db):
        _alembic(USAGE_DOMAIN_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            branch_id = _seed_branch(conn, tenant_id)
            patient_id = _seed_patient(conn, tenant_id, branch_id)
            order_id = _seed_order(conn, tenant_id, branch_id, patient_id)
            report_id = _seed_report(conn, tenant_id, branch_id, order_id)
            sample_id = _seed_sample(conn, tenant_id, branch_id, order_id)

            # Sample image (processed) — tenant_id NULL before backfill.
            processed_storage_id = _seed_storage_object(
                conn, key="samples/processed/a.jpg", size_bytes=1000, tenant_id=None
            )
            sample_image_id = _seed_sample_image(
                conn, tenant_id, branch_id, sample_id, processed_storage_id
            )
            # Sample image rendition (thumbnail) — tenant_id NULL before backfill.
            thumb_storage_id = _seed_storage_object(
                conn, key="samples/thumb/a.jpg", size_bytes=100, tenant_id=None
            )
            _seed_sample_image_rendition(conn, sample_image_id, "thumbnail", thumb_storage_id)

            # Legacy PDF — tenant_id NULL before backfill (sha256_hex NULL
            # distinguishes it from an official PDF sharing the same FK).
            legacy_pdf_storage_id = _seed_storage_object(
                conn, key="reports/x/report.pdf", size_bytes=5000, tenant_id=None,
                content_type="application/pdf",
            )
            # Report JSON — tenant_id NULL before backfill.
            json_storage_id = _seed_storage_object(
                conn, key="reports/x/report.json", size_bytes=200, tenant_id=None,
                content_type="application/json",
            )
            _seed_report_version(
                conn, report_id, version_no=1,
                pdf_storage_id=legacy_pdf_storage_id, json_storage_id=json_storage_id,
            )

            # Live signature — tenant_id NULL before backfill.
            signature_storage_id = _seed_storage_object(
                conn, key="users/x/signature/a.png", size_bytes=50, tenant_id=None,
                content_type="image/png",
            )
            user_id = _seed_app_user(
                conn, tenant_id, email="sig@test.example",
                signature_storage_id=signature_storage_id,
            )

        _alembic("head")

        with migration_db.connect() as conn:
            rows = dict(
                conn.execute(
                    text(
                        "SELECT id, tenant_id FROM storage_object WHERE id IN "
                        "(:a, :b, :c, :d, :e)"
                    ),
                    {
                        "a": processed_storage_id,
                        "b": thumb_storage_id,
                        "c": legacy_pdf_storage_id,
                        "d": json_storage_id,
                        "e": signature_storage_id,
                    },
                ).all()
            )
        for storage_id in (
            processed_storage_id,
            thumb_storage_id,
            legacy_pdf_storage_id,
            json_storage_id,
            signature_storage_id,
        ):
            assert rows[storage_id] == tenant_id, f"{storage_id} not backfilled"

    def test_never_overwrites_an_existing_non_null_tenant_id(self, migration_db):
        _alembic(USAGE_DOMAIN_REVISION)
        with migration_db.begin() as conn:
            tenant_a = _seed_tenant(conn)
            tenant_b = _seed_tenant(conn)
            branch_id = _seed_branch(conn, tenant_a)
            patient_id = _seed_patient(conn, tenant_a, branch_id)
            order_id = _seed_order(conn, tenant_a, branch_id, patient_id)
            sample_id = _seed_sample(conn, tenant_a, branch_id, order_id)

            # Deliberately wrong tenant already set — a pre-Block-C row
            # that, for whatever reason, already carries a tenant_id. The
            # backfill must leave it exactly as-is.
            storage_id = _seed_storage_object(
                conn, key="samples/processed/b.jpg", size_bytes=999, tenant_id=tenant_b
            )
            _seed_sample_image(conn, tenant_a, branch_id, sample_id, storage_id)

        _alembic("head")

        with migration_db.connect() as conn:
            actual = conn.execute(
                text("SELECT tenant_id FROM storage_object WHERE id = :id"),
                {"id": storage_id},
            ).scalar_one()
        assert actual == tenant_b

    def test_initializes_usage_with_the_computed_baseline_per_tenant(self, migration_db):
        _alembic(USAGE_DOMAIN_REVISION)
        with migration_db.begin() as conn:
            tenant_a = _seed_tenant(conn)
            tenant_b = _seed_tenant(conn)
            tenant_c = _seed_tenant(conn)  # zero billable objects

            branch_a = _seed_branch(conn, tenant_a)
            patient_a = _seed_patient(conn, tenant_a, branch_a)
            order_a = _seed_order(conn, tenant_a, branch_a, patient_a)
            report_a = _seed_report(conn, tenant_a, branch_a, order_a)

            # Tenant A: one official PDF (sha256_hex set) — 10 bytes.
            official_storage = _seed_storage_object(
                conn, key="reports/a/official/1.pdf", size_bytes=10, tenant_id=tenant_a,
                content_type="application/pdf", sha256_hex="deadbeef",
            )
            _seed_report_version(conn, report_a, version_no=1, pdf_storage_id=official_storage)

            branch_b = _seed_branch(conn, tenant_b)
            patient_b = _seed_patient(conn, tenant_b, branch_b)
            order_b = _seed_order(conn, tenant_b, branch_b, patient_b)
            report_b = _seed_report(conn, tenant_b, branch_b, order_b)

            # Tenant B: one report JSON body — 77 bytes.
            json_storage = _seed_storage_object(
                conn, key="reports/b/report.json", size_bytes=77, tenant_id=None,
                content_type="application/json",
            )
            _seed_report_version(conn, report_b, version_no=1, json_storage_id=json_storage)

        _alembic("head")

        with migration_db.connect() as conn:
            usage = dict(
                conn.execute(
                    text(
                        "SELECT tenant_id, billable_storage_bytes FROM tenant_usage "
                        "WHERE tenant_id IN (:a, :b, :c)"
                    ),
                    {"a": tenant_a, "b": tenant_b, "c": tenant_c},
                ).all()
            )
        assert usage[tenant_a] == 10
        assert usage[tenant_b] == 77
        assert usage[tenant_c] == 0

    def test_initialization_is_idempotent_across_a_downgrade_and_re_upgrade(self, migration_db):
        """`downgrade()` is a deliberate no-op (see the revision's module
        docstring), which means re-running `alembic upgrade head` after a
        downgrade genuinely re-executes upgrade() — this is the natural way
        to prove replay safety with this test harness's subprocess-based
        `_alembic()` helper, without reaching into the migration's Python
        function directly."""
        _alembic(USAGE_DOMAIN_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            branch_id = _seed_branch(conn, tenant_id)
            patient_id = _seed_patient(conn, tenant_id, branch_id)
            order_id = _seed_order(conn, tenant_id, branch_id, patient_id)
            report_id = _seed_report(conn, tenant_id, branch_id, order_id)
            official_storage = _seed_storage_object(
                conn, key="reports/idem/official/1.pdf", size_bytes=4096, tenant_id=tenant_id,
                content_type="application/pdf", sha256_hex="abc123",
            )
            _seed_report_version(conn, report_id, version_no=1, pdf_storage_id=official_storage)

        _alembic("head")
        with migration_db.connect() as conn:
            first_run = conn.execute(
                text(
                    "SELECT billable_storage_bytes FROM tenant_usage WHERE tenant_id = :id"
                ),
                {"id": tenant_id},
            ).scalar_one()
        assert first_run == 4096

        _alembic(USAGE_DOMAIN_REVISION, command="downgrade")
        _alembic("head")

        with migration_db.connect() as conn:
            second_run = conn.execute(
                text(
                    "SELECT billable_storage_bytes FROM tenant_usage WHERE tenant_id = :id"
                ),
                {"id": tenant_id},
            ).scalar_one()
            row_count = conn.execute(
                text("SELECT COUNT(*) FROM tenant_usage WHERE tenant_id = :id"),
                {"id": tenant_id},
            ).scalar_one()
        assert second_run == 4096, "second run must not change usage"
        assert row_count == 1, "second run must not create a duplicate row"

    def test_downgrade_is_a_no_op(self, migration_db):
        """No schema to revert (data-only revision) — downgrading must not
        drop any Block B table or fail.

        Pinned to `STORAGE_ATTRIBUTION_REVISION` rather than "head" for the
        same reason as `test_introduces_exactly_the_expected_tables` above:
        the claim is that *`v1_11_0`* changes no schema, and running from head
        turned it into a claim that no revision between head and `v1_10_0`
        does either — which stopped being true when Céluma 1.3, Phase 4,
        Block G added `tenant_usage_threshold_state` in `v1_13_0`.
        """
        _alembic(STORAGE_ATTRIBUTION_REVISION)
        before = set(inspect(migration_db).get_table_names())

        _alembic(USAGE_DOMAIN_REVISION, command="downgrade")

        assert _current_revision(migration_db) == USAGE_DOMAIN_REVISION
        after = set(inspect(migration_db).get_table_names())
        assert before == after
        assert USAGE_DOMAIN_TABLES <= after


class TestMigrationRuntimeParity:
    """Céluma 1.3, Phase 4, Block C remediation — a release-time guard,
    not a promise that these two will always agree.

    `v1_11_0` now freezes its own copy of the Céluma 1.3 billable-storage
    calculation (see that revision's module docstring). This class proves
    the frozen SQL and the current
    `app.services.storage_billing.StorageBillingService` agree, for the
    schema as it exists *today*. If a future release legitimately changes
    billable semantics, `StorageBillingService` will change and this test
    will start failing — that is the intended signal that the new
    semantics need their own migration/reconciliation step to transition
    existing `TenantUsage` rows explicitly, not a signal that `v1_11_0`
    itself needs to change (it must not, once externally released — see
    the revision's own docstring).
    """

    def test_frozen_baseline_matches_storage_billing_service_across_every_category(
        self, migration_db
    ):
        from sqlmodel import Session as SQLModelSession

        from app.core.config import settings
        from app.services.storage_billing import StorageBillingService

        _alembic(USAGE_DOMAIN_REVISION)
        with migration_db.begin() as conn:
            # ---- Tenant A: one of every category, including the tricky
            # superseded/stale special cases. ----
            tenant_a = _seed_tenant(conn)
            branch_a = _seed_branch(conn, tenant_a)
            patient_a = _seed_patient(conn, tenant_a, branch_a)
            order_a = _seed_order(conn, tenant_a, branch_a, patient_a)
            report_a = _seed_report(conn, tenant_a, branch_a, order_a)
            sample_a = _seed_sample(conn, tenant_a, branch_a, order_a)

            # Sample processed image + thumbnail rendition.
            processed_storage = _seed_storage_object(
                conn, key="samples/a/processed/1.jpg", size_bytes=3000, tenant_id=tenant_a,
            )
            sample_image_a = _seed_sample_image(
                conn, tenant_a, branch_a, sample_a, processed_storage
            )
            thumb_storage = _seed_storage_object(
                conn, key="samples/a/thumb/1.jpg", size_bytes=300, tenant_id=tenant_a,
            )
            _seed_sample_image_rendition(conn, sample_image_a, "thumbnail", thumb_storage)

            # Official PDF — historically superseded by a second generation,
            # both must remain billable (never decremented).
            official_1 = _seed_storage_object(
                conn, key="reports/a/official/1.pdf", size_bytes=5000, tenant_id=tenant_a,
                content_type="application/pdf", sha256_hex="official-hash-1",
            )
            official_2 = _seed_storage_object(
                conn, key="reports/a/official/2.pdf", size_bytes=6000, tenant_id=tenant_a,
                content_type="application/pdf", sha256_hex="official-hash-2",
            )

            # Legacy PDF — a stale, superseded same-version row (must be
            # excluded) plus the currently-referenced one (must count).
            legacy_stale = _seed_storage_object(
                conn, key="reports/a/report.pdf", size_bytes=9_999_999, tenant_id=tenant_a,
                content_type="application/pdf",
            )
            legacy_current = _seed_storage_object(
                conn, key="reports/a/report.pdf", size_bytes=4_000_000, tenant_id=tenant_a,
                content_type="application/pdf",
            )

            # Report JSON.
            json_storage = _seed_storage_object(
                conn, key="reports/a/report.json", size_bytes=800, tenant_id=tenant_a,
                content_type="application/json",
            )
            _seed_report_version(
                conn, report_a, version_no=1,
                pdf_storage_id=legacy_current, json_storage_id=json_storage,
            )
            # official_1/official_2/legacy_stale are deliberately NOT
            # referenced by any report_version.pdf_storage_id — official
            # PDFs count by tenant_id + sha256_hex alone (never via the FK),
            # and legacy_stale is exactly the "superseded, no longer
            # reachable" row the delta rule exists to exclude.

            # Tenant logo — current + superseded.
            base = (settings.media_public_base_url or "").rstrip("/")
            old_logo = _seed_storage_object(
                conn, key="tenants/a/logo/old.png", size_bytes=1500, tenant_id=tenant_a,
                content_type="image/png",
            )
            new_logo = _seed_storage_object(
                conn, key="tenants/a/logo/new.png", size_bytes=2500, tenant_id=tenant_a,
                content_type="image/png",
            )
            conn.execute(
                text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                {"url": f"{base}/tenants/a/logo/new.png", "id": tenant_a},
            )

            # Letterhead/template asset — billable while retained, whether
            # or not any version still references it (the ratified policy).
            letterhead_asset = _seed_storage_object(
                conn, key="report-letterheads/xyz/logos/1.png", size_bytes=1200,
                tenant_id=tenant_a, content_type="image/png",
            )
            template_asset = _seed_storage_object(
                conn, key="report-templates/xyz/logos/1.png", size_bytes=1300,
                tenant_id=tenant_a, content_type="image/png",
            )

            # Live signature.
            sig_storage = _seed_storage_object(
                conn, key="users/a/signature/1.png", size_bytes=64, tenant_id=tenant_a,
                content_type="image/png",
            )
            _seed_app_user(
                conn, tenant_a, email="parity-a@t.example", signature_storage_id=sig_storage,
            )
            # A replaced signature's retained-but-detached PNG is, by
            # construction, not representable as a StorageObject row at all
            # (its row is deleted the moment the user replaces/deletes their
            # signature — see storage-tenant-attribution-contract.md). No
            # seed call is the correct way to represent it: proving the
            # calculation counts only `sig_storage` above already proves
            # nothing else leaks in.

            # ---- Tenant B: zero billable objects. ----
            tenant_b = _seed_tenant(conn)

            # ---- Tenant C: an independent, disjoint slice, to prove
            # multi-tenant isolation in the same run. ----
            tenant_c = _seed_tenant(conn)
            branch_c = _seed_branch(conn, tenant_c)
            patient_c = _seed_patient(conn, tenant_c, branch_c)
            order_c = _seed_order(conn, tenant_c, branch_c, patient_c)
            report_c = _seed_report(conn, tenant_c, branch_c, order_c)
            json_storage_c = _seed_storage_object(
                conn, key="reports/c/report.json", size_bytes=42, tenant_id=tenant_c,
                content_type="application/json",
            )
            _seed_report_version(conn, report_c, version_no=1, json_storage_id=json_storage_c)

        _alembic("head")

        with SQLModelSession(migration_db) as session:
            for tenant_id in (tenant_a, tenant_b, tenant_c):
                frozen_baseline = migration_db.connect().execute(
                    text(
                        "SELECT billable_storage_bytes FROM tenant_usage WHERE tenant_id = :id"
                    ),
                    {"id": tenant_id},
                ).scalar_one()
                runtime_total = StorageBillingService.compute_billable_storage_bytes(
                    session, tenant_id
                )
                assert frozen_baseline == runtime_total, (
                    f"tenant {tenant_id}: migration baseline {frozen_baseline} != "
                    f"runtime StorageBillingService total {runtime_total}"
                )

        # And the numbers are exactly what the seeded fixture implies —
        # not just "equal to each other by coincidence."
        with migration_db.connect() as conn:
            usage = dict(
                conn.execute(
                    text(
                        "SELECT tenant_id, billable_storage_bytes FROM tenant_usage "
                        "WHERE tenant_id IN (:a, :b, :c)"
                    ),
                    {"a": tenant_a, "b": tenant_b, "c": tenant_c},
                ).all()
            )
        expected_a = (
            3000 + 300  # sample processed + thumbnail
            + 5000 + 6000  # both official PDFs, historically superseded included
            + 4_000_000  # only the current legacy PDF, stale excluded
            + 800  # report JSON
            + 2500  # only the current tenant logo, superseded excluded
            + 1200 + 1300  # letterhead + template asset, retained
            + 64  # live signature
        )
        assert usage[tenant_a] == expected_a
        assert usage[tenant_b] == 0
        assert usage[tenant_c] == 42


class TestV1_11_0EnvironmentIndependence:
    """Céluma 1.3, Phase 4, Block D (D0) — the DB-backed half of the
    historical-determinism guarantee.

    The structural guard above proves the revision does not *read* the CDN
    settings. This proves the consequence: the same rows, migrated under two
    deliberately different environments, produce byte-identical
    `TenantUsage` baselines — including for a tenant whose `logo_url` was
    persisted under a CDN hostname neither environment is configured with.

    Two real databases, seeded with the same ids and the same values, each
    migrated in its own subprocess with its own environment.
    """

    ENV_A = {
        "MEDIA_PUBLIC_BASE_URL": "https://cdn-alpha.example",
        "S3_BUCKET_NAME": "bucket-alpha",
        "AWS_REGION": "us-east-1",
    }
    ENV_B = {
        "MEDIA_PUBLIC_BASE_URL": "https://cdn-beta.example",
        "S3_BUCKET_NAME": "bucket-beta",
        "AWS_REGION": "eu-west-1",
    }

    #: The hostname the tenant's logo_url was persisted under — a third
    #: value, matching neither environment. Under the pre-D0 logic this
    #: produced a logo contribution of zero in both.
    PERSISTED_CDN = "https://cdn-at-upload-time.example"

    def _run(self, database: str, env: dict, tenant_id: uuid.UUID) -> int:
        admin = _admin_engine()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{database}"'))
        admin.dispose()

        engine = create_engine(make_url(settings.database_url).set(database=database))
        try:
            _alembic(USAGE_DOMAIN_REVISION, database=database, env=env)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO tenant (id, name, created_at) "
                        "VALUES (:id, 'Determinism', now())"
                    ),
                    {"id": tenant_id},
                )
                logo_key = f"tenants/{tenant_id}/logo/current.png"
                _seed_storage_object(
                    conn,
                    key=logo_key,
                    size_bytes=2500,
                    tenant_id=tenant_id,
                    content_type="image/png",
                )
                conn.execute(
                    text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                    {"url": f"{self.PERSISTED_CDN}/{logo_key}", "id": tenant_id},
                )

            _alembic(STORAGE_ATTRIBUTION_REVISION, database=database, env=env)
            with engine.connect() as conn:
                return conn.execute(
                    text(
                        "SELECT billable_storage_bytes FROM tenant_usage "
                        "WHERE tenant_id = :id"
                    ),
                    {"id": tenant_id},
                ).scalar_one()
        finally:
            engine.dispose()
            admin = _admin_engine()
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            admin.dispose()

    def test_the_same_rows_produce_the_same_baseline_under_different_cdns(self):
        tenant_id = uuid.uuid4()
        baseline_a = self._run("celuma_determinism_a", self.ENV_A, tenant_id)
        baseline_b = self._run("celuma_determinism_b", self.ENV_B, tenant_id)

        assert baseline_a == baseline_b
        # And the shared value is the *correct* one: the logo resolves from
        # DB state alone, so it is counted even though the URL's hostname
        # matches neither environment's configuration.
        assert baseline_a == 2500


class TestTenantLogoBackfill:
    """Céluma 1.3, Phase 4, Block D — `v1_12_0`'s `tenant.logo_storage_id`
    column and its DB-scoped backfill."""

    def _logo_storage_id(self, migration_db, tenant_id):
        with migration_db.connect() as conn:
            return conn.execute(
                text("SELECT logo_storage_id FROM tenant WHERE id = :id"),
                {"id": tenant_id},
            ).scalar_one()

    def test_the_column_and_foreign_key_exist(self, migration_db):
        _alembic("head")
        inspector = inspect(migration_db)
        columns = {c["name"]: c for c in inspector.get_columns("tenant")}
        assert "logo_storage_id" in columns
        assert columns["logo_storage_id"]["nullable"] is True, (
            "unresolved legacy rows must be representable"
        )

        fks = [
            fk
            for fk in inspector.get_foreign_keys("tenant")
            if fk["constrained_columns"] == ["logo_storage_id"]
        ]
        assert len(fks) == 1
        assert fks[0]["referred_table"] == "storage_object"

    def test_the_foreign_key_has_no_destructive_cascade(self, migration_db):
        _alembic("head")
        with migration_db.connect() as conn:
            rule = conn.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conname = 'fk_tenant_logo_storage_id_storage_object'"
                )
            ).scalar_one()
        # 'a' = NO ACTION. Deleting a StorageObject must never silently
        # delete a tenant or blank its identity.
        assert rule == "a"

    def test_a_tenant_with_no_logo_is_left_null(self, migration_db):
        _alembic(STORAGE_ATTRIBUTION_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)

        _alembic("head")

        assert self._logo_storage_id(migration_db, tenant_id) is None

    def test_a_resolvable_logo_is_backfilled(self, migration_db):
        _alembic(STORAGE_ATTRIBUTION_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            key = f"tenants/{tenant_id}/logo/current.png"
            storage_id = _seed_storage_object(
                conn, key=key, size_bytes=1500, tenant_id=tenant_id,
                content_type="image/png",
            )
            conn.execute(
                text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                {"url": f"https://cdn.example/{key}", "id": tenant_id},
            )

        _alembic("head")

        assert self._logo_storage_id(migration_db, tenant_id) == storage_id

    def test_a_logo_stored_under_a_different_cdn_is_still_backfilled(self, migration_db):
        """The backfill reads persisted DB state, never the currently
        configured CDN — so a URL written under a hostname nothing in this
        environment knows about still resolves."""
        _alembic(STORAGE_ATTRIBUTION_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            key = f"tenants/{tenant_id}/logo/legacy.png"
            storage_id = _seed_storage_object(
                conn, key=key, size_bytes=800, tenant_id=tenant_id,
                content_type="image/png",
            )
            conn.execute(
                text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                {"url": f"https://an-old-cdn.example/{key}", "id": tenant_id},
            )

        _alembic(
            "head",
            env={"MEDIA_PUBLIC_BASE_URL": "https://a-completely-different-cdn.example"},
        )

        assert self._logo_storage_id(migration_db, tenant_id) == storage_id

    def test_a_url_with_a_query_string_still_resolves(self, migration_db):
        _alembic(STORAGE_ATTRIBUTION_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            key = f"tenants/{tenant_id}/logo/cached.png"
            storage_id = _seed_storage_object(
                conn, key=key, size_bytes=400, tenant_id=tenant_id,
                content_type="image/png",
            )
            conn.execute(
                text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                {"url": f"https://cdn.example/{key}?v=3#frag", "id": tenant_id},
            )

        _alembic("head")

        assert self._logo_storage_id(migration_db, tenant_id) == storage_id

    def test_another_tenants_object_is_never_selected(self, migration_db):
        """Ownership comes from `storage_object.tenant_id`, not from the
        key string — so a key that happens to name another tenant cannot
        pull that tenant's object into this one's logo FK."""
        _alembic(STORAGE_ATTRIBUTION_REVISION)
        with migration_db.begin() as conn:
            tenant_a = _seed_tenant(conn)
            tenant_b = _seed_tenant(conn)
            key = f"tenants/{tenant_b}/logo/b.png"
            _seed_storage_object(
                conn, key=key, size_bytes=900, tenant_id=tenant_b,
                content_type="image/png",
            )
            # Tenant A's stored URL points at tenant B's object.
            conn.execute(
                text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                {"url": f"https://cdn.example/{key}", "id": tenant_a},
            )

        _alembic("head")

        assert self._logo_storage_id(migration_db, tenant_a) is None
        assert self._logo_storage_id(migration_db, tenant_b) is None

    def test_an_ambiguous_match_is_left_null(self, migration_db):
        """Two of the tenant's own rows carrying the same object_key both
        satisfy the persisted URL. The migration does not pick one."""
        _alembic(STORAGE_ATTRIBUTION_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            key = f"tenants/{tenant_id}/logo/dup.png"
            _seed_storage_object(
                conn, key=key, size_bytes=100, tenant_id=tenant_id,
                content_type="image/png",
            )
            _seed_storage_object(
                conn, key=key, size_bytes=200, tenant_id=tenant_id,
                content_type="image/png",
            )
            conn.execute(
                text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                {"url": f"https://cdn.example/{key}", "id": tenant_id},
            )

        _alembic("head")

        assert self._logo_storage_id(migration_db, tenant_id) is None

    def test_a_superseded_logo_object_is_not_selected(self, migration_db):
        _alembic(STORAGE_ATTRIBUTION_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            old_key = f"tenants/{tenant_id}/logo/old.png"
            new_key = f"tenants/{tenant_id}/logo/new.png"
            _seed_storage_object(
                conn, key=old_key, size_bytes=100, tenant_id=tenant_id,
                content_type="image/png",
            )
            new_id = _seed_storage_object(
                conn, key=new_key, size_bytes=200, tenant_id=tenant_id,
                content_type="image/png",
            )
            conn.execute(
                text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                {"url": f"https://cdn.example/{new_key}", "id": tenant_id},
            )

        _alembic("head")

        assert self._logo_storage_id(migration_db, tenant_id) == new_id

    def test_downgrade_then_re_upgrade_restores_the_backfill(self, migration_db):
        _alembic(STORAGE_ATTRIBUTION_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            key = f"tenants/{tenant_id}/logo/x.png"
            storage_id = _seed_storage_object(
                conn, key=key, size_bytes=321, tenant_id=tenant_id,
                content_type="image/png",
            )
            conn.execute(
                text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                {"url": f"https://cdn.example/{key}", "id": tenant_id},
            )

        _alembic("head")
        assert self._logo_storage_id(migration_db, tenant_id) == storage_id

        _alembic(STORAGE_ATTRIBUTION_REVISION, command="downgrade")
        assert "logo_storage_id" not in {
            c["name"] for c in inspect(migration_db).get_columns("tenant")
        }

        _alembic("head")
        assert self._logo_storage_id(migration_db, tenant_id) == storage_id

    def test_the_backfill_agrees_with_the_frozen_v1_11_0_baseline(self, migration_db):
        """The two must resolve the same object: `v1_11_0` bills the logo it
        resolves, `v1_12_0` records the logo it resolves, and the runtime
        calculation then reads the FK. If they disagreed, a tenant's
        initialized baseline would be permanently out of step with what
        `StorageBillingService` computes on the very next reconciliation."""
        from sqlmodel import Session as SQLModelSession

        from app.services.storage_billing import StorageBillingService

        # Seeded *before* v1_11_0 runs, so the frozen historical baseline is
        # computed over this fixture — the whole point of the comparison.
        _alembic(USAGE_DOMAIN_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            key = f"tenants/{tenant_id}/logo/agreed.png"
            _seed_storage_object(
                conn, key=f"tenants/{tenant_id}/logo/superseded.png",
                size_bytes=9999, tenant_id=tenant_id, content_type="image/png",
            )
            _seed_storage_object(
                conn, key=key, size_bytes=2500, tenant_id=tenant_id,
                content_type="image/png",
            )
            conn.execute(
                text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                {"url": f"https://whatever-cdn.example/{key}", "id": tenant_id},
            )

        _alembic("head")

        with migration_db.connect() as conn:
            baseline = conn.execute(
                text(
                    "SELECT billable_storage_bytes FROM tenant_usage "
                    "WHERE tenant_id = :id"
                ),
                {"id": tenant_id},
            ).scalar_one()
        with SQLModelSession(migration_db) as session:
            runtime = StorageBillingService.compute_billable_storage_bytes(
                session, tenant_id
            )
        assert baseline == runtime == 2500


class TestReconciliationHardeningMigration:
    """Céluma 1.3, Phase 4, Block D — `v1_12_0`'s changes to
    `tenant_usage_reconciliation`."""

    def _insert_run(self, conn, tenant_id, *, status, started_at="now()", **columns):
        run_id = uuid.uuid4()
        extra_names = "".join(f", {name}" for name in columns)
        extra_values = "".join(f", :{name}" for name in columns)
        completed = "NULL" if status == "RUNNING" else "now()"
        conn.execute(
            text(
                f"INSERT INTO tenant_usage_reconciliation "
                f"(id, tenant_id, status, started_at, completed_at{extra_names}) "
                f"VALUES (:id, :tenant_id, :status, {started_at}, {completed}"
                f"{extra_values})"
            ),
            {"id": run_id, "tenant_id": tenant_id, "status": status, **columns},
        )
        return run_id

    def test_metadata_mismatches_column_exists_and_is_nullable(self, migration_db):
        _alembic("head")
        columns = {
            c["name"]: c
            for c in inspect(migration_db).get_columns("tenant_usage_reconciliation")
        }
        assert "metadata_mismatches_found" in columns
        assert columns["metadata_mismatches_found"]["nullable"] is True

    def test_metadata_mismatches_rejects_a_negative_value(self, migration_db):
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
        try:
            with migration_db.begin() as conn:
                self._insert_run(
                    conn,
                    tenant_id,
                    status="SUCCEEDED",
                    metadata_mismatches_found=-1,
                )
            raise AssertionError("a negative mismatch count must be rejected")
        except AssertionError:
            raise
        except Exception as exc:
            assert "metadata_mismatches" in str(exc)

    def test_only_one_running_run_per_tenant_is_representable(self, migration_db):
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            self._insert_run(conn, tenant_id, status="RUNNING")

        try:
            with migration_db.begin() as conn:
                self._insert_run(conn, tenant_id, status="RUNNING")
            raise AssertionError("a second RUNNING run must be rejected")
        except AssertionError:
            raise
        except Exception as exc:
            assert "ix_tenant_usage_reconciliation_one_running" in str(exc)

    def test_terminal_runs_are_unconstrained(self, migration_db):
        """History is append-only and unbounded — the index constrains only
        the *active* run, never how many completed ones a tenant has."""
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            self._insert_run(conn, tenant_id, status="SUCCEEDED")
            self._insert_run(conn, tenant_id, status="SUCCEEDED")
            self._insert_run(conn, tenant_id, status="FAILED")
            self._insert_run(conn, tenant_id, status="RUNNING")

        with migration_db.connect() as conn:
            total = conn.execute(
                text(
                    "SELECT COUNT(*) FROM tenant_usage_reconciliation "
                    "WHERE tenant_id = :id"
                ),
                {"id": tenant_id},
            ).scalar_one()
        assert total == 4

    def test_two_tenants_may_each_have_a_running_run(self, migration_db):
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_a = _seed_tenant(conn)
            tenant_b = _seed_tenant(conn)
            self._insert_run(conn, tenant_a, status="RUNNING")
            self._insert_run(conn, tenant_b, status="RUNNING")

    def test_the_index_is_unique_and_partial(self, migration_db):
        _alembic("head")
        with migration_db.connect() as conn:
            definition = conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
                    "AND indexname = 'ix_tenant_usage_reconciliation_one_running'"
                )
            ).scalar_one()
        assert "CREATE UNIQUE INDEX" in definition
        assert "WHERE ((status)::text = 'RUNNING'::text)" in definition

    def test_downgrade_removes_both_and_re_upgrade_restores_them(self, migration_db):
        _alembic("head")
        _alembic(STORAGE_ATTRIBUTION_REVISION, command="downgrade")

        columns = {
            c["name"]
            for c in inspect(migration_db).get_columns("tenant_usage_reconciliation")
        }
        assert "metadata_mismatches_found" not in columns
        with migration_db.connect() as conn:
            indexes = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                        "AND tablename = 'tenant_usage_reconciliation'"
                    )
                ).all()
            }
        assert "ix_tenant_usage_reconciliation_one_running" not in indexes
        # The Block B indexes must survive a Block D downgrade untouched.
        assert "ix_tenant_usage_reconciliation_tenant_started_at" in indexes
        assert "ix_tenant_usage_reconciliation_status_started_at" in indexes

        _alembic("head")
        assert "metadata_mismatches_found" in {
            c["name"]
            for c in inspect(migration_db).get_columns("tenant_usage_reconciliation")
        }


class TestUsageThresholdStateMigration:
    """Céluma 1.3, Phase 4, Block G — `v1_13_0`, the durable
    usage-threshold-state table.

    Schema only. The single most important assertion in this class is
    `test_creates_no_rows_and_no_notifications`: the revision must arrive on a
    production database with 133 tenants and change nothing but the catalog.
    """

    def _insert_state(self, conn, tenant_id, *, resource="STORAGE", state="NORMAL", **columns):
        state_id = uuid.uuid4()
        extra_names = "".join(f", {name}" for name in columns)
        extra_values = "".join(f", :{name}" for name in columns)
        conn.execute(
            text(
                f"INSERT INTO tenant_usage_threshold_state "
                f"(id, tenant_id, resource, state, created_at, updated_at"
                f"{extra_names}) "
                f"VALUES (:id, :tenant_id, :resource, :state, now(), now()"
                f"{extra_values})"
            ),
            {
                "id": state_id,
                "tenant_id": tenant_id,
                "resource": resource,
                "state": state,
                **columns,
            },
        )
        return state_id

    def test_introduces_exactly_one_table(self, migration_db):
        _alembic(BLOCK_D_REVISION)
        before = set(inspect(migration_db).get_table_names())

        _alembic(BLOCK_G_REVISION)
        after = set(inspect(migration_db).get_table_names())

        assert after - before == THRESHOLD_STATE_TABLES

    def test_creates_no_rows_and_no_notifications(self, migration_db):
        """The revision's load-bearing property.

        A baseline pass inside the migration would either record state without
        notifying — permanently swallowing the first real crossing for every
        tenant already above a threshold — or fan a mail-out across every
        tenant from inside a DDL transaction. It does neither, because it
        reads nothing and writes nothing.
        """
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            conn.execute(
                text(
                    "INSERT INTO tenant_usage (tenant_id, billable_storage_bytes, "
                    "last_updated) VALUES (:id, 950, now())"
                ),
                {"id": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO tenant_limits (tenant_id, storage_limit_bytes, "
                    "user_limit, updated_at) VALUES (:id, 1000, 5, now())"
                ),
                {"id": tenant_id},
            )

        # Re-running the revision must still not evaluate anything: it is DDL.
        _alembic(BLOCK_D_REVISION, command="downgrade")
        _alembic("head")

        with migration_db.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT COUNT(*) FROM tenant_usage_threshold_state")
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(text("SELECT COUNT(*) FROM notification")).scalar_one() == 0
            )

    def test_upgrade_writes_no_rows_and_creates_no_notification(self):
        """Structural, not behavioural: `upgrade()` may not so much as import
        `NotificationService`, and may not write a row of any kind. A future
        edit that adds a "helpful" baseline notification fails here before it
        can reach a database.

        Scoped to `upgrade()` by AST rather than to the whole file, because
        `downgrade()` legitimately deletes the Block G notification rows a
        narrowed CHECK constraint would reject — a different operation with a
        different justification (see the revision's own docstring).
        """
        path = VERSIONS_DIR / "v1_13_0_block_g_usage_threshold_state.py"
        module = ast.parse(path.read_text(encoding="utf-8"))
        upgrade = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        )
        body = ast.unparse(upgrade)

        for forbidden in (
            "NotificationService",
            "notify(",
            "INSERT INTO",
            "UPDATE ",
            "DELETE FROM",
            "tenant_usage",  # never reads the counter either
        ):
            assert forbidden not in body.replace(
                "tenant_usage_threshold_state", "_state_table"
            ), forbidden
        # The docstring is allowed to *discuss* notifications; upgrade() is not.
        assert "notification" in path.read_text(encoding="utf-8").lower()

    def test_the_notification_type_constraints_admit_the_four_new_types(
        self, migration_db
    ):
        """The `VARCHAR` + `CHECK` enum convention's one cost: adding a
        notification type is a constraint change. Missing either table would
        make the feature fail at the first real crossing — `notification` on
        insert, `notification_preference` the first time an admin switched
        email off for one of these types."""
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            _seed_branch(conn, tenant_id)
            user_id = _seed_app_user(conn, tenant_id, email=f"u-{uuid.uuid4().hex[:8]}@lab.test")

        for notification_type in (
            "STORAGE_USAGE_APPROACHING",
            "STORAGE_LIMIT_REACHED",
            "USER_LIMIT_APPROACHING",
            "USER_LIMIT_REACHED",
        ):
            with migration_db.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO notification (id, tenant_id, type, severity, "
                        "title, resource_type, resource_id, idempotency_key, "
                        "locale, created_at) VALUES (:id, :tenant_id, :type, "
                        "'WARNING', 'x', 'tenant', :tenant_id, :key, 'es-MX', now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "type": notification_type,
                        "key": f"{notification_type}:key",
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO notification_preference (id, tenant_id, "
                        "user_id, notification_type, in_app_enabled, "
                        "email_enabled, updated_at) VALUES (:id, :tenant_id, "
                        ":user_id, :type, true, false, now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "type": notification_type,
                    },
                )

        # An invented type is still rejected — the constraint was widened, not
        # dropped.
        with pytest.raises(IntegrityError):
            with migration_db.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO notification (id, tenant_id, type, severity, "
                        "title, resource_type, resource_id, idempotency_key, "
                        "locale, created_at) VALUES (:id, :tenant_id, "
                        "'STORAGE_USAGE_90', 'WARNING', 'x', 'tenant', "
                        ":tenant_id, 'k2', 'es-MX', now())"
                    ),
                    {"id": uuid.uuid4(), "tenant_id": tenant_id},
                )

    def test_downgrade_narrows_the_constraints_and_removes_only_block_g_rows(
        self, migration_db
    ):
        """A downgrade must reproduce `v1_12_0`'s schema exactly, which means
        narrowing the type constraints — which cannot be done while rows carry
        the wider values. The Phase 3 notification history must survive
        untouched; only the four Block G types go."""
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            _seed_branch(conn, tenant_id)
            user_id = _seed_app_user(conn, tenant_id, email=f"u-{uuid.uuid4().hex[:8]}@lab.test")
            kept = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO notification (id, tenant_id, type, severity, "
                    "title, resource_type, resource_id, idempotency_key, locale, "
                    "created_at) VALUES (:id, :tenant_id, 'REPORT_PUBLISHED', "
                    "'INFO', 'kept', 'report', :tenant_id, 'kept-key', 'es-MX', now())"
                ),
                {"id": kept, "tenant_id": tenant_id},
            )
            dropped = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO notification (id, tenant_id, type, severity, "
                    "title, resource_type, resource_id, idempotency_key, locale, "
                    "created_at) VALUES (:id, :tenant_id, 'STORAGE_LIMIT_REACHED', "
                    "'WARNING', 'gone', 'tenant', :tenant_id, 'gone-key', 'es-MX', now())"
                ),
                {"id": dropped, "tenant_id": tenant_id},
            )
            for notification_id in (kept, dropped):
                conn.execute(
                    text(
                        "INSERT INTO notification_recipient (id, notification_id, "
                        "tenant_id, user_id, status, created_at) VALUES (:id, "
                        ":notification_id, :tenant_id, :user_id, 'UNREAD', now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "notification_id": notification_id,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                    },
                )

        _alembic(BLOCK_D_REVISION, command="downgrade")

        with migration_db.connect() as conn:
            remaining = [
                row[0]
                for row in conn.execute(text("SELECT type FROM notification")).all()
            ]
            assert remaining == ["REPORT_PUBLISHED"]
            assert (
                conn.execute(
                    text("SELECT COUNT(*) FROM notification_recipient")
                ).scalar_one()
                == 1
            )
            constraint = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_notification_type'"
                )
            ).scalar_one()
            assert "STORAGE_LIMIT_REACHED" not in constraint
            assert "REPORT_PUBLISHED" in constraint

    def test_columns_nullability_and_defaults(self, migration_db):
        _alembic("head")
        columns = {
            c["name"]: c
            for c in inspect(migration_db).get_columns("tenant_usage_threshold_state")
        }
        assert set(columns) == {
            "id",
            "tenant_id",
            "resource",
            "state",
            "last_value",
            "last_limit",
            "transition_count",
            "last_transition_at",
            "created_at",
            "updated_at",
        }
        for name in ("id", "tenant_id", "resource", "state", "created_at", "updated_at"):
            assert columns[name]["nullable"] is False, name
        # Nullable on purpose: a state that is not evaluable has no numbers,
        # and a zero there would be indistinguishable from a real zero.
        for name in ("last_value", "last_limit", "last_transition_at"):
            assert columns[name]["nullable"] is True, name
        assert columns["transition_count"]["nullable"] is False
        assert "UNMONITORED" in str(columns["state"]["default"])
        assert "0" in str(columns["transition_count"]["default"])

    def test_one_row_per_tenant_and_resource(self, migration_db):
        """The constraint the whole idempotency design rests on: the service's
        `INSERT ... ON CONFLICT DO NOTHING` infers this index, which is what
        serializes two concurrent first evaluations."""
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            self._insert_state(conn, tenant_id, resource="STORAGE")
            # A different resource for the same tenant is fine.
            self._insert_state(conn, tenant_id, resource="USERS")

        with pytest.raises(IntegrityError):
            with migration_db.begin() as conn:
                self._insert_state(conn, tenant_id, resource="STORAGE")

        constraints = {
            c["name"]: c["column_names"]
            for c in inspect(migration_db).get_unique_constraints(
                "tenant_usage_threshold_state"
            )
        }
        assert constraints["uq_tenant_usage_threshold_state_tenant_resource"] == [
            "tenant_id",
            "resource",
        ]

    @pytest.mark.parametrize("resource", ["storage", "REPORTS", "", "Storage"])
    def test_rejects_an_unknown_resource(self, migration_db, resource):
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
        with pytest.raises(IntegrityError):
            with migration_db.begin() as conn:
                self._insert_state(conn, tenant_id, resource=resource)

    @pytest.mark.parametrize("state", ["OVER", "normal", "", "UNKNOWN"])
    def test_rejects_an_unknown_state(self, migration_db, state):
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
        with pytest.raises(IntegrityError):
            with migration_db.begin() as conn:
                self._insert_state(conn, tenant_id, state=state)

    def test_unmonitored_may_not_carry_evaluated_values(self, migration_db):
        """`UNMONITORED` means "not evaluable". A row in that state holding
        the numbers of a real evaluation would be a lie the service could
        later read back as truth."""
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
        with pytest.raises(IntegrityError):
            with migration_db.begin() as conn:
                self._insert_state(
                    conn, tenant_id, state="UNMONITORED", last_value=10, last_limit=100
                )
        # The same row with no values is accepted.
        with migration_db.begin() as conn:
            self._insert_state(conn, tenant_id, state="UNMONITORED")

    @pytest.mark.parametrize(
        "columns",
        [
            {"last_value": -1, "last_limit": 100},
            {"last_value": 10, "last_limit": 0},
            {"last_value": 10, "last_limit": -5},
            {"last_value": 10, "last_limit": 100, "transition_count": -1},
        ],
    )
    def test_rejects_impossible_numbers(self, migration_db, columns):
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
        with pytest.raises(IntegrityError):
            with migration_db.begin() as conn:
                self._insert_state(conn, tenant_id, **columns)

    def test_tenant_fk_has_no_cascade(self, migration_db):
        """Same no-cascade convention as every other table in this domain:
        deleting a tenant that still has threshold state is refused, not
        silently erased."""
        _alembic("head")
        fks = inspect(migration_db).get_foreign_keys("tenant_usage_threshold_state")
        tenant_fk = [fk for fk in fks if fk["referred_table"] == "tenant"]
        assert tenant_fk, "missing tenant_id -> tenant.id foreign key"
        for fk in fks:
            assert not fk.get("options", {}).get("ondelete")

        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            self._insert_state(conn, tenant_id, last_value=10, last_limit=100)
        with pytest.raises(IntegrityError):
            with migration_db.begin() as conn:
                conn.execute(
                    text("DELETE FROM tenant WHERE id = :id"), {"id": tenant_id}
                )

    def test_downgrade_drops_the_table_and_re_upgrade_restores_it(self, migration_db):
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            self._insert_state(conn, tenant_id, last_value=900, last_limit=1000)

        _alembic(BLOCK_D_REVISION, command="downgrade")
        assert _current_revision(migration_db) == BLOCK_D_REVISION
        assert THRESHOLD_STATE_TABLES.isdisjoint(
            set(inspect(migration_db).get_table_names())
        )

        _alembic("head")
        assert _current_revision(migration_db) == BLOCK_G_REVISION
        assert THRESHOLD_STATE_TABLES <= set(inspect(migration_db).get_table_names())
        with migration_db.connect() as conn:
            # Remembered state is lost, which is correct and safe: the next
            # evaluation re-derives it from live usage and limits under
            # first-evaluation semantics. The worst case is one repeated
            # notification for a tenant genuinely above a threshold — never a
            # missed one.
            assert (
                conn.execute(
                    text("SELECT COUNT(*) FROM tenant_usage_threshold_state")
                ).scalar_one()
                == 0
            )


def _seed_tenant(conn) -> uuid.UUID:
    """A minimal tenant row — everything `tenant_usage`/`tenant_limits`/
    `tenant_usage_reconciliation` need to satisfy their tenant_id FK."""
    tenant_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO tenant (id, name, created_at) VALUES (:id, :name, now())"),
        {"id": tenant_id, "name": f"T-{tenant_id.hex[:8]}"},
    )
    return tenant_id


def _seed_branch(conn, tenant_id: uuid.UUID) -> uuid.UUID:
    branch_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO branch (id, tenant_id, code, name, timezone, country, "
            "is_active, created_at) "
            "VALUES (:id, :tenant_id, :code, 'Main', 'America/Mexico_City', 'MX', "
            "true, now())"
        ),
        {"id": branch_id, "tenant_id": tenant_id, "code": f"B-{branch_id.hex[:6]}"},
    )
    return branch_id


def _seed_patient(conn, tenant_id: uuid.UUID, branch_id: uuid.UUID) -> uuid.UUID:
    patient_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO patient (id, tenant_id, branch_id, patient_code, "
            "first_name, last_name, created_at) "
            "VALUES (:id, :tenant_id, :branch_id, :code, 'Jane', 'Doe', now())"
        ),
        {
            "id": patient_id,
            "tenant_id": tenant_id,
            "branch_id": branch_id,
            "code": f"P-{patient_id.hex[:6]}",
        },
    )
    return patient_id


def _seed_order(conn, tenant_id: uuid.UUID, branch_id: uuid.UUID, patient_id: uuid.UUID) -> uuid.UUID:
    order_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO \"order\" (id, tenant_id, branch_id, patient_id, "
            "order_code, status, billed_lock, created_at) "
            "VALUES (:id, :tenant_id, :branch_id, :patient_id, :code, 'RECEIVED', "
            "false, now())"
        ),
        {
            "id": order_id,
            "tenant_id": tenant_id,
            "branch_id": branch_id,
            "patient_id": patient_id,
            "code": f"O-{order_id.hex[:6]}",
        },
    )
    return order_id


def _seed_report(conn, tenant_id: uuid.UUID, branch_id: uuid.UUID, order_id: uuid.UUID) -> uuid.UUID:
    report_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO report (id, tenant_id, branch_id, order_id, status, created_at) "
            "VALUES (:id, :tenant_id, :branch_id, :order_id, 'DRAFT', now())"
        ),
        {"id": report_id, "tenant_id": tenant_id, "branch_id": branch_id, "order_id": order_id},
    )
    return report_id


def _seed_report_version(
    conn,
    report_id: uuid.UUID,
    *,
    version_no: int,
    pdf_storage_id: uuid.UUID | None = None,
    json_storage_id: uuid.UUID | None = None,
) -> uuid.UUID:
    version_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO report_version (id, report_id, version_no, pdf_storage_id, "
            "json_storage_id, authored_at, is_current, created_at) "
            "VALUES (:id, :report_id, :version_no, :pdf_storage_id, :json_storage_id, "
            "now(), true, now())"
        ),
        {
            "id": version_id,
            "report_id": report_id,
            "version_no": version_no,
            "pdf_storage_id": pdf_storage_id,
            "json_storage_id": json_storage_id,
        },
    )
    return version_id


def _seed_sample(conn, tenant_id: uuid.UUID, branch_id: uuid.UUID, order_id: uuid.UUID) -> uuid.UUID:
    sample_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO sample (id, tenant_id, branch_id, order_id, sample_code, "
            "type, state) "
            "VALUES (:id, :tenant_id, :branch_id, :order_id, :code, 'TEJIDO', 'RECEIVED')"
        ),
        {
            "id": sample_id,
            "tenant_id": tenant_id,
            "branch_id": branch_id,
            "order_id": order_id,
            "code": f"S-{sample_id.hex[:6]}",
        },
    )
    return sample_id


def _seed_sample_image(
    conn, tenant_id: uuid.UUID, branch_id: uuid.UUID, sample_id: uuid.UUID, storage_id: uuid.UUID
) -> uuid.UUID:
    sample_image_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO sample_image (id, tenant_id, branch_id, sample_id, storage_id, "
            "is_primary, created_at) "
            "VALUES (:id, :tenant_id, :branch_id, :sample_id, :storage_id, false, now())"
        ),
        {
            "id": sample_image_id,
            "tenant_id": tenant_id,
            "branch_id": branch_id,
            "sample_id": sample_id,
            "storage_id": storage_id,
        },
    )
    return sample_image_id


def _seed_sample_image_rendition(
    conn, sample_image_id: uuid.UUID, kind: str, storage_id: uuid.UUID
) -> uuid.UUID:
    rendition_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO sample_image_rendition (id, sample_image_id, kind, storage_id) "
            "VALUES (:id, :sample_image_id, :kind, :storage_id)"
        ),
        {
            "id": rendition_id,
            "sample_image_id": sample_image_id,
            "kind": kind,
            "storage_id": storage_id,
        },
    )
    return rendition_id


def _seed_storage_object(
    conn,
    *,
    key: str,
    size_bytes: int,
    tenant_id: uuid.UUID | None,
    content_type: str = "image/jpeg",
    sha256_hex: str | None = None,
) -> uuid.UUID:
    storage_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO storage_object (id, provider, region, bucket, object_key, "
            "content_type, size_bytes, sha256_hex, tenant_id, created_at) "
            "VALUES (:id, 'aws', 'mx-test-1', 'celuma-test-bucket', :key, "
            ":content_type, :size_bytes, :sha256_hex, :tenant_id, now())"
        ),
        {
            "id": storage_id,
            "key": key,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256_hex": sha256_hex,
            "tenant_id": tenant_id,
        },
    )
    return storage_id


def _seed_app_user(
    conn, tenant_id: uuid.UUID, *, email: str, signature_storage_id: uuid.UUID | None = None
) -> uuid.UUID:
    user_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO app_user (id, tenant_id, email, full_name, hashed_password, "
            "is_active, signature_storage_id, created_at) "
            "VALUES (:id, :tenant_id, :email, 'Test User', 'x', true, "
            ":signature_storage_id, now())"
        ),
        {
            "id": user_id,
            "tenant_id": tenant_id,
            "email": email,
            "signature_storage_id": signature_storage_id,
        },
    )
    return user_id


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
