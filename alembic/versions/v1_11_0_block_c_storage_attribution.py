"""v1.11.0 - Céluma 1.3, Phase 4, Block C: storage attribution & usage
initialization

Revision ID: v1_11_0
Revises: v1_10_0
Create Date: 2026-08-10
Remediated: 2026-08-10 (Block C remediation — see docs/celuma-1.3/
phase-4-block-c/block-c-remediation-report.md, "Migration determinism")

Data-only migration — no schema change. Two steps, in order, both required
before incremental usage accounting (the application-code counter
mutations shipped alongside this revision) can be trusted:

  1. Backfill `storage_object.tenant_id` for the four billable categories
     Block A/B found it missing on (sample images, legacy/manual report
     PDFs, report JSON bodies, live user signatures), by joining each
     category to its owning parent row. Idempotent and non-destructive by
     construction: every UPDATE below is guarded by
     `storage_object.tenant_id IS NULL`, so a row that already carries a
     tenant_id (from before this migration, or from a re-run) is never
     touched. Official report PDFs, tenant logos, and letterhead/template
     logos already had `tenant_id` populated at write time and are
     untouched by this step.

  2. Initialize exactly one `TenantUsage` row per tenant that does not
     already have one, seeded with that tenant's billable baseline as
     defined by the Céluma 1.3 Block C contract — computed here as frozen
     SQL, not by calling `app.services.storage_billing.
     StorageBillingService` (see "Historical determinism" below). A
     tenant that already has a `TenantUsage` row (there should be none
     before this migration ever runs, since Block B shipped the table
     empty and this is the first revision that writes to it) is left
     untouched — this is an `INSERT ... WHERE NOT EXISTS`, never a
     recompute-and-overwrite.

Historical determinism — why this migration does NOT import
app.services.storage_billing / app.services.usage
--------------------------------------------------------------------------
An earlier version of this migration called `StorageBillingService.
compute_billable_storage_bytes()` and `UsageService.initialize_usage()`
directly. That worked, but made the migration's behavior depend on
whatever those services' business logic happens to be at the moment
`alembic upgrade` runs — not at the moment this revision was authored. A
fresh environment running the full chain from `v1_0_0` months or years
from now, after `storage_billing.py` has legitimately grown an eighth
category or changed a join, would silently apply *today's* rules to
*this* revision's historical backfill, producing a different
`TenantUsage` baseline than every environment that upgraded through
`v1_11_0` before that change shipped. A released migration must produce
the same result forever, independent of the application code around it.

The fix: every category's selection rule is frozen into this file as
literal SQL (see `_BASELINE_INSERT` below), using only stable, generic
SQL — no reference to `StorageBillingService`, no reference to
`UsageService`, no import of any `app.services.*` module. The frozen SQL
mirrors `StorageBillingService.compute_billable_storage_bytes()` exactly
as it existed when this revision was authored (proved by the
migration/runtime parity tests in `tests/test_alembic_migrations.py::
TestMigrationRuntimeParity` — a
release-time guard, not a promise that the two will always agree after a
future, legitimate change to runtime billing semantics). If billable
semantics ever change, that change ships as its own migration/reconciliation
step that explicitly transitions existing `TenantUsage` rows — it must
never retroactively alter what `v1_11_0` is defined to have done.

Only `alembic.op` and stable SQLAlchemy Core primitives (`sqlalchemy.text`)
are imported — no ORM model, no application service. This keeps the
frozen SQL below the sole source of truth for what this revision computes,
with nothing left to accidentally re-resolve against a model class that
could itself change shape later.

`downgrade()` is a deliberate no-op — this is a pure data migration with no
schema to revert, and reverting the data would be actively destructive:
tenant_id backfill cannot distinguish "we set this" from "was already set"
without extra bookkeeping this revision does not add, and deleting
TenantUsage rows would discard any real incremental accounting that
occurred after initialization (a subsequent re-upgrade recomputing the
historical baseline from scratch would UNDER-count relative to whatever
was actually accumulated). Unchanged by the remediation — see
database-migration-notes.md §"Why downgrade is a no-op".
"""
import os
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "v1_11_0"
down_revision: Union[str, Sequence[str], None] = "v1_10_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Step 1: storage_object.tenant_id backfill (raw SQL — matches the existing
# data-migration convention in this codebase, e.g. v1_1_0's role_permission
# seeding). Every statement is idempotent and never overwrites a non-NULL
# tenant_id.
# ---------------------------------------------------------------------------

