"""HTTP integration tests for Céluma 1.3.1 Block D — the three system
metadata base fields (plan ticket CEL-131-04; labelled CEL-131-07 in some
handoff notes — see docs/celuma-1.3.1/block-d/block-d-summary.md for the
ticket-id discrepancy):

    reception_date          earliest non-null Sample.received_at
    requesting_physician    the Order's requesting/referring physician
                             (reused existing key — see report_metadata.py)
    delivery_date           the report's signature date

All server-authoritative (`app/services/report_metadata.py`), overwriting
whatever the client submits for these keys — the same pattern Block A used
for `signatureMetadata`. Every test here uses a LEGACY report (no
`template_id`/`template_version_id`): these are ordinary base fields, not a
V2-only concept, and Legacy needs no letterhead/template setup, keeping the
tests focused on the metadata resolution itself.
"""
import uuid
from datetime import datetime

from app.models.report import Report
from app.models.requesting_physician import RequestingPhysician

from .conftest import make_pdf_bytes
from .factories import (
    assign_reviewer,
    auth_headers,
    create_branch,
    create_order,
    create_sample,
    create_tenant,
    create_user,
)

BASE_TEMPLATE = {
    "order_code": {"is_visible": True, "label": "Código de orden", "value": ""},
    "reception_date": {"is_visible": True, "label": "Fecha de recepción", "value": ""},
    "requesting_physician": {
        "is_visible": True, "label": "Médico solicitante", "value": "",
    },
    "delivery_date": {
        "is_visible": True, "label": "Fecha de entrega de resultados", "value": "",
    },
}


def _template_doc(base: dict) -> dict:
    """A Legacy report's effective template — the `template` field on
    `ReportCreate`, persisted as `Report.template`. It is what decides which
    system metadata fields this report declares."""
    return {
        "base": {k: dict(v) for k, v in base.items()},
        "sections": {},
        "base_order": list(base.keys()),
        "section_order": [],
    }


def _create_legacy_report(
    client,
    tenant,
    branch,
    order,
    user,
    *,
    base_overrides=None,
    submitted_base=None,
    declared_base=None,
):
    """Create a Legacy report.

    `submitted_base` is what the client puts in `report.base` (defaults to the
    full BASE_TEMPLATE); `declared_base` is what the report's own template
    declares (defaults to the same). Separating the two is what lets a test
    exercise "the client omitted a key the template declares".
    """
    base = {
        k: dict(v)
        for k, v in (BASE_TEMPLATE if submitted_base is None else submitted_base).items()
    }
    if base_overrides:
        for key, value in base_overrides.items():
            base[key]["value"] = value
    declared = BASE_TEMPLATE if declared_base is None else declared_base
    return client.post(
        "/api/v1/reports/",
        json={
            "tenant_id": str(tenant.id),
            "branch_id": str(branch.id),
            "order_id": str(order.id),
            "template": _template_doc(declared),
            "report": {
                "base": base,
                "sections": {},
                "base_order": list(base.keys()),
                "section_order": [],
            },
        },
        headers=auth_headers(user),
    )


