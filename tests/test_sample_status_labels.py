"""Sample-status presentation label tests (pre-release remediation).

No database, no network — pure registry tests, mirroring the style of
`test_notification_localization.py`. What matters is the structural
guarantee: every `SampleState` member has Spanish copy, so adding a new
state later without adding a label fails here rather than shipping raw
English into a notification.
"""
import pytest

from app.models.enums import SampleState
from app.services.locale import DEFAULT_LOCALE
from app.services.sample_status_labels import (
    SAMPLE_STATUS_LABELS_BY_LOCALE,
    sample_status_label,
)


class TestEveryStateHasALabel:
    @pytest.mark.parametrize("state", list(SampleState))
    def test_every_sample_state_has_an_es_mx_label(self, state):
        assert state in SAMPLE_STATUS_LABELS_BY_LOCALE[DEFAULT_LOCALE]
        label = sample_status_label(state)
        assert isinstance(label, str) and label.strip()
        # The label must be prose, not the enum's own spelling.
        assert label != state.value

    def test_no_label_equals_its_raw_enum_value(self):
        """The regression this whole remediation is about: a label that
        happens to equal the enum's English spelling would defeat the
        point of having a label at all."""
        for state, label in SAMPLE_STATUS_LABELS_BY_LOCALE[DEFAULT_LOCALE].items():
            assert label != state.value


class TestLookup:
    def test_default_locale_resolves_directly(self):
        assert sample_status_label(SampleState.PROCESSING, "es-MX") == "En proceso"

    def test_none_locale_is_the_default(self):
        assert sample_status_label(SampleState.PROCESSING, None) == sample_status_label(
            SampleState.PROCESSING, "es-MX"
        )

    def test_an_unsupported_locale_falls_back_to_the_default(self):
        assert sample_status_label(SampleState.PROCESSING, "en-US") == "En proceso"

    def test_an_invalid_locale_raises(self):
        from app.services.locale import InvalidLocaleError

        with pytest.raises(InvalidLocaleError):
            sample_status_label(SampleState.PROCESSING, "../../etc")
