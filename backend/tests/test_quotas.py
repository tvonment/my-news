"""core.quotas: one conditional increment per unit, exact boundaries, the
reset clock, and the create race."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import cosmos, quotas
from core.errors import ApiError
from tests.fakes import jwks as ids
from tests.fakes.cosmos import FakeCosmos

OID = ids.OID_ALICE
PK = f"user|{OID}"
# A Wednesday.
NOW = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)


def _exceeded(key: str, n: int = 1, now: datetime = NOW) -> ApiError:
    with pytest.raises(ApiError) as caught:
        quotas.consume(OID, key, n, now=now)
    return caught.value


class TestTable:
    def test_the_documented_limits(self) -> None:
        assert {key: (q.limit, q.period) for key, q in quotas.QUOTAS.items()} == {
            "feedback": (2000, "day"),
            "describe": (5, "day"),
            "reports": (20, "day"),
            "suggestions": (3, "week"),
            "export": (2, "day"),
            "redeem": (10, "day"),
        }

    def test_an_unknown_key_is_a_programming_error(self, fake_cosmos: FakeCosmos) -> None:
        with pytest.raises(KeyError):
            quotas.consume(OID, "anything-from-a-request")

    def test_n_must_be_positive(self, fake_cosmos: FakeCosmos) -> None:
        with pytest.raises(ValueError):
            quotas.consume(OID, "redeem", 0)


class TestConsume:
    def test_the_first_unit_creates_the_days_counter(self, fake_cosmos: FakeCosmos) -> None:
        assert quotas.consume(OID, "redeem", now=NOW) == 1
        doc = fake_cosmos.get("quota|2026-10-21", PK)
        assert doc is not None
        assert (doc["type"], doc["v"], doc["redeem"], doc["ttl"]) == ("quota", 1, 1, 172800)

    def test_later_units_increment_with_one_write(self, fake_cosmos: FakeCosmos) -> None:
        quotas.consume(OID, "redeem", now=NOW)
        before = len(fake_cosmos.writes())
        assert quotas.consume(OID, "redeem", now=NOW) == 2
        assert len(fake_cosmos.writes()) == before + 1

    def test_keys_share_the_days_document(self, fake_cosmos: FakeCosmos) -> None:
        quotas.consume(OID, "redeem", now=NOW)
        quotas.consume(OID, "export", now=NOW)
        doc = fake_cosmos.get("quota|2026-10-21", PK)
        assert (doc["redeem"], doc["export"]) == (1, 1)

    def test_exactly_the_limit_is_allowed_and_one_more_is_not(self, fake_cosmos: FakeCosmos) -> None:
        for expected in range(1, 11):
            assert quotas.consume(OID, "redeem", now=NOW) == expected
        error = _exceeded("redeem")
        assert (error.code, error.status) == ("quota_exceeded", 429)
        assert error.details == {"quota": "redeem", "limit": 10, "period": "day", "resetsAt": "2026-10-22T00:00:00Z"}
        assert error.retry_after == 14 * 3600
        # The refused attempt did not move the counter.
        assert fake_cosmos.get("quota|2026-10-21", PK)["redeem"] == 10

    def test_a_multi_unit_take_needs_room_for_all_of_it(self, fake_cosmos: FakeCosmos) -> None:
        quotas.consume(OID, "feedback", 1990, now=NOW)
        assert _exceeded("feedback", 11).code == "quota_exceeded"
        assert quotas.consume(OID, "feedback", 10, now=NOW) == 2000

    def test_more_than_the_limit_at_once_never_writes(self, fake_cosmos: FakeCosmos) -> None:
        assert _exceeded("describe", 6).code == "quota_exceeded"
        assert fake_cosmos.journal == []

    def test_a_new_day_is_a_new_counter(self, fake_cosmos: FakeCosmos) -> None:
        for _ in range(2):
            quotas.consume(OID, "export", now=NOW)
        assert _exceeded("export").code == "quota_exceeded"
        tomorrow = datetime(2026, 10, 22, 0, 0, 1, tzinfo=timezone.utc)
        assert quotas.consume(OID, "export", now=tomorrow) == 1

    def test_counters_live_in_the_callers_partition(self, fake_cosmos: FakeCosmos) -> None:
        quotas.consume(ids.OID_BOB, "export", now=NOW)
        quotas.consume(OID, "export", now=NOW)
        quotas.consume(OID, "export", now=NOW)
        assert quotas.consume(ids.OID_BOB, "export", now=NOW) == 2

    def test_the_last_second_of_the_day_waits_one_second(self, fake_cosmos: FakeCosmos) -> None:
        late = datetime(2026, 10, 21, 23, 59, 59, 500000, tzinfo=timezone.utc)
        for _ in range(2):
            quotas.consume(OID, "export", now=late)
        assert _exceeded("export", now=late).retry_after == 1


class TestWeekly:
    def test_suggestions_count_per_iso_week(self, fake_cosmos: FakeCosmos) -> None:
        for _ in range(3):
            quotas.consume(OID, "suggestions", now=NOW)
        error = _exceeded("suggestions")
        assert error.details["period"] == "week"
        assert error.details["resetsAt"] == "2026-10-26T00:00:00Z"  # the next Monday
        doc = fake_cosmos.get("quota|w2026-W43", PK)
        assert (doc["suggestions"], doc["ttl"]) == (3, 691200)

    def test_monday_starts_a_new_week(self, fake_cosmos: FakeCosmos) -> None:
        for _ in range(3):
            quotas.consume(OID, "suggestions", now=NOW)
        monday = datetime(2026, 10, 26, 0, 0, 0, tzinfo=timezone.utc)
        assert quotas.consume(OID, "suggestions", now=monday) == 1

    def test_the_iso_year_owns_the_new_years_week(self) -> None:
        # 2027-01-01 is a Friday in ISO week 53 of 2026: same counter as Dec 31.
        december = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc)
        january = datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc)
        quota = quotas.QUOTAS["suggestions"]
        assert quotas.quota_doc_id(quota, december) == quotas.quota_doc_id(quota, january) == "quota|w2026-W53"
        assert quotas.reset_at(quota, january) == datetime(2027, 1, 4, tzinfo=timezone.utc)

    def test_reset_on_a_monday_is_the_following_monday(self) -> None:
        monday_noon = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        assert quotas.reset_at(quotas.QUOTAS["suggestions"], monday_noon) == datetime(2026, 11, 2, tzinfo=timezone.utc)


class TestRaces:
    def test_a_concurrent_first_unit_is_counted_not_lost(self, fake_cosmos: FakeCosmos) -> None:
        # Both requests see no counter; the other one creates it first, so our
        # create gets a 409 and must go back and increment instead.
        def other_request(fake: FakeCosmos) -> None:
            quotas.consume(OID, "redeem", now=NOW)

        fake_cosmos.before("create", other_request, id="quota|2026-10-21")
        assert quotas.consume(OID, "redeem", now=NOW) == 2
        assert fake_cosmos.get("quota|2026-10-21", PK)["redeem"] == 2

    def test_two_requests_for_the_last_unit_cannot_both_win(self, fake_cosmos: FakeCosmos) -> None:
        for _ in range(9):
            quotas.consume(OID, "redeem", now=NOW)
        results: list[object] = []

        def other_request(fake: FakeCosmos) -> None:
            results.append(quotas.consume(OID, "redeem", now=NOW))

        fake_cosmos.before("patch", other_request, id="quota|2026-10-21")
        assert _exceeded("redeem").code == "quota_exceeded"
        assert results == [10]

    def test_a_throttled_store_is_not_a_quota_answer(self, fake_cosmos: FakeCosmos) -> None:
        from tests.fakes.cosmos import throttled

        fake_cosmos.fail("patch", error=throttled())
        with pytest.raises(cosmos.Unavailable):
            quotas.consume(OID, "redeem", now=NOW)


class TestBatchOperation:
    def test_the_same_increment_in_a_transactional_batch(self, fake_cosmos: FakeCosmos) -> None:
        quotas.consume(OID, "feedback", 1995, now=NOW)
        cosmos.batch(PK, [quotas.batch_patch_operation("feedback", 5, NOW)])
        assert fake_cosmos.get("quota|2026-10-21", PK)["feedback"] == 2000
        with pytest.raises(cosmos.BatchFailed) as caught:
            cosmos.batch(PK, [quotas.batch_patch_operation("feedback", 1, NOW)])
        assert caught.value.status == 412
