"""HTTP integration tests for `.celuma` export/import — post-Fase-2
remediation, R12/R15."""
import base64
import hashlib
import json

from .factories import (
    auth_headers,
    create_letterhead,
    create_letterhead_version,
    create_tenant,
    create_user,
    valid_presentation,
)


class TestExport:
    def test_export_never_leaks_tenant_or_storage_ids(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant, name="Exportable")
        version = create_letterhead_version(session, tenant, letterhead)

        resp = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version.id}/export",
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["format"] == "celuma-letterhead"
        assert body["format_version"] == 2
        assert body["letterhead"]["name"] == "Exportable"

        raw_text = json.dumps(body)
        assert str(tenant.id) not in raw_text
        assert "storage_object_id" not in raw_text
        assert body["letterhead"]["presentation"]["header"]["logo_storage_id"] is None

    def test_export_without_logo_has_no_assets(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant)
        version = create_letterhead_version(session, tenant, letterhead)  # valid_presentation() has no logo

        resp = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version.id}/export",
            headers=auth_headers(user),
        )
        assert resp.status_code == 200
        assert resp.json()["assets"] == {}

    def test_cross_tenant_export_is_not_found(self, client, session):
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        user_b = create_user(session, tenant_b, email="admin@b.example")
        letterhead_a = create_letterhead(session, tenant_a)
        version_a = create_letterhead_version(session, tenant_a, letterhead_a)

        resp = client.get(
            f"/api/v1/report-letterheads/{letterhead_a.id}/versions/{version_a.id}/export",
            headers=auth_headers(user_b),
        )
        assert resp.status_code == 404


class TestRoundTrip:
    def test_export_then_import_preserves_configuration(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant, name="Round Trip")
        version = create_letterhead_version(
            session, tenant, letterhead,
            configuration=valid_presentation(style={"primary_color": "#654321"}),
        )

        exported = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version.id}/export",
            headers=auth_headers(user),
        ).json()

        imported = client.post(
            "/api/v1/report-letterheads/import",
            files={"file": ("roundtrip.celuma", json.dumps(exported).encode(), "application/json")},
            headers=auth_headers(user),
        )
        assert imported.status_code == 200, imported.text
        body = imported.json()
        assert body["configuration"]["style"]["primary_color"] == "#654321"
        assert body["status"] == "PUBLISHED"
        # Never reuses the source letterhead/version id.
        assert body["report_letterhead_id"] != str(letterhead.id)
        assert body["id"] != str(version.id)

    def test_import_never_makes_it_default_or_active(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant)
        version = create_letterhead_version(session, tenant, letterhead)

        exported = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version.id}/export",
            headers=auth_headers(user),
        ).json()
        imported = client.post(
            "/api/v1/report-letterheads/import",
            files={"file": ("x.celuma", json.dumps(exported).encode(), "application/json")},
            headers=auth_headers(user),
        ).json()

        letterheads = client.get("/api/v1/report-letterheads/", headers=auth_headers(user)).json()
        new_letterhead = next(l for l in letterheads["letterheads"] if l["id"] == imported["report_letterhead_id"])
        assert new_letterhead["is_default"] is False
        assert imported["status"] == "PUBLISHED"  # not ACTIVE


