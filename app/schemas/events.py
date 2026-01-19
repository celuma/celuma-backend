from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime

class EventCreate(BaseModel):
    """Schema for creating an event"""
    event_type: str
    description: str
    metadata: Optional[Dict[str, Any]] = None

class EventResponse(BaseModel):
    """Schema for event response"""
    id: str
    tenant_id: str
    branch_id: str
    order_id: str
    sample_id: Optional[str] = None  # For sample-specific events
    event_type: str
    description: str
    metadata: Optional[Dict[str, Any]] = None
    created_by: Optional[str] = None
    created_by_name: Optional[str] = None  # User's full name for display
    created_by_avatar: Optional[str] = None  # User's avatar URL for display
    created_at: datetime

class EventsListResponse(BaseModel):
    """Response schema for events list"""
    events: List[EventResponse]

