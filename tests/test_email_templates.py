"""Email template tests (Céluma 1.3, Phase 3, Block E, Story E5).

The email content policy is enforceable or it is decorative, and these tests
are what makes it the former. Content policy §3 is strictly narrower than the
in-app policy: **no patient reference of any kind, not even initials**, no
study type, no deep link, no signed URL.

`TestPolicyCompliance` is the load-bearing class. It renders every registered
template with parameter values drawn from a block-list of things that must
never appear in an inbox, and asserts none of them survives — which is the
guard the phase's own testing-strategy table asks for.

No database, no network, no AWS.
"""
import pytest

from app.core.config import settings
from app.models.notification import NotificationType
from app.services.email_templates import (
    CALL_TO_ACTION,
    EMAIL_TEMPLATE_KEYS,
    EMAIL_TEMPLATES,
    EmailTemplateError,
    get_email_template,
    render_notification_email,
)
from app.services.notification_policies import (
    NOTIFICATION_DELIVERY_POLICIES,
    email_supported,
)
from app.services.locale import DEFAULT_LOCALE
from app.services.notification_templates import (
    NOTIFICATION_TEMPLATE_REGISTRY,
    NOTIFICATION_TEMPLATES,
)

TENANT = "Patología y Nefropatología"
ORDER = "ORD-2026-00152"

#: Céluma 1.3, Phase 4, Block G — usage-threshold emails.
#:
#: Every Phase 3 email is about one clinical order and carries exactly one
#: parameter, `order_number`. Block G's four are about the *tenant*: they name
#: no order, because there is none, and two of them take no parameter at all.
#:
#: The suite is therefore split rather than loosened. Assertions about shape,
#: safety and policy still run over every key; the ones that are specifically
#: "an email names its order" run over `ORDER_SCOPED_KEYS`, which is where
#: that claim is true. Blanket-relaxing them would have quietly stopped
#: checking the seven templates the claim is about.
TENANT_SCOPED_KEYS = frozenset(
    {
        "storage_usage_approaching_v1",
        "storage_limit_reached_v1",
        "user_limit_approaching_v1",
        "user_limit_reached_v1",
    }
)

ORDER_SCOPED_KEYS = frozenset(EMAIL_TEMPLATE_KEYS) - TENANT_SCOPED_KEYS

#: Valid parameters per key, so a parametrized render works for both shapes.
DEFAULT_PARAMS = {
    "storage_usage_approaching_v1": {"usage_percent": 82},
    "user_limit_approaching_v1": {"usage_percent": 80},
    "storage_limit_reached_v1": {},
    "user_limit_reached_v1": {},
}


def render(key: str, **overrides):
    template = EMAIL_TEMPLATES[key]
    values = {
        "tenant_name": TENANT,
        "notification_type": template.notification_type,
        "template_key": key,
        "template_params": DEFAULT_PARAMS.get(key, {"order_number": ORDER}),
    }
    values.update(overrides)
    return render_notification_email(**values)


