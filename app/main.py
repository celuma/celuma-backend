from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import logging
import time
from app.api.v1.users import router as users_router
from app.api.v1.auth import router as auth_router
from app.api.v1.tenants import router as tenants_router
from app.api.v1.branches import router as branches_router
from app.api.v1.patients import router as patients_router
from app.api.v1.laboratory import router as laboratory_router
from app.api.v1.reports import router as reports_router
from app.api.v1.billing import router as billing_router

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Celuma API", description="Multi-tenant Laboratory Management System")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()

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
    logger.info(f"🔥 INCOMING REQUEST: {request.method} {request.url} | client={client_ip}")
    try:
        logger.info(f"📋 Headers: {_redact_headers(dict(request.headers))}")
    except Exception:
        # Defensive: avoid breaking middleware due to header serialization
        logger.warning("Could not serialize request headers for logging")

    status_code: int = 0
    response: Response = None  # type: ignore
    try:
        # Process request
        response = await call_next(request)
        status_code = getattr(response, "status_code", 200)
        return response
    except Exception:
        status_code = 500
        # Log full traceback for unhandled exceptions
        logger.exception("Unhandled exception while processing request")
        # Re-raise to let FastAPI default handlers respond
        raise
    finally:
        process_time = time.time() - start_time
        logger.info(f"✅ RESPONSE: {status_code} | Time: {process_time:.3f}s")
        if response is not None:
            try:
                logger.info(f"📤 Response Headers: {dict(response.headers)}")
            except Exception:
                logger.warning("Could not serialize response headers for logging")

app.include_router(users_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(tenants_router, prefix="/api/v1")
app.include_router(branches_router, prefix="/api/v1")
app.include_router(patients_router, prefix="/api/v1")
app.include_router(laboratory_router, prefix="/api/v1")
app.include_router(reports_router, prefix="/api/v1")
app.include_router(billing_router, prefix="/api/v1")

@app.get("/")
def root():
    return {
        "message": "Welcome to Celuma API",
        "version": "1.0.0",
        "features": [
            "Multi-tenant support",
            "Laboratory management",
            "Patient management",
            "Sample tracking",
            "Report generation",
            "Billing system",
            "Audit logging"
        ]
    }
