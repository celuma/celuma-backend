"""Localization-readiness tests (Céluma 1.3, Phase 3, Block F, Story F16).

No database, no network — these are about the registries and the locale
identifier, both of which are pure.

What is being asserted is **architecture, not translation**. Céluma 1.3 ships
one locale. The tests that matter are therefore the ones that would catch a
future locale being added carelessly: a key without a version suffix, a copy
change in place, a translated email that quietly grew a parameter, a locale
string that reaches a registry lookup without being validated.
"""
import pytest

from app.models.notification import NotificationType
from app.services import email_templates as email_registry
from app.services import notification_templates as inapp_registry
from app.services.email_templates import (
    EMAIL_TEMPLATE_REGISTRY,
    EmailTemplateError,
    get_email_template,
    render_notification_email,
)
from app.services.locale import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    InvalidLocaleError,
    is_supported,
    parse_locale,
    resolve_locale,
)
from app.services.notification_policies import (
    NOTIFICATION_DELIVERY_POLICIES,
    email_supported,
)
from app.services.notification_templates import (
    CURRENT_TEMPLATE_KEY,
    NOTIFICATION_TEMPLATE_REGISTRY,
    NotificationTemplateError,
    get_template,
)


# ---------------------------------------------------------------------------
# The locale identifier
# ---------------------------------------------------------------------------

class TestLocaleIdentifier:
    def test_the_default_locale_is_es_mx(self):
        assert DEFAULT_LOCALE == "es-MX"

    def test_the_default_is_the_only_supported_locale(self):
        """One entry, on purpose. An unused locale in a shipped registry is
        copy nobody reviewed."""
        assert SUPPORTED_LOCALES == frozenset({"es-MX"})
        assert is_supported(DEFAULT_LOCALE)

    @pytest.mark.parametrize("value", ["es-MX", "en-US", "pt-BR", "fr", "zh-Hant-TW", "es-419"])
    def test_well_formed_identifiers_parse(self, value):
        assert parse_locale(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "../../foo",
            "es_MX",
            "ES-mx",
            "es-MX; DROP TABLE notification",
            "es-MX/../en",
            "<script>",
            "",
            "   ",
            "e",
            "x" * 40,
        ],
    )
    def test_malformed_identifiers_are_rejected(self, value):
        """Rejected, not defaulted. A path-traversal-shaped value silently
        becoming `es-MX` would be indistinguishable from a legitimate request
        for Portuguese."""
        with pytest.raises(InvalidLocaleError):
            parse_locale(value)

    @pytest.mark.parametrize("value", [123, None, ["es-MX"], {"locale": "es-MX"}, True])
    def test_non_string_identifiers_are_rejected(self, value):
        with pytest.raises(InvalidLocaleError):
            parse_locale(value)

    def test_a_valid_but_unsupported_locale_falls_back_to_the_default(self):
        """The distinction Story F8 requires: `en-US` is a real locale Céluma
        cannot yet serve, so it falls back rather than failing."""
        assert resolve_locale("en-US") == DEFAULT_LOCALE
        assert resolve_locale("pt-BR") == DEFAULT_LOCALE

    def test_none_resolves_to_the_default(self):
        assert resolve_locale(None) == DEFAULT_LOCALE

    def test_an_invalid_locale_still_raises_through_resolve(self):
        """`resolve_locale` falls back; it does not launder."""
        with pytest.raises(InvalidLocaleError):
            resolve_locale("../../foo")

    def test_the_error_carries_a_code_and_not_the_value(self):
        """So it can be logged without echoing whatever was injected."""
        with pytest.raises(InvalidLocaleError) as exc:
            parse_locale("<script>alert(1)</script>")
        assert exc.value.code == "invalid_locale_format"
        assert "script" not in str(exc.value)


# ---------------------------------------------------------------------------
# Registry structure — Story F7, template version immutability
# ---------------------------------------------------------------------------

