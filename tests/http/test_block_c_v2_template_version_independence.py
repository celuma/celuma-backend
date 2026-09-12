"""Céluma 1.3.1 Block C — Reports V2 without an active template version.

CEL-131-05. The production regression, in one sentence: a laboratory whose
report template was saved *before* its letterhead was configured has no
`report_template_version` row, cannot obtain one without re-saving the
template, and was therefore locked out of Reports V2 entirely — the editor
refused to open with "La plantilla de este estudio no está publicada" even
though the letterhead resolved perfectly.

The dependency ran through three layers and this file exercises all three:

  1. `GET /study-types/{id}/report-defaults` returned
     `v2_blocked_reason = "NO_ACTIVE_TEMPLATE_VERSION"`.
  2. `POST /reports/` accepted only `template_version_id` as its V2 selector,
     so with no version there was no way to ask for a V2 report — it silently
     produced a Legacy one instead.
  3. `ck_report_version_v2_requires_template_version` (v1_3_0) forbade a V2
     `report_version` row from carrying a NULL `template_version_id` at the
     database level, which is why (2) could not simply be relaxed. The
     consolidated `v1_3_1` drops it.

The property that matters most is in `TestHistoricalReportsAreDeterministic`:
removing the dependency must not make an existing report's appearance depend
on whichever version happens to be ACTIVE *now*. A V2 report is reconstructed
from the `rendering_snapshot` frozen into its own JSON body and from nothing
else — before this block and after it.
"""
import json
import uuid

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.models.enums import ReportStatus
from app.models.report import Report, ReportTemplate, ReportVersion
from app.models.report_template_version import (
    ReportTemplateVersion,
    ReportTemplateVersionStatus,
)
from app.models.storage import StorageObject
from app.services.report_template_hash import hash_clinical_template_block

from .conftest import FakeS3Service
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


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

#: The live clinical structure, as an administrator would have saved it into
#: `ReportTemplate.template_json`. Deliberately DIFFERENT from
#: `valid_rendering_snapshot()["template"]` so that every assertion below can
#: tell which of the two a report actually froze.
LIVE_TEMPLATE_JSON = {
    "base": {"diagnosis": {"label": "Diagnóstico", "type": "text"}},
    "sections": {"micro": {"type": "text", "content": "", "label": "Microscopía"}},
    "base_order": ["diagnosis"],
    "section_order": ["micro"],
}

#: What an older, published template version froze. Distinguishable by a
#: section the live template does not have.
HISTORICAL_TEMPLATE_JSON = {
    "base": {"diagnosis": {"label": "Diagnóstico (histórico)", "type": "text"}},
    "sections": {"legacy_only": {"type": "text", "content": "", "label": "Histórico"}},
    "base_order": ["diagnosis"],
    "section_order": ["legacy_only"],
}


