from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from collections import defaultdict
from urllib.parse import parse_qsl
import asyncio
from app.api.v1.users import router as users_router
from app.api.v1.auth import router as auth_router
from app.api.v1.auth import current_user
from app.api.v1.tenants import router as tenants_router
from app.api.v1.branches import router as branches_router
from app.api.v1.patients import router as patients_router
from app.api.v1.requesting_physicians import router as requesting_physicians_router
from app.api.v1.laboratory import router as laboratory_router
from app.api.v1.reports import router as reports_router
from app.api.v1.report_letterheads import router as report_letterheads_router
from app.api.v1.report_sections import router as report_sections_router
from app.api.v1.study_types import router as study_types_router
from app.api.v1.price_catalog import router as price_catalog_router
from app.api.v1.billing import router as billing_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.portal import router as portal_router
from app.api.v1.worklist import router as worklist_router
from app.api.v1.rbac import router as rbac_router
from app.api.v1.internal_render import router as internal_render_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.notification_preferences import (
    router as notification_preferences_router,
)
from app.core.config import settings
# Céluma 1.3 Phase 3, Block E, Story E6: the notification delivery worker.
# Imported as its two lifecycle functions rather than as the module, so this
# file has no access to the claim primitive at all — the worker owns the queue.
# `test_main_cannot_drive_the_queue_itself` asserts that from this source, so
# an HTTP handler added here could not reach into the queue by accident.
from app.api.v1.tenant_usage import router as tenant_usage_router
from app.services.notification_delivery_worker import start_worker, stop_worker
# Céluma 1.3 Phase 4, Block D: the usage reconciliation worker. A second,
# separate in-process poller — reconciliation and email delivery share a
# process, not a loop, so a failure or a slow cycle in one never delays the
# other. Disabled by default (USAGE_RECONCILIATION_ENABLED).
from app.services.usage_reconciliation_worker import (
    start_reconciliation_worker,
    stop_reconciliation_worker,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Startup/shutdown events
#
# Céluma 1.3 Phase 3, Block E, Story E6: the in-process notification delivery
# poller is started and stopped here, and nowhere else.
#
# This is the mechanism Block A's delivery strategy §3 selected after
# evaluating all four options: a long-lived asyncio task in the API container.
# Not a per-request post-response callback (lost on restart, invisible to any
# other process, no retry), not a second ECS worker service, not a queue
# library. Its *state* lives entirely in PostgreSQL, so a restart costs
# wall-clock time and no information.
#
# `start_worker` never raises and returns None when email is disabled or
# misconfigured — `EMAIL_ENABLED` defaults to false, so by default nothing
# starts here at all. Boot must not depend on email being configured
# (architectural principle §4.3/§4.7), and that is also what keeps the worker
# out of the test suite: `TestClient` runs this lifespan, so anything gated on
# something other than configuration would start under pytest.
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Celuma API starting up...")
    await start_worker()
    await start_reconciliation_worker()
    try:
        yield
    finally:
        # In a `finally` so a failure anywhere in shutdown still stops the
        # pollers: a worker left running against a closing process is a
        # worker holding claimed rows nothing will resolve. Reconciliation
        # is stopped first and independently — its own `stop()` never
        # raises, so the delivery worker is stopped either way.
        await stop_reconciliation_worker()
        await stop_worker()
        logger.info("🛑 Celuma API shutting down...")

app = FastAPI(
    title="Celuma API", 
    description="Multi-tenant Laboratory Management System",
    lifespan=lifespan
)

# Add security middlewares
# Allow all hosts since the backend sits behind the nginx reverse proxy;
# host validation is the proxy's responsibility.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"],
)