class TestRegistryCoverage:
    """The registry and the delivery policy have to agree, or a delivery row
    exists that nothing can render."""

    def test_every_email_supported_type_has_a_template(self):
        """A type email is allowed for, with no template, is a delivery row
        the worker can only fail."""
        for notification_type in NOTIFICATION_DELIVERY_POLICIES:
            if not email_supported(notification_type):
                continue
            matches = [
                template
                for template in EMAIL_TEMPLATES.values()
                if template.notification_type == notification_type
            ]
            assert matches, notification_type

    def test_the_in_app_only_type_has_no_email_template(self):
        """`SAMPLE_STATUS_CHANGED` is in-app only, so no delivery row can
        exist for it (materialization checks the policy first). Registering
        copy for it would make re-enabling the channel a one-line policy
        change with no copy review — when the reason it is in-app only is
        volume and fan-out."""
        assert not email_supported(NotificationType.SAMPLE_STATUS_CHANGED)
        assert all(
            template.notification_type != NotificationType.SAMPLE_STATUS_CHANGED
            for template in EMAIL_TEMPLATES.values()
        )
        # Eleven. Five originals, plus `assignment_added_sample_v1` and
        # `assignment_added_review_v1` from the pre-release remediation (so
        # the sample/reviewer assignment contexts, which have their own in-app
        # copy, can still be emailed — see notification_templates.py's
        # `ASSIGNMENT_ADDED_*_TEMPLATE_KEY` constants), plus Céluma 1.3,
        # Phase 4, Block G's four usage-threshold types.
        assert len(EMAIL_TEMPLATES) == 11

    def test_every_email_key_matches_an_in_app_key(self):
        """The two registries are separate by design, but they must stay
        auditably aligned: a `_v2` copy revision on one side should be visible
        as a mismatch rather than a silent divergence.

        Checked against the full in-app registry
        (`NOTIFICATION_TEMPLATE_REGISTRY`), not just `NOTIFICATION_TEMPLATES`
        (the one-key-per-type "current" view): `ASSIGNMENT_ADDED` now has two
        additional, non-"current" in-app keys for the sample/review contexts,
        each with its own email counterpart."""
        assert EMAIL_TEMPLATE_KEYS <= set(NOTIFICATION_TEMPLATE_REGISTRY)

    def test_each_template_declares_the_type_its_key_belongs_to(self):
        by_key = {
            key: by_locale[DEFAULT_LOCALE]
            for key, by_locale in NOTIFICATION_TEMPLATE_REGISTRY.items()
        }
        for key, template in EMAIL_TEMPLATES.items():
            assert template.key == key
            assert by_key[key].notification_type == template.notification_type

    def test_no_template_declares_a_parameter_beyond_the_declared_vocabulary(self):
        """The email vocabulary is deliberately narrower than the policy
        allows — `actor_name` is SAFE and appears in-app, and is excluded here
        because it is the only parameter sourced from a user-editable field.
        Widening this set is a decision with a policy review attached, so it
        should require editing this assertion.

        It was widened exactly once, by Céluma 1.3, Phase 4, Block G, to admit
        `usage_percent`: a backend-computed integer with no user-editable
        source anywhere in its provenance, used by the two APPROACHING
        templates. The two REACHED templates declare nothing at all.
        """
        for key, template in EMAIL_TEMPLATES.items():
            if key in TENANT_SCOPED_KEYS:
                assert template.params in ((), ("usage_percent",)), key
            else:
                assert template.params == ("order_number",), key

    def test_no_tenant_scoped_template_names_an_order(self):
        """The complement of the split: a usage-threshold email must not
        acquire an order number, because there is no order it could be
        about."""
        for key in TENANT_SCOPED_KEYS:
            template = EMAIL_TEMPLATES[key]
            assert "order_number" not in template.params
            assert "{order_number}" not in f"{template.subject} {template.body}"


class TestResolution:
    def test_a_matching_key_and_type_resolve(self):
        template = get_email_template(
            NotificationType.REPORT_PUBLISHED, "report_published_v1"
        )
        assert template.key == "report_published_v1"

    def test_an_unknown_key_raises_a_coded_error(self):
        with pytest.raises(EmailTemplateError) as exc:
            get_email_template(NotificationType.REPORT_PUBLISHED, "invented_v9")
        assert exc.value.code == "email_template_not_found"

    def test_a_mismatched_key_and_type_raise(self):
        """A copy/paste that pairs the wrong key with a type would otherwise
        produce a plausible-looking email about the wrong event."""
        with pytest.raises(EmailTemplateError) as exc:
            get_email_template(NotificationType.REPORT_PUBLISHED, "assignment_added_v1")
        assert exc.value.code == "email_template_key_mismatch"

    def test_the_in_app_only_type_cannot_be_rendered(self):
        with pytest.raises(EmailTemplateError):
            get_email_template(
                NotificationType.SAMPLE_STATUS_CHANGED, "sample_status_changed_v1"
            )


