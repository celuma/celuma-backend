from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.models.study_type import StudyType
from app.models.report import ReportTemplate
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.schemas.study_type import (
    StudyTypeCreate,
    StudyTypeUpdate,
    StudyTypeResponse,
    StudyTypeDetailResponse,
    StudyTypesListResponse,
    TemplateRef,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/study-types")


@router.get("/", response_model=StudyTypesListResponse)
def list_study_types(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    active_only: bool = True,
):
    """List all study types for the tenant"""
    query = select(StudyType).where(StudyType.tenant_id == ctx.tenant_id)
    
    if active_only:
        query = query.where(StudyType.is_active == True)
    
    study_types = session.exec(query).all()
    
    results = []
    for t in study_types:
        # Get template reference if set
        template_ref = None
        if t.default_report_template_id:
            template = session.get(ReportTemplate, t.default_report_template_id)
            if template:
                template_ref = TemplateRef(id=str(template.id), name=template.name)
        
        results.append(
            StudyTypeResponse(
                id=str(t.id),
                tenant_id=str(t.tenant_id),
                code=t.code,
                name=t.name,
                description=t.description,
                is_active=t.is_active,
                created_at=t.created_at,
                default_report_template_id=str(t.default_report_template_id) if t.default_report_template_id else None,
                default_template=template_ref,
            )
        )
    
    return StudyTypesListResponse(study_types=results)


@router.get("/{study_type_id}", response_model=StudyTypeDetailResponse)
def get_study_type(
    study_type_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get a specific study type by ID"""
    study_type = session.get(StudyType, study_type_id)
    if not study_type:
        raise HTTPException(404, "Study type not found")
    
    if str(study_type.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Study type not found")
    
    # Get template reference if set
    template_ref = None
    if study_type.default_report_template_id:
        template = session.get(ReportTemplate, study_type.default_report_template_id)
        if template:
            template_ref = TemplateRef(id=str(template.id), name=template.name)
    
    return StudyTypeDetailResponse(
        id=str(study_type.id),
        tenant_id=str(study_type.tenant_id),
        code=study_type.code,
        name=study_type.name,
        description=study_type.description,
        is_active=study_type.is_active,
        created_at=study_type.created_at,
        default_report_template_id=str(study_type.default_report_template_id) if study_type.default_report_template_id else None,
        default_template=template_ref,
    )


@router.post("/", response_model=StudyTypeResponse)
def create_study_type(
    study_type_data: StudyTypeCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new study type"""
    # Check if code already exists for this tenant
    existing = session.exec(
        select(StudyType).where(
            StudyType.tenant_id == ctx.tenant_id,
            StudyType.code == study_type_data.code.upper()
        )
    ).first()
    
    if existing:
        raise HTTPException(400, f"Study type with code '{study_type_data.code}' already exists")
    
    # Validate default_report_template_id if provided
    if study_type_data.default_report_template_id:
        template = session.get(ReportTemplate, study_type_data.default_report_template_id)
        if not template:
            raise HTTPException(404, "Report template not found")
        if str(template.tenant_id) != ctx.tenant_id:
            raise HTTPException(403, "Report template does not belong to your tenant")
    
    study_type = StudyType(
        tenant_id=ctx.tenant_id,
        code=study_type_data.code.upper(),
        name=study_type_data.name,
        description=study_type_data.description,
        is_active=study_type_data.is_active if study_type_data.is_active is not None else True,
        default_report_template_id=study_type_data.default_report_template_id,
    )
    
    session.add(study_type)
    session.commit()
    session.refresh(study_type)
    
    # Get template reference if set
    template_ref = None
    if study_type.default_report_template_id:
        template = session.get(ReportTemplate, study_type.default_report_template_id)
        if template:
            template_ref = TemplateRef(id=str(template.id), name=template.name)
    
    logger.info(
        f"Study type '{study_type.code}' created",
        extra={
            "event": "study_type.created",
            "study_type_id": str(study_type.id),
            "user_id": str(user.id),
        },
    )
    
    return StudyTypeResponse(
        id=str(study_type.id),
        tenant_id=str(study_type.tenant_id),
        code=study_type.code,
        name=study_type.name,
        description=study_type.description,
        is_active=study_type.is_active,
        created_at=study_type.created_at,
        default_report_template_id=str(study_type.default_report_template_id) if study_type.default_report_template_id else None,
        default_template=template_ref,
    )


@router.put("/{study_type_id}", response_model=StudyTypeResponse)
def update_study_type(
    study_type_id: str,
    study_type_data: StudyTypeUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update an existing study type"""
    study_type = session.get(StudyType, study_type_id)
    if not study_type:
        raise HTTPException(404, "Study type not found")
    
    if str(study_type.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Study type does not belong to your tenant")
    
    # If updating code, check for duplicates
    if study_type_data.code is not None:
        existing = session.exec(
            select(StudyType).where(
                StudyType.tenant_id == ctx.tenant_id,
                StudyType.code == study_type_data.code.upper(),
                StudyType.id != study_type.id
            )
        ).first()
        
        if existing:
            raise HTTPException(400, f"Study type with code '{study_type_data.code}' already exists")
        
        study_type.code = study_type_data.code.upper()
    
    # Validate default_report_template_id if provided
    if study_type_data.default_report_template_id is not None:
        if study_type_data.default_report_template_id:
            template = session.get(ReportTemplate, study_type_data.default_report_template_id)
            if not template:
                raise HTTPException(404, "Report template not found")
            if str(template.tenant_id) != ctx.tenant_id:
                raise HTTPException(403, "Report template does not belong to your tenant")
        study_type.default_report_template_id = study_type_data.default_report_template_id
    
    # Update fields if provided
    if study_type_data.name is not None:
        study_type.name = study_type_data.name
    if study_type_data.description is not None:
        study_type.description = study_type_data.description
    if study_type_data.is_active is not None:
        study_type.is_active = study_type_data.is_active
    
    session.add(study_type)
    session.commit()
    session.refresh(study_type)
    
    # Get template reference if set
    template_ref = None
    if study_type.default_report_template_id:
        template = session.get(ReportTemplate, study_type.default_report_template_id)
        if template:
            template_ref = TemplateRef(id=str(template.id), name=template.name)
    
    logger.info(
        f"Study type '{study_type.code}' updated",
        extra={
            "event": "study_type.updated",
            "study_type_id": str(study_type.id),
            "user_id": str(user.id),
        },
    )
    
    return StudyTypeResponse(
        id=str(study_type.id),
        tenant_id=str(study_type.tenant_id),
        code=study_type.code,
        name=study_type.name,
        description=study_type.description,
        is_active=study_type.is_active,
        created_at=study_type.created_at,
        default_report_template_id=str(study_type.default_report_template_id) if study_type.default_report_template_id else None,
        default_template=template_ref,
    )


@router.delete("/{study_type_id}")
def delete_study_type(
    study_type_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    hard_delete: bool = False,
):
    """Delete a study type (soft delete by default, hard delete optional)"""
    study_type = session.get(StudyType, study_type_id)
    if not study_type:
        raise HTTPException(404, "Study type not found")
    
    if str(study_type.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Study type does not belong to your tenant")
    
    if hard_delete:
        # Permanently delete the study type
        session.delete(study_type)
        session.commit()
        
        logger.info(
            f"Study type '{study_type.code}' permanently deleted",
            extra={
                "event": "study_type.hard_deleted",
                "study_type_id": study_type_id,
                "user_id": str(user.id),
            },
        )
        
        return {"message": "Study type permanently deleted", "id": study_type_id}
    else:
        # Soft delete - just mark as inactive
        study_type.is_active = False
        session.add(study_type)
        session.commit()
        
        logger.info(
            f"Study type '{study_type.code}' soft deleted (deactivated)",
            extra={
                "event": "study_type.soft_deleted",
                "study_type_id": str(study_type.id),
                "user_id": str(user.id),
            },
        )
        
        return {"message": "Study type deactivated", "id": str(study_type.id)}
