"""Céluma 1.5 order conversation: retries (#88), image attachments (#96), notifications (#98).

Synthetic tenants, users, orders and images only. Contract:
`celuma-engineering/specs/laboratory/order-conversation.md`.
"""
from __future__ import annotations

from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from sqlmodel import select

from app.core.config import settings
from app.core.rbac import replace_user_roles
from app.models.events import OrderEvent
from app.models.laboratory import OrderComment, OrderCommentAttachment
from app.models.notification import Notification, NotificationDelivery, NotificationType
from app.models.storage import StorageObject
from app.models.tenant_usage import TenantUsage
from app.models.user import UserBranch
from app.services.email_provider_fake import FakeEmailProvider
from app.services.notification_delivery_worker import process_delivery_batch
from app.services.usage import UsageService

from .conftest import FakeS3Service
from .factories import (
    add_order_reviewer,
    assign_to_order,
    auth_headers,
    create_branch,
    create_order,
    create_tenant,
    create_user,
    deliveries_for,
    notifications_for,
    set_email_preference,
)

SECRET_TEXT = "Texto sintético confidencial 7731"


def jpeg_bytes(size=(64, 48), color=(73, 182, 173), with_gps=False) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    if with_gps:
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (19.0, 26.0, 0.0)}  # GPSInfo
        exif[0x010F] = "SyntheticCam"
        image.save(buffer, format="JPEG", exif=exif)
    else:
        image.save(buffer, format="JPEG")
    return buffer.getvalue()


def png_bytes(size=(40, 40)) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", size, (249, 141, 132, 128)).save(buffer, format="PNG")
    return buffer.getvalue()


class World:
    def __init__(self, session):
        self.tenant = create_tenant(session, name="Laboratorio Sintético")
        UsageService.initialize_usage(session, self.tenant.id, billable_storage_bytes=0)
        session.commit()
        self.branch = create_branch(session, self.tenant)
        self.order = create_order(session, self.tenant, self.branch, order_code="ORD-SYN-001")
        self.sender = self.user(session, "remitente", ("lab_tech",), "Remitente Sintético")
        self.assignee = self.user(session, "asignado", ("lab_tech",), "Asignado Sintético")
        self.reviewer = self.user(session, "revisor", ("pathologist",), "Revisor Sintético")
        self.outsider = self.user(session, "ajeno", ("lab_tech",), "Ajeno Sintético")
        assign_to_order(session, self.tenant, self.order, self.assignee)
        add_order_reviewer(session, self.tenant, self.order, self.reviewer)
        self.session = session

    def user(self, session, name, roles, full_name):
        user = create_user(session, self.tenant, email=f"{name}@lab.example.test", roles=roles, full_name=full_name)
        session.add(UserBranch(user_id=user.id, branch_id=self.branch.id))
        session.commit()
        return user

    @property
    def url(self):
        return f"/api/v1/laboratory/orders/{self.order.id}/comments"


@pytest.fixture
def world(session):
    return World(session)


def post_text(client, world, user, text, *, key=None, mentions=()):
    return client.post(
        world.url,
        json={"text": text, "mentions": [str(m) for m in mentions]},
        headers={**auth_headers(user), "Idempotency-Key": key or f"k-{uuid4().hex}"},
    )


def post_multipart(client, world, user, text, files, *, key=None, mentions=()):
    data = {"text": text, "mentions": [str(m) for m in mentions]}
    return client.post(
        world.url,
        data=data,
        files=[("files", (f"foto{i}.jpg", content, "image/jpeg")) for i, content in enumerate(files)],
        headers={**auth_headers(user), "Idempotency-Key": key or f"k-{uuid4().hex}"},
    )


def comment_rows(session):
    return session.exec(select(OrderComment)).all()


# =========================================================================== #88 retries


