"""Céluma 1.3.1 Block C — the ReportTemplate mutation race is refused (C-8).

**This file is the permanent specification of the C-8 guard.** It began as
characterization of a confirmed defect; those assertions have been INVERTED now
that the guard exists. The historical record of what the defect was, and of the
evidence gathered before the fix, is in
docs/celuma-1.3.1/block-c/template-mutation-race.md — deliberately there rather
than in tests whose passing condition would be clinical corruption.

The window CEL-131-05 opened, and what now happens in it:

    t0  editor bootstrap   GET /reports/templates/{id}
                           -> template_json = A  AND  template_hash = hash(A)
    t1  author writes clinical content against structure A
    t2  administrator saves the same template            -> template_json = B
    t3  author saves       POST /reports { template_id, template_hash: hash(A) }
                           -> backend computes hash(B), they differ
                           -> 409, nothing created

`ReportTemplate.template_json` is a mutable column and `create_report` reads it
at t3, so without a token the report would persist the author's content keyed by
A while freezing B as its structure — and the V2 renderer resolves sections
through the snapshot, so the author's text would be silently absent from the
report and from the official PDF.

The invariant, stated once:

    the clinical structure the author worked against at bootstrap MUST be the
    structure frozen into rendering_snapshot.template, or the report is not
    created at all.

Never a silent substitution. The token is compared, never trusted as data.
"""
import json
import uuid

import pytest
from sqlmodel import select

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
    create_tenant,
    create_user,
    valid_presentation,
    valid_rendering_snapshot,
)


# ---------------------------------------------------------------------------
# The two structures. Deliberately DISJOINT section keys, because that is the
# case where the consequence is worst and most visible: content authored into
# A's section has nowhere to live in B.
# ---------------------------------------------------------------------------

STRUCTURE_A = {
    "base": {"diagnosis": {"label": "Diagnóstico", "type": "text", "is_visible": True}},
    "sections": {
        "macroscopia": {
            "label": "Macroscopía",
            "type": "richtext",
            "is_visible": True,
            "content": "",
        }
    },
    "base_order": ["diagnosis"],
    "section_order": ["macroscopia"],
}

STRUCTURE_B = {
    "base": {"diagnosis": {"label": "Diagnóstico", "type": "text", "is_visible": True}},
    "sections": {
        "inmunohistoquimica": {
            "label": "Inmunohistoquímica",
            "type": "richtext",
            "is_visible": True,
            "content": "",
        }
    },
    "base_order": ["diagnosis"],
    "section_order": ["inmunohistoquimica"],
}

#: What the author actually wrote, against structure A.
AUTHORED_CONTENT = {
    "base": {"diagnosis": {"value": "Carcinoma ductal infiltrante"}},
    "sections": {
        "macroscopia": {
            "content": "Pieza de 4.2 cm con lesión central de bordes irregulares."
        }
    },
    "base_order": ["diagnosis"],
    "section_order": ["macroscopia"],
}


def _lab(session, *, name=None, template_json=None):
    from app.models.study_type import StudyType

    tenant = create_tenant(
        session, name=name or f"Race Lab {uuid.uuid4().hex[:6]}", reports_v2_enabled=True
    )
    branch = create_branch(session, tenant)
    template = ReportTemplate(
        tenant_id=tenant.id,
        name="Plantilla clínica",
        template_json=template_json if template_json is not None else STRUCTURE_A,
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
        "letterhead_version": lh_version,
    }


def _author(session, lab):
    return create_user(
        session,
        lab["tenant"],
        email=f"path-{uuid.uuid4().hex[:8]}@race.example",
        roles=("pathologist",),
    )


def _admin(session, lab):
    return create_user(
        session,
        lab["tenant"],
        email=f"admin-{uuid.uuid4().hex[:8]}@race.example",
        roles=("admin",),
    )


