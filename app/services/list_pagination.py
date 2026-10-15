"""Page parameters shared by the server-paginated lists (Céluma 1.4, #6).

Contract: `celuma-engineering/specs/lists/search-and-pagination.md`.

A list endpoint is in *paged mode* when the request carries `page` or
`page_size`; otherwise it keeps its pre-1.4 behaviour and returns the whole
(filtered, sorted) collection, so a client that predates the contract keeps
working. In paged mode search, filters, ordering and the total are computed
over the whole authorized collection before the page is cut, and the ordering
always ends with the row id so OFFSET paging over an unchanged dataset never
repeats or skips a row.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence, TypeVar
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import select

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class PageRequest:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_request(page: Optional[int], page_size: Optional[int]) -> Optional[PageRequest]:
    """`None` means the caller asked for the legacy full collection."""
    if page is None and page_size is None:
        return None
    return PageRequest(page=page or 1, page_size=page_size or DEFAULT_PAGE_SIZE)


def count_rows(session, stmt) -> int:
    """Total rows `stmt` selects, ignoring its ORDER BY/LIMIT."""
    return session.exec(
        select(func.count()).select_from(stmt.order_by(None).limit(None).offset(None).subquery())
    ).one()


def split_csv(values: Optional[list[str]]) -> list[str]:
    """Accept both `?state=A&state=B` and `?state=A,B`."""
    out: list[str] = []
    for value in values or []:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def enum_filter(values: Optional[list[str]], enum_cls: type[Enum], name: str) -> list[str]:
    """Validated enum filter values; an unknown value is a 422, not a 500."""
    out = []
    for value in split_csv(values):
        try:
            out.append(enum_cls(value).value)
        except ValueError:
            raise HTTPException(422, f"Invalid {name} filter value: {value}")
    return out


def uuid_filter(values: Optional[list[str]], name: str) -> list[str]:
    """Validated UUID filter values; an id of another tenant simply matches nothing."""
    out = []
    for value in split_csv(values):
        try:
            out.append(str(UUID(value)))
        except ValueError:
            raise HTTPException(422, f"Invalid {name} filter value: {value}")
    return out


def sort_field(value: Optional[str], allowed: Sequence[str]) -> str:
    """The requested sort field, or the list's default (the first allowed)."""
    if value is None:
        return allowed[0]
    if value not in allowed:
        raise HTTPException(422, f"Invalid sort field: {value}. Allowed: {', '.join(allowed)}")
    return value


T = TypeVar("T")


def run_list(session, stmt, request: Optional[PageRequest], build: Callable[..., list[T]]) -> tuple[list[T], int]:
    """Items and total for `stmt`: the page in paged mode, everything otherwise."""
    if request is None:
        rows = session.exec(stmt).all()
        return build(session, rows), len(rows)
    total = count_rows(session, stmt)
    rows = session.exec(stmt.offset(request.offset).limit(request.page_size)).all()
    return build(session, rows), total
