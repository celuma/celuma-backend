"""Céluma 1.3, Phase 4, Block G — trigger coverage.

`test_usage_thresholds.py` proves the state machine is right. This module
proves it is actually *reached*: that every production path which can move
storage, seats or limits ends in an evaluation, and that no such path can be
added later without one.

Two kinds of test, and both are needed:

  **End-to-end**, through the real HTTP endpoints, so the wiring is proved
  against the transaction boundary it really runs in rather than against a
  service call in isolation.

  **Structural**, over the source tree, so the inventory in
  `usage-threshold-trigger-matrix.md` cannot silently go stale. A trigger
  matrix that is only a document is a document that is wrong within two
  blocks; the two `TestStorageTriggerCoverage` / `TestLimitMutationInventory`
  assertions are what keep it honest.
"""
from __future__ import annotations

import ast
import io
import pathlib

import pytest
from PIL import Image
from sqlmodel import Session, select

from app.models.notification import Notification, NotificationDelivery, NotificationType
from app.models.tenant_limits import TenantLimits
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_threshold_state import (
    TenantUsageThresholdState,
    UsageResource,
    UsageThresholdState,
)
from app.models.user import AppUser
from app.services.usage import UsageService
from app.services.usage_reconciliation import UsageReconciliationService

from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_sample,
    create_tenant,
    create_user,
    set_email_preference,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


def _jpeg_bytes(size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(200, 50, 50)).save(buf, format="JPEG")
    return buf.getvalue()


#: The four types this block introduces. Every helper below filters to them:
#: the flows under test (a sample image upload, a report write) legitimately
#: produce Phase 3 clinical notifications of their own, and counting those
#: would make "no usage notification was created" accidentally false.
USAGE_THRESHOLD_TYPES = (
    NotificationType.STORAGE_USAGE_APPROACHING.value,
    NotificationType.STORAGE_LIMIT_REACHED.value,
    NotificationType.USER_LIMIT_APPROACHING.value,
    NotificationType.USER_LIMIT_REACHED.value,
)


def _notifications(session: Session, tenant, notification_type=None):
    """Usage-threshold notifications for `tenant`, oldest first."""
    session.expire_all()
    statement = select(Notification).where(
        Notification.tenant_id == tenant.id,
        Notification.type.in_(USAGE_THRESHOLD_TYPES),
    )
    if notification_type is not None:
        statement = statement.where(Notification.type == notification_type.value)
    return list(session.exec(statement.order_by(Notification.created_at)).all())


def _state(session: Session, tenant, resource=UsageResource.STORAGE):
    session.expire_all()
    return session.exec(
        select(TenantUsageThresholdState).where(
            TenantUsageThresholdState.tenant_id == tenant.id,
            TenantUsageThresholdState.resource == resource.value,
        )
    ).first()


def _set_limits(session: Session, tenant, *, storage=None, users=None):
    row = session.get(TenantLimits, tenant.id)
    if row is None:
        row = TenantLimits(tenant_id=tenant.id)
    row.storage_limit_bytes = storage
    row.user_limit = users
    session.add(row)
    session.commit()


# ---------------------------------------------------------------------------
# Structural: the inventory cannot go stale
# ---------------------------------------------------------------------------