def _bootstrap(client, lab, actor):
    """t0 — exactly what the editor reads to build its editing surface, and the
    only place the concurrency token comes from."""
    resp = client.get(
        f"/api/v1/reports/templates/{lab['template'].id}", headers=auth_headers(actor)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _administrator_edits_the_template(client, lab, admin, new_structure):
    """t2 — through the real route, so the auto-versioning side effect runs too."""
    resp = client.put(
        f"/api/v1/reports/templates/{lab['template'].id}",
        json={"template_json": new_structure},
        headers=auth_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    return resp


def _author_saves(
    client, lab, author, session, *, pinned_version=None, template_hash=..., **extra
):
    """t3 — the editor's create call, carrying content authored against A.

    `pinned_version=None` is the CURRENT (Block C) editor: it selects the
    clinical template by `template_id` and echoes the `template_hash` it
    received at t0. Passing a version id instead reproduces the PRE-Block-C
    editor, which pinned an immutable `ReportTemplateVersion` and needs no token.
    Both send `letterhead_version_id`, because both versions of the editor
    always did.

    `template_hash` defaults to the sentinel `...`, meaning "whatever the
    template hashes to right now" — the correct token for a test that does not
    mutate anything. Pass an explicit value to simulate a stale editor, or
    `None` to omit it entirely.
    """
    order = create_order(
        session, lab["tenant"], lab["branch"], order_code=f"ORD-{uuid.uuid4().hex[:8]}"
    )
    payload = {
        "tenant_id": str(lab["tenant"].id),
        "branch_id": str(lab["branch"].id),
        "order_id": str(order.id),
        "title": "Reporte con carrera",
        # The editor round-trips the structure it bootstrapped with.
        "template": STRUCTURE_A,
        "report": dict(AUTHORED_CONTENT),
        "letterhead_version_id": str(lab["letterhead_version"].id),
    }
    if pinned_version is None:
        payload["template_id"] = str(lab["template"].id)
        if template_hash is ...:
            session.refresh(lab["template"])
            payload["template_hash"] = hash_clinical_template_block(
                lab["template"].template_json
            )
        elif template_hash is not None:
            payload["template_hash"] = template_hash
    else:
        payload["template_version_id"] = str(pinned_version)
    payload.update(extra)
    resp = client.post("/api/v1/reports/", json=payload, headers=auth_headers(author))
    return resp, order


def _stored_body(session, report_id) -> dict:
    version = session.exec(
        select(ReportVersion).where(
            ReportVersion.report_id == uuid.UUID(str(report_id)),
            ReportVersion.is_current == True,  # noqa: E712
        )
    ).first()
    storage = session.get(StorageObject, version.json_storage_id)
    return json.loads(FakeS3Service.store[storage.object_key].decode("utf-8"))


# ---------------------------------------------------------------------------
# 1. What each side actually contributes
# ---------------------------------------------------------------------------

class TestTheTokenShipsWithTheStructure:
    """The contract at t0: the token and the structure it describes come back
    together, from one read, in one response."""

    def test_bootstrap_returns_the_structure_and_its_hash_together(
        self, client, session
    ):
        lab = _lab(session)
        detail = _bootstrap(client, lab, _author(session, lab))

        assert detail["template_json"] == STRUCTURE_A
        assert detail["template_hash"] == hash_clinical_template_block(STRUCTURE_A)

    def test_the_hash_describes_the_template_json_in_the_same_response(
        self, client, session
    ):
        """Why the token lives here and not on `report-defaults`: a token fetched
        in a different request could describe a structure the editor never
        loaded, which is a smaller version of the race C-8 closes."""
        lab = _lab(session)
        detail = _bootstrap(client, lab, _author(session, lab))

        assert detail["template_hash"] == hash_clinical_template_block(
            detail["template_json"]
        )

    def test_the_hash_is_stable_across_reads(self, client, session):
        lab = _lab(session)
        author = _author(session, lab)

        first = _bootstrap(client, lab, author)["template_hash"]
        second = _bootstrap(client, lab, author)["template_hash"]

        assert first == second

    def test_the_hash_is_canonical_over_key_order(self, session):
        """A no-op save must not invalidate open editors, which is the whole
        reason this is a content hash and not a timestamp. Key order is the
        cheapest way serialization differs between two equal structures."""
        reordered = {
            "section_order": STRUCTURE_A["section_order"],
            "sections": STRUCTURE_A["sections"],
            "base_order": STRUCTURE_A["base_order"],
            "base": STRUCTURE_A["base"],
        }

        assert hash_clinical_template_block(reordered) == (
            hash_clinical_template_block(STRUCTURE_A)
        )

    def test_a_different_structure_hashes_differently(self, session):
        assert hash_clinical_template_block(STRUCTURE_A) != (
            hash_clinical_template_block(STRUCTURE_B)
        )

    def test_the_hash_moves_when_the_administrator_changes_the_structure(
        self, client, session
    ):
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        before = _bootstrap(client, lab, author)["template_hash"]

        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)

        after = _bootstrap(client, lab, author)["template_hash"]
        assert after != before
        assert after == hash_clinical_template_block(STRUCTURE_B)

    def test_the_template_row_still_has_no_updated_at_column(self, session):
        """Kept from the investigation. The absence of `updated_at` is why the
        token is a content hash rather than a timestamp comparison; if someone
        adds the column, this trips and the choice can be revisited — but note
        that a timestamp would also be WRONG, because a no-op save moves it."""
        columns = {c.name for c in ReportTemplate.__table__.columns}

        assert "updated_at" not in columns
        assert "created_at" in columns


class TestWhatTheTwoSidesContribute:
    """Which half of the persisted report comes from the request body and which
    from the template read at save time. Unchanged by C-8 — the guard decides
    *whether* to proceed, not where the data comes from."""

    def test_content_comes_from_the_request_body(self, client, session):
        """Correct and deliberate: the author owns clinical content."""
        lab = _lab(session)
        author = _author(session, lab)
        _bootstrap(client, lab, author)

        resp, _ = _author_saves(client, lab, author, session)

        assert resp.status_code == 200, resp.text
        body = _stored_body(session, resp.json()["id"])
        assert body["sections"]["macroscopia"]["content"] == (
            AUTHORED_CONTENT["sections"]["macroscopia"]["content"]
        )
        assert body["base"]["diagnosis"]["value"] == "Carcinoma ductal infiltrante"

    def test_the_frozen_structure_comes_from_the_template(self, client, session):
        """No mutation, so the token matches and A is frozen — and the two halves
        agree, which is the invariant the guard protects."""
        lab = _lab(session)
        author = _author(session, lab)
        _bootstrap(client, lab, author)

        resp, _ = _author_saves(client, lab, author, session)

        body = _stored_body(session, resp.json()["id"])
        assert body["rendering_snapshot"]["template"] == STRUCTURE_A
        assert set(body["sections"]) <= set(
            body["rendering_snapshot"]["template"]["sections"]
        )


class TestTheMutationRaceIsRefused:
    """t0 -> t2 -> t3 with a real template edit in between. Every assertion here
    was inverted from the characterization: the report used to be created with
    inconsistent halves, and now is not created at all."""

    def test_creation_is_refused_with_409(self, client, session):
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)

        resp, _ = _author_saves(client, lab, author, session, template_hash=stale)

        assert resp.status_code == 409, resp.text

    def test_the_message_tells_the_author_to_reload(self, client, session):
        """The author must learn that the template moved and that reloading is
        the remedy — not merely that the save failed."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)

        resp, _ = _author_saves(client, lab, author, session, template_hash=stale)

        detail = resp.json()["detail"].lower()
        assert "plantilla" in detail
        assert "cambió" in detail
        assert "cargar" in detail

    def test_the_newer_template_is_never_silently_substituted(
        self, client, session
    ):
        """The defect, stated as the thing that must not happen: no report
        exists whose frozen structure is B."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)

        resp, order = _author_saves(client, lab, author, session, template_hash=stale)

        assert resp.status_code == 409
        assert (
            session.exec(select(Report).where(Report.order_id == order.id)).first()
            is None
        )

    def test_a_removed_section_is_also_refused(self, client, session):
        """Not only wholesale replacement. Removing the section the author is
        writing into is the likelier edit and the same hazard."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        without_macroscopia = {
            "base": STRUCTURE_A["base"],
            "sections": {},
            "base_order": ["diagnosis"],
            "section_order": [],
        }
        _administrator_edits_the_template(client, lab, admin, without_macroscopia)

        resp, _ = _author_saves(client, lab, author, session, template_hash=stale)

        assert resp.status_code == 409, resp.text

    def test_a_hidden_section_is_also_refused(self, client, session):
        """The quietest edit of all — a single visibility toggle — used to drop
        the author's text with no other visible difference."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        hidden = {
            **STRUCTURE_A,
            "sections": {
                "macroscopia": {
                    **STRUCTURE_A["sections"]["macroscopia"],
                    "is_visible": False,
                }
            },
        }
        _administrator_edits_the_template(client, lab, admin, hidden)

        resp, _ = _author_saves(client, lab, author, session, template_hash=stale)

        assert resp.status_code == 409, resp.text

    def test_a_renamed_section_is_also_refused(self, client, session):
        """Content would survive a rename, but under a heading the author never
        saw. The report would assert something they did not write."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        renamed = {
            **STRUCTURE_A,
            "sections": {
                "macroscopia": {
                    **STRUCTURE_A["sections"]["macroscopia"],
                    "label": "Descripción macroscópica (rev. 2)",
                }
            },
        }
        _administrator_edits_the_template(client, lab, admin, renamed)

        resp, _ = _author_saves(client, lab, author, session, template_hash=stale)

        assert resp.status_code == 409, resp.text

    def test_a_canonically_identical_save_does_NOT_conflict(self, client, session):
        """The no-op case, and the reason this is a content hash. An
        administrator who opens a template and saves it unchanged — or saves it
        with the same structure serialized in a different key order — must not
        invalidate every open editor."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        reordered = {
            "section_order": STRUCTURE_A["section_order"],
            "sections": STRUCTURE_A["sections"],
            "base_order": STRUCTURE_A["base_order"],
            "base": STRUCTURE_A["base"],
        }
        _administrator_edits_the_template(client, lab, admin, reordered)

        resp, _ = _author_saves(client, lab, author, session, template_hash=stale)

        assert resp.status_code == 200, resp.text
        body = _stored_body(session, resp.json()["id"])
        assert body["rendering_snapshot"]["template"]["sections"].keys() == {
            "macroscopia"
        }

    def test_a_name_only_template_save_does_NOT_conflict(self, client, session):
        """Editing the template's NAME is not a clinical-structure change, so it
        must not invalidate an open editor either."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        renamed = client.put(
            f"/api/v1/reports/templates/{lab['template'].id}",
            json={"name": "Plantilla clínica (renombrada)"},
            headers=auth_headers(admin),
        )
        assert renamed.status_code == 200, renamed.text

        resp, _ = _author_saves(client, lab, author, session, template_hash=stale)

        assert resp.status_code == 200, resp.text

    def test_the_round_trip_after_reloading_succeeds(self, client, session):
        """The remedy works: the author reloads, gets B and hash(B), and saves."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        _bootstrap(client, lab, author)
        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)

        reloaded = _bootstrap(client, lab, author)
        resp, _ = _author_saves(
            client,
            lab,
            author,
            session,
            template_hash=reloaded["template_hash"],
            report={
                "base": {"diagnosis": {"value": "Carcinoma ductal infiltrante"}},
                "sections": {"inmunohistoquimica": {"content": "Receptores +"}},
                "base_order": ["diagnosis"],
                "section_order": ["inmunohistoquimica"],
            },
        )

        assert resp.status_code == 200, resp.text
        body = _stored_body(session, resp.json()["id"])
        assert body["rendering_snapshot"]["template"] == STRUCTURE_B
        assert set(body["sections"]) <= set(
            body["rendering_snapshot"]["template"]["sections"]
        )


