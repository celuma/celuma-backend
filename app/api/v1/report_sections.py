from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.core.rbac import has_permission
from app.models.report_section import ReportSection
from app.models.user import AppUser
from app.schemas.report_section import (
    ReportSectionCreate,
    ReportSectionUpdate,
    ReportSectionResponse,
    ReportSectionDetailResponse,
    ReportSectionsListResponse,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/report-sections")


@router.get("/", response_model=ReportSectionsListResponse)
def list_report_sections(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all report sections (requires reports:read)."""
    if not has_permission(user.id, "reports:read", session):
        raise HTTPException(403, "Permission required: reports:read")
    query = select(ReportSection).where(ReportSection.tenant_id == ctx.tenant_id)
    
    report_sections = session.exec(query).all()
    
    results = []
    for section in report_sections:
        results.append(
            ReportSectionResponse(
                id=str(section.id),
                tenant_id=str(section.tenant_id),
                section=section.section,
                description=section.description,
                predefined_text=section.predefined_text,
                created_at=section.created_at,
                created_by=str(section.created_by) if section.created_by else None,
            )
        )
    
    return ReportSectionsListResponse(report_sections=results)


@router.get("/{report_section_id}", response_model=ReportSectionDetailResponse)
def get_report_section(
    report_section_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get a specific report section by ID (requires reports:read)."""
    if not has_permission(user.id, "reports:read", session):
        raise HTTPException(403, "Permission required: reports:read")
    report_section = session.get(ReportSection, report_section_id)
    if not report_section:
        raise HTTPException(404, "Report section not found")
    
    if str(report_section.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Report section not found")
    
    return ReportSectionDetailResponse(
        id=str(report_section.id),
        tenant_id=str(report_section.tenant_id),
        section=report_section.section,
        description=report_section.description,
        predefined_text=report_section.predefined_text,
        created_at=report_section.created_at,
        created_by=str(report_section.created_by) if report_section.created_by else None,
    )


@router.post("/", response_model=ReportSectionResponse)
def create_report_section(
    section_data: ReportSectionCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new report section (requires reports:manage_templates)."""
    if not has_permission(user.id, "reports:manage_templates", session):
        raise HTTPException(403, "Permission required: reports:manage_templates")
    report_section = ReportSection(
        tenant_id=ctx.tenant_id,
        created_by=user.id,
        section=section_data.section,
        description=section_data.description,
        predefined_text=section_data.predefined_text,
    )
    
    session.add(report_section)
    session.commit()
    session.refresh(report_section)
    
    logger.info(
        f"Report section '{report_section.section}' created",
        extra={
            "event": "report_section.created",
            "report_section_id": str(report_section.id),
            "user_id": str(user.id),
        },
    )
    
    return ReportSectionResponse(
        id=str(report_section.id),
        tenant_id=str(report_section.tenant_id),
        section=report_section.section,
        description=report_section.description,
        predefined_text=report_section.predefined_text,
        created_at=report_section.created_at,
        created_by=str(report_section.created_by) if report_section.created_by else None,
    )


@router.put("/{report_section_id}", response_model=ReportSectionResponse)
def update_report_section(
    report_section_id: str,
    section_data: ReportSectionUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update an existing report section (requires reports:manage_templates)."""
    if not has_permission(user.id, "reports:manage_templates", session):
        raise HTTPException(403, "Permission required: reports:manage_templates")
    report_section = session.get(ReportSection, report_section_id)
    if not report_section:
        raise HTTPException(404, "Report section not found")
    
    if str(report_section.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report section does not belong to your tenant")
    
    # Update fields if provided
    if section_data.section is not None:
        report_section.section = section_data.section
    if section_data.description is not None:
        report_section.description = section_data.description
    if section_data.predefined_text is not None:
        report_section.predefined_text = section_data.predefined_text
    
    session.add(report_section)
    session.commit()
    session.refresh(report_section)
    
    logger.info(
        f"Report section '{report_section.section}' updated",
        extra={
            "event": "report_section.updated",
            "report_section_id": str(report_section.id),
            "user_id": str(user.id),
        },
    )
    
    return ReportSectionResponse(
        id=str(report_section.id),
        tenant_id=str(report_section.tenant_id),
        section=report_section.section,
        description=report_section.description,
        predefined_text=report_section.predefined_text,
        created_at=report_section.created_at,
        created_by=str(report_section.created_by) if report_section.created_by else None,
    )


@router.delete("/{report_section_id}")
def delete_report_section(
    report_section_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Delete a report section (requires reports:manage_templates)."""
    if not has_permission(user.id, "reports:manage_templates", session):
        raise HTTPException(403, "Permission required: reports:manage_templates")
    report_section = session.get(ReportSection, report_section_id)
    if not report_section:
        raise HTTPException(404, "Report section not found")
    
    if str(report_section.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report section does not belong to your tenant")
    
    section_name = report_section.section
    
    # Permanently delete the report section
    session.delete(report_section)
    session.commit()
    
    logger.info(
        f"Report section '{section_name}' deleted",
        extra={
            "event": "report_section.deleted",
            "report_section_id": report_section_id,
            "user_id": str(user.id),
        },
    )
    
    return {"message": "Report section deleted", "id": report_section_id}
