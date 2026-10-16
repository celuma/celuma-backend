"""The operator guide's synthetic example, executed for real.

`celuma-engineering/runbooks/sample-type-transfer.md` promises what the
synthetic export previews as and how a dry run behaves. This runs it, so the
instructions cannot rot silently. Synthetic data only; never a customer's
catalog.

The engineering repository is a sibling checkout (or `CELUMA_ENGINEERING_DIR`);
when it is absent the comparison with it is skipped, but the example fixture is
always exercised.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest
from sqlmodel import select

from app.models.sample_type import SampleType

from .factories import auth_headers, create_branch, create_tenant, create_user

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "catalog" / "synthetic_study_types_export.json"
ENGINEERING = pathlib.Path(
    os.environ.get("CELUMA_ENGINEERING_DIR") or pathlib.Path(__file__).resolve().parents[3] / "celuma-engineering"
)
RUNBOOK_EXAMPLE = ENGINEERING / "runbooks" / "examples" / "study-types-export.synthetic.json"

IMPORT = "/api/v1/sample-types/import/"


@pytest.fixture
def lab(session):
    tenant = create_tenant(session, name="Sintético")
    create_branch(session, tenant)
    user = create_user(session, tenant, email="op@sintetico.invalid", roles=("admin",))
    return tenant, auth_headers(user)


def document():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestSyntheticExample:
    def test_the_documented_preview(self, client, lab):
        _, headers = lab
        body = client.post(f"{IMPORT}preview", json={"document": document()}, headers=headers).json()
        assert [(i["code"], i["status"], i["is_active"], i["template_reference"]) for i in body["items"]] == [
            ("CITO", "ready", False, "none"),
            ("HISTO", "ready", True, "dropped"),
            ("IHQ", "ready", True, "none"),
            ("IHQ", "duplicate_in_source", True, "none"),
        ]

    def test_the_documented_dry_run_and_its_idempotence(self, client, session, lab):
        tenant, headers = lab
        doc = document()
        plan = client.post(f"{IMPORT}preview", json={"document": doc}, headers=headers).json()
        # abort policy refuses because of the duplicate; selecting only the ready ones is the documented path
        done = client.post(f"{IMPORT}confirm", headers=headers, json={
            "document": doc, "selected_indexes": [0, 1, 2], "on_conflict": "abort", "plan_digest": plan["plan_digest"]})
        assert done.status_code == 200, done.text
        rows = session.exec(select(SampleType).where(SampleType.tenant_id == tenant.id).order_by(SampleType.code)).all()
        assert [(r.code, r.is_active) for r in rows] == [("CITO", False), ("HISTO", True), ("IHQ", True)]
        # "repeat the import: the three created records become El código ya existe"
        # and the repeated source code stays flagged as a source duplicate.
        again = client.post(f"{IMPORT}preview", json={"document": doc}, headers=headers).json()
        assert [i["status"] for i in again["items"]] == ["code_exists"] * 3 + ["duplicate_in_source"]

    @pytest.mark.skipif(not RUNBOOK_EXAMPLE.exists(), reason="celuma-engineering is not checked out beside the backend")
    def test_the_fixture_is_the_runbooks_example(self):
        assert json.loads(RUNBOOK_EXAMPLE.read_text(encoding="utf-8")) == document()