class TestTheTokenIsRequiredForTheTemplateIdSelector:
    """`template_id` is new in 1.3.1, so there is no released client to keep
    compatible and no reason to leave an unsafe no-token variant."""

    def test_omitting_the_token_is_a_422(self, client, session):
        lab = _lab(session)
        author = _author(session, lab)

        resp, _ = _author_saves(client, lab, author, session, template_hash=None)

        assert resp.status_code == 422, resp.text
        assert "template_hash" in resp.text

    def test_an_empty_token_is_a_422(self, client, session):
        """Not a 409: an empty string is an incomplete request, not a conflict —
        there is nothing to compare."""
        lab = _lab(session)
        author = _author(session, lab)

        resp, _ = _author_saves(client, lab, author, session, template_hash="   ")

        assert resp.status_code == 422, resp.text

    def test_a_garbage_token_is_a_409(self, client, session):
        lab = _lab(session)
        author = _author(session, lab)

        resp, _ = _author_saves(
            client, lab, author, session, template_hash="not-a-real-hash"
        )

        assert resp.status_code == 409, resp.text

    def test_a_hash_for_another_template_does_not_authorize_this_one(
        self, client, session
    ):
        """The token is an equivalence fingerprint, not a capability: it is only
        ever compared against the template the caller already named and was
        authorized to read."""
        lab = _lab(session)
        author = _author(session, lab)
        other = ReportTemplate(
            tenant_id=lab["tenant"].id, name="Otra", template_json=STRUCTURE_B
        )
        session.add(other)
        session.commit()

        resp, _ = _author_saves(
            client,
            lab,
            author,
            session,
            template_hash=hash_clinical_template_block(STRUCTURE_B),
        )

        assert resp.status_code == 409, resp.text


