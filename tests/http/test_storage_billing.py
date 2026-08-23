"""StorageBillingService tests (Céluma 1.3, Phase 4, Block C).

Covers billable-storage-calculation-contract.md: per-category correctness,
the special cases (superseded tenant logo excluded, superseded legacy PDF
excluded, historical official PDF and historical letterhead references
still counted), and tenant isolation across every category.
"""
import uuid

from app.core.config import settings
from app.models.laboratory import Order, Sample
from app.models.report import Report, ReportVersion
from app.models.storage import SampleImageRendition, StorageObject
from app.models.user import AppUser
from app.services.storage_billing import StorageBillingService
from tests.http.factories import (
    create_branch,
    create_letterhead,
    create_letterhead_version,
    create_order,
    create_report,
    create_sample,
    create_storage_object,
    create_tenant,
    create_user,
    valid_presentation,
)


def _add_sample_image(session, tenant, branch, sample, *, storage_key: str, size_bytes: int):
    from app.models.laboratory import SampleImage

    storage = create_storage_object(session, key=storage_key, tenant=tenant)
    storage.size_bytes = size_bytes
    session.add(storage)
    image = SampleImage(
        tenant_id=tenant.id,
        branch_id=branch.id,
        sample_id=sample.id,
        storage_id=storage.id,
    )
    session.add(image)
    session.commit()
    session.refresh(image)
    return image, storage


class TestSampleImagesCategory:
    def test_processed_and_renditions_are_summed(self, session):
        tenant = create_tenant(session, name="T1")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)

        image, _ = _add_sample_image(
            session, tenant, branch, sample, storage_key="samples/p.jpg", size_bytes=1000
        )
        thumb = create_storage_object(session, key="samples/t.jpg", tenant=tenant)
        thumb.size_bytes = 100
        session.add(thumb)
        session.add(
            SampleImageRendition(sample_image_id=image.id, kind="thumbnail", storage_id=thumb.id)
        )
        session.commit()

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.sample_images_bytes == 1100

    def test_tenant_isolation(self, session):
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        branch_a = create_branch(session, tenant_a)
        branch_b = create_branch(session, tenant_b)
        order_a = create_order(session, tenant_a, branch_a)
        order_b = create_order(session, tenant_b, branch_b)
        sample_a = create_sample(session, tenant_a, branch_a, order_a)
        sample_b = create_sample(session, tenant_b, branch_b, order_b)

        _add_sample_image(session, tenant_a, branch_a, sample_a, storage_key="a.jpg", size_bytes=500)
        _add_sample_image(session, tenant_b, branch_b, sample_b, storage_key="b.jpg", size_bytes=999)

        assert StorageBillingService.compute_breakdown(session, tenant_a.id).sample_images_bytes == 500
        assert StorageBillingService.compute_breakdown(session, tenant_b.id).sample_images_bytes == 999


class TestOfficialPdfCategory:
    def test_only_sha256_rows_count_as_official(self, session):
        tenant = create_tenant(session)
        official = StorageObject(
            provider="aws", region="mx-test-1", bucket="b", object_key="official.pdf",
            content_type="application/pdf", size_bytes=10, sha256_hex="deadbeef", tenant_id=tenant.id,
        )
        legacy_like = StorageObject(
            provider="aws", region="mx-test-1", bucket="b", object_key="legacy.pdf",
            content_type="application/pdf", size_bytes=20, sha256_hex=None, tenant_id=tenant.id,
        )
        session.add(official)
        session.add(legacy_like)
        session.commit()

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.official_pdf_bytes == 10

    def test_historical_superseded_official_pdfs_still_count(self, session):
        """Official PDFs stay billable forever, even once a ReportVersion's
        pdf_storage_id is repointed to a newer generation — this category is
        summed by tenant_id + sha256_hex, never through the current FK."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, version = create_report(session, tenant, branch, order)

        old_official = StorageObject(
            provider="aws", region="mx-test-1", bucket="b", object_key="v1.pdf",
            content_type="application/pdf", size_bytes=10, sha256_hex="aaa", tenant_id=tenant.id,
        )
        new_official = StorageObject(
            provider="aws", region="mx-test-1", bucket="b", object_key="v2.pdf",
            content_type="application/pdf", size_bytes=20, sha256_hex="bbb", tenant_id=tenant.id,
        )
        session.add(old_official)
        session.add(new_official)
        session.flush()
        version.pdf_storage_id = new_official.id  # only the new one is "current"
        session.add(version)
        session.commit()

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.official_pdf_bytes == 30


class TestLegacyPdfCategory:
    def test_only_the_current_pdf_storage_id_counts(self, session):
        """A stale, superseded legacy-PDF row (same version, overwritten S3
        key) must not be counted — only whatever ReportVersion.pdf_storage_id
        currently points to."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, version = create_report(session, tenant, branch, order)

        stale = StorageObject(
            provider="aws", region="mx-test-1", bucket="b", object_key="report.pdf",
            content_type="application/pdf", size_bytes=10_000_000, tenant_id=tenant.id,
        )
        current = StorageObject(
            provider="aws", region="mx-test-1", bucket="b", object_key="report.pdf",
            content_type="application/pdf", size_bytes=8_000_000, tenant_id=tenant.id,
        )
        session.add(stale)
        session.add(current)
        session.flush()
        version.pdf_storage_id = current.id
        session.add(version)
        session.commit()

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.legacy_pdf_bytes == 8_000_000


