"""Configurable monthly numbering for orders and samples (Céluma 1.4, issue #8).

Contract: `celuma-engineering/specs/laboratory/monthly-numbering.md`;
decision record: ADR 0013.

Everything that renders, allocates, previews or validates a generated code
lives here, so the creation routes (standalone and unified) cannot drift.

Registration is AUTOMATIC (issue #9 follow-up, 2026-10-16): no creation route accepts a typed
order or sample code. An inactive kind uses the compatibility allocator (`<base code>-<n>`, a
race-safe counter plus the permanent ledger) for samples as it always did for orders; an active kind
uses the configured monthly format. The old manual-code policy no longer affects registration. The
narrow post-registration sample-code correction lives in `app/services/sample_code_correction.py`.

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

POLICY_ALLOWED = "allowed"
POLICY_FORBIDDEN = "forbidden"
POLICY_REQUIRED = "required"
MANUAL_POLICIES = (POLICY_ALLOWED, POLICY_FORBIDDEN, POLICY_REQUIRED)

SCOPE_TENANT = "tenant"
SCOPE_BRANCH = "branch"
SCOPES = (SCOPE_TENANT, SCOPE_BRANCH)

SCHEME_MONTHLY = "monthly"
SCHEME_LEGACY = "legacy"

#: Counter `branch_id` of tenant-wide and compatibility counters.
NIL_BRANCH = UUID("00000000-0000-0000-0000-000000000000")
#: Capability required to type a code while numbering is active (tenant policy AND this).
MANUAL_CODE_PERMISSION = "lab:enter_manual_code"
START_VALUE_LIMITS = (1, 99_999_999)

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

TOKENS = ("BASE", "BRANCH", "SEQ", "MM", "YY", "YYYY")
#: Matched with `fullmatch`: `$` would accept a trailing newline.
_LITERAL_RE = re.compile(r"[A-Za-z0-9._-]*")
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
            if not _LITERAL_RE.fullmatch(part):
                raise ValueError("Literals may only contain letters, digits, '.', '_' and '-'")
            segments.append(("lit", part))
    return segments


def validate_format(fmt: str, scope: str = SCOPE_TENANT) -> List[str]:
    """Human-readable problems with a format (empty list = valid).

    `scope` ties the format to the counter scope: a branch-scoped format must
    carry `{BRANCH}` exactly once (the branch is part of the code's identity,
    not only of the counter key); a tenant-wide one must not.
    """
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
    if scope == SCOPE_BRANCH and counts["BRANCH"] != 1:
        problems.append("{BRANCH} must appear exactly once when the numbering is branch-scoped")
    if scope != SCOPE_BRANCH and counts["BRANCH"]:
        problems.append("{BRANCH} is only allowed when the numbering is branch-scoped")
    return problems


def validate_min_width(width) -> List[str]:
    lo, hi = MIN_WIDTH_LIMITS
    if isinstance(width, bool) or not isinstance(width, int) or not lo <= width <= hi:
        return [f"sequence_min_width must be an integer between {lo} and {hi}"]
    return []


def validate_start_value(value) -> List[str]:
    lo, hi = START_VALUE_LIMITS
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return [f"start_value must be an integer between {lo} and {hi}"]
    return []


def render_code(
    fmt: str, min_width: int, base: str, sequence: int, year: int, month: int, max_length: int = MAX_CODE_LENGTH,
    branch: str = "",
) -> str:
    """Render a code. Never truncates: a result longer than `max_length` raises 409."""
    out = []
    for kind, value in parse_format(fmt):
        if kind == "lit":
            out.append(value)
        elif value == "BASE":
            out.append(base)
        elif value == "BRANCH":
            if not _LITERAL_RE.fullmatch(branch) or not branch:
                raise NumberingError(
                    422, "numbering_branch_code_unsafe",
                    "The branch code is empty or has characters not allowed in a generated code (letters, digits, '.', '_', '-')",
                )
            out.append(branch)
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


_ZONE_RE = re.compile(r"[A-Za-z0-9_+/-]{1,100}")


def validate_timezone(name) -> List[str]:
    """An IANA zone known to this runtime. Unknown or malformed names are rejected, never guessed."""
    if not isinstance(name, str) or not _ZONE_RE.fullmatch(name):
        return ["timezone must be an IANA zone name such as America/Mexico_City"]
    try:
        ZoneInfo(name)
    except Exception:
        return [f"unknown timezone '{name}'"]
    return []


def _zone(name: Optional[str]) -> ZoneInfo:
    """Resolve a stored zone; a stored value that no longer resolves never fails a registration
    (it falls back to the default zone and is logged)."""
    name = (name or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("Unresolvable timezone %r; using %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def period_in_zone(zone_name: Optional[str], instant: Optional[datetime] = None) -> Period:
    """The local month of `instant` (default: now) in the named zone."""
    instant = instant or _now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    local = instant.astimezone(_zone(zone_name))
    return Period(local.year, local.month)


def period_for(branch: Optional[Branch], instant: Optional[datetime] = None) -> Period:
    """The local month of `instant` in a BRANCH's timezone (`branch.timezone`)."""
    return period_in_zone(getattr(branch, "timezone", None), instant)


