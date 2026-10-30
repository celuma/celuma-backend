"""Who may clean a laboratory, and what a valid confirmation is (Céluma 1.4, #39; ADR 0021, E1/E3).

Every denial asserts the database and the object store are byte-for-byte unchanged. Synthetic data only.
"""
from __future__ import annotations

import json
import unicodedata

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models.laboratory_cleanup import LaboratoryCleanupOperation
from app.services import laboratory_cleanup as service
from app.services.password_confirmation import PASSWORD_CONFIRMATION_FAILED_ACTION

from .cleanup_world import Flow, base, build_world, object_store, snapshot
from .factories import TEST_USER_PASSWORD, auth_headers, create_user
from .numbering_support import fresh_client  # noqa: F401


@pytest.fixture
def world(session, fresh_client, monkeypatch, stub_pdf_render):
    return build_world(session, fresh_client, monkeypatch, stub_pdf_render)


def untouched(session, world):
    """Hash of everything a refused request must not change (audit growth is checked separately)."""
    return (
        {t: snapshot(session, t, skip={"audit_log"}) for t in (world.target.tenant.id, world.other.tenant.id)},
        object_store(),
        session.execute(text("SELECT count(*) FROM laboratory_cleanup_operation")).scalar(),
    )


def register(client, suffix: str) -> dict:
    """A laboratory created the way a real one is: through the unauthenticated onboarding route."""
    body = {
        "tenant": {"name": f"Laboratorio Registrado {suffix}"}, "branch": {"code": "MAIN", "name": "Sede"},
        "admin_user": {"email": f"creador-{suffix}@example.com", "password": "Creator!2026", "full_name": "Creador Sintético"},
    }
    r = client.post("/api/v1/auth/register/unified", json=body)
    assert r.status_code == 200, r.text
    login = client.post("/api/v1/auth/login", json={"username_or_email": body["admin_user"]["email"], "password": "Creator!2026"})
    assert login.status_code == 200, login.text
    return {**r.json(), "token": login.json()["access_token"], "headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


class TestCreatorBinding:
    """Owner decision (2026-10-28): the capability belongs to the person who CREATED the laboratory at its initial
    registration, persisted and write-once; no role, custom role, user, invitation or tenant-update path can grant it."""

    def test_onboarding_records_the_creator_atomically(self, session, fresh_client):
        lab = register(fresh_client, "a1")
        session.expire_all()
        creator = session.execute(text("SELECT created_by_user_id::text FROM tenant WHERE id = :t"), {"t": lab["tenant_id"]}).scalar()
        assert creator == lab["user_id"]
        flow = Flow(fresh_client, type("W", (), {"tenant": type("T", (), {"id": lab["tenant_id"], "name": "x"})()})(), headers=lab["headers"])
        assert flow.authorization().json() == {"authorized": True}
        assert flow.preview().status_code == 200

    def test_a_failed_onboarding_leaves_no_tenant_and_therefore_no_dangling_binding(self, session, fresh_client, monkeypatch):
        from app.services.usage_thresholds import UsageThresholdService

        before = session.execute(text("SELECT count(*) FROM tenant")).scalar(), session.execute(text("SELECT count(*) FROM app_user")).scalar()

        def boom(*args, **kwargs):
            raise RuntimeError("onboarding failed after the creator was bound")

        monkeypatch.setattr(UsageThresholdService, "evaluate_users", boom)
        body = {"tenant": {"name": "Laboratorio Fallido"}, "branch": {"code": "MAIN", "name": "Sede"},
                "admin_user": {"email": "fallido@example.com", "password": "Creator!2026", "full_name": "F"}}
        assert fresh_client.post("/api/v1/auth/register/unified", json=body).status_code == 500
        session.expire_all()
        assert (session.execute(text("SELECT count(*) FROM tenant")).scalar(), session.execute(text("SELECT count(*) FROM app_user")).scalar()) == before

    def test_a_laboratory_without_a_recorded_creator_fails_closed_for_everyone(self, session, fresh_client, world):
        """Every laboratory that predates the binding: nobody — not the oldest user, not a current superuser — may clean it."""
        # a legacy row is one the trigger never saw: simulate it by pausing the trigger for this synthetic row only
        session.execute(text("ALTER TABLE tenant DISABLE TRIGGER trg_tenant_creator_write_once"))
        session.execute(text("UPDATE tenant SET created_by_user_id = NULL WHERE id = :t"), {"t": str(world.target.tenant.id)})
        session.execute(text("ALTER TABLE tenant ENABLE TRIGGER trg_tenant_creator_write_once"))
        session.commit()
        flow = Flow(fresh_client, world.target)
        for headers in (world.target.lab.headers, auth_headers(world.target.admin), auth_headers(world.target.reviewer)):
            assert flow.authorization(headers=headers).json() == {"authorized": False}
            assert flow.preview(headers=headers).status_code == 403
        assert flow.start("x" * 20).status_code == 403

    def test_the_binding_is_write_once_for_every_client(self, session, world):
        t = str(world.target.tenant.id)
        stranger = str(world.target.admin.id)
        with pytest.raises(Exception, match="write-once"):
            session.execute(text("UPDATE tenant SET created_by_user_id = :u WHERE id = :t"), {"u": stranger, "t": t})
            session.commit()
        session.rollback()
        with pytest.raises(Exception, match="write-once"):
            session.execute(text("UPDATE tenant SET created_by_user_id = NULL WHERE id = :t"), {"t": t})
            session.commit()
        session.rollback()
        # setting the SAME value is a no-op; the first assignment (NULL -> user) is the only transition allowed
        session.execute(text("UPDATE tenant SET created_by_user_id = created_by_user_id WHERE id = :t"), {"t": t})
        session.commit()
        assert session.execute(text("SELECT created_by_user_id::text FROM tenant WHERE id = :t"), {"t": t}).scalar() == str(world.target.operator.id)

    def test_no_application_path_can_make_another_user_the_creator(self, session, fresh_client, world):
        admin = world.target.admin
        t = str(world.target.tenant.id)
        create_user(session, world.target.tenant, email="admin2@x.invalid", roles=("admin",))   # so admin is not the last one
        h = auth_headers(admin)
        attempts = [
            # tenant update (admin:manage_tenant): an unknown field is not a column
            fresh_client.patch(f"/api/v1/tenants/{t}", headers=h, json={"created_by_user_id": str(admin.id), "name": world.target.tenant.name}),
            # role routes, including the self-escalation the legacy model allows
            fresh_client.put(f"/api/v1/users/{admin.id}", headers=h, json={"role": "superuser"}),
            fresh_client.put(f"/api/v1/rbac/users/{admin.id}/roles", headers=auth_headers(world.target.operator), json={"roles": ["admin", "superuser"]}),
            # user creation and invitations carry a role code, never an identity binding
            fresh_client.post("/api/v1/users/", headers=h, json={
                "email": "nuevo@x.invalid", "password": "Nuevo!2026", "full_name": "Nuevo", "first_name": "N", "last_name": "N",
                "role": "superuser", "created_by_user_id": str(admin.id)}),
            fresh_client.post("/api/v1/users/invitations", headers=h, json={
                "email": "inv@x.invalid", "full_name": "Inv", "role": "superuser", "created_by_user_id": str(admin.id)}),
        ]
        assert any(r.status_code == 200 for r in attempts)   # (the legacy routes do run; they just cannot touch the binding)
        session.expire_all()
        assert session.execute(text("SELECT created_by_user_id::text FROM tenant WHERE id = :t"), {"t": t}).scalar() == str(world.target.operator.id)
        assert Flow(fresh_client, world.target).authorization(headers=h).json() == {"authorized": False}
        assert Flow(fresh_client, world.target).preview(headers=h).status_code == 403

    def test_the_creator_loses_the_capability_only_by_being_deactivated(self, session, fresh_client, world):
        flow = Flow(fresh_client, world.target)
        assert flow.preview().status_code == 200
        # roles are irrelevant in both directions: dropping every role does not remove it...
        session.execute(text("DELETE FROM user_role WHERE user_id = :u"), {"u": str(world.target.operator.id)})
        session.commit()
        assert flow.authorization().json() == {"authorized": True}
        # ...an inactive account does (and an inactive user cannot even authenticate)
        session.execute(text("UPDATE app_user SET is_active = false WHERE id = :u"), {"u": str(world.target.operator.id)})
        session.commit()
        assert flow.authorization().status_code == 401


class TestAuthorizationMatrix:
    def roles(self, session, world):
        out = {
            "admin": world.target.admin, "lab_tech": world.target.tech, "reviewer": world.target.reviewer,
            "pathologist": world.target.author,
        }
        for role in ("assistant", "billing", "viewer", "physician", "superuser"):
            out[role] = create_user(session, world.target.tenant, email=f"{role}@x.invalid", roles=(role,))
        return out

    def test_only_the_creator_of_the_laboratory_passes(self, session, fresh_client, world):
        flow = Flow(fresh_client, world.target)
        assert flow.authorization().json() == {"authorized": True}
        assert flow.preview().status_code == 200
        users = self.roles(session, world)
        before = untouched(session, world)
        for role, user in users.items():   # every other user of the laboratory, `superuser` and `admin` included
            headers = auth_headers(user)
            for response in (flow.preview(headers=headers), flow.start("x" * 20, headers=headers),
                             flow.client.get(f"{flow.base}/operations", headers=headers)):
                assert response.status_code == 403, (role, response.text)
                assert response.json()["detail"]["code"] == "laboratory_cleanup_not_authorized"
            assert flow.authorization(headers=headers).json() == {"authorized": False}, role
        assert untouched(session, world) == before

    def test_a_tenant_admin_cannot_obtain_the_authority_by_escalating_to_superuser(self, session, fresh_client, world):
        """The legacy user routes let an `admin` make themselves `superuser` (OD4, not fixed here). It does not matter:
        the capability is the creator binding, not a role."""
        flow = Flow(fresh_client, world.target)
        admin = world.target.admin
        create_user(session, world.target.tenant, email="admin2@x.invalid", roles=("admin",))  # not the last administrator
        escalate = fresh_client.put(f"/api/v1/users/{admin.id}", headers=auth_headers(admin), json={"role": "superuser"})
        assert escalate.status_code == 200, escalate.text
        assert flow.authorization(headers=auth_headers(admin)).json() == {"authorized": False}
        assert flow.preview(headers=auth_headers(admin)).status_code == 403

    def test_the_path_tenant_is_never_a_source_of_authority(self, session, fresh_client, world):
        target, other = world.target, world.other
        before = untouched(session, world)
        # Each creator is the operator of their OWN laboratory...
        assert Flow(fresh_client, target).preview().status_code == 200
        assert Flow(fresh_client, other).preview().status_code == 200
        # ...and a 404 (revealing nothing) for the other's, for every endpoint.
        cross = Flow(fresh_client, other, headers=target.lab.headers)  # target's creator, other's path
        assert cross.preview().status_code == 404
        assert cross.start("x" * 20).status_code == 404
        assert cross.authorization().status_code == 404
        assert fresh_client.get(f"{cross.base}/operations", headers=target.lab.headers).status_code == 404
        for bad in ("00000000-0000-0000-0000-000000000000", "not-a-uuid"):
            assert fresh_client.post(f"{base(bad)}/preview", json={}, headers=target.lab.headers).status_code == 404
        assert untouched(session, world) == before

    def test_an_operation_id_of_another_tenant_is_not_found(self, session, fresh_client, world):
        op = Flow(fresh_client, world.target).run()
        theirs = Flow(fresh_client, world.other)  # the other laboratory's creator, on their OWN path
        assert fresh_client.get(f"{theirs.base}/operations/{op['id']}", headers=theirs.headers).status_code == 404
        assert theirs.retry(op["id"]).status_code == 404
        assert theirs.cancel(op["id"]).status_code == 404

    def test_unauthenticated_requests_are_rejected(self, fresh_client, world):
        for method, url in (("post", "/preview"), ("post", "/operations"), ("get", "/operations"), ("get", "/authorization")):
            r = getattr(fresh_client, method)(f"{base(world.target.tenant.id)}{url}")
            assert r.status_code in (401, 403), (url, r.status_code)


class TestConfirmation:
    @pytest.fixture
    def flow(self, fresh_client, world):
        return Flow(fresh_client, world.target)

    def token(self, flow):
        return flow.preview().json()["preview_token"]

    def refused(self, session, world, response, status, code):
        assert response.status_code == status, response.text
        detail = response.json()["detail"]
        assert (detail["code"] if isinstance(detail, dict) else detail) == code
        # nothing leaked
        assert TEST_USER_PASSWORD not in response.text and "SENTINEL" not in response.text

    def test_wrong_name_reason_and_password_change_nothing(self, session, world, flow):
        token = self.token(flow)
        before = untouched(session, world)
        self.refused(session, world, flow.start(token, name="Laboratorio Embajador Nandu"), 422, "confirmation_name_mismatch")
        self.refused(session, world, flow.start(token, name="laboratorio embajador ñandú"), 422, "confirmation_name_mismatch")
        self.refused(session, world, flow.start(token, reason="corto"), 422, "reason_invalid")
        self.refused(session, world, flow.start(token, reason="x" * 1001), 422, "reason_invalid")
        self.refused(session, world, flow.start(token, password=None), 403, "password_confirmation_required")
        self.refused(session, world, flow.start(token, password=""), 403, "password_confirmation_required")
        self.refused(session, world, flow.start(token, password="wrong-password-1"), 403, "password_confirmation_invalid")
        assert untouched(session, world) == before
        # The only trace of a wrong password is the credential-free security row.
        rows = session.execute(text("SELECT new_values::text, entity_type FROM audit_log WHERE action = :a"),
                               {"a": PASSWORD_CONFIRMATION_FAILED_ACTION}).all()
        assert len(rows) == 1 and rows[0][1] == "laboratory_cleanup" and "wrong-password-1" not in rows[0][0]

    def test_another_users_password_never_confirms(self, session, fresh_client, world, flow):
        token = self.token(flow)
        before = untouched(session, world)
        self.refused(session, world, flow.start(token, password="another-users-secret"), 403, "password_confirmation_invalid")
        assert untouched(session, world) == before

    def test_repeated_wrong_passwords_are_throttled_without_verifying(self, session, world, flow, monkeypatch):
        monkeypatch.setattr(settings, "password_confirmation_max_failures", 3)
        token = self.token(flow)
        for _ in range(3):
            self.refused(session, world, flow.start(token, password="nope-nope"), 403, "password_confirmation_invalid")
        throttled = flow.start(token, password=TEST_USER_PASSWORD)  # the CORRECT password, but the limiter does not verify
        self.refused(session, world, throttled, 429, "password_confirmation_throttled")
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_operation")).scalar() == 0

    def test_the_name_is_compared_normalized_and_trimmed(self, session, world, flow):
        token = self.token(flow)
        decomposed = unicodedata.normalize("NFD", world.target.tenant.name)
        assert decomposed != world.target.tenant.name
        r = flow.start(token, name=f"  {decomposed}  ")
        assert r.status_code == 202, r.text

    def test_tampered_foreign_and_expired_tokens_are_rejected_server_side(self, session, fresh_client, world, flow, monkeypatch):
        # --- setup: mint every kind of token first, THEN snapshot, so only the refusals fall inside the window.
        token = self.token(flow)
        second = create_user(session, world.target.tenant, email="op2@x.invalid", roles=("superuser",))
        session.execute(text("DELETE FROM sample_image_rendition WHERE kind = 'cross_tenant_probe'"))  # no shared object: the other lab can preview
        session.commit()
        foreign = Flow(fresh_client, world.other).preview().json()["preview_token"]
        assert foreign
        monkeypatch.setattr(settings, "laboratory_cleanup_preview_ttl_seconds", -5)
        expired = self.token(flow)
        monkeypatch.setattr(settings, "laboratory_cleanup_preview_ttl_seconds", 900)
        token = self.token(flow)  # a fresh, valid token for the (now unchanged) scope
        before = untouched(session, world)

        body, sig = token.split(".")
        flipped = body[:-2] + ("A" if body[-2] != "A" else "B") + body[-1]
        for bad in (f"{flipped}.{sig}", f"{body}.{sig[:-2]}AA", "garbage-token-value", body, "." * 12, f"{body}.{sig}x"):
            self.refused(session, world, flow.start(bad), 409, "preview_stale")
        # the token is the creator's alone: any other user is refused before the token is even read
        assert flow.start(token, headers=auth_headers(second)).status_code == 403
        # tenant bound: the other laboratory's token is useless here
        self.refused(session, world, flow.start(foreign), 409, "preview_stale")
        # expiry
        self.refused(session, world, flow.start(expired), 409, "preview_stale")
        assert untouched(session, world) == before

    def test_content_changes_since_the_preview_invalidate_it_without_raising_the_gate(self, session, world, flow):
        token = self.token(flow)
        session.execute(text("UPDATE sample SET notes = 'editada despues de la vista previa' WHERE id = "
                             "(SELECT id FROM sample WHERE tenant_id = :t LIMIT 1)"), {"t": str(world.target.tenant.id)})
        session.commit()
        before = untouched(session, world)
        self.refused(session, world, flow.start(token), 409, "preview_stale")
        assert untouched(session, world) == before
        # no operation row, hence no gate: ordinary writers are unaffected
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_operation")).scalar() == 0
        # a fresh preview works
        assert flow.start(self.token(flow)).status_code == 202

    def test_a_confirmation_is_single_use_and_a_duplicate_replays_the_same_operation(self, session, world, flow):
        token = self.token(flow)
        first = flow.start(token)
        assert first.status_code == 202, first.text
        again = flow.start(token)
        assert again.status_code == 202 and again.headers.get("Idempotent-Replayed") == "true"
        assert again.json()["id"] == first.json()["id"]
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_operation")).scalar() == 1
        # execution happened exactly once
        assert session.execute(text("SELECT count(*) FROM audit_log WHERE action = 'LABORATORY_CLEANUP.COMPLETED'")).scalar() == 1

    def test_a_new_confirmation_while_an_operation_is_open_is_refused(self, session, world, flow, monkeypatch):
        # Leave an operation FAILED (open) by making the object store fail, then try to start another.
        from .conftest import FakeS3Service
        calls = []

        def boom(self, key):
            calls.append(key)
            raise RuntimeError("object store down")

        monkeypatch.setattr(FakeS3Service, "delete_object", boom)
        op = flow.run()
        assert op["status"] == "FAILED" and op["error_code"] == "objects_failed"
        preview = flow.preview().json()
        assert [b["code"] for b in preview["blockers"]] == ["operation_open"] and preview["preview_token"] is None
        assert flow.start("x" * 20).status_code == 409
