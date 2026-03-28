"""
RBAC Phase 2 smoke tests.

Verifies that `has_permission` returns False for a user with no roles (→ 403 in endpoints)
and True after assigning the appropriate role (→ 200/201 in endpoints).

Uses an in-memory SQLite database seeded with the minimum required data
(permissions, roles, role_permission links) to exercise the RBAC core without
hitting a real Postgres instance.
"""
import uuid
import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.models.user_role import UserRoleLink
from app.core.rbac import has_permission, assign_role_by_code, get_user_roles, replace_user_roles


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with all RBAC tables created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed_permission(session: Session, code: str, domain: str) -> Permission:
    perm = Permission(
        id=uuid.uuid4(),
        code=code,
        domain=domain,
        display_name=code,
        description="",
    )
    session.add(perm)
    session.flush()
    return perm


def _seed_role(session: Session, code: str, name: str) -> Role:
    role = Role(
        id=uuid.uuid4(),
        code=code,
        name=name,
        description="",
        is_system=True,
        is_protected=True,
    )
    session.add(role)
    session.flush()
    return role


def _link_role_permission(session: Session, role: Role, perm: Permission) -> None:
    rp = RolePermission(role_id=role.id, permission_id=perm.id)
    session.add(rp)
    session.flush()


# ---------------------------------------------------------------------------
# Core RBAC unit tests
# ---------------------------------------------------------------------------

class TestHasPermission:
    """Verify basic permission resolution without endpoints."""

    def test_user_without_roles_has_no_permissions(self, session: Session):
        user_id = uuid.uuid4()
        perm = _seed_permission(session, "lab:read", "lab")
        _seed_role(session, "lab_tech", "Lab Tech")
        assert not has_permission(user_id, "lab:read", session)

    def test_user_with_correct_role_has_permission(self, session: Session):
        user_id = uuid.uuid4()
        perm = _seed_permission(session, "lab:read", "lab")
        role = _seed_role(session, "lab_tech", "Lab Tech")
        _link_role_permission(session, role, perm)

        link = UserRoleLink(user_id=user_id, role_id=role.id)
        session.add(link)
        session.flush()

        assert has_permission(user_id, "lab:read", session)

    def test_user_lacks_unassigned_permission(self, session: Session):
        user_id = uuid.uuid4()
        perm_read = _seed_permission(session, "lab:read", "lab")
        perm_write = _seed_permission(session, "lab:create_order", "lab")
        role = _seed_role(session, "viewer", "Viewer")
        _link_role_permission(session, role, perm_read)

        link = UserRoleLink(user_id=user_id, role_id=role.id)
        session.add(link)
        session.flush()

        assert has_permission(user_id, "lab:read", session)
        assert not has_permission(user_id, "lab:create_order", session)

    def test_multiple_roles_union_permissions(self, session: Session):
        user_id = uuid.uuid4()
        perm_lab = _seed_permission(session, "lab:read", "lab")
        perm_billing = _seed_permission(session, "billing:read", "billing")
        role_lab = _seed_role(session, "lab_tech", "Lab Tech")
        role_billing = _seed_role(session, "billing", "Billing")
        _link_role_permission(session, role_lab, perm_lab)
        _link_role_permission(session, role_billing, perm_billing)

        session.add(UserRoleLink(user_id=user_id, role_id=role_lab.id))
        session.add(UserRoleLink(user_id=user_id, role_id=role_billing.id))
        session.flush()

        assert has_permission(user_id, "lab:read", session)
        assert has_permission(user_id, "billing:read", session)
        assert not has_permission(user_id, "reports:sign", session)


class TestAssignRoleByCode:
    """Verify role assignment helper."""

    def test_assign_role_grants_permissions(self, session: Session):
        user_id = uuid.uuid4()
        perm = _seed_permission(session, "reports:create", "reports")
        role = _seed_role(session, "pathologist", "Pathologist")
        _link_role_permission(session, role, perm)
        session.commit()

        assert not has_permission(user_id, "reports:create", session)
        assign_role_by_code(user_id, "pathologist", session)
        session.commit()
        assert has_permission(user_id, "reports:create", session)

    def test_assign_nonexistent_role_raises(self, session: Session):
        user_id = uuid.uuid4()
        with pytest.raises(ValueError, match="Role 'nonexistent' not found"):
            assign_role_by_code(user_id, "nonexistent", session)

    def test_idempotent_assignment(self, session: Session):
        user_id = uuid.uuid4()
        _seed_permission(session, "admin:manage_users", "admin")
        role = _seed_role(session, "admin", "Admin")
        session.commit()

        assign_role_by_code(user_id, "admin", session)
        session.commit()
        assign_role_by_code(user_id, "admin", session)
        session.commit()

        roles = get_user_roles(user_id, session)
        assert roles.count("admin") == 1


# ---------------------------------------------------------------------------
# replace_user_roles regression tests
# ---------------------------------------------------------------------------

