"""Operational removal ("tombstones") of erroneous captures (Céluma 1.4, issue #11).

A removed order, sample or draft report keeps its row, identifiers, ledger entry and audit
evidence, but every ordinary ORM SELECT stops seeing it. The filter is global on purpose: a
hand-added `removed_at IS NULL` per query is exactly how a removed record leaks into one list, one
count or one portal that nobody remembered. It covers lists, counts, filter options, the worklist,
the dashboard, portals, lazy relationship loads and `Session.get`.

Two things deliberately bypass it:

- raw SQL (`text(...)`): numbering availability checks and idempotency receipt verification must keep
  seeing removed rows, so a removed order's code is never reissued and a replay cannot resurrect it;
- statements carrying `execution_options(include_removed=True)`: the management endpoints that must
  inspect or reconcile a removed row.

Contract: `celuma-engineering/specs/laboratory/order-sample-management.md` (removal semantics).
"""
from __future__ import annotations

from sqlalchemy import event, exists, select
from sqlalchemy.orm import Session as _OrmSession
from sqlalchemy.orm import with_loader_criteria

from app.models.billing import Invoice
from app.models.laboratory import Order, Sample
from app.models.report import Report

INCLUDE_REMOVED = "include_removed"

_TOMBSTONED = (Order, Sample, Report)

# The invoice created with an order has no tombstone of its own: it is retained untouched and hidden
# from billing together with its order. Core table on purpose, so this subquery is not itself filtered.
_ORDER_TABLE = Order.__table__


@event.listens_for(_OrmSession, "do_orm_execute")
def _hide_removed(state) -> None:
    if not state.is_select or state.execution_options.get(INCLUDE_REMOVED):
        return
    state.statement = state.statement.options(
        *(with_loader_criteria(model, lambda cls: cls.removed_at.is_(None), include_aliases=True) for model in _TOMBSTONED),
        with_loader_criteria(
            Invoice,
            lambda cls: ~exists(select(1).where(_ORDER_TABLE.c.id == cls.order_id, _ORDER_TABLE.c.removed_at.is_not(None))),
            include_aliases=True,
        ),
    )