# Add CORS middleware (more secure in production)
#
# Céluma 1.3 Phase 2, Block C, Story C11: `allow_origins` previously
# included a literal "*" alongside explicit origins, together with
# `allow_credentials=True`. Starlette's CORSMiddleware treats ANY "*" in the
# list (not just an exact `["*"]`) as allow_all_origins=True, and in that
# mode its *actual*-response headers (as opposed to preflight) always send
# `Access-Control-Allow-Origin: *` — which is invalid combined with
# `Access-Control-Allow-Credentials: true` per the Fetch/CORS spec. Browsers
# silently reject that combination, so every credentialed cross-origin
# request (`credentials: "include"`, used by login.tsx and elsewhere)
# failed with "Failed to fetch" for any origin actually reached the backend
# directly instead of through Vite's dev-only proxy — e.g. `vite preview`
# (used to validate the static production build) on port 4173, or any real
# deployment where the frontend and backend are on different origins. This
# went unnoticed until this block because the dev server (5173) proxies
# `/api/*` server-side, so the browser never saw it as cross-origin at all.
# Fixed by listing only explicit origins — no bare "*" — so
# Access-Control-Allow-Origin correctly echoes the literal requesting origin.
#
# Céluma 1.3 Phase 2, Block D, Story D1: the origin list itself now comes
# from `settings.cors_allowed_origins` (CORS_ALLOWED_ORIGINS env var) instead
# of being hardcoded here, so the production frontend origin can be
# configured without a code change. `cors_allowed_origins_list` still
# enforces the no-bare-"*" invariant above.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(f"🚨 Unhandled exception in request {request_id}: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "request_id": request_id,
            "type": "internal_server_error"
        }
    )

# Request ID and security headers middleware
@app.middleware("http")
async def add_request_id_and_security_headers(request: Request, call_next):
    # Generate unique request ID
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    
    # Process request
    response = await call_next(request)
    
    # Add security headers
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    
    return response

# Céluma 1.3 Phase 5, Block G-B (F-007): read the routed path from the ASGI
# scope, never from `request.url.path`.
#
# Starlette rebuilds `request.url` as f"{scheme}://{host_header}{path}" and
# re-parses the result, where `host_header` is the raw, UNVALIDATED `Host`
# header (starlette/datastructures.py, URL.__init__). A `Host` value carrying
# `?`, `#` or a path segment therefore shifts what `.path` returns, so the
# value can disagree with the path Starlette actually routed on. That is the
# substance of CVE-2026-48710 and CVE-2026-54282, neither of which is fixed
# in any Starlette release FastAPI 0.116.2 permits (`starlette<0.49.0`).
#
# The middleware below uses the path to decide whether to SKIP rate limiting
# and request-size limiting. Those decisions must be made on the same value
# the router uses, which is `scope["path"]` — the input Starlette itself
# feeds into the reconstruction. Reading it directly removes the
# concatenate-and-reparse round trip through attacker-controlled input
# entirely, and is identical to `request.url.path` for every well-formed
# request.
#
# See docs/celuma-1.3/phase-5-block-g/block-g-dependency-disposition.md.
def _routed_path(request: Request) -> str:
    return request.scope.get("path", "")


# Request size limiting middleware
@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    # Skip size check for health endpoints
    if _routed_path(request) in ["/", "/health", "/api/v1/health"]:
        return await call_next(request)

    # Determine per-route/per-type limits
    path = _routed_path(request).lower()
    content_type = (request.headers.get("content-type") or "").lower()

    # Endpoints that handle streaming/chunked reads with their own limits
    streaming_paths = [
        "/api/v1/laboratory/samples/",  # base; specific images endpoint starts with this
        "/api/v1/reports/",             # reports endpoints may upload PDFs, validated inside
    ]
    # If it's the specific sample images upload endpoint, skip global limit (endpoint enforces 50/500MB)
    if path.startswith("/api/v1/laboratory/samples/") and path.endswith("/images"):
        return await call_next(request)

    # Default limits
    fifty_mb = 50 * 1024 * 1024
    five_hundred_mb = 500 * 1024 * 1024

    # Heuristics: PDFs and standard images up to 50MB; RAW images up to 500MB
    # RAW detection via typical extensions or vendor content-types
    is_raw_like = any(ext in path for ext in [".cr2", ".cr3", ".nef", ".nrw", ".arw", ".sr2", ".raf", ".rw2", ".orf", ".pef", ".dng"]) or "raw" in content_type

    if request.headers.get("content-length"):
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0

        # For report PDF uploads, allow 50MB
        if path.startswith("/api/v1/reports/"):
            max_size = fifty_mb
        else:
            max_size = five_hundred_mb if is_raw_like else fifty_mb

        if content_length > max_size:
            return JSONResponse(
                status_code=413,
                content={
                    "detail": f"Request body too large. Maximum size allowed: {max_size} bytes",
                    "type": "request_entity_too_large"
                }
            )

    return await call_next(request)

# Basic rate limiting middleware (simple in-memory)

# Simple rate limiter storage (use Redis in production)
rate_limit_storage = defaultdict(list)
rate_limit_lock = asyncio.Lock()

