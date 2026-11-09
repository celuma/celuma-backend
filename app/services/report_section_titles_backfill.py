"""Operator-only backfill. A report and all of its eligible versions share one transaction."""

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, text
from sqlmodel import Session, select

from app.core.maintenance_guard import assert_not_in_maintenance
from app.models.audit import AuditLog
from app.models.enums import EventType, OrderStatus
from app.models.events import OrderEvent
from app.models.laboratory import Order
from app.models.report import Report, ReportTemplate, ReportVersion
from app.models.report_amendment import AMENDMENT_STATE_OPEN, ReportAmendment
from app.models.storage import StorageObject
from app.schemas.report_template_version import ReportRenderingSnapshotV2
from app.services.order_lifecycle import lock_report_order
from app.services.report_authorization import is_signed_version
from app.services.report_section_titles import rename_description_labels, rename_report_description_labels
from app.services.report_template_autoversion import snapshot_and_activate_template_version
from app.services.report_template_hash import hash_clinical_template_block
from app.services.report_v2_policy import UNSIGNED_STATUSES
from app.services.s3 import S3Service
from app.services.usage_thresholds import record_storage_delta_with_thresholds


class NeedsReview(ValueError):
    """An inconsistency prevents a safe automatic rewrite. Messages are reason codes only."""


@dataclass(frozen=True)
class JsonChange:
    version: ReportVersion
    storage: StorageObject
    before: bytes
    after: bytes
    key: str


def update_template(session: Session, template: ReportTemplate, actor_id: UUID | None) -> bool:
    before = template.template_json
    if not isinstance(before, dict):
        raise NeedsReview("invalid_template_json")
    after = rename_description_labels(before)
    if before is after:
        return False
    if actor_id is not None:
        assert_not_in_maintenance(session, template.tenant_id)
        template.template_json = after
        snapshot_and_activate_template_version(session, template, actor_id, commit=False)
        session.add(AuditLog(
            tenant_id=template.tenant_id, actor_user_id=actor_id,
            action="REPORT_TEMPLATE.BACKFILL_DESCRIPTION_LABELS",
            entity_type="report_template", entity_id=template.id,
            old_values={"template_hash": hash_clinical_template_block(before)},
            new_values={"template_hash": hash_clinical_template_block(after)},
        ))
    return True


def _eligible_versions(session: Session, report: Report, versions: list[ReportVersion]) -> list[ReportVersion]:
    current = [version for version in versions if version.is_current]
    if len(current) != 1:
        raise NeedsReview("missing_or_ambiguous_current_version")

    amendments = session.exec(select(ReportAmendment).where(ReportAmendment.report_id == report.id)).all()
    open_cycles = [amendment for amendment in amendments if amendment.state == AMENDMENT_STATE_OPEN]
    signed = [version for version in versions if is_signed_version(version)]
    if not open_cycles:
        if amendments or report.published_at or signed or session.exec(
            select(OrderEvent.id).where(
                OrderEvent.order_id == report.order_id,
                OrderEvent.event_type.in_((EventType.REPORT_SIGNED, EventType.REPORT_PUBLISHED)),
            ).limit(1)
        ).first() is not None:
            raise NeedsReview("previously_issued")
        if any(version.amendment_id is not None for version in versions):
            raise NeedsReview("invalid_amendment_links")
        return versions

    amendment = open_cycles[0]
    delivered = max(signed, key=lambda version: version.version_no, default=None)
    if (
        len(open_cycles) != 1 or amendment.tenant_id != report.tenant_id
        or amendment.published_version_id is not None or amendment.published_at is not None
        or current[0].amendment_id != amendment.id or delivered is None or delivered.signed_at is None
        or delivered.id != amendment.source_version_id or delivered.schema_version != 2
        or any(version.amendment_id == amendment.id for version in signed)
    ):
        raise NeedsReview("invalid_open_amendment")
    published_ids = {item.published_version_id for item in amendments}
    candidates = [version for version in versions if version.amendment_id == amendment.id]
    if any(version.id in published_ids or version.schema_version != 2 for version in candidates):
        raise NeedsReview("invalid_open_amendment")
    return candidates