def _lab(session, *, name="Block C Lab", v2=True, template_json=None):
    """A laboratory whose V2 configuration is COMPLETE — template with clinical
    structure, study type pointing at it, default letterhead with an ACTIVE
    version — and which has no `report_template_version` row at all."""
    from app.models.study_type import StudyType

    tenant = create_tenant(session, name=name, reports_v2_enabled=v2)
    branch = create_branch(session, tenant)
    template = ReportTemplate(
        tenant_id=tenant.id,
        name="Plantilla clínica",
        template_json=LIVE_TEMPLATE_JSON if template_json is None else template_json,
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
    lh_version = create_letterhead_version(
        session, tenant, letterhead, status="ACTIVE", configuration=valid_presentation()
    )
    session.commit()
    session.refresh(template)
    session.refresh(study_type)
    return {
        "tenant": tenant,
        "branch": branch,
        "template": template,
        "study_type": study_type,
        "letterhead": letterhead,
        "letterhead_version": lh_version,
    }


def _author(session, lab, *, email=None, roles=("pathologist",)):
    return create_user(
        session,
        lab["tenant"],
        email=email or f"author-{uuid.uuid4().hex[:8]}@blockc.example",
        roles=roles,
    )


def _template_version(session, lab, *, status, template_json, version_number=1):
    version = ReportTemplateVersion(
        tenant_id=lab["tenant"].id,
        report_template_id=lab["template"].id,
        version_number=version_number,
        schema_version=2,
        configuration=valid_rendering_snapshot(template=template_json),
        status=status,
    )
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


def _defaults(client, lab, actor):
    return client.get(
        f"/api/v1/study-types/{lab['study_type'].id}/report-defaults",
        headers=auth_headers(actor),
    )


def _create_v2(client, lab, actor, session, *, selector="template_id", **extra):
    """Create a report the way the editor does, via the given V2 selector.

    Céluma 1.3.1 C-8: the `template_id` selector also carries `template_hash`,
    the optimistic-concurrency token the editor receives alongside
    `template_json`. Computed here from the template's current state, which is
    what a freshly-bootstrapped editor holds — the staleness cases live in
    `test_block_c_template_mutation_race.py`, which is where that guard is
    specified.
    """
    order = create_order(
        session, lab["tenant"], lab["branch"], order_code=f"ORD-{uuid.uuid4().hex[:8]}"
    )
    payload = {
        "tenant_id": str(lab["tenant"].id),
        "branch_id": str(lab["branch"].id),
        "order_id": str(order.id),
        "title": "Reporte Block C",
        "template": lab["template"].template_json,
        "report": {"base": {}, "sections": {}},
    }
    if selector == "template_id":
        payload["template_id"] = str(lab["template"].id)
        session.refresh(lab["template"])
        payload["template_hash"] = hash_clinical_template_block(
            lab["template"].template_json
        )
    payload.update(extra)
    resp = client.post("/api/v1/reports/", json=payload, headers=auth_headers(actor))
    return resp, order


def _new_version(client, lab, report, actor, *, report_body=None, **extra):
    """`POST /{id}/new_version` takes the whole envelope, as the editor sends
    it, not just the changed content."""
    payload = {
        "tenant_id": str(lab["tenant"].id),
        "branch_id": str(lab["branch"].id),
        "order_id": str(report.order_id),
        "report": report_body if report_body is not None else {"base": {}, "sections": {}},
    }
    payload.update(extra)
    return client.post(
        f"/api/v1/reports/{report.id}/new_version",
        json=payload,
        headers=auth_headers(actor),
    )


def _frozen_snapshot(session, report_id) -> dict | None:
    """The report's own source of truth: `rendering_snapshot` in its JSON body."""
    version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == report_id,
            ReportVersion.is_current == True,  # noqa: E712
        )
    ).first()
    if version is None or version.json_storage_id is None:
        return None
    storage = session.get(StorageObject, version.json_storage_id)
    body = json.loads(FakeS3Service.store[storage.object_key].decode("utf-8"))
    return body.get("rendering_snapshot")


def _seed_v2_report(
    session,
    lab,
    *,
    status,
    snapshot_template,
    template_version=None,
    order=None,
    signed_by=None,
    signed_at=None,
    published_at=None,
):
    """An EXISTING V2 report, inserted directly with a frozen snapshot, the way
    one created before this block looks on disk. `template_version` is the
    provenance it carries: a real id for a pre-1.3.1 report, None for one
    created through the Block C path."""
    order = order or create_order(
        session, lab["tenant"], lab["branch"], order_code=f"ORD-{uuid.uuid4().hex[:8]}"
    )
    report = Report(
        tenant_id=lab["tenant"].id,
        branch_id=lab["branch"].id,
        order_id=order.id,
        status=status,
        published_at=published_at,
    )
    session.add(report)
    session.flush()
    body = {
        "base": {},
        "sections": {},
        "schema_version": 2,
        "rendering_snapshot": valid_rendering_snapshot(template=snapshot_template),
    }
    key = (
        f"reports/{lab['tenant'].id}/{lab['branch'].id}/{report.id}/versions/1/report.json"
    )
    FakeS3Service.store[key] = json.dumps(body, ensure_ascii=False).encode("utf-8")
    storage = create_storage_object(session, key=key, tenant=lab["tenant"])
    version = ReportVersion(
        report_id=report.id,
        version_no=1,
        is_current=True,
        json_storage_id=storage.id,
        schema_version=2,
        template_version_id=(template_version.id if template_version else None),
        signed_by=signed_by,
        signed_at=signed_at,
    )
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version


# ---------------------------------------------------------------------------
# 1. The reproduction, inverted
# ---------------------------------------------------------------------------

