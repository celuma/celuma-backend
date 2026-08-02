"""Cuarta remediación post-Fase 2 — pruebas de reproducción y regresión.

Cubre las dos observaciones con superficie de backend, más el contrato del
adaptador Legacy del que depende la paridad visual del frontend:

  Observación 2 — la descripción de un membrete no se puede dejar vacía
  Observación 3 — el membrete Legacy exportado no expresa el diseño Legacy

La Observación 1 (impresión local) no tiene backend a propósito: imprimir
es una acción del navegador sobre el renderer ya montado, y esta suite
incluye una prueba que fija justamente eso — que no se añadió ningún
endpoint ni se tocó el flujo del PDF oficial.
"""
import base64
import json

from app.schemas.report_letterhead import normalize_optional_description
from app.services.legacy_letterhead_adapter import build_legacy_letterhead_export

from .factories import (
    auth_headers,
    create_letterhead,
    create_letterhead_version,
    create_tenant,
    create_user,
    valid_presentation,
)

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _create(client, user, **payload):
    return client.post(
        "/api/v1/report-letterheads/",
        json=payload,
        headers=auth_headers(user),
    )


def _update(client, user, letterhead_id, **payload):
    return client.put(
        f"/api/v1/report-letterheads/{letterhead_id}",
        json=payload,
        headers=auth_headers(user),
    )


def _get(client, user, letterhead_id):
    return client.get(
        f"/api/v1/report-letterheads/{letterhead_id}",
        headers=auth_headers(user),
    )


def _import_envelope(client, user, envelope: dict, filename: str = "membrete.cell"):
    return client.post(
        "/api/v1/report-letterheads/import",
        files={"file": (filename, json.dumps(envelope).encode("utf-8"), "application/json")},
        headers=auth_headers(user),
    )


# ===========================================================================
# Observación 2 — descripción opcional
# ===========================================================================

class TestOptionalDescriptionNormalization:
    """El normalizador compartido es el único sitio donde se decide qué
    significa "vacío"; create/update/import/export lo reutilizan."""

    def test_none_stays_none(self):
        assert normalize_optional_description(None) is None

    def test_empty_string_becomes_none(self):
        assert normalize_optional_description("") is None

    def test_whitespace_only_becomes_none(self):
        assert normalize_optional_description("   ") is None
        assert normalize_optional_description("\n\t  \n") is None

    def test_text_is_trimmed_but_preserved(self):
        assert normalize_optional_description("  Texto  ") == "Texto"
        assert normalize_optional_description("Texto") == "Texto"


class TestOptionalDescriptionCreate:
    def test_create_without_description(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        resp = _create(client, user, name="Sin descripción")
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] is None

    def test_create_with_explicit_null_description(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        resp = _create(client, user, name="Nulo explícito", description=None)
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] is None

    def test_create_with_empty_string_description(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        resp = _create(client, user, name="Cadena vacía", description="")
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] is None, "\"\" debe normalizarse a null"

    def test_create_with_whitespace_description(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        resp = _create(client, user, name="Solo espacios", description="   ")
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] is None


