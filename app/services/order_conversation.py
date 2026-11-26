"""Order conversation: messages, image attachments and their reads (Céluma 1.5, #88/#96/#98).

Contract: `celuma-engineering/specs/laboratory/order-conversation.md`; attachment model:
`celuma-engineering/adr/0026-order-conversation-attachments.md`.

Shape of one send (`POST /laboratory/orders/{id}/comments`), in order:

    authenticate, `lab:manage_comments`, order in tenant (404), branch access   (every call)
    -> Idempotency-Key required; fingerprint = text + mentions + SHA-256 of each image
    -> cheap validation (count, bytes, sniffed type) before any lock
    -> idempotency.begin()  advisory lock; a committed receipt replays the message as it is now
    -> decode + re-encode every image (nothing is written before all of them pass)
    -> objects written, rows added (comment, mentions, attachments, storage rows,
       timeline event, usage delta, notifications, receipt) -> ONE commit
    -> any failure: rollback + best-effort delete of the objects this request wrote

A message is therefore all-or-nothing: there is no "uploaded but unsent" image, so
there is nothing to garbage-collect, and a retry of a lost response with the same key
never stores a second message, a second image or a second notification.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Dict, Iterable, List, Optional, Sequence
from uuid import UUID, uuid4

from fastapi import HTTPException
from PIL import Image
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.rbac import user_has_full_branch_access
from app.models.enums import EventType
from app.models.events import OrderEvent
from app.models.laboratory import Order, OrderComment, OrderCommentAttachment, OrderCommentMention
from app.models.storage import StorageObject
from app.models.user import AppUser
from app.schemas.laboratory import CommentAttachmentResponse, CommentResponse, MentionedUser
from app.services import idempotency
from app.services.image_processing import (
    ImageProcessingError,
    _heif_brands,
    _is_heic_container,
    process_image_bytes,
)
from app.services.notification_integrations.conversations import notify_order_comment_added
from app.services.s3 import S3Service
from app.services.usage_thresholds import record_storage_delta_with_thresholds

logger = logging.getLogger(__name__)

OP_COMMENT = idempotency.OP_ORDER_COMMENT

#: Limits (engineering defaults, ADR 0026; not owner-approved clinical limits).
MAX_TEXT_LENGTH = 5000
MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
#: Accepted by CONTENT, never by filename or declared MIME type.
ACCEPTED_IMAGE_KINDS = ("jpeg", "png", "webp", "heic")

ATTACHMENT_VARIANTS = ("thumbnail", "full")


def sniff_image_kind(data: bytes) -> Optional[str]:
    """The accepted image kind of `data` by its leading bytes, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if _is_heic_container(_heif_brands(data)):
        return "heic"
    return None


@dataclass
class IncomingImage:
    position: int
    data: bytes
    kind: str
    sha256: str


@dataclass
class EncodedImage:
    position: int
    full: bytes
    thumbnail: bytes
    width: int
    height: int


class CommentFingerprint(BaseModel):
    """What makes two sends the same logical message (the hash is all that is stored)."""
    text: str
    mentions: List[str]
    attachments: List[str]


def _error(status: int, message: str) -> HTTPException:
    return HTTPException(status, message)


def validate_text(raw: Optional[str], *, has_attachments: bool) -> str:
    text = (raw or "").strip()
    if not text and not has_attachments:
        raise _error(422, "Escribe un mensaje o adjunta al menos una imagen.")
    if len(text) > MAX_TEXT_LENGTH:
        raise _error(422, f"El mensaje no puede superar {MAX_TEXT_LENGTH} caracteres.")
    return text


def validate_images(files: Sequence[bytes]) -> List[IncomingImage]:
    """Count, size and type of every image, before any decode, lock or write."""
    if len(files) > MAX_ATTACHMENTS:
        raise _error(422, f"Puedes adjuntar como máximo {MAX_ATTACHMENTS} imágenes por mensaje.")
    images: List[IncomingImage] = []
    for position, data in enumerate(files):
        label = f"La imagen {position + 1}"
        if not data:
            raise _error(422, f"{label} está vacía.")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise _error(413, f"{label} supera el límite de {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.")
        kind = sniff_image_kind(data)
        if kind is None:
            raise _error(415, f"{label} no es un formato compatible. Usa JPG, PNG, WebP o HEIC.")
        images.append(IncomingImage(position, data, kind, hashlib.sha256(data).hexdigest()))
    return images


def encode_images(images: Iterable[IncomingImage]) -> List[EncodedImage]:
    """Decode and re-encode as JPEG (EXIF/GPS dropped, orientation applied)."""
    encoded: List[EncodedImage] = []
    for image in images:
        try:
            processed = process_image_bytes(f"imagen.{image.kind}", image.data)
            with Image.open(BytesIO(processed.processed_jpeg_bytes)) as pil:
                width, height = pil.size
        except ImageProcessingError as exc:
            raise _error(422, f"La imagen {image.position + 1}: {exc.message}") from None
        encoded.append(EncodedImage(image.position, processed.processed_jpeg_bytes,
                                    processed.thumbnail_jpeg_bytes, width, height))
    return encoded


