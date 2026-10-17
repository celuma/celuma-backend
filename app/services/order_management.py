"""Requesting physicians and expediente of an order (Céluma 1.4, issue #16).

Contract: `celuma-engineering/specs/laboratory/order-sample-management.md`.

An order has a duplicate-free, same-tenant SET of external requesting physicians (no primary, no
hierarchy; `position` is only the display order the user chose) and an optional textual
`expediente`. Physicians only, expediente only, or both are valid. Referencing a physician grants
that physician nothing and sends nothing.

Compatibility. `order.requesting_physician_id` and `order.requested_by` are the pre-1.4 single
physician and free-text fallback. The join table is authoritative; the legacy columns are *mirrored*,
never promoted: `requesting_physician_id` carries a value only when exactly one physician is listed
(so 1.3.x readers keep working) and is NULL otherwise, because with several physicians choosing one
would invent a primary. `requested_by` is regenerated from the names when it was derived from the
previous names (or empty) and otherwise kept as typed.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models.laboratory import Order
from app.models.order_management import OrderRequestingPhysician
from app.models.requesting_physician import RequestingPhysician

#: An engineering validation bound (not an owner decision): bounds one request's size and the report line.
MAX_REQUESTING_PHYSICIANS = 10
MAX_EXPEDIENTE_LENGTH = 100
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]")


class ManagementError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code, {"code": code, "message": message})


def normalize_expediente(value: Optional[str]) -> Optional[str]:
    """Trimmed text; blank means none. Kept verbatim otherwise: leading zeroes, dashes and letters
    survive (an expediente is a reference, not a number)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expediente must be text")
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_EXPEDIENTE_LENGTH:
        raise ValueError(f"expediente cannot exceed {MAX_EXPEDIENTE_LENGTH} characters")
    if _CONTROL.search(value):
        raise ValueError("expediente cannot contain control characters or line breaks")
    return value


def normalize_physician_ids(values: Optional[Sequence[str]]) -> Optional[List[str]]:
    """Shape check of a submitted list: bounded, no blanks, no duplicates (a duplicate is refused,
    never silently merged: the user should see what they sent)."""
    if values is None:
        return None
    ids = [str(v).strip() for v in values]
    if any(not v for v in ids):
        raise ValueError("requesting physician ids cannot be blank")
    if len(ids) > MAX_REQUESTING_PHYSICIANS:
        raise ValueError(f"an order can list at most {MAX_REQUESTING_PHYSICIANS} requesting physicians")
    if len(set(ids)) != len(ids):
        raise ValueError("requesting physicians cannot be repeated")
    return ids


def resolve_requested_ids(physician_ids: Optional[Sequence[str]], legacy_id: Optional[str]) -> List[str]:
    """The submitted physician set. `requesting_physician_ids` is authoritative; the pre-1.4
    `requesting_physician_id` is accepted as a one-element alias and must not contradict it."""
    ids = list(physician_ids) if physician_ids is not None else []
    legacy = legacy_id.strip() if isinstance(legacy_id, str) and legacy_id.strip() else None
    if legacy:
        if physician_ids is None:
            return [legacy]
        if legacy not in ids:
            raise ManagementError(
                422, "requesting_physician_conflict",
                "requesting_physician_id (deprecated) must be one of requesting_physician_ids; send only the list",
            )
    return ids


def load_physicians(
    session: Session, tenant_id, ids: Sequence[str], keep_inactive: Iterable = (),
) -> List[RequestingPhysician]:
    """Validate and load the physicians in the submitted order: each must exist in THIS tenant and be
    active (same 404 / 403 / 400 answers the single-physician field always gave)."""
    physicians: List[RequestingPhysician] = []
    for raw in ids:
        try:
            pid = UUID(str(raw))
        except ValueError:
            raise ManagementError(422, "requesting_physician_invalid", f"Invalid requesting physician id: {raw}") from None
        physician = session.get(RequestingPhysician, pid)
        if physician is None:
            raise HTTPException(404, "Requesting physician not found")
        if str(physician.tenant_id) != str(tenant_id):
            raise HTTPException(403, "Requesting physician does not belong to this tenant")
        # A physician already on the order may stay after being deactivated (history); a NEW one must be active.
        if not physician.is_active and physician.id not in set(keep_inactive):
            raise HTTPException(400, "Requesting physician is inactive")
        physicians.append(physician)
    return physicians


def physician_name(physician: RequestingPhysician) -> str:
    return physician.full_name or f"{physician.first_name} {physician.last_name}".strip()


