"""HTTP integration tests for the `sample_codes` base field (Céluma 1.4, #12).

The field is server-owned like `reception_date` / `expediente`: the backend resolves the actual
`sample_code` of every sample linked to the report's order, discards whatever the client submitted,
creates the key only when the report's effective (frozen) template declares it, and never lets a
sample of another tenant in. It consumes persisted codes; it never allocates or corrects one.
"""
import uuid
from datetime import datetime

from sqlmodel import select

from app.models.report import Report, ReportTemplate
from app.models.report_letterhead import ReportLetterhead
from app.services.report_template_hash import hash_clinical_template_block

from .factories import (
    auth_headers,
    create_branch,
    create_default_letterhead,
    create_order,
    create_sample,
    create_tenant,
    create_user,
)

CODES_FIELD = {"is_visible": True, "label": "Códigos de muestra", "value": ""}
PATIENT_FIELD = {"is_visible": True, "label": "Paciente", "value": ""}


def _template_doc(base: dict) -> dict:
    return {
        "base": {k: dict(v) for k, v in base.items()},
        "sections": {},
        "base_order": list(base.keys()),
        "section_order": [],
    }


def _create_report(client, session, tenant, branch, order, user, *, declared, submitted=None):
    template_doc = _template_doc(declared)
    template = ReportTemplate(
        tenant_id=tenant.id,
        name=f"Plantilla {uuid.uuid4().hex[:6]}",
        template_json=template_doc,
        is_active=True,
    )
    session.add(template)
    session.commit()
    session.refresh(template)
    if not session.exec(
        select(ReportLetterhead).where(ReportLetterhead.tenant_id == tenant.id)
    ).first():
        create_default_letterhead(session, tenant)
    base = {k: dict(v) for k, v in (declared if submitted is None else submitted).items()}
    return client.post(
        "/api/v1/reports/",
        json={
            "tenant_id": str(tenant.id),
            "branch_id": str(branch.id),
            "order_id": str(order.id),
            "template": template_doc,
            "template_id": str(template.id),
            "template_hash": hash_clinical_template_block(template.template_json),
            "report": {
                "base": base,
                "sections": {},
                "base_order": list(base.keys()),
                "section_order": [],
            },
        },
        headers=auth_headers(user),
    )


