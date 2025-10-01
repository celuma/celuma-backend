from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer
from jose import jwt, JWTError
from sqlmodel import select, Session
import logging
from app.core.db import get_session
from app.models.user import AppUser, BlacklistedToken, UserBranch
from app.models.tenant import Tenant, Branch
from app.models.enums import UserRole
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
    UserProfileUpdate,
    RegistrationRequest,
    RegistrationResponse,
)
from datetime import datetime
from typing import Union
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")
scheme = HTTPBearer()

@router.post("/register", response_model=UserResponse)
def register(user_data: UserRegister, session: Session = Depends(get_session)):
    """Register a new user"""
    logger.info(
        "Register request received",
        extra={
            "event": "auth.register",
            "email": user_data.email,
            "username": user_data.username,
            "tenant_id": str(user_data.tenant_id),
        },
    )
    # Check if email already exists for this tenant
    if session.exec(select(AppUser).where(AppUser.email == user_data.email, AppUser.tenant_id == user_data.tenant_id)).first():
        logger.warning("Email already registered for tenant", extra={"event": "auth.register.conflict_email", "email": user_data.email, "tenant_id": str(user_data.tenant_id)})
        raise HTTPException(400, "Email already registered for this tenant")
    
    # Check if username already exists for this tenant (if provided)
    if user_data.username:
        if session.exec(select(AppUser).where(AppUser.username == user_data.username, AppUser.tenant_id == user_data.tenant_id)).first():
            logger.warning("Username already registered for tenant", extra={"event": "auth.register.conflict_username", "username": user_data.username, "tenant_id": str(user_data.tenant_id)})
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
    logger.info("User registered successfully", extra={"event": "auth.register.success", "user_id": str(u.id), "email": u.email, "tenant_id": str(u.tenant_id)})
    return UserResponse(
        id=str(u.id), 
        email=u.email, 
        username=u.username,
        full_name=u.full_name, 
        role=u.role
    )

def current_user(request: Request, token=Depends(scheme), session: Session = Depends(get_session)):
    request_id = getattr(request.state, "request_id", "unknown")[:8]
    logger.info(f"🔐 [{request_id}] Authenticating token: {token.credentials[:20]}...")
    
    try:
        # Check if token is blacklisted
        blacklisted = session.exec(select(BlacklistedToken).where(BlacklistedToken.token == token.credentials)).first()
        if blacklisted:
            logger.warning(f"🚫 [{request_id}] Token is blacklisted: {token.credentials[:20]}...")
            raise HTTPException(401, "Token has been revoked")
        
        payload = jwt.decode(token.credentials, settings.jwt_secret, algorithms=["HS256"])
        uid = payload["sub"]
        logger.info(f"✅ [{request_id}] Token decoded successfully, user ID: {uid}")
    except JWTError as e:
        logger.error(f"❌ [{request_id}] JWT decode error: {str(e)}")
        raise HTTPException(401, "Invalid token")
    
    u = session.get(AppUser, uid)
    if not u:
        logger.error(f"❌ [{request_id}] User not found: {uid}")
        raise HTTPException(401, "User not found")
    if not u.is_active:
        logger.error(f"❌ [{request_id}] User inactive: {uid}")
        raise HTTPException(401, "Inactive user")
        
    logger.info(f"🎉 [{request_id}] Authentication successful for user: {u.email}")
    return u


class AuthContext(BaseModel):
    """Lightweight auth context extracted from the current user.

    This object is designed to be passed as a dependency to endpoints which need
    quick access to the authenticated tenant and user identifiers for
    multi-tenant scoping.
    """

    user_id: str
    tenant_id: str


def get_auth_ctx(user: AppUser = Depends(current_user)) -> AuthContext:
    """Return the authentication context for the current request.

    It exposes the `tenant_id` and `user_id` to be used in database queries to
    enforce tenant isolation.
    """
    return AuthContext(user_id=str(user.id), tenant_id=str(user.tenant_id))

