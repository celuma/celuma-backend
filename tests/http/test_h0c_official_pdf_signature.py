"""H-0c Blocker B — the official PDF must carry the autograph, and must never
misrepresent a signed report.

ROOT CAUSE. `sign-and-publish` runs:

    claim -> embed signature URL -> GENERATE THE OFFICIAL PDF -> finalize

and `finalize_publish` is what sets `signed_by`/`signed_at`. So the official
PDF was always rendered in the window BEFORE the version was marked signed.
`GET /reports/internal/render-data/...` therefore served `signed_at: None`, and
`SignatureBlock` derives `isSigned` from `signed_at` and refuses to draw the
autograph without it. The official PDF came out with no signature and no
"Firmado digitalmente el ..." caption — deterministically, every time, in every
environment. Reopening the report afterwards showed the autograph, because by
then finalize had run. That is exactly the reported local-vs-official
asymmetry, and it is why a fixture-based test could not reproduce it: fixtures
carry `signed_at`, i.e. they model the state AFTER publication, never the state
the official renderer actually sees.

THE FIX has two halves that must agree:

  * `finalize_publish` stores the publish CLAIM instant as `signed_at`, rather
    than whatever time PDF rendering happened to finish;
  * `get_internal_render_data` reports that same claim as the effective
    signature state while a publication is in flight — scoped to that endpoint,
    so the public report API still reports a mid-publication report as unsigned.

The PDF and the database therefore show the identical timestamp.

Also covered here:

  * the fail-safe for a required autograph that cannot be drawn (a real but
    SECONDARY robustness gap found while investigating: the render route
    refuses instead of publishing a signed-looking PDF with an empty box);
  * `POST /{id}/sign`, which required a PDF generated before signing and never
    regenerated it — a live authenticated API that could publish a permanently
    inconsistent artifact.
"""
import base64
import json

import pytest

from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion
from app.models.storage import StorageObject
from app.services.report_pdf_generation import ReportPdfGenerationError

from .conftest import FakeS3Service, make_pdf_bytes
from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_storage_object,
    create_tenant,
    create_user,
)

# A real 1x1 PNG — the fake S3 stores real bytes, so this stays a real image.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _report_body(*, show=True, require_digital=True):
    return {
        "base": {"diagnosis": {"label": "Diagnóstico", "value": "Benigno"}},
        "sections": {},
        "base_order": ["diagnosis"],
        "section_order": [],
        "signatureMetadata": {
            "show_signature_section": show,
            "require_digital_signature": require_digital,
        },
    }


def _approved_report(session, tenant, branch, order, *, show=True, require_digital=True):
    """An APPROVED report whose JSON body already lives in (fake) S3, which is
    what `embed_signature_metadata_if_required` reads and rewrites."""
    s3 = FakeS3Service()
    key = f"reports/{order.id}/v1.json"
    body = json.dumps(_report_body(show=show, require_digital=require_digital)).encode()
    info = s3.upload_bytes(body, key=key, content_type="application/json")
    storage = StorageObject(
        provider="aws",
        region="mx-test-1",
        bucket="celuma-test-bucket",
        object_key=key,
        content_type="application/json",
        size_bytes=info.size_bytes,
        tenant_id=tenant.id,
    )
    session.add(storage)
    session.flush()

    report = Report(
        tenant_id=tenant.id,
        branch_id=branch.id,
        order_id=order.id,
        status=ReportStatus.APPROVED,
    )
    session.add(report)
    session.flush()
    version = ReportVersion(
        report_id=report.id, version_no=1, is_current=True, json_storage_id=storage.id
    )
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version, storage


def _signer(session, tenant, *, with_autograph=True, email="signer@a.example",
            tenant_for_storage=None, roles=("reviewer",)):
    """A reviewer — the only role allowed to sign."""
    user = create_user(session, tenant, email=email, roles=roles)
    if with_autograph:
        key = f"signatures/{user.id}.png"
        FakeS3Service().upload_bytes(PNG_1X1, key=key, content_type="image/png")
        sig = create_storage_object(
            session, key=key, tenant=(tenant_for_storage or tenant)
        )
        user.signature_storage_id = sig.id
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def _persisted(storage) -> dict:
    return json.loads(FakeS3Service().download_text(storage.object_key))


