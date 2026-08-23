"""Tenant-logo DB scope tests (Céluma 1.3, Phase 4, Block D).

Covers tenant-logo-db-scope-contract.md at the runtime level: uploading and
replacing a logo populates `Tenant.logo_storage_id`, billing reads the FK,
and nothing in the current-logo path parses `logo_url` or depends on
`MEDIA_PUBLIC_BASE_URL` any more. The migration-level backfill (including
the ambiguous and cross-tenant cases) is covered in
`tests/test_alembic_migrations.py::TestTenantLogoBackfill`.
"""
import ast
import io
import pathlib

from PIL import Image

from app.core.config import settings
from app.models.storage import StorageObject
from app.models.tenant import Tenant
from app.services.storage_billing import (
    StorageBillingService,
    resolve_current_tenant_logo_storage_object,
)
from tests.http.factories import auth_headers, create_tenant, create_user

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _png_bytes(size=(32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _upload(client, session, tenant, user, *, size=(32, 32)):
    resp = client.post(
        f"/api/v1/tenants/{tenant.id}/logo",
        files={"file": ("logo.png", _png_bytes(size), "image/png")},
        headers=auth_headers(user),
    )
    assert resp.status_code == 200, resp.text
    session.expire_all()
    return resp


class TestUploadPopulatesTheForeignKey:
    def test_upload_sets_logo_storage_id(self, client, session):
        tenant = create_tenant(session, name="T")
        user = create_user(session, tenant, email="u@t.example")

        _upload(client, session, tenant, user)

        session.refresh(tenant)
        assert tenant.logo_storage_id is not None
        storage = session.get(StorageObject, tenant.logo_storage_id)
        assert storage is not None
        assert storage.tenant_id == tenant.id
        assert storage.object_key.startswith(f"tenants/{tenant.id}/logo/")

    def test_logo_url_is_still_written_for_compatibility(self, client, session):
        tenant = create_tenant(session, name="T")
        user = create_user(session, tenant, email="u2@t.example")

        resp = _upload(client, session, tenant, user)

        session.refresh(tenant)
        assert tenant.logo_url == resp.json()["logo_url"]
        assert tenant.logo_url.startswith("https://")

    def test_billing_uses_the_fk(self, client, session):
        tenant = create_tenant(session, name="T")
        user = create_user(session, tenant, email="u3@t.example")

        _upload(client, session, tenant, user)

        session.refresh(tenant)
        storage = session.get(StorageObject, tenant.logo_storage_id)
        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.tenant_logo_bytes == storage.size_bytes

    def test_billing_still_finds_the_logo_after_the_cdn_base_url_changes(
        self, client, session, monkeypatch
    ):
        """The regression this block closes. Before Block D the current logo
        was recovered by stripping `MEDIA_PUBLIC_BASE_URL` off `logo_url`,
        so changing that setting silently made the logo unbillable."""
        tenant = create_tenant(session, name="T")
        user = create_user(session, tenant, email="u4@t.example")
        _upload(client, session, tenant, user)
        session.refresh(tenant)
        storage = session.get(StorageObject, tenant.logo_storage_id)

        monkeypatch.setattr(
            settings, "media_public_base_url", "https://a-totally-different-cdn.example"
        )

        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.tenant_logo_bytes == storage.size_bytes


class TestReplacement:
    def test_replacement_moves_the_fk_and_bills_only_the_new_logo(
        self, client, session
    ):
        tenant = create_tenant(session, name="T")
        user = create_user(session, tenant, email="r@t.example")

        _upload(client, session, tenant, user, size=(16, 16))
        session.refresh(tenant)
        logo_a = tenant.logo_storage_id
        assert logo_a is not None

        _upload(client, session, tenant, user, size=(200, 200))
        session.refresh(tenant)
        logo_b = tenant.logo_storage_id

        assert logo_b != logo_a
        storage_b = session.get(StorageObject, logo_b)
        breakdown = StorageBillingService.compute_breakdown(session, tenant.id)
        assert breakdown.tenant_logo_bytes == storage_b.size_bytes

        # The superseded row is retained (nothing deletes it) and simply
        # stops being billable — the ratified "only the current logo counts"
        # rule, now expressed by the FK rather than by URL matching.
        assert session.get(StorageObject, logo_a) is not None

    def test_replacement_decrements_the_outgoing_logo_in_the_counter(
        self, client, session
    ):
        from app.models.tenant_usage import TenantUsage
        from app.services.usage import UsageService

        tenant = create_tenant(session, name="T")
        user = create_user(session, tenant, email="r2@t.example")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=0)
        session.commit()

        _upload(client, session, tenant, user, size=(16, 16))
        after_first = session.get(TenantUsage, tenant.id).billable_storage_bytes

        _upload(client, session, tenant, user, size=(200, 200))
        session.expire_all()
        after_second = session.get(TenantUsage, tenant.id).billable_storage_bytes
        session.refresh(tenant)
        current = session.get(StorageObject, tenant.logo_storage_id)

        assert after_first > 0
        assert after_second == current.size_bytes


class TestResolution:
    def test_no_logo_resolves_to_none(self, session):
        tenant = create_tenant(session)
        assert resolve_current_tenant_logo_storage_object(session, tenant) is None

    def test_a_cross_tenant_reference_resolves_to_none(self, session):
        from tests.http.factories import create_storage_object

        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        b_logo = create_storage_object(
            session, key=f"tenants/{tenant_b.id}/logo/b.png", tenant=tenant_b
        )
        tenant_a.logo_storage_id = b_logo.id
        session.add(tenant_a)
        session.commit()

        assert resolve_current_tenant_logo_storage_object(session, tenant_a) is None


class TestNoUrlParsingRemains:
    """Structural guards: the point of Block D is that no runtime path can
    reach the current logo through a URL any more, so a future change
    cannot quietly reintroduce the dependency."""

    def test_the_url_to_key_helper_is_gone(self):
        import app.services.storage_billing as storage_billing

        assert not hasattr(storage_billing, "resolve_object_key_from_public_url")

    def test_storage_billing_does_not_read_the_cdn_settings(self):
        source = (BACKEND_ROOT / "app" / "services" / "storage_billing.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        for forbidden in (
            "media_public_base_url",
            "s3_bucket_name",
            "aws_region",
        ):
            assert forbidden not in attributes, (
                f"the billable calculation must not read settings.{forbidden}"
            )

    def test_storage_billing_never_reads_logo_url(self):
        source = (BACKEND_ROOT / "app" / "services" / "storage_billing.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        # Docstrings still discuss `logo_url` — the ban is on code reading it.
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "logo_url", (
                    "the billable calculation must resolve the current logo "
                    "through Tenant.logo_storage_id, never through logo_url"
                )

    def test_reconciliation_does_not_read_the_cdn_settings(self):
        source = (
            BACKEND_ROOT / "app" / "services" / "usage_reconciliation.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert "media_public_base_url" not in attributes