@router.post("/login", response_model=Union[LoginResponse, LoginTenantSelectionResponse])
def login(credentials: UserLogin, session: Session = Depends(get_session)):
    """Login user with username/email and password"""
    logger.info(
        "Login attempt",
        extra={
            "event": "auth.login.attempt",
            "username_or_email": credentials.username_or_email,
            "tenant_id": str(credentials.tenant_id) if credentials.tenant_id else None,
        },
    )
    # If tenant_id is provided, behave as before (tenant-scoped login)
    if credentials.tenant_id:
        user = session.exec(
            select(AppUser).where(
                (AppUser.username == credentials.username_or_email) | (AppUser.email == credentials.username_or_email),
                AppUser.tenant_id == credentials.tenant_id,
            )
        ).first()

        if not user or not verify_password(credentials.password, user.hashed_password):
            logger.warning("Invalid credentials (tenant-scoped)", extra={"event": "auth.login.invalid_credentials", "username_or_email": credentials.username_or_email, "tenant_id": str(credentials.tenant_id)})
            raise HTTPException(401, "Invalid credentials")
        if not user.is_active:
            logger.warning("Inactive user login attempt", extra={"event": "auth.login.inactive", "user_id": str(user.id), "tenant_id": str(user.tenant_id)})
            raise HTTPException(401, "User account is inactive")
        logger.info("Login success", extra={"event": "auth.login.success", "user_id": str(user.id), "tenant_id": str(user.tenant_id)})
        return LoginResponse(access_token=create_jwt(sub=str(user.id)), token_type="bearer", tenant_id=str(user.tenant_id))

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
        logger.warning("Invalid credentials (multi-tenant)", extra={"event": "auth.login.invalid_credentials", "username_or_email": credentials.username_or_email})
        raise HTTPException(401, "Invalid credentials")

    if len(valid_users) == 1:
        user = valid_users[0]
        logger.info("Login success (single match)", extra={"event": "auth.login.success", "user_id": str(user.id), "tenant_id": str(user.tenant_id)})
        return LoginResponse(access_token=create_jwt(sub=str(user.id)), token_type="bearer", tenant_id=str(user.tenant_id))

    # Multiple tenants: return selection list
    options = []
    for u in valid_users:
        tenant = session.get(Tenant, u.tenant_id)
        options.append(TenantOption(tenant_id=str(u.tenant_id), tenant_name=tenant.name if tenant else "Unknown"))

    logger.info("Login requires tenant selection", extra={"event": "auth.login.need_tenant_selection", "options_count": len(options)})
    return LoginTenantSelectionResponse(need_tenant_selection=True, options=options)

@router.post("/logout", response_model=LogoutResponse)
def logout(token: str = Depends(scheme), session: Session = Depends(get_session)):
    """Logout user by blacklisting the current token"""
    try:
        logger.info("Logout requested", extra={"event": "auth.logout.request", "token_preview": token.credentials[:20] + "..."})
        # Decode token to get user ID and expiration
        payload = decode_jwt(token.credentials)
        if not payload:
            logger.warning("Invalid token on logout", extra={"event": "auth.logout.invalid_token"})
            raise HTTPException(401, "Invalid token")
        
        user_id = payload.get("sub")
        exp_timestamp = payload.get("exp")
        
        if not user_id or not exp_timestamp:
            logger.warning("Invalid token payload on logout", extra={"event": "auth.logout.invalid_payload"})
            raise HTTPException(401, "Invalid token payload")
        
        # Check if token is already blacklisted
        existing_blacklist = session.exec(select(BlacklistedToken).where(BlacklistedToken.token == token.credentials)).first()
        if existing_blacklist:
            logger.info("Token already revoked", extra={"event": "auth.logout.already_revoked", "user_id": str(user_id)})
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
        logger.info("Logout successful", extra={"event": "auth.logout.success", "user_id": str(user_id)})
        
        return LogoutResponse(message="Logout successful", token_revoked=True)
        
    except Exception as e:
        session.rollback()
        logger.exception("Logout failed")
        raise HTTPException(500, f"Logout failed: {str(e)}")

@router.get("/me", response_model=UserProfile)
def me(request: Request, user: AppUser = Depends(current_user)):
    """Get current user profile"""
    request_id = getattr(request.state, "request_id", "unknown")[:8]
    logger.info(f"🔍 [{request_id}] GET /auth/me called for user ID: {user.id}")
    logger.info(f"👤 [{request_id}] User details: email={user.email}, username={user.username}, role={user.role}")
    
    profile = UserProfile(
        id=str(user.id), 
        email=user.email, 
        username=user.username,
        full_name=user.full_name, 
        role=user.role, 
        tenant_id=str(user.tenant_id)
    )
    
    logger.info(f"📤 [{request_id}] Returning profile: {profile.dict()}")
    return profile

