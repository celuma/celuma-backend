"""v1.3.0 - Céluma 1.3 release schema (versioned reports, letterheads, official
PDF, notifications, tenant usage)

Revision ID: v1_3_0
Revises: v1_2_0
Create Date: 2026-08-03
Amended: 2026-08-10 (Phase 3 closure — absorbs the Phase 3 notification chain)
Amended: 2026-08-12 (pre-Phase-5 closure — absorbs the Phase 4 usage chain)

**FROZEN.** This is the complete and final database contract for release
Céluma 1.3. The pre-Phase-5 migration squash
(docs/celuma-1.3/pre-phase-5-migration-squash/) was the last permitted rewrite
of this revision. Phase 5 validates it; Phase 5 does not edit it. A schema
defect discovered after this freeze is a release decision — a new deliberate
migration — not another amendment here. Céluma 1.4 begins from this revision
and is expected to add `v1_4_0`.

The single contractual database migration for release Céluma 1.3. It carries
the complete 1.3 delta, developed across three squashes:

  - **Phase 2 closure** folded in the chain that lived on the `celuma-1.3`
    branch as v1_3_0 → v1_4_0 → v1_5_0 → v1_6_0 → v1_7_0 → v1_8_0 → v1_9_0
    (Phase 2 Blocks A–E plus five post-Phase-2 remediation rounds).
  - **Phase 3 closure** folded in the notification chain that reused three of
    those freed identifiers: v1_4_0 → v1_5_0 → v1_6_0 (Phase 3 Blocks B, D
    and F).
  - **Pre-Phase-5 closure** folded in the Phase 4 usage chain:
    v1_10_0 → v1_11_0 → v1_12_0 → v1_13_0 (Blocks B, C, D and G).

Development history and release history are deliberately different, and the
distinction matters when reading the per-block documents under
docs/celuma-1.3/ — those record what actually happened while 1.3 was built:

    development history:  v1_3_0 → … → v1_9_0, then v1_10_0 → … → v1_13_0
    release history:      v1_3_0 only

`v1_10_0` through `v1_13_0` were development-time revisions of product version
**1.3** — they were never products 1.10 through 1.13, and they are not release
history. Because the first two squashes reused the same identifiers, an
`ex-v1_x_0` marker below is always qualified by its phase. No revision folded
in by any of the three squashes ever reached production, staging or a customer
database — the evidence is in
docs/celuma-1.3/phase-2-closure/alembic-squash-inventory.md,
docs/celuma-1.3/phase-3-closure/phase-3-alembic-squash-inventory.md and
docs/celuma-1.3/pre-phase-5-migration-squash/migration-squash-inventory.md.

Delta, in application order:

   1. `tenant.reports_v2_enabled`            (Phase 2 Block A, ex-v1_3_0)
   2. `report_template_version`              (Phase 2 Block B, ex-v1_4_0)
   3. `report_version` V2 metadata           (Phase 2 Block B, ex-v1_5_0)
   4. `storage_object.tenant_id`             (Phase 2 Block C, ex-v1_6_0)
   5. `report_version` PDF artifact fields   (Phase 2 Block E, ex-v1_7_0)
   6. `report_letterhead[_version]`          (remediation 1, ex-v1_8_0)
   7. `preferred_letterhead_id` + publish lock (remediation 2, ex-v1_9_0)
   8. `notification`                         (Phase 3 Block B, ex-v1_4_0;
                                              `locale` from Block F,
                                              ex-v1_6_0; final ten-type
                                              CHECK from Phase 4 Block G,
                                              ex-v1_13_0)
   9. `notification_recipient`               (Phase 3 Block B, ex-v1_4_0)
  10. `notification_delivery`                (Phase 3 Block B, ex-v1_4_0,
                                              with Block D's final uniqueness
                                              model, ex-v1_5_0)
  11. `notification_preference`              (Phase 3 Block B, ex-v1_4_0;
                                              final ten-type CHECK from Phase 4
                                              Block G, ex-v1_13_0)
  12. usage domain                           (Phase 4 Block B, ex-v1_10_0,
                                              with Block D's reconciliation
                                              hardening, ex-v1_12_0)
  13. `tenant.logo_storage_id`               (Phase 4 Block D, ex-v1_12_0)
  14. `tenant_usage_threshold_state`         (Phase 4 Block G, ex-v1_13_0)
  15. upgrade-time data migrations           (Phase 4 Block C, ex-v1_11_0;
                                              Block D backfill, ex-v1_12_0)

Sections 8–14 are written in **final form**, not as a replay of how they were
developed. Schema evolution is collapsed; upgrade-time *data* transformation
is not — see section 15 and "What the squash collapsed" below.

Intermediate states that are therefore absent by design:

  - `notification.locale` is created as part of `CREATE TABLE notification`
    rather than added by a later `ALTER TABLE`. The column is NOT NULL with
    `server_default 'es-MX'`; on a clean database there is no row to backfill,
    and the default is kept so a hand-inserted debugging row stays consistent.
  - `notification_delivery` is created directly with Block D's two partial
    unique indexes. The single `UNIQUE (notification_id, channel,
    recipient_address)` constraint that Block B created and Block D dropped is
    never created here — it encoded the assumption that one address belongs to
    one person, which is false of a laboratory, and reproducing it only to drop
    it would replay a defect.
  - `ck_notification_type` and `ck_notification_preference_type` are created
    once, with all ten permitted values. The chain created them with the six
    Phase 3 clinical types and widened both in `v1_13_0`; creating the narrow
    form here only to `DROP CONSTRAINT`/`ADD CONSTRAINT` it in the same
    migration would be pure ceremony. PostgreSQL stores a parsed expression
    and re-renders it, so an inline `CHECK` and one added by `ALTER TABLE`
    are byte-identical in `pg_get_constraintdef` — which is what makes this
    collapse provably equivalent rather than merely plausible.
  - `tenant_usage_reconciliation.metadata_mismatches_found` is a column of
    `CREATE TABLE` rather than a later `ALTER TABLE ADD COLUMN` (ex-v1_12_0),
    and its partial unique index `ix_tenant_usage_reconciliation_one_running`
    is created with the table's other indexes. It is deliberately declared
    *last*, after `error_code`, rather than next to `missing_objects_found`
    where it semantically belongs: `ALTER TABLE ADD COLUMN` appends, so last
    is the physical position the chain produced, and physical column order is
    part of what the equivalence proof compares.

What the squash collapsed, and what it could not
------------------------------------------------
Schema evolution is history and was collapsed: if the chain created a column
and later altered it, this revision creates the final form directly. Data
transformation is contract and was preserved verbatim — a real Céluma 1.2
database still has to be carried to 1.3, and section 15 is what carries it.
Nothing in section 15 is a no-op on a populated database:

  - `storage_object.tenant_id` is NULL for four billable categories on any
    pre-1.3 database; the backfill is the only thing that attributes them.
  - `tenant_usage` starts empty; without the baseline INSERT every existing
    tenant would begin 1.3 with no usage row at all, and incremental
    accounting has nothing to increment from.
  - `tenant.logo_storage_id` starts NULL; without the backfill every existing
    tenant is detached from its own logo for billing purposes.

On a fresh install all three are correctly no-ops — there are no rows to
transform — which is why `base -> head` and `v1_2_0 -> v1_3_0` are separately
validated rather than assumed equivalent.

Determinism is unchanged by the squash and remains load-bearing: section 15
imports no `app.services.*` module and reads no environment setting. Its
billable-storage rules are frozen literal SQL, and its logo resolution is
DB-scoped (persisted `logo_url` suffix vs. `object_key`), never reconstructed
from `MEDIA_PUBLIC_BASE_URL`/`S3_BUCKET_NAME`/`AWS_REGION`. A migration must
produce the same result years from now, in an environment whose CDN hostname
has changed, as it did the day it was written. See
docs/celuma-1.3/phase-4-block-c/block-c-remediation-report.md for the defect
that established this rule.

This revision creates **zero notifications and zero threshold-state rows**.
`tenant_usage_threshold_state` arrives empty, so every `(tenant, resource)`
pair is "never evaluated" and the first runtime evaluation applies the
documented Block G first-evaluation semantics. A baseline pass inside the
migration would either swallow the first real crossing for every tenant
already above a threshold, or fan a bulk mail-out across 133 tenants from
inside a DDL transaction. Neither is acceptable; the empty table is the
design. See docs/celuma-1.3/phase-4-block-g/usage-threshold-state-machine.md.

Design notes preserved verbatim from the superseded revisions — the full
rationale lives in the per-block contracts under docs/celuma-1.3/:

  - `status` on both version tables, and every notification enum, is a plain
    VARCHAR + CHECK, not a native Postgres ENUM, so future states can be added
    with a constraint change instead of `ALTER TYPE ... ADD VALUE` (which
    cannot run inside a transaction on older Postgres and is awkward to
    revert). This is what makes it cheap to ship
    `notification_delivery.channel` with EMAIL as its only permitted value.
  - Partial unique indexes enforce "at most one ACTIVE version" per logical
    template/letterhead, "at most one default letterhead" per tenant, and one
    delivery per (event, channel, recipient) at the database level, not only
    in application code.
  - No FK carries an ON DELETE clause, which defaults to Postgres NO
    ACTION/RESTRICT: a template/letterhead version referenced by any report
    can never be hard-deleted, only archived, and deleting a user or tenant
    that still owns notification history is refused rather than silently
    erasing the audit trail.
  - `ck_report_version_pdf_ready_requires_artifact` is the DB-level backstop
    for Block E's core invariant: no `READY` PDF without a real, hashed,
    persisted artifact.
  - `UNIQUE (tenant_id, idempotency_key)` on `notification` is the
    load-bearing idempotency guarantee — a real database constraint, so
    concurrent inserts of the same occurrence are serialized by PostgreSQL.
  - `notification.resource_type` carries NO check constraint and
    `resource_id` NO foreign key: the pair is polymorphic, exactly like
    `audit_log.entity_type`/`entity_id`, so a new resource type never needs a
    migration.

Historical-compatibility decisions, deliberately preserved:

  - Every column added to a preexisting table is nullable, except
    `tenant.reports_v2_enabled`, whose `server_default=false` backfills
    existing rows inside the same ALTER TABLE.
  - NO backfill runs anywhere in this migration. Specifically: no
    `schema_version` is inferred for existing `report_version` rows, no
    `pdf_generation_status`/`pdf_sha256` is invented for PDFs that may sit
    behind `pdf_storage_id` from the old ad-hoc upload endpoints, no
    `storage_object.tenant_id` is derived from bucket/key, and no
    `letterhead_version_id` is assigned to historical V2 reports.
  - No `rendering_snapshot` JSON is read or rewritten.
  - The letterhead tables and their FK references are purely additive, so
    reports created before letterhead entities existed keep
    `letterhead_version_id = NULL` and remain readable, editable and
    submittable to review.
  - The four notification tables arrive empty. In particular NO
    `notification_preference` row is seeded, per user or per type — absence of
    a row is what "use the default" means, and the whole preference contract
    depends on that staying true.

Column addition order within `report_version` and `report_template` matches
the order of the superseded chain, and `notification.locale` is the table's
last column exactly as the `ALTER TABLE` left it, so the resulting physical
column order is identical to what the chain produced.
"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


revision: str = "v1_3_0"
down_revision: Union[str, Sequence[str], None] = "v1_2_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Notification-domain enum values, kept as literals rather than imported from
#: app.models so the migration stays a frozen historical record: a later edit
#: to the Python enum must not retroactively change what this revision created.
#:
#: The six clinical types the notification domain shipped with (Phase 3).
_PHASE_3_NOTIFICATION_TYPES = (
    "REPORT_SUBMITTED",
    "REPORT_PDF_READY",
    "REPORT_PUBLISHED",
    "REPORT_RETRACTED",
    "ASSIGNMENT_ADDED",
    "SAMPLE_STATUS_CHANGED",
)

#: The four administrative usage-threshold types (Phase 4 Block G,
#: ex-v1_13_0). Kept as their own tuple rather than merged into the list
#: above: the distinction is real — these carry no patient, sample or report
#: reference — and the release contract states the final set as "the six
#: Phase 3 types plus these four".
_USAGE_THRESHOLD_NOTIFICATION_TYPES = (
    "STORAGE_USAGE_APPROACHING",
    "STORAGE_LIMIT_REACHED",
    "USER_LIMIT_APPROACHING",
    "USER_LIMIT_REACHED",
)

#: The final ten-value set both notification-domain CHECK constraints admit.
_NOTIFICATION_TYPES = (
    _PHASE_3_NOTIFICATION_TYPES + _USAGE_THRESHOLD_NOTIFICATION_TYPES
)

_SEVERITIES = ("INFO", "WARNING", "ACTION_REQUIRED")
_RECIPIENT_STATUSES = ("UNREAD", "READ", "DISMISSED")
_CHANNELS = ("EMAIL",)
_DELIVERY_STATUSES = ("PENDING", "SENDING", "SENT", "FAILED")

#: Reconciliation-run lifecycle (Phase 4 Block B, ex-v1_10_0).
_RECONCILIATION_STATUSES = ("RUNNING", "SUCCEEDED", "FAILED")

#: Threshold-state domain (Phase 4 Block G, ex-v1_13_0).
_THRESHOLD_RESOURCES = ("STORAGE", "USERS")
_THRESHOLD_STATES = ("UNMONITORED", "NORMAL", "APPROACHING", "REACHED")

#: The locale every notification created before Céluma 1.3 shipped was
#: rendered in — the only one the template registry could produce.
_DEFAULT_LOCALE = "es-MX"


logger = logging.getLogger("alembic.runtime.migration")


def _in_list(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


# ===========================================================================
# Section 15's frozen SQL — upgrade-time data transformation.
#
# Everything below is literal SQL on purpose. It mirrors
# `StorageBillingService.compute_billable_storage_bytes()` as it existed when
# Phase 4 Block C was authored, and it must keep producing that result forever
# — not whatever that service's business logic happens to be on the day some
# future environment runs `alembic upgrade` from base. That is why no
# `app.services.*` module is imported here and why no setting is read. See
# "What the squash collapsed, and what it could not" in the module docstring.
# ===========================================================================

# ---------------------------------------------------------------------------
# 15a. storage_object.tenant_id backfill (Phase 4 Block C, ex-v1_11_0).
#
# Four billable categories carried a NULL tenant_id on pre-1.3 databases.
# Every statement is guarded by `so.tenant_id IS NULL`, so it is idempotent
# and never overwrites an attribution that already exists — official report
# PDFs, tenant logos and letterhead/template logos populated tenant_id at
# write time and are untouched.
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
# report_version.pdf_storage_id (legacy/manual PDFs). Official PDFs also reach
# storage_object via pdf_storage_id, but they already carry a non-NULL
# tenant_id, so the `IS NULL` guard correctly excludes them here.
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
# 15b. tenant_usage baseline (Phase 4 Block C, ex-v1_11_0).
#
# One row per tenant that does not already have one, seeded with that tenant's
# billable baseline under the frozen Céluma 1.3 Block C contract (the
# narrative spec is billable-storage-calculation-contract.md):
#
#   - Sample images (processed + renditions): every reachable StorageObject.
#   - Official report PDFs: `sha256_hex IS NOT NULL` — the application's own
#     standing invariant, since only the official-PDF generation path sets it.
#     ALL such rows count, including superseded ones (never decremented).
#   - Legacy/manual report PDFs: only the row each report_version.
#     pdf_storage_id CURRENTLY points at, excluding official PDFs. Stale
#     same-version re-uploads are excluded by construction.
#   - Report JSON bodies: every json_storage_id, current or historical.
#   - Tenant logo: only the CURRENT logo, resolved from persisted DB values
#     alone — the tenant's own rows in the tenant-logo key family whose
#     object_key the stored logo_url ends with. Ambiguity counts zero rather
#     than guessing (`HAVING COUNT(*) = 1`); Block D's logo backfill in 15c
#     uses the identical rule, so the two agree by construction.
#   - Letterhead/report-template assets: billable once persistently stored,
#     regardless of whether any version currently references them.
#   - Live user signature: only app_user.signature_storage_id, current.
#
# `INSERT ... WHERE NOT EXISTS`, never recompute-and-overwrite: a tenant that
# somehow already has a row keeps whatever incremental accounting produced.
# ---------------------------------------------------------------------------

_TENANT_USAGE_BASELINE_INSERT = """
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
    tenant_logo_candidate AS (
        SELECT t.id AS tenant_id, so.size_bytes AS bytes
        FROM tenant t
        JOIN storage_object so
            ON so.tenant_id = t.id
           AND so.object_key LIKE 'tenants/%/logo/%'
           AND right(
                   split_part(split_part(t.logo_url, '#', 1), '?', 1),
                   length(so.object_key) + 1
               ) = '/' || so.object_key
        WHERE t.logo_url IS NOT NULL
    ),
    tenant_logo AS (
        SELECT tenant_id, MIN(bytes) AS bytes
        FROM tenant_logo_candidate
        GROUP BY tenant_id
        HAVING COUNT(*) = 1
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


# ---------------------------------------------------------------------------
# 15c. tenant.logo_storage_id backfill (Phase 4 Block D, ex-v1_12_0).
#
# `right(url, length(key) + 1) = '/' || key` is a plain suffix comparison
# rather than a LIKE pattern on purpose: object keys legitimately contain `%`
# and `_` (uploaded filenames are part of the key), which LIKE would treat as
# wildcards. Ownership is the relational column, never inferred from the key,
# so a StorageObject belonging to a different tenant can never be selected.
# A tenant with two or more candidates — or none — is left NULL and
# reconciliation reports it as `legacy_logo_reference_unresolved`.
# ---------------------------------------------------------------------------

_LOGO_CANDIDATES_CTE = """
    WITH candidate AS (
        SELECT t.id AS tenant_id, so.id AS storage_object_id
        FROM tenant t
        JOIN storage_object so
            ON so.tenant_id = t.id
           AND so.object_key LIKE 'tenants/%/logo/%'
           AND right(
                   split_part(split_part(t.logo_url, '#', 1), '?', 1),
                   length(so.object_key) + 1
               ) = '/' || so.object_key
        WHERE t.logo_url IS NOT NULL
    ),
    resolved AS (
        SELECT tenant_id, (array_agg(storage_object_id))[1] AS storage_object_id
        FROM candidate
        GROUP BY tenant_id
        HAVING COUNT(*) = 1
    )
"""

_BACKFILL_LOGO_STORAGE_ID = _LOGO_CANDIDATES_CTE + """
    UPDATE tenant t
    SET logo_storage_id = r.storage_object_id
    FROM resolved r
    WHERE r.tenant_id = t.id
      AND t.logo_storage_id IS NULL
"""

#: Aggregate counts only — no tenant name, no URL, no object key.
_LOGO_BACKFILL_REPORT = _LOGO_CANDIDATES_CTE + """
    SELECT
        (SELECT COUNT(*) FROM tenant WHERE logo_url IS NOT NULL) AS with_logo_url,
        (SELECT COUNT(*) FROM tenant WHERE logo_storage_id IS NOT NULL) AS backfilled,
        (SELECT COUNT(*) FROM (
            SELECT tenant_id FROM candidate GROUP BY tenant_id HAVING COUNT(*) > 1
        ) amb) AS ambiguous,
        (SELECT COUNT(*) FROM tenant t
          WHERE t.logo_url IS NOT NULL AND t.logo_storage_id IS NULL) AS unresolved
"""


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Tenant-level V2 feature flag (Phase 2 Block A, ex-v1_3_0)
    # ------------------------------------------------------------------
    op.add_column(
        "tenant",
        sa.Column(
            "reports_v2_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # ------------------------------------------------------------------
    # 2. report_template_version — append-only, immutable
    #    (Phase 2 Block B, ex-v1_4_0)
    # ------------------------------------------------------------------
    op.create_table(
        "report_template_version",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="PUBLISHED",
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('PUBLISHED', 'ACTIVE', 'ARCHIVED')",
            name="ck_report_template_version_status",
        ),
        sa.CheckConstraint(
            "version_number >= 1", name="ck_report_template_version_number_positive"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["report_template_id"], ["report_template.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "report_template_id", "version_number", name="uq_report_template_version_number"
        ),
    )
    op.create_index(
        "ix_report_template_version_tenant_id", "report_template_version", ["tenant_id"]
    )
    op.create_index(
        "ix_report_template_version_report_template_id",
        "report_template_version",
        ["report_template_id"],
    )
    op.create_index(
        "ix_report_template_version_status", "report_template_version", ["status"]
    )
    # At most one ACTIVE version per logical template, enforced at the DB level.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_report_template_version_one_active
        ON public.report_template_version (report_template_id)
        WHERE (status = 'ACTIVE')
        """
    )

    # ------------------------------------------------------------------
    # 3. report_version V2 metadata (Phase 2 Block B, ex-v1_5_0)
    # ------------------------------------------------------------------
    op.add_column(
        "report_version",
        sa.Column("schema_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("template_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("generated_by_renderer_version", sa.String(length=100), nullable=True),
    )
    op.create_foreign_key(
        "report_version_template_version_id_fkey",
        source_table="report_version",
        referent_table="report_template_version",
        local_cols=["template_version_id"],
        remote_cols=["id"],
    )
    op.create_index(
        "ix_report_version_template_version_id", "report_version", ["template_version_id"]
    )
    op.create_index("ix_report_version_schema_version", "report_version", ["schema_version"])
    # A V2 report_version (schema_version = 2) must always carry a
    # template_version_id. Legacy rows (schema_version IS NULL) are exempt.
    op.create_check_constraint(
        "ck_report_version_v2_requires_template_version",
        "report_version",
        "schema_version IS DISTINCT FROM 2 OR template_version_id IS NOT NULL",
    )

    # ------------------------------------------------------------------
    # 4. storage_object.tenant_id — nullable ownership tag
    #    (Phase 2 Block C, ex-v1_6_0)
    # ------------------------------------------------------------------
    op.add_column(
        "storage_object",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "storage_object_tenant_id_fkey",
        source_table="storage_object",
        referent_table="tenant",
        local_cols=["tenant_id"],
        remote_cols=["id"],
    )
    op.create_index("ix_storage_object_tenant_id", "storage_object", ["tenant_id"])

    # ------------------------------------------------------------------
    # 5. report_version PDF artifact fields (Phase 2 Block E, ex-v1_7_0)
    # ------------------------------------------------------------------
    op.add_column(
        "report_version",
        sa.Column("pdf_generation_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_generation_started_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_generated_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_size_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_page_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_generator_version", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_error_code", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_error_message", sa.String(length=500), nullable=True),
    )
    op.create_index(
        "ix_report_version_pdf_generation_status",
        "report_version",
        ["pdf_generation_status"],
    )
    op.create_check_constraint(
        "ck_report_version_pdf_generation_status_values",
        "report_version",
        "pdf_generation_status IS NULL OR pdf_generation_status IN "
        "('GENERATING', 'READY', 'FAILED')",
    )
    op.create_check_constraint(
        "ck_report_version_pdf_ready_requires_artifact",
        "report_version",
        "pdf_generation_status IS DISTINCT FROM 'READY' OR "
        "(pdf_storage_id IS NOT NULL AND pdf_sha256 IS NOT NULL AND "
        "pdf_size_bytes IS NOT NULL AND pdf_page_count IS NOT NULL)",
    )

    # ------------------------------------------------------------------
    # 6. report_letterhead / report_letterhead_version (ex-v1_8_0, remediation 1)
    # ------------------------------------------------------------------
    op.create_table(
        "report_letterhead",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_letterhead_tenant_id", "report_letterhead", ["tenant_id"])
    # At most one default letterhead per tenant, enforced at the DB level.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_report_letterhead_one_default
        ON public.report_letterhead (tenant_id)
        WHERE (is_default = true)
        """
    )

    op.create_table(
        "report_letterhead_version",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_letterhead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="PUBLISHED",
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('PUBLISHED', 'ACTIVE', 'ARCHIVED')",
            name="ck_report_letterhead_version_status",
        ),
        sa.CheckConstraint(
            "version_number >= 1", name="ck_report_letterhead_version_number_positive"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["report_letterhead_id"], ["report_letterhead.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "report_letterhead_id", "version_number", name="uq_report_letterhead_version_number"
        ),
    )
    op.create_index(
        "ix_report_letterhead_version_tenant_id", "report_letterhead_version", ["tenant_id"]
    )
    op.create_index(
        "ix_report_letterhead_version_report_letterhead_id",
        "report_letterhead_version",
        ["report_letterhead_id"],
    )
    op.create_index(
        "ix_report_letterhead_version_status", "report_letterhead_version", ["status"]
    )
    # At most one ACTIVE version per logical letterhead, enforced at the DB level.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_report_letterhead_version_one_active
        ON public.report_letterhead_version (report_letterhead_id)
        WHERE (status = 'ACTIVE')
        """
    )

    op.add_column(
        "report_template",
        sa.Column(
            "preferred_letterhead_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_report_template_preferred_letterhead_version_id",
        "report_template",
        "report_letterhead_version",
        ["preferred_letterhead_version_id"],
        ["id"],
    )

    op.add_column(
        "report_version",
        sa.Column("letterhead_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_report_version_letterhead_version_id",
        "report_version",
        "report_letterhead_version",
        ["letterhead_version_id"],
        ["id"],
    )

    # ------------------------------------------------------------------
    # 7. preferred_letterhead_id + publish lock (ex-v1_9_0, remediation 2)
    # ------------------------------------------------------------------
    op.add_column(
        "report_template",
        sa.Column("preferred_letterhead_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_report_template_preferred_letterhead_id",
        "report_template",
        "report_letterhead",
        ["preferred_letterhead_id"],
        ["id"],
    )

    op.add_column(
        "report_version",
        sa.Column("publish_started_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("publish_started_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_report_version_publish_started_by",
        "report_version",
        "app_user",
        ["publish_started_by"],
        ["id"],
    )

    # ------------------------------------------------------------------
    # 8. notification — shared, immutable event record
    #    (Phase 3 Block B, ex-v1_4_0; `locale` from Block F, ex-v1_6_0)
    #
    # `locale` is created here, as the table's last column, rather than added
    # by a later ALTER TABLE. It records the locale the frozen title/body were
    # rendered in, which the delivery worker needs — it re-renders the email
    # from template_key/template_params instead of copying the in-app text —
    # and which audit cannot reconstruct from stored Spanish strings once a
    # second locale exists. VARCHAR(35) is the bound app/services/locale.py
    # enforces, wide enough for a BCP-47 tag with a script subtag.
    # ------------------------------------------------------------------
    op.create_table(
        "notification",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column(
            "severity",
            sa.String(length=20),
            nullable=False,
            server_default="INFO",
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.String(length=1000), nullable=True),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_metadata", sa.JSON(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "locale",
            sa.String(length=35),
            nullable=False,
            server_default=_DEFAULT_LOCALE,
        ),
        sa.CheckConstraint(
            _in_list("type", _NOTIFICATION_TYPES), name="ck_notification_type"
        ),
        sa.CheckConstraint(
            _in_list("severity", _SEVERITIES), name="ck_notification_severity"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        # The core idempotency guarantee. Scoped by tenant so two tenants may
        # legitimately carry the same key for their own separate occurrences.
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_notification_tenant_idempotency_key"
        ),
    )
    op.create_index("ix_notification_tenant_id", "notification", ["tenant_id"])
    # "All notifications about this resource" — audit/debug access path.
    op.create_index(
        "ix_notification_tenant_resource",
        "notification",
        ["tenant_id", "resource_type", "resource_id"],
    )
    # Type-filtered, time-ordered tenant-wide queries.
    op.create_index(
        "ix_notification_tenant_type_created_at",
        "notification",
        ["tenant_id", "type", "created_at"],
    )

    # ------------------------------------------------------------------
    # 9. notification_recipient — per-user inbox and read state
    #    (Phase 3 Block B, ex-v1_4_0)
    #
    # No `delivered_at` column: for an in-app notification "delivered" is
    # exactly "the row exists", which `created_at` already records.
    # `created_at` is denormalized from the parent notification so the inbox
    # list query never joins for its sort key.
    # ------------------------------------------------------------------
    op.create_table(
        "notification_recipient",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="UNREAD",
        ),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            _in_list("status", _RECIPIENT_STATUSES),
            name="ck_notification_recipient_status",
        ),
        # A READ row must carry the timestamp that says when. Enforced in the
        # database so "marked read with no read_at" is unrepresentable, not
        # merely unlikely.
        sa.CheckConstraint(
            "status <> 'READ' OR read_at IS NOT NULL",
            name="ck_notification_recipient_read_requires_timestamp",
        ),
        sa.ForeignKeyConstraint(["notification_id"], ["notification.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        # Recipient-level idempotency: a user is a recipient of a given
        # notification at most once.
        sa.UniqueConstraint(
            "notification_id", "user_id", name="uq_notification_recipient_notification_user"
        ),
    )
    op.create_index(
        "ix_notification_recipient_notification_id",
        "notification_recipient",
        ["notification_id"],
    )
    # Unread-count query: COUNT(*) WHERE tenant_id/user_id/status.
    op.create_index(
        "ix_notification_recipient_inbox_status",
        "notification_recipient",
        ["tenant_id", "user_id", "status"],
    )
    # Inbox list query: WHERE tenant_id/user_id ORDER BY created_at DESC.
    op.create_index(
        "ix_notification_recipient_inbox_created_at",
        "notification_recipient",
        ["tenant_id", "user_id", "created_at"],
    )

    # ------------------------------------------------------------------
    # 10. notification_delivery — external-channel lifecycle
    #     (Phase 3 Block B, ex-v1_4_0, with Block D's final uniqueness model,
    #     ex-v1_5_0)
    #
    # The table is created directly in its final shape. Block B's
    # `UNIQUE (notification_id, channel, recipient_address)` is deliberately
    # NOT created: it assumed one address belongs to one person, so two staff
    # sharing a mailbox — a shared recepcion@, a technician covering a
    # colleague — collapsed into one delivery and the second person silently
    # received nothing. Block D replaced it with two partial unique indexes
    # that split the table by whether the recipient has an account, so every
    # row is covered by exactly one and neither overlaps the other:
    #
    #   - account-backed recipients are keyed on the user, so a shared mailbox
    #     is not a shared delivery, and one user still cannot receive two
    #     deliveries for one event even under two different addresses;
    #   - account-less recipients (a requesting physician resolved straight to
    #     an address) keep the original address guarantee verbatim, because
    #     they have no user id to key on. A plain unique constraint on the
    #     nullable recipient_user_id would reintroduce the NULLs-compare-
    #     distinct hole for exactly those rows.
    #
    # Both indexes are inferrable by `INSERT ... ON CONFLICT (…) WHERE …`, so
    # the duplicate defence stays in the database, not in application logic.
    # ------------------------------------------------------------------
    op.create_table(
        "notification_delivery",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recipient_address", sa.String(length=320), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            _in_list("channel", _CHANNELS), name="ck_notification_delivery_channel"
        ),
        sa.CheckConstraint(
            _in_list("status", _DELIVERY_STATUSES),
            name="ck_notification_delivery_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_notification_delivery_attempts_non_negative"
        ),
        sa.ForeignKeyConstraint(["notification_id"], ["notification.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_delivery_notification_id",
        "notification_delivery",
        ["notification_id"],
    )
    op.create_index(
        "ix_notification_delivery_tenant_id", "notification_delivery", ["tenant_id"]
    )
    # The delivery poller's primary query:
    # WHERE status = 'PENDING' AND next_attempt_at <= now().
    op.create_index(
        "ix_notification_delivery_poller",
        "notification_delivery",
        ["status", "next_attempt_at"],
    )
    # Account-backed recipients: one delivery per (event, channel, user).
    op.create_index(
        "uq_notification_delivery_recipient_user",
        "notification_delivery",
        ["notification_id", "channel", "recipient_user_id"],
        unique=True,
        postgresql_where=sa.text("recipient_user_id IS NOT NULL"),
    )
    # Account-less recipients: the original address guarantee, unchanged.
    op.create_index(
        "uq_notification_delivery_recipient_address",
        "notification_delivery",
        ["notification_id", "channel", "recipient_address"],
        unique=True,
        postgresql_where=sa.text("recipient_user_id IS NULL"),
    )

    # ------------------------------------------------------------------
    # 11. notification_preference — per-user, per-type override
    #     (Phase 3 Block B, ex-v1_4_0)
    # ------------------------------------------------------------------
    op.create_table(
        "notification_preference",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_type", sa.String(length=50), nullable=False),
        sa.Column(
            "in_app_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "email_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _in_list("notification_type", _NOTIFICATION_TYPES),
            name="ck_notification_preference_type",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "notification_type", name="uq_notification_preference_user_type"
        ),
    )
    op.create_index(
        "ix_notification_preference_tenant_id", "notification_preference", ["tenant_id"]
    )
    op.create_index(
        "ix_notification_preference_user_id", "notification_preference", ["user_id"]
    )
    # No preference row is seeded. Absence of a row means "use the default".

    # ------------------------------------------------------------------
    # 12. Usage domain (Phase 4 Block B, ex-v1_10_0, with Block D's
    #     reconciliation hardening folded in, ex-v1_12_0)
    #
    # All three tables arrive empty. `tenant_usage` is populated by section
    # 15's baseline; the other two stay empty until runtime writes to them.
    # Absence of a `tenant_usage` row means "not yet initialized", not "zero
    # usage"; absence of a `tenant_limits` row means "no limits configured",
    # which is unlimited. No FK carries ON DELETE: deleting a tenant that
    # still owns usage history is refused, never silently cascaded.
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_usage",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "billable_storage_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_updated", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "billable_storage_bytes >= 0",
            name="ck_tenant_usage_storage_non_negative",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    op.create_table(
        "tenant_limits",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_limit_bytes", sa.BigInteger(), nullable=True),
        sa.Column("user_limit", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "storage_limit_bytes IS NULL OR storage_limit_bytes > 0",
            name="ck_tenant_limits_storage_limit_positive",
        ),
        sa.CheckConstraint(
            "user_limit IS NULL OR user_limit > 0",
            name="ck_tenant_limits_user_limit_positive",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    # `metadata_mismatches_found` is declared last, after `error_code`, not
    # beside `missing_objects_found` where it belongs semantically: ex-v1_12_0
    # appended it with ALTER TABLE, and physical column order is part of the
    # schema-equivalence proof. It is its own integrity class deliberately —
    # an object whose S3 size or ETag disagrees with the DB row still exists,
    # and folding that into `missing_objects_found` would make "a row is
    # stale" indistinguishable from "we may have lost clinical data".
    op.create_table(
        "tenant_usage_reconciliation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="RUNNING",
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("expected_storage_bytes", sa.BigInteger(), nullable=True),
        sa.Column("actual_storage_bytes", sa.BigInteger(), nullable=True),
        sa.Column("difference_bytes", sa.BigInteger(), nullable=True),
        sa.Column("objects_checked", sa.BigInteger(), nullable=True),
        sa.Column("orphans_found", sa.BigInteger(), nullable=True),
        sa.Column("missing_objects_found", sa.BigInteger(), nullable=True),
        sa.Column("repaired", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=255), nullable=True),
        sa.Column("metadata_mismatches_found", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            _in_list("status", _RECONCILIATION_STATUSES),
            name="ck_tenant_usage_reconciliation_status",
        ),
        # RUNNING must not yet have a completion timestamp.
        sa.CheckConstraint(
            "status <> 'RUNNING' OR completed_at IS NULL",
            name="ck_tenant_usage_reconciliation_running_no_completed_at",
        ),
        # A terminal status (SUCCEEDED/FAILED) must carry one.
        sa.CheckConstraint(
            "status = 'RUNNING' OR completed_at IS NOT NULL",
            name="ck_tenant_usage_reconciliation_terminal_requires_completed_at",
        ),
        # A successful run should not be carrying a sanitized error code.
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR error_code IS NULL",
            name="ck_tenant_usage_reconciliation_succeeded_no_error_code",
        ),
        sa.CheckConstraint(
            "objects_checked IS NULL OR objects_checked >= 0",
            name="ck_tenant_usage_reconciliation_objects_checked_non_negative",
        ),
        sa.CheckConstraint(
            "orphans_found IS NULL OR orphans_found >= 0",
            name="ck_tenant_usage_reconciliation_orphans_found_non_negative",
        ),
        sa.CheckConstraint(
            "missing_objects_found IS NULL OR missing_objects_found >= 0",
            name="ck_tenant_usage_reconciliation_missing_objects_non_negative",
        ),
        sa.CheckConstraint(
            "metadata_mismatches_found IS NULL OR metadata_mismatches_found >= 0",
            name="ck_tenant_usage_reconciliation_metadata_mismatches_non_neg",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # "Latest reconciliation for tenant" — UsageService.get_latest_reconciliation().
    op.create_index(
        "ix_tenant_usage_reconciliation_tenant_started_at",
        "tenant_usage_reconciliation",
        ["tenant_id", "started_at"],
    )
    # "RUNNING reconciliations older than a staleness threshold" — crash recovery.
    op.create_index(
        "ix_tenant_usage_reconciliation_status_started_at",
        "tenant_usage_reconciliation",
        ["status", "started_at"],
    )
    # At most one RUNNING reconciliation per tenant, at the database level
    # rather than by application convention: the API runs at desired_count=1
    # today, but "unlikely because there happens to be one process" is not a
    # property Céluma should have to re-verify the day the service scales out.
    # Same technique as ix_report_letterhead_one_default.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_tenant_usage_reconciliation_one_running
        ON public.tenant_usage_reconciliation (tenant_id)
        WHERE (status = 'RUNNING')
        """
    )

    # `active_internal_users`/`active_physician_portal_users` run this
    # predicate on every usage-dashboard load.
    op.create_index(
        "ix_app_user_tenant_id_is_active", "app_user", ["tenant_id", "is_active"]
    )

    # ------------------------------------------------------------------
    # 13. tenant.logo_storage_id (Phase 4 Block D, ex-v1_12_0)
    #
    # The canonical answer to "which StorageObject is this tenant's current
    # logo". Until Block D the only answer was `logo_url`, a string that had
    # to be parsed back into an object key by stripping the *currently
    # configured* CDN prefix — so changing MEDIA_PUBLIC_BASE_URL silently
    # detached every tenant from its own logo for billing purposes.
    # `logo_url` is kept for API/presentation compatibility and is not
    # removed in Céluma 1.3.
    #
    # No cascade on the FK, deliberately: deleting a StorageObject must not
    # silently blank a tenant's identity. Storage-integrity problems are
    # reported, never auto-repaired. Backfilled in section 15c.
    # ------------------------------------------------------------------
    op.add_column(
        "tenant",
        sa.Column("logo_storage_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tenant_logo_storage_id_storage_object",
        "tenant",
        "storage_object",
        ["logo_storage_id"],
        ["id"],
    )

    # ------------------------------------------------------------------
    # 14. tenant_usage_threshold_state (Phase 4 Block G, ex-v1_13_0)
    #
    # One row per (tenant, resource), holding where a tenant currently sits
    # relative to its storage/user limit. Arrives EMPTY and stays empty until
    # runtime evaluates — see "This revision creates zero notifications" in
    # the module docstring.
    #
    # UNIQUE (tenant_id, resource) is the load-bearing constraint: it is what
    # makes "one semantic threshold transition -> one notification"
    # enforceable in the database rather than in application logic. The
    # service's `INSERT ... ON CONFLICT DO NOTHING` infers it, so two
    # concurrent first evaluations serialize on the index instead of both
    # inserting. No index beyond it — every read is
    # `WHERE tenant_id = ? AND resource = ?`, which the unique index serves.
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_usage_threshold_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource", sa.String(length=20), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default="UNMONITORED",
        ),
        sa.Column("last_value", sa.BigInteger(), nullable=True),
        sa.Column("last_limit", sa.BigInteger(), nullable=True),
        sa.Column(
            "transition_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_transition_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _in_list("resource", _THRESHOLD_RESOURCES),
            name="ck_tenant_usage_threshold_state_resource",
        ),
        sa.CheckConstraint(
            _in_list("state", _THRESHOLD_STATES),
            name="ck_tenant_usage_threshold_state_state",
        ),
        # "Not evaluable" must not carry the numbers of an evaluation.
        sa.CheckConstraint(
            "state <> 'UNMONITORED' OR (last_value IS NULL AND last_limit IS NULL)",
            name="ck_tenant_usage_threshold_state_unmonitored_has_no_values",
        ),
        sa.CheckConstraint(
            "last_value IS NULL OR last_value >= 0",
            name="ck_tenant_usage_threshold_state_value_non_negative",
        ),
        sa.CheckConstraint(
            "last_limit IS NULL OR last_limit > 0",
            name="ck_tenant_usage_threshold_state_limit_positive",
        ),
        sa.CheckConstraint(
            "transition_count >= 0",
            name="ck_tenant_usage_threshold_state_transition_count_non_negative",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "resource",
            name="uq_tenant_usage_threshold_state_tenant_resource",
        ),
    )

    # ------------------------------------------------------------------
    # 15. Upgrade-time data migration (Phase 4 Block C, ex-v1_11_0; Block D
    #     backfill, ex-v1_12_0)
    #
    # The only part of this revision that is not schema. On a fresh install
    # every statement here matches zero rows; on a real Céluma 1.2 database
    # this is what actually carries the data to 1.3. Order is load-bearing:
    # the tenant_id backfill must complete before the baseline, which filters
    # on `storage_object.tenant_id IS NOT NULL`.
    # ------------------------------------------------------------------
    op.execute(_BACKFILL_SAMPLE_IMAGES)
    op.execute(_BACKFILL_SAMPLE_IMAGE_RENDITIONS)
    op.execute(_BACKFILL_REPORT_JSON_AND_LEGACY_PDF)
    op.execute(_BACKFILL_LIVE_SIGNATURES)

    bind = op.get_bind()
    bind.execute(text(_TENANT_USAGE_BASELINE_INSERT))

    bind.execute(text(_BACKFILL_LOGO_STORAGE_ID))
    counts = bind.execute(text(_LOGO_BACKFILL_REPORT)).mappings().one()
    logger.info(
        "v1_3_0 tenant-logo backfill: %s tenant(s) with logo_url, %s backfilled, "
        "%s ambiguous, %s left unresolved",
        counts["with_logo_url"],
        counts["backfilled"],
        counts["ambiguous"],
        counts["unresolved"],
    )


