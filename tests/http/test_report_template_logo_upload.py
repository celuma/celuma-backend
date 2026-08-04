"""HTTP integration tests for the report-template logo upload endpoint
(Céluma 1.3, Phase 2, Block D, Story D2)."""
from io import BytesIO

from PIL import Image
from sqlmodel import Session

from app.models.report import ReportTemplate
from app.models.storage import StorageObject

from .conftest import FakeS3Service
from .factories import auth_headers, create_tenant, create_user


def _create_template(session: Session, tenant, *, name: str = "Default") -> ReportTemplate:
    template = ReportTemplate(tenant_id=tenant.id, name=name, template_json={}, is_active=True)
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def _image_bytes(fmt: str = "PNG", size: tuple[int, int] = (100, 100)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=(10, 20, 30)).save(buf, format=fmt)
    return buf.getvalue()


def _upload(client, template_id, headers, *, filename="logo.png", content_type="image/png", data=None):
    data = data if data is not None else _image_bytes("PNG")
    return client.post(
        f"/api/v1/reports/templates/{template_id}/logo",
        files={"file": (filename, data, content_type)},
        headers=headers,
    )


class TestUploadTemplateLogo:
    def test_valid_png_is_accepted(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        resp = _upload(client, template.id, auth_headers(user))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["content_type"] == "image/png"
        assert body["size_bytes"] > 0
        assert body["url"].startswith("https://fake-cdn.example/")

        storage = session.get(StorageObject, body["storage_object_id"])
        assert storage is not None
        assert str(storage.tenant_id) == str(tenant.id)
        assert storage.object_key.startswith(f"report-templates/{template.id}/logos/")

    def test_valid_jpeg_is_accepted(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        resp = _upload(
            client, template.id, auth_headers(user),
            filename="logo.jpg", content_type="image/jpeg", data=_image_bytes("JPEG"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["content_type"] == "image/jpeg"

    def test_valid_webp_is_accepted(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        resp = _upload(
            client, template.id, auth_headers(user),
            filename="logo.webp", content_type="image/webp", data=_image_bytes("WEBP"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["content_type"] == "image/webp"

    def test_svg_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        resp = _upload(
            client, template.id, auth_headers(user),
            filename="logo.svg", content_type="image/svg+xml", data=svg,
        )
        assert resp.status_code == 400
        assert "not supported" in resp.text.lower() or "not allowed" in resp.text.lower() or "png" in resp.text.lower()

    def test_invalid_mime_type_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        resp = _upload(
            client, template.id, auth_headers(user),
            filename="doc.pdf", content_type="application/pdf", data=b"%PDF-1.4 fake",
        )
        assert resp.status_code == 400

    def test_empty_file_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        resp = _upload(client, template.id, auth_headers(user), data=b"")
        assert resp.status_code == 400
        assert "empty" in resp.text.lower()

    def test_file_too_large_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        oversized = _image_bytes("PNG", size=(1, 1)) + (b"\x00" * (5 * 1024 * 1024 + 1))
        resp = _upload(client, template.id, auth_headers(user), data=oversized)
        assert resp.status_code == 400
        assert "5mb" in resp.text.lower()

    def test_corrupt_image_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        resp = _upload(client, template.id, auth_headers(user), data=b"not a real png file at all")
        assert resp.status_code == 400
        assert "valid image" in resp.text.lower()

    def test_mismatched_content_type_is_rejected(self, client, session):
        """Declared image/png but the bytes are actually a JPEG."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        resp = _upload(
            client, template.id, auth_headers(user),
            filename="logo.png", content_type="image/png", data=_image_bytes("JPEG"),
        )
        assert resp.status_code == 400
        assert "does not match" in resp.text.lower()

    def test_oversized_dimensions_are_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        resp = _upload(client, template.id, auth_headers(user), data=_image_bytes("PNG", size=(4001, 10)))
        assert resp.status_code == 400
        assert "dimensions" in resp.text.lower()

    def test_cross_tenant_template_is_rejected(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        user_b = create_user(session, tenant_b, email="admin@b.example")
        template_a = _create_template(session, tenant_a)

        resp = _upload(client, template_a.id, auth_headers(user_b))
        assert resp.status_code == 404

    def test_missing_permission_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="viewer@t1.example", roles=[])
        template = _create_template(session, tenant)

        resp = _upload(client, template.id, auth_headers(user))
        assert resp.status_code == 403

    def test_compensates_when_db_commit_fails_after_s3_upload(self, client, session, monkeypatch):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        original_commit = Session.commit
        call_count = {"n": 0}

        def failing_commit(self):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("Simulated DB outage after S3 upload succeeded")
            return original_commit(self)

        monkeypatch.setattr(Session, "commit", failing_commit)

        resp = _upload(client, template.id, auth_headers(user))
        assert resp.status_code == 500
        # The orphaned S3 object must have been deleted (best-effort compensation).
        assert len(FakeS3Service.store) == 0
