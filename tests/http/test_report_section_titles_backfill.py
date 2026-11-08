from dataclasses import replace
from datetime import datetime
import json

import pytest
from sqlmodel import select

from app.models.audit import AuditLog
from app.models.enums import ReportStatus, ReviewStatus
from app.models.report import ReportTemplate, ReportVersion
from app.models.report_amendment import ReportAmendment
from app.models.report_template_version import ReportTemplateVersion
from app.models.storage import StorageObject
from app.services.laboratory_cleanup_scope import resolve_objects
from app.services.report_amendments import externally_delivered_emission
from app.services.report_section_titles_backfill import NeedsReview, update_report, update_template

from .conftest import FakeS3Service
from .factories import (
    TEST_USER_PASSWORD,
    assign_reviewer,
    auth_headers,
    create_branch,
    create_letterhead,
    create_letterhead_version,
    create_order,
    create_report,
    create_tenant,
    create_user,
    valid_presentation,
    valid_rendering_snapshot,
)


OLD = {
    "section_macroscopic": {"label": "Macroscópica", "is_visible": True, "type": "richtext"},
    "section_microscopic": {"label": "Microscópica", "is_visible": False, "type": "richtext"},
    "custom": {"label": "Macroscópica", "is_visible": True, "type": "richtext"},
}
NEW_LABELS = {
    "section_macroscopic": "Descripción macroscópica",
    "section_microscopic": "Descripción microscópica",
    "custom": "Macroscópica",
}


class BackfillStorage(FakeS3Service):
    def head_object(self, key):
        info = super().head_object(key)
        return replace(info, content_type="application/json") if info else None


def _version_body(session, report, version, *, amendment=None):
    if version.json_storage_id:
        storage = session.get(StorageObject, version.json_storage_id)
    else:
        storage = StorageObject(
            provider="aws", bucket=BackfillStorage().bucket, region="mx-test-1", tenant_id=report.tenant_id,
            object_key=f"reports/{report.tenant_id}/{report.branch_id}/{report.id}/versions/{version.version_no}/report.json",
        )
        version.json_storage_id = storage.id
    body = {
        "schema_version": 2,
        "base": {"diagnosis": {"value": "Hallazgo sintético"}},
        "sections": {key: {**value, "content": "<p>Texto Macroscópica, sin cambio.</p>"} for key, value in OLD.items()},
        "section_order": ["custom", "section_macroscopic", "section_microscopic"],
        "rendering_snapshot": valid_rendering_snapshot(template={"sections": OLD}),
    }
    if amendment is not None:
        body["amendment"] = amendment
    raw = json.dumps(body, ensure_ascii=False).encode()
    BackfillStorage().upload_bytes(raw, storage.object_key, content_type="application/json")
    storage.content_type = "application/json"
    storage.size_bytes = len(raw)
    storage.etag = "fake-etag"
    session.add(storage)
    session.flush()
    session.add(version)
    session.flush()
    return storage, body


def _lab(session, status):
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    actor = create_user(session, tenant, email="operador@synthetic.example", roles=("admin",))
    report, version = create_report(session, tenant, branch, order, status=status)
    report.template = {"sections": OLD}
    _version_body(session, report, version)
    session.commit()
    return tenant, order, actor, report, version


def _assert_only_labels_changed(before, after):
    assert {key: field["label"] for key, field in after["sections"].items()} == NEW_LABELS
    snapshot = after["rendering_snapshot"]["template"]["sections"]
    assert {key: field["label"] for key, field in snapshot.items()} == NEW_LABELS
    for key in OLD:
        after["sections"][key]["label"] = OLD[key]["label"]
        snapshot[key]["label"] = OLD[key]["label"]
    assert before == after


