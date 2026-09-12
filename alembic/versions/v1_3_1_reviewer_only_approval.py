"""v1.3.1 - Consolidated Céluma 1.3.1 release migration

Revision ID: v1_3_1
Revises: v1_3_0
Create Date: 2026-09-11

The single database revision for the Céluma 1.3.1 hotfix release. See
docs/celuma-1.3.1/CELUMA-1.3.1-HOTFIX-PLAN.md.

It carries TWO independent corrections, consolidated deliberately:

  1. **Block A / CEL-131-01** — revoke `reports:approve` from `pathologist`
     (DML). §1 below.
  2. **Block C / CEL-131-05** — drop
     `ck_report_version_v2_requires_template_version` (DDL). §2 below.

### Why one revision rather than two

Céluma 1.3.1 has not shipped. This revision was written by Block A and has
never been applied to any production database, so amending it is not a
rewrite of deployed history — it is still the release's only unreleased
revision. The project's standing contract is one Alembic revision per
shipped product release (`tests/test_alembic_migrations.py`,
`TestReleaseRevisionChain`), and consolidating keeps that contract exactly
intact instead of carving an exception for a hotfix. `v1_3_1` becomes frozen
when 1.3.1 is released, not before.

`v1_3_0` is frozen and is NOT edited by this revision.

===========================================================================
§1 — Reviewer-only report approval (Block A, CEL-131-01)
===========================================================================

The `v1_0_0` seed granted `reports:approve` to `pathologist`, which made
every pathologist in a tenant able to approve any report in it: the
pre-1.3.1 approval guard read

    assigned reviewer  OR  has `reports:approve`

so the permission alone satisfied it. Approval is a clinical reviewer
action; a pathologist who is not the assigned reviewer must not perform it.

This is the DATA half of the fix and is NOT sufficient on its own. The CODE
half lives in `app/services/report_authorization.py`, which requires the
`reviewer` role AND the capability AND the `ReportReview` assignment. That
conjunction is what makes a future broad permission grant non-exploitable —
if this revision were ever reverted, or a later migration re-granted the
permission, the clinical boundary would still hold. Do not treat the two
halves as alternatives.

Scope, deliberately minimal:

  * `pathologist` LOSES `reports:approve`.
  * `reviewer` KEEPS `reports:approve` (untouched; seeded by `v1_1_0`).
  * `superuser` KEEPS `reports:approve` as part of holding the full
    catalogue. That is not a bypass: the role check in
    `report_authorization` rejects superuser for approval, signing and
    presentation changes because superuser does not hold the `reviewer`
    role. Stripping a permission from the "has everything" role to express a
    clinical rule would misrepresent what `superuser` is; the rule belongs in
    the authorization contract, where it is enforced for every actor at once.
  * Every other role/permission pair is untouched.

Idempotence and production safety: the upgrade is a targeted DELETE that
matches nothing if the grant is already absent, so it is safe to run against
any already-upgraded 1.3 database and safe to re-run. It touches exactly one
row of `role_permission` and no clinical data. Because `role.code` is
globally unique and no application code path writes `role_permission` (the
RBAC router is read-only for roles and permissions; only user-role
assignment is mutable), there is no per-tenant variant of this grant to
reconcile.

===========================================================================
§2 — Reports V2 no longer requires a template version (Block C, CEL-131-05)
===========================================================================

`v1_3_0` created

    ck_report_version_v2_requires_template_version
      CHECK (schema_version IS DISTINCT FROM 2 OR template_version_id IS NOT NULL)

That constraint encoded an early-1.3 assumption that a V2 report is always
born from a published, ACTIVE `report_template_version`. The final 1.3
architecture does not work that way: a V2 report is reconstructed
exclusively from the immutable `rendering_snapshot` embedded in its own JSON
body (see `ReportTemplateVersion`'s model docstring, and
`_carry_forward_v2_metadata` in `app/api/v1/reports.py`, which re-attaches
the frozen snapshot and deliberately never re-queries this table). The
creation-time source of truth for clinical structure is
`ReportTemplate.template_json`; presentation comes from
`resolve_effective_letterhead_version`. Neither needs a template version.

The constraint was nevertheless the last thing forcing one to exist, so a
laboratory that configured its template before its letterhead — leaving
`snapshot_and_activate_template_version` nothing to resolve and therefore no
version row — could not create a V2 report at all. That is the production
regression CEL-131-05 describes.

Dropping the CHECK is metadata-only in PostgreSQL: no table rewrite, no
lock beyond a brief ACCESS EXCLUSIVE, instant on any table size.

**The column stays.** `report_version.template_version_id`, its foreign key
and its index are untouched. Historical rows carry real provenance — which
published version a report was actually built from — and that is preserved
verbatim. Nothing is cleared, rewritten or backfilled in either direction.
After this revision, a V2 row may *honestly* hold NULL there, meaning "no
template version was read when this report was created".

### Downgrade precondition

Restoring the CHECK is only possible if no row would violate it. Rows
created after this revision may legitimately hold
`schema_version = 2 AND template_version_id IS NULL`, and there is no
truthful value to give them: binding them to whichever version happens to be
ACTIVE would fabricate provenance the report never had, and deleting them
would destroy clinical records.

So the downgrade **refuses, before mutating anything**, if any such row
exists, and names the rows. Clearing the offending `schema_version` would
silently demote real V2 reports to legacy and make them render through the
wrong renderer; that is a data-loss operation and is not something a
migration may decide on an operator's behalf.

A downgrade is therefore allowed to require operational preparation. It is
not allowed to falsify provenance. See
docs/celuma-1.3.1/block-c/block-c-summary.md for the rollback runbook.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v1_3_1"
down_revision: Union[str, Sequence[str], None] = "v1_3_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# §1
ROLE_CODE = "pathologist"
PERMISSION_CODE = "reports:approve"

# §2
V2_TEMPLATE_VERSION_CHECK = "ck_report_version_v2_requires_template_version"
V2_TEMPLATE_VERSION_CHECK_SQL = (
    "schema_version IS DISTINCT FROM 2 OR template_version_id IS NOT NULL"
)


def upgrade() -> None:
    # -- §1 -------------------------------------------------------------
    op.execute(
        f"""
        DELETE FROM public.role_permission rp
        USING public.role r, public.permission p
        WHERE rp.role_id = r.id
          AND rp.permission_id = p.id
          AND r.code = '{ROLE_CODE}'
          AND p.code = '{PERMISSION_CODE}'
        """
    )

    # -- §2 -------------------------------------------------------------
    # Idempotent: `IF EXISTS` so re-running against a database that has
    # already dropped it is a no-op rather than a failure, matching §1's
    # re-runnability. No row is read, written or backfilled.
    op.execute(
        "ALTER TABLE public.report_version "
        f"DROP CONSTRAINT IF EXISTS {V2_TEMPLATE_VERSION_CHECK}"
    )


def downgrade() -> None:
    """Returns the database to the `v1_3_0` contract.

    §1 deliberately re-opens CEL-131-01 at the data level, which is correct
    for a downgrade: its contract is "return the database to the v1_3_0
    state", and a downgrade is only ever run together with a rollback of the
    application code that enforces the reviewer contract.

    §2 refuses rather than falsifying provenance — see the module docstring.
    The precondition is checked FIRST, before either half mutates anything,
    so a refusal leaves the database exactly as it was even on a backend
    that does not wrap DDL in a transaction.
    """
    # -- §2 precondition, before any mutation ---------------------------
    bind = op.get_bind()
    offending = bind.execute(
        sa.text(
            "SELECT id FROM public.report_version "
            "WHERE schema_version = 2 AND template_version_id IS NULL "
            "ORDER BY id LIMIT 20"
        )
    ).scalars().all()
    if offending:
        total = bind.execute(
            sa.text(
                "SELECT count(*) FROM public.report_version "
                "WHERE schema_version = 2 AND template_version_id IS NULL"
            )
        ).scalar_one()
        listed = ", ".join(str(row) for row in offending)
        raise RuntimeError(
            f"Cannot downgrade v1_3_1 -> v1_3_0: {total} report_version row(s) have "
            f"schema_version = 2 with template_version_id IS NULL, which "
            f"{V2_TEMPLATE_VERSION_CHECK} forbids. These are legitimate Céluma 1.3.1 "
            "V2 reports created without reading a report_template_version; there is "
            "no truthful template_version_id to give them.\n\n"
            f"First {len(offending)} id(s): {listed}\n\n"
            "This migration will not resolve it automatically: binding these rows to "
            "an arbitrary ACTIVE version would fabricate provenance, and clearing "
            "schema_version would demote real V2 reports to legacy and render them "
            "through the wrong renderer. Decide explicitly and prepare the database "
            "before retrying — see docs/celuma-1.3.1/block-c/block-c-summary.md, "
            "\"Rollback\"."
        )

    # -- §1 -------------------------------------------------------------
    # Guarded by NOT EXISTS so it is idempotent and cannot violate the
    # composite primary key.
    op.execute(
        f"""
        INSERT INTO public.role_permission (role_id, permission_id)
        SELECT r.id, p.id
        FROM public.role r
        CROSS JOIN public.permission p
        WHERE r.code = '{ROLE_CODE}'
          AND p.code = '{PERMISSION_CODE}'
          AND NOT EXISTS (
              SELECT 1
              FROM public.role_permission existing
              WHERE existing.role_id = r.id
                AND existing.permission_id = p.id
          )
        """
    )

    # -- §2 -------------------------------------------------------------
    op.create_check_constraint(
        V2_TEMPLATE_VERSION_CHECK,
        "report_version",
        V2_TEMPLATE_VERSION_CHECK_SQL,
    )