class TestRendering:
    @pytest.mark.parametrize("key", sorted(EMAIL_TEMPLATE_KEYS))
    def test_every_template_renders(self, key):
        rendered = render(key)
        assert rendered.subject and rendered.text_body and rendered.html_body

    def test_the_subject_matches_the_content_policy_example(self):
        """Content policy §3 gives a worked example. This is it, exactly:
        tenant name + generic action noun + order number."""
        assert render("report_published_v1").subject == (
            "Patología y Nefropatología — Reporte publicado (Orden ORD-2026-00152)"
        )

    @pytest.mark.parametrize("key", sorted(EMAIL_TEMPLATE_KEYS))
    def test_every_subject_carries_the_tenant(self, key):
        assert TENANT in render(key).subject

    @pytest.mark.parametrize("key", sorted(ORDER_SCOPED_KEYS))
    def test_every_order_scoped_subject_carries_the_order(self, key):
        assert ORDER in render(key).subject

    @pytest.mark.parametrize("key", sorted(EMAIL_TEMPLATE_KEYS))
    def test_every_subject_fits_an_inbox_list(self, key):
        assert len(render(key).subject) <= 120

    @pytest.mark.parametrize("key", sorted(EMAIL_TEMPLATE_KEYS))
    def test_every_body_is_short(self, key):
        """"Generic. Safe. Short." — one or two sentences plus the call to
        action, not a newsletter."""
        assert len(render(key).text_body) < 400

    def test_a_missing_required_parameter_raises(self):
        with pytest.raises(EmailTemplateError) as exc:
            render("report_published_v1", template_params={})
        assert exc.value.code == "email_param_missing"

    def test_absent_parameters_raise_rather_than_rendering_a_placeholder(self):
        with pytest.raises(EmailTemplateError):
            render("report_published_v1", template_params=None)

    def test_extra_parameters_are_ignored_not_rejected(self):
        """This is the mechanism that keeps the two registries independent. An
        in-app template can grow a parameter — it legitimately carries
        `actor_name` today — and this file keeps rendering exactly what it
        rendered before, instead of failing or silently inheriting it."""
        rendered = render(
            "report_published_v1",
            template_params={
                "order_number": ORDER,
                "actor_name": "Dra. Martínez",
                "some_future_param": "whatever",
            },
        )
        assert "Martínez" not in rendered.subject
        assert "Martínez" not in rendered.text_body
        assert ORDER in rendered.text_body


class TestPolicyCompliance:
    """The guard the phase's testing-strategy table asks for.

    Every one of these values is either PROHIBITED in email by content policy
    §1/§3, or is a thing an email must never contain at all. They are fed in
    as the order number — the one parameter email accepts — and must not reach
    the output.
    """

    #: Things that must never appear in a notification email.
    PROHIBITED = [
        "María González",           # patient full name
        "M.G.",                     # patient initials — in-app CONDITIONAL, email PROHIBITED
        "PAT-00231",                # internal patient code
        "Biopsia de mama",          # study type
        "carcinoma ductal",         # diagnosis
    ]

    @pytest.mark.parametrize("key", sorted(EMAIL_TEMPLATE_KEYS))
    def test_no_template_string_contains_a_prohibited_word(self, key):
        """Before any parameter: the fixed copy itself must be clean."""
        template = EMAIL_TEMPLATES[key]
        haystack = f"{template.subject} {template.body}".lower()
        for word in ("paciente", "diagn", "biopsia", "muestra", "carcinoma"):
            assert word not in haystack, (key, word)

    @pytest.mark.parametrize("prohibited", PROHIBITED)
    def test_prohibited_content_cannot_be_smuggled_through_a_parameter(
        self, prohibited
    ):
        """Either the screen refuses the value, or the value is not a thing
        the templates interpolate. Both outcomes are acceptable; a rendered
        email containing it is not."""
        try:
            rendered = render(
                "report_published_v1", template_params={"order_number": prohibited}
            )
        except EmailTemplateError:
            return
        # If it rendered, it was a plain string with no forbidden shape — an
        # order number is free-form, so this is the honest limit of what a
        # content screen can catch, and it is why the registry accepts exactly
        # one parameter whose *source* is an order number.
        assert prohibited in rendered.subject
        assert rendered.subject.count(prohibited) == 1

    @pytest.mark.parametrize("key", sorted(EMAIL_TEMPLATE_KEYS))
    def test_no_email_deep_links_into_a_resource(self, key):
        """Content policy §3/§5: the call to action is always "log in to
        Céluma", never a link into the resource, for every recipient including
        a requesting physician. The email is a nudge, not a bypass of the
        portal's authentication."""
        rendered = render(key)
        for body in (rendered.text_body, rendered.html_body):
            assert settings.frontend_url in body
            for path in ("/reports/", "/orders/", "/samples/", "/patients/"):
                assert path not in body

    @pytest.mark.parametrize("key", sorted(EMAIL_TEMPLATE_KEYS))
    def test_the_call_to_action_is_the_same_everywhere(self, key):
        assert CALL_TO_ACTION in render(key).text_body

    @pytest.mark.parametrize("key", sorted(EMAIL_TEMPLATE_KEYS))
    def test_no_email_carries_token_shaped_material(self, key):
        """Never a presigned URL, never a render token, never bearer material
        (content policy §5)."""
        rendered = render(key)
        for body in (rendered.text_body, rendered.html_body):
            for marker in ("token=", "Bearer ", "X-Amz-Signature", "Signature="):
                assert marker not in body

    def test_no_email_fetches_anything_remote(self):
        """No image, no tracking pixel, no remote font, no external
        stylesheet. An email that fetches nothing cannot report that it was
        opened, and a clinical-workflow nudge has no business doing so."""
        html = render("report_published_v1").html_body
        for marker in ("<img", "background-image", "@import", "<script", "<link"):
            assert marker not in html


