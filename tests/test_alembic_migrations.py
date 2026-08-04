"""Alembic chain integrity tests (Céluma 1.3, Phase 2 closure).

Céluma 1.3 shipped its database delta as seven revisions on the
`celuma-1.3` branch (v1_3_0 … v1_9_0). None of them ever reached
production, staging or a customer database, so the release was squashed
into a single contractual migration on top of `v1_2_0` — see
docs/celuma-1.3/phase-2-closure/alembic-squash-inventory.md.

These tests are the regression net for that decision:

  - the static ones guarantee the chain stays single-headed, that the head
    stays the release revision, and that no superseded 1.3 revision id can
    creep back into executable code;
  - the DB-backed ones guarantee the release migration still upgrades a
    clean pre-1.3 database, downgrades without residue, and re-upgrades —
    the Path A/B matrix from the closure brief.

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

#: The single consolidated Céluma 1.3 release revision.
RELEASE_REVISION = "v1_3_0"

#: Revision ids that existed only on the unreleased `celuma-1.3` branch and
#: were folded into RELEASE_REVISION. Nothing executable may reference them.
SUPERSEDED_REVISIONS = ("v1_4_0", "v1_5_0", "v1_6_0", "v1_7_0", "v1_8_0", "v1_9_0")

_MIGRATION_TEST_DB = "celuma_migration_test"


def _executable_source(path: pathlib.Path) -> str:
    """Return a module's source stripped of its module docstring and comments.

    The release migration documents its own provenance in prose — the module
    docstring names the seven revisions it consolidates, and each section of
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

    def test_head_is_the_consolidated_release_revision(self):
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

    def test_no_superseded_revision_file_remains_in_the_versions_directory(self):
        script = _script_directory()
        known = {r.revision for r in script.walk_revisions()}
        assert known.isdisjoint(SUPERSEDED_REVISIONS)

    @pytest.mark.parametrize("stale", SUPERSEDED_REVISIONS)
    def test_no_executable_code_references_a_superseded_revision(self, stale):
        """Documentation intentionally keeps the historical ids as a record
        of how the release was built — docs/celuma-1.3/, and the release
        migration's own module docstring, which names the seven revisions it
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

    def test_downgrade_removes_every_object_the_release_introduced(self, migration_db):
        _alembic("head")
        _alembic(LAST_PRE_1_3_REVISION, command="downgrade")

        inspector = inspect(migration_db)
        tables = set(inspector.get_table_names())
        assert "report_template_version" not in tables
        assert "report_letterhead" not in tables
        assert "report_letterhead_version" not in tables

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
        survive the squash: consolidating seven migrations into one is not a
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