class TestAConflictHasNoSideEffects:
    """A refusal must leave the database and the object store exactly as they
    were. The guard runs before the `Report` row, the `ReportVersion`, the S3
    upload, the storage accounting, the order event and the status transition."""

    def _snapshot_of_everything(self, session, lab):
        from app.models.audit import AuditLog
        from app.models.report_review import ReportReview

        return {
            "reports": session.exec(
                select(Report).where(Report.tenant_id == lab["tenant"].id)
            ).all(),
            "versions": [
                v.id
                for v in session.exec(select(ReportVersion)).all()
            ],
            "storage": [
                o.id
                for o in session.exec(
                    select(StorageObject).where(
                        StorageObject.tenant_id == lab["tenant"].id
                    )
                ).all()
            ],
            "s3_keys": set(FakeS3Service.store),
            "audit": [
                a.id
                for a in session.exec(
                    select(AuditLog).where(AuditLog.tenant_id == lab["tenant"].id)
                ).all()
            ],
            "reviews": [
                r.id
                for r in session.exec(
                    select(ReportReview).where(
                        ReportReview.tenant_id == lab["tenant"].id
                    )
                ).all()
            ],
        }

    def _usage_rows(self, session, lab):
        from app.models.tenant_usage import TenantUsage

        row = session.get(TenantUsage, lab["tenant"].id)
        return None if row is None else row.billable_storage_bytes

    def test_no_report_no_version_no_artifact_no_audit_no_review_mutation(
        self, client, session
    ):
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)
        before = self._snapshot_of_everything(session, lab)

        resp, order = _author_saves(client, lab, author, session, template_hash=stale)

        assert resp.status_code == 409, resp.text
        after = self._snapshot_of_everything(session, lab)
        assert after["reports"] == before["reports"] == []
        assert after["versions"] == before["versions"]
        assert after["storage"] == before["storage"]
        assert after["s3_keys"] == before["s3_keys"]
        assert after["audit"] == before["audit"]
        assert after["reviews"] == before["reviews"]
        # And specifically nothing for the order this attempt named.
        assert (
            session.exec(select(Report).where(Report.order_id == order.id)).first()
            is None
        )

    def test_no_storage_accounting_mutation(self, client, session):
        """`record_storage_delta_with_thresholds` runs on the V2 create path, so
        a conflict reaching it would bill a tenant for a report that does not
        exist."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)
        before = self._usage_rows(session, lab)

        resp, _ = _author_saves(client, lab, author, session, template_hash=stale)

        assert resp.status_code == 409
        assert self._usage_rows(session, lab) == before

    def test_the_order_status_is_not_advanced(self, client, session):
        """`update_order_status_for_report` moves the order to DIAGNOSIS on a
        successful create. A refused create must leave the order alone."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]
        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)
        order = create_order(
            session, lab["tenant"], lab["branch"], order_code=f"ORD-{uuid.uuid4().hex[:8]}"
        )
        status_before = order.status

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(lab["tenant"].id),
                "branch_id": str(lab["branch"].id),
                "order_id": str(order.id),
                "title": "Reporte con carrera",
                "template": STRUCTURE_A,
                "report": dict(AUTHORED_CONTENT),
                "letterhead_version_id": str(lab["letterhead_version"].id),
                "template_id": str(lab["template"].id),
                "template_hash": stale,
            },
            headers=auth_headers(author),
        )

        assert resp.status_code == 409, resp.text
        session.refresh(order)
        assert order.status == status_before

    def test_a_conflict_is_not_an_information_oracle(self, client, session):
        """Guard ordering: a caller who may not read this template gets the same
        404 they always got, never a 409 revealing that the template exists and
        has moved. Tenant and lifecycle checks still run first."""
        lab_a = _lab(session, name="Oracle A")
        lab_b = _lab(session, name="Oracle B")
        author_b = _author(session, lab_b)

        resp, _ = _author_saves(
            client,
            lab_b,
            author_b,
            session,
            template_id=str(lab_a["template"].id),
            template_hash="anything",
        )

        assert resp.status_code == 404, resp.text


