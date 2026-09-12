"""v1.3.1 - Reviewer-only report approval: revoke `reports:approve` from `pathologist`

Revision ID: v1_3_1
Revises: v1_3_0
Create Date: 2026-09-11

Céluma 1.3.1 hotfix, Block A / CEL-131-01. See
docs/celuma-1.3.1/CELUMA-1.3.1-HOTFIX-PLAN.md.

DML only — no DDL. The `v1_0_0` seed granted `reports:approve` to
`pathologist`, which made every pathologist in a tenant able to approve any
report in it: the pre-1.3.1 approval guard read

    assigned reviewer  OR  has `reports:approve`

so the permission alone satisfied it. Approval is a clinical reviewer action;
a pathologist who is not the assigned reviewer must not perform it.

This revision is the DATA half of the fix and is NOT sufficient on its own.
The CODE half lives in `app/services/report_authorization.py`, which requires
the `reviewer` role AND the capability AND the `ReportReview` assignment. That
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

`v1_3_0` is frozen and is NOT edited by this revision.

Idempotence and production safety: the upgrade is a targeted DELETE that
matches nothing if the grant is already absent, so it is safe to run against
any already-upgraded 1.3 database and safe to re-run. It touches exactly one
row of `role_permission` and no clinical data. Because `role.code` is globally
unique and no application code path writes `role_permission` (the RBAC router
is read-only for roles and permissions; only user-role assignment is mutable),
there is no per-tenant variant of this grant to reconcile.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "v1_3_1"
down_revision: Union[str, Sequence[str], None] = "v1_3_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROLE_CODE = "pathologist"
PERMISSION_CODE = "reports:approve"


def upgrade() -> None:
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


def downgrade() -> None:
    """Restores the pre-1.3.1 grant.

    This deliberately re-opens CEL-131-01 at the data level, which is correct
    for a downgrade: its contract is "return the database to the v1_3_0
    state", and a downgrade is only ever run together with a rollback of the
    application code that enforces the reviewer contract. Guarded by NOT
    EXISTS so it is idempotent and cannot violate the composite primary key.
    """
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