class TestStorageTriggerCoverage:
    """Every production storage mutation must go through the
    threshold-aware wrapper.

    `UsageService.record_storage_delta` stays a pure counter mutation — Block
    C's accounting contract §8 commits to that in writing, and it is what
    keeps the atomic `UPDATE` primitive free of a state machine and a
    notification service. The consequence is that the wrapper,
    `record_storage_delta_with_thresholds`, is a *convention* rather than
    something the type system enforces, and a new storage flow could call the
    bare primitive and silently never evaluate.

    This test is the enforcement. It is deliberately a whole-tree scan rather
    than a list of known call sites: a list would have to be updated by the
    same person who forgot the wrapper.
    """

    #: The two modules allowed to name the primitive: the one that defines it,
    #: and the wrapper that is the sanctioned caller.
    ALLOWED = {"app/services/usage.py", "app/services/usage_thresholds.py"}

    def _production_modules(self):
        for path in sorted(APP_ROOT.rglob("*.py")):
            relative = path.relative_to(BACKEND_ROOT).as_posix()
            if "__pycache__" in relative:
                continue
            yield relative, path

    def test_no_production_module_calls_the_bare_primitive(self):
        offenders = []
        for relative, path in self._production_modules():
            if relative in self.ALLOWED:
                continue
            source = path.read_text(encoding="utf-8")
            if "record_storage_delta" not in source:
                continue
            # Comments and docstrings may discuss it; code may not call it.
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "record_storage_delta"
                ):
                    offenders.append(f"{relative}:{node.lineno}")
        assert not offenders, (
            "these production call sites bypass threshold evaluation — use "
            f"record_storage_delta_with_thresholds instead: {offenders}"
        )

    def test_every_wrapper_call_site_is_accounted_for(self):
        """The inventory itself, as a number.

        Thirteen production storage flows, matching
        `usage-threshold-trigger-matrix.md` §Storage. If this count changes,
        the matrix needs a row added or removed — which is the point of
        asserting it.
        """
        call_sites = []
        for relative, path in self._production_modules():
            if relative == "app/services/usage_thresholds.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "record_storage_delta_with_thresholds"
                ):
                    call_sites.append(f"{relative}:{node.lineno}")
        assert len(call_sites) == 13, sorted(call_sites)
        assert {site.split(":")[0] for site in call_sites} == {
            "app/api/v1/laboratory.py",
            "app/api/v1/report_letterheads.py",
            "app/api/v1/reports.py",
            "app/api/v1/tenants.py",
            "app/api/v1/users.py",
            "app/services/report_pdf_generation.py",
            "app/services/report_publishing.py",
        }


class TestUserTriggerCoverage:
    """Every place that can change `active_internal_users` must evaluate.

    The seat count moves on far more than user creation: activation,
    deactivation, a role *replacement* that turns a physician into a reviewer
    (or back), and invitation acceptance all move it, and none of them creates
    or deletes a row that a naive "hook user creation" approach would notice.
    """

    EXPECTED_SOURCES = {
        "user_created",
        "user_updated",
        "user_deactivated",
        "user_activation_toggled",
        "invitation_accepted",
        "user_roles_replaced",
        "user_self_registered",
        "tenant_registration",
    }

    def test_every_user_lifecycle_source_label_is_wired(self):
        found = set()
        for path in sorted(APP_ROOT.rglob("*.py")):
            if "__pycache__" in path.as_posix():
                continue
            source = path.read_text(encoding="utf-8")
            if "evaluate_users" not in source:
                continue
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "evaluate_users"
                ):
                    for keyword in node.keywords:
                        if keyword.arg == "source" and isinstance(
                            keyword.value, ast.Constant
                        ):
                            found.add(keyword.value.value)
        assert found == self.EXPECTED_SOURCES

    def test_no_user_row_is_ever_hard_deleted(self):
        """The matrix says "user deletion: not supported". If that ever
        changes, deletion becomes a seat-freeing operation that needs its own
        evaluation — and this test is where that is noticed."""
        offenders = []
        for path in sorted(APP_ROOT.rglob("*.py")):
            if "__pycache__" in path.as_posix():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "delete"
                    and node.args
                ):
                    continue
                # `session.delete(x)` / `self.session.delete(x)` only — a bare
                # `.delete` match also catches `@router.delete("/{user_id}")`,
                # which is a route decorator, not a row deletion.
                receiver = ast.unparse(node.func.value)
                if receiver not in {"session", "self.session"}:
                    continue
                rendered = ast.unparse(node.args[0])
                if "user" in rendered.lower() and "role" not in rendered.lower():
                    offenders.append(
                        f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}"
                    )
        assert not offenders, offenders


