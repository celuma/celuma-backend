"""
Worklist API endpoints for managing assignments and report reviews.

Provides:
- GET /me/worklist - Unified worklist for current user
- POST /assignments - Create assignment
- DELETE /assignments/{id} - Soft unassign
- GET /assignments - List assignments with filters
- GET /report-reviews - List report reviews with filters
- POST /report-reviews/{id}/decision - Approve/reject review
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select, Session, and_, or_
from sqlalchemy import cast, String
from typing import Optional, List
from uuid import UUID
from datetime import datetime

from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.models.user import AppUser
from app.models.assignment import Assignment
from app.models.report_review import ReportReview
from app.models.laboratory import Order, Sample, OrderComment
from app.models.report import Report
from app.models.patient import Patient
from app.models.enums import AssignmentItemType, ReviewStatus, ReportStatus, EventType
from app.models.events import OrderEvent
from app.schemas.worklist import (
    AssignmentCreate,
    AssignmentResponse,
    AssignmentsListResponse,
    AssigneesUpdate,
    ReviewersUpdate,
    ReportReviewResponse,
    ReportReviewsListResponse,
    ReportReviewDecision,
    WorklistItemResponse,
    WorklistResponse,
    UserRef,
)

import logging

logger = logging.getLogger(__name__)

router = APIRouter()


def _user_to_ref(user: AppUser) -> UserRef:
    """Convert AppUser to UserRef"""
    return UserRef(
        id=str(user.id),
        name=user.full_name or user.username or user.email,
        email=user.email,
        avatar_url=user.avatar_url
    )


def _get_user_ref(session: Session, user_id: Optional[UUID]) -> Optional[UserRef]:
    """Get UserRef for a user ID, or None if not found"""
    if not user_id:
        return None
    user = session.get(AppUser, user_id)
    if not user:
        return None
    return _user_to_ref(user)


# === Worklist Endpoint ===

@router.get("/me/worklist", response_model=WorklistResponse)
def get_my_worklist(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    kind: Optional[str] = Query(None, description="Filter by kind: assignment | review"),
    item_type: Optional[str] = Query(None, description="Filter by item type: lab_order | sample | report"),
    status: Optional[str] = Query(None, description="Filter by status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Get unified worklist for current user.
    
    Returns both assignments and pending reviews in a unified format,
    sorted by assigned_at descending.
    """
    items: List[WorklistItemResponse] = []
    
    # 1. Get assignments for current user
    if kind is None or kind == "assignment":
        assignment_query = select(Assignment).where(
            and_(
                Assignment.tenant_id == UUID(ctx.tenant_id),
                Assignment.assignee_user_id == user.id,
                Assignment.unassigned_at.is_(None),
            )
        )
        
        if item_type:
            assignment_query = assignment_query.where(cast(Assignment.item_type, String) == item_type)
        
        assignments = session.exec(assignment_query).all()
        
        for assignment in assignments:
            item = _build_assignment_worklist_item(session, assignment)
            if item:
                items.append(item)
    
    # 2. Get pending reviews for current user
    if kind is None or kind == "review":
        review_query = select(ReportReview).where(
            and_(
                ReportReview.tenant_id == UUID(ctx.tenant_id),
                ReportReview.reviewer_user_id == user.id,
            )
        )
        
        if status:
            review_query = review_query.where(ReportReview.status == status.upper())
        # Include all reviews (pending, approved, rejected) - no default filter
        
        reviews = session.exec(review_query).all()
        
        for review in reviews:
            item = _build_review_worklist_item(session, review)
            if item:
                items.append(item)
    
    # 3. Sort by assigned_at descending
    items.sort(key=lambda x: x.assigned_at, reverse=True)
    
    # 4. Paginate
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    paginated_items = items[start:end]
    has_more = end < total
    
    return WorklistResponse(
        items=paginated_items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=has_more
    )