#: `order.requested_by` is a legacy varchar(255): the derived text is a BOUNDED SUMMARY for old readers only.
LEGACY_REQUESTED_BY_LIMIT = 255


def derive_requested_by(physicians: Iterable[RequestingPhysician]) -> Optional[str]:
    """Legacy one-line summary of the physician set (never the authoritative data).

    Whole names joined with "; " while they fit in the legacy column; when they do not, the names that fit
    are followed by "; +N más" so a 1.3.x reader can tell the list is longer. A single name longer than the
    column is cut with an ellipsis. The authoritative collection (`order_requesting_physician`) and the
    report metadata always carry every full name; nothing here is a primary physician.
    """
    names = [physician_name(p) for p in physicians]
    if not names:
        return None
    limit = LEGACY_REQUESTED_BY_LIMIT
    joined = "; ".join(names)
    if len(joined) <= limit:
        return joined
    kept: List[str] = []
    for index, name in enumerate(names):
        candidate = "; ".join(kept + [name])
        suffix = f"; +{len(names) - index - 1} más"
        if len(candidate) + (len(suffix) if index < len(names) - 1 else 0) > limit:
            break
        kept.append(name)
    if not kept:
        return names[0][: limit - 1] + "…"
    return "; ".join(kept) + f"; +{len(names) - len(kept)} más"


def order_physicians(session: Session, order: Order) -> List[RequestingPhysician]:
    """The order's requesting physicians in display order. Falls back to the legacy single column
    when the join table has nothing (defensive: the migration backfills it)."""
    rows = session.exec(
        select(RequestingPhysician)
        .join(OrderRequestingPhysician, OrderRequestingPhysician.requesting_physician_id == RequestingPhysician.id)
        .where(OrderRequestingPhysician.order_id == order.id, RequestingPhysician.tenant_id == order.tenant_id)
        .order_by(OrderRequestingPhysician.position, RequestingPhysician.id)
    ).all()
    if rows:
        return list(rows)
    if order.requesting_physician_id:
        legacy = session.get(RequestingPhysician, order.requesting_physician_id)
        if legacy is not None and str(legacy.tenant_id) == str(order.tenant_id):
            return [legacy]
    return []


def physicians_by_order(session: Session, orders: Sequence[Order]) -> dict:
    """Batch `order_physicians` for lists: a fixed number of queries."""
    if not orders:
        return {}
    ids = [o.id for o in orders]
    out: dict = {o.id: [] for o in orders}
    rows = session.exec(
        select(OrderRequestingPhysician.order_id, RequestingPhysician)
        .join(RequestingPhysician, RequestingPhysician.id == OrderRequestingPhysician.requesting_physician_id)
        .where(OrderRequestingPhysician.order_id.in_(ids))
        .order_by(OrderRequestingPhysician.order_id, OrderRequestingPhysician.position, RequestingPhysician.id)
    ).all()
    for order_id, physician in rows:
        out[order_id].append(physician)
    missing = [o for o in orders if not out[o.id] and o.requesting_physician_id]
    if missing:
        legacy = {p.id: p for p in session.exec(
            select(RequestingPhysician).where(RequestingPhysician.id.in_([o.requesting_physician_id for o in missing]))
        ).all()}
        for o in missing:
            p = legacy.get(o.requesting_physician_id)
            if p is not None and str(p.tenant_id) == str(o.tenant_id):
                out[o.id] = [p]
    return out


def set_order_physicians(session: Session, order: Order, physicians: Sequence[RequestingPhysician]) -> None:
    """Replace the order's physician set and mirror the legacy columns (see the module docstring).
    Flushes; the caller commits. `order.id` must exist."""
    previous = order_physicians(session, order)
    for row in session.exec(select(OrderRequestingPhysician).where(OrderRequestingPhysician.order_id == order.id)).all():
        session.delete(row)
    session.flush()
    for position, physician in enumerate(physicians):
        session.add(OrderRequestingPhysician(
            order_id=order.id, requesting_physician_id=physician.id, tenant_id=order.tenant_id, position=position))
    # legacy mirror: a single physician only (never a "primary" out of several)
    order.requesting_physician_id = physicians[0].id if len(physicians) == 1 else None
    # `requested_by`: regenerate only what was generated from the previous names (or empty)
    if not order.requested_by or order.requested_by == derive_requested_by(previous):
        order.requested_by = derive_requested_by(physicians)
    session.add(order)
    session.flush()
