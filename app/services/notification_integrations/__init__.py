"""Domain -> notification integration layer (Céluma 1.3, Phase 3, Block F).

**This package is the only place where clinical knowledge and notification
knowledge are allowed to meet.**

The boundary, in one direction only::

    clinical domain
          |
          v
    notification_integrations     resolves facts, builds a command
          |
          v
    NotificationService           persists
          |
          v
    Notification / Recipient / Delivery

`app/services/notification.py` imports nothing from `app/models/report.py`,
`order`, `sample` or `events`, and it never will —
`test_notification_service_has_no_clinical_dependencies` asserts it from the
module source. A `NotificationService.notify_report_published()` would put the
clinical state machine inside the service that every future non-clinical event
(storage alerts, billing, plan limits — Phase 4) also has to use.

What a function in this package does
------------------------------------
1. decide whether the transition warrants a notification at all;
2. resolve the recipients (`recipients.py`, user ids only);
3. derive safe template parameters;
4. derive a stable occurrence marker from the persisted transition;
5. call `NotificationService.notify()`.

What it must never do
---------------------
Send email. Create a `NotificationDelivery`. Read a `NotificationPreference`.
Touch SES. Manipulate delivery state. Build a frontend URL. Expose an
endpoint. Every one of those belongs to a layer that already owns it, and
`test_the_integration_layer_reaches_no_delivery_machinery` asserts their
absence from this package's source.

Failure behaviour
-----------------
Every function here returns `None` and swallows nothing itself: containment is
`NotificationService.notify()`'s, which never raises into a caller's
transaction by default. The functions are called *after* the domain transition
and its audit/`OrderEvent` row are in the session and *before* the caller
commits, so notification rows land in the same atomic commit as the transition
that produced them — and a notification failure leaves the clinical operation
committable, which is proven per call site in
`tests/http/test_notification_integration_failures.py`.
"""
from app.services.notification_integrations.assignments import (
    notify_order_assignments_added,
    notify_order_reviewers_added,
    notify_sample_assignments_added,
)
from app.services.notification_integrations.reports import (
    notify_report_pdf_ready,
    notify_report_published,
    notify_report_retracted,
    notify_report_submitted,
)
from app.services.notification_integrations.samples import (
    notify_sample_status_changed,
)

__all__ = [
    "notify_report_submitted",
    "notify_report_pdf_ready",
    "notify_report_published",
    "notify_report_retracted",
    "notify_order_assignments_added",
    "notify_order_reviewers_added",
    "notify_sample_assignments_added",
    "notify_sample_status_changed",
]