def _full_base(client, user, report_id):
    """`create_report`'s own response (`ReportResponse`) carries no body —
    only `id`/`status`/`order_id`/`tenant_id`/`branch_id`. The body lives in
    `GET /reports/{id}/full`, the same envelope the editor reads."""
    resp = client.get(f"/api/v1/reports/{report_id}/full", headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()["report"]["report"]["base"]


def _requesting_physician(session, tenant, branch, *, full_name="Dra. Referente"):
    physician = RequestingPhysician(
        tenant_id=tenant.id,
        branch_id=branch.id,
        physician_code=f"RP-{uuid.uuid4().hex[:8]}",
        first_name=full_name.split()[-1],
        last_name="",
        full_name=full_name,
    )
    session.add(physician)
    session.commit()
    session.refresh(physician)
    return physician


# ---------------------------------------------------------------------------
# 1/13 — reception date: earliest non-null, never fabricated
# ---------------------------------------------------------------------------

class TestReceptionDate:
    def test_earliest_non_null_sample_received_at_wins(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")

        earliest = datetime(2026, 1, 5)
        later = datetime(2026, 1, 10)
        s1 = create_sample(session, tenant, branch, order, sample_code="S-1")
        s1.received_at = later
        s2 = create_sample(session, tenant, branch, order, sample_code="S-2")
        s2.received_at = earliest
        session.add(s1)
        session.add(s2)
        session.commit()

        resp = _create_legacy_report(client, tenant, branch, order, user)
        assert resp.status_code == 200, resp.text
        value = _full_base(client, user, resp.json()["id"])["reception_date"]["value"]
        assert value == f"{earliest.day}/{earliest.month}/{earliest.year}"

    def test_null_samples_are_ignored(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")

        real_date = datetime(2026, 2, 1)
        s1 = create_sample(session, tenant, branch, order, sample_code="S-1")
        s1.received_at = None
        s2 = create_sample(session, tenant, branch, order, sample_code="S-2")
        s2.received_at = real_date
        session.add(s1)
        session.add(s2)
        session.commit()

        resp = _create_legacy_report(client, tenant, branch, order, user)
        assert resp.status_code == 200, resp.text
        value = _full_base(client, user, resp.json()["id"])["reception_date"]["value"]
        assert value == f"{real_date.day}/{real_date.month}/{real_date.year}"

    def test_all_samples_null_is_unavailable_not_fabricated(self, client, session):
        """No `created_at`/`collected_at`/order-registration fallback — a
        report with nothing recorded must say so, not invent a date."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")
        sample = create_sample(session, tenant, branch, order)
        sample.received_at = None
        session.add(sample)
        session.commit()

        resp = _create_legacy_report(client, tenant, branch, order, user)
        assert resp.status_code == 200, resp.text
        assert _full_base(client, user, resp.json()["id"])["reception_date"]["value"] == ""

    def test_no_samples_at_all_is_unavailable(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")

        resp = _create_legacy_report(client, tenant, branch, order, user)
        assert resp.status_code == 200, resp.text
        assert _full_base(client, user, resp.json()["id"])["reception_date"]["value"] == ""


# ---------------------------------------------------------------------------
# 2/12 — requesting physician: catalog first, free text fallback, never null-crash
# ---------------------------------------------------------------------------

class TestRequestingPhysician:
    def test_catalog_physician_is_rendered(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")
        physician = _requesting_physician(session, tenant, branch)
        order.requesting_physician_id = physician.id
        session.add(order)
        session.commit()

        resp = _create_legacy_report(client, tenant, branch, order, user)
        assert resp.status_code == 200, resp.text
        assert (
            _full_base(client, user, resp.json()["id"])["requesting_physician"]["value"]
            == "Dra. Referente"
        )

    def test_legacy_free_text_fallback_when_no_catalog_link(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        order.requested_by = "Dr. Texto Libre"
        session.add(order)
        session.commit()
        user = create_user(session, tenant, email="author@t1.example")

        resp = _create_legacy_report(client, tenant, branch, order, user)
        assert resp.status_code == 200, resp.text
        assert (
            _full_base(client, user, resp.json()["id"])["requesting_physician"]["value"]
            == "Dr. Texto Libre"
        )

    def test_missing_physician_is_empty_not_a_crash(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")

        resp = _create_legacy_report(client, tenant, branch, order, user)
        assert resp.status_code == 200, resp.text
        assert _full_base(client, user, resp.json()["id"])["requesting_physician"]["value"] == ""

    def test_another_tenants_physician_cannot_leak(self, client, session):
        """Defensive: even if `requesting_physician_id` somehow pointed
        cross-tenant, the resolver treats it as absent rather than leaking
        the foreign name."""
        tenant = create_tenant(session)
        other_tenant = create_tenant(session, name="Other Lab")
        other_branch = create_branch(session, other_tenant)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        foreign_physician = _requesting_physician(
            session, other_tenant, other_branch, full_name="Dr. Ajeno"
        )
        order.requesting_physician_id = foreign_physician.id
        session.add(order)
        session.commit()
        user = create_user(session, tenant, email="author@t1.example")

        resp = _create_legacy_report(client, tenant, branch, order, user)
        assert resp.status_code == 200, resp.text
        assert _full_base(client, user, resp.json()["id"])["requesting_physician"]["value"] == ""


# ---------------------------------------------------------------------------
# 5 — frontend cannot forge any of the three
# ---------------------------------------------------------------------------

class TestFrontendCannotForgeMetadata:
    def test_forged_values_are_overwritten_at_creation(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")
        sample = create_sample(session, tenant, branch, order)
        sample.received_at = datetime(2026, 3, 1)
        session.add(sample)
        session.commit()

        resp = _create_legacy_report(
            client, tenant, branch, order, user,
            base_overrides={
                "reception_date": "01/01/1999",
                "requesting_physician": "Dr. Forjado",
                "delivery_date": "01/01/1999",
            },
        )
        assert resp.status_code == 200, resp.text
        base = _full_base(client, user, resp.json()["id"])
        assert base["reception_date"]["value"] == "1/3/2026"
        assert base["requesting_physician"]["value"] == ""
        assert base["delivery_date"]["value"] == ""

    def test_forged_values_are_overwritten_on_new_version(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")
        created = _create_legacy_report(client, tenant, branch, order, user)
        report_id = created.json()["id"]

        resp = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": {
                    "base": {
                        **{k: dict(v) for k, v in BASE_TEMPLATE.items()},
                        "delivery_date": {
                            "is_visible": True, "label": "x", "value": "FORGED",
                        },
                    },
                    "sections": {},
                    "base_order": list(BASE_TEMPLATE.keys()),
                    "section_order": [],
                },
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text

        full = client.get(f"/api/v1/reports/{report_id}/full", headers=auth_headers(user))
        assert full.json()["report"]["report"]["base"]["delivery_date"]["value"] == ""


# ---------------------------------------------------------------------------
# 3/10 — delivery date: unavailable until signed, then frozen
# ---------------------------------------------------------------------------

class TestDeliveryDate:
    def test_unsigned_report_has_no_delivery_date(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")

        resp = _create_legacy_report(client, tenant, branch, order, user)
        assert _full_base(client, user, resp.json()["id"])["delivery_date"]["value"] == ""

    def test_approval_alone_does_not_populate_it(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        author = create_user(
            session, tenant, email="author@t1.example", roles=("pathologist",)
        )
        reviewer = create_user(
            session, tenant, email="rev@t1.example", roles=("reviewer",)
        )
        created = _create_legacy_report(client, tenant, branch, order, author)
        report_id = created.json()["id"]
        report = session.get(Report, report_id)
        assign_reviewer(session, order, reviewer, report=report)

        client.post(
            f"/api/v1/reports/{report_id}/submit", json={}, headers=auth_headers(author)
        )
        client.post(
            f"/api/v1/reports/{report_id}/approve",
            json={},
            headers=auth_headers(reviewer),
        )

        full = client.get(f"/api/v1/reports/{report_id}/full", headers=auth_headers(reviewer))
        assert full.json()["report"]["report"]["base"]["delivery_date"]["value"] == ""

    def test_signing_populates_the_delivery_date_and_it_stays_stable(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        author = create_user(
            session, tenant, email="author@t1.example", roles=("pathologist",)
        )
        reviewer = create_user(
            session, tenant, email="rev@t1.example", roles=("reviewer",)
        )
        created = _create_legacy_report(client, tenant, branch, order, author)
        report_id = created.json()["id"]
        report = session.get(Report, report_id)
        assign_reviewer(session, order, reviewer, report=report)
        client.post(
            f"/api/v1/reports/{report_id}/submit", json={}, headers=auth_headers(author)
        )
        client.post(
            f"/api/v1/reports/{report_id}/approve",
            json={},
            headers=auth_headers(reviewer),
        )

        before_sign = datetime.utcnow()
        stub_pdf_render.succeed(make_pdf_bytes(1))
        sign = client.post(
            f"/api/v1/reports/{report_id}/sign-and-publish",
            json={},
            headers=auth_headers(reviewer),
        )
        assert sign.status_code == 200, sign.text

        full = client.get(f"/api/v1/reports/{report_id}/full", headers=auth_headers(reviewer))
        value = full.json()["report"]["report"]["base"]["delivery_date"]["value"]
        expected = f"{before_sign.day}/{before_sign.month}/{before_sign.year}"
        assert value == expected

        # Reading it again (PUBLISHED; immutable) must not change it.
        again = client.get(f"/api/v1/reports/{report_id}/full", headers=auth_headers(reviewer))
        assert (
            again.json()["report"]["report"]["base"]["delivery_date"]["value"] == value
        )


# ---------------------------------------------------------------------------
# 9/14/15 — reopened DRAFT recomputes live fields; published history is frozen
# ---------------------------------------------------------------------------

class TestReopenedAndPublishedDeterminism:
    def test_reopened_draft_recomputes_reception_and_physician_on_resubmit(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        author = create_user(
            session, tenant, email="author@t1.example", roles=("pathologist",)
        )
        reviewer = create_user(
            session, tenant, email="rev@t1.example", roles=("reviewer",)
        )
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        sample = create_sample(session, tenant, branch, order)
        sample.received_at = datetime(2026, 4, 1)
        session.add(sample)
        session.commit()

        created = _create_legacy_report(client, tenant, branch, order, author)
        report_id = created.json()["id"]
        report = session.get(Report, report_id)
        assign_reviewer(session, order, reviewer, report=report)
        client.post(
            f"/api/v1/reports/{report_id}/submit", json={}, headers=auth_headers(author)
        )
        client.post(
            f"/api/v1/reports/{report_id}/approve",
            json={},
            headers=auth_headers(reviewer),
        )

        reopen = client.post(
            f"/api/v1/reports/{report_id}/reopen", json={}, headers=auth_headers(admin)
        )
        assert reopen.status_code == 200, reopen.text

        # The Order's reception date changes before the author edits again.
        sample.received_at = datetime(2025, 12, 1)
        session.add(sample)
        session.commit()

        resp = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": {
                    "base": {k: dict(v) for k, v in BASE_TEMPLATE.items()},
                    "sections": {},
                    "base_order": list(BASE_TEMPLATE.keys()),
                    "section_order": [],
                },
            },
            headers=auth_headers(author),
        )
        assert resp.status_code == 200, resp.text

        full = client.get(f"/api/v1/reports/{report_id}/full", headers=auth_headers(author))
        reception = full.json()["report"]["report"]["base"]["reception_date"]["value"]
        assert reception == "1/12/2025"

    def test_changing_the_live_physician_after_publication_does_not_mutate_history(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        author = create_user(
            session, tenant, email="author@t1.example", roles=("pathologist",)
        )
        reviewer = create_user(
            session, tenant, email="rev@t1.example", roles=("reviewer",)
        )
        original_physician = _requesting_physician(
            session, tenant, branch, full_name="Dr. Original"
        )
        order.requesting_physician_id = original_physician.id
        session.add(order)
        session.commit()

        created = _create_legacy_report(client, tenant, branch, order, author)
        report_id = created.json()["id"]
        report = session.get(Report, report_id)
        assign_reviewer(session, order, reviewer, report=report)
        client.post(
            f"/api/v1/reports/{report_id}/submit", json={}, headers=auth_headers(author)
        )
        client.post(
            f"/api/v1/reports/{report_id}/approve",
            json={},
            headers=auth_headers(reviewer),
        )
        stub_pdf_render.succeed(make_pdf_bytes(1))
        client.post(
            f"/api/v1/reports/{report_id}/sign-and-publish",
            json={},
            headers=auth_headers(reviewer),
        )

        new_physician = _requesting_physician(
            session, tenant, branch, full_name="Dr. Sustituto"
        )
        order.requesting_physician_id = new_physician.id
        session.add(order)
        session.commit()

        full = client.get(f"/api/v1/reports/{report_id}/full", headers=auth_headers(reviewer))
        assert (
            full.json()["report"]["report"]["base"]["requesting_physician"]["value"]
            == "Dr. Original"
        )


# ---------------------------------------------------------------------------
# Server-guaranteed presence — a client cannot suppress a declared system
# field by omitting its key
# ---------------------------------------------------------------------------

class TestDeclaredFieldsCannotBeSuppressedByOmission:
    def _order_with_sample(self, session, tenant, branch, *, received_at=None):
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        sample.received_at = received_at or datetime(2026, 5, 4)
        session.add(sample)
        session.commit()
        return order

    def test_omitted_fields_are_created_when_the_template_declares_them(
        self, client, session
    ):
        """The gap this closes: before, a body that simply left the keys out
        produced a report with no reception date and no physician, with the
        backend none the wiser."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = self._order_with_sample(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")
        physician = _requesting_physician(session, tenant, branch)
        order.requesting_physician_id = physician.id
        session.add(order)
        session.commit()

        resp = _create_legacy_report(
            client,
            tenant,
            branch,
            order,
            user,
            # The client sends ONLY order_code — every system metadata key is
            # absent from the submitted body.
            submitted_base={"order_code": dict(BASE_TEMPLATE["order_code"])},
            declared_base=BASE_TEMPLATE,
        )
        assert resp.status_code == 200, resp.text

        base = _full_base(client, user, resp.json()["id"])
        assert base["reception_date"]["value"] == "4/5/2026"
        assert base["requesting_physician"]["value"] == "Dra. Referente"
        assert base["delivery_date"]["value"] == ""

    def test_created_fields_take_the_templates_label_and_visibility(
        self, client, session
    ):
        """Rebuilt from the template's own declaration, so the report shows
        what that template configured — not a hardcoded default."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = self._order_with_sample(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")
        declared = {
            "order_code": dict(BASE_TEMPLATE["order_code"]),
            "reception_date": {
                "is_visible": True, "label": "Fecha de ingreso", "value": "",
            },
        }

        resp = _create_legacy_report(
            client, tenant, branch, order, user,
            submitted_base={"order_code": dict(BASE_TEMPLATE["order_code"])},
            declared_base=declared,
        )
        assert resp.status_code == 200, resp.text

        base = _full_base(client, user, resp.json()["id"])
        assert base["reception_date"]["label"] == "Fecha de ingreso"
        assert base["reception_date"]["is_visible"] is True
        assert base["reception_date"]["value"] == "4/5/2026"

    def test_a_created_field_joins_base_order_exactly_once(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = self._order_with_sample(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")

        resp = _create_legacy_report(
            client, tenant, branch, order, user,
            submitted_base={"order_code": dict(BASE_TEMPLATE["order_code"])},
            declared_base=BASE_TEMPLATE,
        )
        assert resp.status_code == 200, resp.text

        full = client.get(
            f"/api/v1/reports/{resp.json()['id']}/full", headers=auth_headers(user)
        )
        base_order = full.json()["report"]["report"]["base_order"]
        assert len(base_order) == len(set(base_order))
        for key in ("requesting_physician", "reception_date", "delivery_date"):
            assert base_order.count(key) == 1

    def test_a_field_the_template_does_not_declare_is_never_injected(
        self, client, session
    ):
        """The converse, and the reason the rule is keyed off the template: a
        report whose frozen template predates 1.3.1 must not silently acquire
        a field it was never authored against."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = self._order_with_sample(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")
        pre_1_3_1 = {"order_code": dict(BASE_TEMPLATE["order_code"])}

        resp = _create_legacy_report(
            client, tenant, branch, order, user,
            submitted_base=pre_1_3_1,
            declared_base=pre_1_3_1,
        )
        assert resp.status_code == 200, resp.text

        base = _full_base(client, user, resp.json()["id"])
        assert set(base) == {"order_code"}

    def test_omission_is_also_refused_on_a_later_content_save(
        self, client, session
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = self._order_with_sample(session, tenant, branch)
        user = create_user(session, tenant, email="author@t1.example")
        created = _create_legacy_report(client, tenant, branch, order, user)
        report_id = created.json()["id"]

        resp = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": {
                    "base": {"order_code": dict(BASE_TEMPLATE["order_code"])},
                    "sections": {},
                    "base_order": ["order_code"],
                    "section_order": [],
                },
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text

        base = _full_base(client, user, report_id)
        assert base["reception_date"]["value"] == "4/5/2026"
        assert "delivery_date" in base


# ---------------------------------------------------------------------------
# The same rule on the V2 path, where the declaring template is the report's
# own FROZEN rendering snapshot
# ---------------------------------------------------------------------------

class TestV2ReportsFollowTheSameRule:
    def _v2_lab(self, session, *, template_json):
        from app.models.report import ReportTemplate
        from app.models.study_type import StudyType

        from .factories import (
            create_letterhead,
            create_letterhead_version,
            valid_presentation,
        )

        tenant = create_tenant(session, name="Block D V2 Lab", reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        template = ReportTemplate(
            tenant_id=tenant.id, name="Plantilla", template_json=template_json
        )
        session.add(template)
        session.flush()
        study_type = StudyType(
            tenant_id=tenant.id,
            code=f"BIO-{uuid.uuid4().hex[:6]}",
            name="Biopsia",
            default_report_template_id=template.id,
        )
        session.add(study_type)
        letterhead = create_letterhead(session, tenant, name="Predeterminado")
        letterhead.is_default = True
        session.add(letterhead)
        create_letterhead_version(
            session, tenant, letterhead, status="ACTIVE",
            configuration=valid_presentation(),
        )
        session.commit()
        session.refresh(template)
        return tenant, branch, template

    def _create_v2(self, client, session, tenant, branch, template, order, user, base):
        from app.services.report_template_hash import hash_clinical_template_block

        session.refresh(template)
        return client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
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

    def test_a_v2_report_gets_declared_fields_the_client_omitted(
        self, client, session
    ):
        """For V2 the declaring template is the snapshot the backend froze
        from `ReportTemplate.template_json` — never the client's copy."""
        template_json = {
            "base": {k: dict(v) for k, v in BASE_TEMPLATE.items()},
            "sections": {},
            "base_order": list(BASE_TEMPLATE.keys()),
            "section_order": [],
        }
        tenant, branch, template = self._v2_lab(session, template_json=template_json)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        sample.received_at = datetime(2026, 6, 7)
        session.add(sample)
        session.commit()
        user = create_user(session, tenant, email="author@v2.example")

        resp = self._create_v2(
            client, session, tenant, branch, template, order, user, base={}
        )
        assert resp.status_code == 200, resp.text

        base = _full_base(client, user, resp.json()["id"])
        assert base["reception_date"]["value"] == "7/6/2026"
        assert base["delivery_date"]["value"] == ""

    def test_a_v2_report_whose_snapshot_omits_the_fields_is_not_rewritten(
        self, client, session
    ):
        template_json = {
            "base": {"order_code": dict(BASE_TEMPLATE["order_code"])},
            "sections": {},
            "base_order": ["order_code"],
            "section_order": [],
        }
        tenant, branch, template = self._v2_lab(session, template_json=template_json)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="author@v2.example")

        resp = self._create_v2(
            client, session, tenant, branch, template, order, user, base={}
        )
        assert resp.status_code == 200, resp.text

        base = _full_base(client, user, resp.json()["id"])
        assert "reception_date" not in base
        assert "delivery_date" not in base

    def test_the_frozen_snapshot_itself_is_never_mutated(self, client, session):
        """Block C's invariant: `rendering_snapshot` is the report's frozen
        structure. Block D writes values into `base`, never into the
        snapshot."""
        template_json = {
            "base": {k: dict(v) for k, v in BASE_TEMPLATE.items()},
            "sections": {},
            "base_order": list(BASE_TEMPLATE.keys()),
            "section_order": [],
        }
        tenant, branch, template = self._v2_lab(session, template_json=template_json)
        order = create_order(session, tenant, branch)
        sample = create_sample(session, tenant, branch, order)
        sample.received_at = datetime(2026, 6, 7)
        session.add(sample)
        session.commit()
        user = create_user(session, tenant, email="author@v2.example")

        resp = self._create_v2(
            client, session, tenant, branch, template, order, user, base={}
        )
        assert resp.status_code == 200, resp.text

        full = client.get(
            f"/api/v1/reports/{resp.json()['id']}/full", headers=auth_headers(user)
        )
        snapshot = full.json()["report"]["report"]["rendering_snapshot"]
        assert snapshot["template"]["base"] == template_json["base"]
        assert snapshot["template"]["base"]["reception_date"]["value"] == ""