def now() -> datetime:
    """The single instant of one request (routes read it once; tests replace `_now`)."""
    return _now()


def receipt_local(received_at: datetime, zone_name: Optional[str]) -> datetime:
    """A sample's receipt date as the timezone-less LOCAL wall clock of the numbering zone.

    * Date-only input and timezone-less datetimes already ARE a local calendar date: they are returned
      unchanged (the registration's own calendar date, never shifted by a browser, server or UTC).
    * An instant with an explicit offset is converted to the numbering zone first.

    The result both defines the monthly period and is what is stored in `sample.received_at` (a
    `timestamp without time zone` column), so the stored date, the rendered month/year and the counter
    period always agree. No year range is imposed; an offset instant whose conversion to the numbering
    zone is not representable (beyond year 1 or 9999) is refused with 422 `numbering_receipt_date_invalid`.
    """
    if received_at.tzinfo is None:
        local = received_at
    else:
        try:
            local = received_at.astimezone(_zone(zone_name)).replace(tzinfo=None)
        except (OverflowError, ValueError):
            local = None
    if local is None:
        raise NumberingError(
            422, "numbering_receipt_date_invalid",
            "The receipt date cannot be represented in the numbering timezone",
        )
    return local


def receipt_period(received_at: datetime, zone_name: Optional[str]) -> Period:
    """The monthly period (year, month) of a sample's receipt date in the numbering zone."""
    local = receipt_local(received_at, zone_name)
    return Period(local.year, local.month)


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class NumberingSettings:
    entity_kind: str
    format: str = DEFAULT_FORMAT
    sequence_min_width: int = DEFAULT_MIN_WIDTH
    manual_policy: str = POLICY_ALLOWED
    start_value: int = 1
    scope: str = SCOPE_TENANT
    #: The laboratory's numbering timezone (IANA). Defines the month of every TENANT-WIDE counter,
    #: whichever branch handled the request; a branch-scoped counter uses its branch's own zone.
    timezone: str = DEFAULT_TIMEZONE
    #: Deliberate activation. False (the state of every tenant until it is activated) keeps the
    #: pre-numbering behaviour: orders `<code>-<n>` from a race-safe counter, samples typed by hand.
    active: bool = False
    is_default: bool = True

    @property
    def scheme(self) -> str:
        return SCHEME_MONTHLY if self.active else SCHEME_LEGACY

    def zone_name(self, branch_zone: Optional[str]) -> str:
        """Timezone that defines the period of a code: the branch's for a branch-scoped counter,
        the laboratory's numbering timezone otherwise (never "whichever branch handled it")."""
        if self.scope == SCOPE_BRANCH and branch_zone:
            return branch_zone
        return self.timezone


