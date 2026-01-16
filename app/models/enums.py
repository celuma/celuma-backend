from enum import Enum

class UserRole(str, Enum):
    ADMIN = "admin"
    PATHOLOGIST = "pathologist"
    LAB_TECH = "lab_tech"
    ASSISTANT = "assistant"
    BILLING = "billing"
    VIEWER = "viewer"

class OrderStatus(str, Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    DIAGNOSIS = "DIAGNOSIS"
    REVIEW = "REVIEW"
    RELEASED = "RELEASED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

class SampleType(str, Enum):
    SANGRE = "SANGRE"
    BIOPSIA = "BIOPSIA"
    LAMINILLA = "LAMINILLA"
    TEJIDO = "TEJIDO"
    OTRO = "OTRO"

class SampleState(str, Enum):
    """Sample-specific states: Received -> Processing -> Ready (or Damaged/Cancelled)"""
    RECEIVED = "RECEIVED"      # Recibida - default when created
    PROCESSING = "PROCESSING"  # En Proceso - when images uploaded
    READY = "READY"            # Lista - manually set or when complete
    DAMAGED = "DAMAGED"        # Dañada - sample physically damaged
    CANCELLED = "CANCELLED"    # Cancelada - cannot be used for other reasons

class ReportStatus(str, Enum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    RETRACTED = "RETRACTED"

class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    PARTIAL = "PARTIAL"

class EventType(str, Enum):
    # Order events
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_DELIVERED = "ORDER_DELIVERED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_STATUS_CHANGED = "ORDER_STATUS_CHANGED"
    
    # Sample events
    SAMPLE_CREATED = "SAMPLE_CREATED"
    SAMPLE_RECEIVED = "SAMPLE_RECEIVED"
    SAMPLE_PREPARED = "SAMPLE_PREPARED"
    SAMPLE_STATE_CHANGED = "SAMPLE_STATE_CHANGED"
    SAMPLE_DAMAGED = "SAMPLE_DAMAGED"
    SAMPLE_CANCELLED = "SAMPLE_CANCELLED"
    
    # Image events
    IMAGE_UPLOADED = "IMAGE_UPLOADED"
    IMAGE_DELETED = "IMAGE_DELETED"
    
    # Report events
    REPORT_CREATED = "REPORT_CREATED"
    REPORT_VERSION_CREATED = "REPORT_VERSION_CREATED"
    REPORT_SUBMITTED = "REPORT_SUBMITTED"
    REPORT_APPROVED = "REPORT_APPROVED"
    REPORT_CHANGES_REQUESTED = "REPORT_CHANGES_REQUESTED"
    REPORT_SIGNED = "REPORT_SIGNED"
    REPORT_PUBLISHED = "REPORT_PUBLISHED"
    REPORT_RETRACTED = "REPORT_RETRACTED"
    
    # Billing events
    INVOICE_CREATED = "INVOICE_CREATED"
    PAYMENT_RECEIVED = "PAYMENT_RECEIVED"
    
    # Generic events
    STATUS_CHANGED = "STATUS_CHANGED"  # Legacy/generic
    NOTE_ADDED = "NOTE_ADDED"
    COMMENT_ADDED = "COMMENT_ADDED"