class TestTheProductionRegressionIsFixed:
    """The exact scenario the brief specifies: Reports V2 enabled, template
    exists, NO active template version exists, V2 flow attempted."""

    def test_no_template_version_row_exists_in_this_laboratory(self, session):
        """A guard on the premise. If a fixture ever started creating one,
        every test in this class would pass vacuously."""
        lab = _lab(session)
        rows = session.exec(
            select(ReportTemplateVersion).where(
                ReportTemplateVersion.tenant_id == lab["tenant"].id
            )
        ).all()
        assert rows == []

    def test_report_defaults_does_not_block_the_editor(self, client, session):
        """Was `NO_ACTIVE_TEMPLATE_VERSION`; the laboratory's V2 setup is in
        fact complete, and the response now says so."""
        lab = _lab(session)
        resp = _defaults(client, lab, _author(session, lab))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["v2_blocked_reason"] is None
        assert body["v2_blocked_detail"] is None
        assert body["template_id"] == str(lab["template"].id)
        assert body["letterhead_version_id"] == str(lab["letterhead_version"].id)
        assert body["letterhead_presentation"] is not None

    def test_report_defaults_reports_no_active_version_without_blocking(
        self, client, session
    ):
        """`active_template_version_id` survives as diagnostic information.
        It is null here, and that is no longer an error condition."""
        lab = _lab(session)
        body = _defaults(client, lab, _author(session, lab)).json()

        assert body["active_template_version_id"] is None
        assert body["v2_blocked_reason"] is None

    def test_a_v2_report_can_be_created(self, client, session):
        """The user-visible outcome the regression prevented."""
        lab = _lab(session)
        author = _author(session, lab)

        resp, _ = _create_v2(client, lab, author, session)

        assert resp.status_code == 200, resp.text
        detail = client.get(
            f"/api/v1/reports/{resp.json()['id']}", headers=auth_headers(author)
        ).json()
        assert detail["schema_version"] == 2
        assert detail["report"]["rendering_snapshot"] is not None

    def test_the_created_report_freezes_the_LIVE_template_structure(
        self, client, session
    ):
        """The creation-time source of truth is `ReportTemplate.template_json`,
        and it is frozen into the report — not referenced."""
        lab = _lab(session)
        author = _author(session, lab)

        resp, _ = _create_v2(client, lab, author, session)

        snapshot = _frozen_snapshot(session, resp.json()["id"])
        assert snapshot["template"] == LIVE_TEMPLATE_JSON
        assert snapshot["schema_version"] == 2

    def test_presentation_comes_from_the_resolved_letterhead(self, client, session):
        """Unchanged from before this block: the letterhead resolver owns
        presentation, and the template-version path never did."""
        lab = _lab(session)
        author = _author(session, lab)

        resp, _ = _create_v2(client, lab, author, session)

        snapshot = _frozen_snapshot(session, resp.json()["id"])
        # The resolver normalises the stored letterhead configuration, so
        # compare the fields the letterhead actually carries rather than the
        # whole block.
        presentation = snapshot["presentation"]
        expected = valid_presentation()
        assert presentation["header"]["institution_name"] == (
            expected["header"]["institution_name"]
        )
        assert presentation["style"]["primary_color"] == (
            expected["style"]["primary_color"]
        )
        assert presentation["paper"]["margins_cm"] == expected["paper"]["margins_cm"]
        version = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == resp.json()["id"])
        ).first()
        assert version.letterhead_version_id == lab["letterhead_version"].id

    def test_the_new_version_persists_a_NULL_template_version_id(
        self, client, session
    ):
        """Honest provenance. No version was read, so none is claimed — and
        the consolidated `v1_3_1` permits it."""
        lab = _lab(session)
        author = _author(session, lab)

        resp, _ = _create_v2(client, lab, author, session)

        version = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == resp.json()["id"])
        ).first()
        assert version.schema_version == 2
        assert version.template_version_id is None

    def test_no_template_version_row_is_created_as_a_side_effect(
        self, client, session
    ):
        """The fix must not quietly do what the old flow demanded the user do
        by hand. Nothing auto-creates, activates or reactivates a version."""
        lab = _lab(session)
        author = _author(session, lab)

        _create_v2(client, lab, author, session)

        rows = session.exec(
            select(ReportTemplateVersion).where(
                ReportTemplateVersion.tenant_id == lab["tenant"].id
            )
        ).all()
        assert rows == []

    def test_the_report_renders_and_reopens_from_its_own_snapshot(
        self, client, session
    ):
        """`/full` — the editor's read path for an existing report — needs no
        template version either."""
        lab = _lab(session)
        author = _author(session, lab)
        resp, _ = _create_v2(client, lab, author, session)

        full = client.get(
            f"/api/v1/reports/{resp.json()['id']}/full", headers=auth_headers(author)
        )

        assert full.status_code == 200, full.text
        report = full.json()["report"]
        assert report["schema_version"] == 2
        assert report["template_version_id"] is None
        assert report["report"]["rendering_snapshot"]["template"] == LIVE_TEMPLATE_JSON

    def test_the_report_can_be_edited(self, client, session):
        """A new content version on a report with no template version: the
        carry-forward path keys off the frozen snapshot, not the version id."""
        lab = _lab(session)
        author = _author(session, lab)
        resp, order = _create_v2(client, lab, author, session)
        report_id = resp.json()["id"]
        created = session.get(Report, uuid.UUID(report_id))

        saved = _new_version(
            client,
            lab,
            created,
            author,
            report_body={"base": {}, "sections": {"micro": {"content": "texto"}}},
            changelog="edit",
        )

        assert saved.status_code == 200, saved.text
        snapshot = _frozen_snapshot(session, report_id)
        assert snapshot["template"] == LIVE_TEMPLATE_JSON
        current = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == uuid.UUID(report_id),
                ReportVersion.is_current == True,  # noqa: E712
            )
        ).first()
        assert current.version_no == 2
        assert current.schema_version == 2
        assert current.template_version_id is None