class TestTheHistoricalSelectorNeedsNoToken:
    """Kept from the investigation, and still the explanation of *why* the guard
    exists: `ReportTemplateVersion.configuration` is append-only and documented
    as "never updated afterwards", so the pre-Block-C editor — which pinned one
    version id at bootstrap — was immune to this race by construction.

    That immutability is the property C-8's token restores for `template_id`.
    These tests additionally pin that the historical selector still works
    **without** a token: the requirement must never leak onto a path that does
    not need it.
    """

    def _published_version(self, session, lab, structure, number=1):
        version = ReportTemplateVersion(
            tenant_id=lab["tenant"].id,
            report_template_id=lab["template"].id,
            version_number=number,
            schema_version=2,
            configuration=valid_rendering_snapshot(template=structure),
            status=ReportTemplateVersionStatus.ACTIVE,
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        return version

    def test_the_pinned_version_flow_freezes_structure_a_despite_the_edit(
        self, client, session
    ):
        """Same race, old selector: the author gets what they authored against."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        version_a = self._published_version(session, lab, STRUCTURE_A)

        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)

        resp, _ = _author_saves(client, lab, author, session, pinned_version=version_a.id)

        assert resp.status_code == 200, resp.text
        body = _stored_body(session, resp.json()["id"])
        assert body["rendering_snapshot"]["template"] == STRUCTURE_A
        assert set(body["sections"]) <= set(
            body["rendering_snapshot"]["template"]["sections"]
        )

    def test_the_administrators_edit_left_the_pinned_version_untouched(
        self, client, session
    ):
        """Why the old flow was safe: the row the author pinned is immutable."""
        lab = _lab(session)
        admin = _admin(session, lab)
        version_a = self._published_version(session, lab, STRUCTURE_A)
        before = json.loads(json.dumps(version_a.configuration))

        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)

        session.refresh(version_a)
        assert version_a.configuration == before
        assert version_a.configuration["template"] == STRUCTURE_A
        # The edit produced a NEW version rather than mutating this one.
        all_versions = session.exec(
            select(ReportTemplateVersion).where(
                ReportTemplateVersion.report_template_id == lab["template"].id
            )
        ).all()
        assert len(all_versions) == 2

    def test_both_selectors_now_refuse_to_substitute_silently(self, client, session):
        """The two paths side by side after C-8. Each honours the structure its
        caller actually selected, and neither quietly swaps in B:

          * the pinned selector freezes A, because the version row is immutable;
          * the `template_id` selector with a stale token is REFUSED.

        Before C-8 the second line of this test created a report frozen on B.
        """
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        version_a = self._published_version(session, lab, STRUCTURE_A)
        stale = _bootstrap(client, lab, author)["template_hash"]
        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)

        pinned, _ = _author_saves(
            client, lab, author, session, pinned_version=version_a.id
        )
        unpinned, _ = _author_saves(
            client, lab, author, session, template_hash=stale
        )

        assert pinned.status_code == 200, pinned.text
        assert _stored_body(session, pinned.json()["id"])["rendering_snapshot"][
            "template"
        ] == STRUCTURE_A
        assert unpinned.status_code == 409, unpinned.text

    def test_the_pinned_selector_works_with_no_token_at_all(self, client, session):
        """The requirement is scoped to the effective `template_id` path. A
        client using the historical selector sends no `template_hash` and must
        not be asked for one."""
        lab = _lab(session)
        author = _author(session, lab)
        version_a = self._published_version(session, lab, STRUCTURE_A)

        resp, _ = _author_saves(
            client, lab, author, session, pinned_version=version_a.id
        )

        assert resp.status_code == 200, resp.text

    def test_precedence_is_unchanged_when_both_selectors_are_sent(
        self, client, session
    ):
        """`template_version_id` wins, as documented — and because it wins, the
        token requirement does not apply even though `template_id` is present.
        A stale token must therefore be IGNORED rather than refused here."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        version_a = self._published_version(session, lab, STRUCTURE_A)
        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)

        resp, _ = _author_saves(
            client,
            lab,
            author,
            session,
            template_id=str(lab["template"].id),
            template_version_id=str(version_a.id),
            template_hash="a-stale-token-that-must-be-ignored",
        )

        assert resp.status_code == 200, resp.text
        created = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == uuid.UUID(resp.json()["id"])
            )
        ).first()
        assert created.template_version_id == version_a.id
        assert _stored_body(session, resp.json()["id"])["rendering_snapshot"][
            "template"
        ] == STRUCTURE_A


