"""Céluma 1.4 (#12): the `sample_codes` base field across approval, signing and amendment.

The value is resolved on authoring saves only. Once the report is signed, the issued artifact
(JSON body, official PDF, hash) keeps the codes it was issued with; a code changed afterwards never
rewrites it, and an amendment (a new emission) reads the codes current when it is saved.
"""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from app.models.enums import ReportStatus, ReviewStatus, SampleState
from app.models.laboratory import Sample
from app.models.report import ReportVersion
from app.models.storage import StorageObject

from .conftest import FakeS3Service, make_pdf_bytes
from .factories import (
    assign_reviewer,
    assign_to_order,
    create_branch,
    create_order,
    create_report,
    create_sample,
    create_tenant,
    create_user,
)
from .test_report_amendments import CONFIRM, _approve, _fingerprint, _open, _save, _sign, _submit, _version

CODES_FIELD = {"is_visible": True, "label": "Códigos de muestra", "value": ""}


@pytest.fixture
def coded(session: Session, client, stub_pdf_render):
    """A report whose frozen template declares `sample_codes`, approved and ready to sign."""
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch, order_code="ORD-SC-1")
    for code in ("SC-2", "SC-10", "SC-1"):
        create_sample(session, tenant, branch, order, sample_code=code, state=SampleState.READY)
    author = create_user(session, tenant, email="author@sc.example", roles=("pathologist",))
    reviewer = create_user(session, tenant, email="rev@sc.example", roles=("reviewer",))
    assignee = create_user(session, tenant, email="tech@sc.example", roles=("lab_tech",))
    report, v1 = create_report(
        session, tenant, branch, order, status=ReportStatus.DRAFT,
        pdf_generation_status=None, created_by=author, authored_by=author,
    )
    assign_reviewer(session, order, reviewer, report=report, status=ReviewStatus.PENDING)
    assign_to_order(session, tenant, order, assignee)
    # The stored body declares the field in its frozen template, as a report built from a template
    # that opted in would.
    storage = session.get(StorageObject, v1.json_storage_id)
    body = json.loads(FakeS3Service.store[storage.object_key])
    body["rendering_snapshot"]["template"]["base"]["sample_codes"] = dict(CODES_FIELD)
    body["rendering_snapshot"]["template"]["base_order"].append("sample_codes")
    FakeS3Service.store[storage.object_key] = json.dumps(body).encode("utf-8")
    lab = {
        "tenant": tenant, "branch": branch, "order": order, "report": report, "v1": v1,
        "author": author, "reviewer": reviewer, "assignee": assignee,
    }
    assert _save(client, lab, note="").status_code == 200  # first authoring save resolves the codes
    session.expire_all()
    return lab


def _codes(session, report, version_no):
    body = json.loads(FakeS3Service.store[session.get(StorageObject, _version(session, report, version_no).json_storage_id).object_key])
    return body["base"]["sample_codes"]["value"]


def _publish(client, session, stub_pdf_render, lab):
    assert _submit(client, lab).status_code == 200
    assert _approve(client, lab).status_code == 200
    stub_pdf_render.succeed(make_pdf_bytes(1))
    resp = _sign(client, lab)
    assert resp.status_code == 200, resp.text
    session.expire_all()


class TestSampleCodesAcrossTheLifecycle:
    def test_first_save_resolves_codes_in_natural_order(self, session, coded):
        # The factory body is version 1; the authoring save created version 2.
        assert _codes(session, coded["report"], 2) == "SC-1, SC-2, SC-10"

    def test_signed_artifact_keeps_the_codes_it_was_issued_with(self, client, session, stub_pdf_render, coded):
        _publish(client, session, stub_pdf_render, coded)
        signed = _version(session, coded["report"], 2)
        assert signed.signed_at is not None
        before = _fingerprint(session, signed)
        assert _codes(session, coded["report"], 2) == "SC-1, SC-2, SC-10"

        # A later change of the stored sample code (the correction service itself refuses once the
        # order carries signing evidence; here the row is changed directly to prove that even then
        # nothing the report issued is rewritten).
        sample = session.exec(select(Sample).where(Sample.sample_code == "SC-1")).one()
        sample.sample_code = "SC-1-CORREGIDO"
        session.add(sample)
        session.commit()

        assert _fingerprint(session, _version(session, coded["report"], 2)) == before
        assert _codes(session, coded["report"], 2) == "SC-1, SC-2, SC-10"

    def test_amendment_emission_reads_the_current_codes_and_leaves_the_original_alone(
        self, client, session, stub_pdf_render, coded,
    ):
        _publish(client, session, stub_pdf_render, coded)
        signed = _version(session, coded["report"], 2)
        original = _fingerprint(session, signed)

        sample = session.exec(select(Sample).where(Sample.sample_code == "SC-1")).one()
        sample.sample_code = "SC-1-CORREGIDO"
        session.add(sample)
        session.commit()

        opened = _open(client, coded)
        assert opened.status_code == 200, opened.text
        session.expire_all()
        # Opening the amendment copies the emission and re-resolves the server-owned fields.
        draft = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == coded["report"].id).order_by(ReportVersion.version_no.desc())
        ).first()
        assert draft is not None and draft.version_no > signed.version_no
        assert _codes(session, coded["report"], draft.version_no) == "SC-1-CORREGIDO, SC-2, SC-10"

        saved = _save(client, coded)
        assert saved.status_code == 200, saved.text
        session.expire_all()
        newest = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == coded["report"].id).order_by(ReportVersion.version_no.desc())
        ).first()
        assert _codes(session, coded["report"], newest.version_no) == "SC-1-CORREGIDO, SC-2, SC-10"
        # The signed original: same bytes, same hash, same codes.
        assert _fingerprint(session, _version(session, coded["report"], signed.version_no)) == original
        assert _codes(session, coded["report"], signed.version_no) == "SC-1, SC-2, SC-10"
