"""
Unit tests for Celuma API Pydantic schemas
"""
from app.schemas.tenant import TenantCreate, TenantResponse, TenantDetailResponse, BranchCreate, BranchResponse
from app.schemas.patient import PatientCreate, PatientResponse
from app.schemas.user import UserDetailResponse
from app.schemas.auth import UserRegister, UserLogin, UserResponse

class TestTenantSchemas:
    """Test Tenant schema validation"""
    
    def test_tenant_create_valid(self):
        """Test creating tenant with valid data"""
        tenant_data = {
            "name": "Test Tenant",
            "legal_name": "Test Legal Name",
            "tax_id": "123456789"
        }
        tenant = TenantCreate(**tenant_data)
        assert tenant.name == tenant_data["name"]
        assert tenant.legal_name == tenant_data["legal_name"]
        assert tenant.tax_id == tenant_data["tax_id"]
    
    def test_tenant_response_serialization(self):
        """Test tenant response serialization"""
        tenant_response = TenantResponse(
            id="test-id",
            name="Test Tenant",
            legal_name="Test Legal Name",
            tax_id="123456789"
        )
        assert tenant_response.id == "test-id"
        assert tenant_response.name == "Test Tenant"

    def test_tenant_response_reports_v2_enabled_defaults_false(self):
        """Céluma 1.3 Phase 2, Block A / Story A6."""
        tenant_response = TenantResponse(id="test-id", name="Test Tenant")
        assert tenant_response.reports_v2_enabled is False

    def test_tenant_response_reports_v2_enabled_explicit_true(self):
        tenant_response = TenantResponse(id="test-id", name="Test Tenant", reports_v2_enabled=True)
        assert tenant_response.reports_v2_enabled is True

    def test_tenant_detail_response_reports_v2_enabled_defaults_false(self):
        detail = TenantDetailResponse(id="test-id", name="Test Tenant")
        assert detail.reports_v2_enabled is False

class TestBranchSchemas:
    """Test Branch schema validation"""
    
    def test_branch_create_valid(self):
        """Test creating branch with valid data"""
        branch_data = {
            "tenant_id": "test-tenant-id",
            "code": "TEST",
            "name": "Test Branch",
            "city": "Test City",
            "state": "TS",
            "country": "MX"
        }
        branch = BranchCreate(**branch_data)
        assert branch.tenant_id == branch_data["tenant_id"]
        assert branch.code == branch_data["code"]
        assert branch.name == branch_data["name"]
    
    def test_branch_response_serialization(self):
        """Test branch response serialization"""
        branch_response = BranchResponse(
            id="test-id",
            tenant_id="test-tenant-id",
            code="TEST",
            name="Test Branch",
            city="Test City",
            state="TS",
            country="MX"
        )
        assert branch_response.id == "test-id"
        assert branch_response.name == "Test Branch"

class TestPatientSchemas:
    """Test Patient schema validation"""
    
    def test_patient_create_valid(self):
        """Test creating patient with valid data"""
        patient_data = {
            "tenant_id": "test-tenant-id",
            "branch_id": "test-branch-id",
            "patient_code": "PAT001",
            "first_name": "John",
            "last_name": "Doe",
            "email": "john.doe@example.com"
        }
        patient = PatientCreate(**patient_data)
        assert patient.tenant_id == patient_data["tenant_id"]
        assert patient.branch_id == patient_data["branch_id"]
        assert patient.patient_code == patient_data["patient_code"]
        assert patient.first_name == patient_data["first_name"]
        assert patient.last_name == patient_data["last_name"]

    def test_patient_create_without_code(self):
        """Test creating patient without explicit patient_code"""
        patient_data = {
            "tenant_id": "test-tenant-id",
            "branch_id": "test-branch-id",
            "first_name": "John",
            "last_name": "Doe",
        }
        patient = PatientCreate(**patient_data)
        assert patient.patient_code is None
    
    def test_patient_response_serialization(self):
        """Test patient response serialization"""
        patient_response = PatientResponse(
            id="test-id",
            tenant_id="test-tenant-id",
            branch_id="test-branch-id",
            patient_code="PAT001",
            first_name="John",
            last_name="Doe",
            email="john.doe@example.com"
        )
        assert patient_response.id == "test-id"
        assert patient_response.first_name == "John"

