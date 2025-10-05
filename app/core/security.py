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
