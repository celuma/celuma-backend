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

from app.schemas.laboratory import ListLocation

from app.services.list_search import SearchQueryError, parse_search_query

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


def search_text(q: Optional[str]) -> Optional[str]:
    """`q`, validated against the search limits; over a limit it is a 422.

    The query is rejected rather than shortened: dropping a term from an AND
    query would return a wider result as if the whole query had been applied.
    """
    try:
        parse_search_query(q)
    except SearchQueryError as exc:
        raise HTTPException(422, str(exc))
    return q


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


def locate_id(value: Optional[str], request: Optional[PageRequest]) -> Optional[str]:
    """The `locate` record id, validated before any query runs.

    A malformed id is a 422 rather than "not found"; `locate` needs paged mode,
    because its answer is a page number.
    """
    if value is None:
        return None
    if request is None:
        raise HTTPException(422, "locate requires paged mode (page or page_size)")
    try:
        return str(UUID(value))
    except ValueError:
        raise HTTPException(422, f"Invalid locate value: {value}")


def locate_row(session, stmt, id_column, target_id: Optional[str], request: Optional[PageRequest]) -> Optional[ListLocation]:
    """Where `target_id` falls in the ordered result of `stmt` (Céluma 1.5, #66).

    A list re-opened after visiting a record asks whether that record still matches
    the same search and filters and, if it does, on which page the same ordering
    now puts it. The position is computed with the list's own ORDER BY (which
    always ends with the row id, so it is total), over the same statement, so it
    inherits tenant scope, filters, search and the removed-record exclusion. A
    record of another tenant, a removed record or one that no longer matches all
    answer `matches=False` without saying which. `None` when nothing was asked.
    """
    if target_id is None or request is None:
        return None
    ordering = list(stmt._order_by_clauses)  # the list's total order; never empty for these lists
    ranked = (
        stmt.order_by(None)
        .with_only_columns(id_column.label("row_id"), func.row_number().over(order_by=ordering).label("position"))
        .subquery()
    )
    position = session.exec(select(ranked.c.position).where(ranked.c.row_id == UUID(target_id))).first()
    if position is None:
        return ListLocation(id=target_id, matches=False)
    position = int(position)
    return ListLocation(
        id=target_id, matches=True, position=position, page=(position - 1) // request.page_size + 1,
    )
