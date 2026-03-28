from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.core.rbac import (
    get_user_roles,
    get_user_permissions,
    assign_role_by_code,
    replace_user_roles,
    has_permission,
    user_has_full_branch_access,
    count_active_users_with_role,
)
from app.models.user import AppUser, UserBranch
from app.models.invitation import UserInvitation
from app.models.tenant import Tenant, Branch
from app.models.role import Role
from app.models.user_role import UserRoleLink
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
    if not full_name:
        return "", ""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


class InvitationCreate(BaseModel):
    email: str
    full_name: str
    role: str


class InvitationResponse(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    token: str
    expires_at: datetime


class AcceptInvitationRequest(BaseModel):
    password: str
    username: Optional[str] = None


def _get_user_branch_ids(user: AppUser, session: Session) -> list[str]:
    """Return branch IDs for a user; admins/superusers get all tenant branches implicitly."""
    if user_has_full_branch_access(user.id, session):
        branches = session.exec(select(Branch.id).where(Branch.tenant_id == user.tenant_id)).all()
        return [str(bid) for bid in branches]
    return [str(ub.branch_id) for ub in user.branches]



def _build_user_detail(u: AppUser, session: Session) -> UserDetailResponse:
    return UserDetailResponse(
        id=str(u.id),
        tenant_id=str(u.tenant_id),
        email=u.email,
        username=u.username,
        full_name=u.full_name,
        roles=get_user_roles(u.id, session),
        is_active=u.is_active,
        created_at=u.created_at,
        branch_ids=_get_user_branch_ids(u, session),
        avatar_url=u.avatar_url,
    )


def _check_manage_users(actor: AppUser, session: Session) -> None:
    """Raise 403 if the actor does not have admin:manage_users."""
    if not has_permission(actor.id, "admin:manage_users", session):
        raise HTTPException(403, "Permission required: admin:manage_users")


# ---------------------------------------------------------------------------
# User management endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_model=UsersListResponse)
def list_users(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all users in the tenant (requires admin:manage_users)."""
    _check_manage_users(user, session)

    users = session.exec(
        select(AppUser).where(AppUser.tenant_id == ctx.tenant_id)
    ).all()

    return UsersListResponse(users=[_build_user_detail(u, session) for u in users])


@router.post("/", response_model=UserDetailResponse)
def create_user(
    user_data: UserCreateByAdmin,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new user (requires admin:manage_users)."""
    _check_manage_users(user, session)

    if session.exec(select(AppUser).where(
        AppUser.email == user_data.email,
        AppUser.tenant_id == ctx.tenant_id,
    )).first():
        raise HTTPException(400, "Email already registered for this tenant")

    if user_data.username and session.exec(select(AppUser).where(
        AppUser.username == user_data.username,
        AppUser.tenant_id == ctx.tenant_id,
    )).first():
        raise HTTPException(400, "Username already registered for this tenant")

    new_user = AppUser(
        tenant_id=ctx.tenant_id,
        email=user_data.email,
        username=user_data.username,
        full_name=user_data.full_name or "",
        first_name=user_data.first_name,
        last_name=user_data.last_name,
        hashed_password=hash_password(user_data.password),
    )
    session.add(new_user)
    session.flush()

    # Assign role via RBAC catalog
    try:
        assign_role_by_code(new_user.id, user_data.role, session)
    except ValueError:
        raise HTTPException(400, f"Unknown role: {user_data.role}")

    # Branch assignment (full-access roles don't need explicit records)
    if user_data.branch_ids and not user_has_full_branch_access(new_user.id, session):
        for branch_id in user_data.branch_ids:
            branch = session.get(Branch, branch_id)
            if not branch:
                raise HTTPException(400, f"Branch {branch_id} not found")
            if str(branch.tenant_id) != ctx.tenant_id:
                raise HTTPException(400, f"Branch {branch_id} does not belong to this tenant")
            session.add(UserBranch(user_id=new_user.id, branch_id=branch.id))

    session.commit()
    session.refresh(new_user)

    logger.info(
        f"User {new_user.email} created by admin",
        extra={
            "event": "user.created",
            "user_id": str(new_user.id),
            "created_by": str(user.id),
            "branch_count": len(user_data.branch_ids) if user_data.branch_ids else 0,
        },
    )
    return _build_user_detail(new_user, session)


@router.put("/{user_id}", response_model=UserDetailResponse)
def update_user(
    user_id: str,
    user_data: UserUpdateByAdmin,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update a user (requires admin:manage_users)."""
    _check_manage_users(user, session)

    target_user = session.get(AppUser, user_id)
    if not target_user:
        raise HTTPException(404, "User not found")
    if str(target_user.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "User does not belong to your tenant")

    # Safety: prevent stranding the tenant without an admin
    if user_data.role is not None or user_data.is_active is not None:
        target_roles = set(get_user_roles(target_user.id, session))
        is_demoting = user_data.role is not None and user_data.role not in target_roles
        is_deactivating = user_data.is_active is False

        if ("admin" in target_roles or "superuser" in target_roles) and (is_demoting or is_deactivating):
            admin_count = count_active_users_with_role(session, ctx.tenant_id, "admin")
            superuser_count = count_active_users_with_role(session, ctx.tenant_id, "superuser")
            if admin_count <= 1 and superuser_count <= 1:
                raise HTTPException(400, "Cannot remove or deactivate the last administrator")

    if user_data.email is not None:
        target_user.email = user_data.email
    if user_data.username is not None:
        target_user.username = user_data.username
    if user_data.full_name is not None:
        target_user.full_name = user_data.full_name
    if user_data.is_active is not None:
        target_user.is_active = user_data.is_active
    if user_data.password is not None:
        target_user.hashed_password = hash_password(user_data.password)

    # Update role via RBAC (replace single role assignment)
    if user_data.role is not None:
        try:
            replace_user_roles(target_user.id, [user_data.role], session)
        except ValueError:
            raise HTTPException(400, f"Unknown role: {user_data.role}")

    # Update branches if provided
    if user_has_full_branch_access(target_user.id, session):
        # Clear explicit records for full-access roles
        for assoc in session.exec(select(UserBranch).where(UserBranch.user_id == target_user.id)).all():
            session.delete(assoc)
    elif user_data.branch_ids is not None:
        for assoc in session.exec(select(UserBranch).where(UserBranch.user_id == target_user.id)).all():
            session.delete(assoc)
        for branch_id in user_data.branch_ids:
            branch = session.get(Branch, branch_id)
            if not branch:
                raise HTTPException(400, f"Branch {branch_id} not found")
            if str(branch.tenant_id) != ctx.tenant_id:
                raise HTTPException(400, f"Branch {branch_id} does not belong to this tenant")
            session.add(UserBranch(user_id=target_user.id, branch_id=branch.id))

    session.add(target_user)
    session.commit()
    session.refresh(target_user)

    logger.info(
        f"User {target_user.email} updated by admin",
        extra={"event": "user.updated", "user_id": str(target_user.id), "updated_by": str(user.id)},
    )
    return _build_user_detail(target_user, session)


@router.delete("/{user_id}")
def deactivate_user(
    user_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Deactivate a user (requires admin:manage_users)."""
    _check_manage_users(user, session)

    target_user = session.get(AppUser, user_id)
    if not target_user:
        raise HTTPException(404, "User not found")
    if str(target_user.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "User does not belong to your tenant")
    if str(target_user.id) == str(user.id):
        raise HTTPException(400, "Cannot deactivate your own account")

    target_roles = set(get_user_roles(target_user.id, session))
    if ("admin" in target_roles or "superuser" in target_roles) and target_user.is_active:
        admin_count = count_active_users_with_role(session, ctx.tenant_id, "admin")
        if admin_count <= 1:
            raise HTTPException(400, "Cannot deactivate the last administrator")

    target_user.is_active = False
    session.add(target_user)
    session.commit()

    logger.info(
        f"User {target_user.email} deactivated",
        extra={"event": "user.deactivated", "user_id": str(target_user.id), "deactivated_by": str(user.id)},
    )
    return {"message": "User deactivated successfully"}


@router.post("/{user_id}/toggle-active")
def toggle_user_active(
    user_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Toggle user active status (requires admin:manage_users)."""
    _check_manage_users(user, session)

    target_user = session.get(AppUser, user_id)
    if not target_user:
        raise HTTPException(404, "User not found")
    if str(target_user.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "User does not belong to your tenant")
    if str(target_user.id) == str(user.id):
        raise HTTPException(400, "Cannot toggle your own account status")

    target_roles = set(get_user_roles(target_user.id, session))
    if ("admin" in target_roles or "superuser" in target_roles) and target_user.is_active:
        admin_count = count_active_users_with_role(session, ctx.tenant_id, "admin")
        if admin_count <= 1:
            raise HTTPException(400, "Cannot deactivate the last administrator")

    target_user.is_active = not target_user.is_active
    session.add(target_user)
    session.commit()

    logger.info(
        f"User {target_user.email} status toggled to {target_user.is_active}",
        extra={"event": "user.toggled", "user_id": str(target_user.id), "toggled_by": str(user.id), "new_status": target_user.is_active},
    )
    return {"message": f"User {'activated' if target_user.is_active else 'deactivated'}", "is_active": target_user.is_active}


# ---------------------------------------------------------------------------
# Invitation endpoints
# ---------------------------------------------------------------------------

@router.post("/invitations", response_model=InvitationResponse)
def create_invitation(
    invitation_data: InvitationCreate,
    request: Request,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create and send user invitation (requires admin:manage_invitations)."""
    if not has_permission(user.id, "admin:manage_invitations", session):
        raise HTTPException(403, "Permission required: admin:manage_invitations")

    # Validate role code exists in catalog
    if not session.exec(select(Role).where(Role.code == invitation_data.role)).first():
        raise HTTPException(400, f"Unknown role: {invitation_data.role}")

    if session.exec(select(AppUser).where(
        AppUser.email == invitation_data.email,
        AppUser.tenant_id == ctx.tenant_id,
    )).first():
        raise HTTPException(400, "User with this email already exists")

    if session.exec(select(UserInvitation).where(
        UserInvitation.email == invitation_data.email,
        UserInvitation.tenant_id == ctx.tenant_id,
        UserInvitation.is_used == False,
        UserInvitation.expires_at > datetime.utcnow(),
    )).first():
        raise HTTPException(400, "There's already a pending invitation for this email")

    token = secrets.token_urlsafe(32)
    invitation = UserInvitation(
        tenant_id=ctx.tenant_id,
        email=invitation_data.email,
        full_name=invitation_data.full_name,
        role_code=invitation_data.role,
        token=token,
        expires_at=datetime.utcnow() + timedelta(days=7),
        invited_by=user.id,
    )
    session.add(invitation)
    session.commit()
    session.refresh(invitation)

    tenant = session.get(Tenant, ctx.tenant_id)
    tenant_name = tenant.name if tenant else "Laboratorio"
    base_url = getattr(settings, "frontend_url", "http://localhost:5173")
    invitation_url = f"{base_url}/accept-invitation?token={token}"

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
        extra={"event": "invitation.created", "invitation_id": str(invitation.id), "invited_by": str(user.id)},
    )
    return InvitationResponse(
        id=str(invitation.id),
        email=invitation.email,
        full_name=invitation.full_name,
        role=invitation.role_code,
        token=invitation.token,
        expires_at=invitation.expires_at,
    )


@router.get("/invitations/{token}")
def get_invitation(token: str, session: Session = Depends(get_session)):
    """Get invitation details (public endpoint for verification)."""
    invitation = session.exec(
        select(UserInvitation).where(UserInvitation.token == token, UserInvitation.is_used == False)
    ).first()

    if not invitation:
        raise HTTPException(404, "Invitation not found or already used")
    if invitation.expires_at < datetime.utcnow():
        raise HTTPException(400, "Invitation has expired")

    tenant = session.get(Tenant, invitation.tenant_id)
    return {
        "email": invitation.email,
        "full_name": invitation.full_name,
        "role": invitation.role_code,
        "tenant_name": tenant.name if tenant else "Unknown",
        "expires_at": invitation.expires_at,
    }


@router.post("/invitations/{token}/accept")
def accept_invitation(
    token: str,
    accept_data: AcceptInvitationRequest,
    session: Session = Depends(get_session),
):
    """Accept invitation and create user account."""
    invitation = session.exec(
        select(UserInvitation).where(UserInvitation.token == token, UserInvitation.is_used == False)
    ).first()

    if not invitation:
        raise HTTPException(404, "Invitation not found or already used")
    if invitation.expires_at < datetime.utcnow():
        raise HTTPException(400, "Invitation has expired")

    if accept_data.username and session.exec(select(AppUser).where(
        AppUser.username == accept_data.username,
        AppUser.tenant_id == invitation.tenant_id,
    )).first():
        raise HTTPException(400, "Username already taken")

    first_name, last_name = split_full_name(invitation.full_name)
    new_user = AppUser(
        tenant_id=invitation.tenant_id,
        email=invitation.email,
        username=accept_data.username,
        full_name=invitation.full_name,
        first_name=first_name,
        last_name=last_name,
        hashed_password=hash_password(accept_data.password),
        is_active=True,
    )
    session.add(new_user)
    session.flush()

    # Assign role from invitation
    try:
        assign_role_by_code(new_user.id, invitation.role_code, session)
    except ValueError:
        raise HTTPException(400, f"Unknown role in invitation: {invitation.role_code}")

    invitation.is_used = True
    invitation.accepted_at = datetime.utcnow()
    session.add(invitation)
    session.commit()
    session.refresh(new_user)

    logger.info(
        f"Invitation accepted, user {new_user.email} created",
        extra={"event": "invitation.accepted", "invitation_id": str(invitation.id), "user_id": str(new_user.id)},
    )
    return {
        "message": "Account created successfully",
        "user_id": str(new_user.id),
        "email": new_user.email,
        "branch_ids": [],
    }


# ---------------------------------------------------------------------------
# Profile Avatar
# ---------------------------------------------------------------------------

@router.post("/{user_id}/avatar")
def upload_user_avatar(
    user_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Upload user avatar (self or admin:manage_users)."""
    target_user = session.get(AppUser, user_id)
    if not target_user:
        raise HTTPException(404, "User not found")

    is_self = str(target_user.id) == str(user.id)
    if not is_self and not has_permission(user.id, "admin:manage_users", session):
        raise HTTPException(403, "Can only upload your own avatar")

    if str(target_user.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "User does not belong to your tenant")

    content_type = (file.content_type or "").lower()
    allowed_types = ["image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif"]
    if not any(img_type in content_type for img_type in allowed_types):
        raise HTTPException(400, "Only image files (JPEG, PNG, WEBP, HEIC) are allowed")

    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "File size must be less than 10MB")

    from app.services.image_processing import process_avatar_bytes
    try:
        processed = process_avatar_bytes(file_bytes, max_size=(512, 512), quality=90)
    except Exception as e:
        logger.error(f"Image processing failed: {str(e)}", extra={"event": "user.avatar_processing_failed", "user_id": user_id})
        raise HTTPException(400, "Failed to process image. Please try a different image.")

    from app.services.s3 import S3Service
    import time
    s3 = S3Service()
    key = f"avatars/{user_id}/avatar.jpg"
    s3.upload_bytes(processed.jpeg_bytes, key=key, content_type=processed.content_type)
    avatar_url = f"{s3.object_public_url(key)}?v={int(time.time())}"
    target_user.avatar_url = avatar_url
    session.add(target_user)
    session.commit()

    logger.info(
        f"Avatar uploaded for user {target_user.email}",
        extra={"event": "user.avatar_uploaded", "user_id": str(target_user.id)},
    )
    return {"message": "Avatar uploaded successfully", "avatar_url": target_user.avatar_url}
