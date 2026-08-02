"""HTTP integration tests for the ReportLetterhead/ReportLetterheadVersion
endpoints — post-Fase-2 remediation, R6/R15."""
from app.models.report_letterhead import ReportLetterhead
from app.models.report_letterhead_version import ReportLetterheadVersion

from .factories import (
    auth_headers,
    create_letterhead,
    create_letterhead_version,
    create_tenant,
    create_user,
    valid_presentation,
)


class TestLetterheadCRUD:
    def test_create_list_get_update(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.post(
            "/api/v1/report-letterheads/",
            json={"name": "Membrete General", "description": "Uso general"},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        letterhead_id = resp.json()["id"]
        assert resp.json()["is_default"] is False
        assert resp.json()["is_active"] is True

        listed = client.get("/api/v1/report-letterheads/", headers=auth_headers(user))
        assert listed.status_code == 200
        assert any(l["id"] == letterhead_id for l in listed.json()["letterheads"])

        got = client.get(f"/api/v1/report-letterheads/{letterhead_id}", headers=auth_headers(user))
        assert got.status_code == 200
        assert got.json()["name"] == "Membrete General"

        updated = client.put(
            f"/api/v1/report-letterheads/{letterhead_id}",
            json={"name": "Membrete Renombrado"},
            headers=auth_headers(user),
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Membrete Renombrado"

    def test_soft_delete_deactivates(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant)

        resp = client.delete(f"/api/v1/report-letterheads/{letterhead.id}", headers=auth_headers(user))
        assert resp.status_code == 200

        session.refresh(letterhead)
        assert letterhead.is_active is False

    def test_hard_delete_allowed_when_versions_exist_but_nothing_references_it(
        self, client, session
    ):
        """Tercera remediación: tener versiones ya NO bloquea el borrado.
        Bloqueaba a TODO membrete que se hubiera guardado alguna vez, es
        decir a todos — el problema D del brief. Lo que bloquea ahora son
        las referencias reales (default, preferencia de plantilla, reportes),
        cubiertas en test_letterhead_remediation3.py."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant)
        create_letterhead_version(session, tenant, letterhead)

        resp = client.delete(
            f"/api/v1/report-letterheads/{letterhead.id}?hard_delete=true",
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text

    def test_cross_tenant_letterhead_is_not_found(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        user_b = create_user(session, tenant_b, email="admin@b.example")
        letterhead_a = create_letterhead(session, tenant_a)

        resp = client.get(f"/api/v1/report-letterheads/{letterhead_a.id}", headers=auth_headers(user_b))
        assert resp.status_code == 404

    def test_missing_permission_is_rejected(self, client, session):
        tenant = create_tenant(session)
        # "viewer" role only holds reports:read, not reports:manage_templates.
        user = create_user(session, tenant, email="viewer@t1.example", roles=("viewer",))

        resp = client.post(
            "/api/v1/report-letterheads/",
            json={"name": "Should Fail"},
            headers=auth_headers(user),
        )
        assert resp.status_code == 403


class TestLetterheadVersions:
    def test_publish_activate_archive_lifecycle(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant)

        published = client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/versions",
            json={"configuration": valid_presentation()},
            headers=auth_headers(user),
        )
        assert published.status_code == 200, published.text
        version_id = published.json()["id"]
        assert published.json()["version_number"] == 1
        assert published.json()["status"] == "PUBLISHED"
        assert published.json()["configuration"]["style"]["primary_color"] == "#336699"

        activated = client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version_id}/activate",
            headers=auth_headers(user),
        )
        assert activated.status_code == 200
        assert activated.json()["status"] == "ACTIVE"

        archive_active_blocked = client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{version_id}/archive",
            headers=auth_headers(user),
        )
        assert archive_active_blocked.status_code == 409

    def test_second_version_increments_number(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant)

        for _ in range(2):
            resp = client.post(
                f"/api/v1/report-letterheads/{letterhead.id}/versions",
                json={"configuration": valid_presentation()},
                headers=auth_headers(user),
            )
            assert resp.status_code == 200

        listed = client.get(
            f"/api/v1/report-letterheads/{letterhead.id}/versions", headers=auth_headers(user)
        )
        numbers = sorted(v["version_number"] for v in listed.json()["versions"])
        assert numbers == [1, 2]

    def test_only_one_active_version_at_a_time(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant)
        v1 = create_letterhead_version(session, tenant, letterhead, version_number=1)
        v2 = create_letterhead_version(session, tenant, letterhead, version_number=2)

        client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{v1.id}/activate",
            headers=auth_headers(user),
        )
        client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/versions/{v2.id}/activate",
            headers=auth_headers(user),
        )

        session.refresh(v1)
        session.refresh(v2)
        assert v1.status == "PUBLISHED"
        assert v2.status == "ACTIVE"

    def test_rejects_clinical_template_key_in_configuration(self, client, session):
        """A letterhead version's configuration must never carry a
        `template` key — that would smuggle clinical structure into a
        presentation-only contract (extra='forbid' on
        ReportPresentationSnapshotV2 rejects it)."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        letterhead = create_letterhead(session, tenant)

        payload = valid_presentation()
        payload["template"] = {"base": {}, "sections": {}}
        resp = client.post(
            f"/api/v1/report-letterheads/{letterhead.id}/versions",
            json={"configuration": payload},
            headers=auth_headers(user),
        )
        assert resp.status_code == 422


class TestTenantDefaultLetterhead:
    def test_set_default_flips_previous(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        lh1 = create_letterhead(session, tenant, name="Membrete 1")
        lh2 = create_letterhead(session, tenant, name="Membrete 2")
        # Tercera remediación: solo un membrete con configuración guardada
        # (versión ACTIVE) puede ser predeterminado — si no, la resolución no
        # podría resolverlo y V2 quedaría bloqueado sin que nadie lo pidiera.
        create_letterhead_version(session, tenant, lh1, status="ACTIVE")
        create_letterhead_version(session, tenant, lh2, status="ACTIVE")

        r1 = client.post(f"/api/v1/report-letterheads/{lh1.id}/default", headers=auth_headers(user))
        assert r1.status_code == 200
        assert r1.json()["is_default"] is True

        r2 = client.post(f"/api/v1/report-letterheads/{lh2.id}/default", headers=auth_headers(user))
        assert r2.status_code == 200
        assert r2.json()["is_default"] is True

        session.refresh(lh1)
        session.refresh(lh2)
        assert lh1.is_default is False
        assert lh2.is_default is True

    def test_default_does_not_affect_other_tenants(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        user_a = create_user(session, tenant_a, email="admin@a.example")
        lh_a = create_letterhead(session, tenant_a)
        lh_b = create_letterhead(session, tenant_b, name="Other tenant's letterhead")

        client.post(f"/api/v1/report-letterheads/{lh_a.id}/default", headers=auth_headers(user_a))

        session.refresh(lh_b)
        assert lh_b.is_default is False


class TestLetterheadDuplicate:
    def test_duplicate_clones_active_version(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        source = create_letterhead(session, tenant, name="Original")
        version = create_letterhead_version(session, tenant, source, status="ACTIVE")

        resp = client.post(
            f"/api/v1/report-letterheads/{source.id}/duplicate", headers=auth_headers(user)
        )
        assert resp.status_code == 200, resp.text
        new_id = resp.json()["id"]
        assert new_id != str(source.id)

        versions = client.get(
            f"/api/v1/report-letterheads/{new_id}/versions", headers=auth_headers(user)
        ).json()["versions"]
        assert len(versions) == 1
        assert versions[0]["version_number"] == 1

        detail = client.get(
            f"/api/v1/report-letterheads/{new_id}/versions/{versions[0]['id']}",
            headers=auth_headers(user),
        ).json()
        assert detail["configuration"] == version.configuration