# ---------------------------------------------------------------------------
# 2. Historical determinism — the property that must not regress
# ---------------------------------------------------------------------------

class TestHistoricalReportsAreDeterministic:
    """An existing report's appearance must depend on its own frozen snapshot
    and on nothing whose status can change later."""

    @pytest.mark.parametrize(
        "status",
        [
            ReportStatus.DRAFT,
            ReportStatus.IN_REVIEW,
            ReportStatus.APPROVED,
            ReportStatus.PUBLISHED,
        ],
    )
    def test_an_existing_v2_report_reconstructs_in_every_status(
        self, client, session, status
    ):
        lab = _lab(session)
        author = _author(session, lab)
        report, _ = _seed_v2_report(
            session,
            lab,
            status=status,
            snapshot_template=HISTORICAL_TEMPLATE_JSON,
            published_at=None,
        )

        full = client.get(
            f"/api/v1/reports/{report.id}/full", headers=auth_headers(author)
        )

        assert full.status_code == 200, full.text
        body = full.json()["report"]
        assert body["schema_version"] == 2
        assert body["report"]["rendering_snapshot"]["template"] == (
            HISTORICAL_TEMPLATE_JSON
        )

    def test_an_inactive_historical_version_still_reconstructs(
        self, client, session
    ):
        """The report's original version was demoted to PUBLISHED, or archived
        outright. Neither is consulted, so neither can break the report."""
        lab = _lab(session)
        author = _author(session, lab)
        archived = _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.ARCHIVED,
            template_json=HISTORICAL_TEMPLATE_JSON,
        )
        report, _ = _seed_v2_report(
            session,
            lab,
            status=ReportStatus.APPROVED,
            snapshot_template=HISTORICAL_TEMPLATE_JSON,
            template_version=archived,
        )

        full = client.get(
            f"/api/v1/reports/{report.id}/full", headers=auth_headers(author)
        )

        assert full.status_code == 200, full.text
        assert full.json()["report"]["report"]["rendering_snapshot"]["template"] == (
            HISTORICAL_TEMPLATE_JSON
        )
        assert full.json()["report"]["template_version_id"] == str(archived.id)

    def test_a_newer_active_version_does_not_change_an_existing_report(
        self, client, session
    ):
        """The regression this whole block risks introducing. The laboratory
        publishes and activates a new version with a DIFFERENT structure; the
        already-existing report must be byte-for-byte unchanged."""
        lab = _lab(session)
        author = _author(session, lab)
        old = _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.PUBLISHED,
            template_json=HISTORICAL_TEMPLATE_JSON,
            version_number=1,
        )
        report, _ = _seed_v2_report(
            session,
            lab,
            status=ReportStatus.APPROVED,
            snapshot_template=HISTORICAL_TEMPLATE_JSON,
            template_version=old,
        )
        before = _frozen_snapshot(session, report.id)

        _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.ACTIVE,
            template_json=LIVE_TEMPLATE_JSON,
            version_number=2,
        )

        after = client.get(
            f"/api/v1/reports/{report.id}/full", headers=auth_headers(author)
        ).json()["report"]
        assert after["report"]["rendering_snapshot"] == before
        assert after["report"]["rendering_snapshot"]["template"] == (
            HISTORICAL_TEMPLATE_JSON
        )
        # And its provenance still names the version it really came from.
        assert after["template_version_id"] == str(old.id)

    def test_an_edit_does_not_rebind_to_the_newly_active_version(
        self, client, session
    ):
        """Saving content is the one moment a report could be re-resolved.
        `_carry_forward_v2_metadata` re-attaches the frozen snapshot instead."""
        lab = _lab(session)
        author = _author(session, lab)
        old = _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.PUBLISHED,
            template_json=HISTORICAL_TEMPLATE_JSON,
            version_number=1,
        )
        report, _ = _seed_v2_report(
            session,
            lab,
            status=ReportStatus.DRAFT,
            snapshot_template=HISTORICAL_TEMPLATE_JSON,
            template_version=old,
        )
        newly_active = _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.ACTIVE,
            template_json=LIVE_TEMPLATE_JSON,
            version_number=2,
        )

        saved = _new_version(client, lab, report, author, changelog="edit")

        assert saved.status_code == 200, saved.text
        assert _frozen_snapshot(session, report.id)["template"] == (
            HISTORICAL_TEMPLATE_JSON
        )
        current = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == report.id,
                ReportVersion.is_current == True,  # noqa: E712
            )
        ).first()
        assert current.template_version_id == old.id
        assert current.template_version_id != newly_active.id

    def test_historical_template_version_ids_are_never_cleared(self, session):
        """Block C preserves provenance. No code path and no migration
        rewrites, clears or backfills an existing `template_version_id`."""
        lab = _lab(session)
        historical = _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.PUBLISHED,
            template_json=HISTORICAL_TEMPLATE_JSON,
        )
        _, version = _seed_v2_report(
            session,
            lab,
            status=ReportStatus.PUBLISHED,
            snapshot_template=HISTORICAL_TEMPLATE_JSON,
            template_version=historical,
        )

        session.refresh(version)
        assert version.template_version_id == historical.id


