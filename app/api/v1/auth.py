from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer
from jose import jwt, JWTError
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.user import AppUser
from app.core.security import hash_password, verify_password, create_jwt
from app.core.config import settings

router = APIRouter(prefix="/auth")
scheme = HTTPBearer()

@router.post("/register")
def register(email: str, password: str, full_name: str, role: str, tenant_id: str, session: Session = Depends(get_session)):
    if session.exec(select(AppUser).where(AppUser.email == email, AppUser.tenant_id == tenant_id)).first():
        raise HTTPException(400, "Email already registered for this tenant")
    u = AppUser(email=email, full_name=full_name, role=role, tenant_id=tenant_id, hashed_password=hash_password(password))
    session.add(u)
    session.commit()
    session.refresh(u)
    return {"id": str(u.id), "email": u.email, "full_name": u.full_name, "role": u.role}

def current_user(token=Depends(scheme), session: Session = Depends(get_session)):
    try:
        payload = jwt.decode(token.credentials, settings.jwt_secret, algorithms=["HS256"])
        uid = payload["sub"]
    except JWTError:
        raise HTTPException(401, "Invalid token")
    u = session.get(AppUser, uid)
    if not u or not u.is_active:
        raise HTTPException(401, "Inactive user")
    return u

@router.post("/login")
def login(email: str, password: str, tenant_id: str, session: Session = Depends(get_session)):
    u = session.exec(select(AppUser).where(AppUser.email == email, AppUser.tenant_id == tenant_id)).first()
    if not u or not verify_password(password, u.hashed_password):
        raise HTTPException(401, "Invalid credentials")
    return {"access_token": create_jwt(sub=str(u.id)), "token_type": "bearer"}

@router.get("/me")
def me(user: AppUser = Depends(current_user)):
    return {"id": str(user.id), "email": user.email, "full_name": user.full_name, "role": user.role, "tenant_id": str(user.tenant_id)}
