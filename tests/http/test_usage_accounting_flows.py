"""Flow-integration tests for incremental storage usage accounting
(Céluma 1.3, Phase 4, Block C).

Covers storage-flow-accounting-matrix.md end to end, through the real HTTP
endpoints wherever practical: for every billable creation/delete/replace
path, asserts both the resulting `StorageObject.tenant_id` (§21's
structural attribution regression) and the resulting `TenantUsage.
billable_storage_bytes` delta (§31). Also covers the legacy-PDF and
report-JSON-in-place-rewrite special cases (§15-16) and DB-transaction
rollback behavior (§32).
"""
import io
import json

from PIL import Image
from sqlmodel import select

from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion
from app.models.storage import StorageObject
from app.models.tenant_usage import TenantUsage
from app.models.user import AppUser
from app.services.usage import UsageService
from tests.http.conftest import FakeS3Service, make_pdf_bytes
from tests.http.factories import (
    auth_headers,
    create_branch,
    create_letterhead,
    create_order,
    create_report,
    create_sample,
    create_tenant,
    create_user,
)


def _jpeg_bytes(color=(200, 50, 50), size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


def _usage(session, tenant_id) -> int:
    row = session.get(TenantUsage, tenant_id)
    return row.billable_storage_bytes if row else None


def _init_usage(session, tenant_id):
    UsageService.initialize_usage(session, tenant_id, billable_storage_bytes=0)
    session.commit()


class TestSampleImageAccounting:
    def test_upload_populates_tenant_id_and_increments_usage(self, client, session):
        tenant = create_tenant(session, name="Lab")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        user = create_user(session, tenant, email="u@t.example")
        _init_usage(session, tenant.id)

        resp = client.post(
            f"/api/v1/laboratory/samples/{sample.id}/images",
            files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        sample_image_id = resp.json()["sample_image_id"]

        from app.models.laboratory import SampleImage
        from app.models.storage import SampleImageRendition

        image = session.get(SampleImage, sample_image_id)
        processed_storage = session.get(StorageObject, image.storage_id)
        assert processed_storage.tenant_id == tenant.id

        renditions = session.exec(
            select(SampleImageRendition).where(
                SampleImageRendition.sample_image_id == sample_image_id
            )
        ).all()
        rendition_storages = [session.get(StorageObject, r.storage_id) for r in renditions]
        for rs in rendition_storages:
            assert rs.tenant_id == tenant.id

        expected_delta = (processed_storage.size_bytes or 0) + sum(
            rs.size_bytes or 0 for rs in rendition_storages
        )
        assert _usage(session, tenant.id) == expected_delta

    def test_delete_decrements_usage_by_deleted_bytes(self, client, session):
        tenant = create_tenant(session, name="Lab2")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        user = create_user(session, tenant, email="u2@t.example")
        _init_usage(session, tenant.id)

        resp = client.post(
            f"/api/v1/laboratory/samples/{sample.id}/images",
            files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
            headers=auth_headers(user),
        )
        image_id = resp.json()["sample_image_id"]
        usage_after_upload = _usage(session, tenant.id)
        assert usage_after_upload > 0

        del_resp = client.delete(
            f"/api/v1/laboratory/samples/{sample.id}/images/{image_id}",
            headers=auth_headers(user),
        )
        assert del_resp.status_code == 200, del_resp.text
        assert _usage(session, tenant.id) == 0

    def test_usage_never_goes_negative_on_double_delete_race_shape(self, session):
        """Direct service-level floor check — the DB-level guarantee behind
        the HTTP delete flow (see TestAdjustStorage in test_usage_service.py
        for the exhaustive version)."""
        tenant = create_tenant(session, name="Lab3")
        _init_usage(session, tenant.id)
        UsageService.decrement_storage(session, tenant.id, 10_000, source="test")
        session.commit()
        assert _usage(session, tenant.id) == 0


class TestOfficialPdfAccounting:
    def test_generation_populates_tenant_id_and_increments_usage(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session, name="T")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="u@t.example")
        report, version = create_report(
            session, tenant, branch, order, status=ReportStatus.APPROVED
        )
        _init_usage(session, tenant.id)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(user)
        )
        assert resp.status_code == 200, resp.text

        session.refresh(version)
        storage = session.get(StorageObject, version.pdf_storage_id)
        assert storage.tenant_id == tenant.id
        assert storage.sha256_hex is not None
        assert _usage(session, tenant.id) == storage.size_bytes

    def test_historical_official_pdf_remains_billable_after_regeneration(
        self, session, stub_pdf_render
    ):
        """`force=True` regeneration is service-level only (used by
        sign_and_publish, never a public query param) — call the service
        directly to prove the *previous* artifact stays billable."""
        from app.services.report_pdf_generation import ReportPdfGenerationService

        tenant = create_tenant(session, name="T2")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="u2@t.example")
        report, version = create_report(
            session, tenant, branch, order, status=ReportStatus.APPROVED
        )
        _init_usage(session, tenant.id)

        service = ReportPdfGenerationService(session)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        service.generate(report, version, user.id)
        first_total = _usage(session, tenant.id)
        assert first_total > 0

        stub_pdf_render.succeed(make_pdf_bytes(2))
        service.generate(report, version, user.id, force=True)
        second_total = _usage(session, tenant.id)
        assert second_total > first_total  # old artifact's bytes still counted