class TestSignatureEmbedding:
    """The payload half — what the official renderer is handed."""

    def test_required_signature_is_embedded_and_the_report_publishes(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant)
        report, _, storage = _approved_report(session, tenant, branch, order)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == ReportStatus.PUBLISHED

        meta = _persisted(storage)["signatureMetadata"]
        assert meta["require_digital_signature"] is True
        assert meta["signature_url"], "the autograph URL was never embedded"

        # And the OFFICIAL renderer reads exactly this, through the same
        # builder the preview uses.
        detail = client.get(
            f"/api/v1/reports/{report.id}", headers=auth_headers(signer)
        ).json()
        assert detail["report"]["signatureMetadata"]["signature_url"] == meta["signature_url"]
        assert detail["signed_at"] is not None

    def test_no_autograph_is_embedded_when_none_is_required(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant)
        report, _, storage = _approved_report(
            session, tenant, branch, order, require_digital=False
        )

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        )
        assert resp.status_code == 200, resp.text

        meta = _persisted(storage)["signatureMetadata"]
        assert "signature_url" not in meta
        # Legitimately signed, just not digitally — this must never be
        # confused with a failure.
        assert meta["require_digital_signature"] is False

    def test_publication_is_refused_when_the_signer_has_no_autograph(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant, with_autograph=False)
        report, _, _ = _approved_report(session, tenant, branch, order)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        )

        assert resp.status_code == 422, resp.text
        session.refresh(report)
        assert report.status == ReportStatus.APPROVED
        assert report.published_at is None
        # No PDF was produced from an unsignable report.
        assert stub_pdf_render.call_count == 0


class TestRenderRefusalIsHonoured:
    """The render half — a required autograph that cannot be drawn must fail
    publication rather than produce a silent, incomplete clinical artifact."""

    def test_a_signature_render_failure_leaves_the_report_approved(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant)
        report, _, _ = _approved_report(session, tenant, branch, order)

        # Exactly what the generator now raises when the render route reports
        # `data-report-render-error`.
        stub_pdf_render.fail(
            ReportPdfGenerationError(
                "SIGNATURE_RENDER_FAILED",
                "The report requires a digital signature but its autograph could "
                "not be rendered (SIGNATURE_NOT_LOADED)",
            )
        )

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        )

        assert resp.status_code == 422, resp.text
        assert "autograph" in resp.text

        session.refresh(report)
        # Retryable, not published: the operator can fix the cause and re-run.
        assert report.status == ReportStatus.APPROVED
        assert report.published_at is None

    def test_the_publish_claim_is_released_so_a_retry_is_possible(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant)
        report, _, _ = _approved_report(session, tenant, branch, order)

        stub_pdf_render.fail(
            ReportPdfGenerationError("SIGNATURE_RENDER_FAILED", "autograph failed")
        )
        first = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        )
        assert first.status_code == 422

        # A failed attempt must not wedge the report behind a stale claim.
        stub_pdf_render.succeed(make_pdf_bytes(1))
        second = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        )
        assert second.status_code == 200, second.text
        assert second.json()["status"] == ReportStatus.PUBLISHED