_BACKFILL_SAMPLE_IMAGES = """
    UPDATE storage_object so
    SET tenant_id = si.tenant_id
    FROM sample_image si
    WHERE si.storage_id = so.id
      AND so.tenant_id IS NULL
"""

_BACKFILL_SAMPLE_IMAGE_RENDITIONS = """
    UPDATE storage_object so
    SET tenant_id = si.tenant_id
    FROM sample_image_rendition sir
    JOIN sample_image si ON si.id = sir.sample_image_id
    WHERE sir.storage_id = so.id
      AND so.tenant_id IS NULL
"""

# Covers both report_version.json_storage_id (report JSON bodies) and
# report_version.pdf_storage_id (legacy/manual PDFs). Official PDFs also
# reach storage_object via pdf_storage_id, but they already carry a
# non-NULL tenant_id (set at generation time), so the `IS NULL` guard
# correctly excludes them here — no category-disambiguation needed for a
# backfill step, only for the billable calculation below.
_BACKFILL_REPORT_JSON_AND_LEGACY_PDF = """
    UPDATE storage_object so
    SET tenant_id = r.tenant_id
    FROM report_version rv
    JOIN report r ON r.id = rv.report_id
    WHERE (rv.json_storage_id = so.id OR rv.pdf_storage_id = so.id)
      AND so.tenant_id IS NULL
"""

_BACKFILL_LIVE_SIGNATURES = """
    UPDATE storage_object so
    SET tenant_id = u.tenant_id
    FROM app_user u
    WHERE u.signature_storage_id = so.id
      AND so.tenant_id IS NULL
"""


# ---------------------------------------------------------------------------
# Step 2: TenantUsage baseline — frozen Céluma 1.3 Block C billable contract.
#
# Mirrors app/services/storage_billing.py::StorageBillingService as it
# existed at authoring time (see billable-storage-calculation-contract.md
# for the narrative spec this SQL implements):
#
#   - Sample images (processed + renditions): every StorageObject reachable
#     from sample_image/sample_image_rendition, summed.
#   - Official report PDFs: storage_object.sha256_hex IS NOT NULL — the
#     application's own standing invariant (only the official-PDF
#     generation path ever sets this column), not a guess. ALL such rows
#     count, including historically superseded ones (never decremented).
#   - Legacy/manual report PDFs: only the row each report_version.
#     pdf_storage_id CURRENTLY points at, excluding official PDFs
#     (sha256_hex IS NULL) — stale, superseded same-version re-uploads are
#     excluded by construction (they are no longer reachable via the FK).
#   - Report JSON bodies: every report_version.json_storage_id, current or
#     historical — permanent, one row per version.
#   - Tenant logo: only the tenant's CURRENT logo, resolved by recovering
#     the S3 object key from Tenant.logo_url (the exact inverse of
#     S3Service.object_public_url()) and matching it against
#     storage_object.object_key for that tenant.
#   - Letterhead/report-template assets: every StorageObject whose
#     object_key falls under the report-letterheads/ or report-templates/
#     prefix, for that tenant — billable once persistently stored,
#     regardless of whether any version currently references it (the
#     ratified Céluma 1.3 policy — see billable-storage-calculation-
#     contract.md §3 and block-c-remediation-report.md).
#   - Live user signature: only app_user.signature_storage_id, current —
#     a replaced/deleted signature's retained S3 PNG has no StorageObject
#     row left to count.
# ---------------------------------------------------------------------------

