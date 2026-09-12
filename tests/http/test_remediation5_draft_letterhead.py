"""Fifth post-Phase-2 remediation — letterhead change while the report
is still DRAFT (Observation A).

The immutability boundary is no longer "the report already has an id" and
becomes "the report has left DRAFT". These tests pin both halves of the
contract:

  * what DOES change: `ReportVersion.letterhead_version_id` and
    `rendering_snapshot.presentation`;
  * what NEVER changes: clinical template, `template_version_id`, base
    fields, sections, clinical values, and images.

See draft-letterhead-change-contract.md and
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
    """A V2 tenant with two usable letterheads and an already-persisted
    DRAFT report — the exact Observation A scenario."""
    tenant = create_tenant(session, reports_v2_enabled=True)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    # 1.3.1 Block A: this fixture's user acts as the report's reviewer in
    # `TestLetterheadFreezeAtReview` (it assigns itself a `ReportReview` and
    # calls request-changes). `superuser` holds `reports:approve` but not the
    # `reviewer` role, and the role is now required — so the role is explicit
    # here rather than implied by the permission.
    user = create_user(
        session, tenant, email="admin@t1.example", roles=("superuser", "reviewer")
    )
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


def _enter_review(client, session, w):
    """Move the report to IN_REVIEW with `w["user"]` as its assigned reviewer.

    Céluma 1.3.1 Block A: this is now the precondition for every letterhead
    change below. The fixture user holds `("superuser", "reviewer")`, so it
    satisfies the role and capability halves; the `ReportReview` row is the
    assignment half.
    """
    from sqlmodel import select

    from app.models.enums import ReviewStatus

    # Idempotent: after `request-changes` the existing row is REJECTED, and a
    # partial unique index forbids a second PENDING row for the same
    # (tenant, order, reviewer). Re-arm the existing assignment instead.
    existing = session.exec(
        select(ReportReview).where(
            ReportReview.order_id == w["order"].id,
            ReportReview.reviewer_user_id == w["user"].id,
        )
    ).first()
    if existing is None:
        existing = ReportReview(
            tenant_id=w["tenant"].id,
            branch_id=w["branch"].id,
            order_id=w["order"].id,
            reviewer_user_id=w["user"].id,
        )
    existing.status = ReviewStatus.PENDING
    existing.decision_at = None
    session.add(existing)
    session.commit()
    resp = client.post(
        f"/api/v1/reports/{w['report_id']}/submit", json={}, headers=w["headers"]
    )
    assert resp.status_code == 200, resp.text


def _change_letterhead(client, w, letterhead_version_id):
    """The reviewer's letterhead change — the narrow presentation route."""
    return client.patch(
        f"/api/v1/reports/{w['report_id']}/presentation",
        json={"letterhead_version_id": str(letterhead_version_id)},
        headers=w["headers"],
    )


@pytest.fixture
def in_review(client, session, v2_world):
    """`v2_world`, submitted, with the fixture user as assigned reviewer."""
    _enter_review(client, session, v2_world)
    return v2_world


