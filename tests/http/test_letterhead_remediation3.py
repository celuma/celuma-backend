"""Tercera remediación post-Fase 2 — pruebas de reproducción y regresión.

Cada clase corresponde a un problema del brief (A–F). Escritas PRIMERO,
en rojo, para reproducir el fallo antes de tocar código; se quedan como
suite de regresión permanente.

  A — import/export `.cell` pierde logo y estilo
  B — logo superior sube pero no persiste (rehidratación)
  C — logo de pie no se muestra ni persiste
  D — no se pueden eliminar membretes
  E — default inconsistente / resolución no determinista
  F — fallback incorrecto a Legacy
"""
import base64
import hashlib
import json

import pytest

from .factories import (
    auth_headers,
    create_branch,
    create_letterhead,
    create_letterhead_version,
    create_order,
    create_storage_object,
    create_tenant,
    create_user,
    valid_presentation,
    valid_rendering_snapshot,
)

# Un PNG 1x1 real — ManagedTenantImageService valida bytes de imagen de
# verdad (Pillow), así que un placeholder de texto no sirve.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def rich_presentation() -> dict:
    """Una presentación con TODOS los campos visuales soportados puestos a
    valores no-default — cualquier campo que el round-trip pierda o
    reconstruya con default se detecta comparando contra esta."""
    return {
        "paper": {
            "size": "LETTER",
            "orientation": "PORTRAIT",
            "margins_cm": {"top": 2.7, "right": 1.3, "bottom": 3.1, "left": 1.9},
        },
        "header": {
            "enabled": True,
            "logo_storage_id": None,
            "institution_name": "Laboratorio Céluma",
            "subtitle": "Anatomía Patológica",
            "address": "Av. Vallarta 1234, Guadalajara",
            "phone": "+52 33 1234 5678",
            "email": "contacto@celuma.example",
            "logo_position": "RIGHT",
            "content_alignment": "BOTTOM",
            "height_mm": 31.5,
            "divider": {
                "enabled": True,
                "style": "DOUBLE",
                "primary_width_px": 2.5,
                "secondary_width_px": 0.75,
                "gap_mm": 1.75,
                "color": "#AA1122",
            },
        },
        "footer": {
            "enabled": True,
            "custom_text": "Documento confidencial — uso clínico",
            "show_page_number": False,
            "logo_storage_id": None,
            "logo_position": "RIGHT",
            "content_alignment": "LEFT",
            "height_mm": 18.5,
            "divider": {
                "enabled": True,
                "style": "DOUBLE",
                "primary_width_px": 1.5,
                "secondary_width_px": 3.0,
                "gap_mm": 2.25,
                "color": "#22AA11",
            },
        },
        "style": {
            "primary_color": "#123456",
            "secondary_color": "#ABCDEF",
            "typography": {
                "font_family": "TIMES",
                "base_font_size_pt": 11.5,
                "header_font_size_pt": 13.0,
                "footer_font_size_pt": 6.5,
            },
        },
        "signer": {
            "display_name": "Dra. Ejemplo Pérez",
            "specialty": "Nefropatología",
            "license_number": "DGP-000111",
            "affiliation": "Centro Médico de Ejemplo",
        },
    }