_BASELINE_INSERT = """
    WITH sample_images AS (
        SELECT si.tenant_id AS tenant_id, SUM(so.size_bytes) AS bytes
        FROM sample_image si
        JOIN storage_object so ON so.id = si.storage_id
        GROUP BY si.tenant_id
    ),
    sample_renditions AS (
        SELECT si.tenant_id AS tenant_id, SUM(so.size_bytes) AS bytes
        FROM sample_image_rendition sir
        JOIN sample_image si ON si.id = sir.sample_image_id
        JOIN storage_object so ON so.id = sir.storage_id
        GROUP BY si.tenant_id
    ),
    official_pdf AS (
        SELECT tenant_id, SUM(size_bytes) AS bytes
        FROM storage_object
        WHERE tenant_id IS NOT NULL AND sha256_hex IS NOT NULL
        GROUP BY tenant_id
    ),
    legacy_pdf AS (
        SELECT r.tenant_id AS tenant_id, SUM(so.size_bytes) AS bytes
        FROM report_version rv
        JOIN report r ON r.id = rv.report_id
        JOIN storage_object so ON so.id = rv.pdf_storage_id
        WHERE so.sha256_hex IS NULL
        GROUP BY r.tenant_id
    ),
    report_json AS (
        SELECT r.tenant_id AS tenant_id, SUM(so.size_bytes) AS bytes
        FROM report_version rv
        JOIN report r ON r.id = rv.report_id
        JOIN storage_object so ON so.id = rv.json_storage_id
        GROUP BY r.tenant_id
    ),
    tenant_logo AS (
        SELECT t.id AS tenant_id, so.size_bytes AS bytes
        FROM tenant t
        JOIN storage_object so
            ON so.tenant_id = t.id
           AND t.logo_url = :logo_url_prefix || so.object_key
        WHERE t.logo_url IS NOT NULL
    ),
    letterhead_asset AS (
        SELECT tenant_id, SUM(size_bytes) AS bytes
        FROM storage_object
        WHERE tenant_id IS NOT NULL
          AND (
              object_key LIKE 'report-letterheads/%'
              OR object_key LIKE 'report-templates/%'
          )
        GROUP BY tenant_id
    ),
    signature AS (
        SELECT u.tenant_id AS tenant_id, so.size_bytes AS bytes
        FROM app_user u
        JOIN storage_object so ON so.id = u.signature_storage_id
    )
    INSERT INTO tenant_usage (tenant_id, billable_storage_bytes, last_updated)
    SELECT
        t.id,
        COALESCE(si.bytes, 0)
            + COALESCE(sr.bytes, 0)
            + COALESCE(op.bytes, 0)
            + COALESCE(lp.bytes, 0)
            + COALESCE(rj.bytes, 0)
            + COALESCE(tl.bytes, 0)
            + COALESCE(la.bytes, 0)
            + COALESCE(sg.bytes, 0),
        now()
    FROM tenant t
    LEFT JOIN sample_images si ON si.tenant_id = t.id
    LEFT JOIN sample_renditions sr ON sr.tenant_id = t.id
    LEFT JOIN official_pdf op ON op.tenant_id = t.id
    LEFT JOIN legacy_pdf lp ON lp.tenant_id = t.id
    LEFT JOIN report_json rj ON rj.tenant_id = t.id
    LEFT JOIN tenant_logo tl ON tl.tenant_id = t.id
    LEFT JOIN letterhead_asset la ON la.tenant_id = t.id
    LEFT JOIN signature sg ON sg.tenant_id = t.id
    WHERE NOT EXISTS (
        SELECT 1 FROM tenant_usage tu WHERE tu.tenant_id = t.id
    )
"""


def _tenant_logo_url_prefix() -> str:
    """The exact prefix a tenant's CURRENT-logo public URL starts with,
    frozen-reimplemented from `S3Service.object_public_url()`'s own logic
    (app/services/s3.py) at authoring time — not imported from it, so a
    future change to that method cannot retroactively change what this
    already-released revision computed.

    Reads the same environment variables `app.core.config.Settings` reads
    (`MEDIA_PUBLIC_BASE_URL`, `S3_BUCKET_NAME`, `AWS_REGION`) directly via
    `os.environ`, the same technique `alembic/env.py` already uses for
    `DATABASE_URL` — this is reading configuration, not calling business
    logic, so it does not reintroduce the dependency this remediation
    removes.
    """
    media_base = os.environ.get("MEDIA_PUBLIC_BASE_URL")
    if media_base:
        return media_base.rstrip("/") + "/"
    bucket = os.environ.get("S3_BUCKET_NAME") or ""
    region = os.environ.get("AWS_REGION") or "mx-central-1"
    return f"https://{bucket}.s3.{region}.amazonaws.com/"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Historical tenant_id backfill
    # ------------------------------------------------------------------
    op.execute(_BACKFILL_SAMPLE_IMAGES)
    op.execute(_BACKFILL_SAMPLE_IMAGE_RENDITIONS)
    op.execute(_BACKFILL_REPORT_JSON_AND_LEGACY_PDF)
    op.execute(_BACKFILL_LIVE_SIGNATURES)

    # ------------------------------------------------------------------
    # 2. TenantUsage initialization — one row per tenant, missing rows
    #    only, frozen SQL baseline (see module docstring).
    # ------------------------------------------------------------------
    bind = op.get_bind()
    bind.execute(
        text(_BASELINE_INSERT),
        {"logo_url_prefix": _tenant_logo_url_prefix()},
    )


def downgrade() -> None:
    # Deliberate no-op — see the module docstring's "Why downgrade is a
    # no-op". Nothing to revert at the schema level (no DDL in upgrade()),
    # and reverting the data would be lossy/incorrect rather than merely
    # inconvenient.
    pass
