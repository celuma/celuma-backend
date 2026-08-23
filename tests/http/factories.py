"""Plain (non-fixture) factory helpers for HTTP integration tests.

Kept intentionally simple — direct SQLModel inserts, not a full factory
framework — per Céluma1.3-Fase2.md §11 ("Do not build an unnecessarily
complex testing framework").
"""
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Iterable, Optional

from sqlmodel import Session, select

from app.core.rbac import assign_role_by_code
from app.core.security import create_jwt, hash_password
from app.models.laboratory import Order
from app.models.report import Report, ReportVersion
from app.models.report_template_version import ReportTemplateVersion, ReportTemplateVersionStatus
from app.models.storage import StorageObject
from app.models.tenant import Branch, Tenant
from app.models.user import AppUser

if TYPE_CHECKING:
    # Céluma 1.3 Phase 5, Block F §29: `Notification` and
    # `NotificationRecipient` are imported inside the factory bodies (to keep
    # module import cheap) but named in quoted return annotations, which the
    # backend lint gate flagged as five F821 "undefined name" errors — the
    # only findings in `app`, `tests` and `alembic`. Declaring them here is
    # the whole fix: the annotations are never evaluated at runtime, so this
    # changes no behaviour and adds no import cost.
    from app.models.notification import Notification, NotificationRecipient
    from app.models.report_letterhead import ReportLetterhead
    from app.models.report_letterhead_version import ReportLetterheadVersion


# Céluma 1.3 Phase 2, Block B, Story B10: the RBAC catalog (permissions,
# system roles, role<->permission links) is real seed data applied by
# alembic/versions/v1_0_0_initial_schema.py — the HTTP test database is
# built by running the actual migration chain (see conftest.py), so it is
# already populated. Tests assign real system role codes via
# `assign_role_by_code`, exactly like production registration does; there
# is no separate/duplicated test-only RBAC fixture.
#
# "superuser" holds every permission (_ALL_PERMISSION_CODES in that
# migration) — used as the default "can do everything" test role.
# "reviewer" (added in v1_1_0) holds reports:sign + reports:read, matching
# ROLE_REVIEWER in app/core/rbac.py.


def create_tenant(session: Session, *, name: str = "Test Tenant", reports_v2_enabled: bool = False) -> Tenant:
    tenant = Tenant(name=name, reports_v2_enabled=reports_v2_enabled)
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return tenant


def create_branch(session: Session, tenant: Tenant, *, code: str = "MAIN") -> Branch:
    branch = Branch(tenant_id=tenant.id, code=code, name=f"Branch {code}")
    session.add(branch)
    session.commit()
    session.refresh(branch)
    return branch


def create_user(
    session: Session,
    tenant: Tenant,
    *,
    email: str,
    roles: Iterable[str] = ("superuser",),
) -> AppUser:
    user = AppUser(
        tenant_id=tenant.id,
        email=email,
        username=email.split("@")[0],
        full_name="Test User",
        first_name="Test",
        last_name="User",
        hashed_password=hash_password("irrelevant-in-tests"),
    )
    session.add(user)
    session.flush()
    for role_code in roles:
        assign_role_by_code(user.id, role_code, session)
    session.commit()
    session.refresh(user)
    return user


