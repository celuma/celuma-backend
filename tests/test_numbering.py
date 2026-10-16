"""Pure unit tests of the monthly numbering formatter, grammar and period
(issue #8; contract: celuma-engineering/specs/laboratory/monthly-numbering.md).
No database: allocation, collisions and concurrency are in tests/http/."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import numbering as n

DEFAULT = (n.DEFAULT_FORMAT, n.DEFAULT_MIN_WIDTH)


def code(seq, base="CU", year=2026, month=10, fmt=n.DEFAULT_FORMAT, width=n.DEFAULT_MIN_WIDTH):
    return n.render_code(fmt, width, base, seq, year, month)


class TestDefaultFormat:
    @pytest.mark.parametrize(
        "seq,expected",
        [(1, "CU011026"), (2, "CU021026"), (9, "CU091026"), (10, "CU101026"), (99, "CU991026"),
         (100, "CU1001026"), (999, "CU9991026"), (1000, "CU10001026"), (123456, "CU1234561026")],
    )
    def test_issue_acceptance_values_for_october_2026(self, seq, expected):
        assert code(seq) == expected

    def test_default_has_no_separator_of_any_kind(self):
        assert "-" not in code(1) and " " not in code(1)

    def test_width_grows_and_is_never_truncated_or_capped(self):
        assert code(10**7) == f"CU{10**7}1026"

    def test_month_and_year_transitions(self):
        assert code(1, month=12, year=2026) == "CU011226"
        assert code(1, month=1, year=2027) == "CU010127"
        assert code(1, month=1, year=2100) == "CU010100"

    def test_two_digit_month_and_year_are_zero_padded(self):
        assert code(7, month=3, year=2005) == "CU070305"

    def test_base_code_is_used_verbatim(self):
        assert code(1, base="BIOPSIA-PUNCH") == "BIOPSIA-PUNCH011026"


class TestConfigurableFormat:
    def test_min_width_is_a_minimum(self):
        assert code(5, width=4) == "CU0005" + "1026"
        assert code(12345, width=4) == "CU12345" + "1026"
        assert code(7, width=1) == "CU71026"

    def test_four_digit_year_and_literals(self):
        assert code(3, fmt="{BASE}-{SEQ}-{MM}{YYYY}") == "CU-03-102026"
        assert code(3, fmt="{YYYY}{MM}.{BASE}{SEQ}") == "202610.CU03"

    @pytest.mark.parametrize(
        "fmt",
        ["{BASE}{SEQ}{MM}{YY}", "{BASE}-{SEQ}{MM}{YYYY}", "{SEQ}{BASE}{YY}{MM}", "A.{BASE}_{SEQ}{MM}{YY}"],
    )
    def test_valid_formats(self, fmt):
        assert n.validate_format(fmt) == []

    @pytest.mark.parametrize(
        "fmt",
        [
            "", "   ", "{BASE}{SEQ}{MM}", "{BASE}{SEQ}{YY}", "{SEQ}{MM}{YY}", "{BASE}{MM}{YY}",
            "{BASE}{SEQ}{SEQ}{MM}{YY}", "{BASE}{SEQ}{MM}{YY}{YYYY}", "{BASE}{SEQ}{MM}{MM}{YY}",
            "{BASE}{seq}{MM}{YY}", "{BASE}{SEQ}{MM}{YY}{DAY}", "{BASE}{SEQ}{MM}{YY", "{BASE}}{SEQ}{MM}{YY}",
            "{BASE} {SEQ}{MM}{YY}", "{BASE}/{SEQ}{MM}{YY}", "{BASE}{SEQ:2}{MM}{YY}", "{BASE}{SEQ}{MM}{YY}%",
            "{BASE}{ SEQ }{MM}{YY}", "{{BASE}}{SEQ}{MM}{YY}", "x" * 61,
        ],
    )
    def test_invalid_formats_are_rejected_with_a_reason(self, fmt):
        assert n.validate_format(fmt)

    def test_no_expression_evaluation(self):
        for fmt in ("{__import__('os')}", "{BASE.lower()}{SEQ}{MM}{YY}", "${BASE}{SEQ}{MM}{YY}"):
            assert n.validate_format(fmt)

    @pytest.mark.parametrize("width,ok", [(1, True), (2, True), (6, True), (0, False), (7, False), (-1, False), (True, False), ("2", False), (2.0, False)])
    def test_min_width_bounds(self, width, ok):
        assert (n.validate_min_width(width) == []) is ok

    def test_a_code_longer_than_the_column_is_refused_not_truncated(self):
        with pytest.raises(HTTPException) as exc:
            code(1, base="X" * 100)
        assert exc.value.status_code == 409 and exc.value.detail["code"] == "numbering_code_too_long"
        assert len(code(1, base="X" * 90)) <= n.MAX_CODE_LENGTH


class TestPeriod:
    def branch(self, tz):
        return SimpleNamespace(timezone=tz)

    def test_period_is_the_local_month_in_the_branch_timezone(self):
        instant = datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc)  # still Oct 31, 21:30 in Mexico City
        assert n.period_for(self.branch("America/Mexico_City"), instant) == n.Period(2026, 10)
        assert n.period_for(self.branch("UTC"), instant) == n.Period(2026, 11)
        assert n.period_for(self.branch("Pacific/Auckland"), instant) == n.Period(2026, 11)

    def test_year_boundary(self):
        instant = datetime(2027, 1, 1, 4, 59, tzinfo=timezone.utc)  # Dec 31 22:59 in Mexico City (UTC-6)
        assert n.period_for(self.branch("America/Mexico_City"), instant) == n.Period(2026, 12)
        assert n.period_for(self.branch("America/Mexico_City"), datetime(2027, 1, 1, 6, 0, tzinfo=timezone.utc)) == n.Period(2027, 1)

    def test_naive_instant_is_treated_as_utc(self):
        assert n.period_for(self.branch("UTC"), datetime(2026, 10, 5, 12, 0)) == n.Period(2026, 10)

    @pytest.mark.parametrize("bad", ["", "Not/AZone", None, "../../etc/passwd"])
    def test_unresolvable_timezone_falls_back_instead_of_failing(self, bad):
        instant = datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc)
        assert n.period_for(self.branch(bad), instant) == n.Period(2026, 10)  # default zone: Mexico City

    def test_the_clock_is_one_replaceable_function(self, monkeypatch):
        monkeypatch.setattr(n, "_now", lambda: datetime(2026, 3, 15, tzinfo=timezone.utc))
        assert n.period_for(self.branch("UTC")) == n.Period(2026, 3)


class TestModes:
    """The decision matrix: activation x policy x capability (the tenant policy AND the caller's own permission)."""

    def settings(self, kind="order", **over):
        return n.NumberingSettings(kind, active=True, **over)

    def test_active_allowed_generates_when_blank_and_accepts_manual_only_with_the_capability(self):
        s = self.settings(manual_policy="allowed")
        assert n.check_mode(s, None, True) is True and n.check_mode(s, None, False) is True
        assert n.check_mode(s, "MAN-1", True) is False
        with pytest.raises(HTTPException) as exc:
            n.check_mode(s, "MAN-1", False)
        assert exc.value.status_code == 403 and exc.value.detail["code"] == "numbering_manual_code_forbidden"

    def test_forbidden_refuses_a_supplied_code_whoever_asks(self):
        s = self.settings(manual_policy="forbidden")
        assert n.check_mode(s, None, True) is True
        for permitted in (True, False):
            with pytest.raises(HTTPException) as exc:
                n.check_mode(s, "MAN-1", permitted)
            assert exc.value.status_code == 422 and exc.value.detail["code"] == "numbering_manual_code_not_allowed"

    def test_required_demands_a_code_and_says_403_when_the_caller_cannot_type_one(self):
        s = self.settings(manual_policy="required")
        assert n.check_mode(s, "MAN-1", True) is False
        with pytest.raises(HTTPException) as exc:
            n.check_mode(s, None, True)
        assert exc.value.status_code == 422 and exc.value.detail["code"] == "numbering_code_required"
        with pytest.raises(HTTPException) as exc:
            n.check_mode(s, None, False)
        assert exc.value.status_code == 403

    def test_inactive_keeps_the_pre_numbering_behaviour_with_no_capability_gate(self):
        order, sample = n.NumberingSettings("order"), n.NumberingSettings("sample")
        assert n.check_mode(order, None, False) is True and n.check_mode(order, "X", False) is False
        assert n.check_mode(sample, "X", False) is False
        with pytest.raises(HTTPException) as exc:
            n.check_mode(sample, None, True)
        assert exc.value.detail["code"] == "numbering_code_required"

    def test_blank_and_whitespace_mean_not_supplied(self):
        assert n.normalize_supplied(None) is None
        assert n.normalize_supplied("   ") is None
        assert n.normalize_supplied("  X1 ") == "X1"

    @pytest.mark.parametrize("kind,active,policy,permitted,expected", [
        ("order", False, "allowed", False, "optional"), ("sample", False, "allowed", False, "required"),
        ("order", True, "allowed", True, "optional"), ("order", True, "allowed", False, "hidden"),
        ("order", True, "forbidden", True, "hidden"), ("order", True, "required", True, "required"),
        ("order", True, "required", False, "blocked"),
    ])
    def test_what_the_form_is_told(self, kind, active, policy, permitted, expected):
        s = n.NumberingSettings(kind, active=active, manual_policy=policy)
        assert n.manual_entry_for(s, permitted) == expected


class TestBranchToken:
    def test_branch_scope_requires_the_branch_token_exactly_once_and_tenant_scope_forbids_it(self):
        assert n.validate_format("{BASE}{BRANCH}{SEQ}{MM}{YY}", "branch") == []
        assert n.validate_format("{BASE}{SEQ}{MM}{YY}", "branch")
        assert n.validate_format("{BASE}{BRANCH}{BRANCH}{SEQ}{MM}{YY}", "branch")
        assert n.validate_format("{BASE}{BRANCH}{SEQ}{MM}{YY}", "tenant")

    def test_render_with_the_branch_code(self):
        assert n.render_code("{BASE}-{BRANCH}-{SEQ}{MM}{YY}", 2, "CU", 1, 2026, 10, branch="NORTE") == "CU-NORTE-011026"

    @pytest.mark.parametrize("branch", ["", "NO RTE", "a/b", "a\n", "é"])
    def test_an_unsafe_or_empty_branch_code_is_refused(self, branch):
        with pytest.raises(HTTPException) as exc:
            n.render_code("{BASE}{BRANCH}{SEQ}{MM}{YY}", 2, "CU", 1, 2026, 10, branch=branch)
        assert exc.value.detail["code"] == "numbering_branch_code_unsafe"


class TestStartValueAndLiterals:
    @pytest.mark.parametrize("value,ok", [(1, True), (500, True), (99_999_999, True), (0, False), (-1, False), (100_000_000, False), (True, False), ("1", False), (1.0, False)])
    def test_start_value_bounds(self, value, ok):
        assert (n.validate_start_value(value) == []) is ok

    @pytest.mark.parametrize("literal", ["\n", "\r", "\t", "\x00", "\x1b", "\u202e", "\u200b", "\u00a0", "é", "日本", " ", "/", "\\", "'", '"', ";", "<", ">", "%", "$", "#", "*", "(", ")", "{", "}"])
    def test_every_unsafe_literal_character_is_rejected_anywhere_in_the_format(self, literal):
        for fmt in (f"{{BASE}}{literal}{{SEQ}}{{MM}}{{YY}}", f"{literal}{{BASE}}{{SEQ}}{{MM}}{{YY}}", f"{{BASE}}{{SEQ}}{{MM}}{{YY}}{literal}"):
            assert n.validate_format(fmt), repr(fmt)

    def test_a_trailing_newline_is_not_accepted_as_if_it_were_the_end(self):
        assert n.validate_format("{BASE}{SEQ}{MM}{YY}\n")  # `$` would have let this through

    @pytest.mark.parametrize("literal", ["-", ".", "_", "A", "z", "0", "9", "LAB.", "-x-"])
    def test_safe_literals_are_accepted(self, literal):
        assert n.validate_format(f"{{BASE}}{literal}{{SEQ}}{{MM}}{{YY}}") == []


class TestPreviewExamples:
    def test_examples_cross_every_width_boundary(self):
        examples = n.boundary_examples(n.NumberingSettings("order"), "CU", n.Period(2026, 10))
        assert [(e["sequence"], e["code"]) for e in examples] == [
            (1, "CU011026"), (2, "CU021026"), (9, "CU091026"), (10, "CU101026"),
            (99, "CU991026"), (100, "CU1001026"), (999, "CU9991026"), (1000, "CU10001026"),
        ]


class TestTimezoneValidation:
    @pytest.mark.parametrize("name", ["UTC", "America/Mexico_City", "Europe/Madrid", "Etc/GMT+5", "Pacific/Auckland"])
    def test_known_zones_are_accepted(self, name):
        assert n.validate_timezone(name) == []

    @pytest.mark.parametrize("name", ["", "Mars/Base", "../x", "UTC\n", " UTC", "a b", "é", None, 3, "A" * 101])
    def test_unknown_or_malformed_zones_are_rejected(self, name):
        assert n.validate_timezone(name)

    def test_the_period_zone_is_the_laboratorys_unless_the_counter_is_branch_scoped(self):
        tenant = n.NumberingSettings("order", timezone="UTC")
        branch = n.NumberingSettings("order", timezone="UTC", scope="branch")
        assert tenant.zone_name("Pacific/Auckland") == "UTC"
        assert branch.zone_name("Pacific/Auckland") == "Pacific/Auckland"
        assert branch.zone_name(None) == "UTC"
