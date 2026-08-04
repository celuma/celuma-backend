"""Shared fixtures for HTTP integration tests (Céluma 1.3, Phase 2, Block B,
Story B10).

The project had no HTTP integration tests before this block (confirmed in
phase-2-block-a-implementation-summary.md, "Dependencias para el Block B").
This uses an in-memory SQLite database — the same pattern already
established by tests/test_rbac_phase2.py — plus FastAPI's TestClient, with
`S3Service` replaced by an in-memory fake so these tests never touch the
real AWS bucket configured in `.env`.
"""
import os
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlalchemy import text
from sqlalchemy.engine.url import make_url

import app.models  # noqa: F401  registers every table on SQLModel.metadata
from app.core.config import settings
from app.core.db import get_session
from app.main import app, rate_limit_storage


# Céluma 1.3 Phase 2, Block B, Story B10: HTTP tests run against a real,
# ephemeral Postgres database (dropped and recreated per test function) on
# the same local Postgres server already used by `docker compose`, rather
# than SQLite in-memory. This repo's models rely on native Postgres UUID/JSON
# handling (e.g. `current_user()` looks up AppUser by a raw string primary
# key, which only round-trips correctly through Postgres' UUID adapter) —
# SQLite's generic UUID emulation breaks on that exact path. This database
# is never the tenant's real `celumadb`; it is always dropped+recreated by
# name, never touched interactively.
_TEST_DB_NAME = "celuma_http_test"


def _admin_url():
    return make_url(settings.database_url).set(database="postgres")


def _test_db_url():
    return make_url(settings.database_url).set(database=_TEST_DB_NAME)


class FakeS3ObjectInfo:
    def __init__(self, bucket, key, size_bytes, content_type, etag, version_id):
        self.bucket = bucket
        self.key = key
        self.size_bytes = size_bytes
        self.content_type = content_type
        self.etag = etag
        self.version_id = version_id


class FakeS3Service:
    """Stands in for app.services.s3.S3Service in every HTTP test.

    Keeps uploaded bytes in memory (so tests can assert on what was
    persisted) and can be told to fail on the next upload, to exercise the
    Story B8 atomicity/compensation path without a real S3 outage.
    """

    store: dict = {}
    fail_next_upload: bool = False

    @property
    def region(self):
        return "mx-test-1"

    @property
    def bucket(self):
        return "celuma-test-bucket"

    def upload_bytes(self, data, key, content_type=None, acl=None):
        if FakeS3Service.fail_next_upload:
            FakeS3Service.fail_next_upload = False
            raise RuntimeError("Simulated S3 upload failure (Historia B8 test)")
        FakeS3Service.store[key] = data
        return FakeS3ObjectInfo(
            bucket=self.bucket,
            key=key,
            size_bytes=len(data),
            content_type=content_type,
            etag="fake-etag",
            version_id=None,
        )

    def download_bytes(self, key):
        return FakeS3Service.store[key]

    def download_text(self, key, encoding="utf-8"):
        return FakeS3Service.store[key].decode(encoding)

    def generate_presigned_url(self, key, expires_in=None, response_content_disposition=None):
        suffix = f"?cd={response_content_disposition}" if response_content_disposition else ""
        return f"https://fake-s3.example/{key}{suffix}"

    def object_public_url(self, key):
        return f"https://fake-cdn.example/{key}"

    def delete_object(self, key):
        FakeS3Service.store.pop(key, None)


@pytest.fixture(autouse=True)
def _reset_fake_s3():
    FakeS3Service.store = {}
    FakeS3Service.fail_next_upload = False
    yield