class TestSignatureTenantIsolation:
    """§13 — widening nothing: the autograph resolved for a report must belong
    to that report's tenant."""

    def test_the_embedded_autograph_belongs_to_the_reports_tenant(
        self, client, session, stub_pdf_render
    ):
        tenant_a = create_tenant(session, name="Tenant A")
        branch_a = create_branch(session, tenant_a)
        order_a = create_order(session, tenant_a, branch_a)
        signer_a = _signer(session, tenant_a, email="a@a.example")
        report, _, storage = _approved_report(session, tenant_a, branch_a, order_a)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer_a),
        )
        assert resp.status_code == 200, resp.text

        url = _persisted(storage)["signatureMetadata"]["signature_url"]
        # The autograph is the signer's own object, and the signer is a user of
        # this tenant — never another tenant's storage key.
        assert str(signer_a.id) in url

    def test_a_signer_from_another_tenant_cannot_sign_the_report(
        self, client, session, stub_pdf_render
    ):
        tenant_a = create_tenant(session, name="Tenant A")
        branch_a = create_branch(session, tenant_a)
        order_a = create_order(session, tenant_a, branch_a)
        tenant_b = create_tenant(session, name="Tenant B")
        signer_b = _signer(session, tenant_b, email="b@b.example")
        report, _, _ = _approved_report(session, tenant_a, branch_a, order_a)

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer_b),
        )

        # Cross-tenant: the report is not visible to tenant B at all.
        assert resp.status_code in (403, 404), resp.text
        session.refresh(report)
        assert report.status == ReportStatus.APPROVED
        assert stub_pdf_render.call_count == 0