class TestReviewerLetterheadChange:
    """Céluma 1.3.1 Block A SUPERSEDED remediation 5's boundary.

    Remediation 5 made the letterhead editable while DRAFT and frozen at
    submission, on the premise that it belongs to whoever is writing the
    report. The 1.3.1 product contract is that it belongs to the assigned
    REVIEWER — the letterhead decides what the final clinical document looks
    like — so the window moved to IN_REVIEW and the actor moved to the
    reviewer.

    Everything remediation 5 guaranteed ABOUT a change is unchanged and still
    pinned here: what changes is `ReportVersion.letterhead_version_id` and
    `rendering_snapshot.presentation`; what never changes is the clinical
    template, `template_version_id`, base fields, sections, values and images.
    """

    def test_new_report_resolves_default_letterhead(self, client, v2_world):
        """Creation still resolves the tenant default server-side. Nobody
        picks a letterhead at creation time — not even the reviewer."""
        detail = _get(client, v2_world)
        assert detail["schema_version"] == 2
        assert detail["letterhead_version_id"] == str(v2_world["default_version"].id)

    def test_the_assigned_reviewer_can_change_it(self, client, in_review):
        resp = _change_letterhead(client, in_review, in_review["other_version"].id)
        assert resp.status_code == 200, resp.text
        assert resp.json()["letterhead_version_id"] == str(
            in_review["other_version"].id
        )

        detail = _get(client, in_review)
        assert detail["letterhead_version_id"] == str(in_review["other_version"].id)
        presentation = detail["report"]["rendering_snapshot"]["presentation"]
        assert presentation["header"]["institution_name"] == "Laboratorio Nefropatología"

    def test_can_change_letterhead_several_times(self, client, in_review):
        for expected in (
            in_review["other_version"].id,
            in_review["default_version"].id,
            in_review["other_version"].id,
        ):
            resp = _change_letterhead(client, in_review, expected)
            assert resp.status_code == 200, resp.text
            assert _get(client, in_review)["letterhead_version_id"] == str(expected)

    def test_change_preserves_clinical_content(self, client, session, v2_world):
        content = _content("Carcinoma ductal infiltrante")
        assert _save_draft(client, v2_world, content=content).status_code == 200
        _enter_review(client, session, v2_world)

        resp = _change_letterhead(client, v2_world, v2_world["other_version"].id)
        assert resp.status_code == 200, resp.text

        detail = _get(client, v2_world)
        assert (
            detail["report"]["base"]["diagnosis"]["value"] == "Carcinoma ductal infiltrante"
        )

    def test_change_preserves_images(self, client, session, v2_world):
        images = [{"id": "img-1", "url": "https://example.test/a.png", "caption": "H&E 40x"}]
        content = _content("Benigno", images=images)
        assert _save_draft(client, v2_world, content=content).status_code == 200
        _enter_review(client, session, v2_world)

        resp = _change_letterhead(client, v2_world, v2_world["other_version"].id)
        assert resp.status_code == 200, resp.text

        detail = _get(client, v2_world)
        assert detail["report"]["sections"]["galeria"]["content"] == images

    def test_change_replaces_only_presentation(self, client, in_review):
        """Central invariant of §3.3, carried over verbatim: the snapshot
        `template` block and `template_version_id` remain intact."""
        before = _get(client, in_review)
        template_before = before["report"]["rendering_snapshot"]["template"]
        template_version_before = before["template_version_id"]

        resp = _change_letterhead(client, in_review, in_review["other_version"].id)
        assert resp.status_code == 200, resp.text

        after = _get(client, in_review)
        assert after["report"]["rendering_snapshot"]["template"] == template_before
        assert after["template_version_id"] == template_version_before
        assert (
            after["report"]["rendering_snapshot"]["presentation"]
            != before["report"]["rendering_snapshot"]["presentation"]
        )

    def test_change_resolves_both_logos(self, client, session, in_review):
        """§3.4.5: after the change, `resolved_resources` is recomputed from
        the NEW letterhead — header and footer."""
        from .factories import create_storage_object

        header_logo = create_storage_object(
            session, key="logos/header-neph.png", tenant=in_review["tenant"]
        )
        footer_logo = create_storage_object(
            session, key="logos/footer-neph.png", tenant=in_review["tenant"]
        )
        presentation = _alt_presentation()
        presentation["header"]["logo_storage_id"] = str(header_logo.id)
        presentation["footer"] = dict(presentation["footer"])
        presentation["footer"]["logo_storage_id"] = str(footer_logo.id)

        logo_lh = create_letterhead(session, in_review["tenant"], name="Membrete con logos")
        logo_version = create_letterhead_version(
            session, in_review["tenant"], logo_lh, status="ACTIVE", configuration=presentation
        )

        resp = _change_letterhead(client, in_review, logo_version.id)
        assert resp.status_code == 200, resp.text

        detail = _get(client, in_review)
        resources = detail["resolved_resources"]
        assert resources is not None
        assert "header-neph.png" in resources["header_logo_url"]
        assert "footer-neph.png" in resources["footer_logo_url"]

    def test_content_only_save_keeps_letterhead(self, client, session, v2_world):
        """C9/R regression: a content save remains pure carry-forward — now
        doubly so, since the content path can no longer change the letterhead
        at all."""
        _enter_review(client, session, v2_world)
        assert _change_letterhead(
            client, v2_world, v2_world["other_version"].id
        ).status_code == 200

        report = session.get(Report, v2_world["report_id"])
        report.status = ReportStatus.DRAFT
        session.add(report)
        session.commit()

        assert _save_draft(client, v2_world).status_code == 200
        assert _get(client, v2_world)["letterhead_version_id"] == str(
            v2_world["other_version"].id
        )

    def test_change_is_audited(self, client, session, in_review):
        from sqlmodel import select

        from app.models.audit import AuditLog

        assert _change_letterhead(
            client, in_review, in_review["other_version"].id
        ).status_code == 200

        entries = session.exec(
            select(AuditLog).where(AuditLog.action == "REPORT.PRESENTATION_UPDATE")
        ).all()
        assert len(entries) == 1
        assert entries[0].new_values["letterhead_version_id"] == str(
            in_review["other_version"].id
        )