class TestLegacyPdfAccounting:
    def test_first_upload_populates_tenant_id_and_increments(self, client, session):
        tenant = create_tenant(session, name="T")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="u@t.example")
        report, version = create_report(session, tenant, branch, order)
        _init_usage(session, tenant.id)

        resp = client.post(
            f"/api/v1/reports/{report.id}/versions/1/pdf",
            files={"file": ("r.pdf", make_pdf_bytes(1), "application/pdf")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text

        session.refresh(version)
        storage = session.get(StorageObject, version.pdf_storage_id)
        assert storage.tenant_id == tenant.id
        assert _usage(session, tenant.id) == storage.size_bytes

    def test_replacement_applies_delta_not_double_count(self, client, session):
        """The exact §15 scenario: 10MB-equivalent upload, then a larger,
        then a smaller replacement — final usage reflects only the last
        upload's size, not the sum of all three."""
        tenant = create_tenant(session, name="T2")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="u2@t.example")
        report, version = create_report(session, tenant, branch, order)
        _init_usage(session, tenant.id)

        small = make_pdf_bytes(1)
        big = make_pdf_bytes(1) + b"0" * 5000  # still a valid %PDF prefix, just padded larger
        medium = make_pdf_bytes(1) + b"0" * 1000

        client.post(
            f"/api/v1/reports/{report.id}/versions/1/pdf",
            files={"file": ("r.pdf", small, "application/pdf")},
            headers=auth_headers(user),
        )
        first_size = _usage(session, tenant.id)

        client.post(
            f"/api/v1/reports/{report.id}/versions/1/pdf",
            files={"file": ("r.pdf", big, "application/pdf")},
            headers=auth_headers(user),
        )
        second_size = _usage(session, tenant.id)
        assert second_size > first_size

        client.post(
            f"/api/v1/reports/{report.id}/versions/1/pdf",
            files={"file": ("r.pdf", medium, "application/pdf")},
            headers=auth_headers(user),
        )
        final_size = _usage(session, tenant.id)

        session.refresh(version)
        current_storage = session.get(StorageObject, version.pdf_storage_id)
        # Final usage must equal exactly the currently-referenced PDF's size
        # — not the sum of all three uploads.
        assert final_size == current_storage.size_bytes
        assert final_size < first_size + second_size


class TestReportJsonAccounting:
    def test_create_report_populates_tenant_id_and_increments(self, client, session):
        tenant = create_tenant(session, name="T")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="u@t.example")
        _init_usage(session, tenant.id)

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": {"foo": "bar"},
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        report_id = resp.json()["id"]

        version = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == report_id)
        ).first()
        storage = session.get(StorageObject, version.json_storage_id)
        assert storage.tenant_id == tenant.id
        assert _usage(session, tenant.id) == storage.size_bytes

    def test_in_place_rewrite_applies_size_delta(self, session):
        """Direct service-level test of embed_signature_metadata_if_required
        — the one write path that mutates size_bytes on an existing row
        rather than creating a new one (§16)."""
        from app.services.report_publishing import embed_signature_metadata_if_required
        from app.services.s3 import S3Service

        tenant = create_tenant(session, name="T")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        reviewer = create_user(session, tenant, email="rev@t.example", roles=("reviewer",))
        report, version = create_report(session, tenant, branch, order)
        _init_usage(session, tenant.id)

        import app.services.report_publishing as report_publishing_module

        original_s3 = report_publishing_module.S3Service
        report_publishing_module.S3Service = FakeS3Service
        try:
            small_body = {"signatureMetadata": {"require_digital_signature": True}}
            small_bytes = json.dumps(small_body).encode("utf-8")
            s3 = FakeS3Service()
            key = f"reports/{tenant.id}/{branch.id}/{report.id}/versions/1/report.json"
            info = s3.upload_bytes(small_bytes, key=key, content_type="application/json")
            storage = StorageObject(
                provider="aws", region=s3.region, bucket=info.bucket, object_key=info.key,
                content_type="application/json", size_bytes=info.size_bytes, tenant_id=tenant.id,
            )
            session.add(storage)
            session.flush()
            version.json_storage_id = storage.id
            session.add(version)
            session.commit()
            initial_usage = UsageService.increment_storage(
                session, tenant.id, storage.size_bytes, source="test"
            )
            session.commit()

            sig_storage = StorageObject(
                provider="aws", region=s3.region, bucket="b", object_key="sig.png",
                content_type="image/png", size_bytes=64, tenant_id=tenant.id,
            )
            session.add(sig_storage)
            session.flush()
            reviewer.signature_storage_id = sig_storage.id
            session.add(reviewer)
            session.commit()

            embed_signature_metadata_if_required(session, str(report.id), version, reviewer)

            session.refresh(storage)
            final_usage = _usage(session, tenant.id)
            expected = initial_usage + (storage.size_bytes - info.size_bytes)
            assert final_usage == expected
            # The rewritten JSON is strictly larger (embeds a signature URL),
            # so this also proves it wasn't double-counted as a fresh object.
            assert storage.size_bytes > info.size_bytes
        finally:
            report_publishing_module.S3Service = original_s3


