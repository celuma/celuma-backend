from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from typing import Dict, Optional
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.models.laboratory import LabOrder, Sample, SampleImage
from app.models.storage import StorageObject, SampleImageRendition
from app.models.tenant import Tenant, Branch
from app.models.patient import Patient
from app.models.report import Report, ReportVersion
from app.models.user import AppUser
from app.models.events import CaseEvent
from app.models.enums import EventType, SampleState
from app.schemas.laboratory import (
    LabOrderCreate,
    LabOrderResponse,
    LabOrderDetailResponse,
    OrderNotesUpdate,
    CommentCreate,
    CommentResponse,
    CommentsListResponse,
    PageInfo,
    MentionedUser,
    UserMentionItem,
    UserMentionListResponse,
    SampleCreate,
    SampleResponse,
    SampleStateUpdate,
    SampleNotesUpdate,
    SampleImagesListResponse,
    SampleImageInfo,
    SampleImageUploadResponse,
    LabOrderUnifiedCreate,
    LabOrderUnifiedResponse,
    LabOrderFullDetailResponse,
    PatientCasesListResponse,
    PatientCaseSummary,
    PatientOrdersListResponse,
    PatientOrderSummary,
    OrdersListResponse,
    OrderListItem,
    BranchRef,
    PatientRef,
    SamplesListResponse,
    SampleListItem,
    SampleDetailResponse,
    OrderSlim,
)
from app.schemas.report import ReportMetaResponse
from app.schemas.patient import PatientFullResponse
from app.schemas.events import EventCreate, EventResponse, EventsListResponse
from app.services.s3 import S3Service
from app.services.image_processing import process_image_bytes
from uuid import uuid4
import os
from sqlmodel import select
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/laboratory")


def update_order_status(order_id: str, session: Session) -> None:
    """
    Automatically update order status based on current state of samples and reports.
    
    Rules:
    - RECEIVED: order created + at least 1 sample
    - PROCESSING: at least 1 sample is PROCESSING, READY, CANCELLED, or DAMAGED
    - DIAGNOSIS: meets PROCESSING + at least 1 report exists
    - REVIEW: report is in REVIEW or RETRACTED status
    - CLOSED: report is PUBLISHED
    - RELEASED: payment cleared (billed_lock=False) + meets CLOSED
    - CANCELLED: if report is CANCELLED
    """
    from app.models.enums import OrderStatus, SampleState, ReportStatus
    
    # Get order with samples
    order = session.get(LabOrder, order_id)
    if not order:
        return
    
    # Get all samples for this order
    samples = session.exec(
        select(Sample).where(Sample.order_id == order_id)
    ).all()
    
    # Check if there are any reports for this order
    reports = session.exec(
        select(Report)
        .where(Report.order_id == order_id)
        .order_by(Report.created_at.desc())
    ).all()
    
    latest_report = reports[0] if reports else None
    
    # Rule: At least 1 sample required
    if not samples:
        # Keep as RECEIVED if no samples yet
        if order.status != OrderStatus.RECEIVED:
            order.status = OrderStatus.RECEIVED
            session.add(order)
            session.flush()
        return
    
    # Check sample states
    has_processing_or_beyond = any(
        s.state in [SampleState.PROCESSING, SampleState.READY, SampleState.DAMAGED, SampleState.CANCELLED]
        for s in samples
    )
    
    # Determine new status
    new_status = OrderStatus.RECEIVED
    
    if has_processing_or_beyond:
        new_status = OrderStatus.PROCESSING
        
        if latest_report:
            # Has report - move to DIAGNOSIS or beyond
            new_status = OrderStatus.DIAGNOSIS
            
            if latest_report.status in [ReportStatus.IN_REVIEW, ReportStatus.RETRACTED]:
                new_status = OrderStatus.REVIEW
            elif latest_report.status == ReportStatus.PUBLISHED:
                new_status = OrderStatus.CLOSED
                
                # Check if can be released (payment cleared)
                if not order.billed_lock:
                    new_status = OrderStatus.RELEASED
    
    # Update if changed
    if order.status != new_status:
        order.status = new_status
        session.add(order)
        session.flush()

