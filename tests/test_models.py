"""
Unit tests for Celuma API models
"""
from app.models.tenant import Tenant, Branch
from app.models.patient import Patient
from app.models.user import AppUser
from app.models.role import Role
from app.models.permission import Permission


class TestTenantModel:
    """Test Tenant model functionality"""

    def test_tenant_creation(self):
        tenant_data = {
            "name": "Test Tenant",
            "legal_name": "Test Legal Name",
            "tax_id": "123456789",
        }
        tenant = Tenant(**tenant_data)
        assert tenant.name == tenant_data["name"]
        assert tenant.legal_name == tenant_data["legal_name"]
        assert tenant.tax_id == tenant_data["tax_id"]
        assert tenant.id is not None

    def test_tenant_string_representation(self):
        tenant = Tenant(name="Test Tenant", legal_name="Test Legal", tax_id="123")
        assert "Test Tenant" in str(tenant)


class TestBranchModel:
    """Test Branch model functionality"""

    def test_branch_creation(self):
        branch_data = {
            "tenant_id": "test-tenant-id",
            "code": "TEST",
            "name": "Test Branch",
            "city": "Test City",
            "state": "TS",
            "country": "MX",
        }
        branch = Branch(**branch_data)
        assert branch.tenant_id == branch_data["tenant_id"]
        assert branch.code == branch_data["code"]
        assert branch.name == branch_data["name"]
        assert branch.city == branch_data["city"]
        assert branch.id is not None

    def test_branch_string_representation(self):
        branch = Branch(tenant_id="test", code="TEST", name="Test Branch", city="City")
        assert "TEST" in str(branch)
        assert "Test Branch" in str(branch)


class TestPatientModel:
    """Test Patient model functionality"""

    def test_patient_creation(self):
        patient_data = {
            "tenant_id": "test-tenant-id",
            "branch_id": "test-branch-id",
            "first_name": "John",
            "last_name": "Doe",
            "email": "john.doe@example.com",
        }
        patient = Patient(**patient_data)
        assert patient.tenant_id == patient_data["tenant_id"]
        assert patient.branch_id == patient_data["branch_id"]
        assert patient.first_name == patient_data["first_name"]
        assert patient.last_name == patient_data["last_name"]
        assert patient.email == patient_data["email"]
        assert patient.id is not None

    def test_patient_full_name(self):
        patient = Patient(tenant_id="test", branch_id="test", first_name="John", last_name="Doe")
        assert patient.first_name == "John"
        assert patient.last_name == "Doe"

    def test_patient_string_representation(self):
        patient = Patient(tenant_id="test", branch_id="test", first_name="John", last_name="Doe")
        assert "John" in str(patient)
        assert "Doe" in str(patient)


class TestUserModel:
    """Test AppUser model functionality (no longer has a role field)"""

    def test_user_creation(self):
        user_data = {
            "email": "test@example.com",
            "hashed_password": "hashed_password_123",
            "full_name": "Test User",
            "first_name": "Test",
            "last_name": "User",
        }
        user = AppUser(**user_data)
        assert user.email == user_data["email"]
        assert user.hashed_password == user_data["hashed_password"]
        assert user.full_name == user_data["full_name"]
        assert user.is_active is True
        assert user.id is not None

    def test_user_string_representation(self):
        user = AppUser(
            email="test@example.com",
            hashed_password="hashed_password_123",
            full_name="Test User",
            first_name="Test",
            last_name="User",
        )
        assert "test@example.com" in str(user)


class TestRBACModels:
    """Test RBAC model creation"""

    def test_permission_creation(self):
        perm = Permission(
            code="reports:sign",
            domain="reports",
            display_name="Firmar reportes",
            description="Firma y publica informes aprobados",
        )
        assert perm.code == "reports:sign"
        assert perm.domain == "reports"
        assert perm.id is not None

    def test_role_creation(self):
        role = Role(
            code="pathologist",
            name="Patólogo",
            description="Diagnóstico clínico",
            is_system=True,
            is_protected=False,
        )
        assert role.code == "pathologist"
        assert role.is_system is True
        assert role.id is not None
