"""v1.4.0 - List search, sample-type catalog and monthly numbering (Céluma 1.4 release migration, in development)

Revision ID: v1_4_0
Revises: v1_3_1
Create Date: 2026-10-14

The database revision for the Céluma 1.4 release, following the standing
contract of one Alembic revision per shipped product release
(`v1_0_0` … `v1_3_0`, `v1_3_1`). Later 1.4 areas that need schema changes
extend THIS revision (as Block C extended `v1_3_1`) until 1.4 ships; it
becomes frozen on release, not before.

### The identifier `v1_4_0` had two earlier, retired meanings

During Céluma 1.3 development `v1_4_0` named two short-lived revisions on the
`celuma-1.3` branch, both folded into `v1_3_0` and deleted:
`v1_4_0_report_template_version.py` (Phase 2 Block B, 2026-07-29 → 08-03) and
`v1_4_0_notifications_core.py` (Phase 3 Block B, 2026-08-06 → 08-10). Neither
was ever on a first-parent state of `main` or in a release tag, and CI only
migrated staging from `main`/tags, so no CI-deployed database carried them;
production went `v1_2_0` → `v1_3_0` → `v1_3_1` (1.3.1 release record). The
frozen `v1_3_0` docstring and the backend README already reserved `v1_4_0` for
Céluma 1.4, and Rafael decided (2026-10-14) to use it as planned. The
migration tests now allow `v1_4_0` only as this revision, in this file, on
`v1_3_1`; every other retired id stays forbidden. A database that reports
`v1_4_0` *before* the 1.4 release window would carry a retired meaning — see
the pre-upgrade check in `celuma-engineering/runbooks/migrations.md`.

`v1_3_0` and `v1_3_1` are frozen and are NOT edited by this revision.

===========================================================================
§1 — List search and pagination (issues #5/#6/#7)
===========================================================================

Specification: `celuma-engineering/specs/lists/search-and-pagination.md`;
decision record: `celuma-engineering/adr/0011-server-side-list-search.md`.

**§1a — `fuzzystrmatch`.** Server-paginated lists apply the same matching
contract as the browser, including conservative typo tolerance for human text
("biosia" → "Biopsia"). The edit distance is `levenshtein_less_equal`, from
the `fuzzystrmatch` contrib extension. It ships with PostgreSQL (and the
official images used locally and in CI), is supported by Amazon RDS for
PostgreSQL, and is a *trusted* extension since PostgreSQL 13, so it installs
for a role with CREATE on the database — the RDS master user the API connects
as qualifies. If it cannot be created the migration fails loudly; the API
does not fall back to a weaker search.

**§1b — indexes.** None of `sample`, `order`, `patient`, `report` had an index
on `tenant_id`, and `sample.order_id`, `order.patient_id`, `report.order_id`
and `report_version.report_id` had none either. Every list therefore scanned
the whole shared table — every laboratory's rows — so a small laboratory's
list got slower as other laboratories grew. On the synthetic shared database
of the list benchmark (`scripts/list_performance/`, 18 tenants / 185k samples)
these indexes made a small tenant's first page independent of the other
tenants' volume; the measurements are in
`celuma-engineering/specs/lists/performance-baseline.md`.

Plain `CREATE INDEX` is used, not `CONCURRENTLY`: the release runbook applies
migrations in a drained window (`celuma-engineering/runbooks/migrations.md`),
and building these indexes took well under a second each at the measured
scale. No `IF NOT EXISTS`: an index of the same name created by hand would be
a surprise worth stopping for. Nothing here rewrites or backfills data.

===========================================================================
§2 — Sample-type catalog; the `sampletype` enum is retired (issue #17)
===========================================================================

Specification: `celuma-engineering/specs/laboratory/sample-types.md`;
decision record: `celuma-engineering/adr/0012-sample-type-catalog.md`.

**§2a — `sample_type`.** A tenant-owned catalog with the persisted fields of
`study_type` (id, tenant_id, code, name, description, is_active,
default_report_template_id, created_at) in a separate table, plus a
`UNIQUE (tenant_id, code)` constraint — `study_type` has no such constraint,
but import/copy rely on it as the final guard against a concurrent duplicate.
The table is created EMPTY: the enum values are deliberately not seeded.

**§2b — `sample.sample_type_id`.** A nullable FK to `sample_type`
(`ON DELETE RESTRICT`). It is nullable for exactly one reason: legacy samples.
Every sample that exists at upgrade time gets NULL ("Sin tipo"). New samples
cannot be NULL — that is enforced by every creation route, not by the column.

**§2c — the enum column is dropped.** `sample.type` and the `sampletype` enum
are removed. Nothing is mapped, backfilled or copied: the product decision is
that enum values carry over to nothing. Identity, state, notes, images, order
links and labels of every sample are untouched (only this one column goes).

===========================================================================
§3 — Configurable monthly numbering for orders and samples (issue #8)
===========================================================================

Specification: `celuma-engineering/specs/laboratory/monthly-numbering.md`;
decision record: `celuma-engineering/adr/0013-monthly-numbering-counters.md`.

**§3a — `numbering_config`.** Per tenant and entity kind (`order`/`sample`):
`active` (deliberate activation), manual-code policy, format, minimum width,
start value, counter scope (tenant-wide or per branch) and the numbering
timezone (IANA) that defines the month of tenant-wide counters. Created EMPTY: a
tenant without a row is NOT activated, so applying this migration changes no
laboratory's behaviour (orders keep `<code>-<n>`, now from a race-safe
compatibility counter instead of `count + 1`; samples stay typed by hand).
Activation is an explicit, audited action of an administrator.

**§3b — `numbering_counter`.** The monotonic counter, keyed by tenant, entity
kind, scope id (study-type or sample-type id), branch (the nil UUID for
tenant-wide and compatibility counters) and period (year/month; 0/0 for the
compatibility counters). Empty. `scope_id` has deliberately NO foreign key:
the counter must outlive the catalog row.

**§3c — `numbering_allocation`.** The permanent ledger of generated codes,
unique per tenant + kind + code and per counter + sequence value. No foreign
key to orders, samples or catalogs, so deleting any of them cannot release a
number. Empty.

**§3d — lookup indexes.** `ix_order_tenant_order_code` and
`ix_sample_tenant_sample_code` (plain, non-unique): the availability checks of
generation and the manual-code checks look codes up by tenant. They are NOT
unique because legacy data may contain duplicates (sample codes were only ever
unique per order). Nothing in `order`/`sample` is rewritten.

**§3e — idempotency and the manual-code capability.** `idempotency_receipt`
(tenant + actor + operation + key unique; SHA-256 fingerprint; the ids and
codes it produced; no foreign keys to resources), written in the same
transaction as the work it describes. And the permission `lab:enter_manual_code`,
inserted into `permission` and granted to `admin` and `superuser` only.

**Downgrade of §3 drops the four tables and the permission:** counters, ledger
and receipts are lost. A later re-upgrade restarts sequences and generation
skips every value whose code still exists, so it cannot collide with orders or
samples that are still there (only retired, deleted numbers become reusable and
old idempotency keys are forgotten); prefer a snapshot restore.

**Downgrade is LOSSY.** It recreates `sampletype`, adds `sample.type NOT NULL`
and must give every row *some* value: it writes `OTRO` for ALL samples. The
discarded enum assignments cannot be reconstructed from NULL, and catalog
assignments made after the upgrade are lost with the table. The only way to get
the real 1.3.x assignments back is a backup/snapshot taken before the upgrade
(`celuma-engineering/runbooks/migrations.md`, "Sample-type catalog").

===========================================================================
§4 — Order/sample management: edits, code correction, removal, physicians
===========================================================================

Specification: `celuma-engineering/specs/laboratory/order-sample-management.md`;
decision record: `celuma-engineering/adr/0015-order-sample-management.md`.
Issues #9, #10, #11 and #16.

**§4a — `order.expediente`.** Nullable `varchar(100)`: the optional textual
request reference (a string, so leading zeroes survive). Existing rows get NULL.

**§4b — `order_requesting_physician`.** The duplicate-free, same-tenant set of
external requesting physicians of an order (`position` is display order only; no
primary). **Backfill:** each existing order that has `requesting_physician_id`
gets exactly one row (position 0) when the physician is of the same tenant, so
nothing is lost and no primary is invented. `order.requesting_physician_id` and
`order.requested_by` are NOT modified: they remain the legacy single-physician /
free-text fallback and are only mirrored by the application when exactly one
physician is listed.

**§4c — tombstone columns.** `removed_at`, `removed_by`, `removal_reason` on
`order` and `sample`, and `removed_at` on `report` (all nullable, metadata-only
`ADD COLUMN`; nothing is rewritten). A tombstoned row keeps its identifiers.

**§4d — `sample_code_correction`.** Audit trail and retirement record of
sample-code corrections (empty).

**§4e — event types and permission.** `ALTER TYPE eventtype ADD VALUE` for
`ORDER_UPDATED`, `ORDER_REMOVED`, `SAMPLE_CODE_CORRECTED`, `SAMPLE_REMOVED`; and
the permission `lab:remove_capture`, granted to `admin` and `superuser` only.

**Downgrade of §4.** REFUSED while any order, sample or report is tombstoned: a
1.3.1 application would show the erroneous captures again. Restore from a
pre-upgrade snapshot instead (runbook). Otherwise it drops the new columns and
tables (the expediente and every physician beyond the legacy single one are
lost), deletes the timeline/audit rows of the four new event types and removes
the permission. PostgreSQL cannot drop enum values, so the four values stay in
`eventtype` (harmless: no row uses them after the downgrade).

===========================================================================
§5 — Password-confirmed signing and reopening (issue #13)
===========================================================================

Specification: `celuma-engineering/specs/reports/lifecycle.md` ("Password
confirmation"); decision record:
`celuma-engineering/adr/0017-password-confirmed-clinical-actions.md`.

**§5a — `ix_audit_log_password_confirmation_failures`.** Signing and
reopening require the actor's own password. A wrong one writes a
credential-free `audit_log` row with action
`SECURITY.PASSWORD_CONFIRMATION_FAILED`, and the per-actor throttle counts
those rows in a sliding window before every confirmation. `audit_log` has no
index on actor or action, so without this the count would scan the whole
table on every signature. It is PARTIAL (only those rows), so it stays tiny
and costs other audit writes nothing. Building it reads `audit_log` once; plain
`CREATE INDEX`, like §1b, in the drained migration window. No row is written or
rewritten, and no enum value is added (the action is free text; the timeline
events of #13 use existing `eventtype` members).

**Downgrade of §5** drops the index only. The failure audit rows stay: they
are audit history, and 1.3.1 ignores them.

===========================================================================
§6 — Amendments of signed reports (issue #13)
===========================================================================

Specification: `celuma-engineering/specs/reports/lifecycle.md` ("Amendments of
signed reports"); decision record:
`celuma-engineering/adr/0018-report-amendments-as-emissions.md`.

**§6a — `report_amendment`.** One row per amendment of a signed V2 report:
tenant, report, per-report `amendment_no`, the signed `source_version_id` it
amends, category (CORRECTION/SUPPLEMENT), reason, state (OPEN, PUBLISHED,
CANCELLED), requester, the review decisions at opening (restored on
cancellation), the version that republished it and the cancellation record.
`UNIQUE (report_id, amendment_no)` and the partial unique index
`uq_report_amendment_one_open` (one OPEN amendment per report) are the
database-level guards behind exactly-once reopening. Created EMPTY.

**§6b — `report_version.amendment_id`.** Nullable FK: the amendment a version
was authored in. Every existing version gets NULL (an original cycle); nothing
is backfilled or rewritten.

**§6c — notification type `REPORT_REPUBLISHED`.** The in-app republication
notice (owner decision D4). `notification.type` and
`notification_preference.notification_type` are strings, but `v1_3_0` pins
both to the permitted set with `ck_notification_type` and
`ck_notification_preference_type`; both are recreated with the same ten values
plus this one. A constraint swap, no row is touched.

No `eventtype` value is added: reopening for amendment and cancellation use
`STATUS_CHANGED` and republication uses `REPORT_PUBLISHED`, distinguished by
`metadata.action`.

**Downgrade of §6 is REFUSED while any amendment exists**, open or closed: the
rows are clinical history (reason, requester, superseded emission, cancellation)
that 1.3.1 cannot represent. Restore the pre-upgrade snapshot instead. Without
amendments it deletes any `REPORT_REPUBLISHED` preference rows (user settings
only; no such notification can exist without an amendment), restores both
constraints to the `v1_3_0` set, and drops the column and the table.

==================================================================================================================================================
§7 — Laboratory-content cleanup (issue #39)
===========================================================================

Specification: `celuma-engineering/specs/laboratory/laboratory-content-cleanup.md`;
decision record: `celuma-engineering/adr/0021-laboratory-content-cleanup.md`;
operator procedure: `celuma-engineering/runbooks/laboratory-content-cleanup.md`.

ADDITIVE ONLY: three empty tables, one nullable tenant column and one trigger.
Nothing existing is rewritten and no laboratory is cleaned.

**§7a — `tenant.created_by_user_id`.** The persisted binding to the person who
CREATED the laboratory during initial registration (owner decision, 2026-10-28):
the only holder of the cleanup capability. Set atomically by
`POST /auth/register/unified`. It is NULL for every existing laboratory — there
is no reliable provenance for "who created it" in the 1.3.1 schema (the oldest
or the current `superuser` account would be a guess), so the feature FAILS CLOSED
for them until a controlled, explicit one-time backfill (runbook). A trigger makes
the column WRITE-ONCE: NULL -> value is allowed (registration, or the documented
backfill), changing or clearing a value is refused for every client, API or SQL.

**§7b — `laboratory_cleanup_operation`.** The durable state machine of one
confirmed cleanup (status, phase, lease, approved preview counts, reason, safe
errors). A partial unique index allows at most one OPEN (QUEUED/RUNNING/FAILED)
operation per tenant, which is also what rejects a duplicate concurrent start. It
references only the tenant: it is independent technical evidence that outlives the
graph it removed and holds no business or clinical payload.

**§7c — `laboratory_cleanup_object`.** The object-store deletion outbox, written
in the same transaction as the database deletion, with a copy of bucket/key
(the `storage_object` row is gone by then). No foreign key to storage.

**§7d — `laboratory_cleanup_retired_code`.** Every order/sample code a cleanup
removed, consulted by numbering availability so manual/legacy codes the ledger
never saw are never reissued.

DOWNGRADE is refused while any operation row exists (they are evidence of an
irreversible action); with none, the objects are dropped (the creator binding is
lost with its column). A `v1_4_0` stamp does not prove §7 ran: check the objects
(runbooks/migrations.md).
"""
from typing import Sequence, Union