@app.middleware("http")
async def basic_rate_limiting(request: Request, call_next):
    # Skip rate limiting for health endpoints
    if _routed_path(request) in ["/", "/health", "/api/v1/health"]:
        return await call_next(request)
    
    client_ip = request.client.host if request.client else "unknown"
    current_time = time.time()
    window_size = settings.rate_limit_window_seconds
    max_requests = settings.rate_limit_max_requests
    
    async with rate_limit_lock:
        # Clean old entries
        rate_limit_storage[client_ip] = [
            timestamp for timestamp in rate_limit_storage[client_ip]
            if current_time - timestamp < window_size
        ]
        
        # Check if rate limit exceeded
        if len(rate_limit_storage[client_ip]) >= max_requests:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Rate limit exceeded. Maximum {max_requests} requests per {window_size} seconds.",
                    "type": "rate_limit_exceeded",
                    "retry_after": window_size
                }
            )
        
        # Add current request
        rate_limit_storage[client_ip].append(current_time)
    
    return await call_next(request)

# Céluma 1.3 Phase 5, Block E (E-003): credential redaction for the request
# line below.
#
# `log_requests` already redacts sensitive *headers* before logging them, so
# the rule "a credential must not reach the application log" was established
# long before this block. The request line itself logged `request.url`, and
# two of this API's credentials travel in the URL rather than in a header:
#
#   * `GET /portal/patient/report?code=…` — the patient access code is the
#     only thing between an anonymous caller and a published report, its
#     patient name, and a resolvable presigned URL for the official PDF.
#   * `GET|POST /users/invitations/{token}[/accept]` — the invitation token
#     authorizes creating an account inside a tenant with a preassigned role.
#
# Both were written verbatim, at INFO, on every request. Logs are retained and
# read by a wider audience than the data they describe, so a log reader could
# replay either one.
#
# Only the credential positions are redacted: paths and ordinary query
# parameters stay intact, because worklist filters and pagination cursors are
# what make these lines worth logging at all. `_SENSITIVE_QUERY_KEYS` is the
# contract — a new credential-bearing query parameter must be added here, and
# `tests/http/test_block_e_request_log_redaction.py` is what holds it.
_SENSITIVE_QUERY_KEYS = {"code", "token", "access_code", "secret", "password", "api_key"}
_INVITATION_TOKEN_RE = re.compile(r"(/invitations/)[^/?]+")


def _safe_request_target(request: Request) -> str:
    """`path?query` with credential values replaced by `<redacted>`."""
    path = _INVITATION_TOKEN_RE.sub(r"\1<redacted>", request.url.path)
    if not request.url.query:
        return path
    redacted = "&".join(
        f"{key}=<redacted>" if key.lower() in _SENSITIVE_QUERY_KEYS else f"{key}={value}"
        for key, value in parse_qsl(request.url.query, keep_blank_values=True)
    )
    return f"{path}?{redacted}"


# Enhanced logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Skip logging for health endpoints to avoid noise
    if _routed_path(request) in ["/", "/health", "/api/v1/health"]:
        return await call_next(request)
    
    start_time = time.time()
    request_id = getattr(request.state, "request_id", "unknown")

    # Redact sensitive headers before logging
    def _redact_headers(headers: dict) -> dict:
        """Return a shallow copy with sensitive header values redacted."""
        redacted = {}
        for key, value in headers.items():
            lower_key = key.lower()
            if lower_key in {"authorization", "cookie", "set-cookie", "x-api-key"}:
                redacted[key] = "REDACTED"
            else:
                redacted[key] = value
        return redacted

    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"🔥 [{request_id[:8]}] INCOMING REQUEST: {request.method} {_safe_request_target(request)} | client={client_ip}")
    
    # Only log headers for auth endpoints or if there's an auth header
    if "/auth/" in str(request.url) or "authorization" in dict(request.headers):
        try:
            logger.info(f"📋 [{request_id[:8]}] Headers: {_redact_headers(dict(request.headers))}")
        except Exception:
            logger.warning(f"⚠️ [{request_id[:8]}] Could not serialize request headers for logging")

    status_code: int = 0
    response: Response = None  # type: ignore
    try:
        # Process request
        response = await call_next(request)
        status_code = getattr(response, "status_code", 200)
        return response
    except Exception:
        status_code = 500
        logger.exception(f"💥 [{request_id[:8]}] Unhandled exception while processing request")
        # Re-raise to let global exception handler respond
        raise
    finally:
        process_time = time.time() - start_time
        
        # Use different emoji based on status code
        status_emoji = "✅" if status_code < 400 else "⚠️" if status_code < 500 else "❌"
        logger.info(f"{status_emoji} [{request_id[:8]}] RESPONSE: {status_code} | Time: {process_time:.3f}s")
        
        # Only log response headers if there was an error or it's an auth endpoint
        if response is not None and (status_code >= 400 or "/auth/" in str(request.url)):
            try:
                logger.info(f"📤 [{request_id[:8]}] Response Headers: {dict(response.headers)}")
            except Exception:
                logger.warning(f"⚠️ [{request_id[:8]}] Could not serialize response headers for logging")