def _upload_logo(client, user, letterhead_id: str) -> dict:
    resp = client.post(
        f"/api/v1/report-letterheads/{letterhead_id}/logo",
        files={"file": ("logo.png", PNG_1X1, "image/png")},
        headers=auth_headers(user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _save_current(client, user, letterhead_id: str, configuration: dict):
    return client.put(
        f"/api/v1/report-letterheads/{letterhead_id}/versions/current",
        json={"configuration": configuration},
        headers=auth_headers(user),
    )


def _import_envelope(client, user, envelope: dict, filename: str = "membrete.cell"):
    return client.post(
        "/api/v1/report-letterheads/import",
        files={"file": (filename, json.dumps(envelope).encode("utf-8"), "application/json")},
        headers=auth_headers(user),
    )


# ===========================================================================
# Problema A — import/export `.cell` pierde logo y estilo
# ===========================================================================

class TestProblemAImportExportFidelity:
    def test_imported_letterhead_has_an_active_version(self, client, session):
        """RAÍZ del problema A: el import creaba la versión en PUBLISHED, de
        modo que `GET .../versions/active` devolvía 404 y el editor arrancaba
        desde BLANK_PRESENTATION — "se pierde todo" al importar."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant, name="Origen")
        version = create_letterhead_version(
            session, tenant, letterhead, configuration=rich_presentation()
        )

        exported = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version.id}/export",
            headers=auth_headers(user),
        ).json()

        imported = _import_envelope(client, user, exported)
        assert imported.status_code == 200, imported.text
        body = imported.json()
        assert body["status"] == "ACTIVE", (
            "un membrete importado debe quedar inmediatamente resoluble/editable"
        )

        active = client.get(
            f"/api/v1/report-letterheads/{body['report_letterhead_id']}/versions/active",
            headers=auth_headers(user),
        )
        assert active.status_code == 200, active.text

    def test_round_trip_preserves_every_visual_field(self, client, session):
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        user_a = create_user(session, tenant_a, email="admin@a.example")
        user_b = create_user(session, tenant_b, email="admin@b.example")
        letterhead = create_letterhead(session, tenant_a, name="Completo")
        version = create_letterhead_version(
            session, tenant_a, letterhead, configuration=rich_presentation()
        )

        exported = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version.id}/export",
            headers=auth_headers(user_a),
        ).json()

        imported = _import_envelope(client, user_b, exported)
        assert imported.status_code == 200, imported.text
        got = imported.json()["configuration"]

        expected = rich_presentation()
        # Los ids de StorageObject son deliberadamente regenerados por el
        # import — es lo único que puede diferir.
        got_cmp = json.loads(json.dumps(got))
        got_cmp["header"]["logo_storage_id"] = None
        got_cmp["footer"]["logo_storage_id"] = None

        # Cuarta remediación: el contrato ganó campos opcionales nuevos
        # (`logo_mode`, `offset_mm`, `content_gap_mm`, `padding_mm`,
        # `signer_placement`, `layout`, `body_padding_top_mm`, pesos
        # tipográficos…). Un `.cell` anterior no los trae, así que la
        # comparación se hace sobre las claves que el archivo SÍ traía —
        # que es lo que esta prueba siempre quiso demostrar: que ninguna se
        # pierde ni se reconstruye con un default.
        def project(actual: dict, reference: dict) -> dict:
            out = {}
            for key, ref_value in reference.items():
                got_value = actual.get(key)
                if isinstance(ref_value, dict) and isinstance(got_value, dict):
                    out[key] = project(got_value, ref_value)
                else:
                    out[key] = got_value
            return out

        assert project(got_cmp, expected) == expected

        # …y la otra mitad del contrato: los campos nuevos, ausentes en el
        # archivo, vuelven como `None`. Nunca inventados, para que un
        # membrete importado desde un `.cell` antiguo siga renderizando
        # exactamente como antes de esta remediación.
        assert got_cmp["header"]["logo_mode"] is None
        assert got_cmp["header"]["offset_mm"] is None
        assert got_cmp["header"]["content_gap_mm"] is None
        assert got_cmp["header"]["padding_mm"] is None
        assert got_cmp["header"]["signer_placement"] is None
        assert got_cmp["footer"]["logo_mode"] is None
        assert got_cmp["footer"]["layout"] is None
        assert got_cmp["paper"]["body_padding_top_mm"] is None
        assert got_cmp["style"]["typography"]["header_font_weight"] is None
        assert got_cmp["style"]["typography"]["footer_font_weight"] is None

    def test_round_trip_preserves_both_logos(self, client, session):
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        user_a = create_user(session, tenant_a, email="admin@a.example")
        user_b = create_user(session, tenant_b, email="admin@b.example")
        letterhead = create_letterhead(session, tenant_a, name="Dos logos")

        header_logo = _upload_logo(client, user_a, str(letterhead.id))
        footer_logo = _upload_logo(client, user_a, str(letterhead.id))
        presentation = rich_presentation()
        presentation["header"]["logo_storage_id"] = header_logo["storage_object_id"]
        presentation["footer"]["logo_storage_id"] = footer_logo["storage_object_id"]
        saved = _save_current(client, user_a, str(letterhead.id), presentation)
        assert saved.status_code == 200, saved.text
        version_id = saved.json()["id"]

        exported = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version_id}/export",
            headers=auth_headers(user_a),
        ).json()
        assert set(exported["assets"]) == {"header_logo", "footer_logo"}

        imported = _import_envelope(client, user_b, exported)
        assert imported.status_code == 200, imported.text
        cfg = imported.json()["configuration"]
        assert cfg["header"]["logo_storage_id"] is not None
        assert cfg["footer"]["logo_storage_id"] is not None
        assert cfg["header"]["logo_storage_id"] != cfg["footer"]["logo_storage_id"]
        # ...y son objetos del tenant importador, no del exportador.
        assert cfg["header"]["logo_storage_id"] != header_logo["storage_object_id"]

    def test_export_fails_loudly_when_a_referenced_logo_is_missing(self, client, session):
        """Antes: `_export_asset` devolvía None en silencio y el `.cell`
        salía sin logo — el usuario solo lo descubría tras importar."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant)
        presentation = valid_presentation()
        # Un id con forma válida que no existe en storage_object.
        presentation["header"] = {
            **presentation["header"],
            "logo_storage_id": "11111111-2222-3333-4444-555555555555",
        }
        version = create_letterhead_version(
            session, tenant, letterhead, configuration=presentation
        )

        resp = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version.id}/export",
            headers=auth_headers(user),
        )
        assert resp.status_code == 409, resp.text

    def test_legacy_export_carries_logo_and_full_style(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")

        resp = client.get(
            "/api/v1/report-letterheads/legacy/export", headers=auth_headers(user)
        )
        assert resp.status_code == 200, resp.text
        envelope = resp.json()
        assert "footer_logo" in envelope["assets"], "el logo Legacy vive en el pie"
        presentation = envelope["letterhead"]["presentation"]
        assert presentation["style"]["primary_color"] == "#002060"
        assert presentation["header"]["divider"]["enabled"] is False
        assert presentation["footer"]["content_alignment"] == "RIGHT"
        assert presentation["signer"]["license_number"]

    def test_legacy_import_lands_active_with_footer_logo(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        envelope = client.get(
            "/api/v1/report-letterheads/legacy/export", headers=auth_headers(user)
        ).json()

        imported = _import_envelope(client, user, envelope, filename="legacy.cell")
        assert imported.status_code == 200, imported.text
        body = imported.json()
        assert body["status"] == "ACTIVE"
        cfg = body["configuration"]
        assert cfg["footer"]["logo_storage_id"] is not None
        assert cfg["style"]["primary_color"] == "#002060"
        assert cfg["style"]["typography"]["header_font_size_pt"] == 8.0

    def test_format_version_1_celuma_still_imports(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        envelope = {
            "format": "celuma-letterhead",
            "format_version": 1,
            "exported_at": "2026-01-01T00:00:00+00:00",
            "source": {"product": "Céluma", "schema_version": 2},
            "letterhead": {
                "name": "Membrete v1",
                "description": None,
                "presentation": valid_presentation(),
            },
            "assets": {
                "logo": {
                    "media_type": "image/png",
                    "sha256": hashlib.sha256(PNG_1X1).hexdigest(),
                    "data_base64": base64.b64encode(PNG_1X1).decode("ascii"),
                }
            },
        }
        imported = _import_envelope(client, user, envelope, filename="viejo.celuma")
        assert imported.status_code == 200, imported.text
        body = imported.json()
        assert body["status"] == "ACTIVE"
        assert body["configuration"]["header"]["logo_storage_id"] is not None
        assert body["configuration"]["footer"]["logo_storage_id"] is None


# ===========================================================================
# Problemas B y C — persistencia y resolución de logos
# ===========================================================================

class TestProblemsBCLogoPersistence:
    def test_active_version_exposes_resolved_logo_urls(self, client, session):
        """RAÍZ de B/C en el editor: no existía ninguna forma de obtener la
        URL efímera del logo ya persistido, así que al reabrir el editor
        siempre caía al logo neutral."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant)
        header_logo = _upload_logo(client, user, str(letterhead.id))
        footer_logo = _upload_logo(client, user, str(letterhead.id))

        presentation = valid_presentation()
        presentation["header"] = {
            **presentation["header"],
            "logo_storage_id": header_logo["storage_object_id"],
        }
        presentation["footer"] = {
            **presentation["footer"],
            "logo_storage_id": footer_logo["storage_object_id"],
        }
        assert _save_current(client, user, str(letterhead.id), presentation).status_code == 200

        active = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/active",
            headers=auth_headers(user),
        )
        assert active.status_code == 200, active.text
        body = active.json()
        assert body["configuration"]["header"]["logo_storage_id"] == header_logo["storage_object_id"]
        assert body["configuration"]["footer"]["logo_storage_id"] == footer_logo["storage_object_id"]
        assert body["resolved_resources"]["header_logo_url"]
        assert body["resolved_resources"]["footer_logo_url"]
        assert (
            body["resolved_resources"]["header_logo_url"]
            != body["resolved_resources"]["footer_logo_url"]
        )

    def test_header_logo_survives_save_and_reload(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant)
        logo = _upload_logo(client, user, str(letterhead.id))

        presentation = valid_presentation()
        presentation["header"] = {
            **presentation["header"],
            "logo_storage_id": logo["storage_object_id"],
        }
        _save_current(client, user, str(letterhead.id), presentation)

        # Segundo guardado sin tocar el logo (el editor reenvía el mismo id).
        presentation["style"] = {"primary_color": "#FF0000"}
        _save_current(client, user, str(letterhead.id), presentation)

        active = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/active",
            headers=auth_headers(user),
        ).json()
        assert active["configuration"]["header"]["logo_storage_id"] == logo["storage_object_id"]
        assert active["configuration"]["style"]["primary_color"] == "#FF0000"

    def test_footer_logo_is_validated_on_publish_endpoint_too(self, client, session):
        """`POST .../versions` validaba `header.logo_storage_id` pero no el
        del pie — un id ajeno/inexistente entraba sin ruido."""
        tenant = create_tenant(session)
        other = create_tenant(session, name="Otro")
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant)
        foreign = create_storage_object(session, key="logos/foreign.png", tenant=other)

        presentation = valid_presentation()
        presentation["footer"] = {
            **presentation["footer"],
            "logo_storage_id": str(foreign.id),
        }
        resp = client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/versions",
            json={"configuration": presentation},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400, resp.text

    def test_removing_a_logo_persists_as_null(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant)
        logo = _upload_logo(client, user, str(letterhead.id))

        presentation = valid_presentation()
        presentation["footer"] = {
            **presentation["footer"],
            "logo_storage_id": logo["storage_object_id"],
        }
        _save_current(client, user, str(letterhead.id), presentation)

        presentation["footer"] = {**presentation["footer"], "logo_storage_id": None}
        _save_current(client, user, str(letterhead.id), presentation)

        active = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/active",
            headers=auth_headers(user),
        ).json()
        assert active["configuration"]["footer"]["logo_storage_id"] is None
        assert (active.get("resolved_resources") or {}).get("footer_logo_url") is None


# ===========================================================================
# Problema D — eliminación / desactivación
# ===========================================================================

class TestProblemDDeletion:
    def test_delete_unreferenced_letterhead_with_versions_succeeds(self, client, session):
        """Antes: cualquier membrete con versiones (es decir, cualquiera que
        se hubiese guardado alguna vez) era imposible de eliminar."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant, name="Desechable")
        create_letterhead_version(session, tenant, letterhead, status="ACTIVE")

        resp = client.delete(
            f"/api/v1/report-letterheads/{letterhead.id}?hard_delete=true",
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text

        assert client.get(
            f"/api/v1/report-letterheads/{letterhead.id}", headers=auth_headers(user)
        ).status_code == 404

    def test_delete_default_letterhead_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant)
        create_letterhead_version(session, tenant, letterhead, status="ACTIVE")
        client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/default", headers=auth_headers(user)
        )

        resp = client.delete(
            f"/api/v1/report-letterheads/{letterhead.id}?hard_delete=true",
            headers=auth_headers(user),
        )
        assert resp.status_code == 409, resp.text

    def test_delete_template_preferred_letterhead_is_rejected(self, client, session):
        from app.models.report import ReportTemplate

        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant)
        create_letterhead_version(session, tenant, letterhead, status="ACTIVE")
        template = ReportTemplate(
            tenant_id=tenant.id, name="Plantilla", preferred_letterhead_id=letterhead.id
        )
        session.add(template)
        session.commit()

        resp = client.delete(
            f"/api/v1/report-letterheads/{letterhead.id}?hard_delete=true",
            headers=auth_headers(user),
        )
        assert resp.status_code == 409, resp.text

    def test_delete_letterhead_used_by_a_report_is_rejected(self, client, session):
        from app.models.report import Report, ReportTemplate, ReportVersion
        from app.models.report_template_version import (
            ReportTemplateVersion,
            ReportTemplateVersionStatus,
        )

        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        user = create_user(session, tenant, email="admin@a.example")
        order = create_order(session, tenant, branch)
        letterhead = create_letterhead(session, tenant)
        version = create_letterhead_version(session, tenant, letterhead, status="ACTIVE")

        template = ReportTemplate(tenant_id=tenant.id, name="Clínica")
        session.add(template)
        session.flush()
        template_version = ReportTemplateVersion(
            tenant_id=tenant.id,
            report_template_id=template.id,
            version_number=1,
            schema_version=2,
            configuration=valid_rendering_snapshot(),
            status=ReportTemplateVersionStatus.ACTIVE,
        )
        session.add(template_version)
        session.flush()

        report = Report(tenant_id=tenant.id, branch_id=branch.id, order_id=order.id)
        session.add(report)
        session.flush()
        session.add(
            ReportVersion(
                report_id=report.id,
                version_no=1,
                is_current=True,
                schema_version=2,
                template_version_id=template_version.id,
                letterhead_version_id=version.id,
            )
        )
        session.commit()

        resp = client.delete(
            f"/api/v1/report-letterheads/{letterhead.id}?hard_delete=true",
            headers=auth_headers(user),
        )
        assert resp.status_code == 409, resp.text

    def test_deactivate_keeps_versions_and_hides_from_selection(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant, name="Archivable")
        create_letterhead_version(session, tenant, letterhead, status="ACTIVE")

        resp = client.delete(
            f"/api/v1/report-letterheads/{letterhead.id}", headers=auth_headers(user)
        )
        assert resp.status_code == 200, resp.text

        listed = client.get(
            "/api/v1/report-letterheads/?active_only=true", headers=auth_headers(user)
        ).json()["letterheads"]
        assert all(l["id"] != str(letterhead.id) for l in listed)

        # Sus versiones siguen existiendo (los reportes históricos las usan).
        versions = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions", headers=auth_headers(user)
        ).json()["versions"]
        assert len(versions) == 1

    def test_deactivating_the_default_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant)
        create_letterhead_version(session, tenant, letterhead, status="ACTIVE")
        client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/default", headers=auth_headers(user)
        )

        resp = client.delete(
            f"/api/v1/report-letterheads/{letterhead.id}", headers=auth_headers(user)
        )
        assert resp.status_code == 409, resp.text

    def test_inactive_letterhead_cannot_become_default(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        letterhead = create_letterhead(session, tenant)
        create_letterhead_version(session, tenant, letterhead, status="ACTIVE")
        client.delete(
            f"/api/v1/report-letterheads/{letterhead.id}", headers=auth_headers(user)
        )

        resp = client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/default", headers=auth_headers(user)
        )
        assert resp.status_code == 409, resp.text


