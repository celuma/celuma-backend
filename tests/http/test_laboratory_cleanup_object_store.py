"""Laboratory cleanup against a REAL S3-protocol endpoint (Céluma 1.4, #39).

Every other cleanup test uses the in-memory `FakeS3Service`, which proves which keys the executor asked to
delete but not that a store actually dropped them. This module runs the real `S3Service` (boto3, SigV4,
HTTP) against an isolated local **moto** server bound to 127.0.0.1 with a synthetic bucket and synthetic
credentials. What it proves: the executor's real `head_object`/`delete_object` round trip removes exactly the
recorded objects and nothing else, verifies the deletion, treats an absent object as done, and behaves
honestly on a versioned bucket. What it does NOT prove: AWS S3 itself, IAM policy, lifecycle, replication or
production volume. Skipped (with the reason) when `moto[server]` is not installed — it is an optional,
dev-only tool, not a runtime dependency.
"""
from __future__ import annotations

import hashlib
import socket

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services.s3 import S3Service as RealS3Service

from .cleanup_world import Flow, build_world
from .conftest import FakeS3Service
from .numbering_support import fresh_client  # noqa: F401

moto_server = pytest.importorskip("moto.server", reason="optional dev tool: pip install 'moto[server]'")
boto3 = pytest.importorskip("boto3")

BUCKET = "celuma-test-bucket"  # the bucket name the synthetic StorageObject rows carry


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def emulator(monkeypatch):
    port = free_port()
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    endpoint = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(settings, "s3_endpoint_url", endpoint)
    monkeypatch.setattr(settings, "s3_bucket_name", BUCKET)
    monkeypatch.setattr(settings, "aws_region", "mx-test-1")
    monkeypatch.setattr(settings, "aws_access_key_id", "synthetic-access-key")
    monkeypatch.setattr(settings, "aws_secret_access_key", "synthetic-secret-key")
    # The executor under test uses the REAL service, not the in-memory fake the other modules patch in.
    monkeypatch.setattr("app.services.laboratory_cleanup.S3Service", RealS3Service)
    # moto keeps its state in the process: start every test from an empty emulator.
    import requests

    requests.post(f"{endpoint}/moto-api/reset", timeout=10)
    client = boto3.client("s3", endpoint_url=endpoint, region_name="mx-test-1",
                          aws_access_key_id="synthetic-access-key", aws_secret_access_key="synthetic-secret-key")
    client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "mx-test-1"})
    try:
        yield client
    finally:
        server.stop()


def upload_everything(client) -> dict:
    """Mirror the in-memory store (produced by the real signing/registration flows) into the emulator."""
    for key, data in FakeS3Service.store.items():
        client.put_object(Bucket=BUCKET, Key=key, Body=data)
    return {key: hashlib.sha256(data).hexdigest() for key, data in FakeS3Service.store.items()}


def bucket_state(client) -> dict:
    out = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for item in page.get("Contents", []) or []:
            out[item["Key"]] = hashlib.sha256(client.get_object(Bucket=BUCKET, Key=item["Key"])["Body"].read()).hexdigest()
    return out


@pytest.fixture
def world(session, fresh_client, monkeypatch, stub_pdf_render):
    w = build_world(session, fresh_client, monkeypatch, stub_pdf_render)
    return w


def test_the_real_object_store_round_trip_removes_exactly_the_recorded_objects(session, fresh_client, world, emulator):
    sent = upload_everything(emulator)
    before = bucket_state(emulator)
    assert before == sent and len(before) > len(world.target.scoped_keys)
    # One recorded object vanished from the store before the run: absent counts as done, not as a failure.
    gone_already = sorted(world.target.scoped_keys)[0]
    emulator.delete_object(Bucket=BUCKET, Key=gone_already)

    op = Flow(fresh_client, world.target).run()
    assert (op["status"], op["phase"]) == ("COMPLETED", "DONE"), (op["error_code"], op["error_message"])
    assert op["objects"]["absent"] == 1 and op["objects"]["deleted"] == op["objects"]["total"] - 1
    assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_object WHERE status = 'PENDING'")).scalar() == 0

    after = bucket_state(emulator)
    expected_gone = set(world.target.scoped_keys)
    assert set(before) - set(after) == expected_gone                         # exactly the recorded objects, no more
    assert expected_gone.isdisjoint(after)
    for key in set(after):
        assert after[key] == before[key], key                              # every survivor is byte-identical
    # Logos, signatures, letterhead assets, the shared objects, the unprovable placeholder, the other laboratory: all there.
    for key in (world.target.preserved_keys | world.target.unprovable_keys | world.other.preserved_keys
                | world.other.scoped_keys | {world.target.shared_key, world.shared_cross}):
        assert key in after, key


def test_a_bucket_outage_mid_run_is_recorded_and_the_retry_finishes_over_the_real_endpoint(session, fresh_client, world, emulator, monkeypatch):
    upload_everything(emulator)
    flow = Flow(fresh_client, world.target)
    real_endpoint = settings.s3_endpoint_url
    # Point the executor at a port nothing listens on: every network call fails.
    monkeypatch.setattr(settings, "s3_endpoint_url", f"http://127.0.0.1:{free_port()}")
    op = flow.run()
    assert (op["status"], op["phase"], op["error_code"]) == ("FAILED", "OBJECTS", "objects_failed")
    assert op["objects"]["failed"] == op["objects"]["total"] > 0
    assert "127.0.0.1" not in str(op) and BUCKET not in str(op)             # no endpoint/bucket text leaks into the view
    assert set(world.target.scoped_keys) <= set(bucket_state(emulator))      # nothing was removed from the store
    monkeypatch.setattr(settings, "s3_endpoint_url", real_endpoint)
    assert flow.retry(op["id"]).status_code == 202
    done = flow.operation(op["id"])
    assert done["status"] == "COMPLETED" and done["objects"]["deleted"] == done["objects"]["total"]
    assert set(world.target.scoped_keys).isdisjoint(bucket_state(emulator))


def test_on_a_versioned_bucket_the_current_object_goes_but_earlier_versions_remain(session, fresh_client, world, emulator):
    """Honest limit: the application removes the CURRENT object only. With versioning on, S3 keeps the previous
    version behind a delete marker until the bucket's lifecycle expires it — the runbook asks the operator to
    record the versioning state because that, not this code, decides whether the bytes are recoverable."""
    emulator.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    upload_everything(emulator)
    victim = sorted(world.target.scoped_keys)[0]
    op = Flow(fresh_client, world.target).run()
    assert op["status"] == "COMPLETED"
    assert victim not in bucket_state(emulator)                              # invisible to ordinary reads
    versions = emulator.list_object_versions(Bucket=BUCKET, Prefix=victim)
    assert versions.get("Versions"), "the earlier version is still stored (recoverable until lifecycle expiry)"
    assert versions.get("DeleteMarkers")
