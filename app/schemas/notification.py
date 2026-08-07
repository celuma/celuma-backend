"""Notification schemas (Céluma 1.3, Phase 3, Blocks B and D).

Three groups:

  - `NotificationCommand`, the typed input to `NotificationService.notify()`.
    It is deliberately the *only* way a production caller can create a
    notification, and it exposes no `title`/`body` field — see
    app/services/notification_templates.py for why.
  - The four inbox response shapes. None of them exposes the raw
    `notification_metadata` blob (content policy §6); the deep-link fields
    the frontend actually needs (`resource_type`/`resource_id`) are promoted
    to top-level response fields instead.
  - The preference request/response shapes (Block D). Like the inbox
    schemas, none of them carries a `user_id` or a `tenant_id`: the
    preference endpoints are self-scoped and take their scope from the
    token, so an identifier a client could send is an identifier a client
    could get wrong.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.notification import (
    NotificationRecipientStatus,
    NotificationResourceType,
    NotificationSeverity,
    NotificationType,
)
from app.services.notification_templates import NOTIFICATION_TEMPLATE_KEYS


# ---------------------------------------------------------------------------
# Service input
# ---------------------------------------------------------------------------

class NotificationCommand(BaseModel):
    """Everything `NotificationService.notify()` needs, and nothing it must
    not be given.

    There is no `title` or `body` field by design: user-facing text is
    produced exclusively by the template registry from `template_key` +
    `template_params`, so no call site can bypass the content policy by
    passing a pre-rendered string.

    `occurrence_marker` identifies *this occurrence* of the event and is
    opaque to this block. It is combined with type/resource into the
    idempotency key, so the same occurrence retried produces the same key
    while a genuinely new occurrence produces a different one. Block B
    imposes no per-event derivation rule — deriving the marker from a
    server-generated, already-committed transition identifier (an
    `OrderEvent.id`, or `f"{order_event.id}:{added_user_id}"` for assignment)
    is Block F's work at each real trigger point.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    type: NotificationType
    severity: NotificationSeverity = NotificationSeverity.INFO
    resource_type: NotificationResourceType
    resource_id: UUID
    occurrence_marker: str = Field(min_length=1, max_length=120)
    template_key: str
    template_params: Dict[str, Any] = Field(default_factory=dict)
    recipient_user_ids: List[UUID] = Field(default_factory=list)
    created_by: Optional[UUID] = None
    #: When True (the default, per the recipient matrix's cross-cutting rule
    #: 1), `created_by` is removed from the recipient set: the actor already
    #: saw the result synchronously in the UI response.
    exclude_actor: bool = True
    #: Additional non-rendered, non-clinical structured data merged into
    #: `notification_metadata` for audit reconstruction. Never returned by
    #: any API.
    extra_metadata: Optional[Dict[str, Any]] = None

    @field_validator("template_key")
    @classmethod
    def _known_template_key(cls, value: str) -> str:
        if value not in NOTIFICATION_TEMPLATE_KEYS:
            raise ValueError(f"Unknown notification template key: {value}")
        return value

    @field_validator("occurrence_marker")
    @classmethod
    def _clean_occurrence_marker(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("occurrence_marker must not be blank")
        if any(char in stripped for char in "\n\r"):
            raise ValueError("occurrence_marker must not contain line breaks")
        return stripped


# ---------------------------------------------------------------------------
# Inbox responses
# ---------------------------------------------------------------------------

class NotificationListItem(BaseModel):
    """One inbox row.

    `recipient_id` and `notification_id` are named explicitly and neither is
    called `id`. The Block A API proposal used `id` ambiguously; the read
    endpoints act on `NotificationRecipient.id`, so conflating the two would
    make it possible for a client to send the shared notification id to a
    per-user endpoint and get a 404 with no hint why.
    """

    recipient_id: str
    notification_id: str
    type: NotificationType
    severity: NotificationSeverity
    title: str
    body: Optional[str] = None
    resource_type: str
    resource_id: str
    status: NotificationRecipientStatus
    created_at: datetime
    read_at: Optional[datetime] = None


class NotificationListResponse(BaseModel):
    items: List[NotificationListItem]
    #: Opaque cursor for the next (older) page, or null when the caller has
    #: reached the end. Encoded by app/services/cursor_pagination.py — the
    #: same format order comments already use.
    next_cursor: Optional[str] = None


class NotificationUnreadCountResponse(BaseModel):
    unread_count: int


class NotificationRecipientReadResponse(BaseModel):
    recipient_id: str
    status: NotificationRecipientStatus
    read_at: Optional[datetime] = None


class NotificationReadAllResponse(BaseModel):
    updated_count: int


# ---------------------------------------------------------------------------
# Preferences (Block D)
# ---------------------------------------------------------------------------

class NotificationPreferenceItem(BaseModel):
    """One notification type's **effective** preference for the caller.

    "Effective", not "stored": every one of the six types appears in a
    response whether or not a `notification_preference` row exists, so a
    client never has to reason about what a missing entry would have meant.
    `is_explicit` is how a client tells the two apart, and it is the only
    thing that distinguishes "the user chose this" from "this is the
    default".
    """

    notification_type: NotificationType
    #: Always true in Céluma 1.3 and not user-editable — see
    #: app/services/notification_policies.py. Returned so a future block can
    #: expose it without a contract change.
    in_app_enabled: bool
    #: Already bounded by policy: false whenever `email_supported` is false,
    #: regardless of what any stored row says.
    email_enabled: bool
    #: Whether this type may use email at all. When false the client must
    #: render the control disabled — and the API refuses to enable it.
    email_supported: bool
    #: True when a row backs these values; false when they come from policy
    #: defaults and no row exists.
    is_explicit: bool
    #: The row's timestamp, or null for an implicit default.
    updated_at: Optional[datetime] = None


class NotificationPreferenceListResponse(BaseModel):
    """All six types, in a stable order, for the authenticated user."""

    preferences: List[NotificationPreferenceItem]


class NotificationPreferenceUpdateItem(BaseModel):
    """One requested change.

    `extra="forbid"` is doing real work: it is what rejects a client that
    tries to send `user_id`, `tenant_id`, `channel` or `in_app_enabled`.
    Rather than accepting-and-ignoring those fields — which would let a
    caller believe it had disabled in-app notifications — the request fails
    with 422 and names the field.
    """

    model_config = ConfigDict(extra="forbid")

    notification_type: NotificationType
    email_enabled: bool


class NotificationPreferenceUpdateRequest(BaseModel):
    """A **partial** batch: only the types the user actually changed.

    Deliberately not a full-set replace, despite `PUT` normally implying one
    in this codebase (`PUT /orders/{id}/reviewers`). A full replace here
    would mean a client that knows about five of six types silently reverts
    the sixth to its default — a real hazard while `NotificationType` is
    still growing. The resource being replaced is "the set of overrides the
    caller is asserting right now", and unmentioned types are left exactly
    as they are.
    """

    model_config = ConfigDict(extra="forbid")

    preferences: List[NotificationPreferenceUpdateItem] = Field(
        min_length=1, max_length=len(NotificationType)
    )

    @model_validator(mode="after")
    def _no_duplicate_types(self) -> "NotificationPreferenceUpdateRequest":
        """Reject a type mentioned twice in one request.

        Last-write-wins would be a plausible alternative, but a request
        carrying both `{X: true}` and `{X: false}` has no discoverable
        intent — it is a client bug, and resolving it silently means the user
        sees a switch flip to a value they did not pick.
        """
        seen: set[NotificationType] = set()
        duplicated: set[str] = set()
        for item in self.preferences:
            if item.notification_type in seen:
                duplicated.add(item.notification_type.value)
            seen.add(item.notification_type)
        if duplicated:
            raise ValueError(
                f"Duplicate notification_type in request: {sorted(duplicated)}"
            )
        return self


#: The update response is the full effective list, identical in shape to the
#: GET. A client never has to merge its own optimistic guess with a partial
#: answer — it replaces local state with what the server actually resolved,
#: including the policy-bounded values it may have been unaware of.
NotificationPreferenceUpdateResponse = NotificationPreferenceListResponse
