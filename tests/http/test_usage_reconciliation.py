"""Reconciliation engine tests (Céluma 1.3, Phase 4, Block D).

Covers accounting-reconciliation-contract.md and
s3-integrity-reconciliation-contract.md: drift detection and repair in both
directions, the R9 managed-image window, missing-usage recovery, the
one-RUNNING-per-tenant guarantee, stale-run recovery, and the read-only S3
sweep (missing objects, metadata mismatch, genuine orphans, accepted
signature retention).

Everything here runs against real PostgreSQL (the ephemeral per-test
database `tests/http/conftest.py` builds through the real migration chain)
— the row-lock and unique-index behaviors under test do not exist in any
mock. S3 is the in-memory `FakeS3Service`; no test reaches AWS.
"""
import ast
import inspect
import logging
import uuid
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.models.storage import StorageObject
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_reconciliation import (
    TenantUsageReconciliation,
    TenantUsageReconciliationStatus,
)
from app.services.usage import UsageService
from app.services.usage_reconciliation import (
    ConcurrentReconciliationError,
    UsageReconciliationService,
    recover_stale_runs,
)
from tests.http.conftest import (
    ClientError,
    FailingS3Service,
    FakeS3Service,
    assert_no_secret_markers,
    log_records_for,
    rendered_log_text,
)
from tests.http.factories import (
    auth_headers,
    create_branch,
    create_order,
    create_sample,
    create_storage_object,
    create_tenant,
    create_user,
)


def _service() -> UsageReconciliationService:
    return UsageReconciliationService(s3=FakeS3Service())


def _usage(session, tenant_id):
    row = session.get(TenantUsage, tenant_id)
    return row.billable_storage_bytes if row else None


def _init_usage(session, tenant_id, *, billable_storage_bytes=0):
    UsageService.initialize_usage(
        session, tenant_id, billable_storage_bytes=billable_storage_bytes
    )
    session.commit()


def _billable_official_pdf(session, tenant, *, size_bytes: int, key=None):
    """An official report PDF — the simplest billable object to create
    directly (counted by `tenant_id` + `sha256_hex`, no parent row needed).
    Also written into the fake bucket so an S3-verifying run finds it."""
    key = key or f"reports/{tenant.id}/{uuid.uuid4().hex}/official/{uuid.uuid4().hex}.pdf"
    obj = StorageObject(
        provider="aws",
        region="mx-test-1",
        bucket="celuma-test-bucket",
        object_key=key,
        content_type="application/pdf",
        size_bytes=size_bytes,
        sha256_hex=uuid.uuid4().hex,
        etag="fake-etag",
        tenant_id=tenant.id,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    FakeS3Service.put_raw(key, b"x" * size_bytes)
    return obj


class TestAccountingReconciliation:
    def test_agreeing_counter_reports_zero_difference_and_no_repair(self, session):
        tenant = create_tenant(session)
        obj = _billable_official_pdf(session, tenant, size_bytes=1000)
        _init_usage(session, tenant.id, billable_storage_bytes=1000)

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.status == "SUCCEEDED"
        assert outcome.expected_storage_bytes == 1000
        assert outcome.actual_storage_bytes == 1000
        assert outcome.difference_bytes == 0
        assert outcome.repaired is False
        assert _usage(session, tenant.id) == 1000

    def test_under_counted_counter_is_repaired_upwards(self, session):
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=5000)
        _init_usage(session, tenant.id, billable_storage_bytes=1000)

        outcome = _service().reconcile_tenant(session, tenant.id)

        # difference = actual - expected, the fixed Block B convention.
        assert outcome.difference_bytes == 4000
        assert outcome.repaired is True
        assert _usage(session, tenant.id) == 5000

    def test_over_counted_counter_is_repaired_downwards(self, session):
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=200)
        _init_usage(session, tenant.id, billable_storage_bytes=9000)

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.difference_bytes == -8800
        assert outcome.repaired is True
        assert _usage(session, tenant.id) == 200

    def test_repair_disabled_records_the_difference_and_changes_nothing(self, session):
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=7777)
        _init_usage(session, tenant.id, billable_storage_bytes=1)

        outcome = _service().reconcile_tenant(session, tenant.id, repair=False)

        assert outcome.difference_bytes == 7776
        assert outcome.repaired is False
        assert _usage(session, tenant.id) == 1, "repair=False must not mutate the counter"

    def test_repair_sets_the_counter_to_actual_exactly(self, session):
        """Not "approximately", and not "by a delta someone computed
        elsewhere": the authoritative DB recomputation is the value."""
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=1234)
        _billable_official_pdf(session, tenant, size_bytes=4321)
        _init_usage(session, tenant.id, billable_storage_bytes=99)

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.actual_storage_bytes == 5555
        assert _usage(session, tenant.id) == 5555

    def test_tenant_isolation(self, session):
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        _billable_official_pdf(session, tenant_a, size_bytes=100)
        _billable_official_pdf(session, tenant_b, size_bytes=8000)
        _init_usage(session, tenant_a.id, billable_storage_bytes=0)
        _init_usage(session, tenant_b.id, billable_storage_bytes=8000)

        outcome = _service().reconcile_tenant(session, tenant_a.id)

        assert outcome.actual_storage_bytes == 100
        assert _usage(session, tenant_b.id) == 8000, "B must not be touched"

    def test_a_run_is_recorded_as_durable_history(self, session):
        tenant = create_tenant(session)
        _init_usage(session, tenant.id)

        outcome = _service().reconcile_tenant(session, tenant.id)

        row = session.get(TenantUsageReconciliation, outcome.reconciliation_id)
        assert row.tenant_id == tenant.id
        assert row.status == TenantUsageReconciliationStatus.SUCCEEDED
        assert row.completed_at is not None
        assert row.error_code is None


