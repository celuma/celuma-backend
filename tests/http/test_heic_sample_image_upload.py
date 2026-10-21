"""HEIC sample-image upload over HTTP (Céluma 1.4, #21).

Real HEIC bytes go through the real endpoint, decoder and accounting; only the
object store is the suite's in-memory fake. Complements
tests/test_heic_image_processing.py (decoder behaviour) with what only the
endpoint can prove: authority and tenancy before any effect, byte limits,
storage/usage accounting, and cleanup when storage or the database fails.
"""
import io

import pytest
from PIL import Image
from sqlmodel import Session, select

from app.api.v1 import laboratory
from app.models.events import OrderEvent
from app.models.laboratory import SampleImage
from app.models.enums import EventType, SampleState
from app.models.storage import StorageObject
from app.models.tenant_usage import TenantUsage
from app.services.usage import UsageService
from tests.heic_fixtures import heic_bytes, heic_multi_image, with_brand
from tests.http.conftest import FakeS3Service
from tests.http.factories import (
    auth_headers,
    create_branch,
    create_order,
    create_sample,
    create_tenant,
    create_user,
)

MIB = 1024 * 1024


@pytest.fixture
def lab_sample(session: Session):
    tenant = create_tenant(session, name="Lab")
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    sample = create_sample(session, tenant, branch, order)
    user = create_user(session, tenant, email="u@t.example")
    UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=0)
    session.commit()
    return {"tenant": tenant, "sample": sample, "user": user, "headers": auth_headers(user)}


def _upload(client, ctx, name, data, mime="image/heic"):
    return client.post(
        f"/api/v1/laboratory/samples/{ctx['sample'].id}/images",
        files={"file": (name, data, mime)},
        headers=ctx["headers"],
    )


def _usage(session, tenant_id) -> int:
    session.expire_all()
    row = session.get(TenantUsage, tenant_id)
    return row.billable_storage_bytes


def _nothing_persisted(session, ctx):
    session.expire_all()
    assert session.exec(select(SampleImage)).all() == []
    assert session.exec(select(StorageObject)).all() == []
    assert FakeS3Service.store == {}
    assert _usage(session, ctx["tenant"].id) == 0
    assert session.get(type(ctx["sample"]), ctx["sample"].id).state == SampleState.RECEIVED
    assert session.exec(select(OrderEvent).where(OrderEvent.sample_id == ctx["sample"].id)).all() == []


class TestHeicUploadSucceeds:
    def test_converts_stores_and_accounts(self, client, session, lab_sample):
        resp = _upload(client, lab_sample, "IMG_0001.HEIC", heic_bytes((320, 240)))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["filename"] == "IMG_0001.HEIC" and body["is_raw"] is False
        assert set(body["urls"]) == {"processed", "thumbnail"}  # no retained original

        rows = session.exec(select(StorageObject)).all()
        assert len(rows) == 2 and {r.content_type for r in rows} == {"image/jpeg"}
        assert all(r.object_key.endswith(".jpg") for r in rows)
        sizes = 0
        for row in rows:
            blob = FakeS3Service.store[row.object_key]
            assert Image.open(io.BytesIO(blob)).format == "JPEG"
            assert row.size_bytes == len(blob)
            sizes += len(blob)
        assert _usage(session, lab_sample["tenant"].id) == sizes
        assert set(FakeS3Service.store) == {r.object_key for r in rows}

        listed = client.get(
            f"/api/v1/laboratory/samples/{lab_sample['sample'].id}/images",
            headers=lab_sample["headers"],
        )
        assert [i["id"] for i in listed.json()["images"]] == [body["sample_image_id"]]
        event = session.exec(
            select(OrderEvent).where(OrderEvent.event_type == EventType.IMAGE_UPLOADED)
        ).first()
        assert event.event_metadata["converted_from"] == "heic"

    def test_accepted_by_content_under_a_misleading_name_and_mime(self, client, session, lab_sample):
        resp = _upload(client, lab_sample, "photo.png", heic_bytes(), mime="image/png")
        assert resp.status_code == 200, resp.text

    def test_report_selection_sees_the_converted_image(self, client, session, lab_sample):
        """The sample listing is what the report picker reads: both renditions resolve."""
        assert _upload(client, lab_sample, "a.heic", heic_bytes()).status_code == 200
        listed = client.get(
            f"/api/v1/laboratory/samples/{lab_sample['sample'].id}/images",
            headers=lab_sample["headers"],
        ).json()["images"]
        assert {"processed", "thumbnail"} <= set(listed[0]["urls"])