class TestLimitMutationInventory:
    """Céluma 1.3 has no production write path for `TenantLimits`.

    The threshold contract requires that any code making a limit durable
    evaluates afterwards (`UsageThresholdService.evaluate_tenant`). Today
    there is nothing to attach that to — limits are seeded operationally, not
    through the API. That is a fact about the codebase, and a fact stated only
    in prose is a fact that rots, so it is asserted here: the day someone adds
    a limits endpoint, this test fails and points at the hook they need.
    """

    def test_no_production_module_writes_tenant_limits(self):
        offenders = []
        for path in sorted(APP_ROOT.rglob("*.py")):
            relative = path.relative_to(BACKEND_ROOT).as_posix()
            if "__pycache__" in relative:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # `TenantLimits(...)` — constructing a row to persist.
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "TenantLimits"
                ):
                    offenders.append(f"{relative}:{node.lineno}")
                # `something.storage_limit_bytes = ...` / `.user_limit = ...`
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr in {
                            "storage_limit_bytes",
                            "user_limit",
                        }:
                            offenders.append(f"{relative}:{node.lineno}")
        assert not offenders, (
            "a limit is now written in production code — it must be followed by "
            f"UsageThresholdService.evaluate_tenant(...): {offenders}"
        )


class TestNoPeriodicThresholdWorker:
    """No scheduled worker is added for thresholds, and that is a decision.

    Every state-changing path is covered by a trigger (storage mutations, user
    lifecycle, limit changes, reconciliation repair), so a periodic sweep
    would re-derive states nothing had changed — cost with no coverage gain.
    Reconciliation already provides the self-healing pass a worker would have
    been for.
    """

    def test_no_threshold_worker_module_exists(self):
        workers = {
            path.name
            for path in APP_ROOT.rglob("*worker*.py")
            if "__pycache__" not in path.as_posix()
        }
        assert not any("threshold" in name for name in workers), workers


# ---------------------------------------------------------------------------
# End-to-end: storage
# ---------------------------------------------------------------------------

class TestStorageFlowEndToEnd:
    def test_a_sample_image_upload_that_crosses_80_percent_notifies(
        self, client, session
    ):
        """The whole chain, through the real endpoint: HTTP upload -> S3 ->
        `StorageObject` -> counter -> threshold evaluation -> notification,
        all in one committed transaction."""
        tenant = create_tenant(session, name="Lab")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        image = _jpeg_bytes()
        # Sit just under the boundary, then let one upload carry it over.
        limit = 100_000
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=79_990, source="test"
        )
        _set_limits(session, tenant, storage=limit)

        response = client.post(
            f"/api/v1/laboratory/samples/{sample.id}/images",
            files={"file": ("a.jpg", image, "image/jpeg")},
            headers=auth_headers(admin),
        )
        assert response.status_code == 200, response.text

        created = _notifications(session, tenant)
        assert len(created) == 1
        assert created[0].type == NotificationType.STORAGE_USAGE_APPROACHING.value
        assert _state(session, tenant).state == UsageThresholdState.APPROACHING

    def test_a_second_upload_inside_the_same_band_notifies_nothing(
        self, client, session
    ):
        tenant = create_tenant(session, name="Lab")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=79_990, source="test"
        )
        _set_limits(session, tenant, storage=100_000)

        for _ in range(3):
            response = client.post(
                f"/api/v1/laboratory/samples/{sample.id}/images",
                files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
                headers=auth_headers(admin),
            )
            assert response.status_code == 200, response.text

        assert len(_notifications(session, tenant)) == 1

    def test_an_unlimited_tenant_uploading_produces_nothing(self, client, session):
        """The local-database safety property, as a test: with no
        `TenantLimits` row — which is every tenant in the development database
        — a storage write creates no notification and not even a state row."""
        tenant = create_tenant(session, name="Lab")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=0, source="test"
        )

        response = client.post(
            f"/api/v1/laboratory/samples/{sample.id}/images",
            files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
            headers=auth_headers(admin),
        )
        assert response.status_code == 200, response.text

        assert _notifications(session, tenant) == []
        assert _state(session, tenant) is None

    def test_deleting_an_image_re_arms_the_threshold(self, client, session):
        tenant = create_tenant(session, name="Lab")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=79_990, source="test"
        )
        _set_limits(session, tenant, storage=100_000)

        upload = client.post(
            f"/api/v1/laboratory/samples/{sample.id}/images",
            files={"file": ("a.jpg", _jpeg_bytes((400, 400)), "image/jpeg")},
            headers=auth_headers(admin),
        )
        assert upload.status_code == 200, upload.text
        image_id = upload.json()["sample_image_id"]
        assert _state(session, tenant).state == UsageThresholdState.APPROACHING

        delete = client.delete(
            f"/api/v1/laboratory/samples/{sample.id}/images/{image_id}",
            headers=auth_headers(admin),
        )
        assert delete.status_code in (200, 204), delete.text

        assert _state(session, tenant).state == UsageThresholdState.NORMAL
        # Downward moves never notify.
        assert len(_notifications(session, tenant)) == 1