class TestRetrySafety:
    def test_missing_key_is_refused_and_stores_nothing(self, client, session, world):
        client.auto_key = False
        response = client.post(world.url, json={"text": "Hola"}, headers=auth_headers(world.sender))
        assert response.status_code == 428
        assert comment_rows(session) == []

    def test_retry_after_a_lost_response_replays_and_stores_once(self, client, session, world):
        first = post_text(client, world, world.sender, "Mensaje único", key="retry-key-0001")
        again = post_text(client, world, world.sender, "Mensaje único", key="retry-key-0001")
        assert first.status_code == again.status_code == 200
        assert again.headers.get("Idempotent-Replayed") == "true"
        assert again.json()["id"] == first.json()["id"]
        assert len(comment_rows(session)) == 1
        events = session.exec(select(OrderEvent).where(OrderEvent.order_id == world.order.id)).all()
        assert len(events) == 1
        assert len(session.exec(select(Notification)).all()) == 1  # no second notification

    def test_same_key_with_another_message_is_a_conflict(self, client, session, world):
        post_text(client, world, world.sender, "Primero", key="retry-key-0002")
        response = post_text(client, world, world.sender, "Otro texto", key="retry-key-0002")
        assert response.status_code == 409
        assert len(comment_rows(session)) == 1

    def test_storage_failure_saves_nothing_and_the_same_key_can_retry(self, client, session, world):
        FakeS3Service.fail_next_upload = True
        with pytest.raises(RuntimeError):
            post_multipart(client, world, world.sender, "Con imagen", [jpeg_bytes()], key="retry-key-0003")
        session.rollback()
        assert comment_rows(session) == []
        assert session.exec(select(OrderCommentAttachment)).all() == []
        assert session.exec(select(Notification)).all() == []
        assert session.exec(select(StorageObject)).all() == []
        assert FakeS3Service.store == {}

        retry = post_multipart(client, world, world.sender, "Con imagen", [jpeg_bytes()], key="retry-key-0003")
        assert retry.status_code == 200, retry.text
        assert retry.headers.get("Idempotent-Replayed") is None
        assert len(comment_rows(session)) == 1
        assert len(retry.json()["attachments"]) == 1

    def test_timeline_event_keeps_its_preview_and_counts_images(self, client, session, world):
        post_multipart(client, world, world.sender, "Vista previa", [jpeg_bytes()])
        event = session.exec(select(OrderEvent).where(OrderEvent.order_id == world.order.id)).one()
        assert event.event_metadata["comment_preview"] == "Vista previa"
        assert event.event_metadata["attachments_count"] == 1


# =========================================================================== #96 attachments


