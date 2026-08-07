"""Notification delivery-policy registry (Céluma 1.3, Phase 3, Block D).

This registry answers one question per notification type: **which channels is
this event allowed to use at all?** It is deliberately separate from
`app/services/notification_templates.py`, which answers a different question
— *what does this event say?*

Keeping them apart matters because they change for different reasons and
have different blast radii. Correcting copy ships a new `_v2` template key and
touches nothing about delivery; deciding that an event must stop generating
email is a channel decision that must hold even for users whose stored
preference still says `email_enabled = true`. Folding the two together would
mean a copy fix and a delivery-policy change were the same edit.

Precedence
----------
The policy is the outer bound, the user preference is the inner one:

    effective_email_enabled = email_supported AND (explicit override,
                                                   else email_default_enabled)

A stale `notification_preference` row that says `email_enabled = true` for a
type whose policy says `email_supported = false` therefore resolves to
**false**. The registry wins; a row is never deleted or rewritten to enforce
it (a read must not mutate).

`in_app_required`
-----------------
True for every type in Céluma 1.3. Internal notifications are the durable
operational channel — Block C's Notification Center is the primary surface,
and event-level "mandatory vs optional" policy is not designed yet — so no
user-facing control disables in-app delivery and the preference API rejects
`in_app_enabled = false`. The field is modeled now because the moment a
genuinely optional event exists (a future digest, a marketing-shaped alert),
the distinction has to be expressible per type rather than assumed globally.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from app.models.notification import NotificationType


@dataclass(frozen=True)
class NotificationDeliveryPolicy:
    """Which channels a notification type may use, before user preference."""

    #: In-app delivery cannot be switched off for this type. True everywhere
    #: in Céluma 1.3 — see the module docstring.
    in_app_required: bool
    #: Whether an EMAIL `NotificationDelivery` row may ever be created for
    #: this type. False here is absolute: no preference can re-enable it.
    email_supported: bool
    #: The effective email preference when the user has no explicit row.
    #: Meaningless (and asserted False) when `email_supported` is False, so
    #: that "value equals the default" is one uniform rule on the write path.
    email_default_enabled: bool

    def __post_init__(self) -> None:
        if not self.email_supported and self.email_default_enabled:
            raise ValueError(
                "email_default_enabled cannot be True for an unsupported channel"
            )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
#
# Six entries, one per NotificationType. No speculative type is declared: a
# future event gets its policy written beside its template, in the block that
# introduces it.
#
# Why SAMPLE_STATUS_CHANGED is the one type with email_supported = False,
# confirmed against Block A rather than assumed:
#
#   - It is the only event in the MUST_HAVE_1_3 set that fires once per
#     PATCH on a state machine a lab tech walks through in a single sitting.
#     Block A's event inventory names the sequence explicitly
#     (RECEIVED -> PROCESSING -> READY), and folds SAMPLE_DAMAGED /
#     SAMPLE_CANCELLED into the same trigger point — so one sample can
#     legitimately produce several notifications within minutes.
#   - Its recipient rule is the broadest in the matrix: *all* order
#     assignees, plus the report author when a report exists. Every other
#     MUST_HAVE event resolves to a narrow, action-bearing set (the assigned
#     reviewers, the eligible signer, the newly added assignee).
#   - Multiply those and this single type is the concrete instance of the
#     "notification storms" and "notification fatigue" risks named in
#     Céluma1.3-Phase3.md §10. Every other type is a discrete, low-frequency
#     workflow milestone that a recipient plausibly wants to hear about while
#     away from the application.
#   - Nothing is lost: in-app delivery is unaffected, and Block A's recipient
#     matrix never proposed an email path for this event (its only email
#     recipients anywhere are the requesting physician on publish/retract).
#
# The remaining five are email_supported = True because each one asks a
# specific person to do a specific next thing, or records a terminal change
# of state on a signed clinical document.

NOTIFICATION_DELIVERY_POLICIES: Dict[NotificationType, NotificationDeliveryPolicy] = {
    # A reviewer is being asked to review. Actionable, one per submission.
    NotificationType.REPORT_SUBMITTED: NotificationDeliveryPolicy(
        in_app_required=True,
        email_supported=True,
        email_default_enabled=True,
    ),
    # The official PDF is ready to sign. Actionable, one per successful
    # generation.
    NotificationType.REPORT_PDF_READY: NotificationDeliveryPolicy(
        in_app_required=True,
        email_supported=True,
        email_default_enabled=True,
    ),
    # Terminal transition on a signed clinical document. Block E's physician
    # email path hangs off this type.
    NotificationType.REPORT_PUBLISHED: NotificationDeliveryPolicy(
        in_app_required=True,
        email_supported=True,
        email_default_enabled=True,
    ),
    # A published report was withdrawn — the one event where *not* hearing
    # about it promptly is actively harmful.
    NotificationType.REPORT_RETRACTED: NotificationDeliveryPolicy(
        in_app_required=True,
        email_supported=True,
        email_default_enabled=True,
    ),
    # Work was handed to this specific person. Narrow by construction: only
    # the newly added user is a recipient.
    NotificationType.ASSIGNMENT_ADDED: NotificationDeliveryPolicy(
        in_app_required=True,
        email_supported=True,
        email_default_enabled=True,
    ),
    # In-app only — see the note above.
    NotificationType.SAMPLE_STATUS_CHANGED: NotificationDeliveryPolicy(
        in_app_required=True,
        email_supported=False,
        email_default_enabled=False,
    ),
}


def get_delivery_policy(
    notification_type: NotificationType,
) -> NotificationDeliveryPolicy:
    """The policy for `notification_type`.

    Raises `KeyError` for a type with no entry rather than inventing a
    permissive default: a type that reached this function without a declared
    policy is a wiring bug, and defaulting to "email allowed" would let it
    generate email nobody signed off on.
    """
    return NOTIFICATION_DELIVERY_POLICIES[notification_type]


def email_supported(notification_type: NotificationType) -> bool:
    return get_delivery_policy(notification_type).email_supported


def default_email_enabled(notification_type: NotificationType) -> bool:
    """The effective email default with no explicit preference row.

    `email_supported` is folded in here so callers never have to remember to
    check it: an unsupported type defaults to False, which makes "the value
    equals the default" the single rule the write path needs.
    """
    policy = get_delivery_policy(notification_type)
    return policy.email_supported and policy.email_default_enabled
