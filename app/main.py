from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
import logging
import time
import uuid
from contextlib import asynccontextmanager
from collections import defaultdict
import asyncio
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

# Startup/shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Celuma API starting up...")
    yield
    logger.info("🛑 Celuma API shutting down...")

app = FastAPI(
    title="Celuma API", 
    description="Multi-tenant Laboratory Management System",
    lifespan=lifespan
)

# Add security middlewares
app.add_middleware(
    TrustedHostMiddleware, 
    allowed_hosts=["localhost", "127.0.0.1", "*.localhost", "backend", "frontend"]
)

# Add CORS middleware (more secure in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "*"],  # In production, specify exact origins
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

# Request size limiting middleware
@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    # Skip size check for health endpoints
    if request.url.path in ["/", "/health", "/api/v1/health"]:
        return await call_next(request)
    
    # Check Content-Length header
    content_length = request.headers.get("content-length")
    if content_length:
        content_length = int(content_length)
        max_size = 10 * 1024 * 1024  # 10MB
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
    if request.url.path in ["/", "/health", "/api/v1/health"]:
        return await call_next(request)
    
    client_ip = request.client.host if request.client else "unknown"
    current_time = time.time()
    window_size = 60  # 1 minute
    max_requests = 100  # 100 requests per minute per IP
    
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

# Enhanced logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Skip logging for health endpoints to avoid noise
    if request.url.path in ["/", "/health", "/api/v1/health"]:
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
    logger.info(f"🔥 [{request_id[:8]}] INCOMING REQUEST: {request.method} {request.url} | client={client_ip}")
    
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

@app.get("/health")
def health_check():
    """Health check endpoint for load balancers and monitoring"""
    return {"status": "healthy", "version": "1.0.0"}

@app.get("/api/v1/health")
def api_health_check():
    """API-specific health check"""
    return {"status": "healthy", "api_version": "v1"}
