"""Request-log credential redaction (Céluma 1.3, Phase 5, Block E — E-003).

`log_requests` in `app/main.py` already redacts sensitive *headers* before
logging them — `authorization`, `cookie`, `set-cookie`, `x-api-key` all become
`REDACTED`. The intent is explicit and long-standing: a credential must not
reach the application log.

The request line immediately above that redaction logged `request.url`, which
carries the query string and the full path. Two of this API's credentials
travel exactly there, and neither is a header:

  * `GET /portal/patient/report?code=<ACCESS_CODE>` — the patient access code
    is the *only* thing standing between an anonymous caller and a published
    report: the patient's name, the report title, and a resolvable presigned
    URL for the official PDF. It is a bearer credential in a query parameter.
  * `GET /users/invitations/{token}` and `POST /users/invitations/{token}/accept`
    — the invitation token authorizes creating an account inside a tenant with
    a pre-assigned role.

Both were written verbatim, at INFO, on every request. Application logs are
retained and are readable by a wider audience than the data they describe, so
this turns a log reader into someone who can replay the request and retrieve
another laboratory's patient report.

The fix keeps the log operationally useful — path and non-sensitive query
parameters are preserved, because worklist filters and pagination cursors are
genuinely needed when reading these lines — and redacts only the credential
positions. The redaction list is the contract; these tests are what hold it in
place.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session

from app.models.invitation import UserInvitation

from .factories import create_branch, create_tenant

ACCESS_CODE = "A1B2C3D4E5F60718"
INVITE_TOKEN = "invitation-token-that-must-not-be-logged"


def _request_lines(caplog) -> str:
    """Every INCOMING REQUEST line the middleware emitted, joined."""
    return "\n".join(
        r.getMessage() for r in caplog.records if "INCOMING REQUEST" in r.getMessage()
    )


def test_patient_access_code_is_not_written_to_the_request_log(client, caplog):
    """The credential must not appear, even though the request itself 404s.

    A wrong code is the interesting case: an attacker probing codes generates
    exactly these log lines, and a valid one would be logged the same way.
    """
    with caplog.at_level(logging.INFO):
        client.get("/api/v1/portal/patient/report", params={"code": ACCESS_CODE})

    lines = _request_lines(caplog)
    assert lines, "the request-logging middleware did not run"
    assert ACCESS_CODE not in lines
    assert "code=<redacted>" in lines


def test_the_request_log_still_records_the_path_and_method(client, caplog):
    """Redaction must not cost the log its operational value."""
    with caplog.at_level(logging.INFO):
        client.get("/api/v1/portal/patient/report", params={"code": ACCESS_CODE})

    lines = _request_lines(caplog)
    assert "/api/v1/portal/patient/report" in lines
    assert "GET" in lines


def test_non_sensitive_query_parameters_are_still_logged(client, caplog):
    """Only credential-bearing keys are redacted, not the whole query string."""
    with caplog.at_level(logging.INFO):
        client.get("/api/v1/portal/patient/report", params={"code": ACCESS_CODE, "page": "2"})

    lines = _request_lines(caplog)
    assert "page=2" in lines
    assert ACCESS_CODE not in lines


def test_invitation_token_is_not_written_to_the_request_log(
    client, session: Session, caplog
):
    """The token is a path segment, not a query parameter."""
    tenant = create_tenant(session, name="Invite Tenant")
    create_branch(session, tenant, code="INV")
    session.add(
        UserInvitation(
            tenant_id=tenant.id,
            email="invitee@example.com",
            full_name="Invitee",
            role_code="viewer",
            token=INVITE_TOKEN,
            expires_at=datetime.utcnow() + timedelta(days=3),
        )
    )
    session.commit()

    with caplog.at_level(logging.INFO):
        response = client.get(f"/api/v1/users/invitations/{INVITE_TOKEN}")

    # The endpoint itself must still work — redaction is a logging concern only.
    assert response.status_code == 200, response.text
    lines = _request_lines(caplog)
    assert lines, "the request-logging middleware did not run"
    assert INVITE_TOKEN not in lines
    assert "/users/invitations/<redacted>" in lines


def test_invitation_accept_path_is_redacted_too(client, session: Session, caplog):
    """`POST .../{token}/accept` keeps its trailing segment, loses the token."""
    tenant = create_tenant(session, name="Invite Tenant 2")
    create_branch(session, tenant, code="INV2")
    session.add(
        UserInvitation(
            tenant_id=tenant.id,
            email="invitee2@example.com",
            full_name="Invitee Two",
            role_code="viewer",
            token=INVITE_TOKEN + "-2",
            expires_at=datetime.utcnow() + timedelta(days=3),
        )
    )
    session.commit()

    with caplog.at_level(logging.INFO):
        client.post(
            f"/api/v1/users/invitations/{INVITE_TOKEN}-2/accept",
            json={"username": "invitee2", "password": "irrelevant-in-tests"},
        )

    lines = _request_lines(caplog)
    assert INVITE_TOKEN not in lines
    assert "/users/invitations/<redacted>/accept" in lines


def test_ordinary_paths_are_logged_unchanged(client, caplog):
    """The redaction must not rewrite paths that carry no credential."""
    with caplog.at_level(logging.INFO):
        client.get("/api/v1/patients/")

    lines = _request_lines(caplog)
    assert "/api/v1/patients/" in lines
    assert "<redacted>" not in lines