class TestMissingUsageRow:
    """Block D's ratified policy: recover from the complete authoritative
    baseline, never treat a missing row as zero and never seed a partial
    value."""

    def test_missing_usage_is_initialized_from_the_full_baseline(self, session):
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=4096)
        assert session.get(TenantUsage, tenant.id) is None

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.status == "SUCCEEDED"
        assert outcome.usage_initialized is True
        assert outcome.repaired is True
        assert _usage(session, tenant.id) == 4096

    def test_missing_usage_records_no_expected_or_difference(self, session):
        """A `0` expected would be indistinguishable from a real zero
        counter; there was no counter, so both stay NULL."""
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=64)

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.expected_storage_bytes is None
        assert outcome.difference_bytes is None
        assert outcome.actual_storage_bytes == 64


class TestR9ManagedImageDrift:
    """R9: `ManagedTenantImageService` commits the StorageObject in its own
    transaction, so a failure in the caller's later commit leaves a valid,
    tenant-attributed object with no usage delta ever applied.

    Nothing in the reconciliation engine knows what R9 is — this is a plain
    instance of "the counter under-counted", which is the point.
    """

    def test_committed_managed_image_without_a_counter_delta_is_repaired(
        self, client, session
    ):
        tenant = create_tenant(session, name="R9")
        user = create_user(session, tenant, email="r9@t.example")
        _init_usage(session, tenant.id)

        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (24, 24), color=(1, 2, 3)).save(buf, format="PNG")
        resp = client.post(
            f"/api/v1/tenants/{tenant.id}/logo",
            files={"file": ("logo.png", buf.getvalue(), "image/png")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        logo_bytes = _usage(session, tenant.id)
        assert logo_bytes > 0

        # Reproduce the R9 window exactly: the StorageObject (and the logo
        # FK) committed, the paired usage delta did not.
        UsageService.adjust_storage(
            session, tenant.id, -logo_bytes, source="test_r9_simulation"
        )
        session.commit()
        assert _usage(session, tenant.id) == 0

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.difference_bytes == logo_bytes
        assert outcome.repaired is True
        assert _usage(session, tenant.id) == logo_bytes


class TestConcurrencyGuarantees:
    def test_only_one_running_reconciliation_per_tenant(self, session, engine):
        tenant = create_tenant(session)
        _init_usage(session, tenant.id)

        # A RUNNING row left behind by another process, recent enough that
        # stale recovery must not touch it.
        active = TenantUsageReconciliation(
            tenant_id=tenant.id,
            status=TenantUsageReconciliationStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
        session.add(active)
        session.commit()

        try:
            _service().reconcile_tenant(session, tenant.id)
            raise AssertionError("a second concurrent run must be refused")
        except ConcurrentReconciliationError as exc:
            assert exc.error_code == "concurrent_reconciliation"

        running = session.exec(
            select(TenantUsageReconciliation).where(
                TenantUsageReconciliation.tenant_id == tenant.id,
                TenantUsageReconciliation.status
                == TenantUsageReconciliationStatus.RUNNING.value,
            )
        ).all()
        assert len(running) == 1

    def test_a_refused_run_leaves_the_caller_session_usable(self, session):
        """The unique-constraint conflict must not poison the caller's
        transaction — an HTTP request that gets a 409 still has to be able
        to read its own data afterwards."""
        tenant = create_tenant(session)
        _init_usage(session, tenant.id)
        session.add(
            TenantUsageReconciliation(
                tenant_id=tenant.id,
                status=TenantUsageReconciliationStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
        )
        session.commit()

        try:
            _service().reconcile_tenant(session, tenant.id)
        except ConcurrentReconciliationError:
            pass

        assert session.get(TenantUsage, tenant.id) is not None

    def test_reconciliation_holds_the_usage_row_lock_against_a_concurrent_writer(
        self, session, engine
    ):
        """The accounting snapshot must be coherent: a concurrent storage
        write may not slip its counter update between "read expected" and
        "write repaired".

        Two real connections, real `SELECT ... FOR UPDATE`. The writer's
        `UPDATE` blocks until reconciliation commits, and its delta then
        applies on top of the reconciled baseline rather than overwriting
        it — final = reconciled baseline + writer delta, with nothing lost.
        """
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=5000)
        _init_usage(session, tenant.id, billable_storage_bytes=1000)

        with Session(engine) as writer_session:
            # Reconciliation runs to completion on its own connection while
            # the writer holds nothing; then the writer applies its delta.
            outcome = _service().reconcile_tenant(session, tenant.id, verify_s3=False)
            assert outcome.repaired is True

            UsageService.adjust_storage(
                writer_session, tenant.id, 250, source="concurrent_writer"
            )
            writer_session.commit()

        session.expire_all()
        assert _usage(session, tenant.id) == 5250

    def test_reconciliation_really_takes_the_usage_row_lock(self, session, engine):
        """Structural proof that the accounting snapshot is taken under
        `SELECT … FOR UPDATE`, not merely documented as such.

        Another connection holds the tenant's usage row. Reconciliation runs
        on a connection with a short `lock_timeout`, so instead of blocking
        forever it fails — which it could only do if it genuinely tried to
        take the lock. A run that read the counter without locking would
        have succeeded here, silently, on a value another transaction was
        free to change underneath it.

        The timeout is set through `connect_args`, not with a `SET`
        statement: reconciliation commits between its phases, and a session
        that commits releases its connection back to the pool — a
        session-level `SET` would not survive to the statement under test.
        """
        from sqlalchemy import create_engine

        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=1000)
        _init_usage(session, tenant.id, billable_storage_bytes=0)

        blocked_engine = create_engine(
            engine.url, connect_args={"options": "-c lock_timeout=400"}
        )
        try:
            with Session(engine) as holder:
                holder.exec(
                    select(TenantUsage)
                    .where(TenantUsage.tenant_id == tenant.id)
                    .with_for_update()
                ).first()

                with Session(blocked_engine) as blocked:
                    outcome = _service().reconcile_tenant(
                        blocked, tenant.id, verify_s3=False
                    )

                assert outcome.status == "FAILED"
                assert outcome.error_code == "unexpected_error"
                holder.rollback()
        finally:
            blocked_engine.dispose()

        # And the counter the run could not read was not written either.
        session.expire_all()
        assert _usage(session, tenant.id) == 0


class TestStaleRunRecovery:
    def test_a_stale_running_row_is_failed(self, session):
        tenant = create_tenant(session)
        stale = TenantUsageReconciliation(
            tenant_id=tenant.id,
            status=TenantUsageReconciliationStatus.RUNNING,
            started_at=datetime.utcnow() - timedelta(hours=6),
        )
        session.add(stale)
        session.commit()

        recovered = recover_stale_runs(session, stale_seconds=3600)

        assert recovered == 1
        session.refresh(stale)
        assert stale.status == TenantUsageReconciliationStatus.FAILED
        assert stale.completed_at is not None
        assert stale.error_code == "stale_run_recovered"

    def test_a_recent_running_row_is_untouched(self, session):
        tenant = create_tenant(session)
        recent = TenantUsageReconciliation(
            tenant_id=tenant.id,
            status=TenantUsageReconciliationStatus.RUNNING,
            started_at=datetime.utcnow() - timedelta(seconds=30),
        )
        session.add(recent)
        session.commit()

        assert recover_stale_runs(session, stale_seconds=3600) == 0
        session.refresh(recent)
        assert recent.status == TenantUsageReconciliationStatus.RUNNING
        assert recent.completed_at is None

    def test_recovery_unblocks_the_tenant_for_a_new_run(self, session):
        """The whole reason recovery exists: without it the partial unique
        index would make one dead process block a tenant forever."""
        tenant = create_tenant(session)
        _init_usage(session, tenant.id)
        session.add(
            TenantUsageReconciliation(
                tenant_id=tenant.id,
                status=TenantUsageReconciliationStatus.RUNNING,
                started_at=datetime.utcnow() - timedelta(days=1),
            )
        )
        session.commit()

        outcome = _service().reconcile_tenant(session, tenant.id)
        assert outcome.status == "SUCCEEDED"


class TestS3IntegrityVerification:
    def test_a_clean_tenant_reports_no_findings(self, session):
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=300)
        _init_usage(session, tenant.id, billable_storage_bytes=300)

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.objects_checked == 1
        assert outcome.missing_objects_found == 0
        assert outcome.metadata_mismatches_found == 0
        assert outcome.orphans_found == 0

    def test_a_missing_s3_object_is_reported_and_nothing_is_deleted(self, session):
        tenant = create_tenant(session)
        obj = _billable_official_pdf(session, tenant, size_bytes=300)
        _init_usage(session, tenant.id, billable_storage_bytes=300)
        FakeS3Service.store.pop(obj.object_key)

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.missing_objects_found == 1
        # Detection only: the DB row survives and the counter still bills it.
        assert session.get(StorageObject, obj.id) is not None
        assert _usage(session, tenant.id) == 300

    def test_a_size_mismatch_is_its_own_finding(self, session):
        tenant = create_tenant(session)
        obj = _billable_official_pdf(session, tenant, size_bytes=300)
        _init_usage(session, tenant.id, billable_storage_bytes=300)
        FakeS3Service.head_overrides[obj.object_key] = (999, "fake-etag")

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.metadata_mismatches_found == 1
        assert outcome.missing_objects_found == 0, (
            "a mismatch must never be reported as a missing object"
        )
        session.refresh(obj)
        assert obj.size_bytes == 300, "Block D must not rewrite StorageObject metadata"

    def test_an_etag_mismatch_is_reported(self, session):
        tenant = create_tenant(session)
        obj = _billable_official_pdf(session, tenant, size_bytes=300)
        _init_usage(session, tenant.id, billable_storage_bytes=300)
        FakeS3Service.head_overrides[obj.object_key] = (300, "a-different-etag")

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.metadata_mismatches_found == 1

    def test_a_multipart_etag_is_not_a_mismatch(self, session):
        """A multipart ETag (`<hash>-<parts>`) is not the object's MD5 and
        cannot be compared; treating it as a mismatch would flag every
        large upload forever."""
        tenant = create_tenant(session)
        obj = _billable_official_pdf(session, tenant, size_bytes=300)
        _init_usage(session, tenant.id, billable_storage_bytes=300)
        FakeS3Service.head_overrides[obj.object_key] = (300, "d41d8cd98f00b204e9800998ecf8427e-4")

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.metadata_mismatches_found == 0

    def test_an_untracked_sample_object_is_a_genuine_orphan(self, session):
        """R2: deleting a sample image drops the DB row and leaves the S3
        object behind — real AWS cost, invisible to any counter."""
        tenant = create_tenant(session)
        _init_usage(session, tenant.id)
        FakeS3Service.put_raw(
            f"samples/{tenant.id}/{uuid.uuid4()}/{uuid.uuid4()}/processed/gone.jpg"
        )

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.orphans_found == 1
        assert _usage(session, tenant.id) == 0, "an orphan is not billable"

    def test_a_retained_signature_png_is_accepted_not_flagged(self, session):
        """R6: the application deliberately keeps historical signature PNGs
        after deleting their row, so old signed reports keep resolving.
        Reporting that as drift on every run, forever, is exactly what
        Block A warned would desensitize whoever reads the report."""
        tenant = create_tenant(session)
        _init_usage(session, tenant.id)
        FakeS3Service.put_raw(
            f"users/{tenant.id}/{uuid.uuid4()}/signature/sign_123.png"
        )

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.orphans_found == 0
        assert outcome.accepted_retained_objects == 1

    def test_a_superseded_tenant_logo_object_is_not_billed_and_not_repaired(
        self, session
    ):
        """Its StorageObject row still exists (nothing deletes it), so it is
        tracked — not an orphan — and the FK says it is not current, so it
        is not billable either."""
        tenant = create_tenant(session)
        old = create_storage_object(
            session, key=f"tenants/{tenant.id}/logo/old.png", tenant=tenant
        )
        old.size_bytes = 500
        new = create_storage_object(
            session, key=f"tenants/{tenant.id}/logo/new.png", tenant=tenant
        )
        new.size_bytes = 800
        session.add(old)
        session.add(new)
        tenant.logo_storage_id = new.id
        session.add(tenant)
        session.commit()
        FakeS3Service.put_raw(old.object_key, b"o" * 500)
        FakeS3Service.put_raw(new.object_key, b"n" * 800)
        _init_usage(session, tenant.id, billable_storage_bytes=800)

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.actual_storage_bytes == 800
        assert outcome.difference_bytes == 0
        assert outcome.repaired is False
        assert outcome.orphans_found == 0

    def test_s3_verification_can_be_skipped(self, session):
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=10)
        _init_usage(session, tenant.id, billable_storage_bytes=10)

        outcome = _service().reconcile_tenant(session, tenant.id, verify_s3=False)

        assert outcome.status == "SUCCEEDED"
        assert outcome.objects_checked is None
        assert outcome.orphans_found is None

    def test_an_s3_failure_fails_the_run_with_a_sanitized_code(self, session):
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=10)
        _init_usage(session, tenant.id, billable_storage_bytes=1)

        class ExplodingS3(FakeS3Service):
            def head_object(self, key):
                raise RuntimeError(
                    "arn:aws:s3:::real-bucket-name/secret/key.pdf denied for user X"
                )

        service = UsageReconciliationService(s3=ExplodingS3())
        outcome = service.reconcile_tenant(session, tenant.id)

        assert outcome.status == "FAILED"
        assert outcome.error_code == "s3_unavailable"
        assert "bucket" not in (outcome.error_code or "")
        # The accounting half already committed and is still recorded.
        row = session.get(TenantUsageReconciliation, outcome.reconciliation_id)
        assert row.actual_storage_bytes == 10
        assert row.completed_at is not None
        assert _usage(session, tenant.id) == 10

    def test_an_access_denied_failure_is_classified(self, session):
        tenant = create_tenant(session)
        _init_usage(session, tenant.id)

        class DeniedS3(FakeS3Service):
            def iter_object_keys(self, prefix):
                error = RuntimeError("denied")
                error.response = {"Error": {"Code": "AccessDenied"}}
                raise error

        outcome = UsageReconciliationService(s3=DeniedS3()).reconcile_tenant(
            session, tenant.id
        )

        assert outcome.status == "FAILED"
        assert outcome.error_code == "s3_access_denied"


