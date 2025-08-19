"""
Pytest configuration and fixtures for Celuma API unit tests
"""
import pytest
from unittest.mock import Mock, patch
from sqlmodel import Session, create_engine
from sqlalchemy.pool import StaticPool

# Mock database for unit tests
@pytest.fixture
def mock_session():
    """Create a mock database session for testing"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with Session(engine) as session:
        yield session

@pytest.fixture
def mock_current_user():
    """Mock authenticated user for testing"""
    user = Mock()
    user.id = "test-user-id"
    user.email = "test@example.com"
    user.role = "admin"
    user.is_active = True
    return user

@pytest.fixture
def mock_tenant_data():
    """Sample tenant data for testing"""
    return {
        "name": "Test Tenant",
        "legal_name": "Test Legal Name",
        "tax_id": "123456789"
    }

@pytest.fixture
def mock_branch_data():
    """Sample branch data for testing"""
    return {
        "tenant_id": "test-tenant-id",
        "code": "TEST",
        "name": "Test Branch",
        "city": "Test City",
        "state": "TS",
        "country": "MX"
    }

@pytest.fixture
def mock_patient_data():
    """Sample patient data for testing"""
    return {
        "tenant_id": "test-tenant-id",
        "branch_id": "test-branch-id",
        "code": "PAT001",
        "first_name": "John",
        "last_name": "Doe",
        "email": "john.doe@example.com",
        "phone": "+1234567890"
    }