def get_settings(session: Session, tenant_id, kind: str) -> NumberingSettings:
    """The tenant's effective configuration; inactive defaults when it has no row."""
    row = session.exec(
        select(NumberingConfig).where(NumberingConfig.tenant_id == tenant_id, NumberingConfig.entity_kind == kind)
    ).first()
    if row is None:
        return NumberingSettings(entity_kind=kind)
    return NumberingSettings(
        entity_kind=kind, format=row.format, sequence_min_width=row.sequence_min_width,
        manual_policy=row.manual_policy, start_value=row.start_value, scope=row.scope,
        timezone=row.timezone, active=row.active, is_default=False,
    )


def can_enter_manual(session: Session, user) -> bool:
    """The caller holds the privileged identifier capability (`lab:enter_manual_code`).

    Registration never types a code any more; the capability now authorizes only the controlled
    post-registration sample-code correction, so an explicit permission (never admin status) still
    gates every identifier a person writes by hand."""
    from app.core.rbac import has_permission

    return has_permission(user.id, MANUAL_CODE_PERMISSION, session)


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


def retired_by_correction(session: Session, tenant_id, code: str) -> bool:
    """A sample code that a correction replaced stays retired for the tenant (#10)."""
    return bool(session.execute(
        text("SELECT EXISTS (SELECT 1 FROM sample_code_correction WHERE tenant_id = :t AND old_code = :c)"),
        {"t": str(tenant_id), "c": code},
    ).scalar())


def retired_by_cleanup(session: Session, tenant_id, kind: str, code: str) -> bool:
    """A code removed by a laboratory-content cleanup (#39) stays retired for the tenant, even a manual or
    legacy one the ledger never saw."""
    return bool(session.execute(
        text("SELECT EXISTS (SELECT 1 FROM laboratory_cleanup_retired_code "
             "WHERE tenant_id = :t AND entity_kind = :k AND code = :c)"),
        {"t": str(tenant_id), "k": kind, "c": code},
    ).scalar())


def is_occupied(session: Session, tenant_id, kind: str, code: str) -> bool:
    """A row of the tenant carries `code` (legacy, manual or generated; REMOVED rows count, they are
    tombstones), the ledger has ever issued it, a correction retired it, or a laboratory-content
    cleanup removed it."""
    live = session.execute(text(_OCCUPIED_SQL[kind]), {"t": str(tenant_id), "c": code}).scalar()
    if live or ledger_has(session, tenant_id, kind, code):
        return True
    if retired_by_cleanup(session, tenant_id, kind, code):
        return True
    return kind == SAMPLE and retired_by_correction(session, tenant_id, code)


# --------------------------------------------------------------------------- allocation


@dataclass
class AllocationRequest:
    """One code to generate. `key` lets the caller find its result."""
    key: object
    kind: str
    scope_id: UUID
    base_code: str
    #: The registering branch (its code feeds `{BRANCH}`; its id keys a branch-scoped counter).
    branch_id: Optional[UUID] = None
    branch_code: str = ""
    #: The registering branch's own zone (used only by a branch-scoped counter).
    branch_timezone: Optional[str] = None
    #: A SAMPLE's receipt date (date-only/timezone-less = local calendar date; with offset = an instant).
    #: It alone defines the sample's monthly period; a sample request without it is refused, never
    #: numbered with the request clock. Orders ignore it and keep the allocation-instant policy.
    received_at: Optional[datetime] = None


@dataclass
class Allocation:
    kind: str
    scope_id: UUID
    sequence_value: int
    code: str
    ledger_id: UUID
    period: Period
    skipped: int = 0


def counter_branch(settings: NumberingSettings, request: AllocationRequest) -> UUID:
    """The counter's branch component: the real branch only for an ACTIVE branch-scoped numbering."""
    if settings.active and settings.scope == SCOPE_BRANCH and request.branch_id is not None:
        return request.branch_id
    return NIL_BRANCH