def _build_assignment_worklist_item(session: Session, assignment: Assignment) -> Optional[WorklistItemResponse]:
    """Build a WorklistItemResponse from an Assignment"""
    item_type = assignment.item_type.value if hasattr(assignment.item_type, 'value') else str(assignment.item_type)
    
    if item_type == "lab_order":
        order = session.get(Order, assignment.item_id)
        if not order:
            return None
        patient = session.get(Patient, order.patient_id)
        return WorklistItemResponse(
            id=str(assignment.id),
            kind="assignment",
            item_type="lab_order",
            item_id=str(assignment.item_id),
            display_id=order.order_code,
            item_status=order.status.value if hasattr(order.status, 'value') else str(order.status),
            assigned_at=assignment.assigned_at,
            patient_id=str(patient.id) if patient else None,
            patient_name=patient.first_name + " " + patient.last_name if patient else None,
            patient_code=patient.patient_code if patient else None,
            order_code=order.order_code,
            link=f"/orders/{assignment.item_id}"
        )
    
    elif item_type == "sample":
        sample = session.get(Sample, assignment.item_id)
        if not sample:
            return None
        order = session.get(Order, sample.order_id)
        patient = session.get(Patient, order.patient_id) if order else None
        return WorklistItemResponse(
            id=str(assignment.id),
            kind="assignment",
            item_type="sample",
            item_id=str(assignment.item_id),
            display_id=sample.sample_code,
            item_status=sample.state.value if hasattr(sample.state, 'value') else str(sample.state),
            assigned_at=assignment.assigned_at,
            patient_id=str(patient.id) if patient else None,
            patient_name=patient.first_name + " " + patient.last_name if patient else None,
            patient_code=patient.patient_code if patient else None,
            order_code=order.order_code if order else None,
            link=f"/samples/{assignment.item_id}"
        )
    
    elif item_type == "report":
        report = session.get(Report, assignment.item_id)
        if not report:
            return None
        order = session.get(Order, report.order_id)
        patient = session.get(Patient, order.patient_id) if order else None
        return WorklistItemResponse(
            id=str(assignment.id),
            kind="assignment",
            item_type="report",
            item_id=str(assignment.item_id),
            display_id=report.title or order.order_code if order else str(assignment.item_id),
            item_status=report.status.value if hasattr(report.status, 'value') else str(report.status),
            assigned_at=assignment.assigned_at,
            patient_id=str(patient.id) if patient else None,
            patient_name=patient.first_name + " " + patient.last_name if patient else None,
            patient_code=patient.patient_code if patient else None,
            order_code=order.order_code if order else None,
            link=f"/reports/{assignment.item_id}"
        )
    
    return None


def _build_review_worklist_item(session: Session, review: ReportReview) -> Optional[WorklistItemResponse]:
    """Build a WorklistItemResponse from a ReportReview"""
    order = session.get(Order, review.order_id)
    if not order:
        return None
    
    # Get report for this order - use review.report_id if available, otherwise query by order_id
    report = None
    if review.report_id:
        report = session.get(Report, review.report_id)
    else:
        report = session.exec(
            select(Report).where(Report.order_id == review.order_id)
        ).first()
    
    # Reviews require a report to exist - skip if no report found
    if not report:
        return None
    
    patient = session.get(Patient, order.patient_id) if order else None
    
    return WorklistItemResponse(
        id=str(review.id),
        kind="review",
        item_type="report",
        item_id=str(report.id),
        display_id=report.title or order.order_code,
        item_status=review.status.value if hasattr(review.status, 'value') else str(review.status),
        assigned_at=review.assigned_at,
        patient_id=str(patient.id) if patient else None,
        patient_name=patient.first_name + " " + patient.last_name if patient else None,
        patient_code=patient.patient_code if patient else None,
        order_code=order.order_code if order else None,
        link=f"/reports/{report.id}"
    )


# === Assignment Endpoints ===

@router.get("/assignments", response_model=AssignmentsListResponse)
def list_assignments(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    item_type: Optional[str] = Query(None),
    item_id: Optional[str] = Query(None),
    assignee_user_id: Optional[str] = Query(None),
    include_unassigned: bool = Query(False),
):
    """List assignments with filters"""
    query = select(Assignment).where(Assignment.tenant_id == UUID(ctx.tenant_id))
    
    if item_type:
        query = query.where(cast(Assignment.item_type, String) == item_type)
    
    if item_id:
        query = query.where(Assignment.item_id == UUID(item_id))
    
    if assignee_user_id:
        query = query.where(Assignment.assignee_user_id == UUID(assignee_user_id))
    
    if not include_unassigned:
        query = query.where(Assignment.unassigned_at.is_(None))
    
    query = query.order_by(Assignment.assigned_at.desc())
    
    assignments = session.exec(query).all()
    
    results = []
    for a in assignments:
        results.append(AssignmentResponse(
            id=str(a.id),
            tenant_id=str(a.tenant_id),
            item_type=a.item_type.value if hasattr(a.item_type, 'value') else str(a.item_type),
            item_id=str(a.item_id),
            assignee_user_id=str(a.assignee_user_id),
            assigned_by_user_id=str(a.assigned_by_user_id) if a.assigned_by_user_id else None,
            assigned_at=a.assigned_at,
            unassigned_at=a.unassigned_at,
            assignee=_get_user_ref(session, a.assignee_user_id),
            assigned_by=_get_user_ref(session, a.assigned_by_user_id),
        ))
    
    return AssignmentsListResponse(assignments=results)


