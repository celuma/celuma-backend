"""Configurable monthly numbering for orders and samples (Céluma 1.4, issue #8).

Contract: `celuma-engineering/specs/laboratory/monthly-numbering.md`;
decision record: ADR 0013.

Everything that renders, allocates, previews or validates a generated code
lives here, so the two creation routes (standalone and unified) cannot drift.

Allocation runs inside the CALLER's transaction. The counter increment, the
ledger row and the order/sample commit or roll back together: a failed
request releases its numbers, a committed number is never issued again.
Nothing in this module commits.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session, select

from app.models.numbering import NumberingAllocation, NumberingConfig
from app.models.tenant import Branch

logger = logging.getLogger(__name__)

ORDER = "order"
SAMPLE = "sample"
ENTITY_KINDS = (ORDER, SAMPLE)

MODE_AUTOMATIC = "automatic"
MODE_AUTOMATIC_ONLY = "automatic_only"
MODE_MANUAL_ONLY = "manual_only"
MODES = (MODE_AUTOMATIC, MODE_AUTOMATIC_ONLY, MODE_MANUAL_ONLY)

#: October 2026, base `CU`, sequence 1 -> `CU011026` (no separators).
DEFAULT_FORMAT = "{BASE}{SEQ}{MM}{YY}"
DEFAULT_MIN_WIDTH = 2
MIN_WIDTH_LIMITS = (1, 6)
MAX_FORMAT_LENGTH = 60
#: `order.order_code` / `sample.sample_code` are varchar(100). Longer is refused, never truncated.
MAX_CODE_LENGTH = 100
#: Per kind. A generated ORDER code is capped at 96 because the invoice created with the order is
#: numbered `INV-<order_code>` in a varchar(100) column; 96 + 4 fits, a longer code would not.
MAX_LENGTH_BY_KIND = {"order": 96, "sample": MAX_CODE_LENGTH}
#: Upper bound on occupied values skipped in one allocation before giving up.
MAX_SKIPS = 10_000
DEFAULT_TIMEZONE = "America/Mexico_City"
#: Sequences shown by the preview, chosen to cross each width boundary.
PREVIEW_SEQUENCES = (1, 2, 9, 10, 99, 100, 999, 1000)
#: How many candidates the read-only estimate inspects.
ESTIMATE_LOOKAHEAD = 50

TOKENS = ("BASE", "SEQ", "MM", "YY", "YYYY")
_LITERAL_RE = re.compile(r"^[A-Za-z0-9._-]*$")
_SPLIT_RE = re.compile(r"(\{[^{}]*\})")


class NumberingError(HTTPException):
    """HTTP error with a stable machine code: `detail = {code, message}`."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code, {"code": code, "message": message})


# --------------------------------------------------------------------------- format


def parse_format(fmt: str) -> List[tuple]:
    """Split a format into `("lit", text)` / `("tok", NAME)` segments.

    Raises `ValueError` with a human reason for anything outside the closed
    grammar. There are no expressions and no escapes.
    """
    if not isinstance(fmt, str) or not fmt:
        raise ValueError("The format is required")
    if len(fmt) > MAX_FORMAT_LENGTH:
        raise ValueError(f"The format cannot exceed {MAX_FORMAT_LENGTH} characters")
    segments: List[tuple] = []
    for part in _SPLIT_RE.split(fmt):
        if not part:
            continue
        if part.startswith("{") and part.endswith("}") and _SPLIT_RE.fullmatch(part):
            name = part[1:-1]
            if name not in TOKENS:
                raise ValueError(f"Unknown token {part}; allowed: " + ", ".join("{" + t + "}" for t in TOKENS))
            segments.append(("tok", name))
        else:
            if "{" in part or "}" in part:
                raise ValueError("Unbalanced braces in the format")
            if not _LITERAL_RE.match(part):
                raise ValueError("Literals may only contain letters, digits, '.', '_' and '-'")
            segments.append(("lit", part))
    return segments


def validate_format(fmt: str) -> List[str]:
    """Human-readable problems with a format (empty list = valid)."""
    try:
        segments = parse_format(fmt)
    except ValueError as exc:
        return [str(exc)]
    counts = {t: sum(1 for kind, v in segments if kind == "tok" and v == t) for t in TOKENS}
    problems = []
    for token in ("BASE", "SEQ", "MM"):
        if counts[token] != 1:
            problems.append(f"{{{token}}} must appear exactly once")
    if counts["YY"] + counts["YYYY"] != 1:
        problems.append("exactly one of {YY} or {YYYY} must appear")
    return problems