_BUMP_SQL = text(
    """
    INSERT INTO numbering_counter
        (tenant_id, entity_kind, scope_id, branch_id, period_year, period_month, last_value, updated_at)
    VALUES (:t, :k, :s, :b, :y, :m, :start, :now)
    ON CONFLICT (tenant_id, entity_kind, scope_id, branch_id, period_year, period_month)
    DO UPDATE SET last_value = GREATEST(numbering_counter.last_value + 1, :start), updated_at = EXCLUDED.updated_at
    RETURNING last_value
    """
)

_LEDGER_SQL = text(
    """
    INSERT INTO numbering_allocation
        (id, tenant_id, entity_kind, scope_id, branch_id, period_year, period_month, sequence_value, code,
         scheme, format, sequence_min_width, created_by, created_at)
    VALUES (gen_random_uuid(), :t, :k, :s, :b, :y, :m, :v, :c, :scheme, :f, :w, :u, :now)
    ON CONFLICT DO NOTHING
    RETURNING id
    """
)

LEGACY_PERIOD = Period(0, 0)
LEGACY_FORMAT = "{BASE}-{N}"


_LEGACY_SUFFIX_DIGITS = 9


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_LEGACY_SOURCE = {
    ORDER: ('"order"', "order_code"),
    SAMPLE: ("sample", "sample_code"),
}


def legacy_high_water(session: Session, tenant_id, scope_id, base_code: str, kind: str = ORDER) -> int:
    """The highest sequence the compatibility allocator must stay above for a study/sample type.

    Evidence only, never the number of rows (a sparse history such as `CU-1`, `CU-50` has
    two rows but a high-water mark of 50):

    1. the highest `<n>` of the tenant's existing orders (samples, for the sample kind; removed rows
       included) whose code is exactly `<base code>-<digits>` (1-9 digits; irregular or manual shapes
       such as `CU-7x`, `CU--3`, `CU-007-A` are ignored, and never altered);
    2. the highest `sequence_value` the permanent ledger recorded for this type under
       the compatibility scheme (so a code issued and later deleted is never forgotten);
    3. the highest `<n>` among codes a laboratory-content cleanup (#39) removed and retired.

    The result is only a floor: allocation takes `GREATEST(counter + 1, floor + 1)` atomically,
    so counters only move forward. History deleted BEFORE the ledger existed leaves no
    evidence and cannot be reconstructed; nothing here pretends it can.
    """
    table, column = _LEGACY_SOURCE[kind]
    prefix = f"{base_code}-"
    suffix_from = len(prefix) + 1
    from_rows = session.execute(
        text(
            f"SELECT max(CASE WHEN substr({column}, :n) ~ :rx THEN substr({column}, :n)::bigint END) "
            f"FROM {table} WHERE tenant_id = :t AND {column} LIKE :like ESCAPE '\\'"
        ),
        {"n": suffix_from, "rx": f"^[0-9]{{1,{_LEGACY_SUFFIX_DIGITS}}}$", "t": str(tenant_id),
         "like": _like_escape(prefix) + "%"},
    ).scalar()
    from_ledger = session.execute(
        text(
            "SELECT max(sequence_value) FROM numbering_allocation WHERE tenant_id = :t AND entity_kind = :k "
            "AND scope_id = :s AND scheme = 'legacy'"
        ),
        {"t": str(tenant_id), "k": kind, "s": str(scope_id)},
    ).scalar()
    from_cleanup = session.execute(
        text(
            "SELECT max(CASE WHEN substr(code, :n) ~ :rx THEN substr(code, :n)::bigint END) "
            "FROM laboratory_cleanup_retired_code WHERE tenant_id = :t AND entity_kind = :k AND code LIKE :like ESCAPE '\\'"
        ),
        {"n": suffix_from, "rx": f"^[0-9]{{1,{_LEGACY_SUFFIX_DIGITS}}}$", "t": str(tenant_id), "k": kind,
         "like": _like_escape(prefix) + "%"},
    ).scalar()
    return int(max(from_rows or 0, from_ledger or 0, from_cleanup or 0))


