"""TenantUsageThresholdState — durable usage-threshold state (Céluma 1.3,
Phase 4, Block G).

One row per `(tenant_id, resource)`. This table is the *only* reason Block G
can notify at all: without it, "the tenant is above 80%" is a condition that
is true on every subsequent usage-changing request, and evaluating it
statelessly would send one notification per sample upload forever. What is
notifiable is not the condition — it is the **transition into** the
condition, and a transition is only observable against a remembered previous
state.

Why a table and not a column on `tenant_usage`
-----------------------------------------------
Three reasons, in decreasing order of weight:

1. `tenant_usage` exists for exactly one tenant-wide storage counter and
   `tenant_limits` for exactly one set of ceilings. Threshold state is
   per-*resource* (`STORAGE`, `USERS`) — two rows for one tenant — so it does
   not fit either table's grain without a pair of parallel column families
   that would have to be kept in step by hand.
2. The user resource has no counter table at all: `active_internal_users` is
   computed live on every read (Block B's `tenant-user-metrics-contract.md`,
   deliberately, because user counts have no S3-equivalent drift risk). There
   is therefore no existing row to hang user threshold state off.
3. `tenant_usage.billable_storage_bytes` is mutated by a single-statement
   atomic `UPDATE` on the hot clinical write path (Block C). Adding a state
   machine's columns to that row would put threshold bookkeeping inside the
   statement whose whole design property is that it is one statement.

Enum storage convention
-----------------------
`resource` and `state` are plain `VARCHAR` plus a `CHECK` constraint, never a
native PostgreSQL `ENUM` — the same convention `notification.py` documents and
every Céluma 1.3 table follows. Adding a resource (a future `REPORTS` quota,
say) or a state is then a constraint change rather than `ALTER TYPE ... ADD
VALUE`.

What is deliberately NOT stored
--------------------------------
No object key, no bucket, no user name, no email, no patient/report/sample
reference, and no formatted percentage string. The row carries a state, the
two integers the state was derived from, and timestamps. `last_ratio` is
absent on purpose (master spec §34's "prefer minimal schema"): it is
`last_value / last_limit` and a persisted copy is a value that can disagree
with the two columns it came from.

Delete policy
-------------
The tenant FK carries no `ON DELETE` clause, matching every other table in
this domain: deleting a tenant that still has threshold state is refused by
the database rather than silently dropping the row.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Column, DateTime, Integer, String
from sqlmodel import Field

from .base import BaseModel


class UsageResource(str, Enum):
    """The two metered resources Céluma 1.3 has limits for.

    Exactly the two `TenantLimits` columns, and nothing speculative: a
    resource declared here with no limit column behind it would be a state
    row nothing could ever evaluate.
    """

    STORAGE = "STORAGE"
    USERS = "USERS"


class UsageThresholdState(str, Enum):
    """Where a tenant/resource pair currently sits relative to its limit.

    `UNMONITORED` is not a fourth degree of "how full" — it means the
    question is not answerable, for one of exactly two reasons: no limit is
    configured (unlimited), or the resource's usage is not initialized. It is
    also the state a freshly created row carries before its first real
    evaluation, so "never evaluated" and "not evaluable" are the same value.
    That is deliberate: both mean *no upward crossing has ever been observed*,
    which is what makes first-evaluation semantics (master spec §9) fall out
    of the ordinary transition rule instead of needing a special case.
    """

    UNMONITORED = "UNMONITORED"
    NORMAL = "NORMAL"
    APPROACHING = "APPROACHING"
    REACHED = "REACHED"


#: Ordering used to decide whether a transition is *upward* — the only
#: direction that notifies. `UNMONITORED` ranks below `NORMAL` so that
#: restoring a limit under existing usage (or evaluating a tenant for the
#: first time) is an upward move into `APPROACHING`/`REACHED` and notifies
#: once, rather than being mistaken for a same-state no-op.
USAGE_THRESHOLD_STATE_RANK = {
    UsageThresholdState.UNMONITORED: -1,
    UsageThresholdState.NORMAL: 0,
    UsageThresholdState.APPROACHING: 1,
    UsageThresholdState.REACHED: 2,
}


class TenantUsageThresholdState(BaseModel, table=True):
    """One tenant's remembered threshold state for one resource."""

    __tablename__ = "tenant_usage_threshold_state"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    resource: UsageResource = Field(
        sa_column=Column("resource", String(20), nullable=False)
    )
    state: UsageThresholdState = Field(
        default=UsageThresholdState.UNMONITORED,
        sa_column=Column(
            "state", String(20), nullable=False, server_default="UNMONITORED"
        ),
    )
    #: The numerator the current `state` was derived from — billable bytes for
    #: `STORAGE`, active internal users for `USERS`. `NULL` while the state is
    #: `UNMONITORED`, and never coerced to `0`: Block B/E's rule that a null
    #: usage is "unknown", not "zero", holds here too.
    last_value: Optional[int] = Field(
        default=None, sa_column=Column("last_value", BigInteger, nullable=True)
    )
    #: The denominator the current `state` was derived from. `NULL` exactly
    #: when the resource was unlimited at evaluation time.
    last_limit: Optional[int] = Field(
        default=None, sa_column=Column("last_limit", BigInteger, nullable=True)
    )
    #: How many *notifying* (upward) transitions this row has recorded. It is
    #: the occurrence marker fed into the notification idempotency key, which
    #: is what makes a genuine re-crossing (81 -> 70 -> 82) a new notification
    #: while a repeated evaluation inside one state is not. A wall-clock
    #: timestamp would have served, but a counter cannot collide, cannot move
    #: backwards, and reads as an audit fact on its own.
    transition_count: int = Field(
        default=0,
        sa_column=Column(
            "transition_count", Integer, nullable=False, server_default="0"
        ),
    )
    #: When `state` last *changed*, in any direction. Untouched by an
    #: evaluation that finds the same state, so it stays a meaningful history
    #: point rather than a second `updated_at`.
    last_transition_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("last_transition_at", DateTime, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", DateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("updated_at", DateTime, nullable=False),
    )
