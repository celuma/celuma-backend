"""Céluma 1.3, Phase 5 Block C — integrated clinical-workflow regression.

Before this module the HTTP suite exercised `/api/v1/patients/*` **not at
all**, and `/api/v1/laboratory/*` only through the sample-image, sample-state
and order-fetch paths that Phase 3/4's notification and accounting tests
happened to need. The canonical clinical workflow Block C is asked to prove —

    patient -> order -> sample -> processing/status

— together with its Céluma 1.2-compatible variants (orders without a patient,
optional patient email, the requesting-physician catalog) had no end-to-end
HTTP coverage, and neither did tenant isolation on any of those routes.

That is what this module adds. It is deliberately a *regression* suite, not a
feature suite: every assertion states behaviour the release already claims,
so a red test here means the release changed, not that a new rule was
invented.

`TestClinicalTenantIsolation` is the release-critical half. Read
`block-c-release-findings.md` (C-001) before changing anything in it.
"""
from __future__ import annotations

import io
import uuid

import pytest
from sqlmodel import Session, select

from app.models.enums import SampleState
from app.models.laboratory import Order, Sample
from app.models.patient import Patient
from app.models.study_type import StudyType
from app.models.tenant import Branch, Tenant

from .factories import auth_headers, create_branch, create_tenant, create_user


# ---------------------------------------------------------------------------
# Local factories — study types have no factory yet, and every order needs one
# ---------------------------------------------------------------------------


def create_study_type(
    session: Session, tenant: Tenant, *, code: str = "HIST", name: str = "Histopatología"
) -> StudyType:
    study_type = StudyType(tenant_id=tenant.id, code=code, name=name)
    session.add(study_type)
    session.commit()
    session.refresh(study_type)
    return study_type


@pytest.fixture(name="lab")
def lab_fixture(session: Session):
    """One tenant, one branch, one study type, one superuser — the minimum a
    clinical workflow needs."""
    tenant = create_tenant(session, name="Laboratorio Uno")
    branch = create_branch(session, tenant, code="MAIN")
    study_type = create_study_type(session, tenant)
    user = create_user(session, tenant, email="uno@example.com", roles=("superuser",))
    return {
        "tenant": tenant,
        "branch": branch,
        "study_type": study_type,
        "user": user,
        "headers": auth_headers(user),
    }


@pytest.fixture(name="other_lab")
def other_lab_fixture(session: Session):
    """A second, completely unrelated tenant. Nothing in it may ever be
    reachable from `lab`'s credentials, in either direction."""
    tenant = create_tenant(session, name="Laboratorio Dos")
    branch = create_branch(session, tenant, code="SEC")
    study_type = create_study_type(session, tenant, code="CITO", name="Citología")
    user = create_user(session, tenant, email="dos@example.com", roles=("superuser",))
    return {
        "tenant": tenant,
        "branch": branch,
        "study_type": study_type,
        "user": user,
        "headers": auth_headers(user),
    }


def _patient_payload(lab, **overrides) -> dict:
    payload = {
        "tenant_id": str(lab["tenant"].id),
        "branch_id": str(lab["branch"].id),
        "first_name": "Ana",
        "last_name": "García",
        "sex": "F",
    }
    payload.update(overrides)
    return payload


def _create_patient_via_api(client, lab, **overrides) -> dict:
    response = client.post(
        "/api/v1/patients/", json=_patient_payload(lab, **overrides), headers=lab["headers"]
    )
    assert response.status_code == 200, response.text
    return response.json()


def _order_payload(lab, **overrides) -> dict:
    payload = {
        "tenant_id": str(lab["tenant"].id),
        "branch_id": str(lab["branch"].id),
        "study_type_id": str(lab["study_type"].id),
    }
    payload.update(overrides)
    return payload