class TestReviewerLetterheadValidation:
    """The validation chain is unchanged — a reviewer may not select a
    letterhead an author could not have."""

    def test_cross_tenant_letterhead_is_rejected(self, client, session, in_review):
        other_tenant = create_tenant(session, name="Otro laboratorio", reports_v2_enabled=True)
        foreign_lh = create_letterhead(session, other_tenant, name="Ajeno")
        foreign_version = create_letterhead_version(
            session, other_tenant, foreign_lh, status="ACTIVE"
        )

        resp = _change_letterhead(client, in_review, foreign_version.id)
        # 404, never 403: a foreign id is never confirmed to exist.
        assert resp.status_code == 404, resp.text
        assert _get(client, in_review)["letterhead_version_id"] == str(
            in_review["default_version"].id
        )

    def test_archived_letterhead_version_is_rejected(self, client, session, in_review):
        archived_lh = create_letterhead(session, in_review["tenant"], name="Archivado")
        archived_version = create_letterhead_version(
            session, in_review["tenant"], archived_lh, status="ARCHIVED"
        )

        resp = _change_letterhead(client, in_review, archived_version.id)
        assert resp.status_code == 409, resp.text

    def test_inactive_letterhead_is_rejected(self, client, session, in_review):
        inactive_lh = create_letterhead(session, in_review["tenant"], name="Desactivado")
        inactive_version = create_letterhead_version(
            session, in_review["tenant"], inactive_lh, status="ACTIVE"
        )
        inactive_lh.is_active = False
        session.add(inactive_lh)
        session.commit()

        resp = _change_letterhead(client, in_review, inactive_version.id)
        assert resp.status_code == 409, resp.text

    def test_unknown_letterhead_version_is_rejected(self, client, in_review):
        import uuid

        resp = _change_letterhead(client, in_review, uuid.uuid4())
        assert resp.status_code == 404, resp.text


class TestTheAuthorHasNoLetterheadPath:
    """The other half of the moved boundary: the content path no longer
    changes the letterhead in ANY state, so the author cannot reach it.

    Rejected rather than ignored — a letterhead is chosen explicitly, so
    silently keeping the old one would leave the caller believing the change
    landed.
    """

    def test_a_draft_save_cannot_change_it(self, client, v2_world):
        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        )
        assert resp.status_code == 403, resp.text
        assert "revisor" in resp.json()["detail"].lower()
        assert _get(client, v2_world)["letterhead_version_id"] == str(
            v2_world["default_version"].id
        )

    def test_an_in_review_content_save_cannot_change_it_either(
        self, client, session, v2_world
    ):
        _enter_review(client, session, v2_world)
        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["other_version"].id)
        )
        assert resp.status_code == 403, resp.text

    def test_echoing_the_same_letterhead_is_not_a_change(self, client, v2_world):
        """A content save that resends the letterhead it already has is not a
        change and must not be rejected — otherwise the editor could not
        resend its own envelope."""
        resp = _save_draft(
            client, v2_world, letterhead_version_id=str(v2_world["default_version"].id)
        )
        assert resp.status_code == 200, resp.text