from alembic import op


revision: str = "v1_4_0"
down_revision: Union[str, Sequence[str], None] = "v1_3_1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: (index name, table, column). Quoted table names: `order` is reserved.
LIST_INDEXES = (
    ("ix_sample_tenant_id", "sample", "tenant_id"),
    ("ix_sample_order_id", "sample", "order_id"),
    ("ix_order_tenant_id", '"order"', "tenant_id"),
    ("ix_order_patient_id", '"order"', "patient_id"),
    ("ix_patient_tenant_id", "patient", "tenant_id"),
    ("ix_report_tenant_id", "report", "tenant_id"),
    ("ix_report_order_id", "report", "order_id"),
    ("ix_report_version_report_id", "report_version", "report_id"),
)


#: Capability to type a code while numbering is active (§3e).
MANUAL_CODE_PERMISSION = "lab:enter_manual_code"

#: Capability to remove an erroneous capture (§4e).
REMOVE_CAPTURE_PERMISSION = "lab:remove_capture"
#: Event types added by §4e (PostgreSQL cannot drop enum values, so the downgrade keeps them).
MANAGEMENT_EVENT_TYPES = ("ORDER_UPDATED", "ORDER_REMOVED", "SAMPLE_CODE_CORRECTED", "SAMPLE_REMOVED")

