"""Removal of study types and sample types from the catalog (Céluma 1.5, issue #94).

Removal is a retained tombstone, distinct from deactivation:

- **Deactivated** (`is_active = false`): still part of the catalog, listed in administration, can be
  edited and reactivated; refused for new samples (study types: unchanged pre-1.5 behaviour, see the spec).
- **Removed** (`removed_at` set, `is_active` forced false by `ck_*_removed_inactive`): no longer part of
  the catalog. It keeps its row, id and code, so every existing order, sample, price, invoice item,
  numbering configuration, counter, ledger row, timeline event and frozen report keeps resolving it. It
  is hidden from the ordinary catalog, refused by every creation, selection and transfer path, and
  frozen: no edit, deactivation, hard delete, numbering change or price may target it.

**Code reuse (owner decision, 2026-11-25).** The removal RELEASES the code (`code_released_at`) only when
no historical record is associated: no order for a study type, no sample for a sample type, counting the
withdrawn (#11 tombstoned) rows, which are still history. Otherwise the code stays reserved in that catalog
and tenant. A released code may be used by a DIFFERENT type (create or rename); the removed row keeps its
original code and is never reactivated or remapped. Code uniqueness counts only code-reserving rows
(`uq_sample_type_tenant_code_reserved`; the API check for study types), and every code-claiming write of
one tenant and catalog holds `lock_catalog_codes` first, so a release, a create/rename and an import
cannot interleave. The two catalogs are independent namespaces.

There is no restore. Nothing here deletes or rewrites a historical row.

Unlike orders and samples (#11, `app/core/soft_removal.py`) there is deliberately NO global ORM filter:
historical reads use `Session.get` on these ids and must keep resolving them. Every catalog read that
must hide removed types filters `removed_at IS NULL` explicitly (`not_removed`).

Contract: `celuma-engineering/specs/laboratory/catalog-type-removal.md`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session

#: Longest removal reason accepted (the column is varchar(500)).
MAX_REASON_LENGTH = 500

#: Catalog names used in (English) API error messages, keyed by table.
_LABEL = {"study_type": "study type", "sample_type": "sample type"}


class CatalogTypeRemovedError(HTTPException):
    """A write named a removed catalog type. 409 with a stable `detail.code` the UI can map."""

    def __init__(self, table: str, code: str, *, purpose: str):
        label = _LABEL[table]
        super().__init__(409, {
            "code": f"{table}_removed",
            "message": f"The {label} '{code}' was removed from the catalog and cannot be used {purpose}",
            "type_code": code,
        })


def is_removed(row) -> bool:
    return getattr(row, "removed_at", None) is not None


def not_removed(model):
    """SQL criterion for "still part of the catalog"."""
    return model.removed_at.is_(None)


def reserves_code(model):
    """SQL criterion for "this row holds its code": every row except a removed one that released it."""
    return model.code_released_at.is_(None)


def lock_catalog_codes(session: Session, table: str, tenant_id) -> None:
    """Serialise every write that claims or releases a code of one catalog of one tenant (create, rename,
    removal, import/copy confirm). Transaction-scoped; taken BEFORE any row lock of that catalog, so the
    order is always code lock → type row (no deadlock with registrations, which take only the row)."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                        {"k": f"catalog_codes:{table}:{tenant_id}"})


#: The records whose existence keeps a removed type's code reserved (withdrawn rows included).
HISTORICAL_KEYS = {"study_type": ("orders",), "sample_type": ("samples",)}


def has_history(table: str, refs: dict) -> bool:
    return any(refs.get(k, 0) > 0 for k in HISTORICAL_KEYS[table])


def ensure_not_removed(row, table: str, *, purpose: str) -> None:
    """Refuse any write that would use or change a removed type (409 `<table>_removed`)."""
    if is_removed(row):
        raise CatalogTypeRemovedError(table, row.code, purpose=purpose)


def clean_reason(reason: Optional[str]) -> Optional[str]:
    if reason is None:
        return None
    reason = reason.strip()
    if len(reason) > MAX_REASON_LENGTH:
        raise HTTPException(422, {"code": "removal_reason_too_long",
                                  "message": f"The reason cannot exceed {MAX_REASON_LENGTH} characters"})
    return reason or None


def references(session: Session, table: str, type_id) -> dict:
    """What still points at the type, counted for the audit row and the response (never changed here).

    Raw SQL on purpose: it must count removed orders and samples too (the #11 ORM filter would hide them),
    because their rows still reference the type.
    """
    tid = str(type_id)
    one = lambda sql: int(session.execute(text(sql), {"i": tid}).scalar_one())  # noqa: E731
    if table == "study_type":
        return {
            "orders": one('SELECT count(*) FROM "order" WHERE study_type_id = :i'),
            "price_entries": one("SELECT count(*) FROM price_catalog WHERE study_type_id = :i"),
            "invoice_items": one("SELECT count(*) FROM invoice_item WHERE study_type_id = :i"),
            "numbering_type_configs": one(
                "SELECT count(*) FROM numbering_type_config WHERE entity_kind = 'order' AND type_id = :i"),
        }
    return {
        "samples": one("SELECT count(*) FROM sample WHERE sample_type_id = :i"),
        "numbering_type_configs": one(
            "SELECT count(*) FROM numbering_type_config WHERE entity_kind = 'sample' AND type_id = :i"),
    }


def mark_removed(row, *, user_id, reason: Optional[str], release_code: bool, now: Optional[datetime] = None) -> None:
    """Set the tombstone on `row` (not committed). Deactivates it in the same write and, when the caller
    established (under `lock_catalog_codes` and the row lock) that no historical record exists, releases
    its code. The row's `code` itself is never changed."""
    now = now or datetime.utcnow()
    row.is_active = False
    row.removed_at = now
    row.removed_by = user_id
    row.removal_reason = reason
    row.code_released_at = now if release_code else None
