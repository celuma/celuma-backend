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

    def test_tenant_reports_v2_enabled_defaults_to_false(self):
        """Céluma 1.3 Phase 2, Block A / Story A6: existing/new tenants
        must default to reports_v2_enabled=False unless explicitly set."""
        tenant = Tenant(name="Test Tenant")
        assert tenant.reports_v2_enabled is False

    def test_tenant_reports_v2_enabled_can_be_explicitly_enabled(self):
        tenant = Tenant(name="Test Tenant", reports_v2_enabled=True)
        assert tenant.reports_v2_enabled is True


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


class TestNotificationModels:
    """Céluma 1.3 Phase 3, Block B — in-memory field/default assertions.

    Constraint behaviour (uniqueness, CHECKs, nullability) is tested against
    the real migrated schema in tests/http/test_notification_models.py, since
    a constraint that exists only in a model definition guarantees nothing.
    """

    def _notification(self, **overrides):
        import uuid

        from app.models.notification import Notification, NotificationType

        values = {
            "tenant_id": uuid.uuid4(),
            "type": NotificationType.REPORT_SUBMITTED,
            "title": "Reporte listo para revisión — Orden ORD-1",
            "resource_type": "report",
            "resource_id": uuid.uuid4(),
            "idempotency_key": "REPORT_SUBMITTED:report:abc:marker",
        }
        values.update(overrides)
        return Notification(**values)

    def test_notification_defaults(self):
        from app.models.notification import NotificationSeverity

        notification = self._notification()
        assert notification.id is not None
        assert notification.severity == NotificationSeverity.INFO
        assert notification.body is None
        assert notification.created_by is None
        assert notification.notification_metadata is None
        assert notification.created_at is not None

    def test_notification_metadata_is_not_named_metadata(self):
        """`metadata` is reserved by SQLModel/SQLAlchemy on a declarative
        class. The codebase already resolves this the same way for
        `event_metadata`/`comment_metadata`."""
        from app.models.notification import Notification

        notification = self._notification(notification_metadata={"template_key": "x"})
        assert notification.notification_metadata == {"template_key": "x"}
        assert not isinstance(getattr(Notification, "metadata", None), dict)

    def test_recipient_defaults_to_unread(self):
        import uuid

        from app.models.notification import (
            NotificationRecipient,
            NotificationRecipientStatus,
        )

        recipient = NotificationRecipient(
            notification_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
        assert recipient.status == NotificationRecipientStatus.UNREAD
        assert recipient.read_at is None
        assert recipient.created_at is not None

    def test_delivery_defaults(self):
        import uuid

        from app.models.notification import (
            NotificationChannel,
            NotificationDelivery,
            NotificationDeliveryStatus,
        )

        delivery = NotificationDelivery(
            notification_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            recipient_address="destinatario@example.test",
            channel=NotificationChannel.EMAIL,
        )
        assert delivery.status == NotificationDeliveryStatus.PENDING
        assert delivery.attempts == 0
        assert delivery.recipient_user_id is None
        assert delivery.error_code is None

    def test_preference_defaults_to_both_channels_enabled(self):
        import uuid

        from app.models.notification import NotificationPreference, NotificationType

        preference = NotificationPreference(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            notification_type=NotificationType.REPORT_PUBLISHED,
        )
        assert preference.in_app_enabled is True
        assert preference.email_enabled is True

    def test_the_approved_types_and_nothing_speculative(self):
        """The six Phase 3 clinical events plus Céluma 1.3, Phase 4, Block G's
        four usage-threshold events — and nothing else.

        The point of the assertion is unchanged: no type may be declared
        before there is a real transition producing it, a template rendering
        it, a delivery policy for it and a recipient rule. Block G's four
        satisfy all four; there is deliberately no `STORAGE_USAGE_90` or any
        other per-percentage type, because the threshold percentages are
        policy constants in `app/services/usage_thresholds.py`, not identity.
        """
        from app.models.notification import NotificationType

        assert {t.value for t in NotificationType} == {
            "REPORT_SUBMITTED",
            "REPORT_PDF_READY",
            "REPORT_PUBLISHED",
            "REPORT_RETRACTED",
            "ASSIGNMENT_ADDED",
            "SAMPLE_STATUS_CHANGED",
            "STORAGE_USAGE_APPROACHING",
            "STORAGE_LIMIT_REACHED",
            "USER_LIMIT_APPROACHING",
            "USER_LIMIT_REACHED",
        }

    def test_email_is_the_only_declared_channel(self):
        """PUSH/SMS/WHATSAPP are deliberately absent until they are real."""
        from app.models.notification import NotificationChannel

        assert {c.value for c in NotificationChannel} == {"EMAIL"}

    def test_the_reserved_enum_values_are_modeled_completely(self):
        from app.models.notification import (
            NotificationDeliveryStatus,
            NotificationRecipientStatus,
            NotificationSeverity,
        )

        assert {s.value for s in NotificationSeverity} == {
            "INFO",
            "WARNING",
            "ACTION_REQUIRED",
        }
        assert {s.value for s in NotificationRecipientStatus} == {
            "UNREAD",
            "READ",
            "DISMISSED",
        }
        assert {s.value for s in NotificationDeliveryStatus} == {
            "PENDING",
            "SENDING",
            "SENT",
            "FAILED",
        }
