from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.models.user import AppUser, UserBranch
from app.models.invitation import UserInvitation
from app.models.tenant import Tenant, Branch
from app.models.enums import UserRole
from app.core.security import hash_password
from app.core.config import settings
from app.services.email import EmailService
from app.schemas.user import (
    UserCreateByAdmin,
    UserUpdateByAdmin,
    UserDetailResponse,
    UsersListResponse,
)
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel
import secrets
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users")


def split_full_name(full_name: str) -> tuple[str, str]:
    """Split full_name into first_name and last_name.
    
    If full_name has spaces, first token is first_name, rest is last_name.
    If no spaces, entire string is first_name, last_name is empty string.
    """
    if not full_name:
        return "", ""
    parts = full_name.strip().split(None, 1)  # Split on first space only
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


class InvitationCreate(BaseModel):
    """Schema for creating an invitation"""
    email: str
    full_name: str
    role: str


class InvitationResponse(BaseModel):
    """Schema for invitation response"""
    id: str
    email: str
    full_name: str
    role: str
    token: str
    expires_at: datetime


class AcceptInvitationRequest(BaseModel):
    """Schema for accepting an invitation"""
    password: str
    username: Optional[str] = None


def get_user_branch_ids(user: AppUser, session: Session) -> list[str]:
    """Helper to get branch IDs for a user, handling implicit admin access"""
    if user.role == UserRole.ADMIN:
        branches = session.exec(select(Branch.id).where(Branch.tenant_id == user.tenant_id)).all()
        return [str(bid) for bid in branches]
    return [str(ub.branch_id) for ub in user.branches]


def check_last_admin(session: Session, tenant_id: str) -> int:
    """Count active administrators for a tenant"""
    count = session.exec(
        select(AppUser).where(
            AppUser.tenant_id == tenant_id,
            AppUser.role == UserRole.ADMIN,
            AppUser.is_active == True
        )
    ).all()
    return len(count)