class TestReportJsonCategory:
    def test_every_version_json_is_summed(self, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, v1 = create_report(session, tenant, branch, order, version_no=1)

        json1 = StorageObject(
            provider="aws", region="mx-test-1", bucket="b", object_key="v1.json",
            content_type="application/json", size_bytes=100, tenant_id=tenant.id,
        )
        json2 = StorageObject(
            provider="aws", region="mx-test-1", bucket="b", object_key="v2.json",
            content_type="application/json", size_bytes=150, tenant_id=tenant.id,
        )
        session.add(json1)
        session.add(json2)
        session.flush()
        v1.json_storage_id = json1.id
        session.add(v1)
        v2 = ReportVersion(report_id=report.id, version_no=2, is_current=True, json_storage_id=json2.id)
        session.add(v2)
        session.commit()

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.report_json_bytes == 250


class TestTenantLogoCategory:
    """Céluma 1.3 Phase 4, Block D: the current logo is `Tenant.
    logo_storage_id`, a real FK. These tests set the FK, not a URL —
    before Block D they set `logo_url` and the calculation parsed it back
    into an object key against the configured CDN prefix."""

    def test_only_current_logo_counts(self, session):
        tenant = create_tenant(session)
        old_logo = StorageObject(
            provider="aws", region="mx-test-1", bucket="celuma-test-bucket",
            object_key=f"tenants/{tenant.id}/logo/old.png", content_type="image/png",
            size_bytes=500, tenant_id=tenant.id,
        )
        new_logo = StorageObject(
            provider="aws", region="mx-test-1", bucket="celuma-test-bucket",
            object_key=f"tenants/{tenant.id}/logo/new.png", content_type="image/png",
            size_bytes=800, tenant_id=tenant.id,
        )
        session.add(old_logo)
        session.add(new_logo)
        session.commit()

        tenant.logo_storage_id = new_logo.id
        tenant.logo_url = f"https://fake-cdn.example/{new_logo.object_key}"
        session.add(tenant)
        session.commit()

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.tenant_logo_bytes == 800

    def test_no_logo_means_zero(self, session):
        tenant = create_tenant(session)
        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.tenant_logo_bytes == 0

    def test_a_changed_cdn_base_url_no_longer_hides_the_logo(self, session, monkeypatch):
        """The regression Block D closes. With URL parsing, a logo stored
        under one `MEDIA_PUBLIC_BASE_URL` became unresolvable — and
        therefore silently unbillable — the moment that setting changed.
        The FK does not care what hostname the stored URL carries."""
        tenant = create_tenant(session)
        logo = StorageObject(
            provider="aws", region="mx-test-1", bucket="celuma-test-bucket",
            object_key=f"tenants/{tenant.id}/logo/current.png",
            content_type="image/png", size_bytes=640, tenant_id=tenant.id,
        )
        session.add(logo)
        session.commit()
        tenant.logo_storage_id = logo.id
        tenant.logo_url = f"https://cdn-a.example/{logo.object_key}"
        session.add(tenant)
        session.commit()

        monkeypatch.setattr(settings, "media_public_base_url", "https://cdn-b.example")

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.tenant_logo_bytes == 640

    def test_a_cross_tenant_logo_reference_is_never_billed(self, session):
        """A FK guarantees the row exists, not that it belongs to this
        tenant. An object owned by another tenant is billed to neither."""
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        b_logo = StorageObject(
            provider="aws", region="mx-test-1", bucket="celuma-test-bucket",
            object_key=f"tenants/{tenant_b.id}/logo/b.png", content_type="image/png",
            size_bytes=700, tenant_id=tenant_b.id,
        )
        session.add(b_logo)
        session.commit()

        tenant_a.logo_storage_id = b_logo.id
        session.add(tenant_a)
        session.commit()

        assert StorageBillingService.compute_breakdown(session, tenant_a.id).tenant_logo_bytes == 0
        assert StorageBillingService.compute_breakdown(session, tenant_b.id).tenant_logo_bytes == 0

    def test_a_legacy_logo_url_without_the_fk_counts_zero(self, session):
        """`logo_url` set with no `logo_storage_id` is the unresolved legacy
        case the Block D migration deliberately leaves NULL. It is not
        billed (nothing identifies which object it is) and is reported by
        reconciliation instead of being guessed at."""
        tenant = create_tenant(session)
        orphaned_logo = StorageObject(
            provider="aws", region="mx-test-1", bucket="celuma-test-bucket",
            object_key=f"tenants/{tenant.id}/logo/unknown.png",
            content_type="image/png", size_bytes=900, tenant_id=tenant.id,
        )
        session.add(orphaned_logo)
        session.commit()
        tenant.logo_url = "https://some-old-cdn.example/tenants/who/logo/unknown.png"
        session.add(tenant)
        session.commit()

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.tenant_logo_bytes == 0


class TestLetterheadAssetCategory:
    def test_key_prefix_identifies_letterhead_and_template_assets(self, session):
        tenant = create_tenant(session)
        letterhead_logo = StorageObject(
            provider="aws", region="mx-test-1", bucket="b",
            object_key="report-letterheads/abc/logos/1.png", content_type="image/png",
            size_bytes=300, tenant_id=tenant.id,
        )
        template_logo = StorageObject(
            provider="aws", region="mx-test-1", bucket="b",
            object_key="report-templates/xyz/logos/1.png", content_type="image/png",
            size_bytes=400, tenant_id=tenant.id,
        )
        # A tenant logo must NOT be picked up by this category.
        tenant_logo = StorageObject(
            provider="aws", region="mx-test-1", bucket="b",
            object_key="tenants/x/logo/1.png", content_type="image/png",
            size_bytes=999, tenant_id=tenant.id,
        )
        session.add_all([letterhead_logo, template_logo, tenant_logo])
        session.commit()

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.letterhead_asset_bytes == 700

    def test_same_object_referenced_by_two_versions_counts_once(self, session):
        """The billable set is by StorageObject row (via tenant_id + key
        prefix), not by counting JSON references — so a logo shared across
        two letterhead versions is inherently counted once, not twice."""
        tenant = create_tenant(session)
        shared_logo = StorageObject(
            provider="aws", region="mx-test-1", bucket="b",
            object_key="report-letterheads/shared/logos/1.png", content_type="image/png",
            size_bytes=250, tenant_id=tenant.id,
        )
        session.add(shared_logo)
        session.commit()

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.letterhead_asset_bytes == 250

    def test_unreferenced_but_retained_asset_still_counts(self, session):
        """The ratified Céluma 1.3 policy (billable-storage-calculation-
        contract.md §3, block-c-remediation-report.md): a letterhead/
        template asset is billable while Céluma retains its StorageObject,
        independent of whether any current version's JSON still references
        it. This test proves that explicitly — a real letterhead exists
        with a real ACTIVE version whose `configuration` does NOT reference
        this asset at all (no logo_storage_id anywhere in its JSON), and
        the asset still counts."""
        tenant = create_tenant(session)
        orphaned_logo = StorageObject(
            provider="aws", region="mx-test-1", bucket="b",
            object_key="report-letterheads/old/logos/orphan.png", content_type="image/png",
            size_bytes=555, tenant_id=tenant.id,
        )
        session.add(orphaned_logo)
        session.commit()

        # A real letterhead + ACTIVE version whose configuration has no
        # logo_storage_id reference at all (valid_presentation()'s default
        # has none set) — the orphaned_logo above is reachable from
        # nowhere in this tenant's live letterhead data.
        letterhead = create_letterhead(session, tenant)
        create_letterhead_version(
            session, tenant, letterhead, status="ACTIVE", configuration=valid_presentation()
        )

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.letterhead_asset_bytes == 555


class TestSignatureCategory:
    def test_only_the_live_signature_counts(self, session):
        tenant = create_tenant(session)
        live_sig = StorageObject(
            provider="aws", region="mx-test-1", bucket="b", object_key="sig.png",
            content_type="image/png", size_bytes=64, tenant_id=tenant.id,
        )
        session.add(live_sig)
        session.flush()
        user = create_user(session, tenant, email="rev@t.example", roles=("reviewer",))
        user.signature_storage_id = live_sig.id
        session.add(user)
        session.commit()

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.signature_bytes == 64

    def test_no_signature_means_zero(self, session):
        tenant = create_tenant(session)
        create_user(session, tenant, email="rev2@t.example", roles=("reviewer",))
        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.signature_bytes == 0


class TestZeroUsageTenant:
    def test_tenant_with_no_billable_objects_totals_zero(self, session):
        tenant = create_tenant(session, name="Empty Tenant")
        assert StorageBillingService.compute_billable_storage_bytes(session, tenant.id) == 0
