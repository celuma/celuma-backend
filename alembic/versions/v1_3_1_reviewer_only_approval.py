"""v1.3.1 - Consolidated Céluma 1.3.1 release migration

Revision ID: v1_3_1
Revises: v1_3_0
Create Date: 2026-09-11

The single database revision for the Céluma 1.3.1 hotfix release. See
docs/celuma-1.3.1/CELUMA-1.3.1-HOTFIX-PLAN.md.

It carries THREE independent corrections, consolidated deliberately:

  1. **Block A / CEL-131-01** — revoke `reports:approve` from `pathologist`
     (DML). §1 below.
  2. **Block C / CEL-131-05** — drop
     `ck_report_version_v2_requires_template_version` (DDL). §2 below.
  3. **Block D / CEL-131-06 + CEL-131-04** — add `tenant.default_reviewer_id`
     (DDL) and bring the three official system metadata base fields into
     every existing `report_template.template_json`, present and visible
     (DML). §3 below.

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

===========================================================================
§3 — Block D (CEL-131-06 tenant default reviewer; CEL-131-04 report metadata)
===========================================================================

Two independent additions, both purely additive — no existing row, column or
constraint from §1/§2 is touched.

### §3a — `tenant.default_reviewer_id`

A nullable FK to `app_user.id`, `ON DELETE SET NULL`, `use_alter=True` for
the same reason `tenant.logo_storage_id` uses it: `tenant` and `app_user`
reference each other (`app_user.tenant_id -> tenant.id`), so the FK is
emitted separately rather than ordering the cycle. It is a CONFIGURATION
reference only — see
docs/celuma-1.3.1/block-d/default-reviewer-contract.md — and grants no role,
permission or capability by itself.

Downgrade: drops the FK and the column. This is a configuration pointer, not
a clinical record, so an ordinary drop (no precondition) is the correct and
sufficient downgrade — unlike §2, there is no clinical fact that could be
destroyed.

### §3b — the three official system metadata base fields

CEL-131-04 establishes three system base fields in the report's existing,
entirely frontend-defined `template_json.base` architecture (there is no
backend schema for it — `ReportTemplateCreate.template_json` is
`Dict[str, Any]`):

    requesting_physician    pre-existing key, reused rather than duplicated
    reception_date          new in 1.3.1
    delivery_date           new in 1.3.1

All three are **visible by default**, everywhere. That is the release
owner's decision: these are official report content, not opt-in extras.
New templates get them from `celuma-frontend/src/models/report.ts`
`DEFAULT_BASE_FIELDS`; existing templates get them from this migration.

Every EXISTING `report_template` row, across every tenant, ends this
migration with all three keys present in `base`, `is_visible: true`, and
listed in `base_order`. Three cases, per key per row:

  * **absent** -> created from the canonical definition (visible, default
    label, empty value);
  * **present but not visible** -> ONLY `is_visible` is set to `true`; a
    custom label, and every other key the document carries, is preserved
    verbatim;
  * **present and visible** -> untouched.

Everything else on the row — every unrelated base field, every custom base
field, every section, every setting — is preserved verbatim. `base_order`
keeps the tenant's existing order; a key is appended only when it is not
already listed, so no key is ever duplicated and no existing position moves.
Re-running the migration is a no-op, because all three conditions above are
already satisfied after the first run.

**Why an existing hidden visibility is overridden rather than preserved.**
A template carrying `requesting_physician` with `is_visible: false` may have
got that value two ways: from the pre-1.3.1 framework default (the frontend
merged the field in hidden, via `LEGACY_PREDEFINED_BASE_HIDDEN`), or from an
administrator deliberately hiding it. Nothing in the document records which
— there is no provenance marker to read, and the two states are
byte-identical. The 1.3.1 product contract resolves the ambiguity in favour
of visibility: an official system field the release owner has decided every
report shows is made visible on upgrade, and a laboratory that wants it
hidden can turn it off again in *Plantillas de Reporte* afterwards. This is
a deliberate, documented override of a state that cannot be attributed, not
an accident.

A row is skipped entirely (left byte-for-byte untouched) if `template_json`
is not a JSON object or its `base` is not a JSON object — the narrowest safe
behaviour for a column with no backend-enforced shape, rather than writing a
normalizer that guesses at a malformed document's intent.

This targets ONLY the live `ReportTemplate.template_json` used to create
FUTURE reports — never `ReportTemplateVersion.configuration` (administrative
history, immutable by Block C's own contract) and never any
`ReportVersion`'s already-persisted JSON body (existing reports, signed or
not, are not rewritten; see block-d/report-metadata-contract.md "Existing
report behaviour").

### §3b downgrade

Only the two keys this revision CREATED — `reception_date` and
`delivery_date` — are removable, and only from a row where the key is still
byte-identical to what the upgrade wrote (visible, default label,
`value: ""`), i.e. only where nothing has touched it since. A row where an
administrator has since changed the field's visibility, label or position is
left exactly as it is: the downgrade cannot distinguish "deliberately
customized" from "untouched", and unlike §2 this is template CONFIGURATION,
not a clinical record, so the fail-safe is a non-destructive skip rather
than refusing the whole migration.

`requesting_physician` is **never removed, and its visibility is never
restored.** The field predates 1.3.1, so deleting it would destroy
configuration the tenant had before this release; and for the same
provenance reason the upgrade documents above, the downgrade cannot know
whether a given template's pre-upgrade state was hidden-by-framework or
visible-by-choice. It therefore leaves the field visible. A rollback that
must restore a specific laboratory's hidden physician field does so by
editing the template, which is a one-click operation in the UI.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


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

# §3a
DEFAULT_REVIEWER_FK = "fk_tenant_default_reviewer_id_app_user"

# §3b — the three official system metadata fields, in the order the frontend's
# DEFAULT_BASE_FIELDS declares them (report.ts). All three are **visible**:
# the release owner's decision is that these are official report content, not
# opt-in extras, in new templates and existing ones alike.
#
# `requesting_physician` is in this list even though it predates 1.3.1: the
# migration does not *create* the concept, but it does guarantee the key
# exists and is visible, because older templates may either lack it entirely
# or carry it in the pre-1.3.1 framework default of `is_visible: false`.
SYSTEM_METADATA_FIELD_ORDER = [
    "requesting_physician",
    "reception_date",
    "delivery_date",
]
SYSTEM_METADATA_FIELDS = {
    "requesting_physician": {
        "is_visible": True,
        "label": "Médico solicitante",
        "value": "",
    },
    "reception_date": {
        "is_visible": True,
        "label": "Fecha de recepción",
        "value": "",
    },
    "delivery_date": {
        "is_visible": True,
        "label": "Fecha de entrega de resultados",
        "value": "",
    },
}

#: Keys created by this revision, i.e. the ones a downgrade may remove. The
#: physician field is deliberately absent: it predates 1.3.1, so removing it
#: on a rollback would delete a field the tenant had before this release.
DOWNGRADE_REMOVABLE_FIELDS = ["reception_date", "delivery_date"]


def _load_template_documents(bind):
    """Every `report_template` row, parsed, skipping any document this
    migration must not touch. Yields `(row_id, doc, base, base_order)`.

    A row is skipped when `template_json` is not a JSON object or its `base`
    is not a JSON object — the narrowest safe behaviour for a column with no
    backend-enforced shape (`ReportTemplateCreate.template_json` is
    `Dict[str, Any]`), rather than guessing at a malformed document's intent.
    """
    rows = bind.execute(
        sa.text("SELECT id, template_json::text FROM public.report_template")
    ).all()
    for row_id, raw in rows:
        if raw is None:
            continue
        try:
            doc = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        base = doc.get("base")
        if not isinstance(base, dict):
            continue
        base_order = doc.get("base_order")
        if not isinstance(base_order, list):
            base_order = list(base.keys())
        yield row_id, doc, base, base_order


def _write_template_document(bind, row_id, doc, base, base_order) -> None:
    doc["base"] = base
    doc["base_order"] = base_order
    bind.execute(
        sa.text(
            "UPDATE public.report_template "
            "SET template_json = CAST(:doc AS json) WHERE id = :id"
        ),
        {"doc": json.dumps(doc, ensure_ascii=False), "id": row_id},
    )


def _backfill_report_template_base_fields(bind) -> None:
    """§3b upgrade: every existing `report_template.template_json` ends with
    all three system metadata fields present, visible, and ordered. See the
    module docstring §3b for the safety rules and for why an existing hidden
    visibility is overridden rather than preserved."""
    for row_id, doc, base, base_order in _load_template_documents(bind):
        changed = False
        for key in SYSTEM_METADATA_FIELD_ORDER:
            existing = base.get(key)
            if not isinstance(existing, dict):
                # Absent (or stored as something that is not a field object):
                # create it from the canonical definition.
                base[key] = dict(SYSTEM_METADATA_FIELDS[key])
                changed = True
            elif existing.get("is_visible") is not True:
                # Present but hidden (or missing the flag entirely). Only the
                # visibility is touched — a custom label, and any other key the
                # document carries, is preserved verbatim.
                base[key] = {**existing, "is_visible": True}
                changed = True
            if key not in base_order:
                base_order.append(key)
                changed = True

        if changed:
            _write_template_document(bind, row_id, doc, base, base_order)


def _revert_report_template_base_fields(bind) -> None:
    """§3b downgrade: removes ONLY the two keys this revision created, and
    only from a row where the key is still byte-identical to what the upgrade
    wrote. A row an administrator has since customized is left exactly as it
    is.

    `requesting_physician` is never removed and its visibility is never
    restored: the field predates 1.3.1, and the upgrade cannot record whether
    a given template had it hidden by the old framework default or visible by
    the tenant's own choice. Guessing either way would destroy a real
    configuration, so the downgrade leaves it visible and says so — the same
    principle §2's downgrade follows for provenance it cannot reconstruct.
    """
    for row_id, doc, base, base_order in _load_template_documents(bind):
        changed = False
        for key in DOWNGRADE_REMOVABLE_FIELDS:
            if base.get(key) == SYSTEM_METADATA_FIELDS[key]:
                del base[key]
                changed = True
                if key in base_order:
                    base_order.remove(key)

        if changed:
            _write_template_document(bind, row_id, doc, base, base_order)


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

    # -- §3a --------------------------------------------------------------
    op.add_column(
        "tenant",
        sa.Column("default_reviewer_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        DEFAULT_REVIEWER_FK,
        "tenant",
        "app_user",
        ["default_reviewer_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # -- §3b --------------------------------------------------------------
    _backfill_report_template_base_fields(op.get_bind())


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

    # -- §3b --------------------------------------------------------------
    # No precondition: see the module docstring §3b downgrade. A customized
    # row is left exactly as it is rather than refusing the whole migration.
    _revert_report_template_base_fields(bind)

    # -- §3a --------------------------------------------------------------
    # A configuration pointer, not a clinical record — an ordinary drop is
    # the correct and sufficient downgrade.
    op.drop_constraint(DEFAULT_REVIEWER_FK, "tenant", type_="foreignkey")
    op.drop_column("tenant", "default_reviewer_id")
