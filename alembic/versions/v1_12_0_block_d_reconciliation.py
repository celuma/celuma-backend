"""v1.12.0 - Céluma 1.3, Phase 4, Block D: DB-scoped tenant logo &
reconciliation hardening

Revision ID: v1_12_0
Revises: v1_11_0
Create Date: 2026-08-11

Three changes, in order:

  1. `tenant.logo_storage_id` — a nullable FK to `storage_object.id`. This
     is the new canonical answer to "which StorageObject is this tenant's
     current logo". Until now the only answer was `tenant.logo_url`, a
     plain string that had to be parsed back into an object key by
     stripping the *currently configured* CDN prefix
     (`MEDIA_PUBLIC_BASE_URL`) — so changing that setting silently
     detached every tenant from its own logo for billing purposes (Block
     C's `block-d-dependencies.md` §6). `logo_url` is kept for API/
     presentation compatibility and is not removed in Céluma 1.3.

     No cascade on the FK, deliberately: deleting a StorageObject must not
     silently delete or blank a tenant's identity, and Block D's whole
     posture is that storage-integrity problems are reported, never
     auto-repaired.

  2. Backfill of that FK for existing tenants, resolved from persisted DB
     values only (see "Environment independence" below).

  3. Reconciliation hardening on `tenant_usage_reconciliation`:
     `metadata_mismatches_found` (a distinct integrity class from
     `missing_objects_found`, which must not be overloaded to mean it),
     and a partial unique index making two concurrent RUNNING runs for one
     tenant unrepresentable at the database level.

Environment independence
-------------------------
The backfill resolves a tenant's current logo from three persisted values
and nothing else:

    tenant.logo_url          (as stored, whenever it was stored)
    storage_object.tenant_id (relational ownership — never inferred from a key)
    storage_object.object_key

A candidate must belong to the tenant (`so.tenant_id = t.id`), fall in the
tenant-logo key family (`tenants/%/logo/%`), and have an `object_key` that
the persisted URL actually ends with, `'/' || object_key`, after query
string and fragment are stripped. `MEDIA_PUBLIC_BASE_URL`, `S3_BUCKET_NAME`
and `AWS_REGION` are never read: whichever CDN hostname produced the stored
URL, the suffix comparison resolves the same row. This is the same
DB-scoped rule `v1_11_0` now uses for its own historical baseline (that
revision's D0 correction), so the two agree by construction.

Ambiguity is never guessed. A tenant with two or more candidate rows
matching the same persisted URL — or with none — is left `NULL`, and
reconciliation reports it as `legacy_logo_reference_unresolved` for manual
resolution. A StorageObject belonging to a *different* tenant can never be
selected: the ownership predicate is on the relational column, not the key
string.

`downgrade()` reverses all three changes. The backfilled FK values are lost
on downgrade, which is correct: the column itself is what carries them, and
`logo_url` — the value they were derived from — is untouched by this
revision, so a re-upgrade recomputes exactly the same result.
"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "v1_12_0"
down_revision: Union[str, Sequence[str], None] = "v1_11_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


#: Candidate resolution, shared by the backfill and its reporting query.
#: `right(url, length(key) + 1) = '/' || key` is a plain suffix comparison
#: rather than a `LIKE` pattern on purpose: object keys legitimately contain
#: `%` and `_` (uploaded filenames are part of the key — see the sample-image
#: key layout), which a LIKE pattern would interpret as wildcards.
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
_BACKFILL_REPORT = _LOGO_CANDIDATES_CTE + """
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
    # 1. tenant.logo_storage_id — the canonical current-logo relationship
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
    # 2. Backfill, DB-scoped and environment independent (see docstring)
    # ------------------------------------------------------------------
    bind = op.get_bind()
    bind.execute(text(_BACKFILL_LOGO_STORAGE_ID))

    counts = bind.execute(text(_BACKFILL_REPORT)).mappings().one()
    logger.info(
        "v1_12_0 tenant-logo backfill: %s tenant(s) with logo_url, %s backfilled, "
        "%s ambiguous, %s left unresolved",
        counts["with_logo_url"],
        counts["backfilled"],
        counts["ambiguous"],
        counts["unresolved"],
    )

    # ------------------------------------------------------------------
    # 3a. metadata_mismatches_found — its own integrity class
    # ------------------------------------------------------------------
    # Block A named stale/mismatched metadata as a distinct failure mode
    # from a missing object: an object whose S3 size or ETag disagrees with
    # the DB row still exists, and conflating the two under
    # `missing_objects_found` would make "we may have lost clinical data"
    # indistinguishable from "a row is stale".
    op.add_column(
        "tenant_usage_reconciliation",
        sa.Column("metadata_mismatches_found", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_tenant_usage_reconciliation_metadata_mismatches_non_neg",
        "tenant_usage_reconciliation",
        "metadata_mismatches_found IS NULL OR metadata_mismatches_found >= 0",
    )

    # ------------------------------------------------------------------
    # 3b. At most one RUNNING reconciliation per tenant
    # ------------------------------------------------------------------
    # A database-level guarantee rather than an application convention,
    # deliberately: the API runs at `desired_count=1` today, so two
    # concurrent runs are currently unlikely — but "unlikely because there
    # happens to be one process" is not a property Céluma should have to
    # re-verify the day the service scales out. Same technique as
    # `ix_report_letterhead_one_default` and
    # `ix_report_template_version_one_active`.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_tenant_usage_reconciliation_one_running
        ON public.tenant_usage_reconciliation (tenant_id)
        WHERE (status = 'RUNNING')
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tenant_usage_reconciliation_one_running",
        table_name="tenant_usage_reconciliation",
    )
    op.drop_constraint(
        "ck_tenant_usage_reconciliation_metadata_mismatches_non_neg",
        "tenant_usage_reconciliation",
        type_="check",
    )
    op.drop_column("tenant_usage_reconciliation", "metadata_mismatches_found")

    op.drop_constraint(
        "fk_tenant_logo_storage_id_storage_object", "tenant", type_="foreignkey"
    )
    op.drop_column("tenant", "logo_storage_id")