# ---------------------------------------------------------------------------
# End-to-end: users
# ---------------------------------------------------------------------------

class TestUserFlowsEndToEnd:
    @pytest.fixture(name="lab")
    def lab_fixture(self, session):
        tenant = create_tenant(session, name="Seat Lab")
        create_branch(session, tenant)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        return tenant, admin

    def test_creating_an_internal_user_crosses_the_seat_limit(
        self, client, session, lab
    ):
        tenant, admin = lab
        _set_limits(session, tenant, users=2)  # admin = 1 of 2

        response = client.post(
            "/api/v1/users/",
            json={
                "email": "tech@lab.test",
                "password": "Sup3r-secret!",
                "first_name": "Tech",
                "last_name": "One",
                "role": "lab_tech",
            },
            headers=auth_headers(admin),
        )
        assert response.status_code == 200, response.text

        created = _notifications(session, tenant)
        assert len(created) == 1
        assert created[0].type == NotificationType.USER_LIMIT_REACHED.value

    def test_creating_a_physician_only_user_does_not(self, client, session, lab):
        tenant, admin = lab
        _set_limits(session, tenant, users=2)

        response = client.post(
            "/api/v1/users/",
            json={
                "email": "doc@lab.test",
                "password": "Sup3r-secret!",
                "first_name": "Doc",
                "last_name": "Tor",
                "role": "physician",
            },
            headers=auth_headers(admin),
        )
        assert response.status_code == 200, response.text
        assert _notifications(session, tenant) == []

    def test_deactivating_then_reactivating_re_arms_and_notifies_again(
        self, client, session, lab
    ):
        tenant, admin = lab
        tech = create_user(session, tenant, email="tech@lab.test", roles=("lab_tech",))
        _set_limits(session, tenant, users=2)  # admin + tech = 100%

        # Establish REACHED.
        client.post(
            f"/api/v1/users/{tech.id}/toggle-active", headers=auth_headers(admin)
        )
        # …that first call actually deactivates, so evaluate from a clean base:
        # after it the tenant is at 50%.
        assert _state(session, tenant, UsageResource.USERS).state == (
            UsageThresholdState.NORMAL
        )
        assert _notifications(session, tenant) == []

        reactivate = client.post(
            f"/api/v1/users/{tech.id}/toggle-active", headers=auth_headers(admin)
        )
        assert reactivate.status_code == 200, reactivate.text
        assert len(_notifications(session, tenant)) == 1

        client.post(
            f"/api/v1/users/{tech.id}/toggle-active", headers=auth_headers(admin)
        )
        assert _state(session, tenant, UsageResource.USERS).state == (
            UsageThresholdState.NORMAL
        )
        client.post(
            f"/api/v1/users/{tech.id}/toggle-active", headers=auth_headers(admin)
        )
        assert len(_notifications(session, tenant)) == 2

    def test_the_delete_endpoint_deactivates_and_frees_a_seat(
        self, client, session, lab
    ):
        tenant, admin = lab
        tech = create_user(session, tenant, email="tech@lab.test", roles=("lab_tech",))
        _set_limits(session, tenant, users=2)
        from app.services.usage_thresholds import UsageThresholdService

        UsageThresholdService.evaluate_users(session, tenant.id, source="test")
        session.commit()
        assert len(_notifications(session, tenant)) == 1

        response = client.delete(
            f"/api/v1/users/{tech.id}", headers=auth_headers(admin)
        )
        assert response.status_code == 200, response.text

        assert _state(session, tenant, UsageResource.USERS).state == (
            UsageThresholdState.NORMAL
        )
        assert len(_notifications(session, tenant)) == 1

    def test_a_role_replacement_from_physician_to_internal_consumes_a_seat(
        self, client, session, lab
    ):
        """The trigger nothing else would catch: no user is created,
        activated or deactivated — only their roles change — and the seat
        count moves anyway."""
        tenant, admin = lab
        doc = create_user(session, tenant, email="doc@lab.test", roles=("physician",))
        _set_limits(session, tenant, users=2)
        from app.services.usage_thresholds import UsageThresholdService

        UsageThresholdService.evaluate_users(session, tenant.id, source="test")
        session.commit()
        assert _notifications(session, tenant) == []  # 1 of 2 = 50%

        response = client.put(
            f"/api/v1/rbac/users/{doc.id}/roles",
            json={"roles": ["reviewer"]},
            headers=auth_headers(admin),
        )
        assert response.status_code == 200, response.text

        created = _notifications(session, tenant)
        assert len(created) == 1
        assert created[0].type == NotificationType.USER_LIMIT_REACHED.value

    def test_a_role_replacement_back_to_physician_frees_the_seat(
        self, client, session, lab
    ):
        tenant, admin = lab
        reviewer = create_user(
            session, tenant, email="rev@lab.test", roles=("reviewer",)
        )
        _set_limits(session, tenant, users=2)
        from app.services.usage_thresholds import UsageThresholdService

        UsageThresholdService.evaluate_users(session, tenant.id, source="test")
        session.commit()
        assert len(_notifications(session, tenant)) == 1

        response = client.put(
            f"/api/v1/rbac/users/{reviewer.id}/roles",
            json={"roles": ["physician"]},
            headers=auth_headers(admin),
        )
        assert response.status_code == 200, response.text

        assert _state(session, tenant, UsageResource.USERS).state == (
            UsageThresholdState.NORMAL
        )
        assert len(_notifications(session, tenant)) == 1

    def test_updating_a_user_to_inactive_frees_a_seat(self, client, session, lab):
        tenant, admin = lab
        tech = create_user(session, tenant, email="tech@lab.test", roles=("lab_tech",))
        _set_limits(session, tenant, users=2)
        from app.services.usage_thresholds import UsageThresholdService

        UsageThresholdService.evaluate_users(session, tenant.id, source="test")
        session.commit()

        response = client.put(
            f"/api/v1/users/{tech.id}",
            json={"is_active": False},
            headers=auth_headers(admin),
        )
        assert response.status_code == 200, response.text

        assert _state(session, tenant, UsageResource.USERS).state == (
            UsageThresholdState.NORMAL
        )

    def test_accepting_an_invitation_consumes_a_seat(self, client, session, lab):
        """Creating the invitation must change nothing — a pending
        `UserInvitation` is not an `AppUser` and is structurally absent from
        every user metric. Acceptance is the trigger point."""
        tenant, admin = lab
        _set_limits(session, tenant, users=2)

        invite = client.post(
            "/api/v1/users/invitations",
            json={
                "email": "invited@lab.test",
                "full_name": "Invited Person",
                "role": "lab_tech",
            },
            headers=auth_headers(admin),
        )
        assert invite.status_code == 200, invite.text
        assert _notifications(session, tenant) == [], "an invitation is not a seat"

        from app.models.invitation import UserInvitation

        token = session.exec(
            select(UserInvitation.token).where(
                UserInvitation.email == "invited@lab.test"
            )
        ).first()
        token = token[0] if isinstance(token, (tuple, list)) else token

        accept = client.post(
            f"/api/v1/users/invitations/{token}/accept",
            json={"password": "Sup3r-secret!", "username": "invited"},
        )
        assert accept.status_code == 200, accept.text

        created = _notifications(session, tenant)
        assert len(created) == 1
        assert created[0].type == NotificationType.USER_LIMIT_REACHED.value
        # No actor: acceptance is an unauthenticated, token-bearing request.
        assert created[0].created_by is None


