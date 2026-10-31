"""Sample image listing order (Céluma 1.4).

`GET /laboratory/samples/{id}/images` feeds the sample gallery and the report
image picker, and neither sorts on the client. The query had no ORDER BY, so
the order was whatever Postgres scanned first: insertion order on a fresh
table, anything after a row is rewritten or a freed slot is reused. The
contract is oldest upload first, with the image id as the tie-break.

Report figure order is unrelated: it is the stored array order of the report's
images section (specs/reports/renderer.md) and never reads this listing.
"""
import io
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from PIL import Image
from sqlalchemy import text
from sqlmodel import Session

from app.models.laboratory import SampleImage
from app.services.usage import UsageService
from tests.http.factories import (
    auth_headers,
    create_branch,
    create_order,
    create_sample,
    create_storage_object,
    create_tenant,
    create_user,
)

T0 = datetime(2026, 10, 1, 9, 0, 0)


@pytest.fixture
def lab_sample(session: Session):
    tenant = create_tenant(session, name="Lab")
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    sample = create_sample(session, tenant, branch, order)
    user = create_user(session, tenant, email="u@t.example")
    UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=0)
    session.commit()
    return {"tenant": tenant, "branch": branch, "sample": sample, "headers": auth_headers(user)}


def _image(session, ctx, label, created_at, image_id=None) -> SampleImage:
    storage = create_storage_object(session, key=f"samples/{label}.jpg", tenant=ctx["tenant"])
    img = SampleImage(
        tenant_id=ctx["tenant"].id,
        branch_id=ctx["branch"].id,
        sample_id=ctx["sample"].id,
        storage_id=storage.id,
        label=label,
        created_at=created_at,
        **({"id": image_id} if image_id is not None else {}),
    )
    session.add(img)
    session.commit()
    return img


def _listed(client, ctx):
    resp = client.get(f"/api/v1/laboratory/samples/{ctx['sample'].id}/images", headers=ctx["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()["images"]


def _png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


class TestSampleImageListingOrder:
    def test_oldest_upload_first_whatever_the_insertion_order(self, client, session, lab_sample):
        # Inserted C, A, B: a scan without ORDER BY returns them in that order.
        _image(session, lab_sample, "C", T0 + timedelta(minutes=2))
        _image(session, lab_sample, "A", T0)
        _image(session, lab_sample, "B", T0 + timedelta(minutes=1))

        assert [i["label"] for i in _listed(client, lab_sample)] == ["A", "B", "C"]

    def test_equal_timestamps_fall_back_to_the_image_id(self, client, session, lab_sample):
        high = UUID("ffffffff-0000-4000-8000-000000000000")
        low = UUID("00000000-0000-4000-8000-000000000000")
        _image(session, lab_sample, "high", T0, image_id=high)
        _image(session, lab_sample, "low", T0, image_id=low)

        assert [i["id"] for i in _listed(client, lab_sample)] == [str(low), str(high)]

    def test_uploads_keep_their_order_after_a_row_is_rewritten(self, client, session, lab_sample):
        ids = []
        for name, color in (("A.png", "red"), ("B.png", "green"), ("C.png", "blue")):
            resp = client.post(
                f"/api/v1/laboratory/samples/{lab_sample['sample'].id}/images",
                files={"file": (name, _png(color), "image/png")},
                headers=lab_sample["headers"],
            )
            assert resp.status_code == 200, resp.text
            ids.append(resp.json()["sample_image_id"])
        assert [i["id"] for i in _listed(client, lab_sample)] == ids

        # A no-op UPDATE writes a new tuple version, which a heap scan now
        # returns after B and C; the listing must not follow it.
        session.exec(text("UPDATE sample_image SET label = label WHERE id = :id").bindparams(id=ids[0]))
        session.commit()

        assert [i["id"] for i in _listed(client, lab_sample)] == ids
