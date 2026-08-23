"""Alembic chain integrity tests (Céluma 1.3 — the frozen release contract).

Céluma 1.3 was developed as fourteen revisions on the `celuma-1.3` branch and
ships as one. Three squashes got it there, and all three folded their work
into the same contractual revision, `v1_3_0`:

  - **Phase 2 closure** folded in `v1_3_0 … v1_9_0` (Blocks A–E plus five
    post-Phase-2 remediation rounds) — see
    docs/celuma-1.3/phase-2-closure/alembic-squash-inventory.md.
  - **Phase 3 closure** folded in the notification chain that reused three of
    the identifiers Phase 2 had freed, `v1_4_0 → v1_5_0 → v1_6_0` (Blocks B,
    D and F) — see
    docs/celuma-1.3/phase-3-closure/phase-3-alembic-squash-inventory.md.
  - **Pre-Phase-5 closure** folded in the Phase 4 usage chain,
    `v1_10_0 → v1_11_0 → v1_12_0 → v1_13_0` (Blocks B, C, D and G) — see
    docs/celuma-1.3/pre-phase-5-migration-squash/.

Development history and release history are therefore different, and the
per-block documents under docs/celuma-1.3/ record the former on purpose:

    development history:  v1_3_0 → … → v1_9_0, then v1_10_0 → … → v1_13_0
    release history:      v1_3_0 only

The release contract these tests defend is one revision per product release:

    Céluma 1.0 → v1_0_0    Céluma 1.2 → v1_2_0
    Céluma 1.1 → v1_1_0    Céluma 1.3 → v1_3_0   (frozen)

These tests are the regression net for that decision:

  - the static ones guarantee the chain stays single-headed and linear, that
    the head is the release revision, and that no superseded 1.3 revision id
    can creep back into executable code;
  - the DB-backed ones guarantee the release migration still upgrades a clean
    pre-1.3 database, downgrades without residue — including from a
    *populated* database — and re-upgrades;
  - `TestSchemaEquivalence` pins the whole thing to a captured snapshot of
    what the pre-squash chain produced, so a future edit to `v1_3_0` that
    silently changes the released schema fails loudly.

Why almost nothing here names an intermediate revision any more
---------------------------------------------------------------
Before the pre-Phase-5 squash, the Phase 4 tests were organized by revision:
one class per revision, each upgrading to that revision and asserting its
footprint. Those boundaries no longer exist, so the classes were retargeted
rather than deleted — they now assert that the *final* `v1_3_0` contains the
usage domain, the storage-attribution contract, the DB-scoped logo contract
and the threshold-state contract. The behaviours still matter; which revision
introduced them does not.

One consequence is structural: there is no longer a revision boundary between
"the 1.3 schema exists" and "the 1.3 data migration has run" — the squash
merged them by design. Tests that used to seed rows at an intermediate
revision and then run the data migration now seed at `v1_2_0`, the only
boundary left before the release migration, and upgrade across it. That is a
strictly more realistic exercise: it is the actual 1.2 → 1.3 release
transition, against data shaped the way a real pre-1.3 database shapes it
(`storage_object.tenant_id` absent rather than conveniently pre-populated).

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
import importlib.util
import json
import os
import pathlib
import subprocess
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

#: The one file carrying the Céluma 1.3 release contract. Several guards below
#: are about this file's contents rather than about any revision id.
RELEASE_MIGRATION_PATH = VERSIONS_DIR / "v1_3_0_reports_v2_schema.py"

#: Schema of the pre-squash head (`v1_13_0`), captured from PostgreSQL by
#: scripts/capture_schema_snapshot.py immediately before the four Phase 4
#: revisions were deleted. `TestSchemaEquivalence` compares the squashed
#: migration's output against it. Regenerating this file is not a routine
#: action: it is the frozen evidence that the squash changed the revision
#: identity and nothing else.
PRE_SQUASH_SCHEMA_SNAPSHOT = (
    BACKEND_ROOT / "tests" / "fixtures" / "schema" / "v1_13_0_pre_squash_schema.json"
)

#: The last revision belonging to the release *before* Céluma 1.3 — the
#: revision `main` and tag v1.2.0 carry.
LAST_PRE_1_3_REVISION = "v1_2_0"

#: The single consolidated Céluma 1.3 release revision — and, after the
#: pre-Phase-5 migration squash, the head. It carries the complete 1.3
#: database contract and is **frozen**: Phase 5 validates it and does not
#: rewrite it.
RELEASE_REVISION = "v1_3_0"

#: Revision ids that existed only on the unreleased `celuma-1.3` branch and
#: were folded into RELEASE_REVISION. Nothing executable may reference them.
#:
#: The tuple has grown and shrunk with the development history and is now
#: closed. Phase 2 closure retired `v1_4_0 … v1_9_0`; Phase 3 Blocks B, D and
#: F each removed one entry when they reused a freed id for a live revision
#: (`v1_4_0`, `v1_5_0`, `v1_6_0`); the Phase 3 closure squash put all three
#: back, permanently. The pre-Phase-5 squash added the Phase 4 chain,
#: `v1_10_0 … v1_13_0` — development-time revisions of product version *1.3*,
#: never products 1.10–1.13.
#:
#: Every id here belonged to a revision that never reached production,
#: staging or a customer database, so no `alembic_version` row anywhere
#: carries one. For the Phase 4 four this was verified rather than assumed:
#: `main` (the only branch CI deploys to staging) carried nothing past
#: `v1_2_0`, and the four revision files existed solely in unpushed local
#: commits. See docs/celuma-1.3/pre-phase-5-migration-squash/
#: migration-squash-inventory.md.
SUPERSEDED_REVISIONS = (
    "v1_4_0",
    "v1_5_0",
    "v1_6_0",
    "v1_7_0",
    "v1_8_0",
    "v1_9_0",
    "v1_10_0",
    "v1_11_0",
    "v1_12_0",
    "v1_13_0",
)

#: The Phase 2 report/letterhead objects.
REPORTS_V2_TABLES = {
    "report_template_version",
    "report_letterhead",
    "report_letterhead_version",
}

#: The four notification-domain tables, absorbed from the Phase 3 chain.
NOTIFICATION_TABLES = {
    "notification",
    "notification_recipient",
    "notification_delivery",
    "notification_preference",
}

#: The three usage tables, absorbed from Céluma 1.3, Phase 4, Block B.
USAGE_DOMAIN_TABLES = {
    "tenant_usage",
    "tenant_limits",
    "tenant_usage_reconciliation",
}

#: The threshold-state table, absorbed from Céluma 1.3, Phase 4, Block G.
THRESHOLD_STATE_TABLES = {"tenant_usage_threshold_state"}

#: Every table the release migration introduces on top of `v1_2_0`. The four
#: groupings above are kept separate because individual tests still reason
#: about one domain at a time; this union is the release footprint itself,
#: and it grew with each squash — Phase 3 added the notification tables,
#: the pre-Phase-5 squash added the usage and threshold-state ones.
RELEASE_TABLES = (
    REPORTS_V2_TABLES
    | NOTIFICATION_TABLES
    | USAGE_DOMAIN_TABLES
    | THRESHOLD_STATE_TABLES
)

#: The delivery uniqueness constraint Phase 3 Block B created and Block D
#: dropped. The consolidated migration must never create it: it assumed one
#: address belongs to one person, which silently denied email to the second
#: user sharing a mailbox.
SUPERSEDED_DELIVERY_CONSTRAINT = "uq_notification_delivery_notification_channel_address"

_MIGRATION_TEST_DB = "celuma_migration_test"


def _executable_source(path: pathlib.Path) -> str:
    """Return a module's source stripped of every docstring and comment.

    The release migration documents its own provenance in prose — the module
    docstring names the revisions it consolidates, each section of `upgrade()`
    carries an `ex-v1_x_0` comment so a reader can trace any DDL statement
    back to the block that introduced it, and `downgrade()`'s own docstring
    explains which per-revision inverses the squash collapsed and why. All of
    it is inert. What must never come back is a revision id that some code
    path actually resolves, stamps, or branches on, so the search runs against
    code only.

    Docstrings of *any* kind are stripped, not only the module's. The
    pre-Phase-5 squash is what forced the generalization: collapsing four
    downgrades into one made `downgrade()`'s docstring the natural place to
    record what was collapsed, and a function docstring is exactly as inert as
    a module one. Stripping only the module docstring would have made the
    guard reward moving prose into a comment rather than reward not having a
    live reference — which is not what it is for.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - not our concern here
        return source

    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        # Drop the docstring, keeping the body syntactically valid — a
        # docstring-only function would otherwise unparse to nothing.
        node.body = node.body[1:] or [ast.Pass()]

    # `ast.unparse` emits code and nothing else: comments never survive the
    # parse, and the docstrings were removed above.
    return ast.unparse(ast.fix_missing_locations(tree))