class TestImportValidation:
    def _minimal_envelope(self, **overrides):
        env = {
            "format": "celuma-letterhead",
            "format_version": 1,
            "exported_at": "2026-01-01T00:00:00Z",
            "source": {"product": "Céluma", "schema_version": 2},
            "letterhead": {"name": "Test", "description": None, "presentation": valid_presentation()},
            "assets": {},
        }
        env.update(overrides)
        return env

    def test_unknown_format_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        envelope = self._minimal_envelope(format="something-else")

        resp = client.post(
            "/api/v1/report-letterheads/import",
            files={"file": ("bad.celuma", json.dumps(envelope).encode(), "application/json")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400
        assert "format" in resp.text.lower()

    def test_unknown_format_version_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        envelope = self._minimal_envelope(format_version=999)

        resp = client.post(
            "/api/v1/report-letterheads/import",
            files={"file": ("bad.celuma", json.dumps(envelope).encode(), "application/json")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400
        assert "version" in resp.text.lower()

    def test_corrupt_json_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.post(
            "/api/v1/report-letterheads/import",
            files={"file": ("bad.celuma", b"{not json", "application/json")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400
        assert "json" in resp.text.lower()

    def test_missing_required_field_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        envelope = self._minimal_envelope()
        del envelope["letterhead"]

        resp = client.post(
            "/api/v1/report-letterheads/import",
            files={"file": ("bad.celuma", json.dumps(envelope).encode(), "application/json")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400

    def test_logo_hash_mismatch_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        fake_bytes = b"not a real image but bytes"
        envelope = self._minimal_envelope(
            assets={
                "logo": {
                    "media_type": "image/png",
                    "sha256": "0" * 64,  # deliberately wrong
                    "data_base64": base64.b64encode(fake_bytes).decode(),
                }
            }
        )
        resp = client.post(
            "/api/v1/report-letterheads/import",
            files={"file": ("bad.celuma", json.dumps(envelope).encode(), "application/json")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400
        assert "integrity" in resp.text.lower() or "sha256" in resp.text.lower()

    def test_logo_corrupt_base64_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        envelope = self._minimal_envelope(
            assets={
                "logo": {
                    "media_type": "image/png",
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                    "data_base64": "%%%not-valid-base64%%%",
                }
            }
        )
        resp = client.post(
            "/api/v1/report-letterheads/import",
            files={"file": ("bad.celuma", json.dumps(envelope).encode(), "application/json")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400

    def test_oversized_file_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        huge_base64 = base64.b64encode(b"0" * (9 * 1024 * 1024)).decode()
        envelope = self._minimal_envelope(
            assets={
                "logo": {
                    "media_type": "image/png",
                    "sha256": hashlib.sha256(b"0" * (9 * 1024 * 1024)).hexdigest(),
                    "data_base64": huge_base64,
                }
            }
        )
        resp = client.post(
            "/api/v1/report-letterheads/import",
            files={"file": ("huge.celuma", json.dumps(envelope).encode(), "application/json")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400
        assert "size" in resp.text.lower()

    def test_missing_permission_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="viewer@t1.example", roles=("viewer",))
        envelope = self._minimal_envelope()

        resp = client.post(
            "/api/v1/report-letterheads/import",
            files={"file": ("x.celuma", json.dumps(envelope).encode(), "application/json")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 403


class TestLegacyExport:
    def test_legacy_export_is_deterministic_and_matches_frozen_constants(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")

        resp1 = client.get("/api/v1/report-letterheads/legacy/export", headers=auth_headers(user))
        resp2 = client.get("/api/v1/report-letterheads/legacy/export", headers=auth_headers(user))
        assert resp1.status_code == 200
        body1, body2 = resp1.json(), resp2.json()

        # Verbatim copy of legacy_letterhead_config.ts constants (see
        # legacy_letterhead_adapter.py module docstring).
        signer = body1["letterhead"]["presentation"]["signer"]
        assert signer["display_name"] == "Dra. Arisbeth Villanueva Pérez."
        assert signer["affiliation"] == "Centro Médico Nacional de Occidente IMSS. INCMNSZ"
        assert body1["letterhead"]["presentation"]["style"]["primary_color"] == "#002060"

        # Deterministic except for the exported_at timestamp.
        body1.pop("exported_at")
        body2.pop("exported_at")
        assert body1 == body2

    def test_legacy_export_never_contains_clinical_or_patient_data(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.get("/api/v1/report-letterheads/legacy/export", headers=auth_headers(user))
        raw = json.dumps(resp.json()).lower()
        for forbidden in ("patient", "paciente", "diagnos", "template"):
            assert forbidden not in raw

    def test_legacy_export_round_trips_through_import(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")

        exported = client.get(
            "/api/v1/report-letterheads/legacy/export", headers=auth_headers(user)
        ).json()
        imported = client.post(
            "/api/v1/report-letterheads/import",
            files={
                "file": (
                    "legacy-ambassador-letterhead.celuma",
                    json.dumps(exported).encode(),
                    "application/json",
                )
            },
            headers=auth_headers(user),
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["configuration"]["style"]["primary_color"] == "#002060"