class TestUserSchemas:
    """Test User schema validation"""
    
    def test_user_register_valid(self):
        """Test creating user with valid data"""
        user_data = {
            "email": "test@example.com",
            "password": "securepassword123",
            "full_name": "Test User",
            "role": "admin",
            "tenant_id": "test-tenant-id"
        }
        user = UserRegister(**user_data)
        assert user.email == user_data["email"]
        assert user.password == user_data["password"]
        assert user.full_name == user_data["full_name"]
    
    def test_user_register_with_username(self):
        """Test creating user with username field"""
        user_data = {
            "email": "test@example.com",
            "username": "testuser",
            "password": "securepassword123",
            "full_name": "Test User",
            "role": "admin",
            "tenant_id": "test-tenant-id"
        }
        user = UserRegister(**user_data)
        assert user.email == user_data["email"]
        assert user.username == user_data["username"]
        assert user.password == user_data["password"]
        assert user.full_name == user_data["full_name"]
    
    def test_user_login_valid(self):
        """Test user login with valid data"""
        login_data = {
            "username_or_email": "test@example.com",
            "password": "password123",
            "tenant_id": "test-tenant-id"
        }
        login = UserLogin(**login_data)
        assert login.username_or_email == login_data["username_or_email"]
        assert login.password == login_data["password"]
    
    def test_user_response_serialization(self):
        """Test user response serialization"""
        user_response = UserResponse(
            id="test-id",
            email="test@example.com",
            full_name="Test User",
            role="admin"
        )
        assert user_response.id == "test-id"
        assert user_response.email == "test@example.com"
        assert user_response.full_name == "Test User"


class TestNotificationSchemas:
    """Céluma 1.3 Phase 3, Block B."""

    def _valid_command_kwargs(self):
        import uuid

        from app.models.notification import NotificationResourceType, NotificationType

        return {
            "tenant_id": uuid.uuid4(),
            "type": NotificationType.REPORT_SUBMITTED,
            "resource_type": NotificationResourceType.REPORT,
            "resource_id": uuid.uuid4(),
            "occurrence_marker": "order-event-1",
            "template_key": "report_submitted_v1",
            "template_params": {"order_number": "ORD-1", "actor_name": "Dra. M"},
        }

    def test_command_valid(self):
        from app.schemas.notification import NotificationCommand

        command = NotificationCommand(**self._valid_command_kwargs())
        assert command.recipient_user_ids == []
        assert command.exclude_actor is True
        assert command.created_by is None

    def test_command_rejects_an_unknown_template_key(self):
        import pytest

        from app.schemas.notification import NotificationCommand

        kwargs = self._valid_command_kwargs()
        kwargs["template_key"] = "made_up_v1"
        with pytest.raises(ValueError):
            NotificationCommand(**kwargs)

    def test_command_rejects_an_unknown_resource_type(self):
        import pytest

        from app.schemas.notification import NotificationCommand

        kwargs = self._valid_command_kwargs()
        kwargs["resource_type"] = "invoice"
        with pytest.raises(ValueError):
            NotificationCommand(**kwargs)

    def test_command_rejects_a_blank_occurrence_marker(self):
        import pytest

        from app.schemas.notification import NotificationCommand

        for blank in ("", "   ", "\n"):
            kwargs = self._valid_command_kwargs()
            kwargs["occurrence_marker"] = blank
            with pytest.raises(ValueError):
                NotificationCommand(**kwargs)

    def test_list_response_serializes_both_ids_separately(self):
        import uuid
        from datetime import datetime

        from app.schemas.notification import (
            NotificationListItem,
            NotificationListResponse,
        )

        item = NotificationListItem(
            recipient_id=str(uuid.uuid4()),
            notification_id=str(uuid.uuid4()),
            type="REPORT_SUBMITTED",
            severity="INFO",
            title="Reporte listo para revisión — Orden ORD-1",
            body=None,
            resource_type="report",
            resource_id=str(uuid.uuid4()),
            status="UNREAD",
            created_at=datetime.utcnow(),
        )
        dumped = NotificationListResponse(items=[item]).model_dump()

        assert "id" not in dumped["items"][0]
        assert dumped["items"][0]["recipient_id"] != dumped["items"][0]["notification_id"]
        assert dumped["next_cursor"] is None

    def test_count_and_read_responses(self):
        import uuid

        from app.schemas.notification import (
            NotificationReadAllResponse,
            NotificationRecipientReadResponse,
            NotificationUnreadCountResponse,
        )

        assert NotificationUnreadCountResponse(unread_count=4).model_dump() == {
            "unread_count": 4
        }
        assert NotificationReadAllResponse(updated_count=12).model_dump() == {
            "updated_count": 12
        }
        read = NotificationRecipientReadResponse(
            recipient_id=str(uuid.uuid4()), status="READ", read_at=None
        )
        assert set(read.model_dump()) == {"recipient_id", "status", "read_at"}