class TestOptionalDescriptionUpdate:
    """CAUSA RAÍZ de la Observación 2: el endpoint usaba
    `if data.description is not None`, así que enviar `null` (o `""`, que el
    schema normaliza a `null`) se interpretaba como "no tocar"."""

    def test_description_can_be_cleared_with_null(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        created = _create(client, user, name="Con texto", description="Texto inicial").json()
        assert created["description"] == "Texto inicial"

        resp = _update(client, user, created["id"], name="Con texto", description=None)
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] is None

        # Y sigue vacía al releer (no era solo la respuesta del PUT).
        assert _get(client, user, created["id"]).json()["description"] is None

    def test_description_can_be_cleared_with_empty_string(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        created = _create(client, user, name="Con texto", description="Texto inicial").json()

        resp = _update(client, user, created["id"], name="Con texto", description="")
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] is None
        assert _get(client, user, created["id"]).json()["description"] is None

    def test_description_can_be_cleared_with_whitespace(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        created = _create(client, user, name="Con texto", description="Texto inicial").json()

        resp = _update(client, user, created["id"], name="Con texto", description="  \n ")
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] is None

    def test_omitting_description_leaves_it_untouched(self, client, session):
        """La otra mitad del contrato: "campo omitido" NO es "campo vacío".
        Renombrar sin mandar `description` no debe borrar la que hubiera."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        created = _create(client, user, name="Original", description="No me borres").json()

        resp = _update(client, user, created["id"], name="Renombrado")
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Renombrado"
        assert resp.json()["description"] == "No me borres"

    def test_description_can_be_set_again_after_clearing(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        created = _create(client, user, name="Ciclo", description="Uno").json()
        _update(client, user, created["id"], description=None)
        resp = _update(client, user, created["id"], description="Dos")
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Dos"


class TestOptionalDescriptionDuplicateAndPortability:
    def test_duplicate_of_a_letterhead_without_description(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        created = _create(client, user, name="Sin descripción").json()

        resp = client.post(
            f"/api/v1/report-letterheads/{created['id']}/duplicate",
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] is None

    def test_export_of_a_letterhead_without_description_is_null(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        # El nombre lleva acento a propósito: al escribir esta prueba salió
        # a la luz que `Content-Disposition` metía el nombre crudo en una
        # cabecera HTTP (latin-1), y el export entero fallaba con un error
        # de codificación. Ver `export_letterhead_version` en
        # app/api/v1/report_letterheads.py.
        letterhead = create_letterhead(session, tenant, name="Sin descripción")
        version = create_letterhead_version(
            session, tenant, letterhead, configuration=valid_presentation()
        )

        resp = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version.id}/export",
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["letterhead"]["description"] is None
        disposition = resp.headers["content-disposition"]
        assert "filename*=UTF-8''" in disposition, (
            "un nombre con acentos debe viajar percent-encoded (RFC 6266)"
        )
        assert disposition.encode("latin-1"), "la cabecera debe ser codificable en latin-1"

    def test_import_of_a_cell_with_null_description(self, client, session):
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        user_a = create_user(session, tenant_a, email="admin@a.example")
        user_b = create_user(session, tenant_b, email="admin@b.example")
        letterhead = create_letterhead(session, tenant_a, name="Origen")
        version = create_letterhead_version(
            session, tenant_a, letterhead, configuration=valid_presentation()
        )
        exported = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version.id}/export",
            headers=auth_headers(user_a),
        ).json()
        assert exported["letterhead"]["description"] is None

        imported = _import_envelope(client, user_b, exported)
        assert imported.status_code == 200, imported.text
        detail = _get(client, user_b, imported.json()["report_letterhead_id"])
        assert detail.json()["description"] is None

    def test_import_of_a_cell_whose_description_is_blank_text(self, client, session):
        """Un `.cell` escrito a mano (o por otra herramienta) puede traer
        `""` o espacios: el import debe darles la MISMA semántica que
        `null`, no crear un membrete con una descripción invisible."""
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        user_a = create_user(session, tenant_a, email="admin@a.example")
        user_b = create_user(session, tenant_b, email="admin@b.example")
        letterhead = create_letterhead(session, tenant_a, name="Origen")
        version = create_letterhead_version(
            session, tenant_a, letterhead, configuration=valid_presentation()
        )
        exported = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version.id}/export",
            headers=auth_headers(user_a),
        ).json()
        exported["letterhead"]["description"] = "   "

        imported = _import_envelope(client, user_b, exported)
        assert imported.status_code == 200, imported.text
        detail = _get(client, user_b, imported.json()["report_letterhead_id"])
        assert detail.json()["description"] is None


# ===========================================================================
# Observación 3 — capacidades de paridad Legacy en el contrato
# ===========================================================================

class TestPresentationContractCompatibility:
    """Los campos nuevos son ADITIVOS: un `configuration` anterior a esta
    remediación (sin ninguno de ellos) debe seguir siendo válido, y sus
    valores deben quedar en `None` — nunca en un default que cambie cómo se
    ve un reporte V2 ya publicado."""

    def test_pre_remediation_configuration_is_still_accepted(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        created = _create(client, user, name="Histórico").json()

        resp = client.put(
            f"/api/v1/report-letterheads/{created['id']}/versions/current",
            json={"configuration": valid_presentation()},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        config = resp.json()["configuration"]
        # Ausentes en la entrada -> `None` en la salida: sin `logo_mode` el
        # renderer conserva su comportamiento anterior, y sin
        # `offset_mm`/`content_gap_mm`/`padding_mm` conserva su geometría.
        assert config["header"]["logo_mode"] is None
        assert config["header"]["offset_mm"] is None
        assert config["header"]["content_gap_mm"] is None
        assert config["header"]["padding_mm"] is None
        assert config["header"]["signer_placement"] is None
        assert config["footer"]["logo_mode"] is None
        assert config["footer"]["layout"] is None
        assert config["paper"]["body_padding_top_mm"] is None
        assert config["style"]["typography"]["header_font_weight"] is None
        assert config["style"]["typography"]["footer_font_weight"] is None

    def test_new_layout_fields_round_trip_through_a_version(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        created = _create(client, user, name="Nuevo").json()

        configuration = valid_presentation()
        configuration["paper"]["body_padding_top_mm"] = 4.0
        configuration["header"].update(
            {
                "logo_mode": "NONE",
                "offset_mm": 0.0,
                "height_mm": 28.0,
                "content_gap_mm": 0.0,
                "padding_mm": 4.0,
                "signer_placement": "INLINE",
            }
        )
        configuration["footer"].update(
            {
                "logo_mode": "CUSTOM",
                "layout": "SPLIT",
                "offset_mm": 0.0,
                "height_mm": 20.0,
                "content_gap_mm": 0.0,
                "padding_mm": 0.0,
                "logo_height_mm": 16.0,
                "logo_max_width_pct": 35.0,
                "text_max_width_pct": 65.0,
            }
        )
        configuration["style"]["typography"] = {
            "font_family": "ARIAL",
            "base_font_size_pt": 10.0,
            "header_font_size_pt": 8.0,
            "footer_font_size_pt": 7.0,
            "header_secondary_font_size_pt": 8.0,
            "header_font_weight": 700,
            "footer_font_weight": 700,
        }

        resp = client.put(
            f"/api/v1/report-letterheads/{created['id']}/versions/current",
            json={"configuration": configuration},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        stored = resp.json()["configuration"]
        assert stored["header"]["logo_mode"] == "NONE"
        assert stored["header"]["signer_placement"] == "INLINE"
        assert stored["header"]["padding_mm"] == 4.0
        assert stored["footer"]["layout"] == "SPLIT"
        assert stored["footer"]["logo_max_width_pct"] == 35.0
        assert stored["footer"]["text_max_width_pct"] == 65.0
        assert stored["paper"]["body_padding_top_mm"] == 4.0
        assert stored["style"]["typography"]["footer_font_weight"] == 700

    def test_font_weight_is_a_closed_enum(self, client, session):
        """No se admite CSS libre ni pesos arbitrarios."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        created = _create(client, user, name="Peso inválido").json()

        configuration = valid_presentation()
        configuration["style"]["typography"] = {
            "font_family": "ARIAL",
            "base_font_size_pt": 10.0,
            "header_font_size_pt": 10.0,
            "footer_font_size_pt": 7.0,
            "footer_font_weight": 650,
        }
        resp = client.put(
            f"/api/v1/report-letterheads/{created['id']}/versions/current",
            json={"configuration": configuration},
            headers=auth_headers(user),
        )
        assert resp.status_code == 422, resp.text

    def test_logo_mode_is_a_closed_enum(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")
        created = _create(client, user, name="Modo inválido").json()

        configuration = valid_presentation()
        configuration["header"]["logo_mode"] = "WHATEVER"
        resp = client.put(
            f"/api/v1/report-letterheads/{created['id']}/versions/current",
            json={"configuration": configuration},
            headers=auth_headers(user),
        )
        assert resp.status_code == 422, resp.text


class TestLegacyAdapterContract:
    """Fija el mapeo campo a campo del adaptador. La suite visual del
    frontend (`tests-visual/legacy_v2_parity.visual.spec.ts`) copia estos
    mismos valores en su fixture; si el adaptador cambia sin actualizar
    aquélla, esta prueba lo delata primero.

    Cada aserción corresponde a una constante real de
    `legacy_report_renderer_v1.tsx` — ver legacy-adapter-v2-contract.md."""

    def test_paper_matches_legacy_geometry(self):
        p = build_legacy_letterhead_export().letterhead.presentation
        # MARGIN_L_MM / MARGIN_R_MM = 18
        assert p.paper.margins_cm.left == 1.8
        assert p.paper.margins_cm.right == 1.8
        # body.paddingTop = 4mm
        assert p.paper.body_padding_top_mm == 4.0

    def test_header_has_no_logo_and_reserves_no_space(self):
        header = build_legacy_letterhead_export().letterhead.presentation.header
        assert header.logo_mode == "NONE", (
            "el encabezado Legacy no tiene logo: su logotipo vive en el pie"
        )
        assert header.logo_storage_id is None

    def test_header_geometry_matches_legacy(self):
        header = build_legacy_letterhead_export().letterhead.presentation.header
        assert header.height_mm == 28.0      # HEADER_H_MM
        assert header.offset_mm == 0.0       # header.top = 0
        assert header.content_gap_mm == 0.0  # body.top = HEADER_H_MM
        assert header.padding_mm == 4.0      # header.paddingBottom = 4mm
        assert header.content_alignment == "BOTTOM"  # alignItems: flex-end
        assert header.divider.enabled is False

    def test_header_renders_the_physician_block_inline(self):
        header = build_legacy_letterhead_export().letterhead.presentation.header
        assert header.signer_placement == "INLINE"
        # Las cuatro líneas vienen del firmante institucional, no de
        # institution_name/subtitle/address (que tienen tamaños distintos).
        assert header.institution_name is None
        assert header.subtitle is None
        assert header.address is None

    def test_signer_carries_the_four_header_lines(self):
        signer = build_legacy_letterhead_export().letterhead.presentation.signer
        assert signer is not None
        assert signer.display_name == "Dra. Arisbeth Villanueva Pérez."
        assert signer.specialty == (
            "Anatomía Patológica, Nefropatología y Citología Exfoliativa"
        )
        assert signer.affiliation == "Centro Médico Nacional de Occidente IMSS. INCMNSZ"
        assert signer.license_number == "DGP3833349 | DGP. ESP 6133871"

    def test_footer_geometry_and_layout_match_legacy(self):
        footer = build_legacy_letterhead_export().letterhead.presentation.footer
        assert footer.height_mm == 20.0      # FOOTER_H_MM
        assert footer.offset_mm == 0.0       # footer.bottom = 0
        assert footer.content_gap_mm == 0.0  # body.bottom = FOOTER_H_MM
        assert footer.padding_mm == 0.0      # el pie Legacy no declara padding
        assert footer.layout == "SPLIT"      # logo y texto con space-between
        assert footer.logo_position == "LEFT"
        assert footer.content_alignment == "RIGHT"
        assert footer.logo_height_mm == 16.0        # calc(20mm - 4mm)
        assert footer.logo_max_width_pct == 35.0    # max-width: 35%
        assert footer.text_max_width_pct == 65.0    # max-width: 65%
        assert footer.divider.enabled is False

    def test_footer_text_is_address_and_contact_on_two_lines(self):
        footer = build_legacy_letterhead_export().letterhead.presentation.footer
        assert footer.custom_text is not None
        lines = footer.custom_text.split("\n")
        assert len(lines) == 2, "Legacy imprime dirección y contacto en dos renglones"
        assert lines[0].startswith("Francisco Rojas González")
        assert lines[1].startswith("Tel. 33 2015 0100")

    def test_legacy_never_printed_page_numbers(self):
        footer = build_legacy_letterhead_export().letterhead.presentation.footer
        assert footer.show_page_number is False

    def test_typography_matches_legacy_sizes_and_weights(self):
        typo = build_legacy_letterhead_export().letterhead.presentation.style.typography
        assert typo.font_family == "ARIAL"
        assert typo.base_font_size_pt == 10.0            # body 10pt
        assert typo.header_font_size_pt == 8.0           # header 8pt
        assert typo.header_secondary_font_size_pt == 8.0  # las CUATRO líneas
        assert typo.header_font_weight == 700            # fontWeight: bold
        assert typo.footer_font_size_pt == 7.0           # footer 7pt
        assert typo.footer_font_weight == 700            # fontWeight: bold

    def test_primary_color_is_the_legacy_ink(self):
        style = build_legacy_letterhead_export().letterhead.presentation.style
        assert style.primary_color == "#002060"

    def test_export_is_deterministic_except_for_the_timestamp(self):
        a = build_legacy_letterhead_export()
        b = build_legacy_letterhead_export()
        assert a.letterhead.model_dump(mode="json") == b.letterhead.model_dump(mode="json")

    def test_the_adapter_emits_no_field_the_contract_ignores(self):
        """`extra="forbid"` en todos los modelos: si el adaptador emitiera
        una clave inventada, esto fallaría al reconstruir el modelo."""
        envelope = build_legacy_letterhead_export()
        from app.schemas.report_template_version import ReportPresentationSnapshotV2

        rebuilt = ReportPresentationSnapshotV2.model_validate(
            envelope.letterhead.presentation.model_dump(mode="json")
        )
        assert rebuilt == envelope.letterhead.presentation


class TestLegacyExportImportRoundTrip:
    def test_importing_the_legacy_cell_preserves_every_parity_field(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")

        exported = client.get(
            "/api/v1/report-letterheads/legacy/export",
            headers=auth_headers(user),
        )
        assert exported.status_code == 200, exported.text
        envelope = exported.json()

        imported = _import_envelope(client, user, envelope, filename="legado.cell")
        assert imported.status_code == 200, imported.text

        active = client.get(
            f"/api/v1/report-letterheads/{imported.json()['report_letterhead_id']}/versions/active",
            headers=auth_headers(user),
        )
        assert active.status_code == 200, active.text
        config = active.json()["configuration"]

        assert config["header"]["logo_mode"] == "NONE"
        assert config["header"]["signer_placement"] == "INLINE"
        assert config["header"]["height_mm"] == 28.0
        assert config["header"]["offset_mm"] == 0.0
        assert config["header"]["content_gap_mm"] == 0.0
        assert config["header"]["padding_mm"] == 4.0
        assert config["footer"]["layout"] == "SPLIT"
        assert config["footer"]["height_mm"] == 20.0
        assert config["footer"]["show_page_number"] is False
        assert config["footer"]["logo_max_width_pct"] == 35.0
        assert config["footer"]["text_max_width_pct"] == 65.0
        assert config["paper"]["body_padding_top_mm"] == 4.0
        assert config["paper"]["margins_cm"]["left"] == 1.8
        assert config["style"]["typography"]["footer_font_weight"] == 700
        assert "\n" in config["footer"]["custom_text"]

    def test_importing_the_legacy_cell_materializes_the_footer_logo(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@a.example")

        envelope = client.get(
            "/api/v1/report-letterheads/legacy/export",
            headers=auth_headers(user),
        ).json()
        # El logotipo viaja como asset del PIE; si el repositorio no lleva el
        # bitmap, el export sale sin él y esta comprobación no aplica.
        if "footer_logo" not in envelope.get("assets", {}):
            return

        imported = _import_envelope(client, user, envelope, filename="legado.cell")
        assert imported.status_code == 200, imported.text
        active = client.get(
            f"/api/v1/report-letterheads/{imported.json()['report_letterhead_id']}/versions/active",
            headers=auth_headers(user),
        ).json()
        assert active["configuration"]["footer"]["logo_storage_id"] is not None
        assert active["configuration"]["header"]["logo_storage_id"] is None, (
            "el logotipo Legacy nunca debe acabar en el encabezado"
        )
        assert active["resolved_resources"]["footer_logo_url"]


# ===========================================================================
# Observación 1 — la impresión local no toca el backend
# ===========================================================================

class TestLocalPrintHasNoBackendSurface:
    def test_no_local_print_endpoint_was_added(self, client, session):
        """La copia local se compone en el navegador a partir del renderer
        ya montado. Si alguna vez apareciera un endpoint "de impresión",
        habría dos caminos de generación de documentos y el artefacto
        oficial dejaría de ser el único — ver local-print-contract.md."""
        from app.main import app

        paths = {route.path for route in app.routes if hasattr(route, "path")}
        offenders = [p for p in paths if "print" in p.lower()]
        assert offenders == [], f"endpoints de impresión inesperados: {offenders}"