class TestReplaceUserRoles:
    """Regression tests for the flush-before-insert fix in replace_user_roles."""

    def test_replace_with_same_role_no_integrity_error(self, session: Session):
        """Replacing roles when the new list contains the current role must not raise UniqueViolation."""
        user_id = uuid.uuid4()
        _seed_permission(session, "lab:read", "lab")
        role = _seed_role(session, "lab_tech", "Lab Tech")
        session.add(UserRoleLink(user_id=user_id, role_id=role.id))
        session.commit()

        # Replace with the same role — triggered the UniqueViolation before the fix
        replace_user_roles(user_id, ["lab_tech"], session)
        session.commit()

        roles = get_user_roles(user_id, session)
        assert roles == ["lab_tech"]

    def test_replace_changes_role(self, session: Session):
        """Replacing one role with a different one results in only the new role assigned."""
        user_id = uuid.uuid4()
        _seed_permission(session, "lab:read", "lab")
        _seed_permission(session, "billing:read", "billing")
        role_a = _seed_role(session, "lab_tech_r", "Lab Tech")
        role_b = _seed_role(session, "billing_r", "Billing")
        session.add(UserRoleLink(user_id=user_id, role_id=role_a.id))
        session.commit()

        replace_user_roles(user_id, ["billing_r"], session)
        session.commit()

        roles = get_user_roles(user_id, session)
        assert roles == ["billing_r"]

    def test_replace_multiple_roles(self, session: Session):
        """replace_user_roles assigns multiple roles correctly."""
        user_id = uuid.uuid4()
        _seed_permission(session, "lab:read", "lab")
        _seed_permission(session, "reports:read", "reports")
        role_a = _seed_role(session, "lab_tech_m", "Lab Tech")
        role_b = _seed_role(session, "viewer_m", "Viewer")
        session.commit()

        replace_user_roles(user_id, ["lab_tech_m", "viewer_m"], session)
        session.commit()

        roles = sorted(get_user_roles(user_id, session))
        assert roles == ["lab_tech_m", "viewer_m"]

    def test_replace_dedupe_duplicate_codes(self, session: Session):
        """Duplicate role codes in the input must not cause a UniqueViolation on insert."""
        user_id = uuid.uuid4()
        _seed_permission(session, "lab:read", "lab")
        role = _seed_role(session, "lab_tech_d", "Lab Tech")
        session.commit()

        # Pass the same role code twice — should result in exactly one assignment
        replace_user_roles(user_id, ["lab_tech_d", "lab_tech_d"], session)
        session.commit()

        roles = get_user_roles(user_id, session)
        assert roles.count("lab_tech_d") == 1

    def test_replace_unknown_role_raises(self, session: Session):
        """replace_user_roles must raise ValueError for unknown role codes."""
        user_id = uuid.uuid4()
        with pytest.raises(ValueError, match="Role.s. not found"):
            replace_user_roles(user_id, ["nonexistent_xyz"], session)

    def test_roundtrip_role_switch(self, session: Session):
        """Switch role A → B → A without errors, verifying state at each step."""
        user_id = uuid.uuid4()
        _seed_permission(session, "lab:read", "lab")
        _seed_permission(session, "reports:read", "reports")
        role_a = _seed_role(session, "role_a_rt", "Role A")
        role_b = _seed_role(session, "role_b_rt", "Role B")
        session.add(UserRoleLink(user_id=user_id, role_id=role_a.id))
        session.commit()

        replace_user_roles(user_id, ["role_b_rt"], session)
        session.commit()
        assert get_user_roles(user_id, session) == ["role_b_rt"]

        replace_user_roles(user_id, ["role_a_rt"], session)
        session.commit()
        assert get_user_roles(user_id, session) == ["role_a_rt"]


# ---------------------------------------------------------------------------
# Per-domain permission smoke tests
# ---------------------------------------------------------------------------

DOMAIN_PERMISSIONS = [
    ("admin", "admin:manage_users"),
    ("admin", "admin:manage_tenant"),
    ("admin", "admin:manage_branches"),
    ("admin", "admin:manage_catalog"),
    ("lab", "lab:read"),
    ("lab", "lab:create_order"),
    ("lab", "lab:update_order"),
    ("lab", "lab:create_sample"),
    ("lab", "lab:update_sample"),
    ("lab", "lab:manage_assignees"),
    ("lab", "lab:manage_reviewers"),
    ("lab", "lab:manage_comments"),
    ("lab", "lab:manage_labels"),
    ("lab", "lab:upload_images"),
    ("lab", "lab:delete_images"),
    ("lab", "lab:create_patient"),
    ("reports", "reports:read"),
    ("reports", "reports:create"),
    ("reports", "reports:edit"),
    ("reports", "reports:submit"),
    ("reports", "reports:approve"),
    ("reports", "reports:sign"),
    ("reports", "reports:retract"),
    ("reports", "reports:manage_templates"),
    ("billing", "billing:read"),
    ("billing", "billing:create_invoice"),
    ("billing", "billing:register_payment"),
    ("billing", "billing:edit_items"),
    ("portal", "portal:physician_access"),
]


@pytest.mark.parametrize("domain,perm_code", DOMAIN_PERMISSIONS)
def test_permission_granted_after_role_assignment(session: Session, domain: str, perm_code: str):
    """Smoke test: each known permission code is granted once the right role is assigned."""
    user_id = uuid.uuid4()
    perm = _seed_permission(session, perm_code, domain)
    role = _seed_role(session, f"role_{perm_code.replace(':', '_')}", f"Role for {perm_code}")
    _link_role_permission(session, role, perm)
    session.commit()

    assert not has_permission(user_id, perm_code, session)

    link = UserRoleLink(user_id=user_id, role_id=role.id)
    session.add(link)
    session.commit()

    assert has_permission(user_id, perm_code, session)
