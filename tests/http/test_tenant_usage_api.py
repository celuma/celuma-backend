"""Tenant usage read API tests (Céluma 1.3, Phase 4, Block E).

Covers usage-api-contract.md, usage-response-semantics.md,
usage-rbac-contract.md and reconciliation-read-contract.md:
`GET /api/v1/tenant/usage`.

The load-bearing cases are the ones where a wrong answer is *plausible*:
an uninitialized tenant rendered as "0 bytes used", an unlimited tenant
rendered as "0% of nothing", a NULL integrity counter rendered as
"verified, none found", an over-limit tenant clamped to 100%, and a
dashboard read that quietly recomputes the authoritative billable total or
reaches S3. Each has its own test.

Runs against real PostgreSQL (the ephemeral per-test database
`tests/http/conftest.py` builds through the real migration chain). No test
here reaches AWS: the read path must not construct an S3 client at all,
which `TestReadPathCost` asserts by making construction raise.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event
from sqlmodel import select

from app.models.invitation import UserInvitation
from app.models.storage import StorageObject
from app.models.tenant_limits import TenantLimits
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_reconciliation import (
    TenantUsageReconciliation,
    TenantUsageReconciliationStatus,
)
from app.services.usage import UsageService
from app.services.usage_reconciliation import UsageReconciliationService
from tests.http.conftest import FakeS3Service
from tests.http.factories import auth_headers, create_tenant, create_user

USAGE_URL = "/api/v1/tenant/usage"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _admin(session, tenant, email=None):
    return create_user(
        session,
        tenant,
        email=email or f"admin-{uuid.uuid4().hex[:8]}@t.example",
        roles=("admin",),
    )


def _get(client, user):
    return client.get(USAGE_URL, headers=auth_headers(user))


def _init_usage(session, tenant_id, *, billable_storage_bytes=0):
    UsageService.initialize_usage(
        session, tenant_id, billable_storage_bytes=billable_storage_bytes
    )
    session.commit()


def _set_limits(session, tenant_id, *, storage_limit_bytes=None, user_limit=None):
    session.add(
        TenantLimits(
            tenant_id=tenant_id,
            storage_limit_bytes=storage_limit_bytes,
            user_limit=user_limit,
        )
    )
    session.commit()


def _seed_run(session, tenant_id, **fields):
    """A reconciliation history row, written directly.

    Read serialization is what these tests are about; driving a real run for
    every case would test the Block D engine again (it has its own suite)
    and would make the S3-verification-disabled and RUNNING states awkward
    to produce."""
    fields.setdefault("status", TenantUsageReconciliationStatus.SUCCEEDED)
    fields.setdefault("started_at", datetime.utcnow())
    if fields["status"] != TenantUsageReconciliationStatus.RUNNING:
        fields.setdefault("completed_at", datetime.utcnow())
    run = TenantUsageReconciliation(tenant_id=tenant_id, **fields)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


class TestStorageContract:
    def test_initialized_usage_and_a_configured_limit(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=500)
        _set_limits(session, tenant.id, storage_limit_bytes=1000)

        storage = _get(client, admin).json()["storage"]

        assert storage["initialized"] is True
        assert storage["billable_bytes"] == 500
        assert storage["limit_bytes"] == 1000
        assert storage["unlimited"] is False
        assert storage["usage_ratio"] == 0.5
        assert storage["usage_percent"] == 50.0

    def test_over_limit_usage_is_reported_not_clamped(self, client, session):
        """Phase 4 observes over-limit states rather than hiding them, and
        nothing enforces a limit anywhere."""
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=1200)
        _set_limits(session, tenant.id, storage_limit_bytes=1000)

        storage = _get(client, admin).json()["storage"]

        assert storage["usage_ratio"] == pytest.approx(1.2)
        assert storage["usage_percent"] == 120.0

    def test_no_limits_row_means_unlimited_with_no_ratio(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=777)

        storage = _get(client, admin).json()["storage"]

        assert storage["unlimited"] is True
        assert storage["limit_bytes"] is None
        assert storage["usage_ratio"] is None
        assert storage["usage_percent"] is None
        assert storage["billable_bytes"] == 777, "usage is still reported"

    def test_a_limits_row_with_a_null_storage_limit_is_also_unlimited(
        self, client, session
    ):
        """An absent row and an all-NULL row mean the same thing (Block B's
        contract) — the API must not distinguish them."""
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=10)
        _set_limits(session, tenant.id, storage_limit_bytes=None, user_limit=5)

        storage = _get(client, admin).json()["storage"]

        assert storage["unlimited"] is True
        assert storage["limit_bytes"] is None
        assert storage["usage_percent"] is None

    def test_uninitialized_usage_is_never_reported_as_zero(self, client, session):
        """The Block B invariant this whole endpoint could most easily
        violate: no `TenantUsage` row means "not initialized", not "0 bytes
        used"."""
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        assert session.get(TenantUsage, tenant.id) is None

        storage = _get(client, admin).json()["storage"]

        assert storage["initialized"] is False
        assert storage["billable_bytes"] is None
        assert storage["billable_bytes"] != 0

    def test_uninitialized_usage_with_a_limit_has_no_ratio(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _set_limits(session, tenant.id, storage_limit_bytes=1000)

        storage = _get(client, admin).json()["storage"]

        assert storage["initialized"] is False
        assert storage["limit_bytes"] == 1000, "the ceiling is still known"
        assert storage["unlimited"] is False
        assert storage["usage_ratio"] is None, "there is no numerator to divide"
        assert storage["usage_percent"] is None

    def test_initialized_at_zero_is_distinguishable_from_uninitialized(
        self, client, session
    ):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=0)
        _set_limits(session, tenant.id, storage_limit_bytes=1000)

        storage = _get(client, admin).json()["storage"]

        assert storage["initialized"] is True
        assert storage["billable_bytes"] == 0
        assert storage["usage_percent"] == 0.0


