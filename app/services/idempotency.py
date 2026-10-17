"""Idempotent creation requests (Céluma 1.4, issue #8; ADR 0014).

Supported operations: standalone order, standalone sample, unified order with
samples. The client sends an `Idempotency-Key`; the first request that commits
leaves a receipt, written in the SAME transaction as the order/sample/
allocation it describes. A retry of the same logical request replays the
original records (rebuilt, so their current state); the same key with a different payload is refused.

Order of events in a route (the idempotency lock is always the FIRST lock a
request takes, before counter rows and code locks, so it can never join a lock
cycle with them):

    authenticate + authorize + tenant/branch access   (every time, replay included)
    -> begin()     advisory lock on tenant+actor+operation+key, read receipt
         replay?   -> verify the resources still exist -> their representation (+ Idempotent-Replayed)
         mismatch? -> 409 idempotency_key_reused
    -> validate, allocate, create...                  (first execution only)
    -> store()     receipt in this transaction
    -> commit
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session, select

from app.models.numbering import IdempotencyReceipt

OP_ORDER = "order.create"
OP_SAMPLE = "sample.create"
OP_UNIFIED = "order.create_unified"
HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"

_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")


class IdempotencyError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code, {"code": code, "message": message})


def validate_key(raw: Optional[str]) -> str:
    """The key of this request. Required: a creation without one is refused (428), never
    silently processed as if it were retry-safe."""
    if raw is None or not raw.strip():
        raise IdempotencyError(
            428, "idempotency_key_required",
            f"The {HEADER} header is required to create orders and samples; generate one per logical submission "
            "and reuse it only to retry that same submission",
        )
    raw = raw.strip()
    if not _KEY_RE.fullmatch(raw):
        raise IdempotencyError(
            422, "idempotency_key_invalid",
            f"{HEADER} must be 8-128 characters of letters, digits, '.', '_', ':' or '-' (a UUID is recommended)",
        )
    return raw


def fingerprint(payload: BaseModel) -> str:
    """SHA-256 of the canonical request (sorted keys, no whitespace, every field including
    defaults). Only the hash is stored: the request itself never is."""
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Claim:
    tenant_id: object
    actor_id: object
    operation: str
    key: str
    fingerprint: str
    receipt: Optional[IdempotencyReceipt] = None

    @property
    def replay(self) -> bool:
        return self.receipt is not None


def begin(session: Session, *, tenant_id, actor_id, operation: str, key: str, request_fingerprint: str) -> Claim:
    """Serialize on the key and look for a committed receipt.

    The transaction-scoped advisory lock is held until this request commits or
    rolls back, so a concurrent request with the same key waits here and then
    finds the receipt (or, if the first one rolled back, runs for itself):
    exactly one committed outcome per key.
    """
    lock_key = f"idem:{tenant_id}:{actor_id}:{operation}:{key}"
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": lock_key})
    receipt = session.exec(
        select(IdempotencyReceipt).where(
            IdempotencyReceipt.tenant_id == tenant_id,
            IdempotencyReceipt.actor_user_id == actor_id,
            IdempotencyReceipt.operation == operation,
            IdempotencyReceipt.idempotency_key == key,
        )
    ).first()
    if receipt is not None and receipt.request_fingerprint != request_fingerprint:
        raise IdempotencyError(
            409, "idempotency_key_reused",
            "This Idempotency-Key was already used for a different request; use a new key for a new submission",
        )
    return Claim(tenant_id, actor_id, operation, key, request_fingerprint, receipt)


_RESOURCE_TABLES = {"order": '"order"', "sample": "sample"}


def verify_resources(session: Session, claim: Claim) -> None:
    """Replay guard: what the original request created must still exist.

    A deleted OR removed (Céluma 1.4, #11 tombstone) resource is refused explicitly (410): the retry
    must never be answered by quietly creating a second record, by resurrecting a removed capture, nor
    by pointing at nothing. Raw SQL on purpose: the ORM's removed-record filter is not the check here.
    """
    receipt = claim.receipt
    assert receipt is not None
    for resource in receipt.resources:
        table = _RESOURCE_TABLES[resource["kind"]]
        exists = session.execute(
            text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE id = :i AND tenant_id = :t AND removed_at IS NULL)"),
            {"i": resource["id"], "t": str(claim.tenant_id)},
        ).scalar()
        if not exists:
            raise IdempotencyError(
                410, "idempotent_resource_deleted",
                "The record created by this request was deleted; it will not be created again with this key",
            )


def replayed(model: BaseModel, status: int = 200) -> JSONResponse:
    """The response of a replay: the original records' current representation, marked as a replay."""
    return JSONResponse(status_code=status, content=jsonable_encoder(model), headers={REPLAY_HEADER: "true"})


def store(session: Session, claim: Claim, *, result: dict, resources: list, status: int = 200) -> None:
    """Add the receipt to the caller's transaction (it commits or rolls back with the work)."""
    session.add(IdempotencyReceipt(
        tenant_id=claim.tenant_id, actor_user_id=claim.actor_id, operation=claim.operation,
        idempotency_key=claim.key, request_fingerprint=claim.fingerprint,
        response_status=status, result=result, resources=resources,
    ))