class TestTemplateVersioning:
    def test_every_in_app_key_carries_an_explicit_version_suffix(self):
        for key in NOTIFICATION_TEMPLATE_REGISTRY:
            assert key.rsplit("_", 1)[-1].startswith("v"), key
            assert key.rsplit("_", 1)[-1][1:].isdigit(), key

    def test_every_email_key_carries_an_explicit_version_suffix(self):
        for key in EMAIL_TEMPLATE_REGISTRY:
            assert key.rsplit("_", 1)[-1].startswith("v"), key
            assert key.rsplit("_", 1)[-1][1:].isdigit(), key

    def test_every_notification_type_has_a_current_key(self):
        assert set(CURRENT_TEMPLATE_KEY) == set(NotificationType)
        for key in CURRENT_TEMPLATE_KEY.values():
            assert key in NOTIFICATION_TEMPLATE_REGISTRY

    def test_no_duplicate_key_locale_pair_exists(self):
        """A dict cannot hold a duplicate, so what this really asserts is that
        the *assembly* did not silently overwrite one entry with another —
        which is exactly what a `_v2` added under the wrong key would do."""
        for key, by_locale in NOTIFICATION_TEMPLATE_REGISTRY.items():
            for locale, template in by_locale.items():
                assert template.key == key, (
                    f"{key}/{locale} holds a template whose own key is {template.key}"
                )
        for key, by_locale in EMAIL_TEMPLATE_REGISTRY.items():
            for locale, template in by_locale.items():
                assert template.key == key

    def test_every_current_in_app_key_has_default_locale_copy(self):
        for key in CURRENT_TEMPLATE_KEY.values():
            assert DEFAULT_LOCALE in NOTIFICATION_TEMPLATE_REGISTRY[key]

    def test_every_email_supported_type_has_default_locale_copy(self):
        for notification_type in NotificationType:
            if not email_supported(notification_type):
                continue
            key = CURRENT_TEMPLATE_KEY[notification_type]
            assert key in EMAIL_TEMPLATE_REGISTRY, key
            assert DEFAULT_LOCALE in EMAIL_TEMPLATE_REGISTRY[key]

    def test_the_in_app_only_type_has_no_email_copy_in_any_locale(self):
        """`SAMPLE_STATUS_CHANGED` is `email_supported = False`. Registering
        copy for it would make re-enabling the channel a one-line policy change
        with no copy review."""
        key = CURRENT_TEMPLATE_KEY[NotificationType.SAMPLE_STATUS_CHANGED]
        assert key not in EMAIL_TEMPLATE_REGISTRY
        assert not NOTIFICATION_DELIVERY_POLICIES[
            NotificationType.SAMPLE_STATUS_CHANGED
        ].email_supported

    def test_the_existing_v1_keys_are_all_still_valid(self):
        """The Block B/E keys are in production data. Block F must not have
        renamed one, and none of them may vanish from the registry — even the
        pre-release remediation's `sample_status_changed_v1`, superseded but
        still resolvable (§ below)."""
        expected = {
            "report_submitted_v1",
            "report_pdf_ready_v1",
            "report_published_v1",
            "report_retracted_v1",
            "assignment_added_v1",
            "sample_status_changed_v1",
        }
        assert expected <= set(NOTIFICATION_TEMPLATE_REGISTRY)

    def test_current_keys_after_the_pre_release_remediation(self):
        """`sample_status_changed_v1` persisted the raw `SampleState` enum as
        final Spanish text and was superseded by `_v2`, which does not.
        `assignment_added_v1` is unchanged: it is still `ASSIGNMENT_ADDED`'s
        one `CURRENT_TEMPLATE_KEY` entry, used for order-context assignment;
        the sample/reviewer contexts now use their own keys, selected
        directly by their integration functions rather than through this
        1:1-per-type map (see `notification_integrations/assignments.py`)."""
        expected = {
            "report_submitted_v1",
            "report_pdf_ready_v1",
            "report_published_v1",
            "report_retracted_v1",
            "assignment_added_v1",
            "sample_status_changed_v2",
            # Céluma 1.3, Phase 4, Block G — the four usage-threshold types,
            # each with exactly one current key.
            "storage_usage_approaching_v1",
            "storage_limit_reached_v1",
            "user_limit_approaching_v1",
            "user_limit_reached_v1",
        }
        assert set(CURRENT_TEMPLATE_KEY.values()) == expected
        assert "sample_status_changed_v1" in NOTIFICATION_TEMPLATE_REGISTRY
        assert "sample_status_changed_v1" not in CURRENT_TEMPLATE_KEY.values()
        for key in ("assignment_added_sample_v1", "assignment_added_review_v1"):
            assert key in NOTIFICATION_TEMPLATE_REGISTRY
            assert key not in CURRENT_TEMPLATE_KEY.values()

    def test_a_retired_key_stays_resolvable(self, monkeypatch):
        """The deprecation policy, exercised.

        A superseded key is never removed: a notification created under it is
        frozen in somebody's inbox, and a delivery row can outlive a copy
        revision. `_RETIRED_TEMPLATES` is empty today — Block F ships no
        speculative `_v2` — so this synthesizes one and asserts the lookup
        finds it.
        """
        retired = inapp_registry.NotificationTemplate(
            key="report_published_v0",
            notification_type=NotificationType.REPORT_PUBLISHED,
            title="Copia antigua — Orden {order_number}",
            body=None,
            params=(inapp_registry.TemplateParam("order_number", max_length=50),),
        )
        monkeypatch.setitem(
            inapp_registry.NOTIFICATION_TEMPLATE_REGISTRY,
            "report_published_v0",
            {DEFAULT_LOCALE: retired},
        )

        resolved = get_template(
            NotificationType.REPORT_PUBLISHED, "report_published_v0"
        )
        assert resolved.title.startswith("Copia antigua")
        # The current key is unaffected — old and new coexist.
        assert get_template(
            NotificationType.REPORT_PUBLISHED, "report_published_v1"
        ).title.startswith("Reporte publicado")


