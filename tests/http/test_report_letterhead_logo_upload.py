"""HTTP integration tests for the letterhead logo upload endpoint —
post-Phase-2 remediation, R6/R15. Mirrors test_report_template_logo_upload.py
since both now share ManagedTenantImageService; this file focuses on what's
specific to the letterhead endpoint (route, key prefix, tenant isolation)
rather than re-proving every validation rule already covered there."""
from io import BytesIO

from PIL import Image

from app.models.storage import StorageObject

from .factories import auth_headers, create_letterhead, create_tenant, create_user


def _image_bytes(fmt: str = "PNG", size: tuple[int, int] = (100, 100)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=(10, 20, 30)).save(buf, format=fmt)
    return buf.getvalue()


def _upload(client, letterhead_id, headers, *, filename="logo.png", content_type="image/png", data=None):
    data = data if data is not None else _image_bytes("PNG")
    return client.post(
        f"/api/v1/report-letterheads/{letterhead_id}/logo",
        files={"file": (filename, data, content_type)},
        headers=headers,
    )


class TestUploadLetterheadLogo:
    def test_valid_png_is_accepted(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant)

        resp = _upload(client, letterhead.id, auth_headers(user))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["content_type"] == "image/png"
        # Post-Phase-2 remediation: confirms the URL resolution path (which
        # historically was hypothesized-but-not-verified to be broken for
        # BLOCK_ALL buckets) — in tests this is FakeS3Service.object_public_url,
        # proving the letterhead endpoint uses the same resolution call as
        # the template-logo endpoint, not some divergent implementation.
        assert body["url"].startswith("https://fake-cdn.example/")

        storage = session.get(StorageObject, body["storage_object_id"])
        assert storage is not None
        assert str(storage.tenant_id) == str(tenant.id)
        assert storage.object_key.startswith(f"report-letterheads/{letterhead.id}/logos/")

    def test_svg_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant)

        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        resp = _upload(
            client, letterhead.id, auth_headers(user),
            filename="logo.svg", content_type="image/svg+xml", data=svg,
        )
        assert resp.status_code == 400

    def test_cross_tenant_letterhead_is_rejected(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        user_b = create_user(session, tenant_b, email="admin@b.example")
        letterhead_a = create_letterhead(session, tenant_a)

        resp = _upload(client, letterhead_a.id, auth_headers(user_b))
        assert resp.status_code == 404

    def test_missing_permission_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="viewer@t1.example", roles=("viewer",))
        letterhead = create_letterhead(session, tenant)

        resp = _upload(client, letterhead.id, auth_headers(user))
        assert resp.status_code == 403


class TestUploadTenantLogo:
    """Post-Phase-2 remediation: tenant-logo upload had no dedicated test
    file before this remediation (confirmed during inventory) — it now
    shares ManagedTenantImageService with the same validation strength as
    template/letterhead logos, closing that gap."""

    def test_valid_png_is_accepted_and_resolves_via_shared_service(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example", roles=("admin",))

        resp = client.post(
            f"/api/v1/tenants/{tenant.id}/logo",
            files={"file": ("logo.png", _image_bytes("PNG"), "image/png")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["logo_url"].startswith("https://fake-cdn.example/")

        session.refresh(tenant)
        assert tenant.logo_url == resp.json()["logo_url"]

    def test_svg_is_rejected(self, client, session):
        """Before this remediation, upload_tenant_logo only did a substring
        content-type check and would have accepted this — now it shares
        ManagedTenantImageService's real MIME/format validation."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example", roles=("admin",))

        resp = client.post(
            f"/api/v1/tenants/{tenant.id}/logo",
            files={
                "file": (
                    "logo.svg",
                    b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
                    "image/svg+xml",
                )
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 400

    def test_corrupt_image_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example", roles=("admin",))

        resp = client.post(
            f"/api/v1/tenants/{tenant.id}/logo",
            files={"file": ("logo.png", b"not a real image", "image/png")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400
