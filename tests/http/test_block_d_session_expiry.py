"""Session expiry and token-validity behaviour (Céluma 1.3, Phase 5, Block D — D8).

Closes the second half of C-005. Block C established by reading that
`app/api/v1/auth.py:189` rejects an inactive user at *login*, but nothing
drove an already-issued, now-**expired** JWT to a 401 — the case that actually
matters for a session that ages out while a tab is open.

The expected behaviour is taken from the existing auth contract, not invented
here: `current_user` (`auth.py:116`) decodes with `jose`, and
`ExpiredSignatureError` is a `JWTError` subclass, so an expired token joins
malformed and wrongly-signed tokens on the `401 "Invalid token"` path. A
blacklisted token is `401 "Token has been revoked"`, and an authenticated but
deactivated user is `401 "Inactive user"`.

Nothing here changes token lifetime or authentication architecture; these
tests assert the contract as implemented.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from jose import jwt

from app.core.config import settings
from app.models.user import BlacklistedToken

from .factories import (
    auth_headers,
    create_branch,
    create_patient,
    create_tenant,
    create_user,
)

#: Any authenticated route would do; this one is cheap and tenant-scoped.
PROBE_ROUTE = "/api/v1/tenant/usage"


def _token(sub: str, *, minutes: int, secret: str | None = None) -> str:
    """A structurally valid HS256 JWT whose `exp` is `minutes` from now.
    Negative `minutes` produces an already-expired token."""
    return jwt.encode(
        {"sub": sub, "exp": datetime.utcnow() + timedelta(minutes=minutes)},
        secret if secret is not None else settings.jwt_secret,
        algorithm="HS256",
    )


@pytest.fixture
def tenant_user(session):
    tenant = create_tenant(session, name="Tenant Auth")
    create_branch(session, tenant, code="AUTH")
    user = create_user(session, tenant, email="sesion@example.com")
    return tenant, user


class TestValidSession:
    """The control case: without it, every 401 below could be a false positive."""

    def test_a_valid_token_is_accepted(self, client, tenant_user):
        _, user = tenant_user
        response = client.get(PROBE_ROUTE, headers=auth_headers(user))
        assert response.status_code == 200

    def test_a_token_near_the_end_of_its_life_is_still_accepted(
        self, client, tenant_user
    ):
        _, user = tenant_user
        headers = {"Authorization": f"Bearer {_token(str(user.id), minutes=1)}"}
        assert client.get(PROBE_ROUTE, headers=headers).status_code == 200


class TestExpiredSession:
    """The C-005 gap itself."""

    def test_an_expired_token_is_rejected(self, client, tenant_user):
        _, user = tenant_user
        headers = {"Authorization": f"Bearer {_token(str(user.id), minutes=-1)}"}
        response = client.get(PROBE_ROUTE, headers=headers)
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid token"

    def test_a_long_expired_token_is_rejected(self, client, tenant_user):
        _, user = tenant_user
        headers = {
            "Authorization": f"Bearer {_token(str(user.id), minutes=-60 * 24 * 30)}"
        }
        assert client.get(PROBE_ROUTE, headers=headers).status_code == 401

    def test_an_expired_token_returns_no_tenant_data(self, client, tenant_user):
        tenant, user = tenant_user
        headers = {"Authorization": f"Bearer {_token(str(user.id), minutes=-1)}"}
        response = client.get(PROBE_ROUTE, headers=headers)
        assert str(tenant.id) not in response.text


class TestMalformedAndForgedTokens:
    """Two different rejection points, and the status code says which one.

    A **present but bad** credential reaches `current_user`, which raises
    `401 "Invalid token"`. **Absent or structurally malformed** credentials
    never get that far: the shared `HTTPBearer(auto_error=True)` in
    `auth.py:42` refuses them with 403 first. That split is pre-existing and
    deliberate — `notifications.py:63-75` documents it as the reason that one
    router declares its own `HTTPBearer(auto_error=False)`. Block D asserts
    the contract as implemented; it found no defect here and changed nothing.
    """

    def test_a_malformed_token_is_rejected(self, client):
        response = client.get(
            PROBE_ROUTE, headers={"Authorization": "Bearer not.a.jwt"}
        )
        assert response.status_code == 401

    def test_an_empty_bearer_token_is_rejected(self, client):
        """403, not 401 — an empty credential is a *malformed header*, which
        the shared `HTTPBearer(auto_error=True)` (`auth.py:42`) rejects before
        `current_user` ever runs. See the note on
        `TestNoCredentialsUseTheSharedSchemesContract` below."""
        assert (
            client.get(PROBE_ROUTE, headers={"Authorization": "Bearer "}).status_code
            == 403
        )

    def test_a_token_signed_with_the_wrong_secret_is_rejected(
        self, client, tenant_user
    ):
        _, user = tenant_user
        forged = _token(str(user.id), minutes=60, secret="not-the-real-secret")
        response = client.get(
            PROBE_ROUTE, headers={"Authorization": f"Bearer {forged}"}
        )
        assert response.status_code == 401

    def test_a_missing_authorization_header_is_rejected(self, client):
        """Also 403 — same reason as the empty-credential case above."""
        assert client.get(PROBE_ROUTE).status_code == 403

    def test_an_unsigned_none_algorithm_token_is_rejected(self, client, tenant_user):
        """`alg: none` must never be honoured."""
        _, user = tenant_user
        unsigned = jwt.encode(
            {"sub": str(user.id), "exp": datetime.utcnow() + timedelta(minutes=60)},
            "",
            algorithm="HS256",
        )
        # Re-header it as `none` the way a stripped-signature attack would.
        header, payload, _sig = unsigned.split(".")
        import base64
        import json as _json

        none_header = (
            base64.urlsafe_b64encode(
                _json.dumps({"alg": "none", "typ": "JWT"}).encode()
            )
            .decode()
            .rstrip("=")
        )
        response = client.get(
            PROBE_ROUTE,
            headers={"Authorization": f"Bearer {none_header}.{payload}."},
        )
        assert response.status_code == 401


class TestRevokedAndInactiveSessions:
    def test_a_blacklisted_token_is_rejected(self, client, session, tenant_user):
        _, user = tenant_user
        raw = _token(str(user.id), minutes=60)
        session.add(
            BlacklistedToken(
                token=raw,
                user_id=user.id,
                expires_at=datetime.utcnow() + timedelta(minutes=60),
            )
        )
        session.commit()
        response = client.get(
            PROBE_ROUTE, headers={"Authorization": f"Bearer {raw}"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Token has been revoked"

    def test_a_deactivated_users_existing_token_stops_working(
        self, client, session, tenant_user
    ):
        """Deactivation must take effect on the next request, not at the next
        login — an already-issued token is the whole risk."""
        _, user = tenant_user
        headers = auth_headers(user)
        assert client.get(PROBE_ROUTE, headers=headers).status_code == 200

        user.is_active = False
        session.add(user)
        session.commit()

        response = client.get(PROBE_ROUTE, headers=headers)
        assert response.status_code == 401
        assert response.json()["detail"] == "Inactive user"

    def test_a_token_for_a_deleted_user_is_rejected(self, client, session, tenant_user):
        import uuid as _uuid

        headers = {"Authorization": f"Bearer {_token(str(_uuid.uuid4()), minutes=60)}"}
        response = client.get(PROBE_ROUTE, headers=headers)
        assert response.status_code == 401


class TestCrossTenantTokenCannotReachForeignResources:
    """A valid token is authority over its own tenant only. Supplying a
    foreign resource identifier must not convert it into authority elsewhere —
    the property C-001 restored on the clinical routes."""

    def test_a_foreign_patient_id_in_the_path_is_refused(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A")
        create_branch(session, tenant_a, code="A")
        user_a = create_user(session, tenant_a, email="a@example.com")

        tenant_b = create_tenant(session, name="Tenant B")
        branch_b = create_branch(session, tenant_b, code="B")
        patient_b = create_patient(session, tenant_b, branch_b, patient_code="PB-1")

        response = client.get(
            f"/api/v1/patients/{patient_b.id}", headers=auth_headers(user_a)
        )
        assert response.status_code in (403, 404), (
            "a tenant A token reached tenant B's patient by supplying B's id"
        )
        assert "Sintético" not in response.text

    def test_the_usage_endpoint_reports_only_the_callers_own_tenant(
        self, client, session
    ):
        """`/tenant/usage` takes no tenant parameter at all — it derives the
        tenant from the token, so there is no identifier to substitute."""
        tenant_a = create_tenant(session, name="Tenant A")
        create_branch(session, tenant_a, code="A")
        user_a = create_user(session, tenant_a, email="a@example.com")

        tenant_b = create_tenant(session, name="Tenant B")
        create_branch(session, tenant_b, code="B")

        response = client.get(PROBE_ROUTE, headers=auth_headers(user_a))
        assert response.status_code == 200
        assert str(tenant_b.id) not in response.text
