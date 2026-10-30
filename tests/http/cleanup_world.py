"""Synthetic multi-tenant world for the laboratory-content cleanup tests (Céluma 1.4, #39).

Everything here is invented: tenant names, codes, bytes. Two complete laboratories are built so a
cleanup of one can prove the other is byte-for-byte untouched. Operational content is produced through
the REAL routes wherever one exists (unified registration with idempotency receipts and ledger entries,
sign-and-publish with an official PDF, an amendment cycle republished through review and signing), and
by direct inserts only for rows no route fabricates cheaply (images, renditions, comments, payments).

`snapshot()` hashes rows (full row text), so "preserved" means identical content, not just a surviving
count. `FakeS3Service.store` is the object store: it is in-memory evidence, NOT a physical-deletion
proof (see test_laboratory_cleanup_object_store.py for the HTTP-protocol round trip).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List

from sqlalchemy import text
from sqlmodel import Session

from app.core.rbac import assign_role_by_code
from app.models.assignment import Assignment
from app.models.billing import Invoice, Payment
from app.models.enums import AssignmentItemType, ReportStatus, ReviewStatus, SampleState
from app.models.laboratory import Label, Order, OrderComment, OrderCommentMention, OrderLabel, Sample, SampleImage, SampleLabel
from app.models.order_management import OrderRequestingPhysician
from app.models.price_catalog import PriceCatalog
from app.models.report_template_version import ReportTemplateVersion
from app.models.report import ReportTemplate
from app.models.requesting_physician import RequestingPhysician
from app.models.storage import SampleImageRendition, StorageObject
from app.models.audit import AuditLog
from app.models.user import UserBranch

from .conftest import FakeS3Service, make_pdf_bytes
from .factories import (
    TEST_USER_PASSWORD,
    auth_headers,
    create_default_letterhead,
    create_notification,
    create_recipient,
    create_report,
    create_sample,
    create_storage_object,
    create_user,
    valid_rendering_snapshot,
)
from .numbering_support import Lab, configure, freeze

CONFIRM = {"password": TEST_USER_PASSWORD}
SENTINEL = "SENTINEL-CLINICAL-TEXT-DO-NOT-RETAIN"

CLEANUP_BASE = "/api/v1/maintenance/tenants/{tenant}/laboratory-cleanup"


def base(tenant_id) -> str:
    return CLEANUP_BASE.format(tenant=tenant_id)


# --------------------------------------------------------------------------- snapshots

#: tables owning rows through a parent (no tenant_id of their own): (table, predicate over alias x)
_VIA_PARENT = {
    "report_version": "x.report_id IN (SELECT id FROM report WHERE tenant_id = :t)",
    "sample_image_rendition": "x.sample_image_id IN (SELECT id FROM sample_image WHERE tenant_id = :t)",
    "order_labels": 'x.order_id IN (SELECT id FROM "order" WHERE tenant_id = :t)',
    "sample_labels": "x.sample_id IN (SELECT id FROM sample WHERE tenant_id = :t)",
    "order_comment_mention": "x.comment_id IN (SELECT id FROM order_comment WHERE tenant_id = :t)",
    "user_role": "x.user_id IN (SELECT id FROM app_user WHERE tenant_id = :t)",
    "user_branch": "x.user_id IN (SELECT id FROM app_user WHERE tenant_id = :t)",
    "tenant": "x.id = :t",
}
#: never compared: they legitimately move (sessions, tokens), or are global catalogs.
_SKIP = {"alembic_version", "blacklisted_token", "role", "permission", "role_permission"}
#: moves on purpose in a cleanup of THIS tenant; asserted separately.
VOLATILE_FOR_TARGET = {"audit_log", "tenant_usage", "tenant_usage_threshold_state", "tenant_usage_reconciliation",
                       "laboratory_cleanup_operation", "laboratory_cleanup_object", "laboratory_cleanup_retired_code"}


def _digest(session: Session, table: str, where: str, tenant_id) -> tuple[int, str]:
    sql = (f"SELECT count(*), coalesce(encode(sha256(convert_to(string_agg(x::text, E'\\n' ORDER BY x::text), 'UTF8')), 'hex'), '') "
           f'FROM "{table}" x WHERE {where}')
    count, digest = session.execute(text(sql), {"t": str(tenant_id)}).one()
    return int(count), digest


def snapshot(session: Session, tenant_id, *, skip=()) -> Dict[str, tuple[int, str]]:
    """Per-table (row count, content hash) of every row the tenant owns."""
    session.expire_all()
    out: Dict[str, tuple[int, str]] = {}
    tables = [row[0] for row in session.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"))]
    for table in sorted(tables):
        if table in _SKIP or table in skip:
            continue
        if table in _VIA_PARENT:
            where = _VIA_PARENT[table]
        else:
            has_tenant = session.execute(text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=:t AND column_name='tenant_id'"
            ), {"t": table}).first()
            if not has_tenant:
                continue
            where = "x.tenant_id = :t"
        out[table] = _digest(session, table, where, tenant_id)
    # storage_object rows of the tenant OR referenced by it (tenant_id is nullable/indirect historically)
    out["storage_object"] = _digest(session, "storage_object",
                                    "x.tenant_id = :t OR x.id IN (SELECT logo_storage_id FROM tenant WHERE id = :t) "
                                    "OR x.id IN (SELECT signature_storage_id FROM app_user WHERE tenant_id = :t)", tenant_id)
    return out


def object_store() -> Dict[str, str]:
    return {key: hashlib.sha256(data).hexdigest() for key, data in FakeS3Service.store.items()}


# --------------------------------------------------------------------------- the world


@dataclass
class LabWorld:
    lab: Lab
    admin: object = None
    tech: object = None
    reviewer: object = None
    author: object = None
    orders: Dict[str, str] = field(default_factory=dict)       # name -> order id
    order_codes: Dict[str, str] = field(default_factory=dict)
    sample_codes: List[str] = field(default_factory=list)
    keys: Dict[str, str] = field(default_factory=dict)         # name -> idempotency key used
    scoped_keys: set = field(default_factory=set)              # object keys that MUST be deleted
    preserved_keys: set = field(default_factory=set)           # object keys that MUST survive
    unprovable_keys: set = field(default_factory=set)          # tenant-owned but not provably scoped: kept
    shared_key: str = ""
    invoice_ids: List[str] = field(default_factory=list)
    payment_ids: List[str] = field(default_factory=list)

    @property
    def tenant(self):
        return self.lab.tenant

    @property
    def operator(self):
        return self.lab.user


def _store(session: Session, tenant, key: str, data: bytes, *, sha: str | None = None, content_type="image/jpeg") -> StorageObject:
    FakeS3Service.store[key] = data
    obj = StorageObject(
        provider="aws", region="mx-test-1", bucket="celuma-test-bucket", object_key=key,
        content_type=content_type, size_bytes=len(data), sha256_hex=sha, tenant_id=tenant.id,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def build_lab(session: Session, client, stub_pdf_render, *, name: str, tenant_name: str, lab_slug: str,
              with_amendment: bool = True) -> LabWorld:
    lab = Lab(session, lab_slug, sample_types=("BX", "TJ"))
    lab.tenant.name = tenant_name
    session.add(lab.tenant)
    session.commit()
    configure(session, lab)
    tenant, branch = lab.tenant, lab.branch
    # The laboratory's creator (owner decision 2026-10-28): what `POST /auth/register/unified` records atomically.
    tenant.created_by_user_id = lab.user.id
    session.add(tenant)
    session.commit()
    world = LabWorld(lab=lab)
    world.admin = create_user(session, tenant, email=f"admin@{lab_slug}.invalid", roles=("admin",))
    world.tech = create_user(session, tenant, email=f"tech@{lab_slug}.invalid", roles=("lab_tech",))
    world.reviewer = create_user(session, tenant, email=f"rev@{lab_slug}.invalid", roles=("reviewer",))
    world.author = create_user(session, tenant, email=f"author@{lab_slug}.invalid", roles=("pathologist",))
    assign_role_by_code(world.reviewer.id, "reviewer", session)
    for member in (lab.user, world.admin, world.tech):
        session.add(UserBranch(user_id=member.id, branch_id=branch.id))  # memberships are PRESERVED master data
    session.commit()

    # ---- master data (all PRESERVED) -----------------------------------------------------------
    physician = RequestingPhysician(
        tenant_id=tenant.id, branch_id=branch.id, physician_code=f"MED-{lab_slug}", first_name="Médico", last_name="Sintético")
    label = Label(tenant_id=tenant.id, name=f"urgente-{lab_slug}", color="#336699")
    session.add_all([physician, label])
    session.add(PriceCatalog(tenant_id=tenant.id, study_type_id=lab.study_type.id, unit_price=350.0))
    template = ReportTemplate(tenant_id=tenant.id, name="Plantilla sintética", template_json={"sections": []})
    session.add(template)
    session.commit()
    session.add(ReportTemplateVersion(
        tenant_id=tenant.id, report_template_id=template.id, version_number=1, configuration=valid_rendering_snapshot()))
    letterhead_logo = f"report-letterheads/{tenant.id}/logo-{lab_slug}.png"
    _store(session, tenant, letterhead_logo, b"PNG-letterhead-" + lab_slug.encode(), content_type="image/png")
    cfg = valid_rendering_snapshot()["presentation"]
    cfg = {**cfg, "header": {**cfg["header"], "logo_key": letterhead_logo}}
    create_default_letterhead(session, tenant, configuration=cfg)
    logo = _store(session, tenant, f"tenants/{tenant.id}/logo/logo.png", b"PNG-tenant-logo", content_type="image/png")
    tenant.logo_storage_id = logo.id
    tenant.logo_url = f"https://fake-cdn.example/{logo.object_key}"
    tenant.default_reviewer_id = world.reviewer.id
    session.add(tenant)
    signature = _store(session, tenant, f"signatures/{world.reviewer.id}/signature.png", b"PNG-signature", content_type="image/png")
    world.reviewer.signature_storage_id = signature.id
    session.add(world.reviewer)
    session.commit()
    world.preserved_keys |= {letterhead_logo, logo.object_key, signature.object_key}
    # Usage must be initialized for the tenant to prove it is recomputed, not zeroed.
    session.execute(text(
        "INSERT INTO tenant_usage (tenant_id, billable_storage_bytes, last_updated) VALUES (:t, 999999, now()) "
        "ON CONFLICT (tenant_id) DO UPDATE SET billable_storage_bytes = 999999"), {"t": str(tenant.id)})
    session.commit()

    # ---- operational content through the real registration route ---------------------------------
    for name_, codes in (("O1", ("BX", "TJ")), ("O2", ("BX",)), ("O3", ("BX",))):
        key = f"seed-{lab_slug}-{name_}-{uuid.uuid4().hex[:8]}"
        r = client.post("/api/v1/laboratory/orders/unified", json=lab.unified(codes),
                        headers={**lab.headers, "Idempotency-Key": key})
        assert r.status_code == 200, r.text
        body = r.json()
        order = body.get("order", body)
        world.orders[name_] = order["id"]
        world.order_codes[name_] = order["order_code"]
        world.keys[name_] = key
    session.expire_all()
    for row in session.execute(text("SELECT sample_code FROM sample WHERE tenant_id = :t ORDER BY sample_code"), {"t": str(tenant.id)}):
        world.sample_codes.append(row[0])

    o1, o2, o3 = (session.get(Order, uuid.UUID(world.orders[n])) for n in ("O1", "O2", "O3"))
    s1 = session.execute(text("SELECT id FROM sample WHERE order_id = :o ORDER BY sample_code"), {"o": str(o1.id)}).scalars().all()
    sample1 = session.get(Sample, s1[0])

    # ---- O1: images + renditions (incl. one object shared with a preserved signature), conversation, links
    processed = _store(session, tenant, f"samples/{tenant.id}/{sample1.id}/processed/p1.jpg", b"JPEG-processed-1" * 40)
    image = SampleImage(tenant_id=tenant.id, branch_id=branch.id, sample_id=sample1.id, storage_id=processed.id, is_primary=True)
    session.add(image)
    session.commit()
    thumb = _store(session, tenant, f"samples/{tenant.id}/{sample1.id}/thumbnails/t1.jpg", b"JPEG-thumb-1" * 10)
    raw = _store(session, tenant, f"samples/{tenant.id}/{sample1.id}/raw/r1.nef", b"RAW-original-1" * 100, content_type="image/x-nikon-nef")
    session.add_all([
        SampleImageRendition(sample_image_id=image.id, kind="thumbnail", storage_id=thumb.id),
        SampleImageRendition(sample_image_id=image.id, kind="original_raw", storage_id=raw.id),
        # A scoped rendition pointing at the reviewer's signature object: a SHARED asset that must survive.
        SampleImageRendition(sample_image_id=image.id, kind="shared_probe", storage_id=signature.id),
    ])
    world.scoped_keys |= {processed.object_key, thumb.object_key, raw.object_key}
    world.shared_key = signature.object_key
    comment = OrderComment(tenant_id=tenant.id, branch_id=branch.id, order_id=o1.id, created_by=world.admin.id, text=f"Comentario {SENTINEL}")
    session.add(comment)
    session.commit()
    session.add(OrderCommentMention(comment_id=comment.id, user_id=world.tech.id))
    session.add(OrderLabel(order_id=o1.id, label_id=label.id))
    session.add(SampleLabel(sample_id=sample1.id, label_id=label.id))
    session.add(OrderRequestingPhysician(order_id=o1.id, requesting_physician_id=physician.id, tenant_id=tenant.id, position=0))
    session.add(Assignment(tenant_id=tenant.id, item_type=AssignmentItemType.LAB_ORDER, item_id=o1.id, assignee_user_id=world.tech.id))
    session.add(Assignment(tenant_id=tenant.id, item_type=AssignmentItemType.SAMPLE, item_id=sample1.id, assignee_user_id=world.tech.id))
    session.add(AuditLog(tenant_id=tenant.id, actor_user_id=world.admin.id, action="ORDER.UPDATE", entity_type="order",
                         entity_id=o1.id, order_id=o1.id, new_values={"notes": SENTINEL}, old_values={"notes": SENTINEL},
                         event_metadata={"note": SENTINEL}, description="Order updated"))
    # An audit row about the laboratory's CATALOG (not about any removed order/sample/report): must survive.
    session.add(AuditLog(tenant_id=tenant.id, actor_user_id=world.admin.id, action="SAMPLE_TYPE.UPDATE", entity_type="sample_type",
                         entity_id=next(iter(lab.sample_types.values())).id, new_values={"name": "Catálogo sintético"},
                         description="Catalog entry updated"))
    session.commit()
    # Draft report on O1.
    draft, draft_version = create_report(session, tenant, branch, o1, status=ReportStatus.DRAFT, created_by=world.author, authored_by=world.author)
    _ensure_review(session, tenant, o1, world.reviewer, draft, ReviewStatus.PENDING)
    world.scoped_keys.add(session.get(StorageObject, draft_version.json_storage_id).object_key)

    # ---- O2: a report signed through the REAL route, then an amendment republished ---------------
    s2 = create_sample(session, tenant, branch, o2, sample_code=f"X-{lab_slug}-1", state=SampleState.READY)
    report, v1 = create_report(session, tenant, branch, o2, status=ReportStatus.APPROVED, pdf_generation_status="READY",
                               created_by=world.author, authored_by=world.author)
    world.scoped_keys.add(session.get(StorageObject, v1.json_storage_id).object_key)
    # The fixture's placeholder PDF is replaced by the real signing flow below and becomes an UNREFERENCED row
    # whose key does not follow the generator's `reports/<tenant>/<report>/...` shape: not provably owned by the
    # scoped report, so the cleanup conservatively KEEPS it (disclosed limit), and the test asserts exactly that.
    placeholder = session.get(StorageObject, v1.pdf_storage_id)
    FakeS3Service.store[placeholder.object_key] = make_pdf_bytes(1)
    world.unprovable_keys.add(placeholder.object_key)
    # A conforming historical official-PDF attempt (unreferenced, billable) of the scoped report: provably owned.
    hist = _store(session, tenant, f"reports/{tenant.id}/{report.id}/versions/1/official/{uuid.uuid4().hex}.pdf",
                  make_pdf_bytes(1), sha="a" * 64, content_type="application/pdf")
    world.scoped_keys.add(hist.object_key)
    _ensure_review(session, tenant, o2, world.reviewer, report, ReviewStatus.APPROVED)
    stub_pdf_render.succeed(make_pdf_bytes(1))
    signed = client.post(f"/api/v1/reports/{report.id}/sign-and-publish", json=CONFIRM, headers=auth_headers(world.reviewer))
    assert signed.status_code == 200, signed.text
    if with_amendment:
        h = auth_headers(world.reviewer)
        opened = client.post(f"/api/v1/reports/{report.id}/amendments", headers=h,
                             json={"category": "CORRECTION", "reason": "Corrección sintética de prueba", **CONFIRM})
        assert opened.status_code == 200, opened.text
        saved = client.post(
            f"/api/v1/reports/{report.id}/new_version", headers=auth_headers(world.author),
            json={"tenant_id": str(tenant.id), "branch_id": str(branch.id), "order_id": str(o2.id), "title": "Reporte",
                  "report": {"base": {}, "sections": {}, "base_order": [], "section_order": [],
                             "amendment": {"change_note": "Se corrige un dato sintético."}},
                  "created_by": str(world.author.id)})
        assert saved.status_code == 200, saved.text
        assert client.post(f"/api/v1/reports/{report.id}/submit", json={}, headers=auth_headers(world.author)).status_code == 200
        assert client.post(f"/api/v1/reports/{report.id}/approve", json={}, headers=h).status_code == 200
        stub_pdf_render.succeed(make_pdf_bytes(2))
        again = client.post(f"/api/v1/reports/{report.id}/sign-and-publish", json={**CONFIRM, "changelog": "Enmienda"}, headers=h)
        assert again.status_code == 200, again.text
    session.expire_all()
    # Every storage object the report graph now references is scoped (except the shared probe).
    for row in session.execute(text(
        "SELECT o.object_key FROM report_version v JOIN storage_object o ON o.id IN (v.pdf_storage_id, v.json_storage_id, v.html_storage_id) "
        "WHERE v.report_id = :r"), {"r": str(report.id)}):
        world.scoped_keys.add(row[0])

    # ---- finance: payments on O2's invoice; O3's stays PENDING; the unified route made the invoices
    for name_ in ("O1", "O2", "O3"):
        inv = session.execute(text("SELECT id FROM invoice WHERE order_id = :o"), {"o": world.orders[name_]}).scalar()
        assert inv is not None, "unified registration must create an invoice"
        world.invoice_ids.append(str(inv))
    inv2 = session.get(Invoice, uuid.UUID(world.invoice_ids[1]))
    payment = Payment(tenant_id=tenant.id, invoice_id=inv2.id, amount=100.0, method="cash", reference="REF-SYN-1")
    session.add(payment)
    inv2.amount_paid = 100.0
    session.add(inv2)
    session.commit()
    world.payment_ids.append(str(payment.id))

    # ---- notifications tied to scoped resources, with a pending email delivery; one unrelated -----
    n, _ = _notify(session, tenant, world.admin, resource_type="report", resource_id=report.id, status="PENDING")
    _notify(session, tenant, world.admin, resource_type="order", resource_id=o1.id, status="SENT")
    create_notification(session, tenant, notification_type="STORAGE_USAGE_APPROACHING", resource_type="tenant", resource_id=tenant.id,
                        title="Aviso de uso (sin relación)")

    # ---- a previously removed capture (tombstone, #11) and its sample --------------------------
    old = Order(tenant_id=tenant.id, branch_id=branch.id, order_code=f"OLD-{lab_slug}-1", removed_at=datetime.utcnow(),
                removal_reason="captura errónea")
    session.add(old)
    session.commit()
    world.orders["REMOVED"] = str(old.id)
    world.order_codes["REMOVED"] = old.order_code
    session.add(Sample(tenant_id=tenant.id, branch_id=branch.id, order_id=old.id, sample_code=f"OLD-S-{lab_slug}", removed_at=datetime.utcnow()))
    session.commit()
    return world


def _ensure_review(session, tenant, order, reviewer, report, status) -> None:
    """The default reviewer is auto-assigned at registration (a PENDING row): link/advance that row instead
    of inserting a second one (a partial unique index forbids two pending rows)."""
    from app.models.report_review import ReportReview
    from sqlmodel import select

    review = session.exec(select(ReportReview).where(
        ReportReview.order_id == order.id, ReportReview.reviewer_user_id == reviewer.id)).first()
    if review is None:
        review = ReportReview(tenant_id=tenant.id, order_id=order.id, reviewer_user_id=reviewer.id)
    review.report_id = report.id
    review.status = status
    session.add(review)
    session.commit()


def _notify(session, tenant, user, *, resource_type, resource_id, status):
    from app.models.notification import NotificationDelivery

    n = create_notification(session, tenant, resource_type=resource_type, resource_id=resource_id,
                            idempotency_key=f"seed:{uuid.uuid4()}")
    create_recipient(session, n, user)
    d = NotificationDelivery(
        notification_id=n.id, tenant_id=tenant.id, recipient_user_id=user.id, recipient_address=user.email,
        channel="EMAIL", status=status, next_attempt_at=datetime.utcnow() if status == "PENDING" else None,
    )
    session.add(d)
    session.commit()
    return n, d


def share_across_tenants(session: Session, target: LabWorld, other: LabWorld) -> str:
    """A storage object referenced by BOTH a scoped sample image of `target` and a sample image of `other`
    (deduplicated upload): the target's cleanup must keep the object because another tenant still uses it."""
    t_img = session.execute(text(
        "SELECT i.id FROM sample_image i WHERE i.tenant_id = :t LIMIT 1"), {"t": str(target.tenant.id)}).scalar()
    shared = _store(session, target.tenant, f"samples/shared/{uuid.uuid4().hex}.jpg", b"JPEG-shared-cross-tenant")
    session.add(SampleImageRendition(sample_image_id=t_img, kind="cross_tenant_probe", storage_id=shared.id))
    o_img = session.execute(text(
        "SELECT i.id FROM sample_image i WHERE i.tenant_id = :t LIMIT 1"), {"t": str(other.tenant.id)}).scalar()
    session.add(SampleImageRendition(sample_image_id=o_img, kind="cross_tenant_probe", storage_id=shared.id))
    session.commit()
    return shared.object_key


