"""Céluma 1.4 (#12), independent review R3: a real, authorized sample-code correction made after the last
save must never reach review/approval with the old code.

Boundary: submission (under the order lock). A stale `sample_codes` is refused with 409
`report_sample_codes_stale` and nothing changes; saving again (the server resolves the current codes,
whatever a stale client submits) lets the submission through, and the reviewed/approved/signed content then
carries the corrected code. Once review evidence exists the correction itself is already refused, so the
value cannot go stale afterwards.
"""
from sqlmodel import select

from app.models.enums import ReportStatus
from app.models.laboratory import Sample
from app.models.report import Report, ReportVersion

from .factories import auth_headers, create_user
from .test_report_amendments import _approve, _save, _submit
from .test_report_sample_codes_lifecycle import _codes, coded  # noqa: F401  (fixture)

CORRECTION = {"sample_code": "SC-1-CORREGIDO", "reason": "Corrección sintética antes de revisión"}


def _correct(client, session, lab):
    sample = session.exec(select(Sample).where(Sample.sample_code == "SC-1")).one()
    admin = create_user(session, lab["tenant"], email="correction-admin@review.example", roles=("admin",))
    resp = client.post(
        f"/api/v1/laboratory/samples/{sample.id}/code-correction", json=CORRECTION, headers=auth_headers(admin)
    )
    assert resp.status_code == 200, resp.text
    return admin


def test_stale_code_blocks_submission_then_a_save_lets_it_through(client, session, coded):  # noqa: F811
    report = coded["report"]
    assert _codes(session, report, 2) == "SC-1, SC-2, SC-10"
    _correct(client, session, coded)

    blocked = _submit(client, coded)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "report_sample_codes_stale"
    assert "SC-1, SC-2, SC-10" in blocked.json()["detail"]["message"]
    session.expire_all()
    assert session.get(Report, report.id).status == ReportStatus.DRAFT  # nothing moved

    # A stale client (still holding the old value) saves: the server overwrites it with the current codes.
    stale_client = {"sample_codes": {"is_visible": True, "label": "Códigos de muestra", "value": "SC-1, SC-2, SC-10"}}
    assert _save(client, coded, note="", base=stale_client).status_code == 200
    assert _submit(client, coded).status_code == 200
    assert _approve(client, coded).status_code == 200
    session.expire_all()
    version = session.exec(
        select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.is_current == True)  # noqa: E712
    ).one()
    assert _codes(session, report, version.version_no) == "SC-1-CORREGIDO, SC-2, SC-10"


def test_an_up_to_date_report_still_submits(client, session, coded):  # noqa: F811
    assert _submit(client, coded).status_code == 200


def test_correction_is_refused_once_the_report_is_under_review(client, session, coded):  # noqa: F811
    assert _submit(client, coded).status_code == 200
    sample = session.exec(select(Sample).where(Sample.sample_code == "SC-1")).one()
    admin = create_user(session, coded["tenant"], email="late-admin@review.example", roles=("admin",))
    late = client.post(
        f"/api/v1/laboratory/samples/{sample.id}/code-correction", json=CORRECTION, headers=auth_headers(admin)
    )
    assert late.status_code in (409, 422, 403), late.text