@pytest.fixture(autouse=True)
def _patch_s3(monkeypatch):
    monkeypatch.setattr("app.api.v1.reports.S3Service", FakeS3Service)
    monkeypatch.setattr("app.api.v1.portal.S3Service", FakeS3Service)
    # Céluma 1.3 Phase 2, Block E: ReportPdfGenerationService uploads the
    # generated PDF via its own S3Service import — never the real AWS
    # bucket in tests, same as every other flow above.
    monkeypatch.setattr("app.services.report_pdf_generation.S3Service", FakeS3Service)
    # Post-Phase-2 remediation: ManagedTenantImageService (shared by
    # template-logo, tenant-logo, and letterhead-logo endpoints) has its
    # own S3Service import, patched independently of the callers above.
    monkeypatch.setattr(
        "app.services.managed_tenant_image_service.S3Service", FakeS3Service
    )
    # Third post-Phase-2 remediation: `letterhead_portability` downloads
    # logo bytes to embed them in the `.cell`, and `letterhead_resources`
    # signs the editor's ephemeral URLs — both with their own S3Service
    # import. Without these two patches, any round-trip test WITH a logo
    # hit the real bucket (and failed with NoSuchKey), which is why the
    # "export/import a letterhead with logo" path was never covered before
    # this remediation.
    monkeypatch.setattr("app.services.letterhead_portability.S3Service", FakeS3Service)
    monkeypatch.setattr("app.services.letterhead_resources.S3Service", FakeS3Service)


def make_pdf_bytes(num_pages: int = 1) -> bytes:
    """A genuinely valid, parseable PDF (built with pypdf, not a hand-rolled
    byte string) — Céluma 1.3 Phase 2, Block E tests feed this through the
    *real* validation/hash/page-count logic in ReportPdfGenerationService;
    only the headless-Chromium render step itself is stubbed (see
    `stub_pdf_render` below), so E5 (byte validation) is exercised for real."""
    from pypdf import PdfWriter
    import io as _io

    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = _io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def stub_pdf_render(monkeypatch):
    """Replaces ReportPdfGenerationService._render_pdf so tests never launch
    real Chromium. Returns a controller object: call `.succeed(pdf_bytes)` or
    `.fail(exc)` to control the next (and subsequent) render call(s), and
    `.call_count` to assert whether a render actually happened (e.g. to
    prove idempotency skipped it)."""

    class _Controller:
        def __init__(self):
            self.call_count = 0
            self._result = make_pdf_bytes(1)
            self._exc = None

        def succeed(self, pdf_bytes: bytes):
            self._result = pdf_bytes
            self._exc = None

        def fail(self, exc: Exception):
            self._exc = exc

        def _render(self, report, version):
            self.call_count += 1
            if self._exc is not None:
                raise self._exc
            return self._result

    controller = _Controller()
    monkeypatch.setattr(
        "app.services.report_pdf_generation.ReportPdfGenerationService._render_pdf",
        controller._render,
    )
    return controller


@pytest.fixture(autouse=True)
def _reset_rate_limit_storage():
    """`app.main.basic_rate_limiting` is a real, always-on middleware (100
    requests/60s per client IP) with in-memory, process-lifetime state — it
    is not test-aware. TestClient requests all share the same synthetic
    client IP, so without resetting this between test functions, the whole
    HTTP suite shares one 60s window and later tests start failing with 429
    once enough earlier tests have run (Céluma 1.3 Phase 2, Block D —
    surfaced when this block's new HTTP tests pushed the full-suite request
    count over the limit)."""
    rate_limit_storage.clear()
    yield


@pytest.fixture(name="engine")
def engine_fixture():
    admin_engine = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{_TEST_DB_NAME}"'))
    admin_engine.dispose()

    # Build the schema via the real Alembic migration chain (not
    # SQLModel.metadata.create_all()) so the test database's schema is
    # exactly what `alembic upgrade head` produces in every other
    # environment — including native Postgres ENUM types that
    # `create_all()` does not reliably emit in dependency order.
    subprocess.run(
        ["alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            # str(URL) masks the password by default — must render it explicitly.
            "DATABASE_URL": _test_db_url().render_as_string(hide_password=False),
        },
    )

    engine = create_engine(_test_db_url())
    yield engine
    engine.dispose()


@pytest.fixture(name="session")
def session_fixture(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        yield session

    app.dependency_overrides[get_session] = get_session_override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
