from datetime import datetime, timedelta
from passlib.context import CryptContext
from jose import jwt, JWTError
from app.core.config import settings

# Use pbkdf2_sha256 exclusively - no 72-byte password limit, more secure,
# and avoids bcrypt backend compatibility issues across environments.
pwd_context = CryptContext(
    schemes=["pbkdf2_sha256"],
    deprecated="auto",
    pbkdf2_sha256__default_rounds=29000,
)

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_jwt(sub: str) -> str:
    exp = datetime.utcnow() + timedelta(minutes=settings.jwt_expires_min)
    return jwt.encode({"sub": sub, "exp": exp}, settings.jwt_secret, algorithm="HS256")

def decode_jwt(token: str) -> dict:
    """Decode JWT token and return payload"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return payload
    except JWTError:
        return None

def is_token_expired(token: str) -> bool:
    """Check if a JWT token is expired"""
    payload = decode_jwt(token)
    if not payload:
        return True
    
    exp_timestamp = payload.get("exp")
    if not exp_timestamp:
        return True
    
    exp_datetime = datetime.fromtimestamp(exp_timestamp)
    return datetime.utcnow() > exp_datetime

def get_token_expiration(token: str) -> datetime:
    """Get the expiration datetime of a JWT token"""
    payload = decode_jwt(token)
    if not payload:
        return None

    exp_timestamp = payload.get("exp")
    if not exp_timestamp:
        return None

    return datetime.fromtimestamp(exp_timestamp)


# Céluma 1.3 Fase 2, Bloque E: PDF render tokens. Deliberately separate from
# `create_jwt`/`decode_jwt` (user session tokens): different secret
# (`settings.effective_pdf_render_token_secret`), different, narrower payload
# shape (`type`, `report_version_id`, `tenant_id`), and never checked against
# `BlacklistedToken` — a render token authorizes a single headless-browser
# render of one specific report_version_id for a few seconds, not a user
# session. See pdf-generation-contract.md.
RENDER_TOKEN_TYPE = "pdf_render"


def create_render_token(report_version_id: str, tenant_id: str, expires_seconds: int) -> str:
    exp = datetime.utcnow() + timedelta(seconds=expires_seconds)
    payload = {
        "type": RENDER_TOKEN_TYPE,
        "report_version_id": report_version_id,
        "tenant_id": tenant_id,
        "exp": exp,
    }
    return jwt.encode(payload, settings.effective_pdf_render_token_secret, algorithm="HS256")


def verify_render_token(token: str) -> dict:
    """Decode and validate a PDF render token. Returns the payload dict.

    Raises ValueError if the token is missing, malformed, expired, or not a
    render token (e.g. a normal user session JWT presented by mistake).
    """
    try:
        payload = jwt.decode(
            token, settings.effective_pdf_render_token_secret, algorithms=["HS256"]
        )
    except JWTError as exc:
        raise ValueError("Invalid or expired render token") from exc
    if payload.get("type") != RENDER_TOKEN_TYPE:
        raise ValueError("Not a render token")
    if not payload.get("report_version_id") or not payload.get("tenant_id"):
        raise ValueError("Malformed render token")
    return payload