class TestHeicUploadRejected:
    @pytest.mark.parametrize(
        "name,data,fragment",
        [
            ("a.heic", heic_bytes((64, 64))[:200], "HEIC"),
            ("a.heic", heic_multi_image([(1, 2, 3), (4, 5, 6)]), "imágenes"),
            ("a.heic", with_brand(heic_bytes(), b"miaf", b"msf1"), "secuencia"),
            ("a.heic", b"not an image at all", "extensión HEIC"),
        ],
    )
    def test_rejected_before_any_effect(self, client, session, lab_sample, name, data, fragment):
        resp = _upload(client, lab_sample, name, data)
        assert resp.status_code == 422, resp.text
        assert fragment in resp.json()["detail"]
        _nothing_persisted(session, lab_sample)

    def test_oversize_dimensions_rejected(self, client, session, lab_sample, monkeypatch):
        from app.services import image_processing

        monkeypatch.setattr(image_processing, "HEIC_MAX_PIXELS", 100)
        resp = _upload(client, lab_sample, "big.heic", heic_bytes((64, 48)))
        assert resp.status_code == 422 and "megapíxeles" in resp.json()["detail"]
        _nothing_persisted(session, lab_sample)

    def test_byte_limit_still_applies_to_heic(self, client, session, lab_sample):
        resp = _upload(client, lab_sample, "huge.heic", b"\x00" * (50 * MIB + 1))
        assert resp.status_code == 413
        _nothing_persisted(session, lab_sample)

    def test_corrupt_jpeg_is_a_422_not_a_server_error(self, client, session, lab_sample):
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, "JPEG")
        resp = _upload(client, lab_sample, "a.jpg", buf.getvalue()[:30], mime="image/jpeg")
        assert resp.status_code == 422
        _nothing_persisted(session, lab_sample)


class TestAuthorityAndTenancyBeforeEffects:
    @staticmethod
    def _no_decode(monkeypatch):
        def boom(*_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("image was decoded before authorization")

        monkeypatch.setattr(laboratory, "process_image_bytes", boom)

    def test_role_without_upload_images_is_denied(self, client, session, lab_sample, monkeypatch):
        viewer = create_user(session, lab_sample["tenant"], email="v@t.example", roles=("viewer",))
        self._no_decode(monkeypatch)
        resp = client.post(
            f"/api/v1/laboratory/samples/{lab_sample['sample'].id}/images",
            files={"file": ("a.heic", heic_bytes(), "image/heic")},
            headers=auth_headers(viewer),
        )
        assert resp.status_code == 403
        _nothing_persisted(session, lab_sample)

    def test_foreign_tenant_sample_is_404_before_conversion(self, client, session, lab_sample, monkeypatch):
        other = create_tenant(session, name="Other")
        stranger = create_user(session, other, email="s@o.example")
        UsageService.initialize_usage(session, other.id, billable_storage_bytes=0)
        session.commit()
        self._no_decode(monkeypatch)
        resp = client.post(
            f"/api/v1/laboratory/samples/{lab_sample['sample'].id}/images",
            files={"file": ("a.heic", heic_bytes(), "image/heic")},
            headers=auth_headers(stranger),
        )
        assert resp.status_code == 404
        _nothing_persisted(session, lab_sample)
        assert _usage(session, other.id) == 0

    def test_unauthenticated_is_rejected(self, client, session, lab_sample):
        resp = client.post(
            f"/api/v1/laboratory/samples/{lab_sample['sample'].id}/images",
            files={"file": ("a.heic", heic_bytes(), "image/heic")},
        )
        assert resp.status_code in (401, 403)
        _nothing_persisted(session, lab_sample)


class TestFailureCleanup:
    def test_second_storage_write_failing_removes_the_first(self, client, session, lab_sample, monkeypatch):
        original = FakeS3Service.upload_bytes
        calls = {"n": 0}

        def flaky(self, data, key, content_type=None, acl=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated storage outage")
            return original(self, data, key, content_type, acl)

        monkeypatch.setattr(FakeS3Service, "upload_bytes", flaky)
        with pytest.raises(RuntimeError, match="outage"):
            _upload(client, lab_sample, "a.heic", heic_bytes())
        _nothing_persisted(session, lab_sample)

    def test_database_failure_after_storage_removes_both_objects(self, client, session, lab_sample, monkeypatch):
        def failing_commit(self):
            raise RuntimeError("simulated database failure")

        monkeypatch.setattr(Session, "commit", failing_commit)
        with pytest.raises(RuntimeError, match="database failure"):
            _upload(client, lab_sample, "a.heic", heic_bytes())
        monkeypatch.undo()
        _nothing_persisted(session, lab_sample)

    def test_cleanup_failure_does_not_mask_the_original_error(self, client, session, lab_sample, monkeypatch):
        def failing_commit(self):
            raise RuntimeError("simulated database failure")

        def failing_delete(self, key):
            raise OSError("delete refused")

        monkeypatch.setattr(Session, "commit", failing_commit)
        monkeypatch.setattr(FakeS3Service, "delete_object", failing_delete)
        with pytest.raises(RuntimeError, match="database failure"):
            _upload(client, lab_sample, "a.jpg", _jpeg(), "image/jpeg")


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (9, 9, 9)).save(buf, "JPEG")
    return buf.getvalue()


class TestExistingFormatsUnchanged:
    def test_jpeg_upload_still_works_and_is_accounted(self, client, session, lab_sample):
        resp = _upload(client, lab_sample, "a.jpg", _jpeg(), "image/jpeg")
        assert resp.status_code == 200, resp.text
        rows = session.exec(select(StorageObject)).all()
        assert len(rows) == 2
        assert _usage(session, lab_sample["tenant"].id) == sum(r.size_bytes for r in rows)
        event = session.exec(select(OrderEvent).where(OrderEvent.event_type == EventType.IMAGE_UPLOADED)).first()
        assert "converted_from" not in event.event_metadata
