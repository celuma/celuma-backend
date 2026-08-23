"""UsageService (Céluma 1.3, Phase 4, Block B).

The read-only core of the usage domain. Every method here is a pure read —
nothing in this module writes a `TenantUsage`, `TenantLimits`, or
`TenantUsageReconciliation` row, and nothing here scans S3 or backfills
`storage_object.tenant_id`. Write paths (incremental counters, backfill,
the reconciliation engine) are Block C/D's.

Tenant isolation
-----------------
Every method takes an explicit `tenant_id` and filters by it. There is no
method that returns cross-tenant data, and none takes an untrusted
"which tenant" parameter beyond the id the caller already owns —
consistent with every other tenant-scoped service in this codebase.

Missing-row semantics (read this before calling `get_usage`/`get_limits`)
----------------------------------------------------------------------------
`get_usage()` returns `None` when a tenant has no `TenantUsage` row. That
means "usage tracking not initialized for this tenant" — NOT "0 bytes
used". Block C owns initialization; nothing in this service (or any future
caller) may treat `None` as zero.

`get_limits()` returns `None` when a tenant has no `TenantLimits` row. That
case is unambiguous, unlike usage: it means "no limits configured" (both
storage and user limits unlimited) — there is nothing to initialize.

See docs/celuma-1.3/phase-4-block-b/usage-service-contract.md for the full
read-only contract and docs/celuma-1.3/phase-4-block-c/incremental-usage-
accounting-contract.md for the write API added in Block C below.

Block C write API — the load-bearing invariant
------------------------------------------------
`adjust_storage()` (and its `increment_storage`/`decrement_storage`
wrappers) NEVER creates a `TenantUsage` row. It is a pure atomic UPDATE; if
no row exists for the tenant, it raises `UsageNotInitializedError` rather
than lazily inserting one. This is deliberate and non-negotiable — see
tenant-usage-initialization-contract.md: a tenant with historical storage
must never end up with `TenantUsage` seeded from only its first post-Block-
C write. The only way a `TenantUsage` row is created is `initialize_usage()`,
called either by the Block C migration (historical tenants, seeded with the
real computed baseline) or by tenant creation (new tenants, seeded at
zero) — never as a side effect of a storage mutation.

`record_storage_delta()` is the failure-contained wrapper write-flow call
sites should use: it swallows `UsageNotInitializedError` (already logged
inside `adjust_storage`) so a broken/missing usage row never blocks an
otherwise-valid clinical storage operation. See incremental-usage-
accounting-contract.md "failure containment" for the tradeoff this encodes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import func, update as sa_update
from sqlmodel import Session, select

from app.core.rbac import ROLE_PHYSICIAN
from app.models.role import Role
from app.models.tenant_limits import TenantLimits
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_reconciliation import TenantUsageReconciliation
from app.models.user import AppUser
from app.models.user_role import UserRoleLink

logger = logging.getLogger(__name__)


class UsageNotInitializedError(RuntimeError):
    """Raised by `adjust_storage`/`increment_storage`/`decrement_storage`
    when no `TenantUsage` row exists for the tenant. Never auto-repaired by
    those methods — see the module docstring. Callers on write-flow call
    sites should generally use `record_storage_delta()` instead, which
    catches this and contains the failure."""

    def __init__(self, tenant_id: UUID):
        self.tenant_id = tenant_id
        super().__init__(f"TenantUsage is not initialized for tenant {tenant_id}")


@dataclass(frozen=True)
class TenantUserMetrics:
    """Live-computed user counts for one tenant. Never persisted — see
    docs/celuma-1.3/phase-4-block-b/tenant-user-metrics-contract.md for the
    exact SQL semantics behind each field.
    """

    #: Every AppUser row belonging to the tenant, any status.
    registered_users: int
    #: Active, holding at least one non-physician role. The billable/
    #: licensed-seat metric. A roleless active user does NOT count (the
    #: join requires at least one role row); a multi-role user (e.g.
    #: physician + reviewer) counts once.
    active_internal_users: int
    #: Active, holding the physician role AND no non-physician role.
    #: Disjoint from active_internal_users by construction — a multi-role
    #: physician+reviewer user is counted in active_internal_users only.
    active_physician_portal_users: int


class UsageService:
    """The usage domain's service surface. Block B's four read-only methods
    (`get_usage`, `get_limits`, `get_latest_reconciliation`,
    `get_user_metrics`) plus Block C's storage-counter write API
    (`initialize_usage`, `adjust_storage`/`increment_storage`/
    `decrement_storage`, `record_storage_delta`). Reconciliation itself
    (Block D) is not implemented here.
    """

    @staticmethod
    def get_usage(session: Session, tenant_id: UUID) -> Optional[TenantUsage]:
        """The tenant's current billable storage usage, or `None` if usage
        tracking has not been initialized for this tenant yet (Block C's
        job). Never fabricates a zero-valued row."""
        return session.get(TenantUsage, tenant_id)

    @staticmethod
    def get_limits(session: Session, tenant_id: UUID) -> Optional[TenantLimits]:
        """The tenant's configured limits, or `None` if none are configured
        (both storage and user limits are then unlimited)."""
        return session.get(TenantLimits, tenant_id)

    @staticmethod
    def get_latest_reconciliation(
        session: Session, tenant_id: UUID
    ) -> Optional[TenantUsageReconciliation]:
        """The tenant's most recently *started* reconciliation run
        (RUNNING, SUCCEEDED, or FAILED), or `None` if reconciliation has
        never run for this tenant. Ordered by `started_at`, served by
        `ix_tenant_usage_reconciliation_tenant_started_at`.
        """
        statement = (
            select(TenantUsageReconciliation)
            .where(TenantUsageReconciliation.tenant_id == tenant_id)
            .order_by(TenantUsageReconciliation.started_at.desc())
            .limit(1)
        )
        return session.exec(statement).first()

    @staticmethod
    def get_user_metrics(session: Session, tenant_id: UUID) -> TenantUserMetrics:
        """Live-computed user counts. Not read from any counter table —
        `app_user`/`user_role`/`role` are always authoritative, with no
        S3-equivalent drift risk, so there is nothing here for Block C/D's
        reconciliation machinery to maintain.
        """
        registered_users = session.exec(
            select(func.count(AppUser.id)).where(AppUser.tenant_id == tenant_id)
        ).one()

        active_internal_users = session.exec(
            select(func.count(func.distinct(AppUser.id)))
            .select_from(AppUser)
            .join(UserRoleLink, UserRoleLink.user_id == AppUser.id)
            .join(Role, Role.id == UserRoleLink.role_id)
            .where(
                AppUser.tenant_id == tenant_id,
                AppUser.is_active == True,  # noqa: E712
                Role.code != ROLE_PHYSICIAN,
            )
        ).one()

        # "Has a physician role AND no non-physician role" — a correlated
        # NOT EXISTS keeps a multi-role physician+reviewer user out of this
        # count (they are already in active_internal_users above).
        has_non_physician_role = (
            select(UserRoleLink.user_id)
            .join(Role, Role.id == UserRoleLink.role_id)
            .where(
                UserRoleLink.user_id == AppUser.id,
                Role.code != ROLE_PHYSICIAN,
            )
        )
        active_physician_portal_users = session.exec(
            select(func.count(func.distinct(AppUser.id)))
            .select_from(AppUser)
            .join(UserRoleLink, UserRoleLink.user_id == AppUser.id)
            .join(Role, Role.id == UserRoleLink.role_id)
            .where(
                AppUser.tenant_id == tenant_id,
                AppUser.is_active == True,  # noqa: E712
                Role.code == ROLE_PHYSICIAN,
                ~has_non_physician_role.exists(),
            )
        ).one()

        return TenantUserMetrics(
            registered_users=registered_users,
            active_internal_users=active_internal_users,
            active_physician_portal_users=active_physician_portal_users,
        )

    # -- Block C: write API -------------------------------------------------

    @staticmethod
    def initialize_usage(
        session: Session,
        tenant_id: UUID,
        *,
        billable_storage_bytes: int = 0,
        source: str = "block_c_initialization",
    ) -> TenantUsage:
        """Create the tenant's `TenantUsage` row with an explicit baseline —
        the ONLY method in this service allowed to insert one. Idempotent by
        construction: if a row already exists, it is left untouched and
        returned as-is (never overwritten, never recomputed) — running this
        twice must never change a tenant's usage. Does not commit; the
        caller controls the transaction boundary (e.g. the same transaction
        as a new tenant's own creation, or a batched migration commit).
        """
        existing = session.get(TenantUsage, tenant_id)
        if existing is not None:
            logger.info(
                "usage.initialization.skipped",
                extra={
                    "event": "usage.initialization.skipped",
                    "tenant_id": str(tenant_id),
                    "source": source,
                    "reason": "already_initialized",
                },
            )
            return existing

        row = TenantUsage(
            tenant_id=tenant_id,
            billable_storage_bytes=billable_storage_bytes,
            last_updated=datetime.utcnow(),
        )
        session.add(row)
        logger.info(
            "usage.initialization.completed",
            extra={
                "event": "usage.initialization.completed",
                "tenant_id": str(tenant_id),
                "source": source,
                "billable_storage_bytes": billable_storage_bytes,
            },
        )
        return row

    @staticmethod
    def adjust_storage(
        session: Session,
        tenant_id: UUID,
        delta_bytes: int,
        *,
        source: str,
        resource_type: Optional[str] = None,
    ) -> int:
        """Atomically mutate `billable_storage_bytes` by `delta_bytes`
        (positive increments, negative decrements) in one `UPDATE`, floored
        at zero (`GREATEST(current + delta, 0)`) so a decrement can never
        drive the counter negative — the schema's own CHECK constraint is a
        backstop, not the primary defense. Never creates a row: raises
        `UsageNotInitializedError` if none exists. Returns the new total.

        Single-statement atomicity gives correct behavior under concurrent
        callers for the same tenant without an explicit application-level
        lock — two concurrent adjustments serialize on the row itself.
        """
        now = datetime.utcnow()
        result = session.execute(
            sa_update(TenantUsage)
            .where(TenantUsage.tenant_id == tenant_id)
            .values(
                billable_storage_bytes=func.greatest(
                    TenantUsage.billable_storage_bytes + delta_bytes, 0
                ),
                last_updated=now,
            )
            .returning(TenantUsage.billable_storage_bytes)
        )
        row = result.first()
        if row is None:
            logger.error(
                "usage.adjustment.not_initialized",
                extra={
                    "event": "usage.adjustment.not_initialized",
                    "tenant_id": str(tenant_id),
                    "delta_bytes": delta_bytes,
                    "source": source,
                    "resource_type": resource_type,
                },
            )
            raise UsageNotInitializedError(tenant_id)

        new_total = row[0]
        logger.info(
            "usage.adjustment.applied",
            extra={
                "event": "usage.adjustment.applied",
                "tenant_id": str(tenant_id),
                "delta_bytes": delta_bytes,
                "new_total_bytes": new_total,
                "source": source,
                "resource_type": resource_type,
            },
        )
        return new_total

    @staticmethod
    def increment_storage(
        session: Session,
        tenant_id: UUID,
        size_bytes: int,
        *,
        source: str,
        resource_type: Optional[str] = None,
    ) -> int:
        """Convenience wrapper: `adjust_storage` with a non-negative delta."""
        return UsageService.adjust_storage(
            session, tenant_id, abs(size_bytes), source=source, resource_type=resource_type
        )

    @staticmethod
    def decrement_storage(
        session: Session,
        tenant_id: UUID,
        size_bytes: int,
        *,
        source: str,
        resource_type: Optional[str] = None,
    ) -> int:
        """Convenience wrapper: `adjust_storage` with a non-positive delta."""
        return UsageService.adjust_storage(
            session, tenant_id, -abs(size_bytes), source=source, resource_type=resource_type
        )

    @staticmethod
    def record_storage_delta(
        session: Session,
        tenant_id: UUID,
        delta_bytes: int,
        *,
        source: str,
        resource_type: Optional[str] = None,
    ) -> None:
        """Failure-contained counter mutation for write-flow call sites
        (sample image upload/delete, PDF/JSON writes, logo/signature
        writes). Never raises: a missing or broken `TenantUsage` row must
        not block an otherwise-valid storage operation (see incremental-
        usage-accounting-contract.md "failure containment"). The failure is
        already logged inside `adjust_storage` before this swallows it, so
        it stays observable without becoming a hard error on the clinical
        request path. A future reconciliation pass (Block D) is what
        repairs the resulting drift — this method does not attempt to.
        """
        if delta_bytes == 0:
            return
        try:
            UsageService.adjust_storage(
                session, tenant_id, delta_bytes, source=source, resource_type=resource_type
            )
        except UsageNotInitializedError:
            pass