class TestUserContract:
    def _viewers(self, session, tenant, count):
        for i in range(count):
            create_user(
                session, tenant, email=f"viewer{i}-{tenant.id}@t.example",
                roles=("viewer",),
            )

    def test_active_internal_users_against_the_seat_limit(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)          # 1 internal seat
        self._viewers(session, tenant, 7)        # + 7 = 8
        _set_limits(session, tenant.id, user_limit=10)

        users = _get(client, admin).json()["users"]

        assert users["active_internal_users"] == 8
        assert users["user_limit"] == 10
        assert users["unlimited"] is False
        assert users["usage_ratio"] == pytest.approx(0.8)
        assert users["usage_percent"] == 80.0

    def test_over_seat_limit_is_reported_not_clamped(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        self._viewers(session, tenant, 11)       # 12 internal seats
        _set_limits(session, tenant.id, user_limit=10)

        users = _get(client, admin).json()["users"]

        assert users["active_internal_users"] == 12
        assert users["usage_ratio"] == pytest.approx(1.2)
        assert users["usage_percent"] == 120.0

    def test_physician_portal_users_are_separate_and_consume_no_seat(
        self, client, session
    ):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        create_user(session, tenant, email="doc1@t.example", roles=("physician",))
        create_user(session, tenant, email="doc2@t.example", roles=("physician",))
        _set_limits(session, tenant.id, user_limit=10)

        users = _get(client, admin).json()["users"]

        assert users["active_physician_portal_users"] == 2
        assert users["active_internal_users"] == 1, "physicians are not seats"
        assert users["usage_percent"] == 10.0
        assert users["registered_users"] == 3

    def test_a_pending_invitation_does_not_consume_the_metric(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        session.add(
            UserInvitation(
                tenant_id=tenant.id,
                email="invited@t.example",
                full_name="Invited Person",
                role_code="viewer",
                token=f"tok-{uuid.uuid4().hex}",
                expires_at=datetime.utcnow() + timedelta(days=7),
            )
        )
        session.commit()
        _set_limits(session, tenant.id, user_limit=10)

        users = _get(client, admin).json()["users"]

        assert users["registered_users"] == 1
        assert users["active_internal_users"] == 1
        assert users["usage_percent"] == 10.0

    def test_no_user_limit_means_unlimited(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _set_limits(session, tenant.id, storage_limit_bytes=1000)

        users = _get(client, admin).json()["users"]

        assert users["user_limit"] is None
        assert users["unlimited"] is True
        assert users["usage_ratio"] is None
        assert users["usage_percent"] is None
        assert users["active_internal_users"] == 1, "the count is still reported"

    def test_inactive_users_count_as_registered_but_not_as_seats(
        self, client, session
    ):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        inactive = create_user(
            session, tenant, email="gone@t.example", roles=("viewer",)
        )
        inactive.is_active = False
        session.add(inactive)
        session.commit()
        _set_limits(session, tenant.id, user_limit=10)

        users = _get(client, admin).json()["users"]

        assert users["registered_users"] == 2
        assert users["active_internal_users"] == 1
        assert users["usage_percent"] == 10.0


class TestReconciliationRead:
    def test_never_reconciled_is_not_a_clean_bill_of_health(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=1)

        rec = _get(client, admin).json()["reconciliation"]

        assert rec["has_run"] is False
        assert rec["integrity_status"] == "NOT_RUN"
        assert rec["status"] is None
        assert rec["started_at"] is None
        assert rec["completed_at"] is None
        assert rec["actual_storage_bytes"] is None
        assert rec["objects_checked"] is None
        assert rec["orphans_found"] is None
        assert rec["error_code"] is None

    def test_a_running_run_is_reported_as_running(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _seed_run(session, tenant.id, status=TenantUsageReconciliationStatus.RUNNING)

        rec = _get(client, admin).json()["reconciliation"]

        assert rec["has_run"] is True
        assert rec["status"] == "RUNNING"
        assert rec["integrity_status"] == "RUNNING"
        assert rec["completed_at"] is None
        assert rec["orphans_found"] is None, "not measured yet — never 0"

    def test_a_clean_succeeded_run_is_healthy(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _seed_run(
            session,
            tenant.id,
            expected_storage_bytes=1000,
            actual_storage_bytes=1000,
            difference_bytes=0,
            repaired=False,
            objects_checked=142,
            orphans_found=0,
            missing_objects_found=0,
            metadata_mismatches_found=0,
        )

        rec = _get(client, admin).json()["reconciliation"]

        assert rec["status"] == "SUCCEEDED"
        assert rec["integrity_status"] == "HEALTHY"
        assert rec["expected_storage_bytes"] == 1000
        assert rec["actual_storage_bytes"] == 1000
        assert rec["difference_bytes"] == 0
        assert rec["repaired"] is False
        assert rec["objects_checked"] == 142
        assert rec["orphans_found"] == 0

    @pytest.mark.parametrize(
        "counter", ["orphans_found", "missing_objects_found", "metadata_mismatches_found"]
    )
    def test_any_nonzero_integrity_counter_is_a_warning(
        self, client, session, counter
    ):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        counters = {
            "orphans_found": 0,
            "missing_objects_found": 0,
            "metadata_mismatches_found": 0,
        }
        counters[counter] = 3
        _seed_run(session, tenant.id, objects_checked=10, **counters)

        rec = _get(client, admin).json()["reconciliation"]

        assert rec["status"] == "SUCCEEDED"
        assert rec["integrity_status"] == "WARNING"
        assert rec[counter] == 3

    def test_s3_verification_disabled_is_never_healthy(self, client, session):
        """SUCCEEDED with NULL counters means the integrity half never ran.
        A green light for a check that did not happen would be a lie."""
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _seed_run(
            session,
            tenant.id,
            expected_storage_bytes=500,
            actual_storage_bytes=500,
            difference_bytes=0,
            repaired=False,
        )

        rec = _get(client, admin).json()["reconciliation"]

        assert rec["status"] == "SUCCEEDED"
        assert rec["integrity_status"] == "ACCOUNTING_ONLY"
        assert rec["integrity_status"] != "HEALTHY"
        assert rec["orphans_found"] is None
        assert rec["missing_objects_found"] is None
        assert rec["metadata_mismatches_found"] is None
        assert rec["objects_checked"] is None

    def test_a_failed_run_exposes_only_its_sanitized_error_code(
        self, client, session
    ):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _seed_run(
            session,
            tenant.id,
            status=TenantUsageReconciliationStatus.FAILED,
            error_code="s3_access_denied",
        )

        rec = _get(client, admin).json()["reconciliation"]

        assert rec["status"] == "FAILED"
        assert rec["integrity_status"] == "FAILED"
        assert rec["error_code"] == "s3_access_denied"
        assert "error_message" not in rec
        assert "traceback" not in rec

    def test_a_stale_run_recovered_by_the_worker_serializes(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _seed_run(
            session,
            tenant.id,
            status=TenantUsageReconciliationStatus.FAILED,
            error_code="stale_run_recovered",
        )

        rec = _get(client, admin).json()["reconciliation"]

        assert rec["integrity_status"] == "FAILED"
        assert rec["error_code"] == "stale_run_recovered"

    def test_usage_recovered_by_a_previous_run_keeps_null_expected(
        self, client, session
    ):
        """A run that had to initialize the counter has no `expected` to
        compare against — `NULL` there is not "the counter was zero"."""
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=4096)
        _seed_run(
            session,
            tenant.id,
            expected_storage_bytes=None,
            actual_storage_bytes=4096,
            difference_bytes=None,
            repaired=True,
            objects_checked=1,
            orphans_found=0,
            missing_objects_found=0,
            metadata_mismatches_found=0,
        )

        body = _get(client, admin).json()
        rec = body["reconciliation"]

        assert rec["expected_storage_bytes"] is None
        assert rec["difference_bytes"] is None
        assert rec["repaired"] is True
        assert rec["actual_storage_bytes"] == 4096
        assert body["storage"]["billable_bytes"] == 4096

    def test_the_latest_started_run_is_the_one_reported(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        now = datetime.utcnow()
        _seed_run(
            session,
            tenant.id,
            started_at=now - timedelta(hours=2),
            objects_checked=1,
            orphans_found=0,
            missing_objects_found=0,
            metadata_mismatches_found=0,
        )
        _seed_run(
            session,
            tenant.id,
            started_at=now - timedelta(minutes=5),
            status=TenantUsageReconciliationStatus.FAILED,
            error_code="s3_timeout",
        )

        rec = _get(client, admin).json()["reconciliation"]

        assert rec["status"] == "FAILED", "history is not filtered to successes"
        assert rec["error_code"] == "s3_timeout"

    def test_a_real_run_then_a_read_agrees_with_the_counter(self, client, session):
        """One end-to-end case: a genuine reconciliation, then the dashboard
        read of what it produced."""
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        key = f"reports/{tenant.id}/{uuid.uuid4().hex}/official/{uuid.uuid4().hex}.pdf"
        session.add(
            StorageObject(
                provider="aws",
                region="mx-test-1",
                bucket="celuma-test-bucket",
                object_key=key,
                content_type="application/pdf",
                size_bytes=2048,
                sha256_hex=uuid.uuid4().hex,
                etag="fake-etag",
                tenant_id=tenant.id,
            )
        )
        session.commit()
        FakeS3Service.put_raw(key, b"x" * 2048)
        _init_usage(session, tenant.id, billable_storage_bytes=0)
        _set_limits(session, tenant.id, storage_limit_bytes=4096)

        UsageReconciliationService(s3=FakeS3Service()).reconcile_tenant(
            session, tenant.id
        )

        body = _get(client, admin).json()

        assert body["storage"]["billable_bytes"] == 2048
        assert body["storage"]["usage_percent"] == 50.0
        assert body["reconciliation"]["integrity_status"] == "HEALTHY"
        assert body["reconciliation"]["actual_storage_bytes"] == 2048
        assert body["reconciliation"]["difference_bytes"] == 2048
        assert body["reconciliation"]["repaired"] is True
        assert body["reconciliation"]["objects_checked"] == 1


class TestTenantIsolation:
    def test_the_response_describes_the_callers_own_tenant_only(
        self, client, session
    ):
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        admin_a = _admin(session, tenant_a, email="a@t.example")
        _admin(session, tenant_b, email="b@t.example")
        _init_usage(session, tenant_a.id, billable_storage_bytes=111)
        _init_usage(session, tenant_b.id, billable_storage_bytes=999999)
        _set_limits(session, tenant_a.id, storage_limit_bytes=1000, user_limit=10)
        _set_limits(session, tenant_b.id, storage_limit_bytes=5, user_limit=1)
        _seed_run(session, tenant_b.id, actual_storage_bytes=999999, orphans_found=7,
                  missing_objects_found=0, metadata_mismatches_found=0,
                  objects_checked=9)

        body = _get(client, admin_a).json()

        assert body["storage"]["billable_bytes"] == 111
        assert body["storage"]["limit_bytes"] == 1000
        assert body["users"]["registered_users"] == 1
        assert body["reconciliation"]["has_run"] is False
        assert "999999" not in str(body)

    def test_a_tenant_id_query_parameter_is_not_a_way_in(self, client, session):
        """There is no tenant parameter to abuse: whatever an admin sends,
        the response describes their own tenant."""
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        admin_a = _admin(session, tenant_a, email="a2@t.example")
        _init_usage(session, tenant_a.id, billable_storage_bytes=111)
        _init_usage(session, tenant_b.id, billable_storage_bytes=222)

        resp = client.get(
            f"{USAGE_URL}?tenant_id={tenant_b.id}", headers=auth_headers(admin_a)
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["storage"]["billable_bytes"] == 111


class TestRBAC:
    """Permission-driven, not role-driven: the gate is
    `admin:manage_tenant`, and these role assertions describe what the
    seeded RBAC catalog grants."""

    def test_a_tenant_admin_is_allowed(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        assert _get(client, admin).status_code == 200

    def test_a_superuser_is_allowed(self, client, session):
        tenant = create_tenant(session)
        user = create_user(
            session, tenant, email="su@t.example", roles=("superuser",)
        )
        assert _get(client, user).status_code == 200

    @pytest.mark.parametrize(
        "role",
        ["physician", "pathologist", "lab_tech", "assistant", "viewer", "billing"],
    )
    def test_roles_without_the_permission_are_denied(self, client, session, role):
        """`billing` included deliberately: Phase 4 defines this as tenant
        administration information, not invoicing functionality."""
        tenant = create_tenant(session)
        user = create_user(
            session, tenant, email=f"{role}@t.example", roles=(role,)
        )
        assert _get(client, user).status_code == 403

    def test_a_roleless_user_is_denied(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="none@t.example", roles=())
        assert _get(client, user).status_code == 403

    def test_unauthenticated_is_rejected(self, client):
        assert client.get(USAGE_URL).status_code in (401, 403)


class TestSerializationSafety:
    def test_the_response_carries_no_storage_or_patient_identifiers(
        self, client, session
    ):
        tenant = create_tenant(session, name="SECRET_PATIENT_VALUE Laboratorio")
        admin = _admin(session, tenant)
        key = f"reports/{tenant.id}/SECRET_OBJECT_KEY/official/{uuid.uuid4().hex}.pdf"
        obj = StorageObject(
            provider="aws",
            region="mx-test-1",
            bucket="SECRET_BUCKET",
            object_key=key,
            content_type="application/pdf",
            size_bytes=64,
            sha256_hex=uuid.uuid4().hex,
            etag="SECRET_AWS_MESSAGE",
            tenant_id=tenant.id,
        )
        session.add(obj)
        session.commit()
        session.refresh(obj)
        tenant.logo_storage_id = obj.id
        session.add(tenant)
        session.commit()
        _init_usage(session, tenant.id, billable_storage_bytes=64)
        _seed_run(
            session,
            tenant.id,
            actual_storage_bytes=64,
            objects_checked=1,
            orphans_found=0,
            missing_objects_found=0,
            metadata_mismatches_found=0,
        )

        resp = _get(client, admin)
        serialized = resp.text

        assert resp.status_code == 200
        for marker in (
            "SECRET_BUCKET",
            "SECRET_OBJECT_KEY",
            "SECRET_PATIENT_VALUE",
            "SECRET_AWS_MESSAGE",
        ):
            assert marker not in serialized, marker
        assert str(obj.id) not in serialized
        assert str(tenant.id) not in serialized

    def test_the_response_shape_is_exactly_the_documented_contract(
        self, client, session
    ):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=1)

        body = _get(client, admin).json()

        assert set(body) == {"storage", "users", "reconciliation"}
        assert set(body["storage"]) == {
            "initialized",
            "billable_bytes",
            "limit_bytes",
            "unlimited",
            "usage_ratio",
            "usage_percent",
        }
        assert set(body["users"]) == {
            "registered_users",
            "active_internal_users",
            "active_physician_portal_users",
            "user_limit",
            "unlimited",
            "usage_ratio",
            "usage_percent",
        }
        assert set(body["reconciliation"]) == {
            "has_run",
            "integrity_status",
            "status",
            "started_at",
            "completed_at",
            "expected_storage_bytes",
            "actual_storage_bytes",
            "difference_bytes",
            "repaired",
            "objects_checked",
            "orphans_found",
            "missing_objects_found",
            "metadata_mismatches_found",
            "error_code",
        }

    def test_nullable_fields_are_nullable_in_the_openapi_schema(self, client):
        """`null` carries domain meaning here (not initialized / not
        measured / no limit) and must not be flattened into a required
        integer by the generated schema."""
        schema = client.get("/openapi.json").json()["components"]["schemas"]

        storage = schema["StorageUsageResponse"]
        assert "billable_bytes" not in storage.get("required", [])
        assert {"type": "null"} in storage["properties"]["billable_bytes"]["anyOf"]
        assert {"type": "null"} in storage["properties"]["limit_bytes"]["anyOf"]

        rec = schema["ReconciliationSummaryResponse"]
        for field in (
            "expected_storage_bytes",
            "difference_bytes",
            "orphans_found",
            "missing_objects_found",
            "metadata_mismatches_found",
            "objects_checked",
        ):
            assert {"type": "null"} in rec["properties"][field]["anyOf"], field
            assert field not in rec.get("required", []), field


class TestReadPathCost:
    """The dashboard read must stay a counter read. `TenantUsage` exists
    precisely so it does not become the multi-query billable recomputation
    Block C built it to avoid."""

    def test_no_authoritative_recomputation_happens(self, client, session, monkeypatch):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=42)

        def _explode(*args, **kwargs):
            raise AssertionError(
                "GET /tenant/usage must not call StorageBillingService"
            )

        for name in (
            "compute_billable_storage_bytes",
            "compute_breakdown",
            "get_billable_storage_objects",
        ):
            monkeypatch.setattr(
                f"app.services.storage_billing.StorageBillingService.{name}",
                staticmethod(_explode),
            )

        resp = _get(client, admin)

        assert resp.status_code == 200
        assert resp.json()["storage"]["billable_bytes"] == 42

    def test_no_s3_client_is_constructed(self, client, session, monkeypatch):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=42)

        class _ExplodingS3:
            def __init__(self, *args, **kwargs):
                raise AssertionError("GET /tenant/usage must not touch S3")

        monkeypatch.setattr(
            "app.services.usage_reconciliation.S3Service", _ExplodingS3
        )
        monkeypatch.setattr("app.services.s3.S3Service", _ExplodingS3)

        assert _get(client, admin).status_code == 200

    def test_storage_object_is_never_scanned(self, client, session, engine):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=42)
        _seed_run(session, tenant.id, actual_storage_bytes=42, orphans_found=0,
                  missing_objects_found=0, metadata_mismatches_found=0,
                  objects_checked=0)

        statements = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _record)
        try:
            assert _get(client, admin).status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", _record)

        touched = [s for s in statements if "storage_object" in s.lower()]
        assert touched == [], touched
        selects = [s for s in statements if s.lstrip().lower().startswith("select")]
        assert len(selects) <= 12, f"{len(selects)} SELECTs on a dashboard read"

    def test_the_read_never_starts_a_reconciliation(self, client, session):
        tenant = create_tenant(session)
        admin = _admin(session, tenant)
        _init_usage(session, tenant.id, billable_storage_bytes=42)

        assert _get(client, admin).status_code == 200

        runs = session.exec(
            select(TenantUsageReconciliation).where(
                TenantUsageReconciliation.tenant_id == tenant.id
            )
        ).all()
        assert runs == []