def auth_headers(user: AppUser) -> dict[str, str]:
    token = create_jwt(sub=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def create_order(session: Session, tenant: Tenant, branch: Branch, *, order_code: str = "ORD-1") -> Order:
    order = Order(tenant_id=tenant.id, branch_id=branch.id, order_code=order_code)
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


def create_storage_object(
    session: Session, *, key: str = "logos/test.png", tenant: Optional[Tenant] = None
) -> StorageObject:
    obj = StorageObject(
        provider="aws",
        region="mx-test-1",
        bucket="celuma-test-bucket",
        object_key=key,
        content_type="image/png",
        size_bytes=1234,
        tenant_id=(tenant.id if tenant is not None else None),
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def valid_presentation(**overrides) -> dict:
    """A ReportPresentationSnapshotV2-shaped payload — post-Phase-2
    remediation, used as the `configuration` of a
    ReportLetterheadVersionCreate request. Field-for-field the same shape
    as valid_rendering_snapshot()'s `presentation` key, kept separate so
    letterhead tests don't depend on the template-version helper."""
    base = {
        "paper": {
            "size": "LETTER",
            "orientation": "PORTRAIT",
            "margins_cm": {"top": 2.0, "right": 2.0, "bottom": 2.0, "left": 2.0},
        },
        "header": {
            "enabled": True,
            "institution_name": "Céluma Labs",
            "subtitle": "Diagnóstico Anatomopatológico",
            "address": "Av. Siempre Viva 123",
            "phone": "+52 55 1234 5678",
            "email": "contacto@celuma.example",
        },
        "footer": {"enabled": True, "custom_text": "Confidencial", "show_page_number": True},
        "style": {"primary_color": "#336699"},
    }
    base.update(overrides)
    return base


def create_letterhead(session: Session, tenant: Tenant, *, name: str = "Default Letterhead") -> "ReportLetterhead":
    from app.models.report_letterhead import ReportLetterhead

    letterhead = ReportLetterhead(tenant_id=tenant.id, name=name, is_active=True)
    session.add(letterhead)
    session.commit()
    session.refresh(letterhead)
    return letterhead


def create_letterhead_version(
    session: Session,
    tenant: Tenant,
    letterhead: "ReportLetterhead",
    *,
    version_number: int = 1,
    status: str = "PUBLISHED",
    configuration: Optional[dict] = None,
) -> "ReportLetterheadVersion":
    from app.models.report_letterhead_version import ReportLetterheadVersion

    version = ReportLetterheadVersion(
        tenant_id=tenant.id,
        report_letterhead_id=letterhead.id,
        version_number=version_number,
        schema_version=2,
        configuration=configuration or valid_presentation(),
        status=status,
    )
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


def create_default_letterhead(
    session: Session,
    tenant: Tenant,
    *,
    configuration: Optional[dict] = None,
    name: str = "Membrete predeterminado",
) -> tuple["ReportLetterhead", "ReportLetterheadVersion"]:
    """The tenant's default letterhead, with its ACTIVE version ready.

    Third post-Phase-2 remediation: since V2 creation requires a resolvable
    letterhead (and blocks with 409 if there is none — see
    deterministic-letterhead-resolution-contract.md), any test that creates
    a V2 report needs this. Default `configuration` reproduces the
    `presentation` block of `valid_rendering_snapshot()`, so tests that
    already asserted on that presentation keep holding as-is.
    """
    from app.models.report_letterhead import ReportLetterhead

    letterhead = ReportLetterhead(
        tenant_id=tenant.id, name=name, is_active=True, is_default=True
    )
    session.add(letterhead)
    session.commit()
    session.refresh(letterhead)
    version = create_letterhead_version(
        session,
        tenant,
        letterhead,
        status="ACTIVE",
        configuration=configuration or valid_rendering_snapshot()["presentation"],
    )
    return letterhead, version


def valid_rendering_snapshot(**overrides) -> dict:
    """A ReportRenderingSnapshotV2-shaped payload usable as the `configuration`
    of a `ReportTemplateVersionCreate` request."""
    base = {
        "schema_version": 2,
        "template": {
            "base": {"diagnosis": {"label": "Diagnóstico", "type": "text"}},
            "sections": {},
            "base_order": ["diagnosis"],
            "section_order": [],
        },
        "presentation": {
            "paper": {
                "size": "LETTER",
                "orientation": "PORTRAIT",
                "margins_cm": {"top": 2.0, "right": 2.0, "bottom": 2.0, "left": 2.0},
            },
            "header": {
                "enabled": True,
                "institution_name": "Céluma Labs",
                "subtitle": "Diagnóstico Anatomopatológico",
                "address": "Av. Siempre Viva 123",
                "phone": "+52 55 1234 5678",
                "email": "contacto@celuma.example",
            },
            "footer": {"enabled": True, "custom_text": "Confidencial", "show_page_number": True},
            "style": {"primary_color": "#336699"},
        },
    }
    base.update(overrides)
    return base


def create_published_v2_report_directly(
    session: Session,
    tenant: Tenant,
    branch: Branch,
    order: Order,
    template_version: ReportTemplateVersion,
    *,
    status,
) -> tuple[Report, ReportVersion]:
    """Insert a Report + ReportVersion directly at a given status, bypassing
    the submit/approve/sign HTTP lifecycle (which lives outside reports.py's
    template-version scope). Used only to test status-guard endpoints
    (Story B9), not to exercise the review workflow itself.
    """
    report = Report(
        tenant_id=tenant.id,
        branch_id=branch.id,
        order_id=order.id,
        status=status,
    )
    session.add(report)
    session.flush()
    version = ReportVersion(
        report_id=report.id,
        version_no=1,
        is_current=True,
        schema_version=2,
        template_version_id=template_version.id,
    )
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version


# ---------------------------------------------------------------------------
# Céluma 1.3 Phase 3, Block B — notifications
# ---------------------------------------------------------------------------
#
# Block B wires no real clinical trigger, so every notification in these tests
# is seeded here or created through NotificationService directly. Keeping the
# seeding helper separate from the service means the API tests can build an
# arbitrary inbox state (already-read rows, old timestamps) that the service's
# own contract would not let them produce.


def create_notification(
    session: Session,
    tenant: Tenant,
    *,
    notification_type: str = "REPORT_SUBMITTED",
    title: str = "Reporte listo para revisión — Orden ORD-1",
    body: Optional[str] = "El reporte fue enviado a revisión por Dra. Martínez.",
    resource_type: str = "report",
    resource_id: Optional[uuid.UUID] = None,
    idempotency_key: Optional[str] = None,
    severity: str = "INFO",
    created_at: Optional[datetime] = None,
    created_by: Optional[uuid.UUID] = None,
    metadata: Optional[dict] = None,
) -> "Notification":
    from app.models.notification import Notification

    notification = Notification(
        tenant_id=tenant.id,
        type=notification_type,
        severity=severity,
        title=title,
        body=body,
        resource_type=resource_type,
        resource_id=resource_id or uuid.uuid4(),
        idempotency_key=idempotency_key or f"seed:{uuid.uuid4()}",
        created_at=created_at or datetime.utcnow(),
        created_by=created_by,
        notification_metadata=metadata
        or {"template_key": "report_submitted_v1", "template_params": {"order_number": "ORD-1"}},
    )
    session.add(notification)
    session.commit()
    session.refresh(notification)
    return notification


def create_recipient(
    session: Session,
    notification: "Notification",
    user: AppUser,
    *,
    status: str = "UNREAD",
    read_at: Optional[datetime] = None,
) -> "NotificationRecipient":
    from app.models.notification import NotificationRecipient

    recipient = NotificationRecipient(
        notification_id=notification.id,
        tenant_id=notification.tenant_id,
        user_id=user.id,
        status=status,
        # The service always copies the parent's timestamp; seeded rows do the
        # same so ordering assertions match production behaviour.
        created_at=notification.created_at,
        read_at=read_at,
    )
    session.add(recipient)
    session.commit()
    session.refresh(recipient)
    return recipient


def create_inbox_notification(
    session: Session,
    tenant: Tenant,
    user: AppUser,
    **kwargs,
) -> tuple["Notification", "NotificationRecipient"]:
    """One notification addressed to exactly one user — the common case."""
    status = kwargs.pop("status", "UNREAD")
    read_at = kwargs.pop("read_at", None)
    notification = create_notification(session, tenant, **kwargs)
    recipient = create_recipient(
        session, notification, user, status=status, read_at=read_at
    )
    return notification, recipient


# ---------------------------------------------------------------------------
# Céluma 1.3 Phase 3, Block F — domain integrations
# ---------------------------------------------------------------------------
#
# Block F is the first block whose notifications come from real clinical
# transitions, so its tests need the clinical rows those transitions read:
# samples, order/sample assignments, and reviewer records. Direct SQLModel
# inserts, same as everything above — the endpoints that create these are not
# what Block F is testing, and driving them through HTTP would make every
# notification assertion depend on an unrelated endpoint's contract.


def create_sample(
    session: Session,
    tenant: Tenant,
    branch: Branch,
    order: Order,
    *,
    sample_code: str = "S-1",
    state=None,
):
    from app.models.enums import SampleState, SampleType
    from app.models.laboratory import Sample

    sample = Sample(
        tenant_id=tenant.id,
        branch_id=branch.id,
        order_id=order.id,
        sample_code=sample_code,
        type=SampleType.BIOPSIA,
        state=state or SampleState.RECEIVED,
    )
    session.add(sample)
    session.commit()
    session.refresh(sample)
    return sample


def assign_to_order(session: Session, tenant: Tenant, order: Order, user: AppUser):
    """A live LAB_ORDER assignment.

    `unassigned_at` stays NULL — the resolvers filter on it, because
    `_sync_assignments` soft-unassigns and a test that ignored the column
    would pass against a resolver that notified every user ever assigned.
    """
    from app.models.assignment import Assignment
    from app.models.enums import AssignmentItemType

    assignment = Assignment(
        tenant_id=tenant.id,
        item_type=AssignmentItemType.LAB_ORDER,
        item_id=order.id,
        assignee_user_id=user.id,
    )
    session.add(assignment)
    session.commit()
    session.refresh(assignment)
    return assignment


def add_order_reviewer(
    session: Session, tenant: Tenant, order: Order, user: AppUser, *, report=None
):
    """A `ReportReview` row — the table reviewers actually live in.

    Not `Assignment`: reviewers were decoupled from the assignment table, and
    the resolvers read `report_review`.
    """
    from app.models.enums import ReviewStatus
    from app.models.report_review import ReportReview

    review = ReportReview(
        tenant_id=tenant.id,
        order_id=order.id,
        report_id=(report.id if report is not None else None),
        reviewer_user_id=user.id,
        status=ReviewStatus.PENDING,
    )
    session.add(review)
    session.commit()
    session.refresh(review)
    return review


def create_report(
    session: Session,
    tenant: Tenant,
    branch: Branch,
    order: Order,
    *,
    status=None,
    created_by: Optional[AppUser] = None,
    authored_by: Optional[AppUser] = None,
    version_no: int = 1,
    pdf_generation_status: Optional[str] = None,
):
    """A Report plus its current ReportVersion, at an arbitrary status.

    Distinct from `create_published_v2_report_directly`, which needs a
    template version and exists for the V2 status-guard tests. Block F's
    integrations never read the template, so this stays minimal and lets a
    test set the author fields the recipient resolvers actually use.
    """
    from app.models.enums import ReportStatus
    from app.models.report import Report, ReportVersion

    report = Report(
        tenant_id=tenant.id,
        branch_id=branch.id,
        order_id=order.id,
        status=status or ReportStatus.DRAFT,
        created_by=(created_by.id if created_by is not None else None),
    )
    session.add(report)
    session.flush()
    version = ReportVersion(
        report_id=report.id,
        version_no=version_no,
        is_current=True,
        authored_by=(authored_by.id if authored_by is not None else None),
        pdf_generation_status=pdf_generation_status,
    )
    if pdf_generation_status == "READY":
        # `ck_report_version_pdf_ready_requires_artifact` (v1_3_0) is the
        # database-level statement of Block E's core invariant: no READY
        # status without a real, hashed, persisted artifact. A fixture that
        # set the status alone would be asserting against a state the
        # production schema forbids.
        storage = create_storage_object(
            session, key=f"reports/{report.id}/official/{uuid.uuid4().hex}.pdf", tenant=tenant
        )
        version.pdf_storage_id = storage.id
        version.pdf_sha256 = "0" * 64
        version.pdf_size_bytes = 1024
        version.pdf_page_count = 1
    session.add(version)
    order.report_id = report.id
    session.add(order)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version


def notifications_for(session: Session, user: AppUser, *, notification_type=None):
    """Every notification addressed to `user`, newest first.

    Returns `(Notification, NotificationRecipient)` pairs, because assertions
    need both — the frozen content lives on one and the inbox state on the
    other.
    """
    from app.models.notification import Notification, NotificationRecipient

    statement = (
        select(Notification, NotificationRecipient)
        .join(NotificationRecipient, NotificationRecipient.notification_id == Notification.id)
        .where(NotificationRecipient.user_id == user.id)
        .order_by(Notification.created_at.desc(), Notification.id)
    )
    if notification_type is not None:
        value = getattr(notification_type, "value", notification_type)
        statement = statement.where(Notification.type == value)
    return list(session.exec(statement).all())


def deliveries_for(session: Session, notification_id):
    from app.models.notification import NotificationDelivery

    return list(
        session.exec(
            select(NotificationDelivery).where(
                NotificationDelivery.notification_id == notification_id
            )
        ).all()
    )


def set_email_preference(
    session: Session, tenant: Tenant, user: AppUser, notification_type, *, enabled: bool
):
    """An explicit `notification_preference` override."""
    from app.models.notification import NotificationPreference

    preference = NotificationPreference(
        tenant_id=tenant.id,
        user_id=user.id,
        notification_type=getattr(notification_type, "value", notification_type),
        in_app_enabled=True,
        email_enabled=enabled,
    )
    session.add(preference)
    session.commit()
    return preference


def create_patient(
    session: Session,
    tenant: Tenant,
    branch: Branch,
    *,
    patient_code: str = "PAT-1",
    first_name: str = "Paciente",
    last_name: str = "Sintético",
):
    """A patient, needed only because `SampleDetailResponse` requires one.

    Block F's notifications never read patient data — that is the point of the
    sentinel sweep — but the sample-assignee endpoint's *response model* does,
    so a test driving that endpoint needs an order with a patient attached.
    """
    from app.models.patient import Patient

    patient = Patient(
        tenant_id=tenant.id,
        branch_id=branch.id,
        patient_code=patient_code,
        first_name=first_name,
        last_name=last_name,
    )
    session.add(patient)
    session.commit()
    session.refresh(patient)
    return patient
