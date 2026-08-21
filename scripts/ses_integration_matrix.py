#!/usr/bin/env python3
"""Drive the real Céluma email path against real Amazon SES.

Céluma 1.3, Phase 5, Block F SES closure — F-017.

The point of this script is what it *does not* do. It does not call
`ses:SendEmail` itself, and it does not construct an `EmailMessage`. It seeds a
`Notification` and a `NotificationDelivery` and then hands the database to
`process_delivery_batch`, which is the same function the deployed worker's loop
calls. Claiming, rendering, sending, `provider_message_id` persistence, error
mapping and retry scheduling are therefore the application's, not this
harness's — a direct `aws ses send-email` would prove SES works and say nothing
about whether Céluma works.

Recipients are AWS mailbox-simulator addresses, so nothing reaches a human and
the account's sandbox state is irrelevant:

    success@simulator.amazonses.com          accepted, then DELIVERY
    bounce@simulator.amazonses.com           accepted, then a hard BOUNCE
    complaint@simulator.amazonses.com        accepted, delivered, then COMPLAINT
    suppressionlist@simulator.amazonses.com  refused — on the account suppression list

Every send is associated with the SES configuration set through
`EMAIL_CONFIGURATION_SET`, which is what makes the events observable at all
(F-016).

Usage:

    python scripts/ses_integration_matrix.py            # the whole matrix
    python scripts/ses_integration_matrix.py success    # one scenario
    python scripts/ses_integration_matrix.py --idempotency

Requires `EMAIL_ENABLED=true`, `EMAIL_PROVIDER=ses`, `EMAIL_SENDER`,
`EMAIL_SES_REGION` and AWS credentials that carry `ses:SendEmail`. It refuses
to run without them rather than producing a green result that means nothing.

Synthetic data only. It creates its own tenant and notification rows and never
reads existing ones.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, create_engine, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.notification import (  # noqa: E402
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryStatus,
    NotificationResourceType,
    NotificationType,
)
from app.models.tenant import Tenant  # noqa: E402
from app.services.email_provider_factory import build_email_provider  # noqa: E402
from app.services.notification_delivery_worker import (  # noqa: E402
    process_delivery_batch,
)

#: The mailbox simulator. Documented at
#: https://docs.aws.amazon.com/ses/latest/dg/send-an-email-from-console.html
SCENARIOS: dict[str, str] = {
    "success": "success@simulator.amazonses.com",
    "bounce": "bounce@simulator.amazonses.com",
    "complaint": "complaint@simulator.amazonses.com",
    "suppression": "suppressionlist@simulator.amazonses.com",
}

TEMPLATE_KEY = "report_published_v1"
NOTIFICATION_TYPE = NotificationType.REPORT_PUBLISHED


def _refuse_unless_configured() -> None:
    problems = settings.validate_email_configuration()
    if not settings.email_enabled:
        problems.append("EMAIL_ENABLED is false; this harness would send nothing")
    if settings.email_provider != "ses":
        problems.append(
            f"EMAIL_PROVIDER is {settings.email_provider!r}; this harness "
            "validates the real SES provider"
        )
    if problems:
        print("REFUSING TO RUN — the email configuration is incomplete:")
        for problem in problems:
            print(f"  - {problem}")
        raise SystemExit(2)


def _tenant(session: Session) -> Tenant:
    """A synthetic laboratory. Reused across scenarios so the matrix's rows
    share a tenant name in the rendered subject, as a real fan-out would."""
    existing = session.exec(
        select(Tenant).where(Tenant.name == "Céluma SES Validation")
    ).first()
    if existing:
        return existing
    tenant = Tenant(name="Céluma SES Validation", legal_name="Céluma SES Validation")
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return tenant


def _seed(session: Session, tenant: Tenant, recipient: str, label: str) -> tuple:
    """One notification and one PENDING email delivery addressed to `recipient`."""
    order_number = f"ORD-SES-{label.upper()}-{uuid.uuid4().hex[:6]}"
    notification = Notification(
        tenant_id=tenant.id,
        type=NOTIFICATION_TYPE,
        title="Reporte publicado",
        body="Hay un reporte publicado disponible.",
        locale="es-MX",
        # `resource_type`/`resource_id` are NOT NULL for every notification —
        # deliberately, so the frontend's single `resource_type -> route`
        # switch is the only place a notification becomes a destination. The
        # id is synthetic; nothing in this harness dereferences it.
        resource_type=NotificationResourceType.REPORT,
        resource_id=uuid.uuid4(),
        # Also NOT NULL. Real callers derive it from the event being
        # announced; here it only has to be unique per row.
        idempotency_key=f"ses-closure:{label}:{uuid.uuid4()}",
        notification_metadata={
            "template_key": TEMPLATE_KEY,
            "template_params": {"order_number": order_number},
        },
    )
    session.add(notification)
    session.flush()

    delivery = NotificationDelivery(
        tenant_id=tenant.id,
        notification_id=notification.id,
        recipient_address=recipient,
        channel=NotificationChannel.EMAIL,
        status=NotificationDeliveryStatus.PENDING,
        # Due *now*. This is not a harness convenience — `select_due_delivery_ids`
        # requires a non-null `next_attempt_at` at or before now, and treats
        # NULL as the terminal "has given up" marker so no code path can
        # accidentally resurrect a row. A seeded row without it is silently
        # never claimed, which is exactly the false-green this line prevents.
        next_attempt_at=datetime.utcnow(),
    )
    session.add(delivery)
    session.commit()
    session.refresh(notification)
    session.refresh(delivery)
    return notification, delivery, order_number


def _observe(session: Session, delivery_id) -> dict:
    session.expire_all()
    row = session.get(NotificationDelivery, delivery_id)
    return {
        "delivery_id": str(row.id),
        "status": str(row.status),
        "attempts": row.attempts,
        "provider_message_id": row.provider_message_id,
        "error_code": row.error_code,
        "next_attempt_at": (
            row.next_attempt_at.isoformat() if row.next_attempt_at else None
        ),
    }


def run_scenario(session: Session, provider, label: str, recipient: str) -> dict:
    tenant = _tenant(session)
    notification, delivery, order_number = _seed(session, tenant, recipient, label)

    print(f"\n=== {label.upper()} → {recipient} ===")
    print(f"  notification_id = {notification.id}")
    print(f"  delivery_id     = {delivery.id}")
    print(f"  order_number    = {order_number}")

    result = process_delivery_batch(session, provider)
    observed = _observe(session, delivery.id)

    print(f"  batch           = claimed={result.claimed} sent={result.sent} failed={result.failed}")
    print(f"  status          = {observed['status']}")
    print(f"  attempts        = {observed['attempts']}")
    print(f"  MessageId       = {observed['provider_message_id']}")
    print(f"  error_code      = {observed['error_code']}")

    return {
        "scenario": label,
        "recipient": recipient,
        "notification_id": str(notification.id),
        "order_number": order_number,
        "batch": {
            "claimed": result.claimed,
            "sent": result.sent,
            "failed": result.failed,
        },
        **observed,
    }


def run_idempotency(session: Session, provider) -> dict:
    """Re-running the worker after an accepted send must not send again.

    The property that matters with a real provider: SES has accepted the
    message and `DELIVERY` has not arrived yet. A worker that treated "no
    delivery event" as "not sent" would double-send every message in the gap.
    """
    tenant = _tenant(session)
    notification, delivery, order_number = _seed(
        session, tenant, SCENARIOS["success"], "idem"
    )

    print("\n=== IDEMPOTENCY — re-running the worker after an accepted send ===")
    print(f"  delivery_id     = {delivery.id}")

    first = process_delivery_batch(session, provider)
    after_first = _observe(session, delivery.id)
    print(f"  pass 1          = claimed={first.claimed} sent={first.sent} "
          f"status={after_first['status']} MessageId={after_first['provider_message_id']}")

    second = process_delivery_batch(session, provider)
    after_second = _observe(session, delivery.id)
    print(f"  pass 2          = claimed={second.claimed} sent={second.sent} "
          f"status={after_second['status']} MessageId={after_second['provider_message_id']}")

    unchanged = (
        after_first["provider_message_id"] == after_second["provider_message_id"]
        and after_first["attempts"] == after_second["attempts"]
    )
    print(f"  no duplicate    = {unchanged} (pass 2 claimed {second.claimed} rows)")

    return {
        "scenario": "idempotency",
        "notification_id": str(notification.id),
        "order_number": order_number,
        "first": {"claimed": first.claimed, "sent": first.sent, **after_first},
        "second": {"claimed": second.claimed, "sent": second.sent, **after_second},
        "no_duplicate_send": unchanged,
    }


def main() -> int:
    _refuse_unless_configured()

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    idempotency_only = "--idempotency" in sys.argv
    selected = args or list(SCENARIOS)

    print("Céluma SES integration matrix")
    print(f"  region             = {settings.effective_email_ses_region}")
    print(f"  sender             = {settings.email_sender}")
    print(f"  configuration set  = {settings.email_configuration_set or '(none)'}")

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    provider = build_email_provider()

    results: list[dict] = []
    with Session(engine) as session:
        if not idempotency_only:
            for label in selected:
                if label not in SCENARIOS:
                    print(f"Unknown scenario {label!r}; known: {', '.join(SCENARIOS)}")
                    return 2
                results.append(run_scenario(session, provider, label, SCENARIOS[label]))
        results.append(run_idempotency(session, provider))

    print("\n--- machine-readable summary ---")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
