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
    """The caller holds the capability to type a code (not inferred from admin status:
    it is an explicit permission granted to named roles)."""
    from app.core.rbac import has_permission

    return has_permission(user.id, MANUAL_CODE_PERMISSION, session)


def manual_entry_for(settings: NumberingSettings, permitted: bool) -> str:
    """How a registration form should treat the code input for this caller:
    `optional` | `required` | `hidden` | `blocked` (required but the caller may not type one)."""
    if not settings.active:  # compatibility: no tenant policy, no capability gate (pre-numbering behaviour)
        return "required" if settings.entity_kind == SAMPLE else "optional"
    if settings.manual_policy == POLICY_FORBIDDEN:
        return "hidden"
    if settings.manual_policy == POLICY_REQUIRED:
        return "required" if permitted else "blocked"
    return "optional" if permitted else "hidden"


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


def check_mode(settings: NumberingSettings, supplied: Optional[str], permitted: bool = True) -> bool:
    """Apply the tenant policy AND the caller's capability. Returns True when a
    code must be GENERATED; raises when the request is not allowed.

    Inactive (compatibility): a supplied code is accepted as before; an order
    without one is generated by the legacy allocator; a sample without one is
    refused (as before, samples were typed by hand). Active: the policy and
    the `lab:enter_manual_code` capability decide.
    """
    if not settings.active:
        if supplied is None and settings.entity_kind == SAMPLE:
            raise NumberingError(422, "numbering_code_required", "A sample code is required: automatic sample numbering is not active")
        return supplied is None
    if supplied is not None:
        if settings.manual_policy == POLICY_FORBIDDEN:
            raise NumberingError(
                422, "numbering_manual_code_not_allowed",
                "This laboratory generates codes automatically; leave the code empty",
            )
        if not permitted:
            raise NumberingError(
                403, "numbering_manual_code_forbidden",
                f"Permission required to type a code manually: {MANUAL_CODE_PERMISSION}",
            )
        return False
    if settings.manual_policy == POLICY_REQUIRED:
        if not permitted:
            raise NumberingError(
                403, "numbering_manual_code_forbidden",
                f"This laboratory requires a typed code and you lack the permission: {MANUAL_CODE_PERMISSION}",
            )
        raise NumberingError(422, "numbering_code_required", "A code is required: automatic numbering is disabled")
    return True


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


def audit_manual_code(session: Session, *, tenant_id, user_id, kind: str, entity_id, code: str) -> None:
    """Record that a typed (not generated) code was used. Same transaction as the entity."""
    from app.services.sample_types import record_audit

    record_audit(
        session, tenant_id=tenant_id, user_id=user_id, action="NUMBERING.MANUAL_CODE_USED",
        entity_type=kind, entity_id=entity_id, description=f"Manual {kind} code used",
        metadata={"entity_kind": kind, "code": code},
    )


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


def legacy_high_water(session: Session, tenant_id, scope_id, base_code: str) -> int:
    """The highest sequence the compatibility allocator must stay above for a study type.

    Evidence only, never the number of rows (a sparse history such as `CU-1`, `CU-50` has
    two rows but a high-water mark of 50):

    1. the highest `<n>` of the tenant's existing orders whose code is exactly
       `<base code>-<digits>` (1-9 digits; irregular or manual shapes such as `CU-7x`,
       `CU--3`, `CU-007-A` are ignored, and never altered);
    2. the highest `sequence_value` the permanent ledger recorded for this study type under
       the compatibility scheme (so a code issued and later deleted is never forgotten).

    The result is only a floor: allocation takes `GREATEST(counter + 1, floor + 1)` atomically,
    so counters only move forward. History deleted BEFORE the ledger existed leaves no
    evidence and cannot be reconstructed; nothing here pretends it can.
    """
    prefix = f"{base_code}-"
    suffix_from = len(prefix) + 1
    from_orders = session.execute(
        text(
            'SELECT max(CASE WHEN substr(order_code, :n) ~ :rx THEN substr(order_code, :n)::bigint END) '
            'FROM "order" WHERE tenant_id = :t AND order_code LIKE :like ESCAPE \'\\\''
        ),
        {"n": suffix_from, "rx": f"^[0-9]{{1,{_LEGACY_SUFFIX_DIGITS}}}$", "t": str(tenant_id),
         "like": _like_escape(prefix) + "%"},
    ).scalar()
    from_ledger = session.execute(
        text(
            "SELECT max(sequence_value) FROM numbering_allocation WHERE tenant_id = :t AND entity_kind = 'order' "
            "AND scope_id = :s AND scheme = 'legacy'"
        ),
        {"t": str(tenant_id), "s": str(scope_id)},
    ).scalar()
    return int(max(from_orders or 0, from_ledger or 0))


def _allocate_one(
    session: Session, *, tenant_id, request: AllocationRequest, instant: datetime,
    settings: NumberingSettings, actor_id,
) -> Allocation:
    legacy = settings.scheme == SCHEME_LEGACY
    branch = counter_branch(settings, request)
    if legacy:
        period = LEGACY_PERIOD
        # Floor from evidence (never a row count), re-read on every allocation so a code typed or
        # imported later also moves the counter forward; the bump below can only increase it.
        start = legacy_high_water(session, tenant_id, request.scope_id, request.base_code) + 1
    else:
        period = period_in_zone(settings.zone_name(request.branch_timezone), instant)
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

    Counter rows are locked ordered by `(kind, scope_id, branch)` (orders before
    samples), keeping the request order inside a counter, so concurrent unified
    requests take their row locks in the same order and cannot deadlock. Two
    samples of one type in one order get consecutive values. `instant` is the ONE
    instant of the request: each kind converts it with its own numbering timezone. (The idempotency
    lock, when present, is always taken BEFORE any of these.)
    """
    rank = {ORDER: 0, SAMPLE: 1}
    indexed = list(enumerate(requests))
    indexed.sort(key=lambda item: (
        rank[item[1].kind], str(item[1].scope_id),
        str(counter_branch(settings_by_kind[item[1].kind], item[1])), item[0],
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
    value = max(last, legacy_high_water(session, tenant_id, scope_id, base_code)) if legacy else last

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
