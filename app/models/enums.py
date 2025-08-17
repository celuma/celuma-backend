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