@router.get("/", response_model=UsersListResponse)
def list_users(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all users in the tenant (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can list users")
    
    users = session.exec(
        select(AppUser).where(AppUser.tenant_id == ctx.tenant_id)
    ).all()
    
    return UsersListResponse(
        users=[
            UserDetailResponse(
                id=str(u.id),
                tenant_id=str(u.tenant_id),
                email=u.email,
                username=u.username,
                full_name=u.full_name,
                role=u.role,
                is_active=u.is_active,
                created_at=u.created_at,
                branch_ids=get_user_branch_ids(u, session),
                avatar_url=u.avatar_url,
            )
            for u in users
        ]
    )


@router.post("/", response_model=UserDetailResponse)
def create_user(
    user_data: UserCreateByAdmin,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new user (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can create users")
    
    # Check if email already exists
    existing_email = session.exec(
        select(AppUser).where(
            AppUser.email == user_data.email,
            AppUser.tenant_id == ctx.tenant_id
        )
    ).first()
    
    if existing_email:
        raise HTTPException(400, "Email already registered for this tenant")
    
    # Check if username already exists (if provided)
    if user_data.username:
        existing_username = session.exec(
            select(AppUser).where(
                AppUser.username == user_data.username,
                AppUser.tenant_id == ctx.tenant_id
            )
        ).first()
        
        if existing_username:
            raise HTTPException(400, "Username already registered for this tenant")
    
    new_user = AppUser(
        tenant_id=ctx.tenant_id,
        email=user_data.email,
        username=user_data.username,
        full_name=user_data.full_name,
        role=UserRole(user_data.role),
        hashed_password=hash_password(user_data.password),
    )
    
    session.add(new_user)
    session.flush() # Generate ID

    # Handle branch assignment
    # Admins get implicit access, so we don't strictly need to create UserBranch records
    # But if provided, we can validate them. However, for consistency with implicit access,
    # we can choose to NOT create records for admins to keep DB clean and enforce logic.
    if user_data.branch_ids and new_user.role != UserRole.ADMIN:
        for branch_id in user_data.branch_ids:
            branch = session.get(Branch, branch_id)
            if not branch:
                raise HTTPException(400, f"Branch {branch_id} not found")
            if str(branch.tenant_id) != ctx.tenant_id:
                raise HTTPException(400, f"Branch {branch_id} does not belong to this tenant")
            
            user_branch = UserBranch(user_id=new_user.id, branch_id=branch.id)
            session.add(user_branch)

    session.commit()
    session.refresh(new_user)
    
    logger.info(
        f"User {new_user.email} created by admin",
        extra={
            "event": "user.created",
            "user_id": str(new_user.id),
            "created_by": str(user.id),
            "branch_count": len(user_data.branch_ids) if user_data.branch_ids else 0
        },
    )
    
    return UserDetailResponse(
        id=str(new_user.id),
        tenant_id=str(new_user.tenant_id),
        email=new_user.email,
        username=new_user.username,
        full_name=new_user.full_name,
        role=new_user.role,
        is_active=new_user.is_active,
        created_at=new_user.created_at,
        branch_ids=get_user_branch_ids(new_user, session),
    )


@router.put("/{user_id}", response_model=UserDetailResponse)
def update_user(
    user_id: str,
    user_data: UserUpdateByAdmin,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update a user (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can update users")
    
    target_user = session.get(AppUser, user_id)
    if not target_user:
        raise HTTPException(404, "User not found")
    
    if str(target_user.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "User does not belong to your tenant")
    
    # Safety Check: Prevent modifying the last admin if it leaves the tenant without admins
    if target_user.role == UserRole.ADMIN:
        # If demoting or deactivating
        is_demoting = user_data.role is not None and user_data.role != UserRole.ADMIN
        is_deactivating = user_data.is_active is not None and user_data.is_active is False
        
        if is_demoting or is_deactivating:
            admin_count = check_last_admin(session, ctx.tenant_id)
            if admin_count <= 1:
                raise HTTPException(400, "Cannot remove or deactivate the last administrator")

    # Update fields
    if user_data.email is not None:
        target_user.email = user_data.email
    if user_data.username is not None:
        target_user.username = user_data.username
    if user_data.full_name is not None:
        target_user.full_name = user_data.full_name
    if user_data.role is not None:
        target_user.role = UserRole(user_data.role)
    if user_data.is_active is not None:
        target_user.is_active = user_data.is_active
    if user_data.password is not None:
        target_user.hashed_password = hash_password(user_data.password)
    
    # Update branches if provided
    # For admins, we ignore branch updates or clear them as they have global access
    if target_user.role == UserRole.ADMIN:
        # If user is admin, ensure no explicit branches exist to avoid confusion
        existing_associations = session.exec(
            select(UserBranch).where(UserBranch.user_id == target_user.id)
        ).all()
        for assoc in existing_associations:
            session.delete(assoc)
    elif user_data.branch_ids is not None:
        # Clear existing
        existing_associations = session.exec(
            select(UserBranch).where(UserBranch.user_id == target_user.id)
        ).all()
        for assoc in existing_associations:
            session.delete(assoc)
            
        # Add new
        for branch_id in user_data.branch_ids:
            branch = session.get(Branch, branch_id)
            if not branch:
                raise HTTPException(400, f"Branch {branch_id} not found")
            if str(branch.tenant_id) != ctx.tenant_id:
                raise HTTPException(400, f"Branch {branch_id} does not belong to this tenant")
            
            user_branch = UserBranch(user_id=target_user.id, branch_id=branch.id)
            session.add(user_branch)

    session.add(target_user)
    session.commit()
    session.refresh(target_user)
    
    logger.info(
        f"User {target_user.email} updated by admin",
        extra={
            "event": "user.updated",
            "user_id": str(target_user.id),
            "updated_by": str(user.id),
        },
    )
    
    return UserDetailResponse(
        id=str(target_user.id),
        tenant_id=str(target_user.tenant_id),
        email=target_user.email,
        username=target_user.username,
        full_name=target_user.full_name,
        role=target_user.role,
        is_active=target_user.is_active,
        created_at=target_user.created_at,
        branch_ids=get_user_branch_ids(target_user, session),
    )


@router.delete("/{user_id}")
def deactivate_user(
    user_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Deactivate a user (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can deactivate users")
    
    target_user = session.get(AppUser, user_id)
    if not target_user:
        raise HTTPException(404, "User not found")
    
    if str(target_user.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "User does not belong to your tenant")
    
    # Prevent self-deactivation
    if str(target_user.id) == str(user.id):
        raise HTTPException(400, "Cannot deactivate your own account")
    
    # Safety Check: Prevent deactivating the last admin
    if target_user.role == UserRole.ADMIN and target_user.is_active:
        admin_count = check_last_admin(session, ctx.tenant_id)
        if admin_count <= 1:
            raise HTTPException(400, "Cannot deactivate the last administrator")

    target_user.is_active = False
    session.add(target_user)
    session.commit()
    
    logger.info(
        f"User {target_user.email} deactivated by admin",
        extra={
            "event": "user.deactivated",
            "user_id": str(target_user.id),
            "deactivated_by": str(user.id),
        },
    )
    
    return {"message": "User deactivated successfully"}


@router.post("/{user_id}/toggle-active")
def toggle_user_active(
    user_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Toggle user active status (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can toggle user status")
    
    target_user = session.get(AppUser, user_id)
    if not target_user:
        raise HTTPException(404, "User not found")
    
    if str(target_user.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "User does not belong to your tenant")
    
    # Prevent self-toggle
    if str(target_user.id) == str(user.id):
        raise HTTPException(400, "Cannot toggle your own account status")
    
    # Safety Check: Prevent deactivating the last admin
    if target_user.role == UserRole.ADMIN and target_user.is_active:
        # We are about to deactivate (since currently active)
        admin_count = check_last_admin(session, ctx.tenant_id)
        if admin_count <= 1:
            raise HTTPException(400, "Cannot deactivate the last administrator")

    target_user.is_active = not target_user.is_active
    session.add(target_user)
    session.commit()
    
    logger.info(
        f"User {target_user.email} status toggled to {target_user.is_active}",
        extra={
            "event": "user.toggled",
            "user_id": str(target_user.id),
            "toggled_by": str(user.id),
            "new_status": target_user.is_active,
        },
    )
    
    return {
        "message": f"User {'activated' if target_user.is_active else 'deactivated'}",
        "is_active": target_user.is_active
    }


# Invitation Endpoints

@router.post("/invitations", response_model=InvitationResponse)
def create_invitation(
    invitation_data: InvitationCreate,
    request: Request,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create and send user invitation (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can invite users")
    
    # Check if user already exists
    existing_user = session.exec(
        select(AppUser).where(
            AppUser.email == invitation_data.email,
            AppUser.tenant_id == ctx.tenant_id
        )
    ).first()
    
    if existing_user:
        raise HTTPException(400, "User with this email already exists")
    
    # Check if there's a pending invitation
    pending = session.exec(
        select(UserInvitation).where(
            UserInvitation.email == invitation_data.email,
            UserInvitation.tenant_id == ctx.tenant_id,
            UserInvitation.is_used == False,
            UserInvitation.expires_at > datetime.utcnow()
        )
    ).first()
    
    if pending:
        raise HTTPException(400, "There's already a pending invitation for this email")
    
    # Generate secure token
    token = secrets.token_urlsafe(32)
    
    # Create invitation
    invitation = UserInvitation(
        tenant_id=ctx.tenant_id,
        email=invitation_data.email,
        full_name=invitation_data.full_name,
        role=UserRole(invitation_data.role),
        token=token,
        expires_at=datetime.utcnow() + timedelta(days=7),
        invited_by=user.id,
    )
    
    session.add(invitation)
    session.commit()
    session.refresh(invitation)
    
    # Get tenant name for email
    tenant = session.get(Tenant, ctx.tenant_id)
    tenant_name = tenant.name if tenant else "Laboratorio"
    
    # Build invitation URL
    base_url = getattr(settings, 'frontend_url', 'http://localhost:5173')
    invitation_url = f"{base_url}/accept-invitation?token={token}"
    
    # Send email
    email_service = EmailService()
    email_sent = email_service.send_invitation_email(
        recipient_email=invitation_data.email,
        recipient_name=invitation_data.full_name,
        lab_name=tenant_name,
        invitation_url=invitation_url,
    )
    
    if not email_sent:
        logger.warning(f"Failed to send invitation email to {invitation_data.email}")
    
    logger.info(
        f"Invitation created for {invitation_data.email}",
        extra={
            "event": "invitation.created",
            "invitation_id": str(invitation.id),
            "invited_by": str(user.id),
        },
    )
    
    return InvitationResponse(
        id=str(invitation.id),
        email=invitation.email,
        full_name=invitation.full_name,
        role=invitation.role,
        token=invitation.token,
        expires_at=invitation.expires_at,
    )


@router.get("/invitations/{token}")
def get_invitation(
    token: str,
    session: Session = Depends(get_session),
):
    """Get invitation details (public endpoint for verification)"""
    invitation = session.exec(
        select(UserInvitation).where(
            UserInvitation.token == token,
            UserInvitation.is_used == False
        )
    ).first()
    
    if not invitation:
        raise HTTPException(404, "Invitation not found or already used")
    
    if invitation.expires_at < datetime.utcnow():
        raise HTTPException(400, "Invitation has expired")
    
    # Get tenant name
    tenant = session.get(Tenant, invitation.tenant_id)
    
    return {
        "email": invitation.email,
        "full_name": invitation.full_name,
        "role": invitation.role,
        "tenant_name": tenant.name if tenant else "Unknown",
        "expires_at": invitation.expires_at,
    }


@router.post("/invitations/{token}/accept")
def accept_invitation(
    token: str,
    accept_data: AcceptInvitationRequest,
    session: Session = Depends(get_session),
):
    """Accept invitation and create user account"""
    invitation = session.exec(
        select(UserInvitation).where(
            UserInvitation.token == token,
            UserInvitation.is_used == False
        )
    ).first()
    
    if not invitation:
        raise HTTPException(404, "Invitation not found or already used")
    
    if invitation.expires_at < datetime.utcnow():
        raise HTTPException(400, "Invitation has expired")
    
    # Check if username is unique (if provided)
    if accept_data.username:
        existing = session.exec(
            select(AppUser).where(
                AppUser.username == accept_data.username,
                AppUser.tenant_id == invitation.tenant_id
            )
        ).first()
        
        if existing:
            raise HTTPException(400, "Username already taken")
    
    # Create user
    new_user = AppUser(
        tenant_id=invitation.tenant_id,
        email=invitation.email,
        username=accept_data.username,
        full_name=invitation.full_name,
        role=invitation.role,
        hashed_password=hash_password(accept_data.password),
        is_active=True,
    )
    
    session.add(new_user)
    
    # Mark invitation as used
    invitation.is_used = True
    invitation.accepted_at = datetime.utcnow()
    session.add(invitation)
    
    session.commit()
    session.refresh(new_user)
    
    logger.info(
        f"Invitation accepted, user {new_user.email} created",
        extra={
            "event": "invitation.accepted",
            "invitation_id": str(invitation.id),
            "user_id": str(new_user.id),
        },
    )
    
    return {
        "message": "Account created successfully",
        "user_id": str(new_user.id),
        "email": new_user.email,
        "branch_ids": [],
    }


# Profile Avatar Endpoint

@router.post("/{user_id}/avatar")
def upload_user_avatar(
    user_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Upload user avatar with image processing for HDR normalization"""
    target_user = session.get(AppUser, user_id)
    if not target_user:
        raise HTTPException(404, "User not found")
    
    # Users can only upload their own avatar, unless admin
    if str(target_user.id) != str(user.id) and user.role != UserRole.ADMIN:
        raise HTTPException(403, "Can only upload your own avatar")
    
    if str(target_user.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "User does not belong to your tenant")
    
    # Validate file type - accept more formats including HEIC/HEIF
    content_type = (file.content_type or "").lower()
    allowed_types = ["image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif"]
    if not any(img_type in content_type for img_type in allowed_types):
        raise HTTPException(400, "Only image files (JPEG, PNG, WEBP, HEIC) are allowed")
    
    # Read file bytes
    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")
    
    # Validate file size (max 10MB for raw upload)
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "File size must be less than 10MB")
    
    # Process image: normalize HDR, convert to sRGB, resize and optimize
    from app.services.image_processing import process_avatar_bytes
    try:
        processed = process_avatar_bytes(file_bytes, max_size=(512, 512), quality=90)
    except Exception as e:
        logger.error(f"Image processing failed: {str(e)}", extra={"event": "user.avatar_processing_failed", "user_id": user_id})
        raise HTTPException(400, "Failed to process image. Please try a different image.")
    
    # Upload processed JPEG to S3
    from app.services.s3 import S3Service
    s3 = S3Service()
    key = f"avatars/{user_id}/avatar.jpg"  # Always save as JPEG
    s3.upload_bytes(processed.jpeg_bytes, key=key, content_type=processed.content_type)
    
    # Update user avatar_url with cache-busting timestamp
    import time
    avatar_url = f"{s3.object_public_url(key)}?v={int(time.time())}"
    target_user.avatar_url = avatar_url
    session.add(target_user)
    session.commit()
    
    logger.info(
        f"Avatar uploaded for user {target_user.email}",
        extra={
            "event": "user.avatar_uploaded",
            "user_id": str(target_user.id),
        },
    )
    
    return {
        "message": "Avatar uploaded successfully",
        "avatar_url": target_user.avatar_url
    }
