"""Shared fixtures/helpers for the monthly numbering HTTP tests (issue #8)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlmodel import Session

from app.core.db import get_session
from app.main import app
from app.models.study_type import StudyType
from app.services import numbering

from .keyed_client import KeyedClient
from .factories import auth_headers, create_branch, create_patient, create_sample_type, create_tenant, create_user

SAMPLES = "/api/v1/laboratory/samples/"
ORDERS = "/api/v1/laboratory/orders/"
UNIFIED = "/api/v1/laboratory/orders/unified"
NUMBERING = "/api/v1/numbering"


class Lab:
    """One tenant with a branch, a superuser, study types and sample types."""

    def __init__(self, session: Session, name="Alfa", *, roles=("superuser",), study="CU", sample_types=("BX",)):
        self.session = session
        self.tenant = create_tenant(session, name=name)
        self.branch = create_branch(session, self.tenant)
        self.user = create_user(session, self.tenant, email=f"u@{name.lower()}.invalid", roles=roles)
        self.headers = auth_headers(self.user)
        self.patient = create_patient(session, self.tenant, self.branch)
        self.study_type = self.add_study_type(study)
        self.sample_types = {code: create_sample_type(session, self.tenant, code=code) for code in sample_types}

    def add_study_type(self, code: str) -> StudyType:
        st = StudyType(tenant_id=self.tenant.id, code=code, name=f"Estudio {code}")
        self.session.add(st)
        self.session.commit()
        self.session.refresh(st)
        return st

    def add_sample_type(self, code: str):
        st = create_sample_type(self.session, self.tenant, code=code)
        self.sample_types[code] = st
        return st

    def headers_for(self, role: str) -> dict:
        user = create_user(self.session, self.tenant, email=f"{role}@{self.tenant.id}.invalid", roles=(role,))
        return auth_headers(user)

    def order_payload(self, study_type=None, **extra) -> dict:
        return {
            "tenant_id": str(self.tenant.id), "branch_id": str(self.branch.id),
            "study_type_id": str((study_type or self.study_type).id),
            "patient_id": str(self.patient.id), "requested_by": "Dr. Sintético", **extra,
        }

    def unified(self, sample_codes=("BX",), study_type=None, **extra) -> dict:
        """A unified registration payload. `sample_codes` are the sample TYPE codes of the samples (the
        codes themselves are always generated; registration accepts none)."""
        samples = [{"sample_type_id": str(self.sample_types[c].id)} for c in sample_codes]
        return {**self.order_payload(study_type), "samples": samples, **extra}

    def sample_payload(self, order_id, type_code="BX", **extra) -> dict:
        return {
            "tenant_id": str(self.tenant.id), "branch_id": str(self.branch.id), "order_id": str(order_id),
            "sample_type_id": str(self.sample_types[type_code].id), **extra,
        }


def freeze(monkeypatch, year=2026, month=10, day=15, hour=18, minute=0):
    """Pin the allocation clock (UTC)."""
    state = {"now": datetime(year, month, day, hour, minute, tzinfo=timezone.utc)}
    monkeypatch.setattr(numbering, "_now", lambda: state["now"])
    return state


NIL = "00000000-0000-0000-0000-000000000000"


def set_counter(session: Session, lab: Lab, kind: str, scope, value: int, year=2026, month=10, branch=None) -> None:
    session.execute(
        text(
            "INSERT INTO numbering_counter (tenant_id, entity_kind, scope_id, branch_id, period_year, period_month, last_value, updated_at) "
            "VALUES (:t, :k, :s, :b, :y, :m, :v, now()) ON CONFLICT (tenant_id, entity_kind, scope_id, branch_id, period_year, period_month) "
            "DO UPDATE SET last_value = :v"
        ),
        {"t": str(lab.tenant.id), "k": kind, "s": str(scope.id), "b": str(branch.id) if branch else NIL,
         "y": year, "m": month, "v": value},
    )
    session.commit()


def configure(session: Session, lab: Lab, kinds=("order", "sample"), **over) -> None:
    """Store a numbering configuration directly (activation is a deliberate act: tests do it explicitly)."""
    values = {"active": True, "manual_policy": "allowed", "format": "{BASE}{SEQ}{MM}{YY}",
              "sequence_min_width": 2, "start_value": 1, "scope": "tenant", "timezone": "America/Mexico_City", **over}
    for kind in ((kinds,) if isinstance(kinds, str) else kinds):
        session.execute(
            text(
                "INSERT INTO numbering_config (tenant_id, entity_kind, active, manual_policy, format, sequence_min_width, "
                "start_value, scope, timezone, updated_at) VALUES (:t, :k, :active, :manual_policy, :format, :sequence_min_width, "
                ":start_value, :scope, :timezone, now()) ON CONFLICT (tenant_id, entity_kind) DO UPDATE SET active = :active, "
                "manual_policy = :manual_policy, format = :format, sequence_min_width = :sequence_min_width, "
                "start_value = :start_value, scope = :scope, timezone = :timezone"
            ),
            {"t": str(lab.tenant.id), "k": kind, **values},
        )
    session.commit()


def grant_manual_permission(session: Session, role_code: str) -> None:
    """Grant the capability to type codes to a role (a deliberate administrative act)."""
    session.execute(text(
        "INSERT INTO role_permission (role_id, permission_id) "
        "SELECT r.id, p.id FROM role r, permission p WHERE r.code = :rc AND p.code = 'lab:enter_manual_code' "
        "ON CONFLICT DO NOTHING"
    ), {"rc": role_code})
    session.commit()


def counter_rows(session: Session) -> int:
    return session.execute(text("SELECT count(*) FROM numbering_counter")).scalar()


def ledger_rows(session: Session) -> int:
    return session.execute(text("SELECT count(*) FROM numbering_allocation")).scalar()


def order_codes(session: Session, lab: Lab) -> list[str]:
    session.expire_all()
    return [r[0] for r in session.execute(
        text('SELECT order_code FROM "order" WHERE tenant_id = :t ORDER BY created_at, order_code'),
        {"t": str(lab.tenant.id)},
    )]


def sample_codes(session: Session, lab: Lab) -> list[str]:
    session.expire_all()
    return [r[0] for r in session.execute(
        text("SELECT sample_code FROM sample WHERE tenant_id = :t ORDER BY sample_code"), {"t": str(lab.tenant.id)}
    )]


def delete_order_and_samples(session: Session, order_id: str) -> None:
    """Hard-delete a registered order the way an operator's SQL would."""
    for stmt in (
        "DELETE FROM order_event WHERE order_id = :o",
        "DELETE FROM assignment WHERE item_id = :o OR item_id IN (SELECT id FROM sample WHERE order_id = :o)",
        "DELETE FROM sample WHERE order_id = :o",
        "UPDATE \"order\" SET invoice_id = NULL WHERE id = :o",
        "DELETE FROM invoice_item WHERE invoice_id IN (SELECT id FROM invoice WHERE order_id = :o)",
        "DELETE FROM invoice WHERE order_id = :o",
        'DELETE FROM "order" WHERE id = :o',
    ):
        session.execute(text(stmt), {"o": order_id})
    session.commit()


@pytest.fixture
def fresh_client(engine):
    """A client whose every request gets its OWN session, closed afterwards
    (so an aborted request rolls back, and threads do not share a session)."""
    def override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = override
    with KeyedClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def new_client() -> KeyedClient:
    """An independent client for a worker thread (dependency override already set)."""
    return KeyedClient(app)


def run_parallel(jobs):
    """Run callables at the same time (behind a barrier); returns their results in order."""
    import threading

    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def worker(i, job):
        client = new_client()
        barrier.wait(timeout=30)
        try:
            results[i] = job(client)
        except Exception as exc:  # surfaced by the assertions of the caller
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive(), "a worker hung (deadlock?)"
    return results