# ---------------------------------------------------------------------------
# 3. Lifecycle — Block A and Block B shapes
# ---------------------------------------------------------------------------

class TestLifecycleShapesStillWork:
    """Block B's warning: a reopened DRAFT is not a brand-new report. It has a
    current version and a frozen presentation, and nothing in Block C may
    replace either."""

    def test_a_reopened_draft_keeps_its_frozen_snapshot_and_is_editable(
        self, client, session
    ):
        """APPROVED -> reopen -> DRAFT -> edit, on a report with NO template
        version at all. The bootstrap must not treat it as new."""
        lab = _lab(session)
        admin = _author(session, lab, roles=("admin",))
        author = _author(session, lab)
        report, _ = _seed_v2_report(
            session,
            lab,
            status=ReportStatus.APPROVED,
            snapshot_template=HISTORICAL_TEMPLATE_JSON,
            template_version=None,
        )

        reopened = client.post(
            f"/api/v1/reports/{report.id}/reopen", json={}, headers=auth_headers(admin)
        )
        assert reopened.status_code == 200, reopened.text

        saved = _new_version(client, lab, report, author, changelog="after reopen")
        assert saved.status_code == 200, saved.text
        # The reopened report kept the snapshot it was approved with — it did
        # not acquire the live template's structure.
        assert _frozen_snapshot(session, report.id)["template"] == (
            HISTORICAL_TEMPLATE_JSON
        )

    def test_an_approved_v2_report_is_still_content_frozen(self, client, session):
        """Block B's `CONTENT_EDITABLE_STATUSES`. Block C changes no state."""
        lab = _lab(session)
        author = _author(session, lab)
        report, _ = _seed_v2_report(
            session,
            lab,
            status=ReportStatus.APPROVED,
            snapshot_template=HISTORICAL_TEMPLATE_JSON,
        )

        resp = _new_version(client, lab, report, author)

        assert resp.status_code == 409, resp.text

    @pytest.mark.parametrize("status", [ReportStatus.PUBLISHED, ReportStatus.RETRACTED])
    def test_published_and_retracted_stay_frozen(self, client, session, status):
        lab = _lab(session)
        author = _author(session, lab)
        report, _ = _seed_v2_report(
            session, lab, status=status, snapshot_template=HISTORICAL_TEMPLATE_JSON
        )

        resp = _new_version(client, lab, report, author)

        assert resp.status_code == 409, resp.text

    def test_an_in_review_v2_report_is_still_editable(self, client, session):
        lab = _lab(session)
        author = _author(session, lab)
        report, _ = _seed_v2_report(
            session,
            lab,
            status=ReportStatus.IN_REVIEW,
            snapshot_template=HISTORICAL_TEMPLATE_JSON,
        )

        resp = _new_version(client, lab, report, author)

        assert resp.status_code == 200, resp.text

    def test_a_published_report_reconstructs_deterministically_twice(
        self, client, session
    ):
        """Two reads of the same published report must agree, including after
        the laboratory activates a different version in between."""
        lab = _lab(session)
        author = _author(session, lab)
        report, _ = _seed_v2_report(
            session,
            lab,
            status=ReportStatus.PUBLISHED,
            snapshot_template=HISTORICAL_TEMPLATE_JSON,
        )
        first = client.get(
            f"/api/v1/reports/{report.id}/full", headers=auth_headers(author)
        ).json()["report"]["report"]

        _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.ACTIVE,
            template_json=LIVE_TEMPLATE_JSON,
        )

        second = client.get(
            f"/api/v1/reports/{report.id}/full", headers=auth_headers(author)
        ).json()["report"]["report"]
        assert first == second

    def test_block_a_presentation_defaults_still_come_from_the_snapshot(
        self, client, session
    ):
        """Block A: a new report's `signatureMetadata` is overwritten with the
        template's defaults, read for V2 from the SERVER-resolved snapshot.
        That snapshot now comes from the live template, so the source moved —
        the rule did not."""
        lab = _lab(
            session,
            template_json={
                **LIVE_TEMPLATE_JSON,
                "signatureMetadata": {
                    "show_signature_section": True,
                    "require_digital_signature": True,
                },
            },
        )
        author = _author(session, lab)

        resp, _ = _create_v2(client, lab, author, session)

        assert resp.status_code == 200, resp.text
        version = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == resp.json()["id"])
        ).first()
        storage = session.get(StorageObject, version.json_storage_id)
        body = json.loads(FakeS3Service.store[storage.object_key].decode("utf-8"))
        assert body["signatureMetadata"]["show_signature_section"] is True
        assert body["signatureMetadata"]["require_digital_signature"] is True