# ---------------------------------------------------------------------------
# 4. Is presentation exposed to the same race?
# ---------------------------------------------------------------------------

class TestPresentationIsNotExposedToThisRace:
    """Why clinical structure is specifically new, rather than one more instance
    of a hazard the letterhead path already had.

    The editor sends `letterhead_version_id`, and a `ReportLetterheadVersion` is
    append-only and immutable exactly like a template version. So presentation is
    pinned to an immutable row at save time — and when that row is archived
    mid-session the resolver **fails loudly** rather than substituting another.
    """

    def test_presentation_is_pinned_to_the_immutable_version_the_editor_chose(
        self, client, session
    ):
        lab = _lab(session)
        author = _author(session, lab)
        other = create_letterhead(session, lab["tenant"], name="Otro membrete")
        other_version = create_letterhead_version(
            session,
            lab["tenant"],
            other,
            status="ACTIVE",
            configuration={
                **valid_presentation(),
                "header": {
                    **valid_presentation()["header"],
                    "institution_name": "Membrete Nuevo",
                },
            },
        )
        # The tenant default moves after bootstrap.
        lab["letterhead"] = other
        session.commit()

        resp, _ = _author_saves(client, lab, author, session)

        body = _stored_body(session, resp.json()["id"])
        version = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == uuid.UUID(resp.json()["id"])
            )
        ).first()
        # Pinned to what the editor sent, not to whatever resolves now.
        assert version.letterhead_version_id == lab["letterhead_version"].id
        assert version.letterhead_version_id != other_version.id
        assert body["rendering_snapshot"]["presentation"]["header"][
            "institution_name"
        ] != "Membrete Nuevo"

    def test_an_archived_letterhead_version_fails_loudly_instead_of_substituting(
        self, client, session
    ):
        """The contrast in one assertion: when the pinned presentation row is no
        longer usable the request is REFUSED. The template path has no equivalent
        — it silently substitutes."""
        from app.models.report_letterhead_version import ReportLetterheadVersionStatus

        lab = _lab(session)
        author = _author(session, lab)
        lab["letterhead_version"].status = ReportLetterheadVersionStatus.ARCHIVED
        session.add(lab["letterhead_version"])
        session.commit()

        resp, order = _author_saves(client, lab, author, session)

        assert resp.status_code == 409, resp.text
        assert (
            session.exec(select(Report).where(Report.order_id == order.id)).first()
            is None
        )


