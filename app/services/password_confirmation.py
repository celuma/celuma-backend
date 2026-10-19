"""Céluma 1.4 (#13): the acting user's own current password confirms signing
and reopening.

Signing publishes an immutable clinical document and reopening reverses an
approval. An active session alone no longer authorizes either: every route
that signs, publishes or reopens a report calls `require_password_confirmation`
after its tenant, role, capability, assignment and lifecycle guards and before
any claim, storage write, row change, event or notification. A caller refused
by an earlier guard therefore never reaches this check.

Confirmation verifies identity; it grants nothing. Only the authenticated
actor's own hash is consulted, so another user's (or an administrator's)
password never confirms anything, and a correct password never widens the
reviewer contract in `report_authorization.py`.

Failures and the throttle:

* missing/empty password -> 403 ``password_confirmation_required``; no guess
  was made, so nothing is recorded;
* wrong password -> 403 ``password_confirmation_invalid`` plus ONE security
  audit row (``SECURITY.PASSWORD_CONFIRMATION_FAILED``) written in its own
  session, so the refused request itself commits nothing; never an
  `OrderEvent`, notification or clinical audit action;
* ``password_confirmation_max_failures`` such rows for the actor inside
  ``password_confirmation_window_seconds`` -> 429
  ``password_confirmation_throttled`` WITHOUT verifying, so the limiter cannot
  be used to test a guess. The rows live in the database, so every API process
  sees the same count.

The supplied password is never logged, stored or echoed. See
specs/reports/lifecycle.md and adr/0017-password-confirmed-clinical-actions.md
in celuma-engineering.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import verify_password
from app.models.audit import AuditLog
from app.models.report import Report
from app.models.user import AppUser

logger = logging.getLogger(__name__)

PASSWORD_CONFIRMATION_FAILED_ACTION = "SECURITY.PASSWORD_CONFIRMATION_FAILED"

REQUIRED_CODE = "password_confirmation_required"
INVALID_CODE = "password_confirmation_invalid"
THROTTLED_CODE = "password_confirmation_throttled"

#: Longer inputs are refused (and counted) without hashing them.
MAX_PASSWORD_LENGTH = 1024


def _refuse(status_code: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message, **extra})


def _recent_failures(session: Session, actor_id, now: datetime) -> list[datetime]:
    """Newest-first timestamps of the actor's failures inside the window,
    at most `max_failures` of them (all the throttle needs)."""
    window_start = now - timedelta(seconds=settings.password_confirmation_window_seconds)
    return list(
        session.exec(
            select(AuditLog.created_at)
            .where(
                AuditLog.actor_user_id == actor_id,
                AuditLog.action == PASSWORD_CONFIRMATION_FAILED_ACTION,
                AuditLog.created_at >= window_start,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(max(settings.password_confirmation_max_failures, 1))
        ).all()
    )


def _record_failure(session: Session, *, user: AppUser, report: Report, attempted_action: str) -> None:
    """Commit the credential-free security audit row in a separate session.

    The request's own session is never committed here: whatever the refused
    request may have loaded or locked is rolled back when it ends, so a wrong
    password cannot persist anything except this row."""
    with Session(session.get_bind()) as audit_session:
        audit_session.add(
            AuditLog(
                tenant_id=report.tenant_id,
                branch_id=report.branch_id,
                actor_user_id=user.id,
                action=PASSWORD_CONFIRMATION_FAILED_ACTION,
                entity_type="report",
                entity_id=report.id,
                new_values={
                    "attempted_action": attempted_action,
                    "failure": "invalid_password",
                },
            )
        )
        audit_session.commit()


def require_password_confirmation(
    session: Session,
    *,
    user: AppUser,
    report: Report,
    attempted_action: str,
    password: Optional[str],
) -> None:
    """Raise unless `password` is the actor's current password.

    `attempted_action` is the audit action the route would write on success
    (``REPORT.SIGN_AND_PUBLISH``, ``REPORT.SIGN``, ``REPORT.REOPEN``); it is
    recorded on a failure so the security trail says what was attempted.
    """
    if not password:
        raise _refuse(
            403,
            REQUIRED_CODE,
            "Confirma la acción con tu contraseña actual.",
        )

    now = datetime.utcnow()
    max_failures = settings.password_confirmation_max_failures
    failures = _recent_failures(session, user.id, now)
    if max_failures > 0 and len(failures) >= max_failures:
        window = timedelta(seconds=settings.password_confirmation_window_seconds)
        retry_after = max(1, int((failures[max_failures - 1] + window - now).total_seconds()) + 1)
        logger.warning(
            "Password confirmation throttled",
            extra={
                "event": "report.password_confirmation_throttled",
                "report_id": str(report.id),
                "user_id": str(user.id),
                "attempted_action": attempted_action,
            },
        )
        raise _refuse(
            429,
            THROTTLED_CODE,
            "Demasiados intentos con contraseña incorrecta. Espera unos minutos "
            "antes de volver a intentarlo.",
            retry_after_seconds=retry_after,
        )

    try:
        verified = (
            len(password) <= MAX_PASSWORD_LENGTH
            and bool(user.hashed_password)
            and verify_password(password, user.hashed_password)
        )
    except (ValueError, TypeError):
        # A malformed stored hash cannot confirm anything. Never log the input.
        verified = False

    if verified:
        return

    _record_failure(session, user=user, report=report, attempted_action=attempted_action)
    logger.warning(
        "Password confirmation failed",
        extra={
            "event": "report.password_confirmation_failed",
            "report_id": str(report.id),
            "user_id": str(user.id),
            "attempted_action": attempted_action,
        },
    )
    raise _refuse(
        403,
        INVALID_CODE,
        "La contraseña no es correcta. No se realizó ningún cambio.",
    )