# --------------------------------------------------------------------------- access


def require_branch_access(session: Session, user: AppUser, order: Order) -> None:
    """Writing needs the order's branch, unless the role has full branch access.

    #88: `superuser`/`admin` deliberately carry no `user_branch` rows
    (`app/api/v1/users.py` clears them), so checking the rows alone refused every
    message from an administrator created or edited after registration.
    """
    if user_has_full_branch_access(user.id, session):
        return
    if str(order.branch_id) not in {str(link.branch_id) for link in user.branches}:
        raise HTTPException(403, "No access to this order's branch")


def load_order(session: Session, order_id: str, tenant_id: str) -> Order:
    try:
        order = session.get(Order, UUID(str(order_id)))
    except ValueError:
        order = None
    if not order or str(order.tenant_id) != str(tenant_id):
        raise HTTPException(404, "Order not found")
    return order


# --------------------------------------------------------------------------- serialization


def serialize_comments(session: Session, comments: Sequence[OrderComment]) -> List[CommentResponse]:
    if not comments:
        return []
    ids = [comment.id for comment in comments]
    mentions_by_comment: Dict[UUID, List[UUID]] = {}
    for mention in session.exec(select(OrderCommentMention).where(OrderCommentMention.comment_id.in_(ids))).all():
        mentions_by_comment.setdefault(mention.comment_id, []).append(mention.user_id)
    attachments_by_comment: Dict[UUID, List[OrderCommentAttachment]] = {}
    for attachment in session.exec(
        select(OrderCommentAttachment)
        .where(OrderCommentAttachment.comment_id.in_(ids))
        .order_by(OrderCommentAttachment.position)
    ).all():
        attachments_by_comment.setdefault(attachment.comment_id, []).append(attachment)

    user_ids = {comment.created_by for comment in comments}
    for values in mentions_by_comment.values():
        user_ids.update(values)
    users = {u.id: u for u in session.exec(select(AppUser).where(AppUser.id.in_(list(user_ids)))).all()}

    def mentioned(user_id: UUID) -> Optional[MentionedUser]:
        u = users.get(user_id)
        if u is None:
            return None
        return MentionedUser(user_id=str(u.id), username=u.username, name=u.full_name or u.email, avatar=u.avatar_url)

    items: List[CommentResponse] = []
    for comment in comments:
        author = users.get(comment.created_by)
        mention_ids = mentions_by_comment.get(comment.id, [])
        items.append(CommentResponse(
            id=str(comment.id),
            tenant_id=str(comment.tenant_id),
            branch_id=str(comment.branch_id),
            order_id=str(comment.order_id),
            created_at=comment.created_at,
            created_by=str(comment.created_by),
            created_by_name=(author.full_name or author.email) if author else None,
            created_by_avatar=author.avatar_url if author else None,
            text=comment.text,
            mentions=[str(uid) for uid in mention_ids],
            mentioned_users=[m for m in (mentioned(uid) for uid in mention_ids) if m is not None],
            metadata=comment.comment_metadata,
            edited_at=comment.edited_at,
            deleted_at=comment.deleted_at,
            attachments=[
                CommentAttachmentResponse(id=str(a.id), position=a.position, width=a.width, height=a.height)
                for a in attachments_by_comment.get(comment.id, [])
            ],
        ))
    return items


# --------------------------------------------------------------------------- create


def _resolve_mentions(session: Session, raw: Sequence[str], tenant_id: UUID) -> List[AppUser]:
    wanted: List[UUID] = []
    for value in raw:
        try:
            uid = UUID(str(value))
        except ValueError:
            continue
        if uid not in wanted:
            wanted.append(uid)
    if not wanted:
        return []
    found = {u.id: u for u in session.exec(
        select(AppUser).where(AppUser.id.in_(wanted), AppUser.tenant_id == tenant_id, AppUser.is_active == True)  # noqa: E712
    ).all()}
    return [found[uid] for uid in wanted if uid in found]