def _jpeg_bytes() -> bytes:
    """A genuinely decodable JPEG — `upload_sample_image` runs real Pillow
    processing on it to derive the processed rendition and the thumbnail."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (48, 32), color=(120, 30, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def _create_order_with_sample(client, lab, *, sample_code: str = "M-1") -> tuple[str, str]:
    """`(order_id, sample_id)` through the same unified route the frontend's
    order-registration screen uses."""
    patient = _create_patient_via_api(client, lab, patient_code=f"P-{sample_code}")
    created = client.post(
        "/api/v1/laboratory/orders/unified",
        json=_order_payload(
            lab,
            patient_id=patient["id"],
            order_code=f"ORD-{sample_code}",
            samples=[{"sample_code": sample_code, "type": "BIOPSIA"}],
        ),
        headers=lab["headers"],
    )
    assert created.status_code == 200, created.text
    return created.json()["order"]["id"], created.json()["samples"][0]["id"]


# ---------------------------------------------------------------------------
# C2 — patients
# ---------------------------------------------------------------------------


class TestPatientLifecycle:
    def test_create_read_update_and_reload(self, client, session: Session, lab):
        created = _create_patient_via_api(client, lab, email="ana@example.com")
        patient_id = created["id"]
        # The code is generated server-side when the client omits it.
        assert created["patient_code"] == "P-1"

        detail = client.get(f"/api/v1/patients/{patient_id}", headers=lab["headers"])
        assert detail.status_code == 200
        assert detail.json()["first_name"] == "Ana"
        assert detail.json()["email"] == "ana@example.com"

        updated = client.put(
            f"/api/v1/patients/{patient_id}",
            json={"first_name": "Ana María", "phone": "5555555555"},
            headers=lab["headers"],
        )
        assert updated.status_code == 200
        assert updated.json()["first_name"] == "Ana María"

        # Persistence, read back from the database rather than from the
        # response the endpoint just built.
        session.expire_all()
        row = session.get(Patient, uuid.UUID(patient_id))
        assert row.first_name == "Ana María"
        assert row.phone == "5555555555"
        assert row.full_name == "Ana María García"

    def test_patient_email_is_optional(self, client, session: Session, lab):
        """Céluma 1.2 compatibility — a patient may have no email at all."""
        created = _create_patient_via_api(client, lab)
        row = session.get(Patient, uuid.UUID(created["id"]))
        assert row.email is None

    def test_list_returns_only_this_tenants_patients(
        self, client, session: Session, lab, other_lab
    ):
        _create_patient_via_api(client, lab)
        _create_patient_via_api(client, other_lab)

        listed = client.get("/api/v1/patients/", headers=lab["headers"])
        assert listed.status_code == 200
        tenants_seen = {row["tenant_id"] for row in listed.json()}
        assert tenants_seen == {str(lab["tenant"].id)}


# ---------------------------------------------------------------------------
# C2 — orders and samples
# ---------------------------------------------------------------------------


class TestOrderAndSampleWorkflow:
    def test_order_with_patient_then_sample_then_status_progression(
        self, client, session: Session, lab
    ):
        patient = _create_patient_via_api(client, lab)

        order = client.post(
            "/api/v1/laboratory/orders/",
            json=_order_payload(lab, patient_id=patient["id"]),
            headers=lab["headers"],
        )
        assert order.status_code == 200, order.text
        order_body = order.json()
        # Code generated from the study type's own code.
        assert order_body["order_code"] == "HIST-1"
        assert order_body["patient_id"] == patient["id"]

        sample = client.post(
            "/api/v1/laboratory/samples/",
            json={
                "tenant_id": str(lab["tenant"].id),
                "branch_id": str(lab["branch"].id),
                "order_id": order_body["id"],
                "sample_code": "M-1",
                "type": "BIOPSIA",
            },
            headers=lab["headers"],
        )
        assert sample.status_code == 200, sample.text
        sample_body = sample.json()
        assert sample_body["state"] == SampleState.RECEIVED.value

        for target in (SampleState.PROCESSING, SampleState.READY):
            moved = client.patch(
                f"/api/v1/laboratory/samples/{sample_body['id']}/state",
                json={"state": target.value},
                headers=lab["headers"],
            )
            assert moved.status_code == 200, moved.text
            assert moved.json()["state"] == target.value

        session.expire_all()
        row = session.get(Sample, uuid.UUID(sample_body["id"]))
        assert row.state == SampleState.READY

    def test_order_without_patient_uses_requesting_physician(
        self, client, session: Session, lab
    ):
        """Céluma 1.2 compatibility — an order may carry a requesting
        physician instead of a patient."""
        physician = client.post(
            "/api/v1/requesting-physicians/",
            json={
                "branch_id": str(lab["branch"].id),
                "first_name": "Luis",
                "last_name": "Hernández",
            },
            headers=lab["headers"],
        )
        assert physician.status_code == 200, physician.text

        order = client.post(
            "/api/v1/laboratory/orders/",
            json=_order_payload(lab, requesting_physician_id=physician.json()["id"]),
            headers=lab["headers"],
        )
        assert order.status_code == 200, order.text
        assert order.json()["patient_id"] is None

        session.expire_all()
        row = session.get(Order, uuid.UUID(order.json()["id"]))
        assert row.patient_id is None
        assert str(row.requesting_physician_id) == physician.json()["id"]
        # `requested_by` is derived from the physician when not supplied.
        assert row.requested_by == "Luis Hernández"

    def test_order_requires_patient_or_physician(self, client, lab):
        response = client.post(
            "/api/v1/laboratory/orders/", json=_order_payload(lab), headers=lab["headers"]
        )
        assert response.status_code == 400
        assert "physician" in response.json()["detail"].lower()

    def test_unified_order_creates_order_and_samples_atomically(
        self, client, session: Session, lab
    ):
        patient = _create_patient_via_api(client, lab)
        response = client.post(
            "/api/v1/laboratory/orders/unified",
            json=_order_payload(
                lab,
                patient_id=patient["id"],
                samples=[
                    {"sample_code": "M-1", "type": "BIOPSIA"},
                    {"sample_code": "M-2", "type": "BIOPSIA"},
                ],
            ),
            headers=lab["headers"],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["samples"]) == 2
        assert {s["state"] for s in body["samples"]} == {SampleState.RECEIVED.value}

        session.expire_all()
        stored = session.exec(
            select(Sample).where(Sample.order_id == uuid.UUID(body["order"]["id"]))
        ).all()
        assert {s.sample_code for s in stored} == {"M-1", "M-2"}

    def test_samples_list_is_scoped_to_the_callers_tenant(
        self, client, session: Session, lab, other_lab
    ):
        for context in (lab, other_lab):
            patient = _create_patient_via_api(client, context)
            client.post(
                "/api/v1/laboratory/orders/unified",
                json=_order_payload(
                    context,
                    patient_id=patient["id"],
                    samples=[{"sample_code": "M-1", "type": "BIOPSIA"}],
                ),
                headers=context["headers"],
            )

        listed = client.get("/api/v1/laboratory/samples/", headers=lab["headers"])
        assert listed.status_code == 200
        tenants_seen = {row["tenant_id"] for row in listed.json()["samples"]}
        assert tenants_seen == {str(lab["tenant"].id)}


# ---------------------------------------------------------------------------
# C3 — sample images and the automatic state transition they cause
# ---------------------------------------------------------------------------


class TestSampleImageWorkflow:
    """The automatic `RECEIVED -> PROCESSING` transition on the first image.

    `sample-status-transition-contract.md` §1 states the rule, and the
    pre-release remediation verified it by hand against a live API — no
    automated test held it. The Block C brief names exactly this behaviour
    ("image upload must not accidentally perform an unrelated status
    transition"), so it is locked here.
    """

    def test_the_first_image_moves_received_to_processing(
        self, client, session: Session, lab
    ):
        _, sample_id = _create_order_with_sample(client, lab)

        response = client.post(
            f"/api/v1/laboratory/samples/{sample_id}/images",
            files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
            headers=lab["headers"],
        )
        assert response.status_code == 200, response.text

        session.expire_all()
        assert session.get(Sample, uuid.UUID(sample_id)).state == SampleState.PROCESSING

    def test_a_second_image_does_not_change_the_state_again(
        self, client, session: Session, lab
    ):
        _, sample_id = _create_order_with_sample(client, lab, sample_code="M-2")

        for _ in range(2):
            assert (
                client.post(
                    f"/api/v1/laboratory/samples/{sample_id}/images",
                    files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
                    headers=lab["headers"],
                ).status_code
                == 200
            )

        # Manually move it on, then upload again: the automatic path is gated
        # on `is_first_image` *and* `state == RECEIVED`, so it must not drag
        # a deliberately-set state backwards.
        assert (
            client.patch(
                f"/api/v1/laboratory/samples/{sample_id}/state",
                json={"state": SampleState.READY.value},
                headers=lab["headers"],
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/laboratory/samples/{sample_id}/images",
                files={"file": ("b.jpg", _jpeg_bytes(), "image/jpeg")},
                headers=lab["headers"],
            ).status_code
            == 200
        )

        session.expire_all()
        assert session.get(Sample, uuid.UUID(sample_id)).state == SampleState.READY

    def test_an_image_uploaded_to_a_non_received_sample_leaves_the_state_alone(
        self, client, session: Session, lab
    ):
        _, sample_id = _create_order_with_sample(client, lab, sample_code="M-3")
        assert (
            client.patch(
                f"/api/v1/laboratory/samples/{sample_id}/state",
                json={"state": SampleState.DAMAGED.value},
                headers=lab["headers"],
            ).status_code
            == 200
        )

        assert (
            client.post(
                f"/api/v1/laboratory/samples/{sample_id}/images",
                files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
                headers=lab["headers"],
            ).status_code
            == 200
        )

        session.expire_all()
        assert session.get(Sample, uuid.UUID(sample_id)).state == SampleState.DAMAGED

    def test_images_are_listed_and_deleted_within_the_owning_tenant(
        self, client, session: Session, lab
    ):
        _, sample_id = _create_order_with_sample(client, lab, sample_code="M-4")
        upload = client.post(
            f"/api/v1/laboratory/samples/{sample_id}/images",
            files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
            headers=lab["headers"],
        )
        assert upload.status_code == 200
        image_id = upload.json()["sample_image_id"]

        listed = client.get(
            f"/api/v1/laboratory/samples/{sample_id}/images", headers=lab["headers"]
        )
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["images"]] == [image_id]
        # Processed + thumbnail renditions both resolve to a URL.
        assert {"processed", "thumbnail"} <= set(listed.json()["images"][0]["urls"])

        deleted = client.delete(
            f"/api/v1/laboratory/samples/{sample_id}/images/{image_id}",
            headers=lab["headers"],
        )
        assert deleted.status_code == 200, deleted.text
        after = client.get(
            f"/api/v1/laboratory/samples/{sample_id}/images", headers=lab["headers"]
        )
        assert after.json()["images"] == []


# ---------------------------------------------------------------------------
# C1 — role authorisation on the clinical routes
# ---------------------------------------------------------------------------


class TestClinicalRoleAuthorization:
    @pytest.mark.parametrize(
        "role, can_create_patient, can_create_order, can_create_sample",
        [
            ("admin", True, True, True),
            ("assistant", True, True, True),
            ("lab_tech", False, False, True),
            ("pathologist", False, False, False),
            ("viewer", False, False, False),
        ],
    )
    def test_role_permissions_on_creation_routes(
        self,
        client,
        session: Session,
        lab,
        role,
        can_create_patient,
        can_create_order,
        can_create_sample,
    ):
        actor = create_user(session, lab["tenant"], email=f"{role}@example.com", roles=(role,))
        headers = auth_headers(actor)

        patient_response = client.post(
            "/api/v1/patients/", json=_patient_payload(lab), headers=headers
        )
        assert (patient_response.status_code == 200) is can_create_patient, patient_response.text

        seeded_patient = _create_patient_via_api(client, lab, patient_code=f"SEED-{role}")
        order_response = client.post(
            "/api/v1/laboratory/orders/",
            json=_order_payload(lab, patient_id=seeded_patient["id"], order_code=f"ORD-{role}"),
            headers=headers,
        )
        assert (order_response.status_code == 200) is can_create_order, order_response.text

        seeded_order = client.post(
            "/api/v1/laboratory/orders/",
            json=_order_payload(
                lab, patient_id=seeded_patient["id"], order_code=f"SEEDORD-{role}"
            ),
            headers=lab["headers"],
        )
        assert seeded_order.status_code == 200, seeded_order.text
        sample_response = client.post(
            "/api/v1/laboratory/samples/",
            json={
                "tenant_id": str(lab["tenant"].id),
                "branch_id": str(lab["branch"].id),
                "order_id": seeded_order.json()["id"],
                "sample_code": f"M-{role}",
                "type": "BIOPSIA",
            },
            headers=headers,
        )
        assert (sample_response.status_code == 200) is can_create_sample, sample_response.text

    def test_every_clinical_route_rejects_an_unauthenticated_caller(self, client, lab):
        for method, path in (
            ("get", "/api/v1/patients/"),
            ("post", "/api/v1/patients/"),
            ("get", "/api/v1/laboratory/orders/"),
            ("post", "/api/v1/laboratory/orders/"),
            ("get", "/api/v1/laboratory/samples/"),
            ("post", "/api/v1/laboratory/samples/"),
        ):
            kwargs = {"json": {}} if method == "post" else {}
            response = client.request(method.upper(), path, **kwargs)
            assert response.status_code in (401, 403), f"{method} {path} -> {response.status_code}"

    def test_a_structurally_invalid_token_is_rejected(self, client, lab):
        response = client.get(
            "/api/v1/patients/", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# C1 — tenant isolation on the clinical routes
# ---------------------------------------------------------------------------


class TestClinicalTenantIsolation:
    """Reads and writes, both directions, on every clinical resource.

    A caller authenticated in tenant A must not be able to observe or modify
    anything in tenant B — regardless of what identifiers it puts in the path
    or in the request body. The body cases matter most: the path cases fail
    closed because the handler compares the *stored* row's `tenant_id`
    against the context, while the creation handlers historically trusted the
    `tenant_id` the client sent.
    """

    def test_cross_tenant_patient_read_is_not_found(
        self, client, session: Session, lab, other_lab
    ):
        foreign = _create_patient_via_api(client, other_lab)
        response = client.get(f"/api/v1/patients/{foreign['id']}", headers=lab["headers"])
        assert response.status_code == 404

    def test_cross_tenant_patient_write_is_not_found(
        self, client, session: Session, lab, other_lab
    ):
        foreign = _create_patient_via_api(client, other_lab)
        response = client.put(
            f"/api/v1/patients/{foreign['id']}",
            json={"first_name": "Sobrescrito"},
            headers=lab["headers"],
        )
        assert response.status_code == 404

        session.expire_all()
        assert session.get(Patient, uuid.UUID(foreign["id"])).first_name != "Sobrescrito"

    def test_cross_tenant_order_read_is_not_found(self, client, lab, other_lab):
        patient = _create_patient_via_api(client, other_lab)
        foreign_order = client.post(
            "/api/v1/laboratory/orders/",
            json=_order_payload(other_lab, patient_id=patient["id"]),
            headers=other_lab["headers"],
        )
        assert foreign_order.status_code == 200

        response = client.get(
            f"/api/v1/laboratory/orders/{foreign_order.json()['id']}", headers=lab["headers"]
        )
        assert response.status_code == 404

    def test_cross_tenant_sample_read_and_state_write_are_not_found(
        self, client, session: Session, lab, other_lab
    ):
        patient = _create_patient_via_api(client, other_lab)
        created = client.post(
            "/api/v1/laboratory/orders/unified",
            json=_order_payload(
                other_lab,
                patient_id=patient["id"],
                samples=[{"sample_code": "M-1", "type": "BIOPSIA"}],
            ),
            headers=other_lab["headers"],
        )
        assert created.status_code == 200, created.text
        foreign_sample_id = created.json()["samples"][0]["id"]

        read = client.get(
            f"/api/v1/laboratory/samples/{foreign_sample_id}", headers=lab["headers"]
        )
        assert read.status_code == 404

        write = client.patch(
            f"/api/v1/laboratory/samples/{foreign_sample_id}/state",
            json={"state": SampleState.CANCELLED.value},
            headers=lab["headers"],
        )
        assert write.status_code == 404

        session.expire_all()
        assert session.get(Sample, uuid.UUID(foreign_sample_id)).state == SampleState.RECEIVED

    def test_a_patient_cannot_be_created_into_another_tenant(
        self, client, session: Session, lab, other_lab
    ):
        response = client.post(
            "/api/v1/patients/",
            json=_patient_payload(other_lab),
            headers=lab["headers"],
        )
        assert response.status_code == 403

        session.expire_all()
        assert (
            session.exec(
                select(Patient).where(Patient.tenant_id == other_lab["tenant"].id)
            ).all()
            == []
        )

    def test_an_order_cannot_be_created_into_another_tenant(
        self, client, session: Session, lab, other_lab
    ):
        """`POST /laboratory/orders/` must not honour a foreign `tenant_id`.

        Block C finding C-001. The handler validated the *body's* tenant
        against the body's branch/patient/study type and never against
        `AuthContext.tenant_id`, so a caller holding `lab:create_order` in
        tenant A could write an order row owned by tenant B.
        """
        foreign_patient = _create_patient_via_api(client, other_lab)
        response = client.post(
            "/api/v1/laboratory/orders/",
            json=_order_payload(other_lab, patient_id=foreign_patient["id"]),
            headers=lab["headers"],
        )
        assert response.status_code == 403, response.text

        session.expire_all()
        assert (
            session.exec(select(Order).where(Order.tenant_id == other_lab["tenant"].id)).all()
            == []
        )

    def test_a_sample_cannot_be_created_into_another_tenant(
        self, client, session: Session, lab, other_lab
    ):
        """`POST /laboratory/samples/` — same finding, sample half."""
        foreign_patient = _create_patient_via_api(client, other_lab)
        foreign_order = client.post(
            "/api/v1/laboratory/orders/",
            json=_order_payload(other_lab, patient_id=foreign_patient["id"]),
            headers=other_lab["headers"],
        )
        assert foreign_order.status_code == 200

        response = client.post(
            "/api/v1/laboratory/samples/",
            json={
                "tenant_id": str(other_lab["tenant"].id),
                "branch_id": str(other_lab["branch"].id),
                "order_id": foreign_order.json()["id"],
                "sample_code": "INTRUSO-1",
                "type": "BIOPSIA",
            },
            headers=lab["headers"],
        )
        assert response.status_code == 403, response.text

        session.expire_all()
        assert (
            session.exec(select(Sample).where(Sample.sample_code == "INTRUSO-1")).all() == []
        )

    def test_a_unified_order_cannot_be_created_into_another_tenant(
        self, client, session: Session, lab, other_lab
    ):
        """`POST /laboratory/orders/unified` — same finding, and the path the
        frontend's order-registration screen actually uses."""
        foreign_patient = _create_patient_via_api(client, other_lab)
        response = client.post(
            "/api/v1/laboratory/orders/unified",
            json=_order_payload(
                other_lab,
                patient_id=foreign_patient["id"],
                samples=[{"sample_code": "INTRUSO-U1", "type": "BIOPSIA"}],
            ),
            headers=lab["headers"],
        )
        assert response.status_code == 403, response.text

        session.expire_all()
        assert (
            session.exec(select(Order).where(Order.tenant_id == other_lab["tenant"].id)).all()
            == []
        )

    def test_a_foreign_branch_cannot_be_used_inside_the_callers_own_tenant(
        self, client, session: Session, lab, other_lab
    ):
        """The mirror case: the caller's own `tenant_id`, someone else's
        branch. The row would land in the caller's tenant while pointing at a
        branch it does not own."""
        patient = _create_patient_via_api(client, lab)
        response = client.post(
            "/api/v1/laboratory/orders/",
            json=_order_payload(
                lab, patient_id=patient["id"], branch_id=str(other_lab["branch"].id)
            ),
            headers=lab["headers"],
        )
        assert response.status_code == 403, response.text

        session.expire_all()
        assert (
            session.exec(
                select(Order).where(Order.branch_id == other_lab["branch"].id)
            ).all()
            == []
        )

    def test_a_sample_image_cannot_be_uploaded_into_another_tenant(
        self, client, session: Session, lab, other_lab
    ):
        """`POST /laboratory/samples/{id}/images` — the same missing guard.

        This one also mis-attributes storage: the upload's `StorageObject`
        rows and the `TenantUsage` delta are booked against the *victim*
        tenant, so a foreign write shows up on someone else's bill.
        """
        _, foreign_sample_id = _create_order_with_sample(client, other_lab, sample_code="X-1")

        response = client.post(
            f"/api/v1/laboratory/samples/{foreign_sample_id}/images",
            files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
            headers=lab["headers"],
        )
        assert response.status_code == 404, response.text

        session.expire_all()
        assert session.get(Sample, uuid.UUID(foreign_sample_id)).state == SampleState.RECEIVED

    def test_another_tenants_sample_images_cannot_be_listed(
        self, client, lab, other_lab
    ):
        """`GET /laboratory/samples/{id}/images` — clinical images are patient
        data; the list must not resolve across a tenant boundary."""
        _, foreign_sample_id = _create_order_with_sample(client, other_lab, sample_code="X-2")
        assert (
            client.post(
                f"/api/v1/laboratory/samples/{foreign_sample_id}/images",
                files={"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")},
                headers=other_lab["headers"],
            ).status_code
            == 200
        )

        response = client.get(
            f"/api/v1/laboratory/samples/{foreign_sample_id}/images",
            headers=lab["headers"],
        )
        assert response.status_code == 404, response.text

    def test_a_foreign_study_type_is_still_rejected(self, client, lab, other_lab):
        """Pre-existing guard, asserted so it cannot regress silently."""
        patient = _create_patient_via_api(client, lab)
        response = client.post(
            "/api/v1/laboratory/orders/",
            json=_order_payload(
                lab, patient_id=patient["id"], study_type_id=str(other_lab["study_type"].id)
            ),
            headers=lab["headers"],
        )
        assert response.status_code == 403