class TestPresentationFreezesAtApproval:
    """1.3.1 A5: the freeze point moved from submission to APPROVAL, because
    the reviewer needs the IN_REVIEW window to do this work at all."""

    def _force_status(self, session, v2_world, status):
        report = session.get(Report, v2_world["report_id"])
        report.status = status
        session.add(report)
        session.commit()

    def test_draft_is_not_the_reviewers_window(self, client, session, v2_world):
        """Before submission there is nothing to review; the reviewer's
        authority starts when the report reaches them."""
        review = ReportReview(
            tenant_id=v2_world["tenant"].id,
            branch_id=v2_world["branch"].id,
            order_id=v2_world["order"].id,
            reviewer_user_id=v2_world["user"].id,
        )
        session.add(review)
        session.commit()

        resp = _change_letterhead(client, v2_world, v2_world["other_version"].id)
        assert resp.status_code == 409, resp.text

    def test_approved_freezes_it(self, client, session, in_review):
        self._force_status(session, in_review, ReportStatus.APPROVED)
        resp = _change_letterhead(client, in_review, in_review["other_version"].id)
        assert resp.status_code == 409, resp.text

    def test_published_freezes_it(self, client, session, in_review):
        self._force_status(session, in_review, ReportStatus.PUBLISHED)
        resp = _change_letterhead(client, in_review, in_review["other_version"].id)
        assert resp.status_code == 409, resp.text

    def test_returned_to_draft_then_resubmitted_is_changeable_again(
        self, client, session, v2_world
    ):
        """§3.6 carried forward to the new owner: `request-changes` returns the
        report to DRAFT on the SAME editable version, so after a resubmission
        the reviewer can change the letterhead again."""
        _enter_review(client, session, v2_world)
        resp = client.post(
            f"/api/v1/reports/{v2_world['report_id']}/request-changes",
            json={"comment": "Ajusta el diagnóstico"},
            headers=v2_world["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == ReportStatus.DRAFT

        _enter_review(client, session, v2_world)
        resp = _change_letterhead(client, v2_world, v2_world["other_version"].id)
        assert resp.status_code == 200, resp.text
        assert _get(client, v2_world)["letterhead_version_id"] == str(
            v2_world["other_version"].id
        )

    def test_submit_succeeds_with_the_resolved_letterhead(self, client, session, v2_world):
        _enter_review(client, session, v2_world)
        assert _get(client, v2_world)["status"] == ReportStatus.IN_REVIEW


class TestLegacyUnaffected:
    def test_legacy_report_also_refuses_a_letterhead_change(self, client, session):
        """§14 updated. A Legacy report has no `presentation` to replace, so
        remediation 5 ignored the field silently. 1.3.1 refuses it instead:
        the rule is about WHO may change a letterhead, and that does not vary
        by schema version. No real client sends it — the selector is not
        rendered for authors — so this only closes a direct-call path."""
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
        assert resp.status_code == 403, resp.text

    def test_a_legacy_content_save_without_a_letterhead_still_works(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="legacy2@t1.example")
        headers = auth_headers(user)

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
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        detail = client.get(f"/api/v1/reports/{report_id}", headers=headers).json()
        assert detail["schema_version"] is None
        assert detail["letterhead_version_id"] is None
        assert detail["report"]["base"]["diagnosis"]["value"] == "Actualizado"