app.include_router(users_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(tenants_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(branches_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(patients_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(requesting_physicians_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(laboratory_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(reports_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(
    report_letterheads_router, prefix="/api/v1", dependencies=[Depends(current_user)]
)
app.include_router(report_sections_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(study_types_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(price_catalog_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(billing_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(dashboard_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(worklist_router, prefix="/api/v1", dependencies=[Depends(current_user)])
app.include_router(portal_router, prefix="/api/v1")  # Portal has mixed auth requirements
# Céluma 1.3 Phase 2, Block E: token-only auth (render token, not current_user)
# — must NOT inherit reports_router's blanket Depends(current_user) above.
app.include_router(internal_render_router, prefix="/api/v1")
app.include_router(rbac_router, prefix="/api/v1", dependencies=[Depends(current_user)])
# Céluma 1.3 Phase 4, Blocks D and E: the manual reconciliation trigger
# (POST /tenant/usage/reconcile) and the tenant usage dashboard read
# (GET /tenant/usage). Neither takes a tenant identifier — both always act
# on the caller's own tenant — and both gate on admin:manage_tenant inside
# their handler.
app.include_router(tenant_usage_router, prefix="/api/v1", dependencies=[Depends(current_user)])
# Céluma 1.3 Phase 3, Block B: the notifications router resolves the bearer
# credential itself so a request with no Authorization header gets 401 rather
# than the shared scheme's 403 — it must NOT inherit a blanket
# Depends(current_user), which would re-introduce that 403 before the
# router's own dependency runs.
app.include_router(notifications_router, prefix="/api/v1")
# Céluma 1.3 Phase 3, Block D: the preference router reuses that same
# self-resolved credential dependency, so it must be registered the same way —
# without a blanket Depends(current_user) — for the same 401-not-403 reason.
app.include_router(notification_preferences_router, prefix="/api/v1")

# PROVENANCE, not the release identity. Block G (D-5) deliberately sets this
# build arg to the FULL COMMIT SHA so the value stays true when the image is
# promoted by digest — promoting must never require a rebuild (G-003). Leave
# it alone; it is what ties a running container back to its source.
CELUMA_VERSION: str = os.environ.get("CELUMA_VERSION", "dev")

# H-0c (section 10). The RELEASE identity — the human-readable semantic
# version, e.g. "v1.3.0". Separate from CELUMA_VERSION on purpose: the UI
# showed "dev" because it displayed the provenance value, and that value is a
# SHA (or the "dev" default) by design. Both are baked at RC build time, so
# adding this changes nothing about promotion: the digest still carries both
# and is tagged, never rebuilt.
#
# Empty (not "dev") when unset, so `_health_payload` can fall back to the
# provenance value and a local/dev container keeps reporting exactly what it
# reports today.
CELUMA_RELEASE: str = os.environ.get("CELUMA_RELEASE", "")


def _health_payload() -> dict:
    return {
        "status": "healthy",
        "api_version": "v1",
        # Unchanged key and unchanged meaning — existing Block A/G evidence,
        # smoke checks and dashboards that read `celuma_version` keep working.
        "celuma_version": CELUMA_VERSION,
        # H-0c: the version a human should be shown. Falls back to the
        # provenance value so this key is never absent or empty.
        "celuma_release": CELUMA_RELEASE or CELUMA_VERSION,
    }


@app.get("/")
def root():
    return {
        "message": "Welcome to Celuma API",
        "celuma_version": CELUMA_VERSION,
        "features": [
            "Multi-tenant support",
            "Laboratory management",
            "Patient management",
            "Sample tracking",
            "Report generation",
            "Billing system",
            "Audit logging",
        ],
    }


@app.get("/health")
def health_check():
    """Health check endpoint for load balancers and monitoring"""
    return _health_payload()


@app.get("/api/v1/health")
def api_health_check():
    """API health check — canonical endpoint consumed by the SPA"""
    return _health_payload()