def create_comment(
    session: Session,
    *,
    order: Order,
    actor: AppUser,
    raw_text: Optional[str],
    raw_mentions: Sequence[str],
    files: Sequence[bytes],
    metadata: Optional[dict],
    idempotency_key: Optional[str],
):
    """Persist one message (text and/or images) exactly once. Returns `(response, replayed)`."""
    key = idempotency.validate_key(idempotency_key)
    images = validate_images(files)
    text = validate_text(raw_text, has_attachments=bool(images))
    mentions = [str(value) for value in raw_mentions]
    request_fingerprint = idempotency.fingerprint(
        CommentFingerprint(text=text, mentions=mentions, attachments=[image.sha256 for image in images])
    )

    claim = idempotency.begin(
        session, tenant_id=order.tenant_id, actor_id=actor.id, operation=OP_COMMENT,
        key=key, request_fingerprint=request_fingerprint,
    )
    if claim.replay:
        idempotency.verify_resources(session, claim)
        comment_id = UUID(claim.receipt.result["comment"]["id"])
        comment = session.get(OrderComment, comment_id)
        session.rollback()  # releases the advisory lock; nothing was written
        return serialize_comments(session, [comment])[0], True

    encoded = encode_images(images)
    s3 = S3Service() if encoded else None
    uploaded_keys: List[str] = []
    try:
        comment = OrderComment(
            tenant_id=order.tenant_id, branch_id=order.branch_id, order_id=order.id,
            created_by=actor.id, text=text, comment_metadata=metadata or {},
        )
        session.add(comment)
        session.flush()

        mentioned_users = _resolve_mentions(session, mentions, order.tenant_id)
        for user in mentioned_users:
            session.add(OrderCommentMention(comment_id=comment.id, user_id=user.id))

        total_bytes = 0
        for image in encoded:
            attachment_id = uuid4()
            prefix = f"conversations/{order.tenant_id}/{order.branch_id}/{order.id}/{comment.id}/{attachment_id}"
            storage_ids = []
            for variant, data in (("full", image.full), ("thumbnail", image.thumbnail)):
                object_key = f"{prefix}/{variant}.jpg"
                info = s3.upload_bytes(data, key=object_key, content_type="image/jpeg", acl=None)
                uploaded_keys.append(object_key)
                storage = StorageObject(
                    provider="aws", region=s3.region, bucket=info.bucket, object_key=info.key,
                    version_id=info.version_id, etag=info.etag, content_type="image/jpeg",
                    size_bytes=info.size_bytes, tenant_id=order.tenant_id,
                )
                session.add(storage)
                session.flush()
                storage_ids.append(storage.id)
                total_bytes += info.size_bytes or 0
            session.add(OrderCommentAttachment(
                id=attachment_id, tenant_id=order.tenant_id, branch_id=order.branch_id, order_id=order.id,
                comment_id=comment.id, position=image.position, storage_id=storage_ids[0],
                thumbnail_storage_id=storage_ids[1], width=image.width, height=image.height,
                created_by=actor.id,
            ))

        # Timeline event, as before 1.5 (same 50-character preview), plus the image count.
        session.add(OrderEvent(
            tenant_id=order.tenant_id, branch_id=order.branch_id, order_id=order.id,
            event_type=EventType.COMMENT_ADDED, description="",
            event_metadata={
                "comment_id": str(comment.id),
                "order_id": str(order.id),
                "comment_preview": text[:50] + "..." if len(text) > 50 else text,
                "order_code": order.order_code,
                "mentions_count": len(mentioned_users),
                "attachments_count": len(encoded),
            },
            created_by=actor.id,
        ))
        if encoded:
            record_storage_delta_with_thresholds(
                session, order.tenant_id, total_bytes, source="order_comment_attachment",
                resource_type="order_comment_attachment", actor_id=actor.id,
            )
        session.flush()
        notify_order_comment_added(
            session, order=order, comment=comment, actor=actor,
            mentioned_user_ids=[user.id for user in mentioned_users],
        )
        idempotency.store(
            session, claim,
            result={"comment": {"id": str(comment.id)}, "attachments": len(encoded)},
            resources=[{"kind": "order_comment", "id": str(comment.id)}],
        )
        session.commit()
    except Exception:
        session.rollback()
        for object_key in uploaded_keys:
            try:
                s3.delete_object(object_key)
            except Exception:  # best effort; never mask the original failure
                logger.warning(
                    "Could not remove an orphaned conversation attachment object",
                    extra={"event": "order_comment.attachment_cleanup_failed", "order_id": str(order.id)},
                )
        raise
    session.refresh(comment)
    return serialize_comments(session, [comment])[0], False


# --------------------------------------------------------------------------- read


def load_attachment_bytes(
    session: Session, *, order: Order, comment_id: str, attachment_id: str, variant: str
) -> bytes:
    if variant not in ATTACHMENT_VARIANTS:
        raise HTTPException(400, "variant must be 'thumbnail' or 'full'")
    try:
        attachment = session.get(OrderCommentAttachment, UUID(str(attachment_id)))
        comment_uuid = UUID(str(comment_id))
    except ValueError:
        attachment = None
        comment_uuid = None
    if (
        attachment is None
        or attachment.tenant_id != order.tenant_id
        or attachment.order_id != order.id
        or attachment.comment_id != comment_uuid
    ):
        raise HTTPException(404, "Attachment not found")
    comment = session.get(OrderComment, attachment.comment_id)
    if comment is None or comment.deleted_at is not None:
        raise HTTPException(404, "Attachment not found")
    storage = session.get(
        StorageObject, attachment.thumbnail_storage_id if variant == "thumbnail" else attachment.storage_id
    )
    if storage is None or storage.tenant_id != order.tenant_id:
        raise HTTPException(404, "Attachment not found")
    return S3Service().download_bytes(storage.object_key)
