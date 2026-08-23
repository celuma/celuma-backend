"""Security middleware decides on the routed path, not on `request.url.path`.

Céluma 1.3, Phase 5, Block G-B — **G-010** (found while dispositioning F-007).

Starlette rebuilds `request.url` as ``f"{scheme}://{host_header}{path}"`` and
re-parses the result, where ``host_header`` is the raw, unvalidated ``Host``
request header (``starlette/datastructures.py``, ``URL.__init__``). A ``Host``
value containing ``?``, ``#`` or a path segment therefore changes what
``request.url.path`` returns, so it can disagree with the path Starlette
actually routed on. That is the substance of CVE-2026-48710 and
CVE-2026-54282; neither is fixed in any Starlette release FastAPI 0.116.2
permits (``starlette<0.49.0``), so the mitigation is on Céluma's side.

The concrete defect this locks was **not** hypothetical. ``limit_request_size``
decided the maximum request body from the path with a *substring* test::

    is_raw_like = any(ext in path for ext in [".cr2", ..., ".dng"]) or ...
    max_size = five_hundred_mb if is_raw_like else fifty_mb

Sending ``Host: evil.com/x.dng`` made ``request.url.path`` evaluate to
``/x.dng/api/v1/laboratory/branches``, ``is_raw_like`` became true, and the
limit rose from 50 MB to 500 MB — a tenfold request-size bypass available to
an unauthenticated client through one header, on every endpoint outside
``/api/v1/reports/``.

Reading ``scope["path"]`` — the value Starlette itself feeds into the
reconstruction, and the value the router matches — removes the
concatenate-and-reparse round trip through attacker-controlled input. These
tests assert the property directly on the URL machinery, so they keep failing
if someone reverts a call site to ``request.url.path``.
"""
from __future__ import annotations

import pytest
from starlette.datastructures import URL

from app.main import _routed_path

REAL_PATH = "/api/v1/laboratory/branches"

RAW_EXTENSIONS = [
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".sr2",
    ".raf", ".rw2", ".orf", ".pef", ".dng",
]


class _FakeRequest:
    """Just enough of `Request` for `_routed_path` — it only reads `scope`."""

    def __init__(self, scope: dict) -> None:
        self.scope = scope


def build_scope(path: str = REAL_PATH, host: str = "app.celuma.mx") -> dict:
    return {
        "type": "http",
        "scheme": "https",
        "path": path,
        "query_string": b"",
        "headers": [(b"host", host.encode())],
        "server": ("celuma", 443),
    }


# ---------------------------------------------------------------------------
# The property under attack
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hostile_host",
    [
        "evil.com/x.dng",
        "evil.com/health",
        "evil.com?",
        "evil.com#",
        "evil.com/api/v1/laboratory/samples/1",
    ],
)
def test_routed_path_ignores_a_hostile_host_header(hostile_host):
    scope = build_scope(host=hostile_host)

    # The reconstruction is genuinely corruptible ...
    assert URL(scope=scope).path != REAL_PATH
    # ... but the routed path is not.
    assert _routed_path(_FakeRequest(scope)) == REAL_PATH


def test_routed_path_matches_request_url_for_a_well_formed_request():
    # The hardening must be a no-op in normal operation, or it would be a
    # behaviour change dressed up as a security fix.
    scope = build_scope()

    assert _routed_path(_FakeRequest(scope)) == URL(scope=scope).path == REAL_PATH


# ---------------------------------------------------------------------------
# The specific bypass: request-size limit escalation
# ---------------------------------------------------------------------------

def _is_raw_like(path: str) -> bool:
    """The heuristic from `limit_request_size`, applied to a candidate path."""
    return any(extension in path.lower() for extension in RAW_EXTENSIONS)


def test_a_hostile_host_no_longer_escalates_the_request_size_limit():
    scope = build_scope(host="evil.com/x.dng")

    # Pre-fix behaviour, kept here as the thing being prevented: the
    # reconstructed path carries an attacker-chosen RAW extension, which the
    # substring heuristic reads as "this is a RAW upload, allow 500 MB".
    assert _is_raw_like(URL(scope=scope).path) is True

    # Post-fix: the routed path carries no such extension, so the request
    # keeps the ordinary 50 MB ceiling.
    assert _is_raw_like(_routed_path(_FakeRequest(scope))) is False


@pytest.mark.parametrize("extension", RAW_EXTENSIONS)
def test_no_raw_extension_can_be_smuggled_through_the_host_header(extension):
    scope = build_scope(host=f"evil.com/payload{extension}")

    assert _is_raw_like(_routed_path(_FakeRequest(scope))) is False


# ---------------------------------------------------------------------------
# The health-endpoint skip lists
# ---------------------------------------------------------------------------

HEALTH_PATHS = ["/", "/health", "/api/v1/health"]


@pytest.mark.parametrize("hostile_host", ["evil.com/health", "evil.com?", "evil.com#"])
def test_a_hostile_host_cannot_forge_a_health_path(hostile_host):
    # Rate limiting, request-size limiting and request logging are all skipped
    # for the health endpoints. None of them may be reachable by pretending.
    scope = build_scope(host=hostile_host)

    assert _routed_path(_FakeRequest(scope)) not in HEALTH_PATHS


@pytest.mark.parametrize("health_path", HEALTH_PATHS)
def test_genuine_health_requests_are_still_recognised(health_path):
    scope = build_scope(path=health_path)

    assert _routed_path(_FakeRequest(scope)) in HEALTH_PATHS