@dataclass
class World:
    target: LabWorld
    other: LabWorld


def build_world(session: Session, client, monkeypatch, stub_pdf_render) -> World:
    freeze(monkeypatch, 2026, 10, 15)
    target = build_lab(session, client, stub_pdf_render, name="target", tenant_name="Laboratorio Embajador Ñandú",
                       lab_slug="embajador")
    other = build_lab(session, client, stub_pdf_render, name="other", tenant_name="Otro Laboratorio", lab_slug="otro")
    world = World(target=target, other=other)
    world.shared_cross = share_across_tenants(session, target, other)
    return world


# --------------------------------------------------------------------------- the operator's flow


class Flow:
    """Preview -> confirm over the real HTTP routes."""

    def __init__(self, client, world: LabWorld, headers=None):
        self.client = client
        self.world = world
        self.headers = headers or world.lab.headers
        self.base = base(world.tenant.id)

    def authorization(self, headers=None):
        return self.client.get(f"{self.base}/authorization", headers=headers or self.headers)

    def preview(self, headers=None, **body):
        return self.client.post(f"{self.base}/preview", json=body, headers=headers or self.headers)

    def start(self, token, *, reason="Limpieza de contenido de prueba antes del arranque",
              name=None, password=TEST_USER_PASSWORD, headers=None, **extra):
        body = {"preview_token": token, "reason": reason,
                "confirm_name": self.world.tenant.name if name is None else name, "password": password, **extra}
        return self.client.post(f"{self.base}/operations", json=body, headers=headers or self.headers)

    def run(self, **kwargs):
        """Preview, confirm and (the test client runs background tasks) execute. Returns the operation view."""
        pv = self.preview()
        assert pv.status_code == 200, pv.text
        assert pv.json()["preview_token"], pv.json()["blockers"]
        started = self.start(pv.json()["preview_token"], **kwargs)
        assert started.status_code == 202, started.text
        return self.operation(started.json()["id"])

    def operation(self, operation_id, headers=None):
        r = self.client.get(f"{self.base}/operations/{operation_id}", headers=headers or self.headers)
        assert r.status_code == 200, r.text
        return r.json()

    def retry(self, operation_id, headers=None):
        return self.client.post(f"{self.base}/operations/{operation_id}/retry", headers=headers or self.headers)

    def cancel(self, operation_id, headers=None):
        return self.client.post(f"{self.base}/operations/{operation_id}/cancel", headers=headers or self.headers)
