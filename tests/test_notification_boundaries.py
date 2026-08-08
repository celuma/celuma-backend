"""Architectural boundary tests (Céluma 1.3, Phase 3, Block F, §5).

The rule Block F exists to preserve while wiring six clinical triggers::

    clinical domain -> notification_integrations -> NotificationService

and never the reverse. `NotificationService` is the entry point every future
non-clinical event has to use too — Phase 4's storage alerts, user-limit
alerts and billing reminders all reuse it — so a report/order/sample import
inside it would put the clinical state machine in the way of events that have
nothing to do with clinical work.

These assertions run against **module source**, deliberately. A runtime check
would only catch an import that happened to execute; reading the source
catches one that merely exists, including inside a function body where it is
easiest to add without noticing.
"""
import ast
import pathlib

import pytest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVICES = BACKEND_ROOT / "app" / "services"
INTEGRATIONS = SERVICES / "notification_integrations"


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Every module name this file imports, at any nesting depth.

    `ast.walk` rather than a scan of top-level statements, so a deferred
    import inside a function — the codebase's own idiom for breaking cycles,
    and the easiest place to hide a boundary violation — is caught too.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


#: Modules that model the clinical domain. `NotificationService` may not know
#: any of them exist.
CLINICAL_MODULES = (
    "app.models.report",
    "app.models.laboratory",
    "app.models.events",
    "app.models.assignment",
    "app.models.report_review",
    "app.models.patient",
    "app.models.report_letterhead",
    "app.models.report_template_version",
    "app.services.report_publishing",
    "app.services.report_pdf_generation",
    "app.api.v1.reports",
    "app.api.v1.laboratory",
)


class TestNotificationServiceIsDomainAgnostic:
    """§5, the mandatory boundary."""

    @pytest.mark.parametrize("clinical", CLINICAL_MODULES)
    def test_the_service_imports_no_clinical_module(self, clinical):
        imported = _imported_modules(SERVICES / "notification.py")
        assert clinical not in imported, (
            f"app/services/notification.py imports {clinical} — the service must "
            "stay usable by Phase 4's non-clinical events"
        )

    @pytest.mark.parametrize(
        "module",
        [
            "notification.py",
            "notification_delivery.py",
            "notification_delivery_worker.py",
            "notification_templates.py",
            "notification_policies.py",
            "notification_preferences.py",
            "email_templates.py",
        ],
    )
    def test_no_notification_module_imports_a_clinical_module(self, module):
        """The whole notification stack, not just the service.

        A clinical import in the delivery worker or the template registry
        would be the same architectural mistake one layer over.
        """
        imported = _imported_modules(SERVICES / module)
        offenders = sorted(set(CLINICAL_MODULES) & imported)
        assert offenders == [], f"app/services/{module} imports {offenders}"

    def test_the_service_exposes_no_per_event_method(self):
        """§5 names these explicitly as forbidden shapes.

        `NotificationService.notify_report_published()` would move recipient
        resolution and clinical vocabulary inside the service, which is what
        the integration layer exists to hold instead.
        """
        source = (SERVICES / "notification.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        service = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "NotificationService"
        )
        method_names = {
            node.name for node in service.body if isinstance(node, ast.FunctionDef)
        }
        assert method_names == {"notify", "_notify"}, (
            f"NotificationService grew {sorted(method_names - {'notify', '_notify'})}"
        )

    def test_no_clinical_word_appears_in_the_service_source(self):
        """A blunt second net.

        The import check is the real one; this catches a string literal or a
        column name that reached in without an import — a raw SQL fragment
        naming `report_version`, for instance.
        """
        source = (SERVICES / "notification.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Strip the module docstring: the prose legitimately explains *why*
        # publishing a report must not fail on a notification error.
        lines = source.splitlines(keepends=True)
        body = "".join(lines[tree.body[0].end_lineno :]) if ast.get_docstring(tree) else source
        code_only = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        # Only identifiers that could actually reach the database.
        for token in ("ReportVersion", "OrderEvent", "report_version", "order_event", "Sample("):
            assert token not in code_only, f"{token} appears in NotificationService"


class TestIntegrationLayerStaysOutOfDelivery:
    """The other direction of the boundary (§7's "must not" list).

    The integration layer resolves and asks; it does not send, materialize,
    read preferences, or touch a provider. Each of those belongs to a layer
    that already owns it.
    """

    INTEGRATION_MODULES = ("__init__.py", "reports.py", "assignments.py", "samples.py", "recipients.py")

    @pytest.mark.parametrize("module", INTEGRATION_MODULES)
    def test_it_imports_no_delivery_or_provider_machinery(self, module):
        imported = _imported_modules(INTEGRATIONS / module)
        forbidden = {
            "app.services.notification_delivery",
            "app.services.notification_delivery_worker",
            "app.services.notification_preferences",
            "app.services.email_templates",
            "app.services.email_provider",
            "app.services.email_provider_ses",
            "app.services.email_provider_fake",
            "app.services.email_provider_factory",
            "app.services.email",
            "app.services.s3",
            "boto3",
            "smtplib",
        }
        offenders = sorted(forbidden & imported)
        assert offenders == [], (
            f"notification_integrations/{module} imports {offenders} — delivery is "
            "materialized by NotificationService, not by a domain integration"
        )

    @pytest.mark.parametrize("module", INTEGRATION_MODULES)
    def test_it_names_no_send_or_delivery_symbol(self, module):
        source = (INTEGRATIONS / module).read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        body = "".join(lines[tree.body[0].end_lineno :]) if ast.get_docstring(tree) else source
        code_only = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        for token in (
            "NotificationDelivery",
            "materialize_email_deliveries",
            "send_email",
            "render_notification_email",
            "get_effective_preference",
            "frontend_url",
        ):
            assert token not in code_only, (
                f"notification_integrations/{module} references {token}"
            )

    @pytest.mark.parametrize("module", INTEGRATION_MODULES)
    def test_it_defines_no_http_endpoint(self, module):
        source = (INTEGRATIONS / module).read_text(encoding="utf-8")
        assert "APIRouter" not in source
        assert "@router." not in source


class TestCallSitesUseOnlyTheIntegrationLayer:
    """The clinical endpoints reach notifications through one door.

    A `NotificationService.notify()` call inside `reports.py` would work, and
    would put recipient resolution and template keys back into the endpoint —
    which is exactly the distribution of logic Story F1 exists to prevent.
    """

    @pytest.mark.parametrize("module", ["reports.py", "laboratory.py"])
    def test_the_endpoint_module_does_not_import_the_service_directly(self, module):
        imported = _imported_modules(BACKEND_ROOT / "app" / "api" / "v1" / module)
        for forbidden in (
            "app.services.notification",
            "app.services.notification_delivery",
            "app.services.notification_templates",
            "app.services.notification_policies",
            "app.schemas.notification",
        ):
            assert forbidden not in imported, (
                f"app/api/v1/{module} imports {forbidden} directly — clinical code "
                "reaches notifications only through notification_integrations"
            )

    @pytest.mark.parametrize("module", ["reports.py", "laboratory.py"])
    def test_the_endpoint_module_imports_the_integration_layer(self, module):
        """The positive half: the triggers are actually wired."""
        imported = _imported_modules(BACKEND_ROOT / "app" / "api" / "v1" / module)
        assert "app.services.notification_integrations" in imported