@router.post("/assignments", response_model=AssignmentResponse)
def create_assignment(
    data: AssignmentCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new assignment"""
    # Validate assignee belongs to tenant
    assignee = session.get(AppUser, UUID(data.assignee_user_id))
    if not assignee or str(assignee.tenant_id) != ctx.tenant_id:
        raise HTTPException(400, "User not found or not in tenant")
    
    # Validate item exists and belongs to tenant
    item_type = data.item_type
    item_id = UUID(data.item_id)
    
    if item_type == "lab_order":
        item = session.get(Order, item_id)
        if not item or str(item.tenant_id) != ctx.tenant_id:
            raise HTTPException(404, "Order not found or not in tenant")
    elif item_type == "sample":
        item = session.get(Sample, item_id)
        if not item or str(item.tenant_id) != ctx.tenant_id:
            raise HTTPException(404, "Sample not found or not in tenant")
    elif item_type == "report":
        item = session.get(Report, item_id)
        if not item or str(item.tenant_id) != ctx.tenant_id:
            raise HTTPException(404, "Report not found or not in tenant")
    else:
        raise HTTPException(400, f"Invalid item_type: {item_type}")
    
    # Check for existing active assignment
    existing = session.exec(
        select(Assignment).where(
            and_(
                Assignment.tenant_id == UUID(ctx.tenant_id),
                cast(Assignment.item_type, String) == item_type,
                Assignment.item_id == item_id,
                Assignment.assignee_user_id == UUID(data.assignee_user_id),
                Assignment.unassigned_at.is_(None),
            )
        )
    ).first()
    
    if existing:
        raise HTTPException(400, "Assignment already exists")
    
    assignment = Assignment(
        tenant_id=UUID(ctx.tenant_id),
        item_type=AssignmentItemType(item_type),
        item_id=item_id,
        assignee_user_id=UUID(data.assignee_user_id),
        assigned_by_user_id=user.id,
    )
    
    session.add(assignment)
    session.commit()
    session.refresh(assignment)
    
    # Safely get item_type value (handle enum after refresh)
    item_type_value = assignment.item_type.value if hasattr(assignment.item_type, 'value') else str(assignment.item_type)
    
    return AssignmentResponse(
        id=str(assignment.id),
        tenant_id=str(assignment.tenant_id),
        item_type=item_type_value,
        item_id=str(assignment.item_id),
        assignee_user_id=str(assignment.assignee_user_id),
        assigned_by_user_id=str(assignment.assigned_by_user_id) if assignment.assigned_by_user_id else None,
        assigned_at=assignment.assigned_at,
        unassigned_at=assignment.unassigned_at,
        assignee=_user_to_ref(assignee),
        assigned_by=_user_to_ref(user),
    )


@router.delete("/assignments/{assignment_id}")
def delete_assignment(
    assignment_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Soft delete an assignment (set unassigned_at)"""
    assignment = session.get(Assignment, UUID(assignment_id))
    
    if not assignment:
        raise HTTPException(404, "Assignment not found")
    
    if str(assignment.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Assignment does not belong to your tenant")
    
    if assignment.unassigned_at:
        raise HTTPException(400, "Assignment already unassigned")
    
    assignment.unassigned_at = datetime.utcnow()
    session.add(assignment)
    session.commit()
    
    return {"message": "Assignment removed", "id": assignment_id}


# === Report Review Endpoints ===

@router.get("/report-reviews", response_model=ReportReviewsListResponse)
def list_report_reviews(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    order_id: Optional[str] = Query(None),
    reviewer_user_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    """List report reviews with filters"""
    query = select(ReportReview).where(ReportReview.tenant_id == UUID(ctx.tenant_id))
    
    if order_id:
        query = query.where(ReportReview.order_id == UUID(order_id))
    
    if reviewer_user_id:
        query = query.where(ReportReview.reviewer_user_id == UUID(reviewer_user_id))
    
    if status:
        query = query.where(ReportReview.status == status.upper())
    
    query = query.order_by(ReportReview.assigned_at.desc())
    
    reviews = session.exec(query).all()
    
    results = []
    for r in reviews:
        results.append(ReportReviewResponse(
            id=str(r.id),
            tenant_id=str(r.tenant_id),
            order_id=str(r.order_id),
            reviewer_user_id=str(r.reviewer_user_id),
            assigned_by_user_id=str(r.assigned_by_user_id) if r.assigned_by_user_id else None,
            assigned_at=r.assigned_at,
            decision_at=r.decision_at,
            status=r.status.value if hasattr(r.status, 'value') else str(r.status),
            reviewer=_get_user_ref(session, r.reviewer_user_id),
            assigned_by=_get_user_ref(session, r.assigned_by_user_id),
        ))
    
    return ReportReviewsListResponse(reviews=results)


@router.post("/report-reviews/{review_id}/decision", response_model=ReportReviewResponse)
def make_review_decision(
    review_id: str,
    data: ReportReviewDecision,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """
    Make a decision on a report review (approve/reject).
    
    Allows changing decisions. Updates report status based on MVP rule: ≥1 approved = report approved.
    Creates timeline events and order comments for the decision.
    """
    review = session.get(ReportReview, UUID(review_id))
    
    if not review:
        raise HTTPException(404, "Review not found")
    
    if str(review.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Review does not belong to your tenant")
    
    # Only the assigned reviewer can make the decision
    if review.reviewer_user_id != user.id:
        raise HTTPException(403, "Only the assigned reviewer can make this decision")
    
    # Get report to create events and check status (query by order_id)
    report = session.exec(
        select(Report).where(Report.order_id == review.order_id)
    ).first()
    if not report:
        raise HTTPException(404, "Report not found for this order")
    
    # Track previous status for change detection
    previous_status = review.status
    new_status = ReviewStatus.APPROVED if data.decision == "approved" else ReviewStatus.REJECTED
    
    # Update review
    review.status = new_status
    review.decision_at = datetime.utcnow()
    session.add(review)
    
    # Create audit log for review decision
    from app.api.v1.reports import _create_audit_log
    _create_audit_log(
        session=session,
        tenant_id=str(review.tenant_id),
        branch_id=str(report.branch_id),
        actor_user_id=user.id,
        action=f"REVIEW.{new_status.value}",
        entity_type="report_review",
        entity_id=str(review.id),
        old_values={"status": previous_status.value if hasattr(previous_status, 'value') else str(previous_status)},
        new_values={"status": new_status.value, "comment": data.comment},
    )
    
    # Update report status based on review decisions
    if new_status == ReviewStatus.APPROVED:
        # If approved and report is in review, mark as approved (MVP: ≥1 approved)
        if report.status == ReportStatus.IN_REVIEW:
            report.status = ReportStatus.APPROVED
            session.add(report)
            
            # Create timeline event for approval
            event = OrderEvent(
                tenant_id=report.tenant_id,
                branch_id=report.branch_id,
                order_id=report.order_id,
                event_type=EventType.REPORT_APPROVED,
                description="",
                event_metadata={
                    "report_id": str(report.id),
                    "reviewer_id": str(user.id),
                    "reviewer_name": user.full_name or user.username,
                    "previous_review_status": previous_status.value if hasattr(previous_status, 'value') else str(previous_status),
                },
                created_by=user.id,
            )
            session.add(event)
    elif new_status == ReviewStatus.REJECTED:
        # If rejected, check if we need to revert report status
        if previous_status == ReviewStatus.APPROVED:
            # Check if there are other approved reviews
            other_approved = session.exec(
                select(ReportReview).where(
                    and_(
                        ReportReview.order_id == review.order_id,
                        ReportReview.status == ReviewStatus.APPROVED,
                        ReportReview.id != review.id,
                    )
                )
            ).first()
            
            # If no other approvals, revert to IN_REVIEW
            if not other_approved and report.status == ReportStatus.APPROVED:
                report.status = ReportStatus.IN_REVIEW
                session.add(report)
        
        # Create timeline event for rejection/changes requested
        event = OrderEvent(
            tenant_id=report.tenant_id,
            branch_id=report.branch_id,
            order_id=report.order_id,
            event_type=EventType.REPORT_CHANGES_REQUESTED,
            description="",
            event_metadata={
                "report_id": str(report.id),
                "reviewer_id": str(user.id),
                "reviewer_name": user.full_name or user.username,
                "previous_review_status": previous_status.value if hasattr(previous_status, 'value') else str(previous_status),
            },
            created_by=user.id,
        )
        session.add(event)
    
    # Create order comment if comment text is provided
    if data.comment and data.comment.strip():
        order_comment = OrderComment(
            tenant_id=report.tenant_id,
            branch_id=report.branch_id,
            order_id=report.order_id,
            created_by=user.id,
            text=data.comment,
            comment_metadata={
                "source": "review_decision",
                "report_id": str(report.id),
                "review_id": str(review.id),
                "decision": data.decision,
            },
        )
        session.add(order_comment)
    
    session.commit()
    session.refresh(review)
    
    logger.info(
        f"Review decision made: {new_status.value} by {user.id} (previous: {previous_status.value if hasattr(previous_status, 'value') else previous_status})",
        extra={
            "event": "report.review.decision",
            "order_id": str(review.order_id),
            "reviewer_id": str(user.id),
            "decision": new_status.value,
        }
    )
    
    return ReportReviewResponse(
        id=str(review.id),
        tenant_id=str(review.tenant_id),
        order_id=str(review.order_id),
        reviewer_user_id=str(review.reviewer_user_id),
        assigned_by_user_id=str(review.assigned_by_user_id) if review.assigned_by_user_id else None,
        assigned_at=review.assigned_at,
        decision_at=review.decision_at,
        status=review.status.value,
        reviewer=_get_user_ref(session, review.reviewer_user_id),
        assigned_by=_get_user_ref(session, review.assigned_by_user_id),
    )
