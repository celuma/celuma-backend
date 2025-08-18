from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer
from jose import jwt, JWTError
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.user import AppUser, BlacklistedToken
from app.core.security import hash_password, verify_password, create_jwt, decode_jwt
from app.core.config import settings
from app.schemas.auth import UserRegister, UserLogin, UserResponse, LoginResponse, LogoutResponse, UserProfile
from datetime import datetime

router = APIRouter(prefix="/auth")
scheme = HTTPBearer()

@router.post("/register", response_model=UserResponse)
def register(user_data: UserRegister, session: Session = Depends(get_session)):
    """Register a new user"""
    if session.exec(select(AppUser).where(AppUser.email == user_data.email, AppUser.tenant_id == user_data.tenant_id)).first():
        raise HTTPException(400, "Email already registered for this tenant")
    u = AppUser(email=user_data.email, full_name=user_data.full_name, role=user_data.role, tenant_id=user_data.tenant_id, hashed_password=hash_password(user_data.password))
    session.add(u)
    session.commit()
    session.refresh(u)
    return UserResponse(id=str(u.id), email=u.email, full_name=u.full_name, role=u.role)

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

@router.post("/login", response_model=LoginResponse)
def login(credentials: UserLogin, session: Session = Depends(get_session)):
    """Login user with email and password"""
    u = session.exec(select(AppUser).where(AppUser.email == credentials.email, AppUser.tenant_id == credentials.tenant_id)).first()
    if not u or not verify_password(credentials.password, u.hashed_password):
        raise HTTPException(401, "Invalid credentials")
    return LoginResponse(access_token=create_jwt(sub=str(u.id)), token_type="bearer")

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
        full_name=user.full_name, 
        role=user.role, 
        tenant_id=str(user.tenant_id)
    )