def validate_min_width(width) -> List[str]:
    lo, hi = MIN_WIDTH_LIMITS
    if isinstance(width, bool) or not isinstance(width, int) or not lo <= width <= hi:
        return [f"sequence_min_width must be an integer between {lo} and {hi}"]
    return []


def render_code(
    fmt: str, min_width: int, base: str, sequence: int, year: int, month: int, max_length: int = MAX_CODE_LENGTH,
) -> str:
    """Render a code. Never truncates: a result longer than `max_length` raises 409."""
    out = []
    for kind, value in parse_format(fmt):
        if kind == "lit":
            out.append(value)
        elif value == "BASE":
            out.append(base)
        elif value == "SEQ":
            out.append(str(sequence).zfill(min_width))
        elif value == "MM":
            out.append(f"{month:02d}")
        elif value == "YY":
            out.append(f"{year % 100:02d}")
        else:  # YYYY
            out.append(f"{year:04d}")
    code = "".join(out)
    if len(code) > max_length:
        raise NumberingError(
            409, "numbering_code_too_long",
            f"The generated code would exceed {max_length} characters; shorten the base code or the format",
        )
    return code


# --------------------------------------------------------------------------- period


@dataclass(frozen=True)
class Period:
    year: int
    month: int


def _now() -> datetime:
    """The allocation clock (UTC). Tests replace this function."""
    return datetime.now(timezone.utc)