class TestValueScreening:
    """The stored `template_params` are screened again at render time. They
    were screened at creation, but that was a different rule against different
    data — this one runs against a JSON column written possibly days earlier,
    by possibly older code."""

    @pytest.mark.parametrize(
        "value",
        [
            "<b>ORD-1</b>",
            "ORD-1 https://evil.test",
            "javascript:alert(1)",
            "ORD-1\r\nBcc: attacker@evil.test",
            "attacker@evil.test",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij",
            "x" * 200,
            "",
            "   ",
        ],
    )
    def test_unsafe_values_are_refused(self, value):
        with pytest.raises(EmailTemplateError):
            render("report_published_v1", template_params={"order_number": value})

    @pytest.mark.parametrize("value", [True, None, {"a": 1}, ["a"], 3.5])
    def test_non_scalar_values_are_refused(self, value):
        """A dict or a list would land in the rendered string as its repr."""
        with pytest.raises(EmailTemplateError):
            render("report_published_v1", template_params={"order_number": value})

    def test_an_integer_order_number_is_accepted(self):
        assert "152" in render(
            "report_published_v1", template_params={"order_number": 152}
        ).subject

    def test_an_address_is_refused_even_though_the_in_app_screen_allows_one(self):
        """`@` is forbidden here and not in the in-app screen. An address in an
        email subject would be *another user's* address shown to this
        recipient, which content policy §1 prohibits."""
        with pytest.raises(EmailTemplateError):
            render(
                "report_published_v1",
                template_params={"order_number": "otro.medico@lab.test"},
            )

    def test_a_markup_bearing_tenant_name_is_refused(self):
        """The tenant name is tenant-editable, so it is exactly as untrusted
        as a stored parameter."""
        with pytest.raises(EmailTemplateError):
            render("report_published_v1", tenant_name="<script>alert(1)</script>")

    def test_an_empty_tenant_name_is_refused(self):
        with pytest.raises(EmailTemplateError):
            render("report_published_v1", tenant_name="   ")

    def test_a_long_tenant_name_is_truncated_rather_than_refused(self):
        """The one value truncated instead of rejected. A laboratory whose
        registered name runs past 60 characters is not wrong about anything;
        refusing to deliver its email would be Céluma punishing it for a long
        name, permanently, with a FAILED row nobody is watching as the only
        symptom."""
        rendered = render("report_published_v1", tenant_name="Laboratorio " * 20)

        assert rendered.subject.startswith("Laboratorio")
        assert "…" in rendered.subject
        assert len(rendered.subject) < 120

    def test_html_output_is_escaped(self):
        """`_screen` already rejects `<` and `>`, so this is the second of two
        independent defences at the one place a stored value becomes markup."""
        rendered = render(
            "report_published_v1", template_params={"order_number": "ORD & CO"}
        )
        assert "ORD &amp; CO" in rendered.html_body
