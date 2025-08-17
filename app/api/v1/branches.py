from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.tenant import Branch
from app.models.user import AppUser
from app.api.v1.auth import current_user

router = APIRouter(prefix="/branches")

@router.get("/")
def list_branches(session: Session = Depends(get_session)):
    """List all branches"""
    branches = session.exec(select(Branch)).all()
    return [{"id": str(b.id), "name": b.name, "code": b.code, "city": b.city, "tenant_id": str(b.tenant_id)} for b in branches]

@router.post("/")
def create_branch(
    tenant_id: str,
    code: str,
    name: str,
    timezone: str = "America/Mexico_City",
    address_line1: str = None,
    city: str = None,
    state: str = None,
    country: str = "MX",
    session: Session = Depends(get_session)
):
    """Create a new branch"""
    branch = Branch(
        tenant_id=tenant_id,
        code=code,
        name=name,
        timezone=timezone,
        address_line1=address_line1,
        city=city,
        state=state,
        country=country
    )
    session.add(branch)
    session.commit()
    session.refresh(branch)
    return {"id": str(branch.id), "name": branch.name, "code": branch.code, "tenant_id": str(branch.tenant_id)}

@router.get("/{branch_id}")
def get_branch(branch_id: str, session: Session = Depends(get_session)):
    """Get branch details"""
    branch = session.get(Branch, branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    return {
        "id": str(branch.id),
        "name": branch.name,
        "code": branch.code,
        "city": branch.city,
        "state": branch.state,
        "country": branch.country,
        "tenant_id": str(branch.tenant_id)
    }

@router.get("/{branch_id}/users")
def list_branch_users(branch_id: str, session: Session = Depends(get_session)):
    """List all users for a branch"""
    branch = session.get(Branch, branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    users = [{"id": str(ub.user.id), "email": ub.user.email, "full_name": ub.user.full_name, "role": ub.user.role} for ub in branch.users]
    return users
