from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer
from jose import jwt, JWTError
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.user import User
from app.core.security import hash_password, verify_password, create_jwt
from app.core.config import settings

router = APIRouter(prefix="/auth")
scheme = HTTPBearer()

@router.post("/register")
def register(email: str, password: str, session: Session = Depends(get_session)):
    if session.exec(select(User).where(User.email == email)).first():
        raise HTTPException(400, "Email already registered")
    u = User(email=email, hashed_password=hash_password(password))
    session.add(u)
    session.commit()
    session.refresh(u)
    return {"id": u.id, "email": u.email}

def current_user(token=Depends(scheme), session: Session = Depends(get_session)):
    try:
        payload = jwt.decode(token.credentials, settings.jwt_secret, algorithms=["HS256"])
        uid = int(payload["sub"])
    except JWTError:
        raise HTTPException(401, "Invalid token")
    u = session.get(User, uid)
    if not u or not u.is_active:
        raise HTTPException(401, "Inactive user")
    return u

@router.post("/login")
def login(email: str, password: str, session: Session = Depends(get_session)):
    u = session.exec(select(User).where(User.email == email)).first()
    if not u or not verify_password(password, u.hashed_password):
        raise HTTPException(401, "Invalid credentials")
    return {"access_token": create_jwt(sub=str(u.id)), "token_type": "bearer"}

@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email}
