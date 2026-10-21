"""Independent review R1/R2: a layout failure signalled by the render route (`LAYOUT_*`) must fail the
generation with its own code and never produce an artifact (not a warning-only, blank or clipped PDF), and
must not be confused with a missing signature."""
import pytest

from app.core.config import settings
from app.services.report_pdf_generation import ReportPdfGenerationError, ReportPdfGenerationService


class _FakePage:
    def __init__(self, render_error):
        self._error = render_error
        self.pdf_called = False

    def goto(self, *a, **k): ...
    def wait_for_selector(self, *a, **k): ...
    def get_attribute(self, selector, name):
        return self._error if name == "data-report-render-error" else None

    def pdf(self, **k):
        self.pdf_called = True
        return b"%PDF-1.4"


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page

    def close(self): ...


class _FakePlaywright:
    def __init__(self, page):
        self.chromium = type("C", (), {"launch": lambda _s, **k: _FakeBrowser(page)})()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _render(monkeypatch, render_error):
    import playwright.sync_api as sync_api

    page = _FakePage(render_error)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _FakePlaywright(page))
    monkeypatch.setattr(settings, "pdf_generator_base_url", "http://127.0.0.1:1", raising=False)
    version = type("V", (), {"id": "00000000-0000-0000-0000-000000000001", "version_no": 1})()
    report = type("R", (), {"id": "r", "tenant_id": "00000000-0000-0000-0000-000000000002"})()
    service = ReportPdfGenerationService.__new__(ReportPdfGenerationService)
    return page, lambda: service._render_pdf(report, version)


@pytest.mark.parametrize("code", ["LAYOUT_NO_PAGES", "LAYOUT_UNUSABLE", "LAYOUT_OVERFLOW"])
def test_layout_failures_never_produce_a_pdf(monkeypatch, code):
    page, run = _render(monkeypatch, code)
    with pytest.raises(ReportPdfGenerationError) as exc:
        run()
    assert exc.value.code == "LAYOUT_RENDER_FAILED"
    assert code in exc.value.message
    assert page.pdf_called is False


def test_signature_failures_keep_their_own_code(monkeypatch):
    page, run = _render(monkeypatch, "SIGNATURE_NOT_LOADED")
    with pytest.raises(ReportPdfGenerationError) as exc:
        run()
    assert exc.value.code == "SIGNATURE_RENDER_FAILED"
    assert page.pdf_called is False


def test_a_clean_render_produces_the_pdf(monkeypatch):
    page, run = _render(monkeypatch, None)
    assert run().startswith(b"%PDF")
    assert page.pdf_called is True
