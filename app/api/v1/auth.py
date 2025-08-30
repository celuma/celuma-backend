from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer
from jose import jwt, JWTError
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.user import AppUser, BlacklistedToken
from app.models.tenant import Tenant
from app.core.security import hash_password, verify_password, create_jwt, decode_jwt
from app.core.config import settings
from app.schemas.auth import (
    UserRegister,
    UserLogin,
    UserResponse,
    LoginResponse,
    LogoutResponse,
    UserProfile,
    LoginTenantSelectionResponse,
    TenantOption,
)
from datetime import datetime
from typing import Union

router = APIRouter(prefix="/auth")
scheme = HTTPBearer()

@router.post("/register", response_model=UserResponse)
def register(user_data: UserRegister, session: Session = Depends(get_session)):
    """Register a new user"""
    # Check if email already exists for this tenant
    if session.exec(select(AppUser).where(AppUser.email == user_data.email, AppUser.tenant_id == user_data.tenant_id)).first():
        raise HTTPException(400, "Email already registered for this tenant")
    
    # Check if username already exists for this tenant (if provided)
    if user_data.username:
        if session.exec(select(AppUser).where(AppUser.username == user_data.username, AppUser.tenant_id == user_data.tenant_id)).first():
            raise HTTPException(400, "Username already registered for this tenant")
    
    u = AppUser(
        email=user_data.email, 
        username=user_data.username,
        full_name=user_data.full_name, 
        role=user_data.role, 
        tenant_id=user_data.tenant_id, 
        hashed_password=hash_password(user_data.password)
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return UserResponse(
        id=str(u.id), 
        email=u.email, 
        username=u.username,
        full_name=u.full_name, 
        role=u.role
    )

def current_user(token=Depends(scheme), session: Session = Depends(get_session)):
    try:
        # Check if token is blacklisted
        blacklisted = session.exec(select(BlacklistedToken).where(BlacklistedToken.token == token.credentials)).first()
        if blacklisted:
            raise HTTPException(401, "Token has been revoked")
        
        payload = jwt.decode(token.credentials, settings.jwt_secret, algorithms=["HS256"])
        uid = payload["sub"]
    except JWTError:
        raise HTTPException(401, "Invalid token")
    
    u = session.get(AppUser, uid)
    if not u or not u.is_active:
        raise HTTPException(401, "Inactive user")
    return u

@router.post("/login", response_model=Union[LoginResponse, LoginTenantSelectionResponse])
def login(credentials: UserLogin, session: Session = Depends(get_session)):
    """Login user with username/email and password"""
    # If tenant_id is provided, behave as before (tenant-scoped login)
    if credentials.tenant_id:
        user = session.exec(
            select(AppUser).where(
                (AppUser.username == credentials.username_or_email) | (AppUser.email == credentials.username_or_email),
                AppUser.tenant_id == credentials.tenant_id,
            )
        ).first()

        if not user or not verify_password(credentials.password, user.hashed_password):
            raise HTTPException(401, "Invalid credentials")
        if not user.is_active:
            raise HTTPException(401, "User account is inactive")
        return LoginResponse(access_token=create_jwt(sub=str(user.id)), token_type="bearer")

    # No tenant_id provided: find matches across all tenants
    candidates = []
    by_username = session.exec(select(AppUser).where(AppUser.username == credentials.username_or_email)).all()
    by_email = session.exec(select(AppUser).where(AppUser.email == credentials.username_or_email)).all()

    # Deduplicate by user id
    seen = set()
    for u in by_username + by_email:
        if u.id not in seen:
            seen.add(u.id)
            candidates.append(u)

    # Filter by active and password match
    valid_users = [u for u in candidates if u.is_active and verify_password(credentials.password, u.hashed_password)]

    if not valid_users:
        raise HTTPException(401, "Invalid credentials")

    if len(valid_users) == 1:
        user = valid_users[0]
        return LoginResponse(access_token=create_jwt(sub=str(user.id)), token_type="bearer")

    # Multiple tenants: return selection list
    options = []
    for u in valid_users:
        tenant = session.get(Tenant, u.tenant_id)
        options.append(TenantOption(tenant_id=str(u.tenant_id), tenant_name=tenant.name if tenant else "Unknown"))

    return LoginTenantSelectionResponse(need_tenant_selection=True, options=options)

@router.post("/logout", response_model=LogoutResponse)
def logout(token: str = Depends(scheme), session: Session = Depends(get_session)):
    """Logout user by blacklisting the current token"""
    try:
        # Decode token to get user ID and expiration
        payload = decode_jwt(token.credentials)
        if not payload:
            raise HTTPException(401, "Invalid token")
        
        user_id = payload.get("sub")
        exp_timestamp = payload.get("exp")
        
        if not user_id or not exp_timestamp:
            raise HTTPException(401, "Invalid token payload")
        
        # Check if token is already blacklisted
        existing_blacklist = session.exec(select(BlacklistedToken).where(BlacklistedToken.token == token.credentials)).first()
        if existing_blacklist:
            return LogoutResponse(message="Token already revoked", token_revoked=False)
        
        # Create blacklisted token record
        expires_at = datetime.fromtimestamp(exp_timestamp)
        blacklisted_token = BlacklistedToken(
            token=token.credentials,
            user_id=user_id,
            expires_at=expires_at
        )
        
        session.add(blacklisted_token)
        session.commit()
        
        return LogoutResponse(message="Logout successful", token_revoked=True)
        
    except Exception as e:
        session.rollback()
        raise HTTPException(500, f"Logout failed: {str(e)}")

@router.get("/me", response_model=UserProfile)
def me(user: AppUser = Depends(current_user)):
    """Get current user profile"""
    return UserProfile(
        id=str(user.id), 
        email=user.email, 
        username=user.username,
        full_name=user.full_name, 
        role=user.role, 
        tenant_id=str(user.tenant_id)
    )