def test_approved_backfill_is_audited_atomic_and_idempotent(session):
    tenant, order, actor, report, older = _lab(session, ReportStatus.APPROVED)
    reviewer = create_user(session, tenant, email="reviewer@synthetic.example", roles=("reviewer",))
    review = assign_reviewer(session, order, reviewer, report=report, status=ReviewStatus.APPROVED)
    older.is_current = False
    current = ReportVersion(report_id=report.id, version_no=2, is_current=True, schema_version=2)
    current_storage, original = _version_body(session, report, current)
    pdf = StorageObject(
        tenant_id=tenant.id, provider="aws", region="mx-test-1", bucket=BackfillStorage().bucket,
        object_key=f"reports/{tenant.id}/{report.id}/versions/2/official/synthetic.pdf",
        content_type="application/pdf", sha256_hex="0" * 64, size_bytes=9,
    )
    session.add(pdf)
    session.flush()
    BackfillStorage().upload_bytes(b"synthetic", pdf.object_key, content_type="application/pdf")
    current.pdf_storage_id = pdf.id
    current.pdf_generation_status = "READY"
    current.pdf_sha256 = pdf.sha256_hex
    current.pdf_size_bytes = 9
    current.pdf_page_count = 1
    session.commit()
    ids = (tenant.id, report.id, actor.id)
    original_key = current_storage.object_key
    original_objects = dict(BackfillStorage.store)

    assert update_report(session, BackfillStorage(), *ids[:2], None) == ("changed", 2)
    assert BackfillStorage.store == original_objects
    session.rollback()
    assert update_report(session, BackfillStorage(), *ids) == ("changed", 2)
    session.rollback()  # S3 succeeded but PostgreSQL failed before commit
    assert current_storage.object_key == original_key
    assert current.pdf_storage_id == pdf.id
    staged = dict(BackfillStorage.store)

    assert update_report(session, BackfillStorage(), *ids) == ("changed", 2)
    session.commit()
    assert BackfillStorage.store == staged  # retries reuse the verified staged bytes
    assert current.json_storage_id == current_storage.id
    assert current.pdf_storage_id is None and current.pdf_generation_status is None
    assert BackfillStorage.store[pdf.object_key] == original_objects[pdf.object_key]
    assert report.status == ReportStatus.APPROVED and review.status == ReviewStatus.APPROVED
    assert older.is_current is False and current.is_current is True
    _assert_only_labels_changed(original, json.loads(BackfillStorage.store[current_storage.object_key]))
    audit = session.exec(select(AuditLog).where(AuditLog.action == "REPORT_VERSION.BACKFILL_DESCRIPTION_LABELS")).all()
    assert len(audit) == 2
    cleanup = resolve_objects(session, str(tenant.id))
    assert sum(ref.category == "report_json_backup" for ref in cleanup.delete) == 2
    assert update_report(session, BackfillStorage(), *ids) == ("unchanged", 0)
    session.commit()
    assert BackfillStorage.store == staged


def test_open_amendment_keeps_the_delivered_emission_and_shared_template(session):
    tenant, _, actor, report, delivered = _lab(session, ReportStatus.IN_REVIEW)
    report.published_at = delivered.signed_at = datetime.utcnow()
    delivered.signed_by = actor.id
    delivered.is_current = False
    cycle = ReportAmendment(
        tenant_id=tenant.id, report_id=report.id, source_version_id=delivered.id,
        amendment_no=1, category="CORRECTION", reason="Ejemplo", requested_by=actor.id,
    )
    session.add(cycle)
    session.flush()
    current = ReportVersion(
        report_id=report.id, version_no=2, is_current=True, schema_version=2, amendment_id=cycle.id,
    )
    current_storage, original = _version_body(session, report, current, amendment={"change_note": "Se conserva."})
    session.commit()
    original_storage = session.get(StorageObject, delivered.json_storage_id)
    original_key = original_storage.object_key
    original_bytes = BackfillStorage.store[original_key]

    assert update_report(session, BackfillStorage(), tenant.id, report.id, actor.id) == ("changed", 1)
    session.commit()
    assert externally_delivered_emission(session, report).id == delivered.id
    assert original_storage.object_key == original_key
    assert BackfillStorage.store[original_key] == original_bytes
    assert report.template == {"sections": OLD}
    _assert_only_labels_changed(original, json.loads(BackfillStorage.store[current_storage.object_key]))


