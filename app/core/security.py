from datetime import datetime, timedelta
from passlib.context import CryptContext
from jose import jwt
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_jwt(sub: str) -> str:
    exp = datetime.utcnow() + timedelta(minutes=settings.jwt_expires_min)
    return jwt.encode({"sub": sub, "exp": exp}, settings.jwt_secret, algorithm="HS256")