# ---------------------------------------------------------------------------
# 4. The retained historical selector, and the Legacy boundary
# ---------------------------------------------------------------------------

class TestTheHistoricalSelectorIsRetained:
    """`template_version_id` is not removed. It still means "build from exactly
    this published version", and a client that sends it gets the old behaviour
    including real recorded provenance."""

    def test_template_version_id_still_creates_a_v2_report(self, client, session):
        lab = _lab(session)
        author = _author(session, lab)
        version = _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.ACTIVE,
            template_json=HISTORICAL_TEMPLATE_JSON,
        )

        resp, _ = _create_v2(
            client,
            lab,
            author,
            session,
            selector="none",
            template_version_id=str(version.id),
        )

        assert resp.status_code == 200, resp.text
        created = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == resp.json()["id"])
        ).first()
        assert created.schema_version == 2
        assert created.template_version_id == version.id
        assert _frozen_snapshot(session, resp.json()["id"])["template"] == (
            HISTORICAL_TEMPLATE_JSON
        )

    def test_a_PUBLISHED_not_active_version_is_still_accepted(self, client, session):
        """Creation never required ACTIVE — only "not ARCHIVED". Unchanged."""
        lab = _lab(session)
        author = _author(session, lab)
        version = _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.PUBLISHED,
            template_json=HISTORICAL_TEMPLATE_JSON,
        )

        resp, _ = _create_v2(
            client,
            lab,
            author,
            session,
            selector="none",
            template_version_id=str(version.id),
        )

        assert resp.status_code == 200, resp.text

    def test_an_archived_version_is_still_refused(self, client, session):
        lab = _lab(session)
        author = _author(session, lab)
        version = _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.ARCHIVED,
            template_json=HISTORICAL_TEMPLATE_JSON,
        )

        resp, _ = _create_v2(
            client,
            lab,
            author,
            session,
            selector="none",
            template_version_id=str(version.id),
        )

        assert resp.status_code == 409, resp.text

    def test_the_explicit_historical_selector_wins_over_template_id(
        self, client, session
    ):
        """Both sent. The more specific request is honoured, and its provenance
        recorded — not silently dropped in favour of the live template."""
        lab = _lab(session)
        author = _author(session, lab)
        version = _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.PUBLISHED,
            template_json=HISTORICAL_TEMPLATE_JSON,
        )

        resp, _ = _create_v2(
            client,
            lab,
            author,
            session,
            template_version_id=str(version.id),
        )

        assert resp.status_code == 200, resp.text
        created = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == resp.json()["id"])
        ).first()
        assert created.template_version_id == version.id
        assert _frozen_snapshot(session, resp.json()["id"])["template"] == (
            HISTORICAL_TEMPLATE_JSON
        )

    def test_legacy_creation_is_untouched(self, client, session):
        """Neither selector sent: still a Legacy report, byte-for-byte the old
        behaviour, with no snapshot and no V2 metadata."""
        lab = _lab(session)
        author = _author(session, lab)

        resp, _ = _create_v2(client, lab, author, session, selector="none")

        assert resp.status_code == 200, resp.text
        created = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == resp.json()["id"])
        ).first()
        assert created.schema_version is None
        assert created.template_version_id is None
        assert _frozen_snapshot(session, resp.json()["id"]) is None

    def test_a_legacy_tenant_cannot_use_the_new_selector(self, client, session):
        """`reports_v2_enabled=false` still gates V2 entirely. `template_id`
        is not a way around the flag."""
        lab = _lab(session, v2=False)
        author = _author(session, lab)

        resp, _ = _create_v2(client, lab, author, session)

        assert resp.status_code == 403, resp.text
        assert "not enabled" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Explicit failures — corruption is never hidden
# ---------------------------------------------------------------------------