class TestTheGuardDoesNotLeakOntoOtherPaths:
    """C-8 adds one requirement to one selector. These pin that it reaches
    nothing else — the most likely way a concurrency guard goes wrong is by
    quietly becoming mandatory somewhere it has no meaning."""

    def test_legacy_creation_needs_no_token(self, client, session):
        """No selector at all: still Legacy, still accepted, no token."""
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
                "title": "Legacy",
                "template": STRUCTURE_A,
                "report": dict(AUTHORED_CONTENT),
            },
            headers=auth_headers(author),
        )

        assert resp.status_code == 200, resp.text
        created = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == uuid.UUID(resp.json()["id"])
            )
        ).first()
        assert created.schema_version is None
        assert created.template_version_id is None

    def test_a_legacy_request_carrying_a_token_is_still_legacy(self, client, session):
        """A stray token on a request with no selector must not turn it into a V2
        report, and must not be validated against anything."""
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
                "title": "Legacy con token",
                "template": STRUCTURE_A,
                "report": dict(AUTHORED_CONTENT),
                "template_hash": "irrelevant-here",
            },
            headers=auth_headers(author),
        )

        assert resp.status_code == 200, resp.text
        created = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == uuid.UUID(resp.json()["id"])
            )
        ).first()
        assert created.schema_version is None

    def test_editing_an_existing_report_needs_no_token(self, client, session):
        """`POST /{id}/new_version` never reads the template — it re-attaches the
        report's own frozen snapshot — so the guard must not appear there. A
        template edit between two content saves is not a conflict."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        created, order = _author_saves(client, lab, author, session)
        assert created.status_code == 200, created.text
        report_id = created.json()["id"]

        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)

        saved = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json={
                "tenant_id": str(lab["tenant"].id),
                "branch_id": str(lab["branch"].id),
                "order_id": str(order.id),
                "report": {
                    **AUTHORED_CONTENT,
                    "sections": {"macroscopia": {"content": "Texto revisado."}},
                },
            },
            headers=auth_headers(author),
        )

        assert saved.status_code == 200, saved.text
        # Still the structure the report was created with: A.
        assert _stored_body(session, report_id)["rendering_snapshot"]["template"] == (
            STRUCTURE_A
        )

    def test_a_deactivated_template_still_fails_on_its_own_invariant(
        self, client, session
    ):
        """Guard ordering: the lifecycle refusal still comes first, so a
        deactivated template reports that rather than a stale token."""
        lab = _lab(session)
        author = _author(session, lab)
        lab["template"].is_active = False
        session.add(lab["template"])
        session.commit()

        resp, _ = _author_saves(
            client, lab, author, session, template_hash="a-stale-token"
        )

        assert resp.status_code == 409, resp.text
        assert "desactivada" in resp.json()["detail"]

    def test_block_a_signature_defaults_remain_server_authoritative(
        self, client, session
    ):
        """Block A: a new report's `signatureMetadata` comes from the template,
        never from the author's request. C-8 changes which template read supplies
        it, not who owns the field."""
        lab = _lab(
            session,
            template_json={
                **STRUCTURE_A,
                "signatureMetadata": {
                    "show_signature_section": True,
                    "require_digital_signature": True,
                },
            },
        )
        author = _author(session, lab)

        resp, _ = _author_saves(
            client,
            lab,
            author,
            session,
            report={
                **AUTHORED_CONTENT,
                # The author tries to turn the signature block off.
                "signatureMetadata": {
                    "show_signature_section": False,
                    "require_digital_signature": False,
                },
            },
        )

        assert resp.status_code == 200, resp.text
        body = _stored_body(session, resp.json()["id"])
        assert body["signatureMetadata"]["show_signature_section"] is True
        assert body["signatureMetadata"]["require_digital_signature"] is True

    def test_block_b_content_freeze_and_reopen_are_unchanged(self, client, session):
        """Block B's state matrix, exercised on a report created through the
        guarded path: APPROVED is frozen, reopening returns it to DRAFT, and the
        reopened report keeps the structure it was created with."""
        from app.models.enums import ReportStatus

        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        created, order = _author_saves(client, lab, author, session)
        report_id = created.json()["id"]
        report = session.get(Report, uuid.UUID(report_id))
        report.status = ReportStatus.APPROVED
        session.add(report)
        session.commit()

        frozen = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json={
                "tenant_id": str(lab["tenant"].id),
                "branch_id": str(lab["branch"].id),
                "order_id": str(order.id),
                "report": dict(AUTHORED_CONTENT),
            },
            headers=auth_headers(author),
        )
        assert frozen.status_code == 409, frozen.text

        reopened = client.post(
            f"/api/v1/reports/{report_id}/reopen", json={}, headers=auth_headers(admin)
        )
        assert reopened.status_code == 200, reopened.text
        session.refresh(report)
        assert report.status == ReportStatus.DRAFT
        assert _stored_body(session, report_id)["rendering_snapshot"]["template"] == (
            STRUCTURE_A
        )

    def test_the_guard_creates_no_template_version_row(self, client, session):
        """CEL-131-05 must not be regressed by its own fix: the token restores
        concurrency safety, not template-version semantics. Nothing is created,
        activated or read in that table."""
        lab = _lab(session)
        author, admin = _author(session, lab), _admin(session, lab)
        stale = _bootstrap(client, lab, author)["template_hash"]

        refused, _ = _author_saves(client, lab, author, session, template_hash=stale)
        assert refused.status_code == 200, refused.text  # nothing changed yet

        _administrator_edits_the_template(client, lab, admin, STRUCTURE_B)
        conflict, _ = _author_saves(client, lab, author, session, template_hash=stale)
        assert conflict.status_code == 409, conflict.text

        accepted, _ = _author_saves(client, lab, author, session)
        assert accepted.status_code == 200, accepted.text
        created = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == uuid.UUID(accepted.json()["id"])
            )
        ).first()
        # NULL provenance remains legitimate for the template_id selector.
        assert created.schema_version == 2
        assert created.template_version_id is None