def _unique_json_object(
    session: Session, s3: S3Service, report: Report, version: ReportVersion, prefix: str,
) -> StorageObject:
    storage = session.get(StorageObject, version.json_storage_id) if version.json_storage_id else None
    if storage is None:
        raise NeedsReview("missing_json_reference")
    suffix = storage.object_key.removeprefix(f"{prefix}/")
    migrated_key = storage.object_key.startswith(f"{prefix}/") and re.fullmatch(
        r"section-titles-[0-9a-f]{64}\.json", suffix,
    )
    if (
        storage.bucket != s3.bucket or storage.provider != "aws"
        or storage.tenant_id not in (None, report.tenant_id) or storage.sha256_hex is not None
        or storage.content_type not in (None, "application/json")
        or (storage.object_key != f"{prefix}/report.json" and not migrated_key)
    ):
        raise NeedsReview("unexpected_json_storage")
    other_row = session.exec(select(StorageObject.id).where(
        StorageObject.bucket == storage.bucket, StorageObject.object_key == storage.object_key,
        StorageObject.id != storage.id,
    ).limit(1)).first()
    other_reference = session.exec(select(ReportVersion.id).where(or_(
        (ReportVersion.id != version.id) & (ReportVersion.json_storage_id == storage.id),
        ReportVersion.pdf_storage_id == storage.id,
        ReportVersion.html_storage_id == storage.id,
    )).limit(1)).first()
    foreign_reference = session.execute(text("""
        SELECT 1 FROM sample_image WHERE storage_id = :id
        UNION ALL SELECT 1 FROM sample_image_rendition WHERE storage_id = :id
        UNION ALL SELECT 1 FROM app_user WHERE signature_storage_id = :id
        UNION ALL SELECT 1 FROM tenant WHERE logo_storage_id = :id
        LIMIT 1
    """), {"id": storage.id}).first()
    if other_row or other_reference or foreign_reference:
        raise NeedsReview("shared_json_storage")
    return storage


def _unique_fields(pairs: list[tuple]) -> dict:
    fields = dict(pairs)
    if len(pairs) != len(fields):
        raise ValueError("duplicate JSON fields")
    return fields