class TestMissingInvariantsFailExplicitly:
    """Removing the obsolete block must not turn a genuine configuration
    problem into a silent Legacy fallback, nor into the old message."""

    def test_no_template_still_blocks_with_its_own_reason(self, client, session):
        lab = _lab(session)
        lab["study_type"].default_report_template_id = None
        session.add(lab["study_type"])
        session.commit()

        body = _defaults(client, lab, _author(session, lab)).json()

        assert body["v2_blocked_reason"] == "NO_TEMPLATE"

    def test_no_letterhead_still_blocks_with_its_own_reason(self, client, session):
        lab = _lab(session)
        lab["letterhead"].is_default = False
        session.add(lab["letterhead"])
        session.commit()

        body = _defaults(client, lab, _author(session, lab)).json()

        assert body["v2_blocked_reason"] == "NO_LETTERHEAD"

    def test_no_active_template_version_is_never_returned_again(
        self, client, session
    ):
        """Across every reachable configuration state, including a template
        whose only version is archived."""
        lab = _lab(session)
        _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.ARCHIVED,
            template_json=HISTORICAL_TEMPLATE_JSON,
        )

        body = _defaults(client, lab, _author(session, lab)).json()

        assert body["v2_blocked_reason"] != "NO_ACTIVE_TEMPLATE_VERSION"
        assert body["v2_blocked_reason"] is None

    def test_v2_creation_without_a_letterhead_is_refused_not_downgraded(
        self, client, session
    ):
        """A request that asks for V2 and cannot have it fails loudly. It must
        never quietly become a Legacy report."""
        lab = _lab(session)
        lab["letterhead"].is_default = False
        session.add(lab["letterhead"])
        session.commit()
        author = _author(session, lab)

        resp, order = _create_v2(client, lab, author, session)

        assert resp.status_code == 409, resp.text
        assert "membrete" in resp.json()["detail"].lower()
        assert (
            session.exec(select(Report).where(Report.order_id == order.id)).first()
            is None
        )

    def test_an_unknown_template_id_is_404(self, client, session):
        lab = _lab(session)
        author = _author(session, lab)

        resp, _ = _create_v2(
            client, lab, author, session, template_id=str(uuid.uuid4())
        )

        assert resp.status_code == 404, resp.text

    def test_a_deactivated_template_is_refused(self, client, session):
        lab = _lab(session)
        lab["template"].is_active = False
        session.add(lab["template"])
        session.commit()
        author = _author(session, lab)

        resp, _ = _create_v2(client, lab, author, session)

        assert resp.status_code == 409, resp.text

    def test_a_structure_too_large_to_freeze_fails_on_its_own_invariant(
        self, client, session
    ):
        """The real missing invariant is named, and no unrelated ACTIVE version
        is substituted to make the request succeed."""
        lab = _lab(session, template_json={"base": {}, "sections": {}, "blob": "x" * 600_000})
        _template_version(
            session,
            lab,
            status=ReportTemplateVersionStatus.ACTIVE,
            template_json=HISTORICAL_TEMPLATE_JSON,
        )
        author = _author(session, lab)

        resp, order = _create_v2(client, lab, author, session)

        assert resp.status_code == 409, resp.text
        assert "estructura clínica" in resp.json()["detail"]
        assert (
            session.exec(select(Report).where(Report.order_id == order.id)).first()
            is None
        )

    def test_v2_content_is_still_required(self, client, session):
        lab = _lab(session)
        author = _author(session, lab)
        order = create_order(
            session, lab["tenant"], lab["branch"], order_code=f"ORD-{uuid.uuid4().hex[:8]}"
        )

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(lab["tenant"].id),
                "branch_id": str(lab["branch"].id),
                "order_id": str(order.id),
                "template_id": str(lab["template"].id),
                # C-8: sent so this test still exercises the CONTENT
                # requirement. Omitting it would be caught earlier, by request
                # validation (422), and this test would stop testing its subject.
                "template_hash": hash_clinical_template_block(
                    lab["template"].template_json
                ),
            },
            headers=auth_headers(author),
        )

        assert resp.status_code == 400, resp.text
        assert "rendering snapshot" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 6. Tenant isolation
# ---------------------------------------------------------------------------