class TestTenantLogoAccounting:
    def test_upload_increments_usage(self, client, session):
        tenant = create_tenant(session, name="T")
        user = create_user(session, tenant, email="u@t.example")
        _init_usage(session, tenant.id)

        resp = client.post(
            f"/api/v1/tenants/{tenant.id}/logo",
            files={"file": ("logo.png", _png_bytes(), "image/png")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        assert _usage(session, tenant.id) > 0

    def test_replacement_excludes_superseded_logo(self, client, session):
        tenant = create_tenant(session, name="T2")
        user = create_user(session, tenant, email="u2@t.example")
        _init_usage(session, tenant.id)

        client.post(
            f"/api/v1/tenants/{tenant.id}/logo",
            files={"file": ("a.png", _png_bytes((10, 10)), "image/png")},
            headers=auth_headers(user),
        )
        after_first = _usage(session, tenant.id)

        resp = client.post(
            f"/api/v1/tenants/{tenant.id}/logo",
            files={"file": ("b.png", _png_bytes((200, 200)), "image/png")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        after_second = _usage(session, tenant.id)

        session.refresh(tenant)
        from app.services.storage_billing import resolve_current_tenant_logo_storage_object

        current_logo = resolve_current_tenant_logo_storage_object(session, tenant)
        assert after_second == current_logo.size_bytes
        assert after_second != after_first + current_logo.size_bytes


def _png_bytes(size=(32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class TestLetterheadAssetAccounting:
    def test_upload_increments_usage_and_sets_tenant_id(self, client, session):
        tenant = create_tenant(session, name="T")
        user = create_user(session, tenant, email="u@t.example")
        letterhead = create_letterhead(session, tenant)
        _init_usage(session, tenant.id)

        resp = client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/logo",
            files={"file": ("logo.png", _png_bytes(), "image/png")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        storage_id = resp.json()["storage_object_id"]
        storage = session.get(StorageObject, storage_id)
        assert storage.tenant_id == tenant.id
        assert _usage(session, tenant.id) == storage.size_bytes


class TestSignatureAccounting:
    def test_upload_populates_tenant_id_and_increments(self, client, session):
        tenant = create_tenant(session, name="T")
        reviewer = create_user(session, tenant, email="rev@t.example", roles=("reviewer",))
        _init_usage(session, tenant.id)

        resp = client.post(
            "/api/v1/users/me/signature",
            files={"file": ("sig.png", _png_bytes(), "image/png")},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 200, resp.text

        session.refresh(reviewer)
        storage = session.get(StorageObject, reviewer.signature_storage_id)
        assert storage.tenant_id == tenant.id
        assert _usage(session, tenant.id) == storage.size_bytes

    def test_replace_applies_delta_and_only_live_signature_counts(self, client, session):
        tenant = create_tenant(session, name="T2")
        reviewer = create_user(session, tenant, email="rev2@t.example", roles=("reviewer",))
        _init_usage(session, tenant.id)

        client.post(
            "/api/v1/users/me/signature",
            files={"file": ("a.png", _png_bytes((10, 10)), "image/png")},
            headers=auth_headers(reviewer),
        )
        after_first = _usage(session, tenant.id)

        client.post(
            "/api/v1/users/me/signature",
            files={"file": ("b.png", _png_bytes((100, 100)), "image/png")},
            headers=auth_headers(reviewer),
        )
        session.refresh(reviewer)
        after_second = _usage(session, tenant.id)
        live_storage = session.get(StorageObject, reviewer.signature_storage_id)
        assert after_second == live_storage.size_bytes
        assert after_second != after_first + live_storage.size_bytes

    def test_delete_decrements_to_zero(self, client, session):
        tenant = create_tenant(session, name="T3")
        reviewer = create_user(session, tenant, email="rev3@t.example", roles=("reviewer",))
        _init_usage(session, tenant.id)

        client.post(
            "/api/v1/users/me/signature",
            files={"file": ("a.png", _png_bytes(), "image/png")},
            headers=auth_headers(reviewer),
        )
        assert _usage(session, tenant.id) > 0

        resp = client.delete("/api/v1/users/me/signature", headers=auth_headers(reviewer))
        assert resp.status_code == 204
        assert _usage(session, tenant.id) == 0


class TestRollbackBehavior:
    def test_sample_image_partial_upload_failure_leaves_usage_unchanged(self, client, session):
        """FakeS3Service.fail_next_upload makes the *second* of the three S3
        uploads in upload_sample_image raise — the whole request fails
        before any StorageObject/usage commit happens, so usage must be
        exactly where it started (see storage-mutation-flow-inventory.md
        §1: no compensation exists for this flow; the DB side, including
        the counter, must simply never have committed)."""
        tenant = create_tenant(session, name="T")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        user = create_user(session, tenant, email="u@t.example")
        _init_usage(session, tenant.id)

        FakeS3Service.fail_next_upload = True
        # First upload succeeds internally? No — upload_sample_image calls
        # upload_bytes for processed, then thumbnail, then (maybe) raw.
        # fail_next_upload fires on the *next* call, so this fails the
        # processed-image upload — before ANY StorageObject is created. The
        # endpoint has no try/except around the S3 calls (confirmed in
        # storage-mutation-flow-inventory.md §1: no compensation exists for
        # this flow), so the exception propagates through the ASGI stack
        # unhandled — TestClient re-raises it rather than returning a 5xx.
        import pytest

        with pytest.raises(RuntimeError):
            client.post(
                f"/api/v1/laboratory/samples/{sample.id}/images",
                files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
                headers=auth_headers(user),
            )

        assert _usage(session, tenant.id) == 0

    def test_official_pdf_rollback_leaves_usage_unchanged(
        self, client, session, stub_pdf_render, monkeypatch
    ):
        """Inject a failure between the StorageObject insert and the final
        commit inside ReportPdfGenerationService._persist — both the
        StorageObject and the usage adjustment must roll back together."""
        tenant = create_tenant(session, name="T")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="u@t.example")
        report, version = create_report(
            session, tenant, branch, order, status=ReportStatus.APPROVED
        )
        _init_usage(session, tenant.id)

        from app.services import report_pdf_generation as pdf_gen_module

        original_commit = pdf_gen_module.Session.commit
        call_count = {"n": 0}

        def _flaky_commit(self):
            call_count["n"] += 1
            # Call #1 is generate()'s own "status=GENERATING" commit —
            # unrelated to storage/usage. Call #2 is the one inside
            # _persist(), immediately after the StorageObject insert and
            # the usage adjustment: that is the boundary this test targets.
            if call_count["n"] == 2:
                raise RuntimeError("Simulated DB failure before commit")
            return original_commit(self)

        monkeypatch.setattr(pdf_gen_module.Session, "commit", _flaky_commit)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(user)
        )
        # _persist's own try/except converts the commit failure into a
        # ReportPdfGenerationError, which the endpoint maps to 422.
        assert resp.status_code == 422, resp.text

        # Both the StorageObject insert and the usage adjustment must have
        # rolled back together — no orphaned counter change for a row that
        # no longer exists.
        session.refresh(version)
        assert version.pdf_storage_id is None
        assert _usage(session, tenant.id) == 0