def _load_release_migration():
    """Import the release migration as a module, for tests that need its
    frozen SQL rather than its effects.

    The squash removed the seam these tests used to rely on: with the data
    migration folded into the same revision that creates the schema, there is
    no revision to stop at between "the tables exist" and "the data has been
    transformed". Where a property is genuinely about one SQL statement —
    idempotency guards, the resolution rule for an ambiguous logo — the
    honest replacement is to run that exact statement, taken from the
    migration itself so the test cannot drift away from what ships.

    Loaded by path because `alembic/versions/` is not an importable package.
    """
    spec = importlib.util.spec_from_file_location(
        "_celuma_release_migration", RELEASE_MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


class TestChainShape:
    """Static assertions — no database required."""

    def test_exactly_one_head(self):
        assert len(_script_directory().get_heads()) == 1

    def test_head_is_the_release_revision(self):
        """The permanent release contract: one revision per product release,
        and Céluma 1.3's is `v1_3_0`.

        This assertion moved forward four times while Phase 4 was built
        (`v1_10_0` → `v1_11_0` → `v1_12_0` → `v1_13_0`) because each block
        added a development-time revision on top of the release. The
        pre-Phase-5 squash folded all four back in, and this assertion is now
        expected to *stay* here: `v1_3_0` is frozen, so the next legitimate
        move is Céluma 1.4's own release revision, expected `v1_4_0`.
        """
        assert _script_directory().get_current_head() == RELEASE_REVISION

    def test_release_revision_sits_directly_on_the_last_pre_1_3_revision(self):
        revision = _script_directory().get_revision(RELEASE_REVISION)
        assert revision.down_revision == LAST_PRE_1_3_REVISION

    def test_chain_is_exactly_one_revision_per_product_release(self):
        """The whole point of the squash, as a single assertion: four
        revisions, one per shipped release, base to head, in order."""
        script = _script_directory()
        revisions = list(script.walk_revisions())
        assert [r.revision for r in revisions] == [
            RELEASE_REVISION,
            "v1_2_0",
            "v1_1_0",
            "v1_0_0",
        ]

    def test_release_revision_has_no_children(self):
        """Nothing may be appended to the frozen release revision. A Céluma
        1.4 revision built on `v1_3_0` is exactly what this test is meant to
        catch and force a deliberate decision about — the freeze means the
        next schema change is a release decision, not a quiet `down_revision`.
        """
        script = _script_directory()
        children = [
            revision.revision
            for revision in script.walk_revisions()
            if revision.down_revision == RELEASE_REVISION
        ]
        assert children == []

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
    actually shipped with. See the release migration's module docstring
    ("What the squash collapsed, and what it could not") and
    docs/celuma-1.3/phase-4-block-c/block-c-remediation-report.md.

    These guards were written against the revision that carried the frozen
    baseline SQL at the time (Block C's, then Block D's correction of it).
    The squash moved that SQL into `v1_3_0` without changing a character of
    it, so the guards move with it — the property being defended is a
    property of the SQL and its surroundings, not of a revision id.

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

    def test_the_release_migration_does_not_import_application_services(self):
        modules = self._imported_modules(RELEASE_MIGRATION_PATH)
        offenders = {
            m for m in modules if m == "app.services" or m.startswith("app.services.")
        }
        assert offenders == set(), (
            f"{RELEASE_REVISION} must not import runtime business services; "
            f"found {offenders}"
        )

    def test_the_release_migration_does_not_import_the_usage_service(self):
        modules = self._imported_modules(RELEASE_MIGRATION_PATH)
        assert "app.services.usage" not in modules
        assert not any(m.startswith("app.services.usage") for m in modules)

    def test_the_release_migration_does_not_import_the_storage_billing_service(self):
        modules = self._imported_modules(RELEASE_MIGRATION_PATH)
        assert "app.services.storage_billing" not in modules
        assert not any(m.startswith("app.services.storage_billing") for m in modules)

    def test_the_release_migration_does_not_import_notification_services(self):
        """Added by the pre-Phase-5 squash. The threshold-state schema now
        lives in the same file as the usage baseline, so the "a migration
        must not notify" rule and the "a migration must not compute billing"
        rule are now guarding the same file — worth asserting explicitly
        rather than relying on the blanket `app.services` check to imply it.
        """
        modules = self._imported_modules(RELEASE_MIGRATION_PATH)
        for forbidden in (
            "app.services.notification",
            "app.services.usage_threshold",
            "app.services.email",
            "app.services.s3",
        ):
            assert forbidden not in modules
            assert not any(m.startswith(forbidden) for m in modules)

    def test_no_migration_file_imports_an_application_service(self):
        """The general rule this remediation establishes for every
        migration, not only the release revision — a regression guard against
        the same mistake recurring in a later revision."""
        offenders = []
        for path in sorted(VERSIONS_DIR.glob("*.py")):
            modules = self._imported_modules(path)
            bad = {m for m in modules if m == "app.services" or m.startswith("app.services.")}
            if bad:
                offenders.append((path.name, sorted(bad)))
        assert offenders == []

    def test_the_release_migration_only_imports_stable_primitives(self):
        """Whitelist, not blacklist — proves the migration's import surface
        is exactly the small, stable set intended, not merely "no
        app.services", which a differently-shaped business-logic import (e.g.
        a direct app.models import performing hidden computation) could
        technically satisfy while still reintroducing drift risk.

        `os` was on this list until Block D's D0 correction, because the
        tenant-logo baseline read `os.environ` for the CDN prefix. It is
        deliberately no longer allowed: that read is exactly what made the
        revision environment-dependent.

        `logging` is allowed, and arrived with the squash: the tenant-logo
        backfill logs four aggregate counts. It reads nothing, decides
        nothing, and cannot change what the migration produces.
        """
        modules = self._imported_modules(RELEASE_MIGRATION_PATH)
        allowed_prefixes = ("typing", "alembic", "sqlalchemy", "logging")
        offenders = {
            m for m in modules if not any(m == p or m.startswith(p + ".") for p in allowed_prefixes)
        }
        assert offenders == set(), f"unexpected import surface: {offenders}"

    @pytest.mark.parametrize(
        "setting",
        ["MEDIA_PUBLIC_BASE_URL", "S3_BUCKET_NAME", "AWS_REGION", "os.environ", "getenv"],
    )
    def test_the_release_migration_reads_no_environment_configuration(self, setting):
        """Céluma 1.3, Phase 4, Block D (D0). Historical determinism is not
        only "does not import evolvable code" — it is also "does not read
        mutable configuration". Until D0 the baseline rebuilt the public-URL
        prefix from `MEDIA_PUBLIC_BASE_URL`/`S3_BUCKET_NAME`/`AWS_REGION` to
        interpret a persisted `Tenant.logo_url`, so the same rows and the
        same source could produce a different baseline in an environment
        whose CDN hostname had changed. The current-logo resolution is now
        purely relational, and this guard keeps those settings from
        returning to it.

        Runs against executable source only — the module docstring
        legitimately *discusses* these names.
        """
        source = _executable_source(RELEASE_MIGRATION_PATH)
        assert setting not in source

    def test_no_migration_file_reads_the_cdn_base_url(self):
        """The standing rule D0 establishes for every revision: a migration's
        result must not depend on which CDN happens to be configured when it
        runs."""
        offenders = [
            path.name
            for path in sorted(VERSIONS_DIR.glob("*.py"))
            if "MEDIA_PUBLIC_BASE_URL" in _executable_source(path)
        ]
        assert offenders == []

    def test_the_release_migration_creates_no_notification(self):
        """Céluma 1.3, Phase 4, Block G's central migration-safety property,
        preserved verbatim through the squash: the migration adds the
        threshold-state *schema* and evaluates nothing.

        A source-level guard because it is a statement about what the
        migration is incapable of, not only about what it happened not to do
        on the fixtures a DB-backed test provides.
        `TestUsageThresholdStateContract` asserts the runtime half — that a
        migrated database really does contain zero notifications and zero
        threshold-state rows.
        """
        source = _executable_source(RELEASE_MIGRATION_PATH)
        for table in ("tenant_usage_threshold_state", "notification_recipient"):
            assert f"INSERT INTO {table}" not in source
        # `tenant_usage` is the one table the migration legitimately inserts
        # into (the Block C baseline), so the check above is deliberately
        # per-table rather than a blanket "no INSERT".
        assert "INSERT INTO notification" not in source


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

        The expected set grew with the pre-Phase-5 squash, which is the whole
        point of it: the usage domain and threshold state are no longer three
        revisions further along, they are part of the release."""
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


class TestUsageDomainMigration:
    """Céluma 1.3, Phase 4, Block B — the usage domain, now part of the
    consolidated release revision. Mirrors the discipline
    `TestNotificationDomain` established: the chain tests above prove the
    revision is reachable and reversible; these prove it created the right
    tables, columns, constraints and indexes.

    Retargeted by the pre-Phase-5 squash from "the usage-domain revision
    introduces these" to "the final `v1_3_0` contains these". The assertions
    below are unchanged in substance — what disappeared is the revision
    boundary they used to be measured against.
    """

    def test_upgrade_creates_the_three_usage_tables(self, migration_db):
        _alembic(LAST_PRE_1_3_REVISION)
        assert USAGE_DOMAIN_TABLES.isdisjoint(
            set(inspect(migration_db).get_table_names())
        )

        _alembic("head")
        assert USAGE_DOMAIN_TABLES <= set(inspect(migration_db).get_table_names())

    # "These tables and nothing else" is asserted once, for the whole release
    # footprint, by TestReleaseMigration::
    # test_release_introduces_exactly_the_expected_tables. It used to live
    # here as well, scoped to the usage revision — a per-revision exact-set
    # match that had to be edited every time a later block added a table.
    # There is one release footprint now, so there is one such test.

    def test_downgrade_drops_every_usage_table(self, migration_db):
        _alembic("head")
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")

        assert _current_revision(migration_db) == LAST_PRE_1_3_REVISION
        assert USAGE_DOMAIN_TABLES.isdisjoint(
            set(inspect(migration_db).get_table_names())
        )

    def test_downgrade_then_re_upgrade_restores_the_tables(self, migration_db):
        _alembic("head")
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
        _alembic("head")

        assert _current_revision(migration_db) == RELEASE_REVISION
        assert USAGE_DOMAIN_TABLES <= set(inspect(migration_db).get_table_names())

    def test_downgrade_removes_the_app_user_index(self, migration_db):
        """Confirmed absent before this revision (module docstring) — must
        also be confirmed absent again after a downgrade."""
        _alembic("head")
        indexes_before = {
            i["name"] for i in inspect(migration_db).get_indexes("app_user")
        }
        assert "ix_app_user_tenant_id_is_active" in indexes_before

        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
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

        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")

        assert _current_revision(migration_db) == LAST_PRE_1_3_REVISION
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
    """Céluma 1.3, Phase 4, Block C — the storage-attribution and
    usage-initialization contract, now section 15 of the release migration.
    These tests exercise the backfill and initialization logic against a
    populated database, the same way `TestUsageDomainMigration` proves the
    usage schema rather than merely that it is reachable.
    """

    def test_backfills_tenant_id_for_the_four_gapped_categories(self, migration_db):
        _alembic(LAST_PRE_1_3_REVISION)
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

    # The "backfill never overwrites an existing tenant_id" guard moved to
    # test_the_backfill_never_overwrites_an_existing_attribution below. It
    # used to seed a pre-attributed row at an intermediate revision; after
    # the squash there is no such revision, because `storage_object.tenant_id`
    # and the backfill that populates it now arrive together. The guard is
    # exercised against the migration's own frozen statement instead.

    def test_initializes_usage_with_the_computed_baseline_per_tenant(self, migration_db):
        _alembic(LAST_PRE_1_3_REVISION)
        with migration_db.begin() as conn:
            tenant_a = _seed_tenant(conn)
            tenant_b = _seed_tenant(conn)
            tenant_c = _seed_tenant(conn)  # zero billable objects

            branch_a = _seed_branch(conn, tenant_a)
            patient_a = _seed_patient(conn, tenant_a, branch_a)
            order_a = _seed_order(conn, tenant_a, branch_a, patient_a)
            report_a = _seed_report(conn, tenant_a, branch_a, order_a)

            # Tenant A: one official PDF (sha256_hex set) — 10 bytes.
            # Seeded unattributed, as a pre-1.3 row necessarily is; the
            # backfill attributes it via report_version.pdf_storage_id, and
            # only then does the official-PDF category count it.
            official_storage = _seed_storage_object(
                conn, key="reports/a/official/1.pdf", size_bytes=10, tenant_id=None,
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

    def test_initialization_is_deterministic_across_a_downgrade_and_re_upgrade(
        self, migration_db
    ):
        """Replay safety: the baseline must be reproducible, not merely
        correct once.

        Downgrading to `v1_2_0` and upgrading again genuinely re-executes the
        whole release migration — including the `tenant_id` backfill, whose
        results the downgrade discarded along with the column. The second run
        therefore starts from the same raw fixture as the first and has to
        arrive at the same number. That is a stronger claim than the
        pre-squash version of this test could make: it used to downgrade to
        the revision *below* the data migration, leaving the tenant_id
        backfill's output in place, so only the baseline INSERT was replayed.

        It is also what makes the `INSERT ... WHERE NOT EXISTS` guard
        observable — `tenant_usage` is dropped and rebuilt, so the row count
        assertion proves the insert is singular, not that the table was
        merely left alone.
        """
        _alembic(LAST_PRE_1_3_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            branch_id = _seed_branch(conn, tenant_id)
            patient_id = _seed_patient(conn, tenant_id, branch_id)
            order_id = _seed_order(conn, tenant_id, branch_id, patient_id)
            report_id = _seed_report(conn, tenant_id, branch_id, order_id)
            # Official PDF: sha256_hex set, tenant_id absent (the column does
            # not exist yet). The backfill attributes it via
            # report_version.pdf_storage_id -> report.tenant_id, after which
            # the official-PDF category counts it.
            official_storage = _seed_storage_object(
                conn, key="reports/idem/official/1.pdf", size_bytes=4096, tenant_id=None,
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

        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
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

    def test_the_backfill_never_overwrites_an_existing_attribution(self, migration_db):
        """The `IS NULL` guard on every backfill statement, asserted directly.

        Pre-squash this was implicit in the ordering of two revisions. Post-
        squash there is no seam to seed a pre-attributed row at, so the guard
        is exercised where it actually lives: by running the migration's own
        frozen SQL a second time, against rows that already carry a
        tenant_id, and proving nothing moves. Same statements the migration
        executes — imported, not retyped, so they cannot drift apart.
        """
        _alembic("head")
        with migration_db.begin() as conn:
            owner = _seed_tenant(conn)
            squatter = _seed_tenant(conn)
            branch_id = _seed_branch(conn, owner)
            patient_id = _seed_patient(conn, owner, branch_id)
            order_id = _seed_order(conn, owner, branch_id, patient_id)
            sample_id = _seed_sample(conn, owner, branch_id, order_id)
            # Deliberately attributed to the *wrong* tenant. The backfill
            # would derive `owner` from sample_image; the guard must stop it.
            storage_id = _seed_storage_object(
                conn, key="samples/guard/a.jpg", size_bytes=10, tenant_id=squatter
            )
            _seed_sample_image(conn, owner, branch_id, sample_id, storage_id)

        migration = _load_release_migration()
        with migration_db.begin() as conn:
            conn.execute(text(migration._BACKFILL_SAMPLE_IMAGES))

        with migration_db.connect() as conn:
            attributed = conn.execute(
                text("SELECT tenant_id FROM storage_object WHERE id = :id"),
                {"id": storage_id},
            ).scalar_one()
        assert attributed == squatter, (
            "backfill overwrote an existing attribution; the IS NULL guard is gone"
        )


class TestMigrationRuntimeParity:
    """Céluma 1.3, Phase 4, Block C remediation — a release-time guard,
    not a promise that these two will always agree.

    The release migration freezes its own copy of the Céluma 1.3 billable-storage
    calculation (see that revision's module docstring). This class proves
    the frozen SQL and the current
    `app.services.storage_billing.StorageBillingService` agree, for the
    schema as it exists *today*. If a future release legitimately changes
    billable semantics, `StorageBillingService` will change and this test
    will start failing — that is the intended signal that the new
    semantics need their own migration/reconciliation step to transition
    existing `TenantUsage` rows explicitly, not a signal that the migration
    itself needs to change (it must not, once externally released — see
    the revision's own docstring).
    """

    def test_frozen_baseline_matches_storage_billing_service_across_every_category(
        self, migration_db
    ):
        from sqlmodel import Session as SQLModelSession

        from app.core.config import settings
        from app.services.storage_billing import StorageBillingService

        # Seeded at head, with attribution, because several of the categories
        # below are only expressible that way: official PDFs count by
        # tenant_id + sha256_hex and are reachable from no FK, and
        # letterhead/template assets and the tenant logo count by tenant_id +
        # key prefix. A pre-1.3 fixture cannot carry any of that — the column
        # does not exist at `v1_2_0` — so this test seeds the way Céluma 1.3
        # writes storage objects and then runs the migration's own frozen
        # baseline statement over it. `TestRealisticUpgradeFromCeluma12`
        # covers the complementary case: what the same statement does to a
        # genuinely unattributed 1.2 database.
        _alembic("head")
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

        # Both frozen statements, in the order section 15 runs them. The
        # logo backfill is not optional here: the runtime calculation reads
        # `tenant.logo_storage_id`, so skipping it would leave the runtime
        # side blind to a logo the frozen baseline had already billed, and
        # the two would disagree by exactly the logo's size — a difference
        # in the fixture, not in the contract under test.
        migration = _load_release_migration()
        with migration_db.begin() as conn:
            conn.execute(text(migration._TENANT_USAGE_BASELINE_INSERT))
            conn.execute(text(migration._BACKFILL_LOGO_STORAGE_ID))

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


class TestMigrationEnvironmentIndependence:
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
            # Migrated under `env`, then seeded with an attributed logo (the
            # only shape in which a logo is billable at all) and the frozen
            # baseline run under that same environment. What is being proved
            # is that `env` cannot influence the number: the resolution rule
            # compares a persisted URL against a persisted object_key and
            # reads no setting, so a URL stored under a third hostname that
            # neither environment knows about still resolves in both.
            _alembic("head", database=database, env=env)
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

            migration = _load_release_migration()
            with engine.begin() as conn:
                conn.execute(text(migration._TENANT_USAGE_BASELINE_INSERT))

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
    """Céluma 1.3, Phase 4, Block D — the `tenant.logo_storage_id` column
    and its DB-scoped backfill, now part of the release migration."""

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

    def _resolve(self, migration_db, tenant_id):
        """Run the migration's own logo-resolution SQL and report the result.

        The resolution rule needs a `storage_object` that already carries a
        `tenant_id`, because ownership is relational and never inferred from
        the key. Only Céluma 1.3 writes such a row — the column does not
        exist at `v1_2_0` — so after the squash there is no revision to seed
        one at *before* the backfill runs. These tests therefore seed at head
        and execute the frozen statement directly, taken from the migration
        module so it cannot drift from what ships.

        `test_a_pre_1_3_logo_is_left_unresolved` below covers the other half:
        what the backfill does on a real upgrading database.
        """
        migration = _load_release_migration()
        with migration_db.begin() as conn:
            conn.execute(text(migration._BACKFILL_LOGO_STORAGE_ID))
        return self._logo_storage_id(migration_db, tenant_id)

    def test_a_pre_1_3_logo_is_left_unresolved(self, migration_db):
        """A real Céluma 1.2 database upgrading to 1.3 gets `NULL`, and that
        is the accepted contract rather than a defect in this test.

        At `v1_2_0` there is no `storage_object.tenant_id` at all, so after
        the column is added every pre-existing object is unattributed. The
        backfill only attributes the four categories reachable from a parent
        row (sample images, report JSON/legacy PDFs, signatures); tenant
        logos are not among them, because they were specified as "already
        attributed at write time" — which is true of 1.3-era writes and false
        of everything older. So the logo stays unowned, the resolution rule
        finds no candidate, and `logo_storage_id` stays NULL.

        This behaviour is identical before and after the pre-Phase-5 squash —
        verified by running both chains over this same fixture, see
        docs/celuma-1.3/pre-phase-5-migration-squash/
        migration-schema-equivalence-report.md §"Upgrade-path parity". It is
        carried into Phase 5 as known, non-blocking debt: reconciliation
        reports these tenants as `legacy_logo_reference_unresolved`, and
        re-uploading a logo repairs the row through the normal 1.3 write
        path. Asserted explicitly so that if a later change starts resolving
        them, that is a deliberate decision and not a silent one.
        """
        _alembic(LAST_PRE_1_3_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            key = f"tenants/{tenant_id}/logo/current.png"
            _seed_storage_object(
                conn, key=key, size_bytes=1500, tenant_id=None,
                content_type="image/png",
            )
            conn.execute(
                text("UPDATE tenant SET logo_url = :url WHERE id = :id"),
                {"url": f"https://cdn.example/{key}", "id": tenant_id},
            )

        _alembic("head")

        assert self._logo_storage_id(migration_db, tenant_id) is None
        with migration_db.connect() as conn:
            attributed = conn.execute(
                text(
                    "SELECT tenant_id FROM storage_object WHERE object_key = :k"
                ),
                {"k": key},
            ).scalar_one()
        assert attributed is None, (
            "tenant logos are outside the backfill's four categories"
        )

    def test_a_tenant_with_no_logo_is_left_null(self, migration_db):
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)

        assert self._resolve(migration_db, tenant_id) is None

    def test_a_resolvable_logo_is_backfilled(self, migration_db):
        _alembic("head")
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

        assert self._resolve(migration_db, tenant_id) == storage_id

    def test_a_logo_stored_under_a_different_cdn_is_still_backfilled(self, migration_db):
        """The backfill reads persisted DB state, never the currently
        configured CDN — so a URL written under a hostname nothing in this
        environment knows about still resolves."""
        _alembic("head")
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

        assert self._resolve(migration_db, tenant_id) == storage_id

    def test_a_url_with_a_query_string_still_resolves(self, migration_db):
        _alembic("head")
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

        assert self._resolve(migration_db, tenant_id) == storage_id

    def test_another_tenants_object_is_never_selected(self, migration_db):
        """Ownership comes from `storage_object.tenant_id`, not from the
        key string — so a key that happens to name another tenant cannot
        pull that tenant's object into this one's logo FK."""
        _alembic("head")
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

        assert self._resolve(migration_db, tenant_a) is None
        assert self._logo_storage_id(migration_db, tenant_b) is None

    def test_an_ambiguous_match_is_left_null(self, migration_db):
        """Two of the tenant's own rows carrying the same object_key both
        satisfy the persisted URL. The migration does not pick one."""
        _alembic("head")
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

        assert self._resolve(migration_db, tenant_id) is None

    def test_a_superseded_logo_object_is_not_selected(self, migration_db):
        _alembic("head")
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

        assert self._resolve(migration_db, tenant_id) == new_id

    def test_the_backfill_is_idempotent(self, migration_db):
        """Guarded by `t.logo_storage_id IS NULL`: a second run must neither
        change a resolved row nor fail."""
        _alembic("head")
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

        assert self._resolve(migration_db, tenant_id) == storage_id
        assert self._resolve(migration_db, tenant_id) == storage_id

    def test_downgrade_then_re_upgrade_restores_the_column(self, migration_db):
        """`logo_url` is untouched by the release migration, so the FK it is
        derived from recomputes identically after a full downgrade."""
        _alembic("head")
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
        assert "logo_storage_id" not in {
            c["name"] for c in inspect(migration_db).get_columns("tenant")
        }

        _alembic("head")
        assert "logo_storage_id" in {
            c["name"] for c in inspect(migration_db).get_columns("tenant")
        }

    def test_the_backfill_agrees_with_the_frozen_usage_baseline(self, migration_db):
        """The two must resolve the same object: the baseline bills the logo
        it resolves, the backfill records the logo it resolves, and the
        runtime calculation then reads the FK. If they disagreed, a tenant's
        initialized baseline would be permanently out of step with what
        `StorageBillingService` computes on the very next reconciliation.

        Both frozen statements are executed here, in the order the migration
        runs them, over a fixture seeded the way 1.3 writes storage objects
        (attributed). That is what makes the comparison meaningful: on an
        unattributed pre-1.3 fixture both sides agree trivially at zero.
        """
        from sqlmodel import Session as SQLModelSession

        from app.services.storage_billing import StorageBillingService

        _alembic("head")
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

        migration = _load_release_migration()
        with migration_db.begin() as conn:
            # The upgrade already inserted a zero row for no tenant at all
            # (the DB was empty then), so the baseline INSERT is re-run here
            # over the seeded fixture. `WHERE NOT EXISTS` makes that safe.
            conn.execute(text(migration._TENANT_USAGE_BASELINE_INSERT))
            conn.execute(text(migration._BACKFILL_LOGO_STORAGE_ID))

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
    """Céluma 1.3, Phase 4, Block D — the reconciliation hardening applied to
    `tenant_usage_reconciliation`, now part of the release migration."""

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
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")

        # The whole table goes with the 1.3 delta now, so "the column is
        # gone" is asserted as "the table is gone" — the release migration
        # owns both, and there is no intermediate state where one outlives
        # the other.
        assert "tenant_usage_reconciliation" not in set(
            inspect(migration_db).get_table_names()
        )
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
        assert indexes == set()

        _alembic("head")
        assert "metadata_mismatches_found" in {
            c["name"]
            for c in inspect(migration_db).get_columns("tenant_usage_reconciliation")
        }


class TestUsageThresholdStateMigration:
    """Céluma 1.3, Phase 4, Block G — the durable usage-threshold-state
    table, now part of the release migration.

    Schema only. The single most important assertion in this class is
    `test_creates_no_rows_and_no_notifications`: the migration must arrive on
    a production database with 133 tenants and add this table without
    evaluating a single threshold.
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
        _alembic(LAST_PRE_1_3_REVISION)
        before = set(inspect(migration_db).get_table_names())

        _alembic("head")
        after = set(inspect(migration_db).get_table_names())

        assert THRESHOLD_STATE_TABLES <= after - before

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

        # Replaying the release migration must still not evaluate anything.
        # The seeded usage/limits rows go with the downgrade, so this also
        # proves the re-upgrade does not resurrect state from anywhere.
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
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

    def test_upgrade_creates_no_notification_and_no_threshold_state_row(self):
        """Structural, not behavioural: `upgrade()` may not so much as import
        `NotificationService`, and may not write a notification or a
        threshold-state row. A future edit that adds a "helpful" baseline
        notification fails here before it can reach a database.

        The guard narrowed with the squash, and had to. Before it, this
        revision was pure DDL and the check could simply forbid every write
        verb. The migration now also carries the Block C data migration,
        which legitimately runs `UPDATE storage_object` and `INSERT INTO
        tenant_usage` — so a blanket "no writes" assertion would forbid the
        thing the release migration exists to do. What must stay impossible
        is narrower and is what this now states: no write to the notification
        domain, and no write to the threshold-state table.

        Scoped to the whole executable module rather than to `upgrade()`'s
        AST subtree, because the SQL lives in module-level constants — an
        unparsed `upgrade()` shows `op.execute(_BACKFILL_SAMPLE_IMAGES)` and
        no SQL at all, which would make every assertion here vacuous. The
        wider scope is also now exact rather than a compromise: after the
        squash `downgrade()` contains no DML either, since it drops the
        notification tables outright instead of deleting rows from them.
        """
        source = _executable_source(RELEASE_MIGRATION_PATH)

        for forbidden in (
            "NotificationService",
            "notify(",
            "INSERT INTO notification",
            "INSERT INTO tenant_usage_threshold_state",
            "UPDATE notification",
            "DELETE FROM notification",
        ):
            assert forbidden not in source, forbidden

        # The writes it *is* allowed to make, asserted positively so the
        # narrowing above cannot quietly become "asserts nothing".
        assert "INSERT INTO tenant_usage" in source
        assert "UPDATE storage_object" in source

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

    def test_downgrade_removes_the_whole_notification_domain(self, migration_db):
        """What the constraint-narrowing downgrade became after the squash.

        The pre-squash chain had to narrow `ck_notification_type` back to six
        values when stepping down one revision, and — because a CHECK is
        validated against existing rows — had to delete every usage-threshold
        notification, recipient and delivery first, while carefully sparing
        the Phase 3 clinical history one revision below.

        None of that survives, and it should not: the only downgrade now is
        `v1_3_0 -> v1_2_0`, which drops the notification tables outright.
        Transcribing the narrowing step would have been a delete of rows
        immediately followed by a drop of the tables holding them. This test
        replaces it with the assertion that actually matters — populated
        notification history, of both kinds, does not block the downgrade and
        does not survive it.
        """
        _alembic("head")
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            _seed_branch(conn, tenant_id)
            user_id = _seed_app_user(conn, tenant_id, email=f"u-{uuid.uuid4().hex[:8]}@lab.test")
            clinical = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO notification (id, tenant_id, type, severity, "
                    "title, resource_type, resource_id, idempotency_key, locale, "
                    "created_at) VALUES (:id, :tenant_id, 'REPORT_PUBLISHED', "
                    "'INFO', 'kept', 'report', :tenant_id, 'kept-key', 'es-MX', now())"
                ),
                {"id": clinical, "tenant_id": tenant_id},
            )
            threshold = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO notification (id, tenant_id, type, severity, "
                    "title, resource_type, resource_id, idempotency_key, locale, "
                    "created_at) VALUES (:id, :tenant_id, 'STORAGE_LIMIT_REACHED', "
                    "'WARNING', 'gone', 'tenant', :tenant_id, 'gone-key', 'es-MX', now())"
                ),
                {"id": threshold, "tenant_id": tenant_id},
            )
            for notification_id in (clinical, threshold):
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

        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")

        tables = set(inspect(migration_db).get_table_names())
        assert NOTIFICATION_TABLES.isdisjoint(tables)
        assert THRESHOLD_STATE_TABLES.isdisjoint(tables)
        # The tenant and user the history hung off are pre-1.3 and must
        # outlive it — the downgrade drops 1.3's tables, not the lab's data.
        with migration_db.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT COUNT(*) FROM app_user WHERE id = :id"),
                    {"id": user_id},
                ).scalar_one()
                == 1
            )

    def test_the_release_migration_never_creates_a_six_value_type_constraint(self):
        """§13 of the squash contract, as an anti-assertion.

        The final constraint is created once, in its ten-value form. Creating
        the six-value form and altering it would reproduce a development-time
        intermediate state that no released database should ever pass
        through — the same discipline that keeps the superseded
        address-keyed delivery constraint out of this migration.
        """
        source = _executable_source(RELEASE_MIGRATION_PATH)
        # Named exactly once each: at creation. A create-then-widen would
        # name them at least twice.
        assert source.count("ck_notification_type") == 1
        assert source.count("ck_notification_preference_type") == 1

    def test_the_release_migration_admits_all_ten_notification_types(
        self, migration_db
    ):
        """The final set: the six Phase 3 clinical types plus the four
        usage-threshold types, on both notification-domain constraints."""
        _alembic("head")
        expected = {
            "REPORT_SUBMITTED",
            "REPORT_PDF_READY",
            "REPORT_PUBLISHED",
            "REPORT_RETRACTED",
            "ASSIGNMENT_ADDED",
            "SAMPLE_STATUS_CHANGED",
            "STORAGE_USAGE_APPROACHING",
            "STORAGE_LIMIT_REACHED",
            "USER_LIMIT_APPROACHING",
            "USER_LIMIT_REACHED",
        }
        with migration_db.connect() as conn:
            for name in ("ck_notification_type", "ck_notification_preference_type"):
                definition = conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = :name"
                    ),
                    {"name": name},
                ).scalar_one()
                for value in expected:
                    assert value in definition, f"{value} missing from {name}"

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

        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
        assert _current_revision(migration_db) == LAST_PRE_1_3_REVISION
        assert THRESHOLD_STATE_TABLES.isdisjoint(
            set(inspect(migration_db).get_table_names())
        )

        _alembic("head")
        assert _current_revision(migration_db) == RELEASE_REVISION
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