class TestTenantIsolation:
    """Widening the selector must not widen the tenant boundary."""

    def test_another_tenants_template_cannot_be_used(self, client, session):
        lab_a = _lab(session, name="Lab A")
        lab_b = _lab(session, name="Lab B")
        author_a = _author(session, lab_a, email="a@blockc.example")

        resp, order = _create_v2(
            client,
            lab_a,
            author_a,
            session,
            template_id=str(lab_b["template"].id),
        )

        assert resp.status_code == 404, resp.text
        assert (
            session.exec(select(Report).where(Report.order_id == order.id)).first()
            is None
        )

    def test_another_tenants_template_version_cannot_be_used(self, client, session):
        lab_a = _lab(session, name="Lab A2")
        lab_b = _lab(session, name="Lab B2")
        version_b = _template_version(
            session,
            lab_b,
            status=ReportTemplateVersionStatus.ACTIVE,
            template_json=HISTORICAL_TEMPLATE_JSON,
        )
        author_a = _author(session, lab_a, email="a2@blockc.example")

        resp, _ = _create_v2(
            client,
            lab_a,
            author_a,
            session,
            selector="none",
            template_version_id=str(version_b.id),
        )

        assert resp.status_code == 404, resp.text

    def test_another_tenants_active_version_never_satisfies_this_lookup(
        self, client, session
    ):
        """Tenant B has an ACTIVE version; tenant A has none. A's
        `report-defaults` must not see B's, in either direction."""
        lab_a = _lab(session, name="Lab A3")
        lab_b = _lab(session, name="Lab B3")
        _template_version(
            session,
            lab_b,
            status=ReportTemplateVersionStatus.ACTIVE,
            template_json=HISTORICAL_TEMPLATE_JSON,
        )
        author_a = _author(session, lab_a, email="a3@blockc.example")

        body = _defaults(client, lab_a, author_a).json()

        assert body["active_template_version_id"] is None
        assert body["template_id"] == str(lab_a["template"].id)
        assert body["v2_blocked_reason"] is None

    def test_a_foreign_study_types_defaults_are_not_readable(self, client, session):
        lab_a = _lab(session, name="Lab A4")
        lab_b = _lab(session, name="Lab B4")
        author_a = _author(session, lab_a, email="a4@blockc.example")

        resp = client.get(
            f"/api/v1/study-types/{lab_b['study_type'].id}/report-defaults",
            headers=auth_headers(author_a),
        )

        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 7. The schema contract, at the database level
# ---------------------------------------------------------------------------

class TestTheDatabaseAcceptsHonestProvenance:
    """The consolidated `v1_3_1` dropped
    `ck_report_version_v2_requires_template_version`. These assert the state of
    the database the whole suite runs against, built by `alembic upgrade head`
    (see `tests/http/conftest.py`). The migration's own upgrade/downgrade
    behaviour is covered in `tests/test_alembic_migrations.py`."""

    def test_the_obsolete_check_constraint_is_absent_at_head(self, session):
        names = [
            row[0]
            for row in session.exec(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'report_version'::regclass AND contype = 'c'"
                )
            ).all()
        ]
        assert "ck_report_version_v2_requires_template_version" not in names

    def test_the_unrelated_check_constraints_survive(self, session):
        names = [
            row[0]
            for row in session.exec(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'report_version'::regclass AND contype = 'c'"
                )
            ).all()
        ]
        assert "ck_report_version_pdf_ready_requires_artifact" in names
        assert "ck_report_version_pdf_generation_status_values" in names

    def test_the_column_its_foreign_key_and_its_index_all_survive(self, session):
        column = session.exec(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'report_version' "
                "AND column_name = 'template_version_id'"
            )
        ).first()
        assert column is not None
        fk = session.exec(
            text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'report_version_template_version_id_fkey'"
            )
        ).first()
        assert fk is not None
        index = session.exec(
            text(
                "SELECT 1 FROM pg_indexes "
                "WHERE indexname = 'ix_report_version_template_version_id'"
            )
        ).first()
        assert index is not None

    def test_a_v2_row_with_null_provenance_is_insertable(self, session):
        """Directly at the database level, not through the API — the point is
        that the constraint no longer forbids it."""
        lab = _lab(session)
        order = create_order(
            session, lab["tenant"], lab["branch"], order_code=f"ORD-{uuid.uuid4().hex[:8]}"
        )
        report = Report(
            tenant_id=lab["tenant"].id,
            branch_id=lab["branch"].id,
            order_id=order.id,
            status=ReportStatus.DRAFT,
        )
        session.add(report)
        session.flush()
        session.add(
            ReportVersion(
                report_id=report.id,
                version_no=1,
                is_current=True,
                schema_version=2,
                template_version_id=None,
            )
        )
        session.commit()

        stored = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == report.id)
        ).first()
        assert stored.schema_version == 2
        assert stored.template_version_id is None

    def test_the_foreign_key_still_rejects_a_nonexistent_version(self, session):
        """Dropping the CHECK did not loosen referential integrity: a
        `template_version_id` must still point at a real row."""
        import sqlalchemy.exc

        lab = _lab(session)
        order = create_order(
            session, lab["tenant"], lab["branch"], order_code=f"ORD-{uuid.uuid4().hex[:8]}"
        )
        report = Report(
            tenant_id=lab["tenant"].id,
            branch_id=lab["branch"].id,
            order_id=order.id,
            status=ReportStatus.DRAFT,
        )
        session.add(report)
        session.flush()
        session.add(
            ReportVersion(
                report_id=report.id,
                version_no=1,
                is_current=True,
                schema_version=2,
                template_version_id=uuid.uuid4(),
            )
        )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            session.commit()
        session.rollback()
