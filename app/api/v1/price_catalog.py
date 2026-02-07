from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.models.price_catalog import PriceCatalog
from app.models.study_type import StudyType
from app.models.user import AppUser
from app.schemas.price_catalog import (
    PriceCatalogCreate,
    PriceCatalogUpdate,
    PriceCatalogResponse,
    PriceCatalogListResponse,
    StudyTypeRef,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/price-catalog")


@router.get("/", response_model=PriceCatalogListResponse)
def list_price_catalog(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    active_only: bool = True,
):
    """List all price catalog entries for the tenant"""
    query = select(PriceCatalog).where(PriceCatalog.tenant_id == ctx.tenant_id)
    
    if active_only:
        query = query.where(PriceCatalog.is_active == True)
    
    prices = session.exec(query).all()
    
    results = []
    for p in prices:
        # Get study type reference
        study_type_ref = None
        study_type = session.get(StudyType, p.study_type_id)
        if study_type:
            study_type_ref = StudyTypeRef(
                id=str(study_type.id),
                code=study_type.code,
                name=study_type.name
            )
        
        results.append(
            PriceCatalogResponse(
                id=str(p.id),
                tenant_id=str(p.tenant_id),
                study_type_id=str(p.study_type_id),
                unit_price=float(p.unit_price),
                currency=p.currency,
                is_active=p.is_active,
                effective_from=p.effective_from,
                effective_to=p.effective_to,
                created_at=p.created_at,
                study_type=study_type_ref,
            )
        )
    
    return PriceCatalogListResponse(prices=results)


@router.get("/{price_id}", response_model=PriceCatalogResponse)
def get_price_catalog(
    price_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get a specific price catalog entry by ID"""
    price = session.get(PriceCatalog, price_id)
    if not price:
        raise HTTPException(404, "Price catalog entry not found")
    
    if str(price.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Price catalog entry not found")
    
    # Get study type reference
    study_type_ref = None
    study_type = session.get(StudyType, price.study_type_id)
    if study_type:
        study_type_ref = StudyTypeRef(
            id=str(study_type.id),
            code=study_type.code,
            name=study_type.name
        )
    
    return PriceCatalogResponse(
        id=str(price.id),
        tenant_id=str(price.tenant_id),
        study_type_id=str(price.study_type_id),
        unit_price=float(price.unit_price),
        currency=price.currency,
        is_active=price.is_active,
        effective_from=price.effective_from,
        effective_to=price.effective_to,
        created_at=price.created_at,
        study_type=study_type_ref,
    )


@router.post("/", response_model=PriceCatalogResponse)
def create_price_catalog(
    price_data: PriceCatalogCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new price catalog entry"""
    # Validate study_type_id exists and belongs to tenant
    study_type = session.get(StudyType, price_data.study_type_id)
    if not study_type:
        raise HTTPException(404, "Study type not found")
    
    if str(study_type.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Study type does not belong to your tenant")
    
    # Check if a price already exists for this study type (optionally warn or allow multiple)
    # For now, we allow multiple price entries per study type (for historical pricing, etc.)
    
    price = PriceCatalog(
        tenant_id=ctx.tenant_id,
        study_type_id=price_data.study_type_id,
        unit_price=price_data.unit_price,
        currency=price_data.currency or "MXN",
        is_active=price_data.is_active if price_data.is_active is not None else True,
        effective_from=price_data.effective_from,
        effective_to=price_data.effective_to,
    )
    
    session.add(price)
    session.commit()
    session.refresh(price)
    
    # Get study type reference
    study_type_ref = StudyTypeRef(
        id=str(study_type.id),
        code=study_type.code,
        name=study_type.name
    )
    
    logger.info(
        f"Price catalog entry created for study type '{study_type.code}'",
        extra={
            "event": "price_catalog.created",
            "price_id": str(price.id),
            "study_type_id": str(study_type.id),
            "user_id": str(user.id),
        },
    )
    
    return PriceCatalogResponse(
        id=str(price.id),
        tenant_id=str(price.tenant_id),
        study_type_id=str(price.study_type_id),
        unit_price=float(price.unit_price),
        currency=price.currency,
        is_active=price.is_active,
        effective_from=price.effective_from,
        effective_to=price.effective_to,
        created_at=price.created_at,
        study_type=study_type_ref,
    )


@router.put("/{price_id}", response_model=PriceCatalogResponse)
def update_price_catalog(
    price_id: str,
    price_data: PriceCatalogUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update an existing price catalog entry"""
    price = session.get(PriceCatalog, price_id)
    if not price:
        raise HTTPException(404, "Price catalog entry not found")
    
    if str(price.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Price catalog entry does not belong to your tenant")
    
    # Validate study_type_id if provided
    if price_data.study_type_id is not None:
        study_type = session.get(StudyType, price_data.study_type_id)
        if not study_type:
            raise HTTPException(404, "Study type not found")
        if str(study_type.tenant_id) != ctx.tenant_id:
            raise HTTPException(403, "Study type does not belong to your tenant")
        price.study_type_id = price_data.study_type_id
    
    # Update fields if provided
    if price_data.unit_price is not None:
        price.unit_price = price_data.unit_price
    if price_data.currency is not None:
        price.currency = price_data.currency
    if price_data.is_active is not None:
        price.is_active = price_data.is_active
    if price_data.effective_from is not None:
        price.effective_from = price_data.effective_from
    if price_data.effective_to is not None:
        price.effective_to = price_data.effective_to
    
    session.add(price)
    session.commit()
    session.refresh(price)
    
    # Get study type reference
    study_type_ref = None
    study_type = session.get(StudyType, price.study_type_id)
    if study_type:
        study_type_ref = StudyTypeRef(
            id=str(study_type.id),
            code=study_type.code,
            name=study_type.name
        )
    
    logger.info(
        f"Price catalog entry updated",
        extra={
            "event": "price_catalog.updated",
            "price_id": str(price.id),
            "user_id": str(user.id),
        },
    )
    
    return PriceCatalogResponse(
        id=str(price.id),
        tenant_id=str(price.tenant_id),
        study_type_id=str(price.study_type_id),
        unit_price=float(price.unit_price),
        currency=price.currency,
        is_active=price.is_active,
        effective_from=price.effective_from,
        effective_to=price.effective_to,
        created_at=price.created_at,
        study_type=study_type_ref,
    )


@router.delete("/{price_id}")
def delete_price_catalog(
    price_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    hard_delete: bool = False,
):
    """Delete a price catalog entry (soft delete by default)"""
    price = session.get(PriceCatalog, price_id)
    if not price:
        raise HTTPException(404, "Price catalog entry not found")
    
    if str(price.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Price catalog entry does not belong to your tenant")
    
    if hard_delete:
        # Permanently delete
        session.delete(price)
        session.commit()
        
        logger.info(
            f"Price catalog entry permanently deleted",
            extra={
                "event": "price_catalog.hard_deleted",
                "price_id": price_id,
                "user_id": str(user.id),
            },
        )
        
        return {"message": "Price catalog entry permanently deleted", "id": price_id}
    else:
        # Soft delete - mark as inactive
        price.is_active = False
        session.add(price)
        session.commit()
        
        logger.info(
            f"Price catalog entry deactivated",
            extra={
                "event": "price_catalog.soft_deleted",
                "price_id": str(price.id),
                "user_id": str(user.id),
            },
        )
        
        return {"message": "Price catalog entry deactivated", "id": str(price.id)}