class TestTheRendererSeesASignedReport:
    """The root cause, pinned at the exact boundary where it occurred: what the
    official renderer is served AT THE MOMENT the PDF is captured."""

    def _capture_render_state(self, client, session, monkeypatch, report, version):
        """Runs the real pipeline and, at render time, calls the real
        render-data endpoint with a real render token — the same route,
        credential and instant as headless Chromium."""
        from app.services.report_pdf_generation import ReportPdfGenerationService
        from app.core.security import create_render_token

        captured: dict = {}

        def fake_render(self_svc, rep, ver):
            token = create_render_token(str(ver.id), str(rep.tenant_id), 120)
            resp = client.get(
                f"/api/v1/reports/internal/render-data/{rep.id}/{ver.version_no}",
                headers={"Authorization": f"Bearer {token}"},
            )
            body = resp.json()
            meta = (body.get("report") or {}).get("signatureMetadata", {})
            captured.update(
                status=resp.status_code,
                signed_at=body.get("signed_at"),
                signed_by=body.get("signed_by"),
                signature_url=meta.get("signature_url"),
                require_digital=meta.get("require_digital_signature"),
            )
            return make_pdf_bytes(1)

        monkeypatch.setattr(ReportPdfGenerationService, "_render_pdf", fake_render)
        return captured

    def test_the_official_renderer_is_told_the_report_is_signed(
        self, client, session, monkeypatch
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant)
        report, version, _ = _approved_report(session, tenant, branch, order)

        captured = self._capture_render_state(
            client, session, monkeypatch, report, version
        )
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        )
        assert resp.status_code == 200, resp.text

        assert captured["status"] == 200
        # Before the fix this was None, and SignatureBlock drew nothing.
        assert captured["signed_at"] is not None, (
            "the official renderer was served an UNSIGNED report, so the "
            "autograph could never be drawn"
        )
        assert captured["signed_by"] == str(signer.id)
        assert captured["require_digital"] is True
        assert captured["signature_url"]

    def test_the_pdf_and_the_database_agree_on_the_signing_instant(
        self, client, session, monkeypatch
    ):
        """A rendered timestamp that differs from the stored one would be a
        different defect of the same family."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant)
        report, version, _ = _approved_report(session, tenant, branch, order)

        captured = self._capture_render_state(
            client, session, monkeypatch, report, version
        )
        assert client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        ).status_code == 200

        session.refresh(version)
        assert version.signed_at is not None
        rendered = captured["signed_at"].replace("Z", "")
        assert rendered.startswith(version.signed_at.isoformat()[:19]), (
            f"PDF showed {rendered}, database stored {version.signed_at}"
        )

    def test_the_public_api_still_reports_an_unpublished_report_as_unsigned(
        self, client, session, monkeypatch
    ):
        """The in-flight signature state is exposed ONLY to the render route.
        Widening it to the public API would make a report claim to be signed
        before it is."""
        from app.services.report_pdf_generation import ReportPdfGenerationService

        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant)
        report, version, _ = _approved_report(session, tenant, branch, order)

        seen: dict = {}

        def fake_render(self_svc, rep, ver):
            body = client.get(
                f"/api/v1/reports/{rep.id}", headers=auth_headers(signer)
            ).json()
            seen["signed_at"] = body.get("signed_at")
            seen["status"] = body.get("status")
            return make_pdf_bytes(1)

        monkeypatch.setattr(ReportPdfGenerationService, "_render_pdf", fake_render)
        assert client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        ).status_code == 200

        assert seen["signed_at"] is None
        assert seen["status"] == ReportStatus.APPROVED


class TestResidualSignEndpoint:
    """§4 — `POST /{id}/sign` is a live, non-deprecated, authenticated API. It
    must not be able to publish an artifact whose PDF predates its signature."""

    def _ready_pdf(self, client, session, report, version, actor, stub_pdf_render):
        """A genuine pre-signature official PDF, produced through the real
        `generate-pdf` endpoint — the only state `POST /sign` accepts, and the
        state it used to publish as-is. A DB check constraint
        (`ck_report_version_pdf_ready_requires_artifact`) rightly forbids
        faking READY without an artifact."""
        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/versions/{version.version_no}/generate-pdf",
            json={},
            headers=auth_headers(actor),
        )
        assert resp.status_code == 200, resp.text
        session.refresh(version)
        assert version.pdf_generation_status == "READY"
        return stub_pdf_render.call_count

    def test_it_regenerates_the_official_pdf_after_embedding_the_signature(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant, roles=("pathologist", "reviewer"))
        report, version, storage = _approved_report(session, tenant, branch, order)
        before = self._ready_pdf(client, session, report, version, signer, stub_pdf_render)

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign",
            json={},
            headers=auth_headers(signer),
        )
        assert resp.status_code == 200, resp.text

        # The pre-signature PDF must NOT have been published as-is.
        assert stub_pdf_render.call_count == before + 1, (
            "the stale pre-signature PDF was published without regeneration"
        )
        assert _persisted(storage)["signatureMetadata"]["signature_url"]
        session.refresh(report)
        assert report.status == ReportStatus.PUBLISHED

    def test_a_failed_regeneration_does_not_publish(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant, roles=("pathologist", "reviewer"))
        report, version, _ = _approved_report(session, tenant, branch, order)
        self._ready_pdf(client, session, report, version, signer, stub_pdf_render)

        stub_pdf_render.fail(
            ReportPdfGenerationError("SIGNATURE_RENDER_FAILED", "autograph failed")
        )
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign",
            json={},
            headers=auth_headers(signer),
        )

        assert resp.status_code == 422, resp.text
        session.refresh(report)
        assert report.status == ReportStatus.APPROVED
        assert report.published_at is None

    def test_it_still_refuses_without_a_generated_pdf(self, client, session):
        """The endpoint's own precondition is unchanged."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = _signer(session, tenant)
        report, _, _ = _approved_report(session, tenant, branch, order)

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign",
            json={},
            headers=auth_headers(signer),
        )
        assert resp.status_code == 422, resp.text

    def test_it_still_requires_the_reviewer_role(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        pathologist = create_user(
            session, tenant, email="path@a.example", roles=("pathologist",)
        )
        report, version, _ = _approved_report(session, tenant, branch, order)

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign",
            json={},
            headers=auth_headers(pathologist),
        )
        # Role gate fires before any PDF precondition.
        assert resp.status_code == 403, resp.text

    def test_another_tenants_reviewer_cannot_sign(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A")
        branch_a = create_branch(session, tenant_a)
        order_a = create_order(session, tenant_a, branch_a)
        tenant_b = create_tenant(session, name="Tenant B")
        signer_b = _signer(session, tenant_b, email="b@b.example")
        report, version, _ = _approved_report(session, tenant_a, branch_a, order_a)

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign",
            json={},
            headers=auth_headers(signer_b),
        )
        assert resp.status_code in (403, 404), resp.text
        session.refresh(report)
        assert report.status == ReportStatus.APPROVED