def _capture_schema(database: str) -> dict:
    """Normalized schema of `database`, as scripts/capture_schema_snapshot.py
    would write it. Imported from the script rather than reimplemented, so
    the fixture and the comparison can never diverge in how they normalize.
    """
    spec = importlib.util.spec_from_file_location(
        "_celuma_schema_snapshot", BACKEND_ROOT / "scripts" / "capture_schema_snapshot.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.capture(database)


def _diff_schemas(pre: dict, post: dict) -> list[str]:
    """Every difference between two captured schemas, as readable strings."""
    differences: list[str] = []
    pre_tables, post_tables = pre["tables"], post["tables"]

    for name in sorted(set(pre_tables) | set(post_tables)):
        if name not in pre_tables:
            differences.append(f"table only after squash: {name}")
            continue
        if name not in post_tables:
            differences.append(f"table only before squash: {name}")
            continue
        for kind in ("columns", "constraints", "indexes"):
            before, after = pre_tables[name][kind], post_tables[name][kind]
            for key in sorted(set(before) | set(after)):
                if key not in before:
                    differences.append(f"{name}.{kind}: only after squash: {key}")
                elif key not in after:
                    differences.append(f"{name}.{kind}: only before squash: {key}")
                elif before[key] != after[key]:
                    differences.append(
                        f"{name}.{kind}.{key}: {before[key]!r} -> {after[key]!r}"
                    )
    return differences


class TestSchemaEquivalence:
    """The load-bearing proof of the pre-Phase-5 migration squash.

    The claim the squash rests on is narrow and total: upgrading a fresh
    database to the squashed `v1_3_0` produces *exactly* the schema the
    `v1_3_0 -> v1_10_0 -> v1_11_0 -> v1_12_0 -> v1_13_0` chain produced —
    every table, column, type, nullability, default, primary key, foreign
    key, unique constraint, CHECK constraint and index. Not "equivalent in
    the parts we remembered to check": identical, compared field by field
    against a snapshot captured from PostgreSQL before the four revisions
    were deleted.

    The snapshot deliberately excludes `alembic_version`. That table is the
    one thing the squash is *supposed* to change — from `v1_13_0` to
    `v1_3_0` — and including it would turn the intended difference into a
    failure. It also excludes OIDs, sizes and physical ordering, which vary
    between two runs of the same DDL and mean nothing.

    If this test fails, the release migration no longer produces the schema
    Phase 4 signed off on, and that is a release-blocking event rather than a
    test to update.
    """

    def test_the_snapshot_fixture_exists(self):
        assert PRE_SQUASH_SCHEMA_SNAPSHOT.exists(), (
            "the pre-squash schema snapshot is the evidence for the whole "
            "squash; regenerate it only with a deliberate decision"
        )

    def test_squashed_schema_matches_the_pre_squash_head(self, migration_db):
        _alembic("head")

        pre = json.loads(PRE_SQUASH_SCHEMA_SNAPSHOT.read_text(encoding="utf-8"))
        post = _capture_schema(_MIGRATION_TEST_DB)

        differences = _diff_schemas(pre, post)
        assert differences == [], (
            "the squashed v1_3_0 no longer reproduces the pre-squash schema:\n  "
            + "\n  ".join(differences)
        )

    def test_the_comparison_is_actually_comparing_something(self, migration_db):
        """A guard on the guard.

        A snapshot that silently captured nothing — wrong database name, an
        empty result — would make the test above pass vacuously and prove
        exactly nothing. These floors are far below the real numbers (47
        tables, 438 columns, 214 constraints, 145 indexes at the time of the
        squash) and exist only to catch a comparison that has stopped
        happening.
        """
        _alembic("head")
        post = _capture_schema(_MIGRATION_TEST_DB)
        tables = post["tables"]

        assert len(tables) > 40
        assert sum(len(t["columns"]) for t in tables.values()) > 400
        assert sum(len(t["constraints"]) for t in tables.values()) > 200
        assert sum(len(t["indexes"]) for t in tables.values()) > 130
        # And the Phase 4 additions specifically, since they are what the
        # squash moved.
        assert (USAGE_DOMAIN_TABLES | THRESHOLD_STATE_TABLES) <= set(tables)
        assert "logo_storage_id" in tables["tenant"]["columns"]
        assert (
            "metadata_mismatches_found"
            in tables["tenant_usage_reconciliation"]["columns"]
        )

    def test_alembic_version_is_the_only_intended_difference(self, migration_db):
        """Stated as its own assertion rather than left implicit in the
        snapshot's exclusion list: the squash changes the revision identity
        and nothing else."""
        _alembic("head")
        assert _current_revision(migration_db) == RELEASE_REVISION
        assert "alembic_version" not in _capture_schema(_MIGRATION_TEST_DB)["tables"]


class TestRealisticUpgradeFromCeluma12:
    """The release transition this migration actually has to survive:
    `v1_2_0 -> v1_3_0` against a populated Céluma 1.2 database.

    Every other DB-backed test in this file starts from an empty database or
    seeds one table. This one builds a small but complete lab — tenant,
    branch, users, patient, order, sample with images, report with versions,
    storage objects — at `v1_2_0`, using only columns that exist there, and
    then upgrades across the release boundary.

    Fresh-install correctness does not imply this. On an empty database every
    statement in the migration's data section matches zero rows, so a broken
    backfill passes unnoticed; here the same statements have to attribute
    real objects, compute a real baseline, and leave the clinical record
    untouched while doing it.
    """

    def _seed_pre_1_3_lab(self, migration_db) -> dict:
        with migration_db.begin() as conn:
            tenant_a = _seed_tenant(conn)
            tenant_b = _seed_tenant(conn)

            branch_a = _seed_branch(conn, tenant_a)
            patient_a = _seed_patient(conn, tenant_a, branch_a)
            order_a = _seed_order(conn, tenant_a, branch_a, patient_a)
            report_a = _seed_report(conn, tenant_a, branch_a, order_a)
            sample_a = _seed_sample(conn, tenant_a, branch_a, order_a)

            # Sample image + rendition: 3000 + 300 bytes, unattributed.
            image_storage = _seed_storage_object(
                conn, key="samples/a/full.jpg", size_bytes=3000, tenant_id=None
            )
            image_id = _seed_sample_image(
                conn, tenant_a, branch_a, sample_a, image_storage
            )
            thumb_storage = _seed_storage_object(
                conn, key="samples/a/thumb.jpg", size_bytes=300, tenant_id=None
            )
            _seed_sample_image_rendition(conn, image_id, "thumbnail", thumb_storage)

            # Official PDF (sha256 set) + report JSON body.
            official_pdf = _seed_storage_object(
                conn, key="reports/a/official.pdf", size_bytes=5000, tenant_id=None,
                content_type="application/pdf", sha256_hex="f" * 64,
            )
            report_json = _seed_storage_object(
                conn, key="reports/a/body.json", size_bytes=700, tenant_id=None,
                content_type="application/json",
            )
            _seed_report_version(
                conn, report_a, version_no=1,
                pdf_storage_id=official_pdf, json_storage_id=report_json,
            )

            # A signature on a live user.
            signature = _seed_storage_object(
                conn, key="users/a/signature.png", size_bytes=120, tenant_id=None,
                content_type="image/png",
            )
            user_a = _seed_app_user(
                conn, tenant_a, email=f"a-{uuid.uuid4().hex[:8]}@lab.test",
                signature_storage_id=signature,
            )
            # A second tenant with nothing billable, to prove isolation.
            user_b = _seed_app_user(
                conn, tenant_b, email=f"b-{uuid.uuid4().hex[:8]}@lab.test"
            )

        return {
            "tenant_a": tenant_a,
            "tenant_b": tenant_b,
            "patient_a": patient_a,
            "order_a": order_a,
            "report_a": report_a,
            "sample_a": sample_a,
            "user_a": user_a,
            "user_b": user_b,
            "image_storage": image_storage,
            "thumb_storage": thumb_storage,
            "official_pdf": official_pdf,
            "report_json": report_json,
            "signature": signature,
        }

    def test_clinical_rows_survive_the_upgrade(self, migration_db):
        _alembic(LAST_PRE_1_3_REVISION)
        seeded = self._seed_pre_1_3_lab(migration_db)

        _alembic("head")

        assert _current_revision(migration_db) == RELEASE_REVISION
        with migration_db.connect() as conn:
            for table, row_id in (
                ("tenant", seeded["tenant_a"]),
                ("patient", seeded["patient_a"]),
                ('"order"', seeded["order_a"]),
                ("report", seeded["report_a"]),
                ("sample", seeded["sample_a"]),
                ("app_user", seeded["user_a"]),
                ("storage_object", seeded["official_pdf"]),
            ):
                count = conn.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE id = :id"),
                    {"id": row_id},
                ).scalar_one()
                assert count == 1, f"{table} row did not survive the upgrade"

    def test_storage_attribution_is_backfilled_for_all_four_categories(
        self, migration_db
    ):
        _alembic(LAST_PRE_1_3_REVISION)
        seeded = self._seed_pre_1_3_lab(migration_db)

        _alembic("head")

        with migration_db.connect() as conn:
            for label in (
                "image_storage", "thumb_storage", "official_pdf",
                "report_json", "signature",
            ):
                attributed = conn.execute(
                    text("SELECT tenant_id FROM storage_object WHERE id = :id"),
                    {"id": seeded[label]},
                ).scalar_one()
                assert attributed == seeded["tenant_a"], (
                    f"{label} was not attributed by the backfill"
                )

    def test_the_usage_baseline_is_initialized_per_tenant(self, migration_db):
        """3000 + 300 + 5000 + 700 + 120 = 9120 for the working tenant, and a
        real zero row — not a missing row — for the tenant with nothing."""
        _alembic(LAST_PRE_1_3_REVISION)
        seeded = self._seed_pre_1_3_lab(migration_db)

        _alembic("head")

        with migration_db.connect() as conn:
            usage = dict(
                conn.execute(
                    text(
                        "SELECT tenant_id, billable_storage_bytes FROM tenant_usage"
                    )
                ).all()
            )
        assert usage[seeded["tenant_a"]] == 9120
        assert usage[seeded["tenant_b"]] == 0, (
            "every tenant gets a row; absence would mean 'not initialized'"
        )

    def test_tenant_isolation_holds_across_the_upgrade(self, migration_db):
        """Tenant B's baseline must not absorb tenant A's objects, and no
        storage object may end up attributed to the wrong tenant."""
        _alembic(LAST_PRE_1_3_REVISION)
        seeded = self._seed_pre_1_3_lab(migration_db)

        _alembic("head")

        with migration_db.connect() as conn:
            misattributed = conn.execute(
                text(
                    "SELECT COUNT(*) FROM storage_object WHERE tenant_id = :b"
                ),
                {"b": seeded["tenant_b"]},
            ).scalar_one()
        assert misattributed == 0

    def test_the_upgrade_creates_no_notification_and_no_threshold_state(
        self, migration_db
    ):
        """§11 and §12 of the squash contract, against populated data.

        A tenant is seeded *over* a storage limit before the upgrade, which
        is the exact condition a baseline evaluation inside the migration
        would have fired on. Nothing may be created: first evaluation is
        runtime behaviour, and a tenant already above a threshold must get
        its notification from the application, once, rather than silently
        having the crossing recorded and swallowed at deploy time.
        """
        _alembic(LAST_PRE_1_3_REVISION)
        self._seed_pre_1_3_lab(migration_db)

        _alembic("head")

        with migration_db.begin() as conn:
            # tenant_limits only exists after the upgrade, so the "already
            # over the limit" condition is established here and the
            # migration is replayed below.
            over_limit = conn.execute(
                text("SELECT tenant_id FROM tenant_usage ORDER BY "
                     "billable_storage_bytes DESC LIMIT 1")
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO tenant_limits (tenant_id, storage_limit_bytes, "
                    "user_limit, updated_at) VALUES (:id, 100, 1, now())"
                ),
                {"id": over_limit},
            )

        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
        _alembic("head")

        with migration_db.connect() as conn:
            for table in (
                "notification",
                "notification_recipient",
                "notification_delivery",
                "tenant_usage_threshold_state",
            ):
                count = conn.execute(
                    text(f"SELECT COUNT(*) FROM {table}")
                ).scalar_one()
                assert count == 0, f"the migration created rows in {table}"

    def test_downgrade_and_re_upgrade_reproduces_the_same_baseline(
        self, migration_db
    ):
        """The full round trip the release runbook needs: upgrade, roll back,
        roll forward, and land on the same numbers."""
        _alembic(LAST_PRE_1_3_REVISION)
        seeded = self._seed_pre_1_3_lab(migration_db)

        _alembic("head")
        with migration_db.connect() as conn:
            first = conn.execute(
                text(
                    "SELECT billable_storage_bytes FROM tenant_usage "
                    "WHERE tenant_id = :id"
                ),
                {"id": seeded["tenant_a"]},
            ).scalar_one()

        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")
        assert _current_revision(migration_db) == LAST_PRE_1_3_REVISION
        _alembic("head")

        with migration_db.connect() as conn:
            second = conn.execute(
                text(
                    "SELECT billable_storage_bytes FROM tenant_usage "
                    "WHERE tenant_id = :id"
                ),
                {"id": seeded["tenant_a"]},
            ).scalar_one()
            # And the clinical record is still there after the round trip.
            report_count = conn.execute(
                text("SELECT COUNT(*) FROM report WHERE id = :id"),
                {"id": seeded["report_a"]},
            ).scalar_one()

        assert first == second == 9120
        assert report_count == 1