def branch_timezone(branch: Optional[Branch]) -> ZoneInfo:
    """The branch's IANA timezone (`branch.timezone`, default Mexico City)."""
    name = (getattr(branch, "timezone", None) or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except Exception:  # unknown / malformed zone name: never fail a registration
        logger.warning("Unresolvable branch timezone %r; using %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def period_for(branch: Optional[Branch], instant: Optional[datetime] = None) -> Period:
    """The local month of `instant` (default: now) in the branch's timezone."""
    instant = instant or _now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    local = instant.astimezone(branch_timezone(branch))
    return Period(local.year, local.month)


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class NumberingSettings:
    entity_kind: str
    format: str = DEFAULT_FORMAT
    sequence_min_width: int = DEFAULT_MIN_WIDTH
    mode: str = MODE_AUTOMATIC
    is_default: bool = True


def get_settings(session: Session, tenant_id, kind: str) -> NumberingSettings:
    """The tenant's effective configuration; defaults when it has no row."""
    row = session.exec(
        select(NumberingConfig).where(NumberingConfig.tenant_id == tenant_id, NumberingConfig.entity_kind == kind)
    ).first()
    if row is None:
        return NumberingSettings(entity_kind=kind)
    return NumberingSettings(
        entity_kind=kind, format=row.format, sequence_min_width=row.sequence_min_width,
        mode=row.mode, is_default=False,
    )


# --------------------------------------------------------------------------- locks / lookups


def lock_code(session: Session, tenant_id, kind: str, code: str, *, wait: bool = True) -> bool:
    """Transaction-scoped advisory lock on one `(tenant, kind, code)`.

    Held until commit/rollback. The generated path asks with `wait=False` and
    treats a held lock as an occupied value (skip) so two requests can never
    wait on each other in a cycle; the manual path waits, taking its locks in
    sorted order (`claim_manual_codes`).
    """
    func = "pg_advisory_xact_lock" if wait else "pg_try_advisory_xact_lock"
    key = f"numbering:{tenant_id}:{kind}:{code}"
    result = session.execute(text(f"SELECT {func}(hashtextextended(:k, 0))"), {"k": key}).scalar()
    return True if wait else bool(result)


_OCCUPIED_SQL = {
    ORDER: 'SELECT EXISTS (SELECT 1 FROM "order" WHERE tenant_id = :t AND order_code = :c)',
    SAMPLE: "SELECT EXISTS (SELECT 1 FROM sample WHERE tenant_id = :t AND sample_code = :c)",
}


def ledger_has(session: Session, tenant_id, kind: str, code: str) -> bool:
    return bool(session.execute(
        text("SELECT EXISTS (SELECT 1 FROM numbering_allocation WHERE tenant_id = :t AND entity_kind = :k AND code = :c)"),
        {"t": str(tenant_id), "k": kind, "c": code},
    ).scalar())


def is_occupied(session: Session, tenant_id, kind: str, code: str) -> bool:
    """A live order/sample of the tenant carries `code` (legacy, manual or
    generated), or the ledger has ever issued it."""
    live = session.execute(text(_OCCUPIED_SQL[kind]), {"t": str(tenant_id), "c": code}).scalar()
    return bool(live) or ledger_has(session, tenant_id, kind, code)


# --------------------------------------------------------------------------- manual codes


def normalize_supplied(value: Optional[str]) -> Optional[str]:
    """A supplied code, trimmed; blank means "not supplied"."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def check_mode(settings: NumberingSettings, supplied: Optional[str]) -> bool:
    """Apply the tenant's mode. Returns True when a code must be GENERATED."""
    if supplied is not None and settings.mode == MODE_AUTOMATIC_ONLY:
        raise NumberingError(
            422, "numbering_manual_code_not_allowed",
            "This laboratory generates codes automatically; leave the code empty",
        )
    if supplied is None and settings.mode == MODE_MANUAL_ONLY:
        raise NumberingError(422, "numbering_code_required", "A code is required: automatic numbering is disabled")
    return supplied is None


def claim_manual_codes(session: Session, tenant_id, kind: str, codes: Iterable[str]) -> None:
    """Serialize and vet manual codes before the caller's own uniqueness check.

    Locks the codes in sorted order (no lock cycles between manual requests)
    and refuses any code the ledger has ever issued, so a retired generated
    number is not reissued by hand either. The caller then runs its existing
    live-row check, which now cannot race a generator holding the same string.
    """
    for code in sorted(set(codes)):
        lock_code(session, tenant_id, kind, code)
        if ledger_has(session, tenant_id, kind, code):
            raise NumberingError(
                400, "numbering_code_already_issued",
                f"The code '{code}' was already issued by automatic numbering and cannot be reused",
            )


# --------------------------------------------------------------------------- allocation


@dataclass
class AllocationRequest:
    """One code to generate. `key` lets the caller find its result."""
    key: object
    kind: str
    scope_id: UUID
    base_code: str


@dataclass
class Allocation:
    kind: str
    scope_id: UUID
    sequence_value: int
    code: str
    ledger_id: UUID
    period: Period
    skipped: int = 0


_BUMP_SQL = text(
    """
    INSERT INTO numbering_counter (tenant_id, entity_kind, scope_id, period_year, period_month, last_value, updated_at)
    VALUES (:t, :k, :s, :y, :m, 1, :now)
    ON CONFLICT (tenant_id, entity_kind, scope_id, period_year, period_month)
    DO UPDATE SET last_value = numbering_counter.last_value + 1, updated_at = EXCLUDED.updated_at
    RETURNING last_value
    """
)

_LEDGER_SQL = text(
    """
    INSERT INTO numbering_allocation
        (id, tenant_id, entity_kind, scope_id, period_year, period_month, sequence_value, code,
         format, sequence_min_width, created_by, created_at)
    VALUES (gen_random_uuid(), :t, :k, :s, :y, :m, :v, :c, :f, :w, :u, :now)
    ON CONFLICT DO NOTHING
    RETURNING id
    """
)


def _allocate_one(
    session: Session, *, tenant_id, request: AllocationRequest, period: Period,
    settings: NumberingSettings, actor_id,
) -> Allocation:
    skipped = 0
    while skipped <= MAX_SKIPS:
        now = datetime.utcnow()
        value = session.execute(
            _BUMP_SQL,
            {"t": str(tenant_id), "k": request.kind, "s": str(request.scope_id),
             "y": period.year, "m": period.month, "now": now},
        ).scalar()
        code = render_code(
            settings.format, settings.sequence_min_width, request.base_code, value, period.year, period.month,
            MAX_LENGTH_BY_KIND[request.kind],
        )
        if lock_code(session, tenant_id, request.kind, code, wait=False) and not is_occupied(
            session, tenant_id, request.kind, code
        ):
            ledger_id = session.execute(
                _LEDGER_SQL,
                {"t": str(tenant_id), "k": request.kind, "s": str(request.scope_id), "y": period.year,
                 "m": period.month, "v": value, "c": code, "f": settings.format,
                 "w": settings.sequence_min_width, "u": str(actor_id) if actor_id else None, "now": now},
            ).scalar()
            if ledger_id is not None:
                return Allocation(request.kind, request.scope_id, value, code, ledger_id, period, skipped)
        skipped += 1
    raise NumberingError(
        409, "numbering_exhausted",
        f"Could not find a free code after {MAX_SKIPS} attempts; check the format and existing codes",
    )


def allocate_batch(
    session: Session, *, tenant_id, requests: Sequence[AllocationRequest], period: Period,
    settings_by_kind: Dict[str, NumberingSettings], actor_id=None,
) -> Dict[object, Allocation]:
    """Allocate every requested code in one deterministic order.

    Scopes are processed ordered by `(kind, scope_id)` (orders before samples),
    keeping the request order inside a scope, so concurrent unified requests
    take their counter row locks in the same order and cannot deadlock. Two
    samples of one type in one order get consecutive values.
    """
    rank = {ORDER: 0, SAMPLE: 1}
    indexed = list(enumerate(requests))
    indexed.sort(key=lambda item: (rank[item[1].kind], str(item[1].scope_id), item[0]))
    results: Dict[object, Allocation] = {}
    for _, req in indexed:
        results[req.key] = _allocate_one(
            session, tenant_id=tenant_id, request=req, period=period,
            settings=settings_by_kind[req.kind], actor_id=actor_id,
        )
    return results


def attach_entity(session: Session, allocation: Allocation, entity_id) -> None:
    """Record which order/sample carries the code (provenance; no FK)."""
    session.execute(
        text("UPDATE numbering_allocation SET entity_id = :e WHERE id = :i"),
        {"e": str(entity_id), "i": str(allocation.ledger_id)},
    )


# --------------------------------------------------------------------------- preview


def peek_counter(session: Session, tenant_id, kind: str, scope_id, period: Period) -> int:
    """Current counter value, read WITHOUT locking or writing (0 = none yet)."""
    value = session.execute(
        text(
            "SELECT last_value FROM numbering_counter WHERE tenant_id = :t AND entity_kind = :k "
            "AND scope_id = :s AND period_year = :y AND period_month = :m"
        ),
        {"t": str(tenant_id), "k": kind, "s": str(scope_id), "y": period.year, "m": period.month},
    ).scalar()
    return int(value or 0)


@dataclass
class Estimate:
    sequence_value: int
    code: str
    skipped: int
    reserved: bool = field(default=False)


def estimate_next(
    session: Session, *, tenant_id, kind: str, scope_id, base_code: str, period: Period, settings: NumberingSettings,
) -> Estimate:
    """What allocation would produce right now, as a read-only estimate.

    Takes no lock, increments nothing, writes no ledger row: the result is
    NOT reserved and may differ from the real code (concurrent requests,
    skips, configuration changes).
    """
    value = peek_counter(session, tenant_id, kind, scope_id, period)
    limit = MAX_LENGTH_BY_KIND[kind]
    skipped = 0
    while skipped <= ESTIMATE_LOOKAHEAD:
        value += 1
        code = render_code(settings.format, settings.sequence_min_width, base_code, value, period.year, period.month, limit)
        if not is_occupied(session, tenant_id, kind, code):
            return Estimate(value, code, skipped)
        skipped += 1
    return Estimate(value, render_code(settings.format, settings.sequence_min_width, base_code, value, period.year, period.month, limit), skipped)


def boundary_examples(settings: NumberingSettings, base_code: str, period: Period) -> List[dict]:
    return [
        {"sequence": n, "code": render_code(
            settings.format, settings.sequence_min_width, base_code, n, period.year, period.month,
            MAX_LENGTH_BY_KIND[settings.entity_kind],
        )}
        for n in PREVIEW_SEQUENCES
    ]


def ledger_rows(session: Session, tenant_id, kind: str) -> List[NumberingAllocation]:
    return list(session.exec(
        select(NumberingAllocation).where(
            NumberingAllocation.tenant_id == tenant_id, NumberingAllocation.entity_kind == kind
        ).order_by(NumberingAllocation.created_at)
    ).all())
