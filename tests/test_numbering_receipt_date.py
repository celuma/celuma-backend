"""Receipt-date period resolution of sample numbering (issue #33). Pure: no database."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas.laboratory import SampleCreate, UnifiedSampleCreate
from app.services import numbering
from app.services.numbering import AllocationRequest, NumberingError, NumberingSettings, Period

MX = "America/Mexico_City"
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)  # the "registration" instant: October


def sample_request(received, **kw):
    return AllocationRequest(kw.pop("key", "s"), numbering.SAMPLE, kw.pop("scope", uuid.uuid4()), "BX",
                             uuid.uuid4(), "MAIN", kw.pop("zone", MX), received_at=received)


ACTIVE = NumberingSettings(entity_kind=numbering.SAMPLE, active=True, timezone=MX)
ACTIVE_ORDER = NumberingSettings(entity_kind=numbering.ORDER, active=True, timezone=MX)


class TestReceiptLocal:
    def test_a_timezone_less_value_is_already_the_local_calendar_date(self):
        assert numbering.receipt_local(datetime(2026, 10, 1), MX) == datetime(2026, 10, 1)
        assert numbering.receipt_local(datetime(2026, 9, 30, 23, 59, 59), "Asia/Tokyo") == datetime(2026, 9, 30, 23, 59, 59)

    def test_an_offset_value_is_converted_to_the_numbering_zone(self):
        assert numbering.receipt_local(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc), MX) == datetime(2026, 9, 30, 18, 0)
        assert numbering.receipt_local(datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc), MX) == datetime(2026, 10, 1, 0, 0)

    @pytest.mark.parametrize("value,expected", [
        (datetime(1999, 12, 31), Period(1999, 12)), (datetime(2100, 1, 1), Period(2100, 1)),
        (datetime(2000, 1, 1, tzinfo=timezone.utc), Period(1999, 12)), (datetime(1, 1, 1, 12), Period(1, 1)),
        (datetime(9999, 12, 31, 12), Period(9999, 12)),
    ])
    def test_no_year_range_is_imposed(self, value, expected):
        assert numbering.receipt_period(value, MX) == expected

    @pytest.mark.parametrize("value", [
        datetime.min.replace(tzinfo=timezone.utc),  # UTC to UTC-6 leaves year 1
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=14))),     # conversion to UTC-6 leaves year 1
        datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-14))),  # conversion leaves year 9999
    ])
    def test_an_unrepresentable_offset_conversion_is_an_explicit_422(self, value):
        with pytest.raises(NumberingError) as err:
            numbering.receipt_local(value, MX)
        assert err.value.status_code == 422

    def test_year_and_month_boundaries(self):
        assert numbering.receipt_period(datetime(2026, 12, 31, 23, 59), MX) == Period(2026, 12)
        assert numbering.receipt_period(datetime(2027, 1, 1), MX) == Period(2027, 1)
        assert numbering.receipt_period(datetime(2027, 1, 1, 5, 59, tzinfo=timezone.utc), MX) == Period(2026, 12)

    def test_an_unresolvable_stored_zone_falls_back_to_the_default_like_orders(self):
        assert numbering.receipt_period(datetime(2026, 10, 1, 0, tzinfo=timezone.utc), "Mars/Olympus") == Period(2026, 9)


class TestRequestPeriod:
    def test_a_sample_ignores_the_allocation_instant(self):
        assert numbering.request_period(ACTIVE, sample_request(datetime(2026, 9, 30)), NOW) == Period(2026, 9)

    def test_an_order_keeps_the_allocation_instant(self):
        order = AllocationRequest("o", numbering.ORDER, uuid.uuid4(), "CU", uuid.uuid4(), "MAIN", MX, received_at=datetime(2020, 1, 1))
        assert numbering.request_period(ACTIVE_ORDER, order, NOW) == Period(2026, 10)

    def test_a_sample_without_a_receipt_date_is_refused_never_numbered_with_the_clock(self):
        with pytest.raises(NumberingError) as err:
            numbering.request_period(ACTIVE, sample_request(None), NOW)
        assert err.value.status_code == 422

    def test_the_compatibility_scheme_is_not_monthly(self):
        legacy = NumberingSettings(entity_kind=numbering.SAMPLE, active=False)
        assert numbering.request_period(legacy, sample_request(None), NOW) == numbering.LEGACY_PERIOD

    def test_a_branch_scoped_sample_uses_the_branch_zone(self):
        scoped = NumberingSettings(entity_kind=numbering.SAMPLE, active=True, scope=numbering.SCOPE_BRANCH, timezone=MX)
        instant = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        assert numbering.request_period(scoped, sample_request(instant, zone="Pacific/Auckland"), NOW) == Period(2026, 11)
        assert numbering.request_period(scoped, sample_request(instant, zone=MX), NOW) == Period(2026, 10)


class TestBatchLockOrder:
    """Counters are claimed in one deterministic order whatever the order of the request."""

    def run(self, monkeypatch, requests):
        calls = []

        def fake(session, *, tenant_id, request, instant, settings, actor_id):
            calls.append((request.key, numbering.request_period(settings, request, instant)))
            return object()

        monkeypatch.setattr(numbering, "_allocate_one", fake)
        numbering.allocate_batch(None, tenant_id="t", requests=requests, instant=NOW,
                                 settings_by_kind={numbering.SAMPLE: ACTIVE, numbering.ORDER: ACTIVE_ORDER})
        return calls

    def test_order_is_kind_scope_branch_period_then_request_order(self, monkeypatch):
        scope = uuid.uuid4()
        branch = uuid.uuid4()

        def req(key, received):
            return AllocationRequest(key, numbering.SAMPLE, scope, "BX", branch, "MAIN", MX, received_at=received)

        a, b, c, d = req("a", datetime(2026, 10, 2)), req("b", datetime(2026, 9, 2)), req("c", datetime(2026, 10, 3)), req("d", datetime(2026, 9, 5))
        forward = self.run(monkeypatch, [a, b, c, d])
        backward = self.run(monkeypatch, [d, c, b, a])
        assert [k for k, _ in forward] == ["b", "d", "a", "c"]   # September first, request order inside a period
        assert [k for k, _ in backward] == ["d", "b", "c", "a"]
        assert [p for _, p in forward] == [p for _, p in backward] == [Period(2026, 9)] * 2 + [Period(2026, 10)] * 2

    def test_orders_still_precede_samples(self, monkeypatch):
        order = AllocationRequest("o", numbering.ORDER, uuid.uuid4(), "CU", uuid.uuid4(), "MAIN", MX)
        calls = self.run(monkeypatch, [sample_request(datetime(2026, 9, 1), key="s"), order])
        assert [k for k, _ in calls] == ["o", "s"]

    def test_a_bad_receipt_date_fails_before_any_counter_is_touched(self, monkeypatch):
        touched = []
        monkeypatch.setattr(numbering, "_allocate_one", lambda *a, **k: touched.append(1))
        with pytest.raises(NumberingError):
            numbering.allocate_batch(None, tenant_id="t", requests=[sample_request(datetime(2026, 9, 1)), sample_request(None, key="x")],
                                     instant=NOW, settings_by_kind={numbering.SAMPLE: ACTIVE})
        assert touched == []


class TestSchemas:
    BASE = {"sample_type_id": str(uuid.uuid4())}
    STANDALONE = {"tenant_id": "t", "branch_id": "b", "order_id": "o", **BASE}

    @pytest.mark.parametrize("model,base", [(SampleCreate, STANDALONE), (UnifiedSampleCreate, BASE)])
    @pytest.mark.parametrize("bad", [
        None, "", "  ", "x", 0, 1_790_000_000, 1_790_000_000.5, True, [], {},
        "1790000000", "1790000000.5", "-1", "0", "20260930", "2026093", "9/30/2026", "30-09-2026", "2026-9-30",
        "2026-13-01", "2026-02-30", "2026-09-30T25:00:00", "0000-01-01", "2026-09-30T", "2026-09-30 junk", "now", "today",
    ])
    def test_a_missing_or_invalid_receipt_date_is_refused(self, model, base, bad):
        with pytest.raises(ValidationError):
            model(**base, received_at=bad)
        with pytest.raises(ValidationError):
            model(**base)

    @pytest.mark.parametrize("model,base", [(SampleCreate, STANDALONE), (UnifiedSampleCreate, BASE)])
    @pytest.mark.parametrize("value", ["2026-09-30", "2026-09-30T08:30:00", "2026-09-30T08:30:00Z", "2026-09-30T08:30:00-06:00",
                                       "2026-09-30T08:30", "2026-09-30 08:30:00", "2026-09-30T08:30:00.123456+05:30",
                                       datetime(2026, 9, 30)])
    def test_date_only_timezone_less_and_offset_values_are_accepted(self, model, base, value):
        assert model(**base, received_at=value).received_at.year == 2026

    @pytest.mark.parametrize("model,base", [(SampleCreate, STANDALONE), (UnifiedSampleCreate, BASE)])
    @pytest.mark.parametrize("value,year", [("1999-12-31", 1999), ("2100-01-01", 2100), ("0001-01-01", 1), ("9999-12-31", 9999)])
    def test_previously_valid_years_are_not_restricted(self, model, base, value, year):
        assert model(**base, received_at=value).received_at.year == year

    @pytest.mark.parametrize("bad", ["1790000000", "20260930", 1790000000, True, "", "x", "2026-13-01"])
    def test_a_provided_preview_reference_date_has_the_same_strict_shape(self, bad):
        from app.schemas.numbering import NumberingPreviewRequest
        with pytest.raises(ValidationError):
            NumberingPreviewRequest(entity_kind="sample", reference_date=bad)

    @pytest.mark.parametrize("ok", [None, "2026-09-30", "1999-12-31", "2100-01-01T00:00:00Z"])
    def test_the_preview_reference_date_stays_optional(self, ok):
        from app.schemas.numbering import NumberingPreviewRequest
        assert NumberingPreviewRequest(entity_kind="sample", reference_date=ok).entity_kind == "sample"

    def test_collection_date_stays_optional(self):
        assert UnifiedSampleCreate(**self.BASE, received_at="2026-09-30").collected_at is None