# ---------------------------------------------------------------------------
# End-to-end: reconciliation
# ---------------------------------------------------------------------------

class TestReconciliationIntegration:
    """Reconciliation moves the counter in bulk, in both directions, with no
    user request behind it. It is the only trigger that can repair a *stale*
    threshold state, which makes it the block's self-healing path."""

    def _reconcile(self, session, tenant):
        outcome = UsageReconciliationService().reconcile_tenant(
            session, tenant.id, repair=True, verify_s3=False
        )
        session.expire_all()
        return outcome

    def test_a_repair_upward_transitions_and_notifies(self, client, session):
        """An under-counted tenant whose real usage is 90%: the counter says
        NORMAL, reconciliation repairs it, and the crossing is announced."""
        tenant = create_tenant(session, name="Drift Lab")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=0, source="test"
        )

        upload = client.post(
            f"/api/v1/laboratory/samples/{sample.id}/images",
            files={"file": ("a.jpg", _jpeg_bytes((300, 300)), "image/jpeg")},
            headers=auth_headers(admin),
        )
        assert upload.status_code == 200, upload.text

        session.expire_all()
        real = session.get(TenantUsage, tenant.id).billable_storage_bytes
        assert real > 0
        # Break the counter (under-count) and set a limit the real value
        # crosses but the broken one does not.
        limit = int(real / 0.9)
        usage = session.get(TenantUsage, tenant.id)
        usage.billable_storage_bytes = 1
        session.add(usage)
        _set_limits(session, tenant, storage=limit)

        from app.services.usage_thresholds import UsageThresholdService

        UsageThresholdService.evaluate_storage(session, tenant.id, source="test")
        session.commit()
        assert _state(session, tenant).state == UsageThresholdState.NORMAL
        assert _notifications(session, tenant) == []

        self._reconcile(session, tenant)

        assert session.get(TenantUsage, tenant.id).billable_storage_bytes == real
        assert _state(session, tenant).state == UsageThresholdState.APPROACHING
        created = _notifications(session, tenant)
        assert len(created) == 1
        assert created[0].type == NotificationType.STORAGE_USAGE_APPROACHING.value

    def test_a_repair_downward_re_arms_without_notifying(self, client, session):
        """An over-counted tenant sitting at REACHED. Reconciliation brings the
        counter back to reality; the state must follow it down silently, and
        the *next* genuine crossing must notify again."""
        tenant = create_tenant(session, name="Over Lab")
        create_branch(session, tenant)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=15_000, source="test"
        )
        _set_limits(session, tenant, storage=10_000)

        from app.services.usage_thresholds import UsageThresholdService

        UsageThresholdService.evaluate_storage(session, tenant.id, source="test")
        session.commit()
        assert _state(session, tenant).state == UsageThresholdState.REACHED
        assert len(_notifications(session, tenant)) == 1

        # The tenant owns no storage objects at all, so the authoritative
        # recomputation is 0 — a 100% over-count.
        self._reconcile(session, tenant)

        assert session.get(TenantUsage, tenant.id).billable_storage_bytes == 0
        assert _state(session, tenant).state == UsageThresholdState.NORMAL
        assert len(_notifications(session, tenant)), "no downward notification"
        assert len(_notifications(session, tenant)) == 1

        # Re-armed: crossing again notifies again.
        usage = session.get(TenantUsage, tenant.id)
        usage.billable_storage_bytes = 12_000
        session.add(usage)
        UsageThresholdService.evaluate_storage(session, tenant.id, source="test")
        session.commit()
        assert len(_notifications(session, tenant)) == 2

    def test_missing_usage_recovery_evaluates_the_recovered_baseline(
        self, client, session
    ):
        """Reconciliation initializes a missing `TenantUsage` row from the
        authoritative baseline. That baseline may already be above a
        threshold, and the first thing anyone knows about it is this."""
        tenant = create_tenant(session, name="Recovery Lab")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=0, source="test"
        )
        upload = client.post(
            f"/api/v1/laboratory/samples/{sample.id}/images",
            files={"file": ("a.jpg", _jpeg_bytes((300, 300)), "image/jpeg")},
            headers=auth_headers(admin),
        )
        assert upload.status_code == 200, upload.text

        session.expire_all()
        real = session.get(TenantUsage, tenant.id).billable_storage_bytes
        _set_limits(session, tenant, storage=max(1, int(real / 1.2)))

        # Lose the counter entirely.
        session.delete(session.get(TenantUsage, tenant.id))
        session.commit()
        assert _notifications(session, tenant) == []

        self._reconcile(session, tenant)

        assert session.get(TenantUsage, tenant.id).billable_storage_bytes == real
        created = _notifications(session, tenant)
        assert len(created) == 1
        assert created[0].type == NotificationType.STORAGE_LIMIT_REACHED.value

    def test_reconciliation_on_an_unlimited_tenant_notifies_nothing(
        self, client, session
    ):
        tenant = create_tenant(session, name="Unlimited Lab")
        create_branch(session, tenant)
        create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=999_999, source="test"
        )

        self._reconcile(session, tenant)

        assert _notifications(session, tenant) == []
        assert _state(session, tenant) is None

    def test_a_reconciliation_run_still_succeeds_if_evaluation_fails(
        self, client, session, monkeypatch
    ):
        """The counter repair is the part that matters. A threshold failure
        after it must not turn a healthy run into a FAILED one."""
        tenant = create_tenant(session, name="Contained Lab")
        create_branch(session, tenant)
        create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=15_000, source="test"
        )
        _set_limits(session, tenant, storage=10_000)

        def boom(*args, **kwargs):
            raise RuntimeError("simulated threshold failure")

        monkeypatch.setattr(
            "app.services.usage_reconciliation.UsageThresholdService"
            ".evaluate_storage",
            boom,
        )

        outcome = self._reconcile(session, tenant)

        assert outcome.status == "SUCCEEDED"
        assert session.get(TenantUsage, tenant.id).billable_storage_bytes == 0
        assert _notifications(session, tenant) == []