# ---------------------------------------------------------------------------
# Lookup and fallback
# ---------------------------------------------------------------------------

class TestLookupAndFallback:
    def test_the_default_locale_resolves_directly(self):
        template = get_template(
            NotificationType.REPORT_SUBMITTED, "report_submitted_v1", "es-MX"
        )
        assert template.key == "report_submitted_v1"

    def test_an_unsupported_locale_yields_default_locale_copy(self):
        fallback = get_template(
            NotificationType.REPORT_SUBMITTED, "report_submitted_v1", "en-US"
        )
        direct = get_template(
            NotificationType.REPORT_SUBMITTED, "report_submitted_v1", "es-MX"
        )
        assert fallback is direct

    def test_an_invalid_locale_is_rejected_by_the_lookup(self):
        with pytest.raises(InvalidLocaleError):
            get_template(
                NotificationType.REPORT_SUBMITTED, "report_submitted_v1", "../../etc"
            )

    def test_omitting_the_locale_is_the_default(self):
        assert get_template(
            NotificationType.REPORT_SUBMITTED, "report_submitted_v1"
        ) is get_template(NotificationType.REPORT_SUBMITTED, "report_submitted_v1", "es-MX")

    def test_a_key_type_mismatch_still_fails_in_every_locale(self):
        """Locale resolution must not become a way around the type check."""
        for locale in (None, "es-MX", "en-US"):
            with pytest.raises(NotificationTemplateError) as exc:
                get_template(NotificationType.REPORT_SUBMITTED, "report_published_v1", locale)
            assert exc.value.code == "template_key_mismatch"

    def test_the_email_registry_falls_back_the_same_way(self):
        assert get_email_template(
            NotificationType.REPORT_PUBLISHED, "report_published_v1", "en-US"
        ) is get_email_template(
            NotificationType.REPORT_PUBLISHED, "report_published_v1", "es-MX"
        )

    def test_an_invalid_locale_is_rejected_by_the_email_lookup(self):
        with pytest.raises(InvalidLocaleError):
            get_email_template(
                NotificationType.REPORT_PUBLISHED, "report_published_v1", "\n../x"
            )


# ---------------------------------------------------------------------------
# Screening survives localization — Story F16
# ---------------------------------------------------------------------------

