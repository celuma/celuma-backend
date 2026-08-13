"""Notification core domain (Céluma 1.3, Phase 3, Block B).

Four tables implement the model selected in Block A
(docs/celuma-1.3/phase-3-block-a/notification-domain-model-proposal.md §2):

  Notification            shared, immutable event + frozen user-facing content
  NotificationRecipient   per-user inbox row and read state
  NotificationDelivery    external-channel delivery lifecycle (Block D/E)
  NotificationPreference  per-user, per-type channel override (Block D)

Block B creates all four tables because they are part of the approved core
model, but only `Notification`/`NotificationRecipient` are written or read by
any code path in this block: there is no delivery worker, no email send, and
no preference API until Blocks D/E.

Enum storage convention
-----------------------
Every enum below is persisted as a plain ``VARCHAR`` plus a ``CHECK``
constraint, never as a native PostgreSQL ``ENUM`` type. This is the
convention every Céluma 1.3 table already follows (`report_template_version`
/ `report_letterhead_version`, see v1_3_0's module docstring): adding or
retiring a value is a constraint change instead of ``ALTER TYPE ... ADD
VALUE``, which cannot run inside a transaction on older PostgreSQL and is
awkward to revert. The legacy native `public.eventtype` enum from v1_0_0 is
deliberately not the model followed here — Phase 3 explicitly wants channel
values (`PUSH`, `SMS`, ...) to stay *absent* until they are real, which a
CHECK constraint makes cheap to change later.

`resource_type` is the one deliberately unconstrained column: it is stored as
free-form VARCHAR(50) exactly like `audit_log.entity_type`, and validated
against `NotificationResourceType` at the service/API boundary instead. The
polymorphic `resource_id` therefore carries no foreign key, again matching
`audit_log.entity_id`.

Mutability
----------
`Notification` is immutable after insert by application contract — nothing in
this codebase updates `title`/`body`/`notification_metadata`, and no endpoint
exposes a generic update path. `NotificationRecipient.status`/`read_at` are
the only mutable notification fields in Block B, and they transition exactly
once (UNREAD -> READ).

Delete policy
-------------
No foreign key carries an ON DELETE clause, so PostgreSQL's default NO
ACTION/RESTRICT applies: deleting a user or a tenant that still owns
notification history is refused by the database rather than silently erasing
the audit trail. `resource_id` has no FK at all, so deleting a referenced
report/order/sample can never cascade into notifications.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlmodel import Field, JSON

from app.services.locale import DEFAULT_LOCALE

from .base import BaseModel, TenantMixin, TimestampMixin


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class NotificationType(str, Enum):
    """The six MUST_HAVE_1_3 clinical events confirmed against real
    transitions in
    docs/celuma-1.3/phase-3-block-a/notification-event-inventory.md, plus the
    four administrative usage-threshold events added by Céluma 1.3, Phase 4,
    Block G.

    No speculative future type is declared here. Block B wires none of the
    clinical types to a real transition — that is Block F.

    Usage-threshold types (Phase 4, Block G)
    -----------------------------------------
    Exactly four, and deliberately **not** one per threshold percentage.
    "80%" and "100%" are policy numbers owned by
    `app/services/usage_thresholds.py`; a `STORAGE_USAGE_80` type would freeze
    a policy value into the enum, into every stored row, into the preference
    table and into the frontend's label maps, so changing the policy later
    would be a migration rather than a constant. The semantic state
    (APPROACHING / REACHED) is what a recipient is actually being told, and
    that is what these four names carry.

    They differ from the clinical six in three ways, all documented in
    docs/celuma-1.3/phase-4-block-g/usage-threshold-notification-contract.md:
    they are tenant-level rather than resource-level, their recipients are
    resolved from a permission rather than from a workflow relationship, and
    the actor whose action caused the crossing is **not** excluded.
    """
    REPORT_SUBMITTED = "REPORT_SUBMITTED"
    REPORT_PDF_READY = "REPORT_PDF_READY"
    REPORT_PUBLISHED = "REPORT_PUBLISHED"
    REPORT_RETRACTED = "REPORT_RETRACTED"
    ASSIGNMENT_ADDED = "ASSIGNMENT_ADDED"
    SAMPLE_STATUS_CHANGED = "SAMPLE_STATUS_CHANGED"
    STORAGE_USAGE_APPROACHING = "STORAGE_USAGE_APPROACHING"
    STORAGE_LIMIT_REACHED = "STORAGE_LIMIT_REACHED"
    USER_LIMIT_APPROACHING = "USER_LIMIT_APPROACHING"
    USER_LIMIT_REACHED = "USER_LIMIT_REACHED"


class NotificationSeverity(str, Enum):
    """Drives icon/colour in the Notification Center, not delivery priority.

    Only INFO is produced by anything in Block B; the other two are modeled
    completely so the constraint does not need to change when a future event
    needs them. Severity is deliberately NOT exposed as a list filter until a
    second value is in active use (Block A API proposal §2).
    """
    INFO = "INFO"
    WARNING = "WARNING"
    ACTION_REQUIRED = "ACTION_REQUIRED"


class NotificationRecipientStatus(str, Enum):
    """Per-user inbox state.

    DISMISSED is reserved for a future "hide from inbox without marking read"
    affordance. No Block B endpoint can produce it — there is no dismiss
    endpoint — it exists so adding one later is not a constraint change.
    """
    UNREAD = "UNREAD"
    READ = "READ"
    DISMISSED = "DISMISSED"


class NotificationChannel(str, Enum):
    """External delivery channels.

    EMAIL is the only channel in Phase 3 scope. PUSH/SMS/WHATSAPP are
    intentionally absent: an unused value in a shipped constraint invites
    rows that no code knows how to process, and adding one later costs a
    one-line CHECK change under this module's VARCHAR+CHECK convention.
    """
    EMAIL = "EMAIL"


class NotificationDeliveryStatus(str, Enum):
    """Delivery lifecycle. Modeled in Block B, driven in Blocks D/E.

    SENDING is the claim state described in
    notification-idempotency-strategy.md §5 — it exists so a worker crash
    mid-send is distinguishable from "never attempted".
    """
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class NotificationResourceType(str, Enum):
    """Validated at the service/API boundary only — the column itself is
    free-form VARCHAR(50), matching `audit_log.entity_type`.

    `TENANT` (Céluma 1.3, Phase 4, Block G) is the odd one out and worth a
    sentence: the other three name a clinical record a notification is
    *about*, while a usage-threshold event is about the tenant itself. Its
    `resource_id` is therefore the tenant id — which the recipient already
    belongs to, so it discloses nothing — and its deep link is the fixed
    `/config/usage` route rather than one built from the id. Modelling it as
    a resource type rather than inventing a nullable-resource notification
    keeps `resource_type`/`resource_id` NOT NULL for every row, and keeps the
    frontend's single `resource_type -> route` switch the only place a
    notification becomes a destination.
    """
    REPORT = "report"
    ORDER = "order"
    SAMPLE = "sample"
    TENANT = "tenant"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class Notification(BaseModel, TimestampMixin, TenantMixin, table=True):
    """One row per domain-event occurrence, regardless of recipient count.

    Immutable after insert. `title`/`body` hold the final Spanish text frozen
    at creation time; `notification_metadata` retains the `template_key` and
    the safe `template_params` that produced them, so a future localization
    pass can re-render without rewriting history (content policy §8, the
    "hybrid" option).
    """
    __tablename__ = "notification"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    type: NotificationType = Field(
        sa_column=Column("type", String(50), nullable=False)
    )
    severity: NotificationSeverity = Field(
        default=NotificationSeverity.INFO,
        sa_column=Column(
            "severity", String(20), nullable=False, server_default="INFO"
        ),
    )
    title: str = Field(max_length=255)
    body: Optional[str] = Field(max_length=1000, default=None)
    # Polymorphic pair, no FK — same shape as audit_log.entity_type/entity_id.
    resource_type: str = Field(max_length=50)
    resource_id: UUID
    notification_metadata: Optional[Dict[str, Any]] = Field(
        default=None, sa_type=JSON
    )
    idempotency_key: str = Field(max_length=255)
    #: The locale `title`/`body` were rendered in (Céluma 1.3, Phase 3, Block
    #: F). `es-MX` for every row Céluma 1.3 produces.
    #:
    #: Not redundant with the frozen text, which is what the "no schema change"
    #: option assumed. The frozen text makes *in-app display* reproducible; it
    #: does not say which locale produced it, and two consumers need that:
    #: the delivery worker, which renders an independent email from
    #: `template_key`/`template_params` and must render it in the same locale
    #: as the notification rather than in whatever the default is at delivery
    #: time; and audit, which cannot otherwise answer "what did this user
    #: actually read" once a second locale exists. Recording it costs one
    #: additive column now; reconstructing it later is impossible.
    #:
    #: NOT NULL with a server default so the backfill is the default — see
    #: Phase 3 Block F's migration notes for why `es-MX` is provable rather
    #: than assumed for every pre-existing row. (The column ships inside the
    #: `v1_3_0` release migration; the revision that originally added it was
    #: folded in by the Phase 3 closure squash.)
    locale: str = Field(
        default=DEFAULT_LOCALE,
        sa_column=Column(
            "locale", String(35), nullable=False, server_default=DEFAULT_LOCALE
        ),
    )
    created_by: Optional[UUID] = Field(foreign_key="app_user.id", default=None)


class NotificationRecipient(BaseModel, TimestampMixin, TenantMixin, table=True):
    """Per-user inbox row for a shared Notification.

    `created_at` is deliberately denormalized from the parent Notification
    (the service copies the parent's exact timestamp) so the inbox list query
    — the hot path — can filter, sort and paginate without joining
    `notification` for its sort key.

    There is no `delivered_at` column. Block A proposed one, but for an
    in-app notification "delivered" is exactly "the row exists", which
    `created_at` already records; a second column carrying the same fact
    would be redundant state that could drift. Channel-specific delivery
    timing lives on `NotificationDelivery`, where it is genuinely distinct.
    See phase-3-block-b-architecture-decision.md.
    """
    __tablename__ = "notification_recipient"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    notification_id: UUID = Field(foreign_key="notification.id")
    tenant_id: UUID = Field(foreign_key="tenant.id")
    user_id: UUID = Field(foreign_key="app_user.id")
    status: NotificationRecipientStatus = Field(
        default=NotificationRecipientStatus.UNREAD,
        sa_column=Column(
            "status", String(20), nullable=False, server_default="UNREAD"
        ),
    )
    read_at: Optional[datetime] = Field(default=None)


class NotificationDelivery(BaseModel, TenantMixin, table=True):
    """External-channel delivery lifecycle. Created by Block D, driven by
    Block E — nothing in Block B ever inserts one of these rows outside a
    synthetic test.

    `recipient_address` is NOT NULL: EMAIL is the only channel in Phase 3 and
    an email delivery without an address is not a meaningful row. Keeping it
    non-null is also what makes UNIQUE(notification_id, channel,
    recipient_address) a real guarantee — under a nullable column, NULLs
    compare distinct in PostgreSQL and duplicate rows would slip through the
    very constraint meant to prevent double sends.
    """
    __tablename__ = "notification_delivery"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    notification_id: UUID = Field(foreign_key="notification.id")
    tenant_id: UUID = Field(foreign_key="tenant.id")
    # Nullable: a physician/external recipient may have no AppUser account.
    recipient_user_id: Optional[UUID] = Field(
        foreign_key="app_user.id", default=None
    )
    recipient_address: str = Field(max_length=320)
    channel: NotificationChannel = Field(
        sa_column=Column("channel", String(20), nullable=False)
    )
    status: NotificationDeliveryStatus = Field(
        default=NotificationDeliveryStatus.PENDING,
        sa_column=Column(
            "status", String(20), nullable=False, server_default="PENDING"
        ),
    )
    attempts: int = Field(
        default=0,
        sa_column=Column("attempts", Integer, nullable=False, server_default="0"),
    )
    last_attempt_at: Optional[datetime] = Field(default=None)
    next_attempt_at: Optional[datetime] = Field(default=None)
    provider_message_id: Optional[str] = Field(max_length=255, default=None)
    # Sanitized code only — never a raw provider exception (content policy §7).
    error_code: Optional[str] = Field(max_length=255, default=None)
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", DateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("updated_at", DateTime, nullable=False),
    )


class NotificationPreference(BaseModel, TenantMixin, table=True):
    """Per-user, per-type channel override. Block D owns its API.

    Absence of a row means "use the default" (both channels enabled). No row
    is ever seeded for a user or a type — the table stores overrides only, so
    an empty table is the correct steady state until a user changes
    something.
    """
    __tablename__ = "notification_preference"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    user_id: UUID = Field(foreign_key="app_user.id")
    notification_type: NotificationType = Field(
        sa_column=Column("notification_type", String(50), nullable=False)
    )
    in_app_enabled: bool = Field(
        default=True,
        sa_column=Column(
            "in_app_enabled", Boolean, nullable=False, server_default="true"
        ),
    )
    email_enabled: bool = Field(
        default=True,
        sa_column=Column(
            "email_enabled", Boolean, nullable=False, server_default="true"
        ),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("updated_at", DateTime, nullable=False),
    )
