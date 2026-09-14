"""Céluma 1.3.1 Block D — server-authoritative resolution of the three
system base fields CEL-131-04 (plan) / CEL-131-07 (prompt label; see
docs/celuma-1.3.1/block-d/report-metadata-contract.md for the ticket-id note)
requires the generated report to show:

    reception_date          earliest non-null Sample.received_at for the
                             report's Order — "the case first entered the lab"
    requesting_physician     the Order's requesting/referring physician —
                             never the pathologist, reviewer or signer
    delivery_date            the report's signature date — unavailable until
                             the report is actually signed

These are NOT a parallel metadata representation: they are ordinary entries
in `ReportContent.base` (`docs/celuma-1.3.1/...`; see
`celuma-frontend/src/models/report.ts` `ReportBaseFieldConfig`), the same
architecture `order_code`, `patient` and `patient_age` already use. The
physician field reuses the EXISTING `requesting_physician` base-field key
rather than inventing a fourth key — Céluma already renders it, client-side,
as "Médico solicitante". What changes here is that the backend becomes the
authority for all three, by the same pattern Block A established for
`signatureMetadata` (`report_presentation.py`): overwrite the specific JSON
keys with server-resolved data, discarding whatever the client submitted, so
a direct API call cannot forge official clinical metadata.

**Presence is server-guaranteed, not client-controlled.** Omitting a key
from the submitted `body["base"]` must not be a way to suppress an official
field: for each of the three, if the report's EFFECTIVE TEMPLATE declares it
(the frozen `rendering_snapshot.template` for V2, `Report.template` for
Legacy — the same distinction Block A's `signature_defaults_from_template`
call site already makes), the backend creates the entry from that
declaration and then sets the authoritative value.

The converse is equally deliberate: a field the effective template does NOT
declare is never injected. A report created before 1.3.1 carries a frozen
template that predates these fields, and it must not silently acquire one on
a later save — its snapshot is what that clinical document was authored
against.

`delivery_date` is handled separately, by `embed_delivery_date_at_signing`,
because that value does not exist until the report is actually signed — see
its own docstring for why it cannot be resolved at the same time as the other
two.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Optional, Union

from sqlmodel import Session, select

from app.models.laboratory import Order, Sample
from app.models.report import ReportVersion
from app.models.requesting_physician import RequestingPhysician
from app.models.storage import StorageObject
from app.models.user import AppUser
from app.services.s3 import S3Service
from app.services.usage_thresholds import record_storage_delta_with_thresholds

logger = logging.getLogger(__name__)

RECEPTION_DATE_BASE_KEY = "reception_date"
DELIVERY_DATE_BASE_KEY = "delivery_date"
# The canonical existing key — see module docstring on why this is not a new
# "physician_name" field.
PHYSICIAN_NAME_BASE_KEY = "requesting_physician"

#: The three server-owned fields, in the order `DEFAULT_BASE_FIELDS` declares
#: them (report.ts) and `v1_3_1` §3b writes them.
_SYSTEM_METADATA_KEYS = (
    PHYSICIAN_NAME_BASE_KEY,
    RECEPTION_DATE_BASE_KEY,
    DELIVERY_DATE_BASE_KEY,
)


def format_date_es_mx(value: Optional[Union[datetime, date]]) -> str:
    """Date-only, unpadded day/month/year — the exact shape
    `toLocaleDateString('es-MX')` already produces everywhere else in the
    product (report_editor.tsx's sample dates, patient_portal.tsx's
    publication date). No server-side date formatter existed before this;
    this is the one canonical implementation, used by both reception_date and
    delivery_date so the two never drift into different formats."""
    if value is None:
        return ""
    return f"{value.day}/{value.month}/{value.year}"


def resolve_reception_date(session: Session, order_id) -> str:
    """Earliest non-null `Sample.received_at` across every sample of the
    order — "the date the case first entered the laboratory". Deliberately
    does NOT fall back to `collected_at`, `Sample.created_at` or
    `Order.created_at`: those are different facts (collection, record
    creation, order registration), and a report showing "unavailable" is
    correct when nobody has actually recorded a reception date, whereas
    inventing one from an unrelated timestamp would not be."""
    earliest = session.exec(
        select(Sample.received_at)
        .where(Sample.order_id == order_id, Sample.received_at.is_not(None))
        .order_by(Sample.received_at.asc())
    ).first()
    return format_date_es_mx(earliest)


def resolve_requesting_physician_name(session: Session, order: Order) -> str:
    """The order's requesting/referring physician. Catalog entry first
    (`Order.requesting_physician_id` -> `RequestingPhysician`, the modern,
    searchable-selector-backed relationship); the legacy free-text
    `Order.requested_by` only when no catalog entry is linked — the same
    fallback chain the report editor already uses client-side, now
    authoritative server-side. Tenant-scoped: a catalog row from another
    tenant (which should never happen, since the order itself is
    tenant-scoped, but checked defensively) is treated as absent rather than
    leaked."""
    if order.requesting_physician_id:
        physician = session.get(RequestingPhysician, order.requesting_physician_id)
        if physician is not None and str(physician.tenant_id) == str(order.tenant_id):
            return physician.full_name or f"{physician.first_name} {physician.last_name}".strip()
    return order.requested_by or ""


def declared_base_fields(template: Optional[dict]) -> dict:
    """The `base` block of an effective template, or `{}` when the template is
    absent or not shaped like one.

    "Effective template" is the frozen `rendering_snapshot.template` for a V2
    report and `Report.template` for a Legacy one — resolved by the caller,
    the same way `signature_defaults_from_template`'s call sites already
    resolve it, so there is one notion of "what this report was authored
    against" rather than two.
    """
    if not isinstance(template, dict):
        return {}
    base = template.get("base")
    return base if isinstance(base, dict) else {}


def apply_authoritative_report_metadata(
    session: Session,
    body: Optional[dict],
    order: Optional[Order],
    template: Optional[dict] = None,
) -> Optional[dict]:
    """Returns `body` with `reception_date` and `requesting_physician` set to
    server-resolved values and `delivery_date` forced back to "unavailable" —
    discarding whatever the caller submitted for all three, and creating any
    of the three the caller omitted but the effective `template` declares.

    Called on every authoring save (`create_report`, `create_report_new_version`)
    for both V2 and Legacy reports alike: these are ordinary base fields, not
    a V2-only concept. `delivery_date` is reset here because every call site
    that reaches this function runs before signing (content saves are refused
    once APPROVED — see `is_content_editable` — and reopening never un-signs a
    report), so any value already sitting there is either "" or, on a direct
    API call, a forged one; either way it is not yet a fact.

    A field is touched when the submitted body already carries it OR the
    effective template declares it. A field that is in neither is left alone:
    see the module docstring on why a pre-1.3.1 report must not acquire one.
    """
    if body is None or order is None:
        return body

    declared = declared_base_fields(template)
    submitted = body.get("base")
    base = dict(submitted) if isinstance(submitted, dict) else {}
    # A body with no `base` at all is only given one when the template
    # actually declares a field to put in it — never as a side effect.
    if not isinstance(submitted, dict) and not any(
        key in declared for key in _SYSTEM_METADATA_KEYS
    ):
        return body

    authoritative_values = {
        RECEPTION_DATE_BASE_KEY: lambda: resolve_reception_date(session, order.id),
        PHYSICIAN_NAME_BASE_KEY: lambda: resolve_requesting_physician_name(
            session, order
        ),
        # Not a fact until the report is signed — see the module docstring.
        DELIVERY_DATE_BASE_KEY: lambda: "",
    }

    created_keys: list[str] = []
    for key in _SYSTEM_METADATA_KEYS:
        existing = base.get(key)
        if not isinstance(existing, dict):
            declaration = declared.get(key)
            if not isinstance(declaration, dict):
                # Neither submitted nor declared: not this report's field.
                continue
            # Rebuilt from the template's own declaration, so the report shows
            # the label and visibility that template configured.
            existing = dict(declaration)
            created_keys.append(key)
        base[key] = {**existing, "value": authoritative_values[key]()}

    if not base:
        return body

    result = dict(body)
    result["base"] = base

    # Keep `base_order` self-consistent for any key just created. The renderer
    # already appends base keys missing from the order (`resolveBaseOrder` in
    # report.ts), so this changes nothing visually — it just stops the stored
    # document from relying on that fallback.
    if created_keys:
        submitted_order = body.get("base_order")
        if isinstance(submitted_order, list):
            order_list = list(submitted_order)
            for key in created_keys:
                if key not in order_list:
                    order_list.append(key)
            result["base_order"] = order_list

    return result


def embed_delivery_date_at_signing(
    session: Session, report_id: str, version: ReportVersion, user: AppUser
) -> None:
    """Writes the signing instant into `base['delivery_date']` — the one
    moment that value exists. A deliberate separate pass rather than folded
    into `embed_signature_metadata_if_required` (report_publishing.py):
    that function returns early when the report does not require a digital
    signature image, but `delivery_date` must be set for EVERY signed report
    regardless of that setting.

    Uses `version.publish_started_at` (the signing claim set by
    `claim_publish`, BEFORE PDF generation), not `datetime.utcnow()` — the
    exact same instant `finalize_publish` later persists as `signed_at`
    (see its own comment on why: the official PDF is rendered between the
    claim and the finalize, so the value baked into that PDF must be the one
    that ends up stored, not a few seconds later). No-op if the version has
    no persisted JSON, the body can't be read, or the template never defined
    the field — mirrors the defensive shape of
    `embed_signature_metadata_if_required`.
    """
    if version.json_storage_id is None:
        return
    json_storage = session.get(StorageObject, version.json_storage_id)
    if json_storage is None:
        return

    s3 = S3Service()
    try:
        report_doc = json.loads(s3.download_text(json_storage.object_key))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not read the persisted report JSON while embedding the "
            "delivery date at signing time; leaving it unset",
            extra={
                "event": "report.delivery_date_embed_failed",
                "report_id": report_id,
                "object_key": json_storage.object_key,
                "error": str(exc),
            },
        )
        return

    if not isinstance(report_doc, dict) or not isinstance(report_doc.get("base"), dict):
        return
    if DELIVERY_DATE_BASE_KEY not in report_doc["base"]:
        return

    signing_instant = version.publish_started_at or datetime.utcnow()
    old_size_bytes = json_storage.size_bytes or 0

    base = dict(report_doc["base"])
    base[DELIVERY_DATE_BASE_KEY] = {
        **base[DELIVERY_DATE_BASE_KEY],
        "value": format_date_es_mx(signing_instant),
    }
    report_doc["base"] = base

    updated_bytes = json.dumps(report_doc, ensure_ascii=False).encode("utf-8")
    info = s3.upload_bytes(
        updated_bytes, key=json_storage.object_key, content_type="application/json"
    )
    json_storage.etag = info.etag
    json_storage.size_bytes = info.size_bytes
    json_storage.version_id = info.version_id
    session.add(json_storage)

    tenant_id = json_storage.tenant_id
    if tenant_id is not None:
        record_storage_delta_with_thresholds(
            session,
            tenant_id,
            (info.size_bytes or 0) - old_size_bytes,
            source="report_json",
            resource_type="report_json",
            actor_id=user.id,
        )

    session.commit()