def _base(client, user, report_id):
    resp = client.get(f"/api/v1/reports/{report_id}/full", headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()["report"]["report"]["base"]


def _new_version(client, tenant, branch, order, user, report_id, base):
    return client.post(
        f"/api/v1/reports/{report_id}/new_version",
        json={
            "tenant_id": str(tenant.id),
            "branch_id": str(branch.id),
            "order_id": str(order.id),
            "report": {"base": base, "sections": {}, "base_order": list(base), "section_order": []},
        },
        headers=auth_headers(user),
    )


def _setup(session, codes=()):
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    user = create_user(session, tenant, email="author@t1.example")
    for code in codes:
        create_sample(session, tenant, branch, order, sample_code=code)
    return tenant, branch, order, user


class TestSampleCodesResolution:
    def test_many_codes_are_listed_in_stable_natural_order(self, client, session):
        # Registered out of order on purpose; M-10 must follow M-2 (digit-aware), not precede it.
        tenant, branch, order, user = _setup(session, ["M-10", "M-2", "M-1", "B-3"])
        resp = _create_report(client, session, tenant, branch, order, user, declared={"sample_codes": CODES_FIELD})
        assert resp.status_code == 200, resp.text
        assert _base(client, user, resp.json()["id"])["sample_codes"]["value"] == "B-3, M-1, M-2, M-10"

    def test_one_code_has_no_separator(self, client, session):
        tenant, branch, order, user = _setup(session, ["ONLY-1"])
        resp = _create_report(client, session, tenant, branch, order, user, declared={"sample_codes": CODES_FIELD})
        assert _base(client, user, resp.json()["id"])["sample_codes"]["value"] == "ONLY-1"

    def test_no_samples_is_empty_not_a_placeholder(self, client, session):
        tenant, branch, order, user = _setup(session)
        resp = _create_report(client, session, tenant, branch, order, user, declared={"sample_codes": CODES_FIELD})
        assert resp.status_code == 200, resp.text
        assert _base(client, user, resp.json()["id"])["sample_codes"]["value"] == ""

    def test_client_cannot_forge_or_suppress_the_field(self, client, session):
        tenant, branch, order, user = _setup(session, ["R-1"])
        forged = {"sample_codes": {**CODES_FIELD, "value": "FORJADO-9"}, "patient": PATIENT_FIELD}
        resp = _create_report(
            client, session, tenant, branch, order, user,
            declared={"sample_codes": CODES_FIELD, "patient": PATIENT_FIELD}, submitted=forged,
        )
        assert _base(client, user, resp.json()["id"])["sample_codes"]["value"] == "R-1"

        # Omitting the key does not suppress an official field the template declares (one report
        # per order, so a second order carries this case).
        order2 = create_order(session, tenant, branch)
        create_sample(session, tenant, branch, order2, sample_code="R-2")
        omitted = _create_report(
            client, session, tenant, branch, order2, user,
            declared={"sample_codes": CODES_FIELD, "patient": PATIENT_FIELD},
            submitted={"patient": PATIENT_FIELD},
        )
        assert _base(client, user, omitted.json()["id"])["sample_codes"]["value"] == "R-2"

    def test_undeclared_field_is_never_injected(self, client, session):
        tenant, branch, order, user = _setup(session, ["R-1"])
        resp = _create_report(client, session, tenant, branch, order, user, declared={"patient": PATIENT_FIELD})
        assert "sample_codes" not in _base(client, user, resp.json()["id"])

    def test_samples_of_another_tenant_never_appear(self, client, session):
        tenant, branch, order, user = _setup(session, ["MINE-1"])
        other = create_tenant(session, name="Other Lab")
        other_branch = create_branch(session, other)
        # A foreign-tenant row pointing at this order (defensive: it must be ignored, not leaked).
        create_sample(session, other, other_branch, order, sample_code="FOREIGN-1")
        resp = _create_report(client, session, tenant, branch, order, user, declared={"sample_codes": CODES_FIELD})
        assert _base(client, user, resp.json()["id"])["sample_codes"]["value"] == "MINE-1"

    def test_removed_sample_is_not_listed(self, client, session):
        tenant, branch, order, user = _setup(session, ["KEEP-1", "GONE-1"])
        from app.models.laboratory import Sample

        gone = session.exec(select(Sample).where(Sample.sample_code == "GONE-1")).one()
        gone.removed_at = datetime(2026, 1, 1)
        session.add(gone)
        session.commit()
        resp = _create_report(client, session, tenant, branch, order, user, declared={"sample_codes": CODES_FIELD})
        assert _base(client, user, resp.json()["id"])["sample_codes"]["value"] == "KEEP-1"


class TestSampleCodesLifecycle:
    def test_authoring_save_picks_up_a_corrected_code(self, client, session):
        """An external correction while the report is still editable is read on the next save;
        the server value replaces whatever a stale client submits."""
        tenant, branch, order, user = _setup(session, ["OLD-1"])
        created = _create_report(client, session, tenant, branch, order, user, declared={"sample_codes": CODES_FIELD})
        report_id = created.json()["id"]
        assert _base(client, user, report_id)["sample_codes"]["value"] == "OLD-1"

        from app.models.laboratory import Sample

        sample = session.exec(select(Sample).where(Sample.sample_code == "OLD-1")).one()
        sample.sample_code = "NEW-1"
        session.add(sample)
        session.commit()

        stale = {"sample_codes": {**CODES_FIELD, "value": "OLD-1"}}
        resp = _new_version(client, tenant, branch, order, user, report_id, stale)
        assert resp.status_code == 200, resp.text
        assert _base(client, user, report_id)["sample_codes"]["value"] == "NEW-1"

    def test_report_row_keeps_the_declared_template_flag(self, client, session):
        """A per-field repetition flag lives in the frozen template and is not touched by the
        server-owned value refresh."""
        tenant, branch, order, user = _setup(session, ["R-1"])
        declared = {"sample_codes": {**CODES_FIELD, "repeat_on_every_page": True}}
        resp = _create_report(client, session, tenant, branch, order, user, declared=declared)
        assert resp.status_code == 200, resp.text
        report = session.get(Report, resp.json()["id"])
        assert report is not None
        base = _base(client, user, resp.json()["id"])
        assert base["sample_codes"]["value"] == "R-1"
        assert base["sample_codes"].get("repeat_on_every_page") is True
