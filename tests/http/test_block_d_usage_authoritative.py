"""Incremental usage vs authoritative recomputation (Céluma 1.3, Phase 5, Block D
— D1, D2, D15).

Block C proved the *delta* of fifteen individual storage mutations. This module
proves the system-level invariant those deltas are supposed to add up to:

    incremental TenantUsage.billable_storage_bytes
        == authoritative billable recomputation
    and, after reconciliation, difference == 0

"Authoritative" is not redefined here. It is
`StorageBillingService.compute_billable_storage_bytes()` — the single canonical
implementation of the seven billable categories ratified in
`phase-4-block-c/billable-storage-calculation-contract.md`, and the very same
function `UsageReconciliationService._run_accounting` calls. Writing a second
"billable" definition for the test would prove only that the test agrees with
itself.

The incremental side is likewise never simulated: every byte in the controlled
tenant below arrives through a real HTTP mutation flow, so the counter moves
the way production moves it.

The `_patch_s3` autouse fixture in `conftest.py` routes every storage call to
`FakeS3Service`; nothing in this module can reach a real bucket.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlmodel import Session, select

from app.models.storage import StorageObject
from app.models.tenant_usage import TenantUsage
from app.services.storage_billing import StorageBillingService
from app.services.usage import UsageService
from app.services.usage_reconciliation import UsageReconciliationService

from .factories import (
    auth_headers,
    create_branch,
    create_letterhead,
    create_order,
    create_sample,
    create_tenant,
    create_user,
)


def _png(size=(32, 32), color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(size=(64, 64), color=(200, 50, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


def incremental(session: Session, tenant_id) -> int | None:
    """B — what the running system believes, accumulated one delta at a time."""
    row = session.get(TenantUsage, tenant_id)
    return row.billable_storage_bytes if row else None


def authoritative(session: Session, tenant_id) -> int:
    """C — the production canonical recomputation. Not a test reimplementation."""
    return StorageBillingService.compute_billable_storage_bytes(session, tenant_id)


class ControlledTenant:
    """A tenant with a known, deliberately multi-category billable object set.

    Every mutation runs through the real endpoint, so `TenantUsage` is built by
    production's own incremental path.
    """

    def __init__(self, client, session, *, name: str, code: str):
        self.client = client
        self.session = session
        self.tenant = create_tenant(session, name=name)
        self.branch = create_branch(session, self.tenant, code=code)
        self.user = create_user(
            session, self.tenant, email=f"admin@{code.lower()}.example"
        )
        self.reviewer = create_user(
            session,
            self.tenant,
            email=f"rev@{code.lower()}.example",
            roles=("reviewer",),
        )
        UsageService.initialize_usage(session, self.tenant.id, billable_storage_bytes=0)
        session.commit()
        #: A — the expected billable object set, recorded as it is built.
        self.expected_categories: dict[str, int] = {}

    @property
    def id(self):
        return self.tenant.id

    @property
    def headers(self):
        return auth_headers(self.user)

    def add_sample_image(self, *, size=(64, 64)) -> str:
        self._seq = getattr(self, "_seq", 0) + 1
        order = create_order(
            self.session,
            self.tenant,
            self.branch,
            order_code=f"ORD-{self._seq}",
        )
        sample = create_sample(
            self.session,
            self.tenant,
            self.branch,
            order,
            sample_code=f"S-{self._seq}",
        )
        response = self.client.post(
            f"/api/v1/laboratory/samples/{sample.id}/images",
            files={"file": ("img.jpg", _jpeg(size=size), "image/jpeg")},
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        self.expected_categories.setdefault("sample_images", 0)
        self.expected_categories["sample_images"] += 1
        return response.json()["sample_image_id"], sample

    def add_tenant_logo(self, *, size=(32, 32)):
        response = self.client.post(
            f"/api/v1/tenants/{self.tenant.id}/logo",
            files={"file": ("logo.png", _png(size=size), "image/png")},
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        self.expected_categories["tenant_logo"] = 1

    def add_letterhead_asset(self, *, size=(24, 24)):
        letterhead = create_letterhead(
            self.session, self.tenant, name=f"LH-{size[0]}"
        )
        response = self.client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/logo",
            files={"file": ("lh.png", _png(size=size), "image/png")},
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        self.expected_categories.setdefault("letterhead_assets", 0)
        self.expected_categories["letterhead_assets"] += 1

    def add_signature(self, *, size=(20, 20)):
        response = self.client.post(
            "/api/v1/users/me/signature",
            files={"file": ("sig.png", _png(size=size), "image/png")},
            headers=auth_headers(self.reviewer),
        )
        assert response.status_code == 200, response.text
        self.expected_categories["signature"] = 1

    def build_representative_estate(self):
        """One of each category the fixture strategy can reach without the
        environment-blocked official-PDF browser leg."""
        self.add_sample_image(size=(64, 64))
        self.add_tenant_logo(size=(32, 32))
        self.add_letterhead_asset(size=(24, 24))
        self.add_signature(size=(20, 20))
        return self


@pytest.fixture
def reconciler():
    return UsageReconciliationService()


# ---------------------------------------------------------------------------
# D1 — the invariant
# ---------------------------------------------------------------------------


class TestIncrementalMatchesAuthoritative:
    """A ≈ B ≈ C with D = 0, on a tenant built entirely through real flows."""

    def test_a_multi_category_tenant_agrees(self, client, session):
        t = ControlledTenant(client, session, name="Estate", code="EST")
        t.build_representative_estate()

        b = incremental(session, t.id)
        c = authoritative(session, t.id)
        assert b == c, f"incremental {b} != authoritative {c}"
        assert c > 0

    def test_each_category_is_represented_in_the_breakdown(self, client, session):
        """Guards against the invariant holding trivially because a category
        silently contributed nothing."""
        t = ControlledTenant(client, session, name="Breakdown", code="BRK")
        t.build_representative_estate()

        breakdown = StorageBillingService.compute_breakdown(session, t.id)
        assert breakdown.sample_images_bytes > 0
        assert breakdown.tenant_logo_bytes > 0
        assert breakdown.letterhead_asset_bytes > 0
        assert breakdown.signature_bytes > 0
        assert breakdown.total_bytes == incremental(session, t.id)

    def test_a_zero_storage_tenant_agrees_at_zero(self, client, session):
        t = ControlledTenant(client, session, name="Empty", code="EMP")
        assert incremental(session, t.id) == 0
        assert authoritative(session, t.id) == 0

    def test_a_deleted_resource_leaves_both_sides_in_agreement(
        self, client, session
    ):
        t = ControlledTenant(client, session, name="Deleted", code="DEL")
        image_id, sample = t.add_sample_image()
        t.add_tenant_logo()

        response = client.delete(
            f"/api/v1/laboratory/samples/{sample.id}/images/{image_id}",
            headers=t.headers,
        )
        assert response.status_code == 200, response.text

        assert incremental(session, t.id) == authoritative(session, t.id)

    def test_a_superseded_logo_leaves_both_sides_in_agreement(
        self, client, session
    ):
        """Replacement is the classic over-count trap: the superseded row is
        retained but must stop counting."""
        t = ControlledTenant(client, session, name="Superseded", code="SUP")
        t.add_tenant_logo(size=(16, 16))
        t.add_tenant_logo(size=(160, 160))

        assert incremental(session, t.id) == authoritative(session, t.id)

    def test_a_replaced_signature_leaves_both_sides_in_agreement(
        self, client, session
    ):
        t = ControlledTenant(client, session, name="Resigned", code="RSG")
        t.add_signature(size=(16, 16))
        t.add_signature(size=(96, 96))

        assert incremental(session, t.id) == authoritative(session, t.id)

    def test_a_retained_unreferenced_letterhead_asset_still_counts(
        self, client, session
    ):
        """The ratified 'count while retained' policy, checked from both sides
        at once rather than assumed."""
        t = ControlledTenant(client, session, name="Retained", code="RET")
        t.add_letterhead_asset(size=(24, 24))
        t.add_letterhead_asset(size=(48, 48))

        breakdown = StorageBillingService.compute_breakdown(session, t.id)
        assert breakdown.letterhead_asset_bytes > 0
        assert incremental(session, t.id) == authoritative(session, t.id)


# ---------------------------------------------------------------------------
# D2 — reconciliation as an operational process
# ---------------------------------------------------------------------------


class TestReconciliationOnACleanTenant:
    def test_no_false_drift_is_reported(self, client, session, reconciler):
        t = ControlledTenant(client, session, name="Clean", code="CLN")
        t.build_representative_estate()

        outcome = reconciler.reconcile_tenant(
            session, t.id, repair=True, verify_s3=False
        )
        assert outcome.status == "SUCCEEDED"
        assert outcome.difference_bytes == 0
        assert outcome.repaired is False
        assert outcome.expected_storage_bytes == outcome.actual_storage_bytes

    def test_a_clean_run_does_not_move_the_counter(
        self, client, session, reconciler
    ):
        t = ControlledTenant(client, session, name="Clean2", code="CL2")
        t.build_representative_estate()
        before = incremental(session, t.id)

        reconciler.reconcile_tenant(session, t.id, repair=True, verify_s3=False)
        assert incremental(session, t.id) == before


class TestReconciliationOnADriftedTenant:
    """Drift is injected into the *counter*, never into the authoritative
    object set — that is what makes the repair target unambiguous."""

    @pytest.fixture
    def drifted(self, client, session):
        t = ControlledTenant(client, session, name="Drift", code="DRF")
        t.build_representative_estate()
        truth = authoritative(session, t.id)

        UsageService.adjust_storage(
            session, t.id, 5_000_000, source="test_injected_drift"
        )
        session.commit()
        assert incremental(session, t.id) == truth + 5_000_000
        return t, truth

    def test_drift_is_detected(self, session, reconciler, drifted):
        t, truth = drifted
        outcome = reconciler.reconcile_tenant(
            session, t.id, repair=False, verify_s3=False
        )
        assert outcome.status == "SUCCEEDED"
        assert outcome.actual_storage_bytes == truth
        assert outcome.expected_storage_bytes == truth + 5_000_000
        assert outcome.difference_bytes == -5_000_000

    def test_detection_without_repair_does_not_mutate_the_counter(
        self, session, reconciler, drifted
    ):
        t, truth = drifted
        reconciler.reconcile_tenant(session, t.id, repair=False, verify_s3=False)
        assert incremental(session, t.id) == truth + 5_000_000
        assert authoritative(session, t.id) == truth

    def test_repair_produces_the_authoritative_value(
        self, session, reconciler, drifted
    ):
        t, truth = drifted
        outcome = reconciler.reconcile_tenant(
            session, t.id, repair=True, verify_s3=False
        )
        assert outcome.repaired is True
        assert incremental(session, t.id) == truth
        assert incremental(session, t.id) == authoritative(session, t.id)

    def test_a_second_run_is_clean_and_idempotent(
        self, session, reconciler, drifted
    ):
        t, truth = drifted
        reconciler.reconcile_tenant(session, t.id, repair=True, verify_s3=False)

        second = reconciler.reconcile_tenant(
            session, t.id, repair=True, verify_s3=False
        )
        assert second.difference_bytes == 0
        assert second.repaired is False
        assert incremental(session, t.id) == truth

    def test_under_counting_drift_is_also_repaired(self, client, session, reconciler):
        t = ControlledTenant(client, session, name="Under", code="UND")
        t.build_representative_estate()
        truth = authoritative(session, t.id)

        UsageService.decrement_storage(
            session, t.id, truth // 2, source="test_injected_drift"
        )
        session.commit()
        assert incremental(session, t.id) < truth

        outcome = reconciler.reconcile_tenant(
            session, t.id, repair=True, verify_s3=False
        )
        assert outcome.repaired is True
        assert outcome.difference_bytes > 0
        assert incremental(session, t.id) == truth


class TestReconciliationRecordsAreCorrect:
    def test_the_run_is_persisted_against_the_right_tenant(
        self, client, session, reconciler
    ):
        from app.models.tenant_usage_reconciliation import TenantUsageReconciliation

        t = ControlledTenant(client, session, name="Audit", code="AUD")
        t.build_representative_estate()
        outcome = reconciler.reconcile_tenant(
            session, t.id, repair=True, verify_s3=False
        )

        row = session.get(TenantUsageReconciliation, outcome.reconciliation_id)
        assert row is not None
        assert row.tenant_id == t.id
        assert row.status == "SUCCEEDED"
        assert row.completed_at is not None
        assert row.error_code is None

    def test_a_missing_usage_row_is_initialized_from_the_authoritative_baseline(
        self, client, session, reconciler
    ):
        """Recovery must never seed from a partial/incremental value."""
        t = ControlledTenant(client, session, name="Missing", code="MSS")
        t.build_representative_estate()
        truth = authoritative(session, t.id)

        session.delete(session.get(TenantUsage, t.id))
        session.commit()
        assert incremental(session, t.id) is None

        outcome = reconciler.reconcile_tenant(
            session, t.id, repair=True, verify_s3=False
        )
        assert outcome.usage_initialized is True
        assert outcome.expected_storage_bytes is None
        assert outcome.difference_bytes is None
        assert incremental(session, t.id) == truth


# ---------------------------------------------------------------------------
# D15 — multi-tenant operational isolation
# ---------------------------------------------------------------------------


class TestReconciliationTenantIsolation:
    @pytest.fixture
    def two_tenants(self, client, session):
        a = ControlledTenant(client, session, name="Iso A", code="ISA")
        a.build_representative_estate()
        b = ControlledTenant(client, session, name="Iso B", code="ISB")
        b.add_sample_image(size=(128, 128))
        b.add_tenant_logo(size=(64, 64))
        return a, b

    def test_reconciling_one_tenant_does_not_touch_the_other(
        self, session, reconciler, two_tenants
    ):
        a, b = two_tenants
        b_before = incremental(session, b.id)

        UsageService.adjust_storage(session, a.id, 9_000_000, source="test_drift")
        session.commit()

        reconciler.reconcile_tenant(session, a.id, repair=True, verify_s3=False)

        assert incremental(session, b.id) == b_before
        assert incremental(session, b.id) == authoritative(session, b.id)

    def test_each_tenants_authoritative_value_is_its_own(
        self, session, two_tenants
    ):
        a, b = two_tenants
        assert authoritative(session, a.id) != authoritative(session, b.id)
        assert incremental(session, a.id) == authoritative(session, a.id)
        assert incremental(session, b.id) == authoritative(session, b.id)

    def test_a_tenants_objects_are_not_counted_into_its_neighbour(
        self, session, two_tenants
    ):
        """Every object in A's billable set belongs to A, and A's total is
        exactly their sum — so nothing of B's can be contributing to it."""
        a, b = two_tenants
        b_keys = {
            row.object_key
            for row in session.exec(
                select(StorageObject).where(StorageObject.tenant_id == b.id)
            ).all()
        }

        a_objects = StorageBillingService.get_billable_storage_objects(session, a.id)
        assert a_objects, "tenant A's billable set must not be empty"
        assert not ({o.object_key for o in a_objects} & b_keys)
        assert sum(o.size_bytes or 0 for o in a_objects) == authoritative(
            session, a.id
        )

    def test_a_drifted_neighbour_does_not_perturb_a_clean_tenant(
        self, session, reconciler, two_tenants
    ):
        a, b = two_tenants
        UsageService.adjust_storage(session, b.id, 1_234_567, source="test_drift")
        session.commit()

        a_outcome = reconciler.reconcile_tenant(
            session, a.id, repair=True, verify_s3=False
        )
        assert a_outcome.difference_bytes == 0
        assert a_outcome.repaired is False

        b_outcome = reconciler.reconcile_tenant(
            session, b.id, repair=True, verify_s3=False
        )
        assert b_outcome.repaired is True
        assert b_outcome.tenant_id == b.id
        assert incremental(session, b.id) == authoritative(session, b.id)
