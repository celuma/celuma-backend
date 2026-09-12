"""Block C reproduction — CEL-131-05, recorded BEFORE the fix.

Reports V2 enabled, template configured with its clinical structure, default
letterhead with an ACTIVE version, study type pointing at the template — and
NO `ReportTemplateVersion` row. Everything a V2 report needs exists; only the
obsolete administrative artefact is missing.
"""
import pytest

from .factories import (
    auth_headers,
    create_branch,
    create_letterhead,
    create_letterhead_version,
    create_order,
    create_tenant,
    create_user,
    valid_presentation,
)

LIVE_TEMPLATE_JSON = {
    "base": {"diagnosis": {"label": "Diagnóstico", "type": "text"}},
    "sections": {},
    "base_order": ["diagnosis"],
    "section_order": [],
}


def _tenant_without_any_template_version(session, *, name="Repro Tenant"):
    from app.models.report import ReportTemplate
    from app.models.study_type import StudyType

    tenant = create_tenant(session, name=name, reports_v2_enabled=True)
    branch = create_branch(session, tenant)
    template = ReportTemplate(
        tenant_id=tenant.id, name="Clínica", template_json=LIVE_TEMPLATE_JSON
    )
    session.add(template)
    session.flush()
    study_type = StudyType(
        tenant_id=tenant.id,
        code="BIO",
        name="Biopsia",
        default_report_template_id=template.id,
    )
    session.add(study_type)
    letterhead = create_letterhead(session, tenant, name="Predeterminado")
    letterhead.is_default = True
    session.add(letterhead)
    lh_version = create_letterhead_version(
        session, tenant, letterhead, status="ACTIVE", configuration=valid_presentation()
    )
    session.commit()
    return {
        "tenant": tenant,
        "branch": branch,
        "template": template,
        "study_type": study_type,
        "letterhead_version": lh_version,
    }


def test_no_template_version_row_exists(session):
    from app.models.report_template_version import ReportTemplateVersion
    from sqlmodel import select

    env = _tenant_without_any_template_version(session)
    rows = session.exec(
        select(ReportTemplateVersion).where(
            ReportTemplateVersion.tenant_id == env["tenant"].id
        )
    ).all()
    assert rows == []


def test_REPRO_report_defaults_blocks_the_editor(client, session):
    """The production symptom: the V2 editor refuses to open."""
    env = _tenant_without_any_template_version(session)
    user = create_user(
        session, env["tenant"], email="path@repro.example", roles=("pathologist",)
    )
    resp = client.get(
        f"/api/v1/study-types/{env['study_type'].id}/report-defaults",
        headers=auth_headers(user),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    print("REPRO report-defaults:", body["v2_blocked_reason"], body["active_template_version_id"],
          "letterhead:", body["letterhead_version_id"])
    assert body["v2_blocked_reason"] == "NO_ACTIVE_TEMPLATE_VERSION"
    assert body["active_template_version_id"] is None
    # The letterhead is NOT the problem: it resolved fine.
    assert body["letterhead_version_id"] == str(env["letterhead_version"].id)
    assert body["letterhead_presentation"] is not None


def test_REPRO_no_way_to_create_a_v2_report(client, session):
    """There is no accepted input that produces a V2 report here: the only
    V2 selector is `template_version_id`, and no version exists."""
    env = _tenant_without_any_template_version(session)
    user = create_user(
        session,
        env["tenant"],
        email="path2@repro.example",
        roles=("pathologist",),
    )
    order = create_order(session, env["tenant"], env["branch"])

    resp = client.post(
        "/api/v1/reports/",
        headers=auth_headers(user),
        json={
            "tenant_id": str(env["tenant"].id),
            "branch_id": str(env["branch"].id),
            "order_id": str(order.id),
            "title": "Repro",
            "template": LIVE_TEMPLATE_JSON,
            "report": {"base": {}, "sections": {}},
            # the V2 selector the editor would have sent; nothing to send
            "template_id": str(env["template"].id),
        },
    )
    print("REPRO create status:", resp.status_code, resp.text[:300])
    assert resp.status_code == 200, resp.text
    detail = client.get(
        f"/api/v1/reports/{resp.json()['id']}", headers=auth_headers(user)
    ).json()
    print("REPRO created schema_version:", detail["schema_version"])
    # Silently LEGACY: no snapshot, no V2 metadata.
    assert detail["schema_version"] is None
    assert detail["template_version_id"] is None
    assert (detail["report"] or {}).get("rendering_snapshot") is None