def downgrade() -> None:
    """Reverse the full 1.3 delta in reverse dependency order.

    Data loss is expected and confined to columns/tables this release
    introduced: any V2 report metadata, PDF integrity metadata, letterhead
    definitions, publish claims and the entire notification history are
    dropped. Preexisting (pre-1.3) columns and rows are untouched, and no S3
    object is deleted — a downgraded deployment simply loses the ability to
    read that metadata until it upgrades again.

    Order is the exact reverse of `upgrade()`, and it is load-bearing rather
    than cosmetic. The notification tables reference each other
    (`notification_recipient` and `notification_delivery` both point at
    `notification`), so `notification` must be dropped last of the four;
    they also reference `tenant` and `app_user`, which this revision does not
    own and must therefore outlive them. Dropping the four first also means
    the letterhead and template-version tables below are dropped with no
    notification row still pointing anywhere near them.

    Every drop is unconditional DDL, so it works on a populated database, not
    only on an empty one: `DROP TABLE` removes the rows with the table, and
    the dependency order means no drop is ever refused by a foreign key.

    What the squash changed here
    ----------------------------
    This is now one downgrade, not five. The chain's per-revision inverses
    collapsed, and two of them disappeared entirely rather than being
    transcribed:

      - ex-v1_13_0 narrowed both notification CHECK constraints back to six
        values and, because a constraint is validated against existing rows,
        first had to `DELETE` every usage-threshold notification, recipient
        and delivery. None of that is needed here: this downgrade drops the
        `notification` table outright, so there is no constraint left to
        narrow and no row left to delete. Transcribing it would have been a
        delete-then-drop of the same rows.
      - ex-v1_11_0's downgrade was a deliberate no-op and stays one. Its two
        data steps are not reversible in a way worth having: the tenant_id
        backfill cannot distinguish "we set this" from "was already set"
        without bookkeeping the revision does not add, and deleting
        `tenant_usage` rows would discard real incremental accounting that
        happened after initialization. Dropping `tenant_usage` (section 12
        below) removes those rows anyway, which is the honest inverse.

    What this downgrade destroys, stated plainly: all 1.3-created data. The
    entire notification history, every usage counter and configured limit, all
    reconciliation history, and all remembered threshold state. Two of those
    are worth calling out because a re-upgrade does NOT restore them
    identically:

      - `tenant_usage` is re-seeded from the frozen baseline on re-upgrade, so
        any drift that incremental accounting had accumulated since the first
        upgrade is recomputed away rather than preserved. The recomputed value
        is correct by the Block C contract; it is simply not the number that
        was there before.
      - `tenant_usage_threshold_state` comes back empty, so every pair is
        "never evaluated" again. Worst case is one repeated notification for a
        tenant genuinely above a threshold — never a missed one, and never a
        wrong number, because state is re-derived from live usage and limits.

    `tenant.logo_url` is untouched throughout, so the `logo_storage_id`
    backfill recomputes to exactly the same result on re-upgrade. No S3 object
    is deleted by any step here.
    """
    # 15. Data migration — no inverse. See "What the squash changed here".
    #     The rows themselves go with the tables dropped in section 12.

    # 14. tenant_usage_threshold_state (Phase 4 Block G, ex-v1_13_0).
    #     Dropped before the usage tables purely for symmetry with upgrade();
    #     it references only `tenant`, which this revision does not own.
    op.drop_table("tenant_usage_threshold_state")

    # 13. tenant.logo_storage_id (Phase 4 Block D, ex-v1_12_0).
    #     Dropped before `storage_object.tenant_id` (section 4) so the FK is
    #     gone before anything else on storage_object is touched.
    op.drop_constraint(
        "fk_tenant_logo_storage_id_storage_object", "tenant", type_="foreignkey"
    )
    op.drop_column("tenant", "logo_storage_id")

    # 12. Usage domain (Phase 4 Block B, ex-v1_10_0, with Block D's hardening,
    #     ex-v1_12_0). Indexes are dropped explicitly ahead of their tables,
    #     matching the style of the sections below, even though DROP TABLE
    #     would remove them.
    op.drop_index("ix_app_user_tenant_id_is_active", table_name="app_user")

    op.execute("DROP INDEX IF EXISTS ix_tenant_usage_reconciliation_one_running")
    op.drop_index(
        "ix_tenant_usage_reconciliation_status_started_at",
        table_name="tenant_usage_reconciliation",
    )
    op.drop_index(
        "ix_tenant_usage_reconciliation_tenant_started_at",
        table_name="tenant_usage_reconciliation",
    )
    op.drop_table("tenant_usage_reconciliation")

    op.drop_table("tenant_limits")

    op.drop_table("tenant_usage")

    # 11 → 8. Notification domain (Phase 3 Blocks B/D/F).
    #
    # preference → delivery → recipient → notification: the two tables that
    # reference `notification` go first, and `notification_preference`, which
    # references neither, goes ahead of both.
    op.drop_index(
        "ix_notification_preference_user_id", table_name="notification_preference"
    )
    op.drop_index(
        "ix_notification_preference_tenant_id", table_name="notification_preference"
    )
    op.drop_table("notification_preference")

    op.drop_index(
        "uq_notification_delivery_recipient_address",
        table_name="notification_delivery",
    )
    op.drop_index(
        "uq_notification_delivery_recipient_user", table_name="notification_delivery"
    )
    op.drop_index("ix_notification_delivery_poller", table_name="notification_delivery")
    op.drop_index(
        "ix_notification_delivery_tenant_id", table_name="notification_delivery"
    )
    op.drop_index(
        "ix_notification_delivery_notification_id", table_name="notification_delivery"
    )
    op.drop_table("notification_delivery")

    op.drop_index(
        "ix_notification_recipient_inbox_created_at",
        table_name="notification_recipient",
    )
    op.drop_index(
        "ix_notification_recipient_inbox_status", table_name="notification_recipient"
    )
    op.drop_index(
        "ix_notification_recipient_notification_id",
        table_name="notification_recipient",
    )
    op.drop_table("notification_recipient")

    op.drop_index("ix_notification_tenant_type_created_at", table_name="notification")
    op.drop_index("ix_notification_tenant_resource", table_name="notification")
    op.drop_index("ix_notification_tenant_id", table_name="notification")
    # `locale` needs no separate drop — it is a column of this table.
    op.drop_table("notification")

    # 7. preferred_letterhead_id + publish lock
    op.drop_constraint(
        "fk_report_version_publish_started_by", "report_version", type_="foreignkey"
    )
    op.drop_column("report_version", "publish_started_by")
    op.drop_column("report_version", "publish_started_at")

    op.drop_constraint(
        "fk_report_template_preferred_letterhead_id", "report_template", type_="foreignkey"
    )
    op.drop_column("report_template", "preferred_letterhead_id")

    # 6. report_letterhead / report_letterhead_version
    op.drop_constraint(
        "fk_report_version_letterhead_version_id", "report_version", type_="foreignkey"
    )
    op.drop_column("report_version", "letterhead_version_id")

    op.drop_constraint(
        "fk_report_template_preferred_letterhead_version_id",
        "report_template",
        type_="foreignkey",
    )
    op.drop_column("report_template", "preferred_letterhead_version_id")

    op.execute("DROP INDEX IF EXISTS ix_report_letterhead_version_one_active")
    op.drop_index(
        "ix_report_letterhead_version_status", table_name="report_letterhead_version"
    )
    op.drop_index(
        "ix_report_letterhead_version_report_letterhead_id",
        table_name="report_letterhead_version",
    )
    op.drop_index(
        "ix_report_letterhead_version_tenant_id", table_name="report_letterhead_version"
    )
    op.drop_table("report_letterhead_version")

    op.execute("DROP INDEX IF EXISTS ix_report_letterhead_one_default")
    op.drop_index("ix_report_letterhead_tenant_id", table_name="report_letterhead")
    op.drop_table("report_letterhead")

    # 5. report_version PDF artifact fields
    op.drop_constraint(
        "ck_report_version_pdf_ready_requires_artifact", "report_version", type_="check"
    )
    op.drop_constraint(
        "ck_report_version_pdf_generation_status_values", "report_version", type_="check"
    )
    op.drop_index(
        "ix_report_version_pdf_generation_status", table_name="report_version"
    )
    op.drop_column("report_version", "pdf_error_message")
    op.drop_column("report_version", "pdf_error_code")
    op.drop_column("report_version", "pdf_generator_version")
    op.drop_column("report_version", "pdf_page_count")
    op.drop_column("report_version", "pdf_size_bytes")
    op.drop_column("report_version", "pdf_sha256")
    op.drop_column("report_version", "pdf_generated_at")
    op.drop_column("report_version", "pdf_generation_started_at")
    op.drop_column("report_version", "pdf_generation_status")

    # 4. storage_object.tenant_id
    op.drop_index("ix_storage_object_tenant_id", table_name="storage_object")
    op.drop_constraint("storage_object_tenant_id_fkey", "storage_object", type_="foreignkey")
    op.drop_column("storage_object", "tenant_id")

    # 3. report_version V2 metadata
    op.drop_constraint(
        "ck_report_version_v2_requires_template_version", "report_version", type_="check"
    )
    op.drop_index("ix_report_version_schema_version", table_name="report_version")
    op.drop_index("ix_report_version_template_version_id", table_name="report_version")
    op.drop_constraint(
        "report_version_template_version_id_fkey", "report_version", type_="foreignkey"
    )
    op.drop_column("report_version", "generated_by_renderer_version")
    op.drop_column("report_version", "template_version_id")
    op.drop_column("report_version", "schema_version")

    # 2. report_template_version
    op.execute("DROP INDEX IF EXISTS ix_report_template_version_one_active")
    op.drop_index("ix_report_template_version_status", table_name="report_template_version")
    op.drop_index(
        "ix_report_template_version_report_template_id", table_name="report_template_version"
    )
    op.drop_index("ix_report_template_version_tenant_id", table_name="report_template_version")
    op.drop_table("report_template_version")

    # 1. tenant.reports_v2_enabled
    op.drop_column("tenant", "reports_v2_enabled")