@router.put("/me", response_model=UserProfile)
def update_me(
    request: Request,
    data: UserProfileUpdate,
    user: AppUser = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Update current user profile and/or password"""
    request_id = getattr(request.state, "request_id", "unknown")[:8]
    logger.info(f"🔄 [{request_id}] PUT /auth/me called for user ID: {user.id}")
    logger.info(f"📝 [{request_id}] Update data received: {data.dict(exclude_none=True)}")
    logger.info(f"👤 [{request_id}] Current user: email={user.email}, username={user.username}")
    # Validate and update username
    if data.username is not None and data.username != user.username:
        existing_username = session.exec(
            select(AppUser).where(
                AppUser.username == data.username,
                AppUser.tenant_id == user.tenant_id,
                AppUser.id != user.id,
            )
        ).first()
        if existing_username:
            raise HTTPException(400, "Username already registered for this tenant")
        user.username = data.username

    # Validate and update email
    if data.email is not None and data.email != user.email:
        existing_email = session.exec(
            select(AppUser).where(
                AppUser.email == data.email,
                AppUser.tenant_id == user.tenant_id,
                AppUser.id != user.id,
            )
        ).first()
        if existing_email:
            raise HTTPException(400, "Email already registered for this tenant")
        user.email = data.email

    # Update full name
    if data.full_name is not None:
        user.full_name = data.full_name

    # Change password if requested
    if data.new_password:
        if not data.current_password:
            raise HTTPException(400, "Current password is required to set a new password")
        if not verify_password(data.current_password, user.hashed_password):
            raise HTTPException(400, "Current password is incorrect")
        user.hashed_password = hash_password(data.new_password)

    session.add(user)
    session.commit()
    session.refresh(user)

    updated_profile = UserProfile(
        id=str(user.id),
        email=user.email,
        username=user.username,
        full_name=user.full_name,
        role=user.role,
        tenant_id=str(user.tenant_id),
    )
    
    logger.info(f"✅ [{request_id}] Profile updated successfully: {updated_profile.dict()}")
    return updated_profile


@router.post("/register/unified", response_model=RegistrationResponse)
def unified_registration(payload: RegistrationRequest, session: Session = Depends(get_session)):
    """Create tenant, default branch, and admin user in a single atomic operation."""
    try:
        logger.info(
            "Unified registration requested",
            extra={
                "event": "auth.register_unified.request",
                "tenant_name": payload.tenant.name,
                "branch_code": payload.branch.code,
                "admin_email": payload.admin_user.email,
            },
        )
        # Start explicit transaction for atomicity
        with session.begin():
            # 1) Create tenant
            tenant = Tenant(
                name=payload.tenant.name,
                legal_name=payload.tenant.legal_name,
                tax_id=payload.tenant.tax_id,
            )
            session.add(tenant)
            session.flush()  # ensure tenant.id is available
            logger.info("Tenant created", extra={"event": "auth.register_unified.tenant_created", "tenant_id": str(tenant.id)})

            # 2) Create branch (ensure code uniqueness within tenant)
            existing_branch = session.exec(
                select(Branch).where(Branch.tenant_id == tenant.id, Branch.code == payload.branch.code)
            ).first()
            if existing_branch:
                logger.warning("Branch code already exists for tenant", extra={"event": "auth.register_unified.conflict_branch", "tenant_id": str(tenant.id), "branch_code": payload.branch.code})
                raise HTTPException(400, "Branch code already exists for this tenant")

            branch = Branch(
                tenant_id=tenant.id,
                code=payload.branch.code,
                name=payload.branch.name,
                timezone=payload.branch.timezone,
                address_line1=payload.branch.address_line1,
                address_line2=payload.branch.address_line2,
                city=payload.branch.city,
                state=payload.branch.state,
                postal_code=payload.branch.postal_code,
                country=payload.branch.country,
            )
            session.add(branch)
            session.flush()
            logger.info("Branch created", extra={"event": "auth.register_unified.branch_created", "branch_id": str(branch.id), "tenant_id": str(tenant.id)})

            # 3) Create admin user (ensure email/username uniqueness within tenant)
            existing_email = session.exec(
                select(AppUser).where(AppUser.email == payload.admin_user.email, AppUser.tenant_id == tenant.id)
            ).first()
            if existing_email:
                logger.warning("Admin email already registered for tenant", extra={"event": "auth.register_unified.conflict_email", "email": payload.admin_user.email, "tenant_id": str(tenant.id)})
                raise HTTPException(400, "Email already registered for this tenant")

            if payload.admin_user.username:
                existing_username = session.exec(
                    select(AppUser).where(
                        AppUser.username == payload.admin_user.username,
                        AppUser.tenant_id == tenant.id,
                    )
                ).first()
                if existing_username:
                    logger.warning("Admin username already registered for tenant", extra={"event": "auth.register_unified.conflict_username", "username": payload.admin_user.username, "tenant_id": str(tenant.id)})
                    raise HTTPException(400, "Username already registered for this tenant")

            user = AppUser(
                tenant_id=tenant.id,
                email=payload.admin_user.email,
                username=payload.admin_user.username,
                full_name=payload.admin_user.full_name,
                role=UserRole.ADMIN,
                hashed_password=hash_password(payload.admin_user.password),
            )
            session.add(user)
            session.flush()
            logger.info("Admin user created", extra={"event": "auth.register_unified.user_created", "user_id": str(user.id), "tenant_id": str(tenant.id)})

            # 4) Associate user with branch
            session.add(UserBranch(user_id=user.id, branch_id=branch.id))
            logger.info("User associated to branch", extra={"event": "auth.register_unified.user_branch_associated", "user_id": str(user.id), "branch_id": str(branch.id)})

        # After context, transaction committed
        logger.info("Unified registration success", extra={"event": "auth.register_unified.success", "tenant_id": str(tenant.id), "branch_id": str(branch.id), "user_id": str(user.id)})
        return RegistrationResponse(
            tenant_id=str(tenant.id),
            branch_id=str(branch.id),
            user_id=str(user.id),
        )
    except HTTPException:
        # Propagate known errors
        logger.warning("Unified registration failed with HTTP error", extra={"event": "auth.register_unified.error_http"})
        raise
    except Exception as e:
        session.rollback()
        logger.exception("Unified registration failed")
        raise HTTPException(500, f"Registration failed: {str(e)}")