# ===========================================================================
# Problema E — resolución determinista
# ===========================================================================

class TestProblemEDeterministicResolution:
    def test_tenant_default_is_used_when_template_has_no_preference(self, client, session):
        from app.services.letterhead_resolution import (
            LetterheadResolutionSource,
            resolve_effective_letterhead_version,
        )
        from app.models.report import ReportTemplate

        tenant = create_tenant(session)
        letterhead = create_letterhead(session, tenant, name="Default del tenant")
        letterhead.is_default = True
        session.add(letterhead)
        version = create_letterhead_version(session, tenant, letterhead, status="ACTIVE")
        template = ReportTemplate(tenant_id=tenant.id, name="Sin preferencia")
        session.add(template)
        session.commit()

        resolved = resolve_effective_letterhead_version(
            session, str(tenant.id), template=template
        )
        assert resolved.version.id == version.id
        assert resolved.letterhead.id == letterhead.id
        assert resolved.source == LetterheadResolutionSource.TENANT_DEFAULT

    def test_template_preference_wins_over_tenant_default(self, client, session):
        from app.services.letterhead_resolution import (
            LetterheadResolutionSource,
            resolve_effective_letterhead_version,
        )
        from app.models.report import ReportTemplate

        tenant = create_tenant(session)
        default_lh = create_letterhead(session, tenant, name="Default")
        default_lh.is_default = True
        session.add(default_lh)
        create_letterhead_version(session, tenant, default_lh, status="ACTIVE")

        preferred_lh = create_letterhead(session, tenant, name="Preferido")
        preferred_version = create_letterhead_version(
            session, tenant, preferred_lh, status="ACTIVE"
        )
        template = ReportTemplate(
            tenant_id=tenant.id, name="Con preferencia", preferred_letterhead_id=preferred_lh.id
        )
        session.add(template)
        session.commit()

        resolved = resolve_effective_letterhead_version(
            session, str(tenant.id), template=template
        )
        assert resolved.version.id == preferred_version.id
        assert resolved.source == LetterheadResolutionSource.TEMPLATE_PREFERRED

    def test_explicit_letterhead_wins_over_everything(self, client, session):
        from app.services.letterhead_resolution import (
            LetterheadResolutionSource,
            resolve_effective_letterhead_version,
        )

        tenant = create_tenant(session)
        default_lh = create_letterhead(session, tenant, name="Default")
        default_lh.is_default = True
        session.add(default_lh)
        create_letterhead_version(session, tenant, default_lh, status="ACTIVE")
        explicit_lh = create_letterhead(session, tenant, name="Explícito")
        explicit_version = create_letterhead_version(
            session, tenant, explicit_lh, status="ACTIVE"
        )
        session.commit()

        resolved = resolve_effective_letterhead_version(
            session, str(tenant.id), template=None, letterhead_id=str(explicit_lh.id)
        )
        assert resolved.version.id == explicit_version.id
        assert resolved.source == LetterheadResolutionSource.EXPLICIT

    def test_no_resolvable_letterhead_returns_none(self, client, session):
        from app.services.letterhead_resolution import resolve_effective_letterhead_version

        tenant = create_tenant(session)
        session.commit()
        assert resolve_effective_letterhead_version(session, str(tenant.id), template=None) is None

    def test_default_without_active_version_is_a_configuration_error(self, client, session):
        from app.services.letterhead_resolution import (
            LetterheadConfigurationError,
            resolve_effective_letterhead_version,
        )

        tenant = create_tenant(session)
        letterhead = create_letterhead(session, tenant)
        letterhead.is_default = True
        session.add(letterhead)
        create_letterhead_version(session, tenant, letterhead, status="PUBLISHED")
        session.commit()

        with pytest.raises(LetterheadConfigurationError):
            resolve_effective_letterhead_version(session, str(tenant.id), template=None)

    def test_multiple_active_versions_fail_explicitly(self, client, session):
        """El índice parcial único lo impide hoy, pero filas históricas o una
        restauración manual podrían violarlo: nunca elegir arbitrariamente."""
        from app.models.report_letterhead_version import ReportLetterheadVersion
        from app.services.letterhead_resolution import (
            LetterheadConfigurationError,
            resolve_effective_letterhead_version,
        )
        from sqlalchemy import text

        tenant = create_tenant(session)
        letterhead = create_letterhead(session, tenant)
        letterhead.is_default = True
        session.add(letterhead)
        v1 = create_letterhead_version(session, tenant, letterhead, status="ACTIVE")
        v2 = create_letterhead_version(
            session, tenant, letterhead, version_number=2, status="PUBLISHED"
        )
        session.commit()
        # Se salta el índice único parcial deshabilitándolo un instante:
        # reproducimos datos corruptos, no un flujo de la app.
        session.exec(text("DROP INDEX ix_report_letterhead_version_one_active"))
        session.exec(
            text(
                "UPDATE report_letterhead_version SET status = 'ACTIVE' WHERE id = :vid"
            ).bindparams(vid=str(v2.id))
        )
        session.commit()

        with pytest.raises(LetterheadConfigurationError):
            resolve_effective_letterhead_version(session, str(tenant.id), template=None)

    def test_cross_tenant_letterhead_is_never_resolved(self, client, session):
        from app.services.letterhead_resolution import resolve_effective_letterhead_version
        from app.models.report import ReportTemplate

        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        lh_b = create_letterhead(session, tenant_b, name="De B")
        create_letterhead_version(session, tenant_b, lh_b, status="ACTIVE")
        template_a = ReportTemplate(
            tenant_id=tenant_a.id, name="De A", preferred_letterhead_id=lh_b.id
        )
        session.add(template_a)
        session.commit()

        assert (
            resolve_effective_letterhead_version(
                session, str(tenant_a.id), template=template_a
            )
            is None
        )

    def test_inactive_letterhead_is_never_resolved(self, client, session):
        from app.services.letterhead_resolution import resolve_effective_letterhead_version
        from app.models.report import ReportTemplate

        tenant = create_tenant(session)
        letterhead = create_letterhead(session, tenant)
        letterhead.is_active = False
        session.add(letterhead)
        create_letterhead_version(session, tenant, letterhead, status="ACTIVE")
        template = ReportTemplate(
            tenant_id=tenant.id, name="T", preferred_letterhead_id=letterhead.id
        )
        session.add(template)
        session.commit()

        assert (
            resolve_effective_letterhead_version(session, str(tenant.id), template=template)
            is None
        )