def _plan_change(session: Session, s3: S3Service, report: Report, version: ReportVersion) -> JsonChange | None:
    prefix = f"reports/{report.tenant_id}/{report.branch_id}/{report.id}/versions/{version.version_no}"
    storage = _unique_json_object(session, s3, report, version, prefix)
    info = s3.head_object(storage.object_key)
    if info is None:
        raise NeedsReview("missing_json_object")
    before = s3.download_bytes(storage.object_key)
    if (
        info.size_bytes != len(before)
        or storage.size_bytes not in (None, len(before))
        or storage.etag not in (None, info.etag)
        or storage.version_id not in (None, info.version_id)
    ):
        raise NeedsReview("json_metadata_mismatch")
    try:
        body = json.loads(before.decode("utf-8"), object_pairs_hook=_unique_fields)
        if not isinstance(body, dict) or not isinstance(body.get("sections"), dict):
            raise ValueError("invalid report body")
        if version.schema_version == 2:
            if body.get("schema_version") != 2:
                raise ValueError("invalid schema marker")
            snapshot = ReportRenderingSnapshotV2.model_validate(body.get("rendering_snapshot"))
            if not isinstance(snapshot.template.get("sections"), dict):
                raise ValueError("invalid snapshot sections")
        elif (
            version.schema_version not in (None, 1)
            or "rendering_snapshot" in body
            or body.get("schema_version") not in (None, 1)
        ):
            raise ValueError("unknown report format")
        after = rename_report_description_labels(body)
        if after is body:
            return None
        if version.schema_version == 2:
            ReportRenderingSnapshotV2.model_validate(after["rendering_snapshot"])
        encoded = json.dumps(after, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except ValueError:
        raise NeedsReview("invalid_report_json") from None
    if rename_report_description_labels(json.loads(before, parse_float=Decimal)) != json.loads(encoded, parse_float=Decimal):
        raise NeedsReview("json_precision_would_change")
    if version.html_storage_id is not None:
        raise NeedsReview("existing_html_artifact")
    if version.pdf_storage_id is not None:
        pdf = session.get(StorageObject, version.pdf_storage_id)
        if (
            version.schema_version != 2 or pdf is None or pdf.tenant_id != report.tenant_id
            or pdf.bucket != s3.bucket or not version.pdf_sha256 or pdf.sha256_hex != version.pdf_sha256
            or not pdf.object_key.startswith(
                f"reports/{report.tenant_id}/{report.id}/versions/{version.version_no}/official/"
            )
        ):
            raise NeedsReview("unverified_existing_pdf")
    return JsonChange(version, storage, before, encoded, f"{prefix}/section-titles-{sha256(encoded).hexdigest()}.json")


def _apply_json_change(session: Session, s3: S3Service, report: Report, change: JsonChange, actor_id: UUID) -> None:
    storage, version = change.storage, change.version
    if session.exec(select(StorageObject.id).where(
        StorageObject.bucket == storage.bucket, StorageObject.object_key == change.key,
    ).limit(1)).first() is not None:
        raise NeedsReview("replacement_already_referenced")

    original_key = storage.object_key
    old_billable_size = storage.size_bytes or 0
    backup = StorageObject(
        tenant_id=report.tenant_id, provider=storage.provider, region=storage.region, bucket=storage.bucket,
        object_key=original_key, version_id=storage.version_id, etag=storage.etag,
        content_type="application/json", size_bytes=len(change.before), created_by=storage.created_by,
    )
    existing = s3.head_object(change.key)
    if existing is None:
        s3.upload_bytes(change.after, key=change.key, content_type="application/json")
        existing = s3.head_object(change.key)
    if (
        existing is None or existing.size_bytes != len(change.after)
        or existing.content_type != "application/json"
        or s3.download_bytes(change.key) != change.after
    ):
        raise NeedsReview("replacement_integrity_failure")
    session.add(backup)
    storage.object_key = change.key
    storage.size_bytes = len(change.after)
    storage.etag = existing.etag
    storage.version_id = existing.version_id
    storage.content_type = "application/json"
    storage.tenant_id = report.tenant_id

    old_pdf_id = version.pdf_storage_id
    if old_pdf_id is not None or version.pdf_generation_status is not None:
        version.pdf_storage_id = None
        version.pdf_generation_status = None
        version.pdf_generation_started_at = None
        version.pdf_generated_at = None
        version.pdf_sha256 = None
        version.pdf_size_bytes = None
        version.pdf_page_count = None
        version.pdf_generator_version = None
        version.pdf_error_code = None
        version.pdf_error_message = None
    record_storage_delta_with_thresholds(
        session, report.tenant_id, len(change.after) - old_billable_size,
        source="report_json", resource_type="report_json", actor_id=actor_id,
    )
    session.add(AuditLog(
        tenant_id=report.tenant_id, branch_id=report.branch_id, actor_user_id=actor_id,
        action="REPORT_VERSION.BACKFILL_DESCRIPTION_LABELS", entity_type="report_version", entity_id=version.id,
        old_values={
            "backup_storage_id": str(backup.id), "json_sha256": sha256(change.before).hexdigest(),
            "pdf_storage_id": str(old_pdf_id) if old_pdf_id else None,
        },
        new_values={"json_storage_id": str(storage.id), "json_sha256": sha256(change.after).hexdigest()},
    ))


def update_report(
    session: Session, s3: S3Service, tenant_id: UUID, report_id: UUID, actor_id: UUID | None,
) -> tuple[str, int]:
    """Return the outcome and number of changed versions. The caller owns commit/rollback."""
    report = session.exec(select(Report).where(Report.id == report_id, Report.tenant_id == tenant_id)).first()
    if report is None or report.status not in UNSIGNED_STATUSES:
        return "protected", 0
    if actor_id is not None:
        try:
            lock_report_order(session, report)
        except HTTPException as exc:
            if exc.status_code == 404:
                return "protected", 0
            raise
        if report.status not in UNSIGNED_STATUSES:
            return "protected", 0
        assert_not_in_maintenance(session, tenant_id)
    order = session.get(Order, report.order_id)
    if order is None:
        return "protected", 0
    if order.tenant_id != tenant_id or order.branch_id != report.branch_id or order.report_id != report.id:
        raise NeedsReview("unexpected_order_link")

    query = select(ReportVersion).where(ReportVersion.report_id == report.id).order_by(ReportVersion.version_no)
    if actor_id is not None:
        query = query.with_for_update().execution_options(populate_existing=True)
    versions = list(session.exec(query).all())
    eligible = _eligible_versions(session, report, versions)
    if order.status == OrderStatus.CANCELLED or (
        eligible[0].amendment_id is None and order.status in (OrderStatus.CLOSED, OrderStatus.RELEASED)
    ):
        return "protected", 0
    if any(
        version.publish_started_at or version.publish_started_by or version.pdf_generation_status == "GENERATING"
        for version in eligible
    ):
        return "busy", 0
    changes = [change for version in eligible if (change := _plan_change(session, s3, report, version))]
    # The report-level compatibility copy belongs to the original cycle. It
    # may supply legacy/editor headings but must stay unchanged during an amendment.
    before = report.template
    if eligible[0].amendment_id is None and before is not None and not isinstance(before, dict):
        raise NeedsReview("invalid_report_template")
    after = rename_description_labels(before) if before is not None and eligible[0].amendment_id is None else before
    if after is not before and any(
        version.schema_version != 2 and (version.pdf_storage_id or version.html_storage_id)
        for version in eligible
    ):
        raise NeedsReview("legacy_artifact")
    if actor_id is not None:
        for change in changes:
            _apply_json_change(session, s3, report, change, actor_id)
        if after is not before:
            report.template = after
            session.add(AuditLog(
                tenant_id=tenant_id, branch_id=report.branch_id, actor_user_id=actor_id,
                action="REPORT.BACKFILL_DESCRIPTION_LABELS", entity_type="report", entity_id=report.id,
                old_values={"template_hash": hash_clinical_template_block(before)},
                new_values={"template_hash": hash_clinical_template_block(after)},
            ))
    return ("changed" if changes or after is not before else "unchanged"), len(changes)