class TestAttachments:
    def test_text_and_images_are_stored_together_and_read_back(self, client, session, world):
        response = post_multipart(
            client, world, world.sender, "Corte con tinción", [jpeg_bytes(with_gps=True), png_bytes()]
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["text"] == "Corte con tinción"
        assert [a["position"] for a in body["attachments"]] == [0, 1]
        assert (body["attachments"][0]["width"], body["attachments"][0]["height"]) == (64, 48)

        listed = client.get(world.url, headers=auth_headers(world.reviewer)).json()
        assert listed["items"][0]["attachments"] == body["attachments"]

        for variant in ("thumbnail", "full"):
            image = client.get(
                f"{world.url}/{body['id']}/attachments/{body['attachments'][0]['id']}",
                params={"variant": variant}, headers=auth_headers(world.reviewer),
            )
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/jpeg"
            assert image.headers["cache-control"] == "private, no-store"
            decoded = Image.open(BytesIO(image.content))
            assert decoded.format == "JPEG"
            assert not decoded.getexif()  # EXIF (GPS, device) is not propagated

        keys = list(FakeS3Service.store)
        assert len(keys) == 4
        assert all(k.startswith(f"conversations/{world.tenant.id}/{world.branch.id}/{world.order.id}/{body['id']}/")
                   for k in keys)
        assert not any("foto" in k for k in keys)  # no filename in storage
        session.expire_all()
        usage = session.get(TenantUsage, world.tenant.id)
        assert usage.billable_storage_bytes == sum(len(v) for v in FakeS3Service.store.values())

    def test_image_only_message(self, client, world):
        response = post_multipart(client, world, world.sender, "", [jpeg_bytes()])
        assert response.status_code == 200, response.text
        assert response.json()["text"] == ""

    def test_empty_message_is_refused(self, client, session, world):
        assert post_text(client, world, world.sender, "   ").status_code == 422
        assert post_multipart(client, world, world.sender, "  ", []).status_code == 422
        assert comment_rows(session) == []

    @pytest.mark.parametrize("payload,status", [
        (b"GIF89a" + b"\x00" * 64, 415),
        (b"%PDF-1.4 synthetic", 415),
        (b"no soy una imagen", 415),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 32, 422),  # JPEG signature, corrupt body
        (b"\xff\xd8\xff" + b"\x00" * (10 * 1024 * 1024), 413),
    ])
    def test_unusable_files_store_nothing(self, client, session, world, payload, status):
        response = post_multipart(client, world, world.sender, "Adjunto", [jpeg_bytes(), payload])
        assert response.status_code == status, response.text
        assert "imagen 2" in response.json()["detail"].lower()
        assert comment_rows(session) == []
        assert FakeS3Service.store == {}
        assert session.exec(select(StorageObject)).all() == []

    def test_more_than_four_images_is_refused(self, client, session, world):
        response = post_multipart(client, world, world.sender, "Muchas", [jpeg_bytes()] * 5)
        assert response.status_code == 422
        assert comment_rows(session) == []

    def test_reading_needs_lab_read_on_every_request(self, client, session, world):
        body = post_multipart(client, world, world.sender, "x", [jpeg_bytes()]).json()
        url = f"{world.url}/{body['id']}/attachments/{body['attachments'][0]['id']}"
        assert client.get(url, headers=auth_headers(world.outsider)).status_code == 200
        replace_user_roles(world.outsider.id, ["physician"], session)  # no lab:read any more
        session.commit()
        assert client.get(url, headers=auth_headers(world.outsider)).status_code == 403
        assert client.get(world.url, headers=auth_headers(world.outsider)).status_code == 403

    def test_mismatched_ids_and_bad_variant(self, client, world):
        body = post_multipart(client, world, world.sender, "x", [jpeg_bytes()]).json()
        other = post_text(client, world, world.sender, "otro").json()
        attachment = body["attachments"][0]["id"]
        headers = auth_headers(world.sender)
        assert client.get(f"{world.url}/{other['id']}/attachments/{attachment}", headers=headers).status_code == 404
        assert client.get(f"{world.url}/{body['id']}/attachments/{uuid4()}", headers=headers).status_code == 404
        assert client.get(f"{world.url}/{body['id']}/attachments/no-uuid", headers=headers).status_code == 404
        assert client.get(f"{world.url}/{body['id']}/attachments/{attachment}", params={"variant": "raw"},
                          headers=headers).status_code == 400

    def test_cross_tenant_access_is_denied(self, client, session, world):
        body = post_multipart(client, world, world.sender, "x", [jpeg_bytes()]).json()
        other_tenant = create_tenant(session, name="Otro Laboratorio")
        foreign = create_user(session, other_tenant, email="foraneo@otro.example.test", roles=("admin",))
        headers = auth_headers(foreign)
        url = f"{world.url}/{body['id']}/attachments/{body['attachments'][0]['id']}"
        assert client.get(url, headers=headers).status_code == 404
        assert client.get(world.url, headers=headers).status_code == 404
        assert post_multipart(client, world, foreign, "intruso", [jpeg_bytes()]).status_code == 404
        assert len(comment_rows(session)) == 1


# =========================================================================== #98 notifications


def notified(session, user):
    return notifications_for(session, user, notification_type=NotificationType.ORDER_COMMENT_ADDED)