# ===========================================================================
# Problema F — el endpoint de defaults nunca debe dejar caer a Legacy
# ===========================================================================

class TestProblemFNoLegacyFallback:
    def _study_type_with_template(self, session, tenant, *, template):
        from app.models.study_type import StudyType

        study_type = StudyType(
            tenant_id=tenant.id,
            code="HP",
            name="Histopatología",
            default_report_template_id=template.id,
        )
        session.add(study_type)
        session.commit()
        session.refresh(study_type)
        return study_type

    def _template_with_active_version(self, session, tenant):
        from app.models.report import ReportTemplate
        from app.models.report_template_version import (
            ReportTemplateVersion,
            ReportTemplateVersionStatus,
        )

        template = ReportTemplate(tenant_id=tenant.id, name="Clínica")
        session.add(template)
        session.flush()
        version = ReportTemplateVersion(
            tenant_id=tenant.id,
            report_template_id=template.id,
            version_number=1,
            schema_version=2,
            configuration=valid_rendering_snapshot(),
            status=ReportTemplateVersionStatus.ACTIVE,
        )
        session.add(version)
        session.commit()
        session.refresh(template)
        return template, version

    def test_report_defaults_falls_back_to_tenant_default(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        user = create_user(session, tenant, email="admin@a.example")
        template, _ = self._template_with_active_version(session, tenant)
        study_type = self._study_type_with_template(session, tenant, template=template)

        letterhead = create_letterhead(session, tenant, name="Predeterminado")
        letterhead.is_default = True
        session.add(letterhead)
        version = create_letterhead_version(session, tenant, letterhead, status="ACTIVE")
        session.commit()

        resp = client.get(
            f"/api/v1/study-types/{study_type.id}/report-defaults", headers=auth_headers(user)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["letterhead_version_id"] == str(version.id)
        assert body["letterhead_id"] == str(letterhead.id)
        assert body["letterhead_resolution_source"] == "TENANT_DEFAULT"
        assert body["v2_blocked_reason"] is None

    def test_report_defaults_reports_blocking_reason_without_letterhead(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        user = create_user(session, tenant, email="admin@a.example")
        template, _ = self._template_with_active_version(session, tenant)
        study_type = self._study_type_with_template(session, tenant, template=template)

        resp = client.get(
            f"/api/v1/study-types/{study_type.id}/report-defaults", headers=auth_headers(user)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["letterhead_version_id"] is None
        assert body["v2_blocked_reason"] == "NO_LETTERHEAD"

    def test_report_defaults_exposes_the_resolved_presentation(self, client, session):
        """Sin esto el editor tenía que hacer N+1 llamadas y, si alguna
        fallaba, se quedaba sin `presentation` y montaba Legacy."""
        tenant = create_tenant(session, reports_v2_enabled=True)
        user = create_user(session, tenant, email="admin@a.example")
        template, _ = self._template_with_active_version(session, tenant)
        study_type = self._study_type_with_template(session, tenant, template=template)
        letterhead = create_letterhead(session, tenant)
        letterhead.is_default = True
        session.add(letterhead)
        create_letterhead_version(
            session,
            tenant,
            letterhead,
            status="ACTIVE",
            configuration=valid_presentation(style={"primary_color": "#00FF00"}),
        )
        session.commit()

        body = client.get(
            f"/api/v1/study-types/{study_type.id}/report-defaults", headers=auth_headers(user)
        ).json()
        assert body["letterhead_presentation"]["style"]["primary_color"] == "#00FF00"

    def test_create_report_v2_is_blocked_without_a_resolvable_letterhead(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        user = create_user(session, tenant, email="admin@a.example")
        order = create_order(session, tenant, branch)
        _, template_version = self._template_with_active_version(session, tenant)

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "title": "Reporte",
                "template": {"base": {}, "sections": {}},
                "report": {"base": {}, "sections": {}},
                "schema_version": 2,
                "template_version_id": str(template_version.id),
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 409, resp.text
        assert "membrete" in resp.text.lower() or "letterhead" in resp.text.lower()