def request_period(settings: NumberingSettings, request: AllocationRequest, instant: datetime) -> Period:
    """The period a request allocates in. Pure: no database access, so batches can order their locks by it.

    Orders: the allocation instant in the numbering zone (unchanged policy). Samples (#33): the receipt
    date in the numbering zone (tenant-wide counters: the laboratory zone; branch-scoped: the branch's)."""
    if settings.scheme == SCHEME_LEGACY:
        return LEGACY_PERIOD
    zone = settings.zone_name(request.branch_timezone)
    if request.kind == SAMPLE:
        if request.received_at is None:
            raise NumberingError(
                422, "numbering_receipt_date_required",
                "A sample's receipt date is required to number it; the registration time is never used instead",
            )
        return receipt_period(request.received_at, zone)
    return period_in_zone(zone, instant)


def _allocate_one(
    session: Session, *, tenant_id, request: AllocationRequest, instant: datetime,
    settings: NumberingSettings, actor_id,
) -> Allocation:
    legacy = settings.scheme == SCHEME_LEGACY
    branch = counter_branch(settings, request)
    period = request_period(settings, request, instant)
    if legacy:
        # Floor from evidence (never a row count), re-read on every allocation so a code typed or
        # imported later also moves the counter forward; the bump below can only increase it.
        start = legacy_high_water(session, tenant_id, request.scope_id, request.base_code, request.kind) + 1
    else:
        start = settings.start_value
    max_len = MAX_LENGTH_BY_KIND[request.kind]
    skipped = 0
    while skipped <= MAX_SKIPS:
        now = datetime.utcnow()
        value = session.execute(
            _BUMP_SQL,
            {"t": str(tenant_id), "k": request.kind, "s": str(request.scope_id), "b": str(branch),
             "y": period.year, "m": period.month, "start": start, "now": now},
        ).scalar()
        if legacy:
            code = f"{request.base_code}-{value}"
            if len(code) > max_len:
                raise NumberingError(409, "numbering_code_too_long", f"The generated code would exceed {max_len} characters")
        else:
            code = render_code(
                settings.format, settings.sequence_min_width, request.base_code, value, period.year, period.month,
                max_len, request.branch_code,
            )
        if lock_code(session, tenant_id, request.kind, code, wait=False) and not is_occupied(
            session, tenant_id, request.kind, code
        ):
            ledger_id = session.execute(
                _LEDGER_SQL,
                {"t": str(tenant_id), "k": request.kind, "s": str(request.scope_id), "b": str(branch),
                 "y": period.year, "m": period.month, "v": value, "c": code, "scheme": settings.scheme,
                 "f": LEGACY_FORMAT if legacy else settings.format, "w": settings.sequence_min_width,
                 "u": str(actor_id) if actor_id else None, "now": now},
            ).scalar()
            if ledger_id is not None:
                return Allocation(request.kind, request.scope_id, value, code, ledger_id, period, skipped)
        skipped += 1
    raise NumberingError(
        409, "numbering_exhausted",
        f"Could not find a free code after {MAX_SKIPS} attempts; check the format and existing codes",
    )