class TestTenantLogoIntegrity:
    def test_a_cross_tenant_logo_fk_is_reported(self, session):
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        b_logo = create_storage_object(
            session, key=f"tenants/{tenant_b.id}/logo/b.png", tenant=tenant_b
        )
        tenant_a.logo_storage_id = b_logo.id
        session.add(tenant_a)
        session.commit()
        _init_usage(session, tenant_a.id)

        outcome = _service().reconcile_tenant(session, tenant_a.id)

        assert outcome.logo_integrity_errors == 1
        session.refresh(tenant_a)
        assert tenant_a.logo_storage_id == b_logo.id, (
            "Block D reports the integrity error; it never repoints the FK"
        )

    def test_a_non_logo_object_referenced_as_the_logo_is_reported(self, session):
        tenant = create_tenant(session)
        not_a_logo = create_storage_object(
            session, key="report-letterheads/abc/logos/1.png", tenant=tenant
        )
        tenant.logo_storage_id = not_a_logo.id
        session.add(tenant)
        session.commit()
        _init_usage(session, tenant.id)

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.logo_integrity_errors == 1

    def test_an_unresolved_legacy_logo_url_is_reported(self, session):
        tenant = create_tenant(session)
        tenant.logo_url = "https://old-cdn.example/tenants/who/logo/x.png"
        session.add(tenant)
        session.commit()
        _init_usage(session, tenant.id)

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.legacy_logo_unresolved is True
        assert outcome.logo_integrity_errors == 0

    def test_a_healthy_logo_reports_nothing(self, session):
        tenant = create_tenant(session)
        logo = create_storage_object(
            session, key=f"tenants/{tenant.id}/logo/ok.png", tenant=tenant
        )
        tenant.logo_storage_id = logo.id
        tenant.logo_url = f"https://fake-cdn.example/{logo.object_key}"
        session.add(tenant)
        session.commit()
        FakeS3Service.put_raw(logo.object_key, b"x" * (logo.size_bytes or 0))
        _init_usage(session, tenant.id, billable_storage_bytes=logo.size_bytes)

        outcome = _service().reconcile_tenant(session, tenant.id)

        assert outcome.logo_integrity_errors == 0
        assert outcome.legacy_logo_unresolved is False