@router.get("/orders/", response_model=OrdersListResponse)
def list_orders(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all laboratory orders with enriched patient and branch info, plus summary fields."""
    orders = session.exec(select(LabOrder).where(LabOrder.tenant_id == ctx.tenant_id)).all()
    results: list[OrderListItem] = []
    for o in orders:
        # Resolve related names
        branch = session.get(Branch, o.branch_id)
        patient = session.get(Patient, o.patient_id)
        sample_count = len(session.exec(select(Sample).where(Sample.order_id == o.id)).all())
        has_report = session.exec(select(Report).where(Report.order_id == o.id)).first() is not None

        results.append(
            OrderListItem(
                id=str(o.id),
                order_code=o.order_code,
                status=o.status,
                tenant_id=str(o.tenant_id),
                branch=BranchRef(id=str(o.branch_id), name=branch.name if branch else "", code=branch.code if branch else None),
                patient=PatientRef(
                    id=str(o.patient_id),
                    full_name=f"{patient.first_name} {patient.last_name}" if patient else "",
                    patient_code=patient.patient_code if patient else "",
                ),
                requested_by=o.requested_by,
                notes=o.notes,
                created_at=str(getattr(o, "created_at", "")) if getattr(o, "created_at", None) else None,
                sample_count=sample_count,
                has_report=has_report,
            )
        )

    return OrdersListResponse(orders=results)

@router.post("/orders/", response_model=LabOrderResponse)
def create_order(order_data: LabOrderCreate, session: Session = Depends(get_session)):
    """Create a new laboratory order"""
    # Verify tenant, branch, and patient exist
    tenant = session.get(Tenant, order_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, order_data.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    patient = session.get(Patient, order_data.patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")
    
    # Check if order_code is unique for this branch
    existing_order = session.exec(
        select(LabOrder).where(
            LabOrder.order_code == order_data.order_code,
            LabOrder.branch_id == order_data.branch_id
        )
    ).first()
    
    if existing_order:
        raise HTTPException(400, "Order code already exists for this branch")
    
    order = LabOrder(
        tenant_id=order_data.tenant_id,
        branch_id=order_data.branch_id,
        patient_id=order_data.patient_id,
        order_code=order_data.order_code,
        requested_by=order_data.requested_by,
        notes=order_data.notes,
        created_by=order_data.created_by
    )
    
    session.add(order)
    session.commit()
    session.refresh(order)
    
    return LabOrderResponse(
        id=str(order.id),
        order_code=order.order_code,
        status=order.status,
        patient_id=str(order.patient_id),
        tenant_id=str(order.tenant_id),
        branch_id=str(order.branch_id)
    )

@router.get("/orders/{order_id}", response_model=LabOrderDetailResponse)
def get_order(
    order_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get order details"""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Order not found")
    
    return LabOrderDetailResponse(
        id=str(order.id),
        order_code=order.order_code,
        status=order.status,
        patient_id=str(order.patient_id),
        tenant_id=str(order.tenant_id),
        branch_id=str(order.branch_id),
        requested_by=order.requested_by,
        notes=order.notes,
        billed_lock=order.billed_lock
    )


@router.patch("/orders/{order_id}/notes", response_model=LabOrderDetailResponse)
def update_order_notes(
    order_id: str,
    notes_data: OrderNotesUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update the notes/description of an order"""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Order not found")
    
    old_notes = order.notes
    new_notes = notes_data.notes
    
    # Only update and create event if notes actually changed
    if old_notes != new_notes:
        order.notes = new_notes
        session.add(order)
        
        # Create timeline event for notes update
        event = CaseEvent(
            tenant_id=order.tenant_id,
            branch_id=order.branch_id,
            order_id=order.id,
            sample_id=None,
            event_type=EventType.ORDER_NOTES_UPDATED,
            description="",  # Not used - message built in UI from metadata
            event_metadata={
                "old_notes": old_notes or "",
                "new_notes": new_notes or "",
                "order_id": str(order.id),
                "order_code": order.order_code,
            },
            created_by=user.id,
        )
        session.add(event)
        session.commit()
        session.refresh(order)
    
    return LabOrderDetailResponse(
        id=str(order.id),
        order_code=order.order_code,
        status=order.status,
        patient_id=str(order.patient_id),
        tenant_id=str(order.tenant_id),
        branch_id=str(order.branch_id),
        requested_by=order.requested_by,
        notes=order.notes,
        billed_lock=order.billed_lock
    )


# --- Order Comments Endpoints ---

@router.get("/orders/{order_id}/comments", response_model=CommentsListResponse)
def get_order_comments(
    order_id: str,
    limit: int = 20,
    before: Optional[str] = None,
    after: Optional[str] = None,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get paginated comments for an order with cursor pagination"""
    from app.services.cursor_pagination import decode_cursor, encode_cursor
    from app.models.laboratory import OrderComment, OrderCommentMention
    
    # Validate order access
    order = session.get(LabOrder, order_id)
    if not order or str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Order not found")
    
    # Validate limit
    if limit < 1 or limit > 100:
        raise HTTPException(400, "Limit must be between 1 and 100")
    
    # Build base query
    query = select(OrderComment).where(
        OrderComment.tenant_id == ctx.tenant_id,
        OrderComment.order_id == order_id,
        OrderComment.deleted_at.is_(None)
    )
    
    # Apply cursor filters
    if before:
        try:
            cursor_time, cursor_id = decode_cursor(before)
            query = query.where(
                (OrderComment.created_at < cursor_time) |
                ((OrderComment.created_at == cursor_time) & (OrderComment.id < cursor_id))
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
    
    if after:
        try:
            cursor_time, cursor_id = decode_cursor(after)
            query = query.where(
                (OrderComment.created_at > cursor_time) |
                ((OrderComment.created_at == cursor_time) & (OrderComment.id > cursor_id))
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
    
    # Order and limit (fetch limit+1 to check has_more)
    # ASC = oldest first (top) to newest (bottom) like a normal chat
    query = query.order_by(OrderComment.created_at.asc(), OrderComment.id.asc())
    comments = session.exec(query.limit(limit + 1)).all()
    
    # Check if there are more results
    has_more = len(comments) > limit
    if has_more:
        comments = comments[:limit]
    
    # Resolve mentions for all comments
    comment_ids = [c.id for c in comments]
    mentions_query = select(OrderCommentMention).where(
        OrderCommentMention.comment_id.in_(comment_ids)
    )
    all_mentions = session.exec(mentions_query).all()
    
    # Group mentions by comment
    mentions_by_comment = {}
    user_ids = set()
    for mention in all_mentions:
        if mention.comment_id not in mentions_by_comment:
            mentions_by_comment[mention.comment_id] = []
        mentions_by_comment[mention.comment_id].append(str(mention.user_id))
        user_ids.add(mention.user_id)
    
    # Also collect comment creators for user info
    creator_ids = set(c.created_by for c in comments)
    all_user_ids = user_ids | creator_ids
    
    # Fetch user info for mentions and creators
    users_map = {}
    creator_info = {}
    if all_user_ids:
        users = session.exec(select(AppUser).where(AppUser.id.in_(all_user_ids))).all()
        for u in users:
            user_id_str = str(u.id)
            users_map[user_id_str] = MentionedUser(
                user_id=user_id_str,
                username=u.username,
                name=u.full_name or u.email,
                avatar=u.avatar_url
            )
            creator_info[user_id_str] = {
                "name": u.full_name or u.email,
                "avatar": u.avatar_url
            }
    
    # Build response items
    items = []
    for comment in comments:
        mention_ids = mentions_by_comment.get(comment.id, [])
        mentioned_users = [users_map[uid] for uid in mention_ids if uid in users_map]
        
        creator_id_str = str(comment.created_by)
        creator_data = creator_info.get(creator_id_str, {})
        
        items.append(CommentResponse(
            id=str(comment.id),
            tenant_id=str(comment.tenant_id),
            branch_id=str(comment.branch_id),
            order_id=str(comment.order_id),
            created_at=comment.created_at,
            created_by=creator_id_str,
            created_by_name=creator_data.get("name"),
            created_by_avatar=creator_data.get("avatar"),
            text=comment.text,
            mentions=mention_ids,
            mentioned_users=mentioned_users,
            metadata=comment.comment_metadata,
            edited_at=comment.edited_at,
            deleted_at=comment.deleted_at,
        ))
    
    # Build page info
    page_info = PageInfo(has_more=has_more)
    if items:
        last_item = comments[-1]
        page_info.next_before = encode_cursor(last_item.created_at, str(last_item.id))
        if before:  # If scrolling up, provide after cursor to go back
            first_item = comments[0]
            page_info.next_after = encode_cursor(first_item.created_at, str(first_item.id))
    
    return CommentsListResponse(items=items, page_info=page_info)


@router.post("/orders/{order_id}/comments", response_model=CommentResponse)
def create_order_comment(
    order_id: str,
    comment_data: CommentCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new comment on an order"""
    from app.models.laboratory import OrderComment, OrderCommentMention
    
    # Validate order access
    order = session.get(LabOrder, order_id)
    if not order or str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Order not found")
    
    # Verify user has branch access
    user_branch_ids = [str(b.branch_id) for b in user.branches]
    if str(order.branch_id) not in user_branch_ids:
        raise HTTPException(403, "No access to this order's branch")
    
    # Create comment
    comment = OrderComment(
        tenant_id=order.tenant_id,
        branch_id=order.branch_id,
        order_id=order.id,
        created_by=user.id,
        text=comment_data.text,
        comment_metadata=comment_data.metadata or {},
    )
    session.add(comment)
    session.flush()
    
    # Create mentions
    mentioned_users = []
    if comment_data.mentions:
        # Validate mentioned users exist and belong to tenant
        mentioned_user_objs = session.exec(
            select(AppUser).where(
                AppUser.id.in_(comment_data.mentions),
                AppUser.tenant_id == ctx.tenant_id,
                AppUser.is_active == True
            )
        ).all()
        
        for mentioned_user in mentioned_user_objs:
            mention = OrderCommentMention(
                comment_id=comment.id,
                user_id=mentioned_user.id
            )
            session.add(mention)
            
            mentioned_users.append(MentionedUser(
                user_id=str(mentioned_user.id),
                username=mentioned_user.username,
                name=mentioned_user.full_name or mentioned_user.email,
                avatar=mentioned_user.avatar_url
            ))
    
    # Create timeline event
    event = CaseEvent(
        tenant_id=order.tenant_id,
        branch_id=order.branch_id,
        order_id=order.id,
        event_type=EventType.COMMENT_ADDED,
        description="",
        event_metadata={
            "comment_id": str(comment.id),
            "comment_preview": comment.text[:50] + "..." if len(comment.text) > 50 else comment.text,
            "order_id": str(order.id),
            "order_code": order.order_code,
            "mentions_count": len(mentioned_users),
        },
        created_by=user.id,
    )
    session.add(event)
    session.commit()
    session.refresh(comment)
    
    return CommentResponse(
        id=str(comment.id),
        tenant_id=str(comment.tenant_id),
        branch_id=str(comment.branch_id),
        order_id=str(comment.order_id),
        created_at=comment.created_at,
        created_by=str(comment.created_by),
        created_by_name=user.full_name or user.email,
        created_by_avatar=user.avatar_url,
        text=comment.text,
        mentions=[str(u.user_id) for u in mentioned_users],
        mentioned_users=mentioned_users,
        metadata=comment.comment_metadata,
    )


@router.get("/users/search", response_model=UserMentionListResponse)
def search_users_for_mention(
    q: str = "",
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Search users for mention suggestions"""
    # Get all users from the same tenant
    query = select(AppUser).where(AppUser.tenant_id == ctx.tenant_id, AppUser.is_active == True)
    
    # Filter by search query if provided
    if q:
        search_term = f"%{q.lower()}%"
        query = query.where(
            (AppUser.full_name.ilike(search_term)) |
            (AppUser.username.ilike(search_term)) |
            (AppUser.email.ilike(search_term))
        )
    
    users = session.exec(query.limit(10)).all()
    
    return UserMentionListResponse(
        users=[
            UserMentionItem(
                id=str(u.id),
                name=u.full_name if u.full_name else u.email,
                username=u.username,
                email=u.email,
                avatar_url=u.avatar_url,
            )
            for u in users
        ]
    )


@router.get("/samples/", response_model=SamplesListResponse)
def list_samples(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all samples with enriched branch and order objects. Keeps tenant_id as id string."""
    samples = session.exec(select(Sample).where(Sample.tenant_id == ctx.tenant_id)).all()
    items: list[SampleListItem] = []
    for s in samples:
        branch = session.get(Branch, s.branch_id)
        order = session.get(LabOrder, s.order_id)
        patient = session.get(Patient, order.patient_id) if order else None
        items.append(
            SampleListItem(
                id=str(s.id),
                sample_code=s.sample_code,
                type=s.type,
                state=s.state,
                tenant_id=str(s.tenant_id),
                branch=BranchRef(id=str(s.branch_id), name=branch.name if branch else "", code=branch.code if branch else None),
                order=OrderSlim(
                    id=str(s.order_id),
                    order_code=order.order_code if order else "",
                    status=order.status if order else "",
                    requested_by=order.requested_by if order else None,
                    patient=PatientRef(
                        id=str(patient.id) if patient else "",
                        full_name=f"{patient.first_name} {patient.last_name}" if patient else "",
                        patient_code=patient.patient_code if patient else "",
                    ) if patient else None,
                ),
            )
        )
    return SamplesListResponse(samples=items)

@router.get("/samples/{sample_id}", response_model=SampleDetailResponse)
def get_sample_detail(
    sample_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get complete detail for a sample including branch, order, and patient references."""
    s = session.get(Sample, sample_id)
    if not s:
        raise HTTPException(404, "Sample not found")
    if str(s.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Sample not found")

    branch = session.get(Branch, s.branch_id)
    order = session.get(LabOrder, s.order_id)
    patient = session.get(Patient, order.patient_id) if order else None

    return SampleDetailResponse(
        id=str(s.id),
        sample_code=s.sample_code,
        type=s.type,
        state=s.state,
        collected_at=str(getattr(s, "collected_at", "")) if getattr(s, "collected_at", None) else None,
        received_at=str(getattr(s, "received_at", "")) if getattr(s, "received_at", None) else None,
        notes=s.notes,
        tenant_id=str(s.tenant_id),
        branch=BranchRef(id=str(s.branch_id), name=branch.name if branch else "", code=branch.code if branch else None),
        order=OrderSlim(
            id=str(s.order_id),
            order_code=order.order_code if order else "",
            status=order.status if order else "",
            requested_by=order.requested_by if order else None,
            patient=PatientRef(
                id=str(patient.id) if patient else "",
                full_name=f"{patient.first_name} {patient.last_name}" if patient else "",
                patient_code=patient.patient_code if patient else "",
            ) if patient else None,
        ),
        patient=PatientRef(
            id=str(patient.id) if patient else "",
            full_name=f"{patient.first_name} {patient.last_name}" if patient else "",
            patient_code=patient.patient_code if patient else "",
        ),
    )


@router.patch("/samples/{sample_id}/state", response_model=SampleResponse)
def update_sample_state(
    sample_id: str,
    state_data: SampleStateUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update the state of a sample (RECEIVED, PROCESSING, READY, DAMAGED, CANCELLED)"""
    sample = session.get(Sample, sample_id)
    if not sample:
        raise HTTPException(404, "Sample not found")
    if str(sample.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Sample not found")
    
    # Validate state
    try:
        new_state = SampleState(state_data.state)
    except ValueError:
        raise HTTPException(400, f"Invalid state. Must be one of: {[s.value for s in SampleState]}")
    
    old_state = sample.state
    sample.state = new_state
    session.add(sample)
    
    # Determine event type based on new state
    if new_state == SampleState.DAMAGED:
        event_type = EventType.SAMPLE_DAMAGED
    elif new_state == SampleState.CANCELLED:
        event_type = EventType.SAMPLE_CANCELLED
    else:
        event_type = EventType.SAMPLE_STATE_CHANGED
    
    # Create timeline event with structured metadata (no localized description)
    # The UI will build the message based on event_type and metadata
    event = CaseEvent(
        tenant_id=sample.tenant_id,
        branch_id=sample.branch_id,
        order_id=sample.order_id,
        sample_id=sample.id,
        event_type=event_type,
        description="",  # Not used - message built in UI from metadata
        event_metadata={
            "old_state": old_state.value if hasattr(old_state, 'value') else str(old_state),
            "new_state": new_state.value,
            "sample_id": str(sample.id),
            "sample_code": sample.sample_code,
            "sample_type": sample.type.value if hasattr(sample.type, 'value') else str(sample.type),
        },
        created_by=user.id,
    )
    session.add(event)
    
    # Update order status based on sample state change
    update_order_status(str(sample.order_id), session)
    
    session.commit()
    session.refresh(sample)
    
    return SampleResponse(
        id=str(sample.id),
        sample_code=sample.sample_code,
        type=sample.type,
        state=sample.state,
        order_id=str(sample.order_id),
        tenant_id=str(sample.tenant_id),
        branch_id=str(sample.branch_id),
    )


@router.patch("/samples/{sample_id}/notes", response_model=SampleResponse)
def update_sample_notes(
    sample_id: str,
    notes_data: SampleNotesUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update the notes/description of a sample"""
    sample = session.get(Sample, sample_id)
    if not sample:
        raise HTTPException(404, "Sample not found")
    if str(sample.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Sample not found")
    
    old_notes = sample.notes
    new_notes = notes_data.notes
    
    # Only update and create event if notes actually changed
    if old_notes != new_notes:
        sample.notes = new_notes
        session.add(sample)
        
        # Create timeline event for notes update
        event = CaseEvent(
            tenant_id=sample.tenant_id,
            branch_id=sample.branch_id,
            order_id=sample.order_id,
            sample_id=sample.id,
            event_type=EventType.SAMPLE_NOTES_UPDATED,
            description="",  # Not used - message built in UI from metadata
            event_metadata={
                "old_notes": old_notes or "",
                "new_notes": new_notes or "",
                "sample_id": str(sample.id),
                "sample_code": sample.sample_code,
            },
            created_by=user.id,
        )
        session.add(event)
        session.commit()
        session.refresh(sample)
    
    return SampleResponse(
        id=str(sample.id),
        sample_code=sample.sample_code,
        type=sample.type,
        state=sample.state,
        order_id=str(sample.order_id),
        tenant_id=str(sample.tenant_id),
        branch_id=str(sample.branch_id),
    )


@router.post("/samples/", response_model=SampleResponse)
def create_sample(sample_data: SampleCreate, session: Session = Depends(get_session)):
    """Create a new sample"""
    # Verify tenant, branch, and order exist
    tenant = session.get(Tenant, sample_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branch = session.get(Branch, sample_data.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    
    order = session.get(LabOrder, sample_data.order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    # Check if sample_code is unique for this order
    existing_sample = session.exec(
        select(Sample).where(
            Sample.sample_code == sample_data.sample_code,
            Sample.order_id == sample_data.order_id
        )
    ).first()
    
    if existing_sample:
        raise HTTPException(400, "Sample code already exists for this order")
    
    sample = Sample(
        tenant_id=sample_data.tenant_id,
        branch_id=sample_data.branch_id,
        order_id=sample_data.order_id,
        sample_code=sample_data.sample_code,
        type=sample_data.type,
        notes=sample_data.notes,
        collected_at=sample_data.collected_at,
        received_at=sample_data.received_at
    )
    
    session.add(sample)
    session.flush()  # Get sample.id for event
    
    # Create SAMPLE_CREATED event
    sample_created_event = CaseEvent(
        order_id=sample_data.order_id,
        sample_id=sample.id,
        event_type=EventType.SAMPLE_CREATED,
        description="Muestra registrada",
        event_metadata={
            "sample_id": str(sample.id),
            "sample_code": sample.sample_code,
            "sample_type": sample.type,
            "initial_state": SampleState.RECEIVED.value,
        },
        created_by=sample_data.created_by if hasattr(sample_data, 'created_by') else None,
    )
    session.add(sample_created_event)
    
    # Update order status based on new sample
    update_order_status(str(sample_data.order_id), session)
    
    session.commit()
    session.refresh(sample)
    
    return SampleResponse(
        id=str(sample.id),
        sample_code=sample.sample_code,
        type=sample.type,
        state=sample.state,
        order_id=str(sample.order_id),
        tenant_id=str(sample.tenant_id),
        branch_id=str(sample.branch_id)
    )


@router.post("/orders/unified", response_model=LabOrderUnifiedResponse)
def create_order_with_samples(payload: LabOrderUnifiedCreate, session: Session = Depends(get_session)):
    """Create a new laboratory order and multiple samples in one operation.

    - Validates tenant, branch, and patient
    - Ensures `order_code` uniqueness per branch
    - Creates order and all samples atomically
    """
    # Validate tenant, branch, and patient
    tenant = session.get(Tenant, payload.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    branch = session.get(Branch, payload.branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    patient = session.get(Patient, payload.patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")

    # Ensure order_code unique per branch
    existing = session.exec(
        select(LabOrder).where(
            LabOrder.order_code == payload.order_code,
            LabOrder.branch_id == payload.branch_id,
        )
    ).first()
    if existing:
        raise HTTPException(400, "Order code already exists for this branch")

    # Create order
    order = LabOrder(
        tenant_id=payload.tenant_id,
        branch_id=payload.branch_id,
        patient_id=payload.patient_id,
        order_code=payload.order_code,
        requested_by=payload.requested_by,
        notes=payload.notes,
        created_by=payload.created_by,
    )
    session.add(order)
    session.flush()  # get order.id for samples

    # Create samples
    created_samples: list[Sample] = []
    sample_codes_in_order: set[str] = set()
    for s in payload.samples:
        # Prevent duplicate sample_code within this unified request
        if s.sample_code in sample_codes_in_order:
            raise HTTPException(400, f"Duplicate sample_code in request: {s.sample_code}")
        sample_codes_in_order.add(s.sample_code)

        # Ensure sample_code unique for this order
        existing_sample = session.exec(
            select(Sample).where(
                Sample.sample_code == s.sample_code,
                Sample.order_id == order.id,
            )
        ).first()
        if existing_sample:
            raise HTTPException(400, f"Sample code already exists for this order: {s.sample_code}")

        sample = Sample(
            tenant_id=payload.tenant_id,
            branch_id=payload.branch_id,
            order_id=order.id,
            sample_code=s.sample_code,
            type=s.type,
            notes=s.notes,
            collected_at=s.collected_at,
            received_at=s.received_at,
        )
        session.add(sample)
        session.flush()  # Get sample.id for event
        
        # Create SAMPLE_CREATED event for each sample
        sample_created_event = CaseEvent(
            order_id=order.id,
            sample_id=sample.id,
            event_type=EventType.SAMPLE_CREATED,
            description="Muestra registrada",
            event_metadata={
                "sample_id": str(sample.id),
                "sample_code": sample.sample_code,
                "sample_type": sample.type,
                "initial_state": SampleState.RECEIVED.value,
            },
            created_by=payload.created_by,
        )
        session.add(sample_created_event)
        created_samples.append(sample)

    # Update order status based on new samples
    update_order_status(str(order.id), session)

    # Commit transaction
    session.commit()
    session.refresh(order)
    for s in created_samples:
        session.refresh(s)

    return LabOrderUnifiedResponse(
        order=LabOrderResponse(
            id=str(order.id),
            order_code=order.order_code,
            status=order.status,
            patient_id=str(order.patient_id),
            tenant_id=str(order.tenant_id),
            branch_id=str(order.branch_id),
        ),
        samples=[
            SampleResponse(
                id=str(s.id),
                sample_code=s.sample_code,
                type=s.type,
                state=s.state,
                order_id=str(s.order_id),
                tenant_id=str(s.tenant_id),
                branch_id=str(s.branch_id),
            )
            for s in created_samples
        ],
    )


@router.post("/samples/{sample_id}/images", response_model=SampleImageUploadResponse)
def upload_sample_image(
    sample_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Upload an image (regular or RAW) for a sample, store S3 keys and DB mapping.

    Stores:
    - RAW original (if applicable)
    - processed JPEG
    - thumbnail JPEG
    """
    sample = session.get(Sample, sample_id)
    if not sample:
        raise HTTPException(404, "Sample not found")

    filename = file.filename or "upload"
    # Basic size guard: read up to 50MB for non-RAW, 500MB for RAW candidates
    # We decide limit by extension to avoid over-reading large unexpected files
    lower = filename.lower()
    raw_exts = (".cr2", ".cr3", ".nef", ".nrw", ".arw", ".sr2", ".raf", ".rw2", ".orf", ".pef", ".dng")
    max_bytes = 500 * 1024 * 1024 if any(lower.endswith(ext) for ext in raw_exts) else 50 * 1024 * 1024

    # Read in chunks to avoid memory spikes and enforce limit
    chunk_size = 1024 * 1024
    received = 0
    chunks = []
    while True:
        chunk = file.file.read(chunk_size)
        if not chunk:
            break
        received += len(chunk)
        if received > max_bytes:
            raise HTTPException(413, f"File too large. Limit is {max_bytes // (1024*1024)}MB for this file type")
        chunks.append(chunk)
    data = b"".join(chunks)

    processed = process_image_bytes(filename, data)
    is_raw = processed.original_bytes is not None

    s3 = S3Service()
    unique_id = uuid4().hex[:8]
    base_name, ext = os.path.splitext(filename)

    # Build S3 keys (tenant/branch/sample grouping)
    base_prefix = f"samples/{sample.tenant_id}/{sample.branch_id}/{sample.id}"

    # Upload processed and thumbnail
    processed_key = f"{base_prefix}/processed/{base_name}_{unique_id}.jpg"
    processed_info = s3.upload_bytes(
        processed.processed_jpeg_bytes,
        key=processed_key,
        content_type="image/jpeg",
        acl=None,
    )

    thumb_key = f"{base_prefix}/thumbnails/{base_name}_{unique_id}.jpg"
    thumb_info = s3.upload_bytes(
        processed.thumbnail_jpeg_bytes,
        key=thumb_key,
        content_type="image/jpeg",
        acl=None,
    )

    # Create StorageObject rows
    processed_storage = StorageObject(
        provider="aws",
        region=s3.region,
        bucket=processed_info.bucket,
        object_key=processed_info.key,
        version_id=processed_info.version_id,
        etag=processed_info.etag,
        content_type="image/jpeg",
        size_bytes=processed_info.size_bytes,
    )
    session.add(processed_storage)
    session.flush()

    thumb_storage = StorageObject(
        provider="aws",
        region=s3.region,
        bucket=thumb_info.bucket,
        object_key=thumb_info.key,
        version_id=thumb_info.version_id,
        etag=thumb_info.etag,
        content_type="image/jpeg",
        size_bytes=thumb_info.size_bytes,
    )
    session.add(thumb_storage)
    session.flush()

    # Link as SampleImage and renditions
    sample_image = SampleImage(
        tenant_id=sample.tenant_id,
        branch_id=sample.branch_id,
        sample_id=sample.id,
        storage_id=processed_storage.id,
        label=None,
        is_primary=False,
    )
    session.add(sample_image)
    session.flush()

    rendition_thumb = SampleImageRendition(
        sample_image_id=sample_image.id,
        kind="thumbnail",
        storage_id=thumb_storage.id,
    )
    session.add(rendition_thumb)

    original_storage = None
    if is_raw and processed.original_bytes:
        raw_key = f"{base_prefix}/raw/{base_name}_{unique_id}{ext.lower()}"
        raw_info = s3.upload_bytes(
            processed.original_bytes,
            key=raw_key,
            content_type=file.content_type or "application/octet-stream",
            acl=None,
        )
        original_storage = StorageObject(
            provider="aws",
            region=s3.region,
            bucket=raw_info.bucket,
            object_key=raw_info.key,
            version_id=raw_info.version_id,
            etag=raw_info.etag,
            content_type=file.content_type,
            size_bytes=raw_info.size_bytes,
        )
        session.add(original_storage)
        session.flush()
        rendition_original = SampleImageRendition(
            sample_image_id=sample_image.id,
            kind="original_raw",
            storage_id=original_storage.id,
        )
        session.add(rendition_original)

    # Auto-update sample state to PROCESSING only on first image upload
    # Check if this is the first image (only the one we just added)
    existing_images_count = session.exec(
        select(SampleImage).where(SampleImage.sample_id == sample.id)
    ).all()
    is_first_image = len(existing_images_count) == 1
    if is_first_image and sample.state == SampleState.RECEIVED:
        sample.state = SampleState.PROCESSING
        session.add(sample)
    
    # Create timeline event for image upload with structured metadata
    event = CaseEvent(
        tenant_id=sample.tenant_id,
        branch_id=sample.branch_id,
        order_id=sample.order_id,
        sample_id=sample.id,
        event_type=EventType.IMAGE_UPLOADED,
        description="",  # Not used - message built in UI from metadata
        event_metadata={
            "filename": filename,
            "is_raw": is_raw,
            "file_size": len(data),
            "sample_id": str(sample.id),
            "sample_code": sample.sample_code,
            "sample_type": sample.type.value if hasattr(sample.type, 'value') else str(sample.type),
            "image_id": str(sample_image.id),
        },
        created_by=user.id,
    )
    session.add(event)
    
    # If state changed, also add state change event
    if is_first_image and sample.state == SampleState.PROCESSING:
        state_event = CaseEvent(
            tenant_id=sample.tenant_id,
            branch_id=sample.branch_id,
            order_id=sample.order_id,
            sample_id=sample.id,
            event_type=EventType.SAMPLE_STATE_CHANGED,
            description="",  # Not used - message built in UI from metadata
            event_metadata={
                "old_state": "RECEIVED",
                "new_state": "PROCESSING",
                "sample_id": str(sample.id),
                "sample_code": sample.sample_code,
                "sample_type": sample.type.value if hasattr(sample.type, 'value') else str(sample.type),
                "trigger": "first_image_upload",
            },
            created_by=user.id,
        )
        session.add(state_event)

    # Update order status based on sample state change (if it changed)
    if is_first_image and sample.state == SampleState.PROCESSING:
        update_order_status(str(sample.order_id), session)

    session.commit()

    urls: Dict[str, str] = {
        "processed": s3.object_public_url(processed_key),
        "thumbnail": s3.object_public_url(thumb_key),
    }
    if is_raw and original_storage is not None:
        urls["raw"] = s3.object_public_url(original_storage.object_key)

    return SampleImageUploadResponse(
        message="Image uploaded successfully",
        sample_image_id=str(sample_image.id),
        filename=filename,
        is_raw=is_raw,
        file_size=len(data),
        urls=urls,
    )


@router.get("/samples/{sample_id}/images", response_model=SampleImagesListResponse)
def list_sample_images(sample_id: str, session: Session = Depends(get_session)):
    """List images and their URLs for a sample, similar to file_mapping.json idea."""
    sample = session.get(Sample, sample_id)
    if not sample:
        raise HTTPException(404, "Sample not found")

    s3 = S3Service()

    # Fetch images and renditions with simple selects
    images = session.exec(
        select(SampleImage).where(SampleImage.sample_id == sample.id)
    ).all()

    results = []
    for img in images:
        # main processed image
        storage = session.get(StorageObject, img.storage_id)
        urls: Dict[str, str] = {}
        if storage:
            urls["processed"] = s3.object_public_url(storage.object_key)

        # renditions
        renditions = session.exec(
            select(SampleImageRendition).where(SampleImageRendition.sample_image_id == img.id)
        ).all()
        for r in renditions:
            r_storage = session.get(StorageObject, r.storage_id)
            if r_storage:
                urls[r.kind] = s3.object_public_url(r_storage.object_key)

        results.append(
            SampleImageInfo(
                id=str(img.id),
                label=img.label,
                is_primary=img.is_primary,
                created_at=str(img.created_at),
                urls=urls,
            )
        )

    return SampleImagesListResponse(sample_id=str(sample.id), images=results)


@router.delete("/samples/{sample_id}/images/{image_id}")
def delete_sample_image(
    sample_id: str,
    image_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Delete a sample image and its associated storage objects"""
    # Verify sample exists and belongs to tenant
    sample = session.get(Sample, sample_id)
    if not sample:
        raise HTTPException(404, "Sample not found")
    if str(sample.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Sample not found")
    
    # Verify image exists and belongs to this sample
    sample_image = session.get(SampleImage, image_id)
    if not sample_image:
        raise HTTPException(404, "Image not found")
    if str(sample_image.sample_id) != sample_id:
        raise HTTPException(404, "Image not found")
    
    # Get the storage object to get the filename for the event
    storage = session.get(StorageObject, sample_image.storage_id)
    filename = storage.object_key.split("/")[-1] if storage else "imagen"
    
    # Collect storage IDs before deleting anything
    storage_ids_to_delete = [sample_image.storage_id]
    
    # Delete renditions first (thumbnail, original_raw, etc.)
    renditions = session.exec(
        select(SampleImageRendition).where(SampleImageRendition.sample_image_id == sample_image.id)
    ).all()
    
    for rendition in renditions:
        storage_ids_to_delete.append(rendition.storage_id)
        session.delete(rendition)
    
    # Delete the sample image record
    session.delete(sample_image)
    
    # Flush to ensure SampleImage and renditions are deleted before we delete storage objects
    session.flush()
    
    # Delete storage objects (optional: could also delete from S3 here)
    # For now, we just remove the DB records - S3 cleanup can be done via lifecycle rules
    for storage_id in storage_ids_to_delete:
        storage_obj = session.get(StorageObject, storage_id)
        if storage_obj:
            session.delete(storage_obj)
    
    # Create timeline event for image deletion
    event = CaseEvent(
        tenant_id=sample.tenant_id,
        branch_id=sample.branch_id,
        order_id=sample.order_id,
        sample_id=sample.id,
        event_type=EventType.IMAGE_DELETED,
        description="",  # Not used - message built in UI from metadata
        event_metadata={
            "filename": filename,
            "image_id": image_id,
            "sample_id": str(sample.id),
            "sample_code": sample.sample_code,
        },
        created_by=user.id,
    )
    session.add(event)
    
    session.commit()
    
    logger.info(
        f"Image deleted from sample {sample_id}",
        extra={
            "event": "sample_image.deleted",
            "sample_id": sample_id,
            "image_id": image_id,
            "user_id": str(user.id),
        },
    )
    
    return {"message": "Image deleted successfully", "image_id": image_id}


@router.get("/orders/{order_id}/full", response_model=LabOrderFullDetailResponse)
def get_order_full_detail(
    order_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Return complete information for an order: order details, patient details, and samples."""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Order not found")

    patient = session.get(Patient, order.patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")

    # Fetch samples for this order
    samples = session.exec(select(Sample).where(Sample.order_id == order.id)).all()

    # Build response objects
    order_resp = LabOrderDetailResponse(
        id=str(order.id),
        order_code=order.order_code,
        status=order.status,
        patient_id=str(order.patient_id),
        tenant_id=str(order.tenant_id),
        branch_id=str(order.branch_id),
        requested_by=order.requested_by,
        notes=order.notes,
        billed_lock=order.billed_lock,
    )

    patient_resp = PatientFullResponse(
        id=str(patient.id),
        tenant_id=str(patient.tenant_id),
        branch_id=str(patient.branch_id),
        patient_code=patient.patient_code,
        first_name=patient.first_name,
        last_name=patient.last_name,
        dob=getattr(patient, "dob", None),
        sex=getattr(patient, "sex", None),
        phone=getattr(patient, "phone", None),
        email=getattr(patient, "email", None),
    )

    sample_resps = [
        SampleResponse(
            id=str(s.id),
            sample_code=s.sample_code,
            type=s.type,
            state=s.state,
            order_id=str(s.order_id),
            tenant_id=str(s.tenant_id),
            branch_id=str(s.branch_id),
        )
        for s in samples
    ]

    return LabOrderFullDetailResponse(order=order_resp, patient=patient_resp, samples=sample_resps)


@router.get("/patients/{patient_id}/orders", response_model=PatientOrdersListResponse)
def list_patient_orders(
    patient_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all orders for a given patient (enriched summary)."""
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")

    if str(patient.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Patient not found")
    orders = session.exec(select(LabOrder).where(LabOrder.patient_id == patient.id, LabOrder.tenant_id == ctx.tenant_id)).all()
    summaries = []
    for o in orders:
        sample_count = len(session.exec(select(Sample).where(Sample.order_id == o.id)).all())
        has_report = session.exec(select(Report).where(Report.order_id == o.id)).first() is not None
        summaries.append(
            PatientOrderSummary(
                id=str(o.id),
                order_code=o.order_code,
                status=o.status,
                tenant_id=str(o.tenant_id),
                branch_id=str(o.branch_id),
                patient_id=str(o.patient_id),
                requested_by=o.requested_by,
                notes=o.notes,
                created_at=str(getattr(o, "created_at", "")) if getattr(o, "created_at", None) else None,
                sample_count=sample_count,
                has_report=has_report,
            )
        )

    full_patient = PatientFullResponse(
        id=str(patient.id),
        tenant_id=str(patient.tenant_id),
        branch_id=str(patient.branch_id),
        patient_code=patient.patient_code,
        first_name=patient.first_name,
        last_name=patient.last_name,
        dob=getattr(patient, "dob", None),
        sex=getattr(patient, "sex", None),
        phone=getattr(patient, "phone", None),
        email=getattr(patient, "email", None),
    )

    return PatientOrdersListResponse(patient=full_patient, orders=summaries)


@router.get("/patients/{patient_id}/cases", response_model=PatientCasesListResponse)
def list_patient_cases(
    patient_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all cases for a patient with full details: order, samples, and report metadata (if any)."""
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")

    if str(patient.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Patient not found")
    orders = session.exec(select(LabOrder).where(LabOrder.patient_id == patient.id, LabOrder.tenant_id == ctx.tenant_id)).all()
    cases = []
    for order in orders:
        # Order detail
        order_resp = LabOrderDetailResponse(
            id=str(order.id),
            order_code=order.order_code,
            status=order.status,
            patient_id=str(order.patient_id),
            tenant_id=str(order.tenant_id),
            branch_id=str(order.branch_id),
            requested_by=order.requested_by,
            notes=order.notes,
            billed_lock=order.billed_lock,
        )

        # Samples list
        samples = session.exec(select(Sample).where(Sample.order_id == order.id)).all()
        sample_resps = [
            SampleResponse(
                id=str(s.id),
                sample_code=s.sample_code,
                type=s.type,
                state=s.state,
                order_id=str(s.order_id),
                tenant_id=str(s.tenant_id),
                branch_id=str(s.branch_id),
            )
            for s in samples
        ]

        # Report meta (if any) using latest/current version
        report_meta = None
        report = session.exec(select(Report).where(Report.order_id == order.id)).first()
        if report:
            current_version = session.exec(
                select(ReportVersion)
                .where(ReportVersion.report_id == report.id, ReportVersion.is_current == True)
                .order_by(ReportVersion.version_no.desc())
            ).first()
            has_pdf = bool(current_version and current_version.pdf_storage_id)
            report_meta = ReportMetaResponse(
                id=str(report.id),
                status=report.status,
                title=report.title,
                published_at=report.published_at,
                version_no=current_version.version_no if current_version else None,
                has_pdf=has_pdf,
            )

        cases.append({
            "order": order_resp,
            "samples": sample_resps,
            "report": report_meta,
        })

    # Build patient full profile to attach at top-level of cases list
    full_patient = PatientFullResponse(
        id=str(patient.id),
        tenant_id=str(patient.tenant_id),
        branch_id=str(patient.branch_id),
        patient_code=patient.patient_code,
        first_name=patient.first_name,
        last_name=patient.last_name,
        dob=getattr(patient, "dob", None),
        sex=getattr(patient, "sex", None),
        phone=getattr(patient, "phone", None),
        email=getattr(patient, "email", None),
    )

    # Reuse PatientCaseDetail schema structure via dicts already matching response_model
    return PatientCasesListResponse(patient=full_patient, patient_id=str(patient.id), cases=cases)


# Case Events (Timeline) Endpoints

@router.post("/orders/{order_id}/events", response_model=EventResponse)
def create_case_event(
    order_id: str,
    event_data: EventCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new event in the case timeline"""
    # Verify order exists and belongs to tenant
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    if str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Order does not belong to your tenant")
    
    # Create event
    event = CaseEvent(
        tenant_id=order.tenant_id,
        branch_id=order.branch_id,
        order_id=order.id,
        event_type=EventType(event_data.event_type),
        description=event_data.description,
        event_metadata=event_data.metadata,
        created_by=user.id,
    )
    
    session.add(event)
    session.commit()
    session.refresh(event)
    
    logger.info(
        f"Event created for order {order_id}",
        extra={
            "event": "case_event.created",
            "order_id": order_id,
            "event_type": event_data.event_type,
            "user_id": str(user.id),
        },
    )
    
    return EventResponse(
        id=str(event.id),
        tenant_id=str(event.tenant_id),
        branch_id=str(event.branch_id),
        order_id=str(event.order_id),
        event_type=event.event_type,
        description=event.description,
        metadata=event.event_metadata,
        created_by=str(event.created_by) if event.created_by else None,
        created_at=event.created_at,
    )


@router.get("/orders/{order_id}/events", response_model=EventsListResponse)
def list_case_events(
    order_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get timeline of events for a case (order + all samples + report)"""
    # Verify order exists and belongs to tenant
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    
    if str(order.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Order does not belong to your tenant")
    
    # Get events ordered by creation date (oldest first, top to bottom)
    events = session.exec(
        select(CaseEvent)
        .where(CaseEvent.order_id == order_id)
        .order_by(CaseEvent.created_at.asc())
    ).all()
    
    # Get user info for display (name + avatar)
    user_info: dict[str, dict] = {}
    for e in events:
        if e.created_by and str(e.created_by) not in user_info:
            user = session.get(AppUser, e.created_by)
            if user:
                user_info[str(e.created_by)] = {
                    "name": user.full_name,
                    "avatar": user.avatar_url,
                }
    
    return EventsListResponse(
        events=[
            EventResponse(
                id=str(e.id),
                tenant_id=str(e.tenant_id),
                branch_id=str(e.branch_id),
                order_id=str(e.order_id),
                sample_id=str(e.sample_id) if e.sample_id else None,
                event_type=e.event_type,
                description=e.description,
                metadata=e.event_metadata,
                created_by=str(e.created_by) if e.created_by else None,
                created_by_name=user_info.get(str(e.created_by), {}).get("name") if e.created_by else None,
                created_by_avatar=user_info.get(str(e.created_by), {}).get("avatar") if e.created_by else None,
                created_at=e.created_at,
            )
            for e in events
        ]
    )


@router.get("/samples/{sample_id}/events", response_model=EventsListResponse)
def list_sample_events(
    sample_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get timeline of events for a specific sample"""
    # Verify sample exists and belongs to tenant
    sample = session.get(Sample, sample_id)
    if not sample:
        raise HTTPException(404, "Sample not found")
    
    if str(sample.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Sample does not belong to your tenant")
    
    # Get events for this sample ordered by creation date (oldest first, top to bottom)
    events = session.exec(
        select(CaseEvent)
        .where(CaseEvent.sample_id == sample_id)
        .order_by(CaseEvent.created_at.asc())
    ).all()
    
    # Get user info for display (name + avatar)
    user_info: dict[str, dict] = {}
    for e in events:
        if e.created_by and str(e.created_by) not in user_info:
            user = session.get(AppUser, e.created_by)
            if user:
                user_info[str(e.created_by)] = {
                    "name": user.full_name,
                    "avatar": user.avatar_url,
                }
    
    return EventsListResponse(
        events=[
            EventResponse(
                id=str(e.id),
                tenant_id=str(e.tenant_id),
                branch_id=str(e.branch_id),
                order_id=str(e.order_id),
                sample_id=str(e.sample_id) if e.sample_id else None,
                event_type=e.event_type,
                description=e.description,
                metadata=e.event_metadata,
                created_by=str(e.created_by) if e.created_by else None,
                created_by_name=user_info.get(str(e.created_by), {}).get("name") if e.created_by else None,
                created_by_avatar=user_info.get(str(e.created_by), {}).get("avatar") if e.created_by else None,
                created_at=e.created_at,
            )
            for e in events
        ]
    )