def allocate_batch(
    session: Session, *, tenant_id, requests: Sequence[AllocationRequest], instant: datetime,
    settings_by_kind: Dict[str, NumberingSettings], actor_id=None,
) -> Dict[object, Allocation]:
    """Allocate every requested code in one deterministic order.

    Counter rows are locked ordered by `(kind, scope_id, branch, period_year, period_month)` (orders
    before samples), keeping the request order inside a counter, so concurrent unified requests take
    their row locks in the same order and cannot deadlock, even when their samples span several
    receipt-date periods. Two samples of one type AND period in one order get consecutive values.
    `instant` is the ONE instant of the request and defines the period of orders only; each sample
    uses its own receipt date (#33). Every period is resolved (and an invalid or missing receipt date
    refused) BEFORE the first counter is touched. (The idempotency lock, when present, is always taken
    BEFORE any of these.)
    """
    rank = {ORDER: 0, SAMPLE: 1}
    periods = [request_period(settings_by_kind[r.kind], r, instant) for r in requests]
    indexed = list(enumerate(requests))
    indexed.sort(key=lambda item: (
        rank[item[1].kind], str(item[1].scope_id),
        str(counter_branch(settings_by_kind[item[1].kind], item[1])),
        periods[item[0]].year, periods[item[0]].month, item[0],
    ))
    results: Dict[object, Allocation] = {}
    for _, req in indexed:
        results[req.key] = _allocate_one(
            session, tenant_id=tenant_id, request=req, instant=instant,
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


def peek_counter(
    session: Session, tenant_id, kind: str, scope_id, period: Period, branch: UUID = NIL_BRANCH,
) -> int:
    """Current counter value, read WITHOUT locking or writing (0 = none yet)."""
    value = session.execute(
        text(
            "SELECT last_value FROM numbering_counter WHERE tenant_id = :t AND entity_kind = :k "
            "AND scope_id = :s AND branch_id = :b AND period_year = :y AND period_month = :m"
        ),
        {"t": str(tenant_id), "k": kind, "s": str(scope_id), "b": str(branch), "y": period.year, "m": period.month},
    ).scalar()
    return int(value or 0)


@dataclass
class Estimate:
    sequence_value: int
    code: str
    skipped: int
    reserved: bool = field(default=False)


def estimate_next(
    session: Session, *, tenant_id, kind: str, scope_id, base_code: str, period: Period,
    settings: NumberingSettings, branch_id=None, branch_code: str = "",
) -> Estimate:
    """What allocation would produce right now, as a read-only estimate.

    Takes no lock, increments nothing, writes no ledger row: the result is
    NOT reserved and may differ from the real code (concurrent requests,
    skips, configuration changes).
    """
    request = AllocationRequest(None, kind, scope_id, base_code, branch_id, branch_code)
    branch = counter_branch(settings, request)
    limit = MAX_LENGTH_BY_KIND[kind]
    legacy = settings.scheme == SCHEME_LEGACY
    eff = LEGACY_PERIOD if legacy else period
    last = peek_counter(session, tenant_id, kind, scope_id, eff, branch)
    value = max(last, legacy_high_water(session, tenant_id, scope_id, base_code, kind)) if legacy else last

    def render(v: int) -> str:
        if legacy:
            code = f"{base_code}-{v}"
            if len(code) > limit:
                raise NumberingError(409, "numbering_code_too_long", f"The generated code would exceed {limit} characters")
            return code
        return render_code(settings.format, settings.sequence_min_width, base_code, v, period.year, period.month, limit, branch_code)

    skipped = 0
    while skipped <= ESTIMATE_LOOKAHEAD:
        value = max(value + 1, 1 if legacy else settings.start_value)
        code = render(value)
        if not is_occupied(session, tenant_id, kind, code):
            return Estimate(value, code, skipped)
        skipped += 1
    return Estimate(value, render(value), skipped)


def boundary_examples(settings: NumberingSettings, base_code: str, period: Period, branch_code: str = "") -> List[dict]:
    return [
        {"sequence": n, "code": render_code(
            settings.format, settings.sequence_min_width, base_code, n, period.year, period.month,
            MAX_LENGTH_BY_KIND[settings.entity_kind], branch_code,
        )}
        for n in PREVIEW_SEQUENCES
    ]


def ledger_rows(session: Session, tenant_id, kind: str) -> List[NumberingAllocation]:
    return list(session.exec(
        select(NumberingAllocation).where(
            NumberingAllocation.tenant_id == tenant_id, NumberingAllocation.entity_kind == kind
        ).order_by(NumberingAllocation.created_at)
    ).all())