class TestScreeningSurvivesLocalization:
    def _render(self, locale, **overrides):
        kwargs = {
            "tenant_name": "Laboratorio Céluma",
            "notification_type": NotificationType.REPORT_PUBLISHED,
            "template_key": "report_published_v1",
            "template_params": {"order_number": "ORD-2026-00152"},
            "locale": locale,
        }
        kwargs.update(overrides)
        return render_notification_email(**kwargs)

    @pytest.mark.parametrize("locale", [None, "es-MX", "en-US", "pt-BR"])
    def test_unsafe_values_are_rejected_whatever_the_locale(self, locale):
        """The template chosen may differ; the checks applied to every
        interpolated value do not."""
        for unsafe in (
            "https://evil.test/steal",
            "<script>alert(1)</script>",
            "victim@example.test",
            "Bearer abc",
            "a.aaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc",
            "line\nbreak",
        ):
            with pytest.raises(EmailTemplateError):
                self._render(locale, template_params={"order_number": unsafe})

    @pytest.mark.parametrize("locale", [None, "es-MX", "en-US"])
    def test_no_locale_introduces_a_deep_link(self, locale):
        """Every URL in every part is the FRONTEND_URL origin, bare.

        Extracted with a regex rather than by splitting on whitespace: in the
        HTML part the URL appears inside `href="..."`, and a whitespace split
        would compare the whole attribute against the origin and pass or fail
        for the wrong reason.
        """
        import re

        from app.core.config import settings

        rendered = self._render(locale)
        origin = settings.frontend_url.rstrip("/")
        url_pattern = re.compile(r"https?://[^\s\"'<>)]+")

        for body in (rendered.text_body, rendered.html_body):
            urls = url_pattern.findall(body)
            assert urls, "the CTA origin should appear in both parts"
            for url in urls:
                assert url.rstrip("/") == origin, (
                    f"{url} is a path into a resource, not the bare origin"
                )

    @pytest.mark.parametrize("locale", [None, "es-MX", "en-US"])
    def test_no_locale_introduces_remote_content(self, locale):
        """No image, no tracking pixel, no remote font, no external
        stylesheet, no script — an email that fetches nothing cannot report
        that it was opened."""
        html = self._render(locale).html_body.lower()
        for forbidden in (
            "<img",
            "<script",
            "<link",
            "<iframe",
            "@import",
            "background-image",
            "url(",
            "srcset",
        ):
            assert forbidden not in html, forbidden

    def test_no_locale_widens_the_parameter_vocabulary(self):
        """Every locale of a key shares one declared parameter set.

        A translator can change words. They cannot introduce a parameter,
        because rendering only passes the declared ones to `format` — so a
        translated template referencing `{actor_name}` would raise rather than
        interpolate.
        """
        for key, by_locale in EMAIL_TEMPLATE_REGISTRY.items():
            vocabularies = {tuple(sorted(t.params)) for t in by_locale.values()}
            assert len(vocabularies) == 1, (
                f"{key} declares different parameters in different locales"
            )

    def test_the_email_vocabulary_is_still_almost_only_the_order_number(self):
        """Block E's rule, restated after the registry was restructured:
        widening it must require editing an assertion.

        Widened once, by Céluma 1.3, Phase 4, Block G, which added four
        tenant-scoped usage-threshold templates. Those name no order — there
        is none — and take either `usage_percent` (a backend-computed integer
        with no user-editable source) or nothing at all. Every order-scoped
        template still takes exactly `order_number`, and the *locale* rule the
        surrounding class is about is unchanged: no translation may introduce
        a parameter, because `params` is declared per key and shared by every
        locale of it.
        """
        allowed = {("order_number",), ("usage_percent",), ()}
        for key, by_locale in EMAIL_TEMPLATE_REGISTRY.items():
            declared = {template.params for template in by_locale.values()}
            assert len(declared) == 1, f"{key} declares different params per locale"
            assert declared.pop() in allowed, key

    def test_extra_in_app_parameters_are_ignored_not_rejected(self):
        """The selection mechanism, unchanged by localization: an in-app
        template legitimately carries `actor_name`, and the email registry
        reads only what it declared."""
        rendered = self._render(
            "es-MX",
            template_params={
                "order_number": "ORD-2026-00152",
                "actor_name": "Dra. Martínez",
                "sample_code": "S-1",
            },
        )
        assert "ORD-2026-00152" in rendered.subject
        assert "Martínez" not in rendered.subject
        assert "Martínez" not in rendered.text_body
        assert "S-1" not in rendered.text_body


# ---------------------------------------------------------------------------
# Persisted locale — Story F6, Option B
# ---------------------------------------------------------------------------

class TestPersistedLocaleModel:
    def test_the_notification_model_carries_a_non_null_locale(self):
        from app.models.notification import Notification

        column = Notification.__table__.c.locale
        assert column.nullable is False
        assert column.type.length == 35

    def test_the_model_default_is_the_platform_default(self):
        from app.models.notification import Notification

        assert Notification(
            tenant_id=None,
            type=NotificationType.REPORT_SUBMITTED,
            title="t",
            resource_type="report",
            resource_id=None,
            idempotency_key="k",
        ).locale == DEFAULT_LOCALE

    def test_no_command_field_can_set_the_locale(self):
        """Structural, like the absence of `title`: a call site that could name
        a locale could name one with no registered copy, and the resulting
        fallback would be silent."""
        from app.schemas.notification import NotificationCommand

        assert "locale" not in NotificationCommand.model_fields
        with pytest.raises(Exception):
            NotificationCommand(
                tenant_id="00000000-0000-0000-0000-000000000001",
                type=NotificationType.REPORT_SUBMITTED,
                resource_type="report",
                resource_id="00000000-0000-0000-0000-000000000002",
                occurrence_marker="m",
                template_key="report_submitted_v1",
                locale="en-US",
            )