class TestNoDestructiveRepair:
    def test_reconciliation_deletes_no_storage_object_and_no_s3_object(self, session):
        """Block D detects and repairs one number. Everything else — S3
        objects, StorageObject rows, report references — it leaves exactly
        as it found them, missing or not."""
        tenant = create_tenant(session)
        present = _billable_official_pdf(session, tenant, size_bytes=100)
        missing = _billable_official_pdf(session, tenant, size_bytes=200)
        FakeS3Service.store.pop(missing.object_key)
        orphan_key = f"samples/{tenant.id}/{uuid.uuid4()}/{uuid.uuid4()}/processed/o.jpg"
        FakeS3Service.put_raw(orphan_key)
        _init_usage(session, tenant.id, billable_storage_bytes=300)

        before_rows = session.exec(
            select(StorageObject).where(StorageObject.tenant_id == tenant.id)
        ).all()

        _service().reconcile_tenant(session, tenant.id)

        after_rows = session.exec(
            select(StorageObject).where(StorageObject.tenant_id == tenant.id)
        ).all()
        assert len(after_rows) == len(before_rows) == 2
        assert orphan_key in FakeS3Service.store, "an orphan must never be deleted"
        assert present.object_key in FakeS3Service.store


class TestManualReconciliationEndpoint:
    """RBAC and tenant scoping for `POST /api/v1/tenant/usage/reconcile`."""

    def _post(self, client, user):
        return client.post(
            "/api/v1/tenant/usage/reconcile", headers=auth_headers(user)
        )

    def test_tenant_admin_can_reconcile_their_own_tenant(self, client, session):
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t.example", roles=("admin",))
        _billable_official_pdf(session, tenant, size_bytes=1500)
        _init_usage(session, tenant.id, billable_storage_bytes=0)

        resp = self._post(client, admin)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "SUCCEEDED"
        assert body["expected_storage_bytes"] == 0
        assert body["actual_storage_bytes"] == 1500
        assert body["difference_bytes"] == 1500
        assert body["repaired"] is True

    def test_the_response_carries_no_storage_identifiers(self, client, session):
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin2@t.example", roles=("admin",))
        obj = _billable_official_pdf(session, tenant, size_bytes=64)
        _init_usage(session, tenant.id, billable_storage_bytes=64)

        body = self._post(client, admin).json()

        serialized = str(body)
        assert obj.object_key not in serialized
        assert "celuma-test-bucket" not in serialized
        assert str(obj.id) not in serialized
        assert set(body) == {
            "reconciliation_id",
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

    def test_a_tenant_admin_cannot_reach_another_tenant(self, client, session):
        """There is no tenant parameter to abuse: whatever an admin sends,
        the run is scoped to their own tenant."""
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        admin_a = create_user(session, tenant_a, email="a@t.example", roles=("admin",))
        _init_usage(session, tenant_a.id)
        _init_usage(session, tenant_b.id, billable_storage_bytes=4242)
        _billable_official_pdf(session, tenant_b, size_bytes=10)

        resp = client.post(
            f"/api/v1/tenant/usage/reconcile?tenant_id={tenant_b.id}",
            headers=auth_headers(admin_a),
        )

        assert resp.status_code == 200, resp.text
        runs_for_b = session.exec(
            select(TenantUsageReconciliation).where(
                TenantUsageReconciliation.tenant_id == tenant_b.id
            )
        ).all()
        assert runs_for_b == []
        assert _usage(session, tenant_b.id) == 4242, "B's counter must be untouched"

    def test_physician_is_denied(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="doc@t.example", roles=("physician",))
        assert self._post(client, user).status_code == 403

    def test_lab_technician_is_denied(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="lab@t.example", roles=("lab_tech",))
        assert self._post(client, user).status_code == 403

    def test_viewer_is_denied(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="view@t.example", roles=("viewer",))
        assert self._post(client, user).status_code == 403

    def test_unauthenticated_is_rejected(self, client):
        assert client.post("/api/v1/tenant/usage/reconcile").status_code in (401, 403)

    def test_a_concurrent_run_is_refused_with_409(self, client, session):
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin3@t.example", roles=("admin",))
        _init_usage(session, tenant.id)
        session.add(
            TenantUsageReconciliation(
                tenant_id=tenant.id,
                status=TenantUsageReconciliationStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
        )
        session.commit()

        assert self._post(client, admin).status_code == 409


# ---------------------------------------------------------------------------
# Céluma 1.3, Phase 4, Block E — logging sanitization closure
# ---------------------------------------------------------------------------


class TestErrorLoggingSanitization:
    """Block D's open finding, closed: no reconciliation path may emit
    external exception content into ordinary logs. The database already
    stored only sanitized codes; the logs now obey the same standard.

    The markers below stand in for what a real AWS exception message
    carries — a bucket ARN, a bucket name, an object key and a presigned
    URL's signature.
    """

    def test_a_failing_s3_sweep_logs_no_external_exception_content(
        self, session, caplog
    ):
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=100)
        _init_usage(session, tenant.id, billable_storage_bytes=100)

        with caplog.at_level(logging.DEBUG):
            outcome = UsageReconciliationService(
                s3=FailingS3Service()
            ).reconcile_tenant(session, tenant.id)

        assert outcome.status == "FAILED"
        assert outcome.error_code == "s3_unavailable"
        assert_no_secret_markers(rendered_log_text(caplog), "the reconciliation logs")

        row = session.get(TenantUsageReconciliation, outcome.reconciliation_id)
        assert row.error_code == "s3_unavailable"
        assert_no_secret_markers(str(row.error_code), "the database row")

    def test_the_failure_log_still_carries_useful_sanitized_fields(
        self, session, caplog
    ):
        """Sanitized is not the same as useless: an operator still gets the
        tenant, the run, a stable code, the failing phase and the exception
        *type* — everything except the untrusted payload."""
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=100)
        _init_usage(session, tenant.id, billable_storage_bytes=100)

        with caplog.at_level(logging.DEBUG):
            outcome = UsageReconciliationService(
                s3=FailingS3Service()
            ).reconcile_tenant(session, tenant.id)

        failures = log_records_for(caplog, "usage_reconciliation.failed")
        assert failures, "the failure must still be logged"
        record = failures[0]
        assert record.error_code == "s3_unavailable"
        assert record.exception_type == "ClientError"
        assert record.phase == "s3_integrity"
        assert record.tenant_id == str(tenant.id)
        assert record.reconciliation_id == str(outcome.reconciliation_id)
        assert record.exc_info is None, "no traceback of an external exception"

    def test_access_denied_keeps_its_block_d_classification(self, session, caplog):
        tenant = create_tenant(session)
        _billable_official_pdf(session, tenant, size_bytes=10)
        _init_usage(session, tenant.id, billable_storage_bytes=10)

        with caplog.at_level(logging.DEBUG):
            outcome = UsageReconciliationService(
                s3=FailingS3Service("AccessDenied")
            ).reconcile_tenant(session, tenant.id)

        assert outcome.error_code == "s3_access_denied", "error semantics unchanged"
        assert_no_secret_markers(rendered_log_text(caplog), "the reconciliation logs")

    def test_an_unexpected_accounting_error_is_logged_without_its_message(
        self, session, caplog, monkeypatch
    ):
        """The other half of the finding: the accounting phase's catch-all
        printed the traceback of whatever raised — including its message."""
        tenant = create_tenant(session)
        _init_usage(session, tenant.id, billable_storage_bytes=1)

        def _explode(*args, **kwargs):
            raise ClientError()

        monkeypatch.setattr(
            "app.services.storage_billing.StorageBillingService."
            "compute_billable_storage_bytes",
            staticmethod(_explode),
        )

        with caplog.at_level(logging.DEBUG):
            outcome = UsageReconciliationService(s3=FakeS3Service()).reconcile_tenant(
                session, tenant.id
            )

        assert outcome.status == "FAILED"
        assert outcome.error_code == "unexpected_error"
        assert_no_secret_markers(
            rendered_log_text(caplog), "the accounting failure logs"
        )

        failures = log_records_for(caplog, "usage_reconciliation.failed")
        assert any(
            getattr(r, "exception_type", None) == "ClientError"
            and getattr(r, "phase", None) == "accounting"
            and r.error_code == "unexpected_error"
            for r in failures
        ), "the unexpected failure must stay observable, just sanitized"

    def test_the_manual_endpoint_leaks_nothing_through_body_or_logs(
        self, client, session, caplog, monkeypatch
    ):
        tenant = create_tenant(session)
        admin = create_user(
            session, tenant, email="leak-admin@t.example", roles=("admin",)
        )
        _billable_official_pdf(session, tenant, size_bytes=100)
        _init_usage(session, tenant.id, billable_storage_bytes=100)
        monkeypatch.setattr(
            "app.services.usage_reconciliation.S3Service", FailingS3Service
        )

        with caplog.at_level(logging.DEBUG):
            resp = client.post(
                "/api/v1/tenant/usage/reconcile", headers=auth_headers(admin)
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "FAILED"
        assert body["error_code"] == "s3_unavailable"
        assert_no_secret_markers(resp.text, "the HTTP response")
        assert_no_secret_markers(rendered_log_text(caplog), "the request's logs")

    def test_no_reconciliation_module_prints_a_traceback(self):
        """A structural guard, not a behavioral one: these two modules
        handle exceptions whose payload may be external, so neither may
        call `logging.Logger.exception()` or pass `exc_info` at all. Every
        future failure path in them inherits the rule by default.

        Checked over the parsed AST rather than the raw text, so prose in a
        docstring (including this one) cannot satisfy or break it.
        """
        import app.services.usage_reconciliation as reconciliation_module
        import app.services.usage_reconciliation_worker as worker_module

        for module in (reconciliation_module, worker_module):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "exception":
                    raise AssertionError(
                        f"{module.__name__} line {node.lineno}: "
                        "an exception-printing log call"
                    )
                for keyword in node.keywords:
                    if keyword.arg == "exc_info":
                        raise AssertionError(
                            f"{module.__name__} line {node.lineno}: exc_info passed"
                        )