#: Partial index behind the password-confirmation throttle (§5a).
PASSWORD_FAILURE_INDEX = "ix_audit_log_password_confirmation_failures"
PASSWORD_FAILURE_ACTION = "SECURITY.PASSWORD_CONFIRMATION_FAILED"

#: The amendment record (§6).
AMENDMENT_TABLE = "report_amendment"

#: The notification types `v1_3_0` admits (frozen copy) and the one §6c adds.
V1_3_NOTIFICATION_TYPES = (
    "REPORT_SUBMITTED", "REPORT_PDF_READY", "REPORT_PUBLISHED", "REPORT_RETRACTED",
    "ASSIGNMENT_ADDED", "SAMPLE_STATUS_CHANGED", "STORAGE_USAGE_APPROACHING",
    "STORAGE_LIMIT_REACHED", "USER_LIMIT_APPROACHING", "USER_LIMIT_REACHED",
)
REPUBLISHED_NOTIFICATION_TYPE = "REPORT_REPUBLISHED"


def _notification_type_checks(types) -> None:
    """Recreate both notification-domain CHECK constraints over `types`."""
    rendered = ", ".join(f"'{value}'" for value in types)
    for table, column, name in (
        ("notification", "type", "ck_notification_type"),
        ("notification_preference", "notification_type", "ck_notification_preference_type"),
    ):
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE public.{table} ADD CONSTRAINT {name} CHECK ({column} IN ({rendered}))")