# ---------------------------------------------------------------------------
# Preferences and delivery
# ---------------------------------------------------------------------------

class TestPreferencesAndDelivery:
    """The four types plug into the existing Phase 3 preference and delivery
    machinery. Nothing here re-implements either — these tests assert the
    plug, not the socket."""

    @pytest.fixture(name="over_limit")
    def over_limit_fixture(self, session):
        tenant = create_tenant(session, name="Pref Lab")
        create_branch(session, tenant)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=15_000, source="test"
        )
        _set_limits(session, tenant, storage=10_000)
        return tenant, admin

    def _cross(self, session, tenant):
        from app.services.usage_thresholds import UsageThresholdService

        UsageThresholdService.evaluate_storage(session, tenant.id, source="test")
        session.commit()

    def test_the_preferences_api_lists_all_four_new_types(self, client, session):
        tenant = create_tenant(session, name="Pref Lab")
        create_branch(session, tenant)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))

        response = client.get(
            "/api/v1/notification-preferences", headers=auth_headers(admin)
        )
        assert response.status_code == 200, response.text
        by_type = {
            item["notification_type"]: item
            for item in response.json()["preferences"]
        }
        for notification_type in (
            "STORAGE_USAGE_APPROACHING",
            "STORAGE_LIMIT_REACHED",
            "USER_LIMIT_APPROACHING",
            "USER_LIMIT_REACHED",
        ):
            assert notification_type in by_type
            item = by_type[notification_type]
            assert item["email_supported"] is True
            assert item["email_enabled"] is True
            assert item["in_app_enabled"] is True
            assert item["is_explicit"] is False

    def test_each_type_is_independently_configurable(self, client, session):
        tenant = create_tenant(session, name="Pref Lab")
        create_branch(session, tenant)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))

        response = client.put(
            "/api/v1/notification-preferences",
            json={
                "preferences": [
                    {
                        "notification_type": "STORAGE_LIMIT_REACHED",
                        "email_enabled": False,
                    }
                ]
            },
            headers=auth_headers(admin),
        )
        assert response.status_code == 200, response.text
        by_type = {
            item["notification_type"]: item
            for item in response.json()["preferences"]
        }
        assert by_type["STORAGE_LIMIT_REACHED"]["email_enabled"] is False
        # Its three siblings are untouched — the batch is partial by contract.
        for sibling in (
            "STORAGE_USAGE_APPROACHING",
            "USER_LIMIT_APPROACHING",
            "USER_LIMIT_REACHED",
        ):
            assert by_type[sibling]["email_enabled"] is True

    def test_the_default_produces_a_pending_email_delivery(self, session, over_limit):
        tenant, admin = over_limit
        self._cross(session, tenant)

        notification = _notifications(session, tenant)[0]
        deliveries = list(
            session.exec(
                select(NotificationDelivery).where(
                    NotificationDelivery.notification_id == notification.id
                )
            ).all()
        )
        assert len(deliveries) == 1
        assert deliveries[0].recipient_user_id == admin.id
        assert deliveries[0].status == "PENDING"

    def test_disabling_email_suppresses_the_delivery_but_not_the_inbox_row(
        self, session, over_limit
    ):
        tenant, admin = over_limit
        set_email_preference(
            session,
            tenant,
            admin,
            NotificationType.STORAGE_LIMIT_REACHED,
            enabled=False,
        )
        self._cross(session, tenant)

        notification = _notifications(session, tenant)[0]
        deliveries = list(
            session.exec(
                select(NotificationDelivery).where(
                    NotificationDelivery.notification_id == notification.id
                )
            ).all()
        )
        assert deliveries == []
        # In-app is the durable operational channel and is never switchable.
        from app.models.notification import NotificationRecipient

        inbox = list(
            session.exec(
                select(NotificationRecipient).where(
                    NotificationRecipient.notification_id == notification.id
                )
            ).all()
        )
        assert [row.user_id for row in inbox] == [admin.id]

    def test_the_email_template_registry_can_render_all_four(self):
        """A delivery row whose key the email registry cannot resolve is a row
        the worker can only fail. Rendering is exercised here rather than left
        to the first production send."""
        from app.services.email_templates import render_notification_email
        from app.services.notification_templates import CURRENT_TEMPLATE_KEY

        for notification_type, params in (
            (NotificationType.STORAGE_USAGE_APPROACHING, {"usage_percent": 82}),
            (NotificationType.STORAGE_LIMIT_REACHED, {}),
            (NotificationType.USER_LIMIT_APPROACHING, {"usage_percent": 80}),
            (NotificationType.USER_LIMIT_REACHED, {}),
        ):
            rendered = render_notification_email(
                tenant_name="Laboratorio Céluma",
                notification_type=notification_type,
                template_key=CURRENT_TEMPLATE_KEY[notification_type],
                template_params=params,
            )
            assert rendered.subject and rendered.text_body and rendered.html_body
            assert "Laboratorio Céluma" in rendered.subject
            body = f"{rendered.subject} {rendered.text_body}".lower()
            for forbidden in ("plan", "upgrade", "bloque", "suspend", "aws", "s3"):
                assert forbidden not in body, (notification_type, forbidden)


# ---------------------------------------------------------------------------
# Block F stays notification-free
# ---------------------------------------------------------------------------

class TestBlockFRemainsNotificationFree:
    def test_reading_the_usage_dashboard_endpoint_creates_nothing(
        self, client, session
    ):
        """Block F's own guarantee, asserted from the backend side: rendering
        — or in this case *reading* — a tenant far above its limit is not a
        notifiable event. Only a transition is, and a read is not a
        transition.

        Ten polls of the endpoint a dashboard hits every seven seconds during
        a reconciliation must produce zero notifications and zero state.
        """
        tenant = create_tenant(session, name="Dashboard Lab")
        create_branch(session, tenant)
        admin = create_user(session, tenant, email="admin@lab.test", roles=("admin",))
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=95_000, source="test"
        )
        _set_limits(session, tenant, storage=100_000)  # 95%

        for _ in range(10):
            response = client.get(
                "/api/v1/tenant/usage", headers=auth_headers(admin)
            )
            assert response.status_code == 200, response.text
            assert response.json()["storage"]["usage_percent"] == 95.0

        assert _notifications(session, tenant) == []
        assert _state(session, tenant) is None
