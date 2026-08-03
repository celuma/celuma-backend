"""Quinta remediación post-Fase 2 — cambio de membrete mientras el reporte
sigue en DRAFT (Observación A).

La frontera de inmutabilidad deja de ser "el reporte ya tiene id" y pasa a
ser "el reporte ya salió de DRAFT". Estas pruebas fijan las dos mitades del
contrato:

  * lo que SÍ cambia: `ReportVersion.letterhead_version_id` y
    `rendering_snapshot.presentation`;
  * lo que NUNCA cambia: plantilla clínica, `template_version_id`, campos
    base, secciones, valores clínicos e imágenes.

Ver draft-letterhead-change-contract.md y
letterhead-freeze-at-review-contract.md.
"""
import pytest

from app.models.enums import ReportStatus
from app.models.report import Report, ReportTemplate
from app.models.report_review import ReportReview

from .factories import (
    auth_headers,
    create_branch,
    create_default_letterhead,
    create_letterhead,
    create_letterhead_version,
    create_order,
    create_tenant,
    create_user,
    valid_rendering_snapshot,
)


def _create_template(session, tenant, *, name: str = "Default"):
    template = ReportTemplate(tenant_id=tenant.id, name=name, template_json={}, is_active=True)
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def _publish_template_version(client, headers, template_id):
    resp = client.post(
        f"/api/v1/reports/templates/{template_id}/versions",
        json={"configuration": valid_rendering_snapshot()},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _content(diagnosis: str = "Benigno", images=None) -> dict:
    sections = {}
    if images is not None:
        sections["galeria"] = {"type": "images", "content": images}
    return {
        "base": {"diagnosis": {"label": "Diagnóstico", "value": diagnosis}},
        "sections": sections,
        "base_order": ["diagnosis"],
        "section_order": list(sections.keys()),
    }


def _alt_presentation(name: str = "Laboratorio Nefropatología") -> dict:
    presentation = valid_rendering_snapshot()["presentation"]
    presentation["header"] = dict(presentation["header"])
    presentation["header"]["institution_name"] = name
    presentation["style"] = {"primary_color": "#aa0044"}
    return presentation


@pytest.fixture
def v2_world(client, session):
    """Un tenant V2 con dos membretes utilizables y un reporte DRAFT ya
    persistido — el escenario exacto de la Observación A."""
    tenant = create_tenant(session, reports_v2_enabled=True)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    user = create_user(session, tenant, email="admin@t1.example")
    headers = auth_headers(user)

    template = _create_template(session, tenant)
    template_version = _publish_template_version(client, headers, template.id)

    default_lh, default_version = create_default_letterhead(session, tenant, name="Membrete general")
    other_lh = create_letterhead(session, tenant, name="Membrete nefropatología")
    other_version = create_letterhead_version(
        session, tenant, other_lh, status="ACTIVE", configuration=_alt_presentation()
    )

    resp = client.post(
        "/api/v1/reports/",
        json={
            "tenant_id": str(tenant.id),
            "branch_id": str(branch.id),
            "order_id": str(order.id),
            "report": _content(),
            "template_version_id": template_version["id"],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    report_id = resp.json()["id"]

    return {
        "tenant": tenant,
        "branch": branch,
        "order": order,
        "user": user,
        "headers": headers,
        "template_version": template_version,
        "default_version": default_version,
        "other_letterhead": other_lh,
        "other_version": other_version,
        "report_id": report_id,
    }


def _save_draft(client, w, *, content=None, letterhead_version_id=None):
    payload = {
        "tenant_id": str(w["tenant"].id),
        "branch_id": str(w["branch"].id),
        "order_id": str(w["order"].id),
        "report": content if content is not None else _content(),
    }
    if letterhead_version_id is not None:
        payload["letterhead_version_id"] = letterhead_version_id
    return client.post(
        f"/api/v1/reports/{w['report_id']}/new_version", json=payload, headers=w["headers"]
    )


def _get(client, w):
    resp = client.get(f"/api/v1/reports/{w['report_id']}", headers=w["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestDraftLetterheadChange:
    def test_new_report_resolves_default_letterhead(self, client, v2_world):
        detail = _get(client, v2_world)
        assert detail["schema_version"] == 2
        assert detail["letterhead_version_id"] == str(v2_world["default_version"].id)

    def test_persisted_draft_can_change_letterhead(self, client, v2_world):
        """El corazón de la Observación A: un DRAFT YA GUARDADO cambia de
        membrete. Antes, `_carry_forward_v2_metadata` reimponía siempre el
        membrete original y la petición se ignoraba en silencio."""
        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["letterhead_version_id"] == str(v2_world["other_version"].id)

        detail = _get(client, v2_world)
        assert detail["letterhead_version_id"] == str(v2_world["other_version"].id)
        presentation = detail["report"]["rendering_snapshot"]["presentation"]
        assert presentation["header"]["institution_name"] == "Laboratorio Nefropatología"

    def test_reopened_draft_can_change_letterhead(self, client, v2_world):
        """Reabrir (releer por /full) y luego cambiar: el mismo camino que
        recorre la usuaria en la UI."""
        full = client.get(
            f"/api/v1/reports/{v2_world['report_id']}/full", headers=v2_world["headers"]
        )
        assert full.status_code == 200, full.text
        assert full.json()["report"]["status"] == ReportStatus.DRAFT

        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        )
        assert resp.status_code == 200, resp.text
        assert _get(client, v2_world)["letterhead_version_id"] == str(
            v2_world["other_version"].id
        )

    def test_can_change_letterhead_several_times(self, client, v2_world):
        for expected in (
            v2_world["other_version"].id,
            v2_world["default_version"].id,
            v2_world["other_version"].id,
        ):
            resp = _save_draft(client, v2_world, letterhead_version_id=str(expected))
            assert resp.status_code == 200, resp.text
            assert _get(client, v2_world)["letterhead_version_id"] == str(expected)

    def test_change_preserves_clinical_content(self, client, v2_world):
        content = _content("Carcinoma ductal infiltrante")
        assert _save_draft(client, v2_world, content=content).status_code == 200

        resp = _save_draft(
            client,
            v2_world,
            content=content,
            letterhead_version_id=str(v2_world["other_version"].id),
        )
        assert resp.status_code == 200, resp.text

        detail = _get(client, v2_world)
        assert (
            detail["report"]["base"]["diagnosis"]["value"] == "Carcinoma ductal infiltrante"
        )

    def test_change_preserves_images(self, client, v2_world):
        images = [{"id": "img-1", "url": "https://example.test/a.png", "caption": "H&E 40x"}]
        content = _content("Benigno", images=images)
        assert _save_draft(client, v2_world, content=content).status_code == 200

        resp = _save_draft(
            client,
            v2_world,
            content=content,
            letterhead_version_id=str(v2_world["other_version"].id),
        )
        assert resp.status_code == 200, resp.text

        detail = _get(client, v2_world)
        assert detail["report"]["sections"]["galeria"]["content"] == images

    def test_change_replaces_only_presentation(self, client, v2_world):
        """Invariante central de §3.3: el bloque `template` del snapshot y
        `template_version_id` quedan intactos."""
        before = _get(client, v2_world)
        template_before = before["report"]["rendering_snapshot"]["template"]
        template_version_before = before["template_version_id"]

        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        )
        assert resp.status_code == 200, resp.text

        after = _get(client, v2_world)
        assert after["report"]["rendering_snapshot"]["template"] == template_before
        assert after["template_version_id"] == template_version_before
        assert (
            after["report"]["rendering_snapshot"]["presentation"]
            != before["report"]["rendering_snapshot"]["presentation"]
        )

    def test_change_resolves_both_logos(self, client, session, v2_world):
        """§3.4.5: tras el cambio, `resolved_resources` se recalcula desde el
        membrete NUEVO — header y footer."""
        from .factories import create_storage_object

        header_logo = create_storage_object(
            session, key="logos/header-neph.png", tenant=v2_world["tenant"]
        )
        footer_logo = create_storage_object(
            session, key="logos/footer-neph.png", tenant=v2_world["tenant"]
        )
        presentation = _alt_presentation()
        presentation["header"]["logo_storage_id"] = str(header_logo.id)
        presentation["footer"] = dict(presentation["footer"])
        presentation["footer"]["logo_storage_id"] = str(footer_logo.id)

        logo_lh = create_letterhead(session, v2_world["tenant"], name="Membrete con logos")
        logo_version = create_letterhead_version(
            session, v2_world["tenant"], logo_lh, status="ACTIVE", configuration=presentation
        )

        resp = _save_draft(client, v2_world, letterhead_version_id=str(logo_version.id))
        assert resp.status_code == 200, resp.text

        detail = _get(client, v2_world)
        resources = detail["resolved_resources"]
        assert resources is not None
        assert "header-neph.png" in resources["header_logo_url"]
        assert "footer-neph.png" in resources["footer_logo_url"]

    def test_content_only_save_keeps_letterhead(self, client, v2_world):
        """Regresión de C9/R: un guardado sin `letterhead_version_id` sigue
        siendo carry-forward puro."""
        assert _save_draft(client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)).status_code == 200
        assert _save_draft(client, v2_world).status_code == 200
        assert _get(client, v2_world)["letterhead_version_id"] == str(
            v2_world["other_version"].id
        )

    def test_change_is_audited(self, client, session, v2_world):
        from sqlmodel import select

        from app.models.audit import AuditLog

        assert _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        ).status_code == 200

        entries = session.exec(
            select(AuditLog).where(AuditLog.action == "REPORT.CHANGE_LETTERHEAD")
        ).all()
        assert len(entries) == 1
        assert entries[0].new_values["letterhead_version_id"] == str(
            v2_world["other_version"].id
        )


class TestDraftLetterheadValidation:
    def test_cross_tenant_letterhead_is_rejected(self, client, session, v2_world):
        other_tenant = create_tenant(session, name="Otro laboratorio", reports_v2_enabled=True)
        foreign_lh = create_letterhead(session, other_tenant, name="Ajeno")
        foreign_version = create_letterhead_version(
            session, other_tenant, foreign_lh, status="ACTIVE"
        )

        resp = _save_draft(client, v2_world, letterhead_version_id=str(foreign_version.id))
        assert resp.status_code == 404, resp.text
        assert _get(client, v2_world)["letterhead_version_id"] == str(
            v2_world["default_version"].id
        )

    def test_archived_letterhead_version_is_rejected(self, client, session, v2_world):
        archived_lh = create_letterhead(session, v2_world["tenant"], name="Archivado")
        archived_version = create_letterhead_version(
            session, v2_world["tenant"], archived_lh, status="ARCHIVED"
        )

        resp = _save_draft(client, v2_world, letterhead_version_id=str(archived_version.id))
        assert resp.status_code == 409, resp.text

    def test_inactive_letterhead_is_rejected(self, client, session, v2_world):
        inactive_lh = create_letterhead(session, v2_world["tenant"], name="Desactivado")
        inactive_version = create_letterhead_version(
            session, v2_world["tenant"], inactive_lh, status="ACTIVE"
        )
        inactive_lh.is_active = False
        session.add(inactive_lh)
        session.commit()

        resp = _save_draft(client, v2_world, letterhead_version_id=str(inactive_version.id))
        assert resp.status_code == 409, resp.text

    def test_unknown_letterhead_version_is_rejected(self, client, v2_world):
        import uuid

        resp = _save_draft(client, v2_world, letterhead_version_id=str(uuid.uuid4()))
        assert resp.status_code == 404, resp.text


class TestLetterheadFreezeAtReview:
    def _submit(self, client, session, v2_world):
        review = ReportReview(
            tenant_id=v2_world["tenant"].id,
            branch_id=v2_world["branch"].id,
            order_id=v2_world["order"].id,
            reviewer_user_id=v2_world["user"].id,
        )
        session.add(review)
        session.commit()
        resp = client.post(
            f"/api/v1/reports/{v2_world['report_id']}/submit",
            json={},
            headers=v2_world["headers"],
        )
        assert resp.status_code == 200, resp.text

    def _force_status(self, session, v2_world, status):
        report = session.get(Report, v2_world["report_id"])
        report.status = status
        session.add(report)
        session.commit()

    def test_in_review_blocks_letterhead_change(self, client, session, v2_world):
        self._submit(client, session, v2_world)
        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        )
        assert resp.status_code == 409, resp.text
        assert "revisión" in resp.json()["detail"]
        assert _get(client, v2_world)["letterhead_version_id"] == str(
            v2_world["default_version"].id
        )

    def test_approved_blocks_letterhead_change(self, client, session, v2_world):
        self._force_status(session, v2_world, ReportStatus.APPROVED)
        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        )
        assert resp.status_code == 409, resp.text

    def test_published_blocks_letterhead_change(self, client, session, v2_world):
        self._force_status(session, v2_world, ReportStatus.PUBLISHED)
        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        )
        # PUBLISHED ya estaba protegido por _IMMUTABLE_REPORT_STATUSES (B9);
        # lo que importa es que NO sea un 200 ni un 500.
        assert resp.status_code == 409, resp.text

    def test_non_draft_echoing_same_letterhead_is_not_an_error(self, client, session, v2_world):
        """Un guardado de contenido en IN_REVIEW que reenvía el MISMO
        membrete no es un cambio y no debe rechazarse — si no, la UI de solo
        lectura no podría reenviar su propio envelope."""
        self._submit(client, session, v2_world)
        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["default_version"].id)
        )
        assert resp.status_code == 200, resp.text

    def test_returned_to_draft_can_change_letterhead_again(self, client, session, v2_world):
        """§3.6: `request-changes` devuelve el reporte a DRAFT sobre la MISMA
        versión editable (no crea una nueva), así que el membrete vuelve a
        ser modificable. Decisión documentada en
        letterhead-freeze-at-review-contract.md."""
        self._submit(client, session, v2_world)
        resp = client.post(
            f"/api/v1/reports/{v2_world['report_id']}/request-changes",
            json={"comment": "Ajusta el diagnóstico"},
            headers=v2_world["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == ReportStatus.DRAFT

        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        )
        assert resp.status_code == 200, resp.text
        assert _get(client, v2_world)["letterhead_version_id"] == str(
            v2_world["other_version"].id
        )

    def test_submit_succeeds_with_valid_letterhead(self, client, session, v2_world):
        assert _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        ).status_code == 200
        self._submit(client, session, v2_world)
        assert _get(client, v2_world)["status"] == ReportStatus.IN_REVIEW


class TestLegacyUnaffected:
    def test_legacy_report_ignores_letterhead_version_id(self, client, session):
        """§14: la rama Legacy no cambia. Un reporte sin snapshot V2 no tiene
        `presentation` que sustituir; la petición se ignora sin error."""
        tenant = create_tenant(session)  # reports_v2_enabled=False
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="legacy@t1.example")
        headers = auth_headers(user)
        letterhead = create_letterhead(session, tenant, name="Irrelevante")
        version = create_letterhead_version(session, tenant, letterhead, status="ACTIVE")

        created = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _content(),
            },
            headers=headers,
        )
        assert created.status_code == 200, created.text
        report_id = created.json()["id"]

        resp = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _content("Actualizado"),
                "letterhead_version_id": str(version.id),
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        detail = client.get(f"/api/v1/reports/{report_id}", headers=headers).json()
        assert detail["schema_version"] is None
        assert detail["letterhead_version_id"] is None
        assert detail["report"]["base"]["diagnosis"]["value"] == "Actualizado"