class TestMultipleSignaturesPerTenantUpgrade:
    """Finding B-001 — `v1_3_0` used to abort when a tenant had two or more
    users with an uploaded signature.

    `_TENANT_USAGE_BASELINE_INSERT`'s `signature` CTE was the only one of its
    eight without `SUM`/`GROUP BY`. It emitted one row per signature-bearing
    user, `LEFT JOIN signature sg ON sg.tenant_id = t.id` fanned the tenant's
    row out by that many, and `INSERT INTO tenant_usage` then violated
    `tenant_usage_pkey`. A `v1_2_0 -> v1_3_0` upgrade of any laboratory with
    two signing pathologists failed outright and rolled back.

    Every pre-existing signature fixture in this file — including
    `TestRealisticUpgradeFromCeluma12`, realistic in every other dimension —
    seeds exactly **one** signature-bearing user per tenant, which is the
    single shape under which the defective CTE behaved correctly. That is why
    139 tests passed while a realistically-shaped database could not be
    upgraded at all.

    These tests seed the shape that was missing. They assert the arithmetic,
    not merely that the migration survives: "does not raise" would have been
    satisfied by a CTE that silently dropped one of the two signatures.
    """

    def test_two_signature_bearing_users_in_one_tenant_sum_into_one_usage_row(
        self, migration_db
    ):
        """The minimal reproduction of B-001, inverted into a regression.

        One tenant, two active users, one signature each and nothing else
        billable: 1000 + 2000 = 3000 bytes in exactly one `tenant_usage` row.
        Before the fix this upgrade aborted with a `UniqueViolation` on
        `tenant_usage_pkey` and left the database at `v1_2_0`.
        """
        _alembic(LAST_PRE_1_3_REVISION)
        with migration_db.begin() as conn:
            tenant_id = _seed_tenant(conn)
            signature_a = _seed_storage_object(
                conn, key="users/a/signature/sign_1.png", size_bytes=1000,
                tenant_id=None, content_type="image/png",
            )
            signature_b = _seed_storage_object(
                conn, key="users/b/signature/sign_2.png", size_bytes=2000,
                tenant_id=None, content_type="image/png",
            )
            _seed_app_user(
                conn, tenant_id, email=f"sig-a-{uuid.uuid4().hex[:8]}@lab.test",
                signature_storage_id=signature_a,
            )
            _seed_app_user(
                conn, tenant_id, email=f"sig-b-{uuid.uuid4().hex[:8]}@lab.test",
                signature_storage_id=signature_b,
            )

        _alembic("head")

        assert _current_revision(migration_db) == RELEASE_REVISION, (
            "the upgrade did not complete; B-001 has regressed"
        )

        with migration_db.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT billable_storage_bytes FROM tenant_usage "
                    "WHERE tenant_id = :id"
                ),
                {"id": tenant_id},
            ).scalars().all()
            duplicates = conn.execute(
                text(
                    "SELECT COALESCE(SUM(c - 1), 0) FROM ("
                    "  SELECT COUNT(*) AS c FROM tenant_usage "
                    "  GROUP BY tenant_id HAVING COUNT(*) > 1"
                    ") d"
                )
            ).scalar_one()
            attributed = conn.execute(
                text(
                    "SELECT COUNT(*) FROM storage_object "
                    "WHERE id IN (:a, :b) AND tenant_id = :id"
                ),
                {"a": signature_a, "b": signature_b, "id": tenant_id},
            ).scalar_one()

        assert len(rows) == 1, f"expected exactly one tenant_usage row, got {len(rows)}"
        assert duplicates == 0
        assert rows[0] == 3000, (
            "both signatures must be summed into the tenant's baseline; "
            f"got {rows[0]} instead of 1000 + 2000"
        )
        assert attributed == 2, "both signature objects must be attributed"

        with migration_db.connect() as conn:
            for table in (
                "notification",
                "notification_recipient",
                "notification_delivery",
                "notification_preference",
                "tenant_usage_threshold_state",
            ):
                count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                assert count == 0, f"the migration created rows in {table}"

    def test_three_signatures_sum_alongside_the_other_billable_categories(
        self, migration_db
    ):
        """Aggregation over more than two rows, and composition with a
        non-signature category — 100 + 200 + 400 signatures plus a 50-byte
        report JSON body is 750, in one row.

        The two-user case above proves the `GROUP BY` exists. This one proves
        the CTE aggregates rather than picking a single row, and that the
        aggregated value still adds to the other seven categories instead of
        replacing them. A second tenant with a single signature runs in the
        same upgrade, so the grouped CTE is shown not to leak across tenants.
        """
        _alembic(LAST_PRE_1_3_REVISION)
        with migration_db.begin() as conn:
            tenant_a = _seed_tenant(conn)
            tenant_b = _seed_tenant(conn)

            for index, size in enumerate((100, 200, 400), start=1):
                signature = _seed_storage_object(
                    conn, key=f"users/a{index}/signature/sign_{index}.png",
                    size_bytes=size, tenant_id=None, content_type="image/png",
                )
                _seed_app_user(
                    conn, tenant_a,
                    email=f"sig-{index}-{uuid.uuid4().hex[:8]}@lab.test",
                    signature_storage_id=signature,
                )

            branch_a = _seed_branch(conn, tenant_a)
            patient_a = _seed_patient(conn, tenant_a, branch_a)
            order_a = _seed_order(conn, tenant_a, branch_a, patient_a)
            report_a = _seed_report(conn, tenant_a, branch_a, order_a)
            report_json = _seed_storage_object(
                conn, key="reports/a/body.json", size_bytes=50, tenant_id=None,
                content_type="application/json",
            )
            _seed_report_version(
                conn, report_a, version_no=1, json_storage_id=report_json,
            )

            # A single-signature tenant in the same run: the shape the old
            # CTE handled correctly must keep working.
            lone_signature = _seed_storage_object(
                conn, key="users/b/signature/sign_1.png", size_bytes=7,
                tenant_id=None, content_type="image/png",
            )
            _seed_app_user(
                conn, tenant_b, email=f"sig-b-{uuid.uuid4().hex[:8]}@lab.test",
                signature_storage_id=lone_signature,
            )

        _alembic("head")

        assert _current_revision(migration_db) == RELEASE_REVISION

        with migration_db.connect() as conn:
            usage = dict(
                conn.execute(
                    text(
                        "SELECT tenant_id, billable_storage_bytes FROM tenant_usage"
                    )
                ).all()
            )
            row_count = conn.execute(
                text("SELECT COUNT(*) FROM tenant_usage")
            ).scalar_one()

        assert row_count == 2
        assert usage[tenant_a] == 750, (
            "three signatures (700) plus a report JSON body (50) must sum to 750"
        )
        assert usage[tenant_b] == 7


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
    """Insert one storage_object, at `v1_2_0` or at head.

    `tenant_id` is omitted from the INSERT entirely when it is None rather
    than being passed as SQL NULL. The two are equivalent at head and only
    the former works at `v1_2_0`, where `storage_object.tenant_id` does not
    exist yet — that column is part of the 1.3 delta.

    This matters more than it looks. Before the pre-Phase-5 squash these
    fixtures were seeded at an intermediate Phase 4 revision, where the
    column existed and a test could hand a storage object its attribution up
    front. Post-squash the only seam before the release migration is
    `v1_2_0`, which is also what a real upgrading database looks like: every
    storage object arrives unattributed, and the backfill is the only thing
    that attributes any of them.
    """
    storage_id = uuid.uuid4()
    columns = [
        "id", "provider", "region", "bucket", "object_key",
        "content_type", "size_bytes", "sha256_hex", "created_at",
    ]
    values = [
        ":id", "'aws'", "'mx-test-1'", "'celuma-test-bucket'", ":key",
        ":content_type", ":size_bytes", ":sha256_hex", "now()",
    ]
    params = {
        "id": storage_id,
        "key": key,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "sha256_hex": sha256_hex,
    }
    if tenant_id is not None:
        columns.append("tenant_id")
        values.append(":tenant_id")
        params["tenant_id"] = tenant_id

    conn.execute(
        text(
            f"INSERT INTO storage_object ({', '.join(columns)}) "
            f"VALUES ({', '.join(values)})"
        ),
        params,
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
