"""UsageService.get_user_metrics — the exact mixed fixture from
docs/celuma-1.3/phase-4-block-b/tenant-user-metrics-contract.md (Céluma 1.3,
Phase 4, Block B §27 of the block's own spec).

Seeds one tenant with every category the ratified counting contract has to
handle correctly at once (multi-role, roleless, inactive, physician-only,
pending invitation), plus a second tenant that must never leak into the
first tenant's counts.
"""
from datetime import datetime, timedelta

from app.models.invitation import UserInvitation
from app.services.usage import UsageService
from tests.http.factories import create_tenant, create_user


class TestTenantUserMetricsMixedFixture:
    def test_exact_counts_from_the_ratified_fixture(self, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")

        create_user(session, tenant_a, email="admin@a.test", roles=("admin",))
        create_user(
            session, tenant_a, email="pathologist@a.test", roles=("pathologist",)
        )
        create_user(
            session, tenant_a, email="physician@a.test", roles=("physician",)
        )

        inactive_viewer = create_user(
            session, tenant_a, email="viewer@a.test", roles=("viewer",)
        )
        inactive_viewer.is_active = False
        session.add(inactive_viewer)
        session.commit()

        create_user(
            session,
            tenant_a,
            email="multirole@a.test",
            roles=("physician", "reviewer"),
        )

        create_user(session, tenant_a, email="roleless@a.test", roles=())

        # A pending invitation is not an AppUser row at all — structurally
        # absent from every query below. Seeded anyway to prove its mere
        # existence does not perturb the counts.
        session.add(
            UserInvitation(
                tenant_id=tenant_a.id,
                email="invited@a.test",
                full_name="Invited Person",
                role_code="viewer",
                token="test-token-tenant-a",
                expires_at=datetime.utcnow() + timedelta(days=7),
            )
        )
        session.commit()

        create_user(session, tenant_b, email="admin@b.test", roles=("admin",))

        metrics_a = UsageService.get_user_metrics(session, tenant_a.id)

        # registered: admin, pathologist, physician, inactive viewer,
        # multirole, roleless — six AppUser rows. The invitation is not one.
        assert metrics_a.registered_users == 6
        # active_internal: admin + pathologist + multirole (has a
        # non-physician role too) = 3. Excluded: physician-only (external),
        # inactive viewer (inactive), roleless (no role at all).
        assert metrics_a.active_internal_users == 3
        # active_physician_portal: only the physician-only user. The
        # multirole physician+reviewer user is excluded here because it
        # also holds a non-physician role — it is counted as internal
        # instead, not both.
        assert metrics_a.active_physician_portal_users == 1

        metrics_b = UsageService.get_user_metrics(session, tenant_b.id)
        assert metrics_b.registered_users == 1
        assert metrics_b.active_internal_users == 1
        assert metrics_b.active_physician_portal_users == 0