#: Values of the retired `sampletype` enum; only the downgrade recreates it.
RETIRED_SAMPLE_TYPE_VALUES = ("SANGRE", "BIOPSIA", "LAMINILLA", "TEJIDO", "OTRO")


def upgrade() -> None:
    # §1a
    op.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch")
    # §1b
    for name, table, column in LIST_INDEXES:
        op.execute(f"CREATE INDEX {name} ON public.{table} USING btree ({column})")
    # §2a
    op.execute("""
        CREATE TABLE public.sample_type (
            created_at                  timestamp without time zone NOT NULL,
            id                          uuid NOT NULL,
            tenant_id                   uuid NOT NULL,
            code                        character varying(50) NOT NULL,
            name                        character varying(255) NOT NULL,
            description                 character varying,
            is_active                   boolean NOT NULL,
            default_report_template_id  uuid,
            CONSTRAINT sample_type_pkey PRIMARY KEY (id),
            CONSTRAINT uq_sample_type_tenant_code UNIQUE (tenant_id, code),
            CONSTRAINT sample_type_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT fk_sample_type_report_template FOREIGN KEY (default_report_template_id) REFERENCES public.report_template(id)
        )
    """)
    op.execute("CREATE INDEX ix_sample_type_tenant_id ON public.sample_type USING btree (tenant_id)")
    op.execute("CREATE INDEX ix_sample_type_code ON public.sample_type USING btree (code)")
    op.execute("CREATE INDEX ix_sample_type_tenant_active ON public.sample_type USING btree (tenant_id, is_active)")
    # §2b — every existing row starts NULL ("Sin tipo")
    op.execute("ALTER TABLE public.sample ADD COLUMN sample_type_id uuid")
    op.execute(
        "ALTER TABLE ONLY public.sample ADD CONSTRAINT sample_sample_type_id_fkey "
        "FOREIGN KEY (sample_type_id) REFERENCES public.sample_type(id) ON DELETE RESTRICT"
    )
    op.execute("CREATE INDEX ix_sample_sample_type_id ON public.sample USING btree (sample_type_id)")
    # §2c
    op.execute("ALTER TABLE public.sample DROP COLUMN type")
    op.execute("DROP TYPE public.sampletype")
    # §3a-§3d (empty tables; nothing existing is rewritten)
    op.execute("""
        CREATE TABLE public.numbering_config (
            tenant_id           uuid NOT NULL,
            entity_kind         character varying(16) NOT NULL,
            active              boolean NOT NULL,
            manual_policy       character varying(12) NOT NULL,
            format              character varying(60) NOT NULL,
            sequence_min_width  integer NOT NULL,
            start_value         integer NOT NULL,
            scope               character varying(8) NOT NULL,
            timezone            character varying(100) NOT NULL,
            activated_at        timestamp without time zone,
            updated_at          timestamp without time zone NOT NULL,
            updated_by          uuid,
            CONSTRAINT numbering_config_pkey PRIMARY KEY (tenant_id, entity_kind),
            CONSTRAINT numbering_config_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT numbering_config_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.app_user(id),
            CONSTRAINT ck_numbering_config_kind CHECK (entity_kind IN ('order', 'sample')),
            CONSTRAINT ck_numbering_config_policy CHECK (manual_policy IN ('allowed', 'forbidden', 'required')),
            CONSTRAINT ck_numbering_config_scope CHECK (scope IN ('tenant', 'branch')),
            CONSTRAINT ck_numbering_config_width CHECK (sequence_min_width BETWEEN 1 AND 6),
            CONSTRAINT ck_numbering_config_start CHECK (start_value BETWEEN 1 AND 99999999)
        )
    """)
    op.execute("""
        CREATE TABLE public.numbering_counter (
            tenant_id     uuid NOT NULL,
            entity_kind   character varying(16) NOT NULL,
            scope_id      uuid NOT NULL,
            branch_id     uuid NOT NULL,
            period_year   integer NOT NULL,
            period_month  integer NOT NULL,
            last_value    integer NOT NULL,
            updated_at    timestamp without time zone NOT NULL,
            CONSTRAINT numbering_counter_pkey PRIMARY KEY (tenant_id, entity_kind, scope_id, branch_id, period_year, period_month),
            CONSTRAINT numbering_counter_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT ck_numbering_counter_kind CHECK (entity_kind IN ('order', 'sample')),
            CONSTRAINT ck_numbering_counter_month CHECK (period_month BETWEEN 0 AND 12),
            CONSTRAINT ck_numbering_counter_value CHECK (last_value >= 0)
        )
    """)
    op.execute("""
        CREATE TABLE public.numbering_allocation (
            id                  uuid NOT NULL,
            tenant_id           uuid NOT NULL,
            entity_kind         character varying(16) NOT NULL,
            scope_id            uuid NOT NULL,
            branch_id           uuid NOT NULL,
            period_year         integer NOT NULL,
            period_month        integer NOT NULL,
            sequence_value      integer NOT NULL,
            code                character varying(100) NOT NULL,
            scheme              character varying(8) NOT NULL,
            format              character varying(60) NOT NULL,
            sequence_min_width  integer NOT NULL,
            entity_id           uuid,
            created_by          uuid,
            created_at          timestamp without time zone NOT NULL,
            CONSTRAINT numbering_allocation_pkey PRIMARY KEY (id),
            CONSTRAINT numbering_allocation_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT uq_numbering_allocation_code UNIQUE (tenant_id, entity_kind, code),
            CONSTRAINT uq_numbering_allocation_value UNIQUE (tenant_id, entity_kind, scope_id, branch_id, period_year, period_month, sequence_value),
            CONSTRAINT ck_numbering_allocation_kind CHECK (entity_kind IN ('order', 'sample')),
            CONSTRAINT ck_numbering_allocation_scheme CHECK (scheme IN ('monthly', 'legacy'))
        )
    """)
    op.execute("""
        CREATE TABLE public.idempotency_receipt (
            id                   uuid NOT NULL,
            tenant_id            uuid NOT NULL,
            actor_user_id        uuid NOT NULL,
            operation            character varying(32) NOT NULL,
            idempotency_key      character varying(128) NOT NULL,
            request_fingerprint  character varying(64) NOT NULL,
            response_status      integer NOT NULL,
            result               json NOT NULL,
            resources            json NOT NULL,
            created_at           timestamp without time zone NOT NULL,
            CONSTRAINT idempotency_receipt_pkey PRIMARY KEY (id),
            CONSTRAINT idempotency_receipt_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT uq_idempotency_receipt_key UNIQUE (tenant_id, actor_user_id, operation, idempotency_key)
        )
    """)
    op.execute("CREATE INDEX ix_idempotency_receipt_tenant_id ON public.idempotency_receipt USING btree (tenant_id)")
    # §3e — the capability to type a code while numbering is active. Granted to admin and superuser
    # only; every other role (and any custom role) must be given it deliberately.
    op.execute(f"""
        INSERT INTO public.permission (id, code, domain, display_name, description)
        VALUES (gen_random_uuid(), '{MANUAL_CODE_PERMISSION}', 'lab', 'Escribir códigos manualmente',
                'Escribir el código de una orden o muestra en lugar de usar la numeración automática')
    """)
    op.execute(f"""
        INSERT INTO public.role_permission (role_id, permission_id)
        SELECT r.id, p.id FROM public.role r CROSS JOIN public.permission p
        WHERE r.code IN ('admin', 'superuser') AND p.code = '{MANUAL_CODE_PERMISSION}'
    """)
    op.execute("CREATE INDEX ix_numbering_allocation_tenant_id ON public.numbering_allocation USING btree (tenant_id)")
    op.execute("CREATE INDEX ix_numbering_allocation_entity ON public.numbering_allocation USING btree (entity_id)")
    op.execute('CREATE INDEX ix_order_tenant_order_code ON public."order" USING btree (tenant_id, order_code)')
    op.execute("CREATE INDEX ix_sample_tenant_sample_code ON public.sample USING btree (tenant_id, sample_code)")
    # §4a-§4e — order/sample management (#9, #10, #11, #16). Nullable columns and empty tables, plus a
    # one-row-per-order backfill into the new physician table; no existing order/sample value changes.
    op.execute('ALTER TABLE public."order" ADD COLUMN expediente character varying(100)')
    for table in ('public."order"', "public.sample"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN removed_at timestamp without time zone")
        op.execute(f"ALTER TABLE {table} ADD COLUMN removed_by uuid")
        op.execute(f"ALTER TABLE {table} ADD COLUMN removal_reason text")
    op.execute('ALTER TABLE public."order" ADD CONSTRAINT order_removed_by_fkey FOREIGN KEY (removed_by) REFERENCES public.app_user(id)')
    op.execute("ALTER TABLE public.sample ADD CONSTRAINT sample_removed_by_fkey FOREIGN KEY (removed_by) REFERENCES public.app_user(id)")
    op.execute("ALTER TABLE public.report ADD COLUMN removed_at timestamp without time zone")
    op.execute("""
        CREATE TABLE public.order_requesting_physician (
            order_id                uuid NOT NULL,
            requesting_physician_id uuid NOT NULL,
            tenant_id               uuid NOT NULL,
            position                integer NOT NULL,
            created_at              timestamp without time zone NOT NULL,
            CONSTRAINT order_requesting_physician_pkey PRIMARY KEY (order_id, requesting_physician_id),
            CONSTRAINT uq_order_requesting_physician_position UNIQUE (order_id, position),
            CONSTRAINT order_requesting_physician_order_id_fkey FOREIGN KEY (order_id) REFERENCES public."order"(id),
            CONSTRAINT order_requesting_physician_physician_fkey FOREIGN KEY (requesting_physician_id) REFERENCES public.requesting_physician(id),
            CONSTRAINT order_requesting_physician_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT ck_order_requesting_physician_position CHECK (position >= 0)
        )
    """)
    op.execute("CREATE INDEX ix_order_requesting_physician_tenant_id ON public.order_requesting_physician USING btree (tenant_id)")
    op.execute("CREATE INDEX ix_order_requesting_physician_physician ON public.order_requesting_physician USING btree (requesting_physician_id)")
    op.execute("""
        INSERT INTO public.order_requesting_physician (order_id, requesting_physician_id, tenant_id, position, created_at)
        SELECT o.id, o.requesting_physician_id, o.tenant_id, 0, now()
        FROM public."order" o
        JOIN public.requesting_physician p ON p.id = o.requesting_physician_id AND p.tenant_id = o.tenant_id
        WHERE o.requesting_physician_id IS NOT NULL
    """)
    op.execute("""
        CREATE TABLE public.sample_code_correction (
            id            uuid NOT NULL,
            tenant_id     uuid NOT NULL,
            sample_id     uuid NOT NULL,
            order_id      uuid NOT NULL,
            old_code      character varying(100) NOT NULL,
            new_code      character varying(100) NOT NULL,
            reason        text NOT NULL,
            corrected_by  uuid,
            corrected_at  timestamp without time zone NOT NULL,
            CONSTRAINT sample_code_correction_pkey PRIMARY KEY (id),
            CONSTRAINT sample_code_correction_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT sample_code_correction_corrected_by_fkey FOREIGN KEY (corrected_by) REFERENCES public.app_user(id)
        )
    """)
    op.execute("CREATE INDEX ix_sample_code_correction_tenant_id ON public.sample_code_correction USING btree (tenant_id)")
    op.execute("CREATE INDEX ix_sample_code_correction_sample_id ON public.sample_code_correction USING btree (sample_id)")
    op.execute("CREATE INDEX ix_sample_code_correction_tenant_old_code ON public.sample_code_correction USING btree (tenant_id, old_code)")
    for value in MANAGEMENT_EVENT_TYPES:
        op.execute(f"ALTER TYPE public.eventtype ADD VALUE IF NOT EXISTS '{value}'")
    op.execute(f"""
        INSERT INTO public.permission (id, code, domain, display_name, description)
        VALUES (gen_random_uuid(), '{REMOVE_CAPTURE_PERMISSION}', 'lab', 'Retirar capturas erróneas',
                'Retirar una orden o muestra capturada por error antes de la revisión del reporte')
    """)
    op.execute(f"""
        INSERT INTO public.role_permission (role_id, permission_id)
        SELECT r.id, p.id FROM public.role r CROSS JOIN public.permission p
        WHERE r.code IN ('admin', 'superuser') AND p.code = '{REMOVE_CAPTURE_PERMISSION}'
    """)
    # §5a — the throttle's lookup; partial, nothing rewritten.
    op.execute(
        f"CREATE INDEX {PASSWORD_FAILURE_INDEX} ON public.audit_log "
        f"USING btree (actor_user_id, created_at) WHERE action = '{PASSWORD_FAILURE_ACTION}'"
    )
    # §6a — amendments (empty)
    op.execute("""
        CREATE TABLE public.report_amendment (
            created_at            timestamp without time zone NOT NULL,
            id                    uuid NOT NULL,
            tenant_id             uuid NOT NULL,
            report_id             uuid NOT NULL,
            amendment_no          integer NOT NULL,
            source_version_id     uuid NOT NULL,
            category              character varying(20) NOT NULL,
            reason                character varying(1000) NOT NULL,
            state                 character varying(20) NOT NULL,
            requested_by          uuid NOT NULL,
            requested_at          timestamp without time zone NOT NULL,
            reviews_at_opening    json,
            published_version_id  uuid,
            published_at          timestamp without time zone,
            cancelled_by          uuid,
            cancelled_at          timestamp without time zone,
            cancel_reason         character varying(1000),
            CONSTRAINT report_amendment_pkey PRIMARY KEY (id),
            CONSTRAINT uq_report_amendment_no UNIQUE (report_id, amendment_no),
            CONSTRAINT ck_report_amendment_state CHECK (state IN ('OPEN', 'PUBLISHED', 'CANCELLED')),
            CONSTRAINT ck_report_amendment_category CHECK (category IN ('CORRECTION', 'SUPPLEMENT')),
            CONSTRAINT report_amendment_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT report_amendment_report_id_fkey FOREIGN KEY (report_id) REFERENCES public.report(id),
            CONSTRAINT report_amendment_source_version_id_fkey FOREIGN KEY (source_version_id) REFERENCES public.report_version(id),
            CONSTRAINT report_amendment_published_version_id_fkey FOREIGN KEY (published_version_id) REFERENCES public.report_version(id),
            CONSTRAINT report_amendment_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES public.app_user(id),
            CONSTRAINT report_amendment_cancelled_by_fkey FOREIGN KEY (cancelled_by) REFERENCES public.app_user(id)
        )
    """)
    op.execute("CREATE INDEX ix_report_amendment_tenant_id ON public.report_amendment USING btree (tenant_id)")
    op.execute("CREATE INDEX ix_report_amendment_report_id ON public.report_amendment USING btree (report_id)")
    op.execute(
        "CREATE UNIQUE INDEX uq_report_amendment_one_open ON public.report_amendment "
        "USING btree (report_id) WHERE state = 'OPEN'"
    )
    # §6b — nullable link from a version to its amendment; nothing backfilled
    op.execute("ALTER TABLE public.report_version ADD COLUMN amendment_id uuid")
    op.execute(
        "ALTER TABLE ONLY public.report_version ADD CONSTRAINT report_version_amendment_id_fkey "
        "FOREIGN KEY (amendment_id) REFERENCES public.report_amendment(id)"
    )
    op.execute("CREATE INDEX ix_report_version_amendment_id ON public.report_version USING btree (amendment_id)")
    # §6c — admit the republication notification type
    _notification_type_checks(V1_3_NOTIFICATION_TYPES + (REPUBLISHED_NOTIFICATION_TYPE,))
    # §7a-§7d — laboratory-content cleanup (#39): empty tables, one nullable column, one write-once trigger.
    op.execute("ALTER TABLE public.tenant ADD COLUMN created_by_user_id uuid")
    op.execute(
        "ALTER TABLE ONLY public.tenant ADD CONSTRAINT fk_tenant_created_by_user_id_app_user "
        "FOREIGN KEY (created_by_user_id) REFERENCES public.app_user(id) ON DELETE SET NULL"
    )
    op.execute("""
        CREATE FUNCTION public.tenant_creator_write_once() RETURNS trigger LANGUAGE plpgsql AS $fn$
        BEGIN
            IF OLD.created_by_user_id IS NOT NULL AND NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id THEN
                RAISE EXCEPTION 'tenant.created_by_user_id is write-once' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $fn$
    """)
    op.execute(
        "CREATE TRIGGER trg_tenant_creator_write_once BEFORE UPDATE OF created_by_user_id ON public.tenant "
        "FOR EACH ROW EXECUTE FUNCTION public.tenant_creator_write_once()"
    )
    op.execute("""
        CREATE TABLE public.laboratory_cleanup_operation (
            id                       uuid NOT NULL,
            tenant_id                uuid NOT NULL,
            actor_user_id            uuid NOT NULL,
            status                   character varying(16) NOT NULL,
            phase                    character varying(16) NOT NULL,
            reason                   text NOT NULL,
            laboratory_name          character varying(255) NOT NULL,
            scope_revision           character varying(64) NOT NULL,
            confirmation_jti         character varying(64) NOT NULL,
            confirmation_fingerprint character varying(64) NOT NULL,
            approved_counts          json NOT NULL,
            executed_counts          json,
            error_code               character varying(64),
            error_message            character varying(500),
            attempts                 integer NOT NULL DEFAULT 0,
            lease_owner              character varying(64),
            lease_expires_at         timestamp without time zone,
            created_at               timestamp without time zone NOT NULL,
            updated_at               timestamp without time zone NOT NULL,
            started_at               timestamp without time zone,
            database_cleaned_at      timestamp without time zone,
            completed_at             timestamp without time zone,
            cancelled_at             timestamp without time zone,
            CONSTRAINT laboratory_cleanup_operation_pkey PRIMARY KEY (id),
            CONSTRAINT uq_laboratory_cleanup_confirmation_jti UNIQUE (confirmation_jti),
            CONSTRAINT laboratory_cleanup_operation_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT ck_laboratory_cleanup_operation_status
                CHECK (status IN ('QUEUED','RUNNING','FAILED','COMPLETED','CANCELLED')),
            CONSTRAINT ck_laboratory_cleanup_operation_phase
                CHECK (phase IN ('QUIESCE','DATABASE','OBJECTS','DONE'))
        )
    """)
    op.execute("CREATE INDEX ix_laboratory_cleanup_operation_tenant_id ON public.laboratory_cleanup_operation USING btree (tenant_id)")
    op.execute(
        "CREATE UNIQUE INDEX uq_laboratory_cleanup_open_per_tenant ON public.laboratory_cleanup_operation "
        "USING btree (tenant_id) WHERE status IN ('QUEUED','RUNNING','FAILED')"
    )
    op.execute("""
        CREATE TABLE public.laboratory_cleanup_object (
            id                uuid NOT NULL,
            operation_id      uuid NOT NULL,
            tenant_id         uuid NOT NULL,
            storage_object_id uuid NOT NULL,
            provider          character varying(50) NOT NULL,
            bucket            character varying(255) NOT NULL,
            object_key        character varying(1000) NOT NULL,
            size_bytes        bigint,
            category          character varying(32) NOT NULL,
            status            character varying(10) NOT NULL,
            attempts          integer NOT NULL DEFAULT 0,
            error_code        character varying(64),
            created_at        timestamp without time zone NOT NULL,
            deleted_at        timestamp without time zone,
            CONSTRAINT laboratory_cleanup_object_pkey PRIMARY KEY (id),
            CONSTRAINT uq_laboratory_cleanup_object_key UNIQUE (operation_id, bucket, object_key),
            CONSTRAINT laboratory_cleanup_object_operation_id_fkey FOREIGN KEY (operation_id) REFERENCES public.laboratory_cleanup_operation(id),
            CONSTRAINT laboratory_cleanup_object_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT ck_laboratory_cleanup_object_status CHECK (status IN ('PENDING','DELETED','ABSENT','KEPT','FAILED'))
        )
    """)
    op.execute("CREATE INDEX ix_laboratory_cleanup_object_operation_id ON public.laboratory_cleanup_object USING btree (operation_id)")
    op.execute("CREATE INDEX ix_laboratory_cleanup_object_tenant_status ON public.laboratory_cleanup_object USING btree (tenant_id, status)")
    op.execute("""
        CREATE TABLE public.laboratory_cleanup_retired_code (
            id           uuid NOT NULL,
            tenant_id    uuid NOT NULL,
            entity_kind  character varying(16) NOT NULL,
            code         character varying(100) NOT NULL,
            operation_id uuid NOT NULL,
            created_at   timestamp without time zone NOT NULL,
            CONSTRAINT laboratory_cleanup_retired_code_pkey PRIMARY KEY (id),
            CONSTRAINT uq_laboratory_cleanup_retired_code UNIQUE (tenant_id, entity_kind, code),
            CONSTRAINT laboratory_cleanup_retired_code_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id),
            CONSTRAINT laboratory_cleanup_retired_code_operation_id_fkey FOREIGN KEY (operation_id) REFERENCES public.laboratory_cleanup_operation(id)
        )
    """)
    op.execute("CREATE INDEX ix_laboratory_cleanup_retired_code_tenant_id ON public.laboratory_cleanup_retired_code USING btree (tenant_id)")

def downgrade() -> None:
    """Returns the database to the `v1_3_1` contract.

    The sample-type section (§2) is LOSSY: every sample comes back as `OTRO`
    (see the module docstring). Dropping the indexes loses no data. The
    extension is dropped as well: the 1.3.1 schema did not have it, and
    nothing but the 1.4 list search uses it.
    """
    # Reverse order. §4's refusal is checked before anything is dropped: refused while tombstones
    # exist, because a 1.3.1 application would show the erroneous captures again.
    bind = op.get_bind()
    removed = bind.exec_driver_sql(
        'SELECT (SELECT count(*) FROM public."order" WHERE removed_at IS NOT NULL) + '
        "(SELECT count(*) FROM public.sample WHERE removed_at IS NOT NULL) + "
        "(SELECT count(*) FROM public.report WHERE removed_at IS NOT NULL)"
    ).scalar()
    if removed:
        raise RuntimeError(
            f"Downgrade refused: {removed} removed order/sample/report record(s) would reappear in 1.3.1. "
            "Restore from a pre-upgrade snapshot instead (celuma-engineering/runbooks/migrations.md)."
        )
    # §6's refusal, also before anything is dropped: amendments are clinical history.
    amendments = bind.exec_driver_sql(f"SELECT count(*) FROM public.{AMENDMENT_TABLE}").scalar()
    if amendments:
        raise RuntimeError(
            f"Downgrade refused: {amendments} report amendment record(s) cannot be represented in 1.3.1. "
            "Restore from a pre-upgrade snapshot instead (celuma-engineering/runbooks/migrations.md)."
        )
    # §7's refusal, also before anything is dropped: a cleanup operation is evidence of an
    # irreversible action and of retired codes the 1.3.1 schema cannot protect.
    cleanups = bind.exec_driver_sql("SELECT count(*) FROM public.laboratory_cleanup_operation").scalar()
    if cleanups:
        raise RuntimeError(
            f"Downgrade refused: {cleanups} laboratory-cleanup operation(s) are evidence of an irreversible action. "
            "Restore from a pre-upgrade snapshot instead (celuma-engineering/runbooks/migrations.md)."
        )
    # §7
    op.execute("DROP TABLE public.laboratory_cleanup_retired_code")
    op.execute("DROP TABLE public.laboratory_cleanup_object")
    op.execute("DROP TABLE public.laboratory_cleanup_operation")
    op.execute("DROP TRIGGER trg_tenant_creator_write_once ON public.tenant")
    op.execute("DROP FUNCTION public.tenant_creator_write_once()")
    op.execute("ALTER TABLE public.tenant DROP CONSTRAINT fk_tenant_created_by_user_id_app_user")
    op.execute("ALTER TABLE public.tenant DROP COLUMN created_by_user_id")
    # §6
    op.execute(
        f"DELETE FROM public.notification_preference WHERE notification_type = '{REPUBLISHED_NOTIFICATION_TYPE}'"
    )
    _notification_type_checks(V1_3_NOTIFICATION_TYPES)
    op.execute("ALTER TABLE public.report_version DROP CONSTRAINT report_version_amendment_id_fkey")
    op.execute("DROP INDEX IF EXISTS public.ix_report_version_amendment_id")
    op.execute("ALTER TABLE public.report_version DROP COLUMN amendment_id")
    op.execute(f"DROP TABLE public.{AMENDMENT_TABLE}")
    # §5: the index only; the failure audit rows are history and stay.
    op.execute(f"DROP INDEX IF EXISTS public.{PASSWORD_FAILURE_INDEX}")
    # §4
    op.execute(f"""
        DELETE FROM public.role_permission WHERE permission_id IN
            (SELECT id FROM public.permission WHERE code = '{REMOVE_CAPTURE_PERMISSION}')
    """)
    op.execute(f"DELETE FROM public.permission WHERE code = '{REMOVE_CAPTURE_PERMISSION}'")
    values = ", ".join(f"'{v}'" for v in MANAGEMENT_EVENT_TYPES)
    op.execute(f"DELETE FROM public.order_event WHERE event_type IN ({values})")
    op.execute(f"DELETE FROM public.audit_log WHERE event_type IN ({values})")
    op.execute("DROP TABLE public.sample_code_correction")
    op.execute("DROP TABLE public.order_requesting_physician")
    op.execute("ALTER TABLE public.report DROP COLUMN removed_at")
    op.execute("ALTER TABLE public.sample DROP CONSTRAINT sample_removed_by_fkey")
    op.execute('ALTER TABLE public."order" DROP CONSTRAINT order_removed_by_fkey')
    for table in ('public."order"', "public.sample"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN removal_reason")
        op.execute(f"ALTER TABLE {table} DROP COLUMN removed_by")
        op.execute(f"ALTER TABLE {table} DROP COLUMN removed_at")
    op.execute('ALTER TABLE public."order" DROP COLUMN expediente')
    # §3 next: counters and ledger are lost (see §3).
    op.execute("DROP INDEX IF EXISTS public.ix_sample_tenant_sample_code")
    op.execute("DROP INDEX IF EXISTS public.ix_order_tenant_order_code")
    op.execute("DROP TABLE public.idempotency_receipt")
    op.execute(f"""
        DELETE FROM public.role_permission WHERE permission_id IN
            (SELECT id FROM public.permission WHERE code = '{MANUAL_CODE_PERMISSION}')
    """)
    op.execute(f"DELETE FROM public.permission WHERE code = '{MANUAL_CODE_PERMISSION}'")
    op.execute("DROP TABLE public.numbering_allocation")
    op.execute("DROP TABLE public.numbering_counter")
    op.execute("DROP TABLE public.numbering_config")
    # §2 next. LOSSY: see the module docstring.
    values = ", ".join(f"'{v}'" for v in RETIRED_SAMPLE_TYPE_VALUES)
    op.execute(f"CREATE TYPE public.sampletype AS ENUM ({values})")
    op.execute("ALTER TABLE public.sample ADD COLUMN type public.sampletype NOT NULL DEFAULT 'OTRO'")
    op.execute("ALTER TABLE public.sample ALTER COLUMN type DROP DEFAULT")
    op.execute("DROP INDEX IF EXISTS public.ix_sample_sample_type_id")
    op.execute("ALTER TABLE public.sample DROP CONSTRAINT IF EXISTS sample_sample_type_id_fkey")
    op.execute("ALTER TABLE public.sample DROP COLUMN sample_type_id")
    op.execute("DROP TABLE public.sample_type")
    for name, _table, _column in reversed(LIST_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS public.{name}")
    op.execute("DROP EXTENSION IF EXISTS fuzzystrmatch")