def test_new_amendment_gets_new_labels_without_rewriting_its_source(session, client):
    _, _, actor, report, signed = _lab(session, ReportStatus.PUBLISHED)
    report.published_at = signed.signed_at = datetime.utcnow()
    signed.signed_by = actor.id
    original = session.get(StorageObject, signed.json_storage_id)
    source_bytes = BackfillStorage.store[original.object_key]
    session.commit()

    response = client.post(f"/api/v1/reports/{report.id}/amendments", headers=auth_headers(actor), json={
        "category": "CORRECTION", "reason": "Ejemplo sintético", "password": TEST_USER_PASSWORD,
    })
    assert response.status_code == 200, response.text
    current = session.exec(select(ReportVersion).where(
        ReportVersion.report_id == report.id, ReportVersion.is_current.is_(True),
    )).one()
    draft = session.get(StorageObject, current.json_storage_id)
    body = json.loads(BackfillStorage.store[draft.object_key])
    assert {key: field["label"] for key, field in body["sections"].items()} == NEW_LABELS
    assert {key: field["label"] for key, field in body["rendering_snapshot"]["template"]["sections"].items()} == NEW_LABELS
    assert BackfillStorage.store[original.object_key] == source_bytes


@pytest.mark.parametrize("case", ["published", "signed", "publishing", "foreign", "shared", "unknown_schema"])
def test_ineligible_and_ambiguous_reports_leave_storage_untouched(session, case):
    tenant, _, actor, report, version = _lab(session, ReportStatus.DRAFT)
    if case == "published":
        report.status = ReportStatus.PUBLISHED
    elif case == "signed":
        version.signed_by = actor.id  # partial signature evidence still protects it
    elif case == "publishing":
        version.publish_started_at = datetime.utcnow()
    elif case == "foreign":
        session.get(StorageObject, version.json_storage_id).tenant_id = create_tenant(session).id
    elif case == "unknown_schema":
        storage = session.get(StorageObject, version.json_storage_id)
        body = json.loads(BackfillStorage.store[storage.object_key])
        del body["rendering_snapshot"]
        body["schema_version"] = 3
        version.schema_version = None
        raw = json.dumps(body).encode()
        BackfillStorage.store[storage.object_key] = raw
        storage.size_bytes = len(raw)
    else:
        session.add(ReportVersion(report_id=report.id, version_no=2, json_storage_id=version.json_storage_id))
    session.commit()
    before = dict(BackfillStorage.store)
    if case in {"signed", "foreign", "shared", "unknown_schema"}:
        with pytest.raises(NeedsReview):
            update_report(session, BackfillStorage(), tenant.id, report.id, actor.id)
    else:
        expected = "protected" if case == "published" else "busy"
        assert update_report(session, BackfillStorage(), tenant.id, report.id, actor.id) == (expected, 0)
    session.rollback()
    assert BackfillStorage.store == before


def test_backfill_inactive_template_adds_revision_without_rewriting_history(session):
    tenant, _, actor, _, _ = _lab(session, ReportStatus.DRAFT)
    letterhead = create_letterhead(session, tenant)
    letterhead.is_default = True
    create_letterhead_version(session, tenant, letterhead, status="ACTIVE", configuration=valid_presentation())
    template = ReportTemplate(tenant_id=tenant.id, name="Plantilla histórica", is_active=False, template_json={"sections": OLD})
    session.add(template)
    session.flush()
    historical = ReportTemplateVersion(
        tenant_id=tenant.id, report_template_id=template.id, version_number=1, status="ACTIVE",
        configuration=valid_rendering_snapshot(template={"sections": OLD}),
    )
    session.add(historical)
    session.commit()

    assert update_template(session, template, None)
    assert template.template_json == {"sections": OLD}
    assert update_template(session, template, actor.id)
    session.commit()
    versions = session.exec(select(ReportTemplateVersion).where(
        ReportTemplateVersion.report_template_id == template.id,
    ).order_by(ReportTemplateVersion.version_number)).all()
    assert len(versions) == 2
    assert historical.configuration["template"] == {"sections": OLD}
    assert versions[1].status == "ACTIVE"
    assert versions[1].configuration["template"] == template.template_json
    assert {key: field["label"] for key, field in template.template_json["sections"].items()} == NEW_LABELS
    assert not update_template(session, template, actor.id)