class TestNotifications:
    def test_participants_are_notified_and_the_sender_is_not(self, client, session, world):
        post_text(client, world, world.outsider, "Primer mensaje del ajeno")  # becomes a participant
        post_text(client, world, world.sender, SECRET_TEXT)

        assert len(notified(session, world.assignee)) == 2
        assert len(notified(session, world.reviewer)) == 2
        assert len(notified(session, world.outsider)) == 1   # earlier author, not their own message
        # Participants are read when each message commits: the sender was not one yet when
        # the outsider wrote, and is never told about their own message.
        assert notified(session, world.sender) == []
        notification, _ = notified(session, world.assignee)[0]
        assert notification.title == "Nuevo comentario — Orden ORD-SYN-001"
        assert SECRET_TEXT not in notification.title + notification.body
        assert notification.resource_type == "order" and notification.resource_id == world.order.id

    def test_mentions_get_the_mention_notice_and_not_both(self, client, session, world):
        bystander = world.user(session, "mencionado", ("viewer",), "Mencionado Sintético")
        post_text(client, world, world.sender, "Revisa esto", mentions=[bystander.id, world.assignee.id])
        assert [n.title for n, _ in notified(session, bystander)] == ["Te mencionaron — Orden ORD-SYN-001"]
        assert [n.title for n, _ in notified(session, world.assignee)] == ["Te mencionaron — Orden ORD-SYN-001"]
        assert [n.title for n, _ in notified(session, world.reviewer)] == ["Nuevo comentario — Orden ORD-SYN-001"]

    def test_mention_never_widens_who_is_told(self, client, session, world):
        no_read = world.user(session, "sinlectura", ("physician",), "Sin Lectura")
        inactive = world.user(session, "inactivo", ("lab_tech",), "Inactivo")
        inactive.is_active = False
        session.add(inactive)
        session.commit()
        other_tenant = create_tenant(session, name="Otro Laboratorio")
        foreign = create_user(session, other_tenant, email="ext@otro.example.test", roles=("lab_tech",))
        response = post_text(client, world, world.sender, "Hola", mentions=[no_read.id, inactive.id, foreign.id])
        assert response.status_code == 200
        for user in (no_read, inactive, world.outsider):
            assert notified(session, user) == []
        assert foreign.id not in {r.user_id for _, r in notifications_for(session, foreign)}

    def test_email_follows_preference_and_carries_no_content(self, client, session, world, monkeypatch):
        set_email_preference(session, world.tenant, world.reviewer, NotificationType.ORDER_COMMENT_ADDED, enabled=False)
        post_text(client, world, world.sender, SECRET_TEXT)
        notification = session.exec(select(Notification)).one()
        addresses = {d.recipient_address for d in deliveries_for(session, notification.id)}
        assert addresses == {world.assignee.email}  # reviewer opted out; in-app still created
        assert len(notified(session, world.reviewer)) == 1

        monkeypatch.setattr(settings, "email_sender", "notificaciones@celuma.test")
        provider = FakeEmailProvider()
        process_delivery_batch(session, provider)
        [email] = provider.sent
        rendered = email.subject + email.text_body + (email.html_body or "")
        assert SECRET_TEXT not in rendered
        assert "ORD-SYN-001" not in rendered
        assert str(world.order.id) not in rendered
        assert "Nuevo comentario" in email.subject

    def test_access_is_rechecked_when_the_email_is_sent(self, client, session, world, monkeypatch):
        post_text(client, world, world.sender, "Hola")
        notification = session.exec(select(Notification)).one()
        replace_user_roles(world.assignee.id, ["physician"], session)  # loses lab:read before the worker runs
        session.commit()

        monkeypatch.setattr(settings, "email_sender", "notificaciones@celuma.test")
        provider = FakeEmailProvider()
        process_delivery_batch(session, provider)
        assert {e.to_address for e in provider.sent} == {world.reviewer.email}
        revoked = session.exec(select(NotificationDelivery).where(
            NotificationDelivery.notification_id == notification.id,
            NotificationDelivery.recipient_user_id == world.assignee.id,
        )).one()
        assert revoked.status == "FAILED"
        assert revoked.error_code == "recipient_access_revoked"
        assert revoked.next_attempt_at is None  # terminal, never retried

    def test_refused_sends_emit_nothing(self, client, session, world):
        tenant_b_branch = create_branch(session, world.tenant, code="NORTE")
        other = create_order(session, world.tenant, tenant_b_branch, order_code="ORD-SYN-002")
        response = client.post(
            f"/api/v1/laboratory/orders/{other.id}/comments", json={"text": "fuera de sucursal"},
            headers={**auth_headers(world.sender), "Idempotency-Key": "refused-key-01"},
        )
        assert response.status_code == 403
        assert post_multipart(client, world, world.sender, "x", [b"no soy imagen"]).status_code == 415
        assert session.exec(select(Notification)).all() == []
