"""core.cosmos on the in-memory account: typed errors, conditional writes, RU
accounting, pacing, the two containers and the LOCAL_READONLY guard; and the
core.config settings those depend on.

The fake raises the SDK's own exception classes, so what is tested here is the
translation core.cosmos performs on the errors Cosmos really raises.
"""

from __future__ import annotations

import pytest
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError, ServiceResponseError
from azure.cosmos.exceptions import CosmosHttpResponseError

from core import cosmos
from tests.fakes.cosmos import FakeCosmos, throttled

PK = "user|a11ce000-0000-4000-8000-000000000001"


def _doc(doc_id: str = "d1", **fields: object) -> dict:
    return {"id": doc_id, "pk": PK, "type": "thing", **fields}


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Pacers sleep through time.sleep; record instead of sleeping."""
    calls: list[float] = []
    monkeypatch.setattr(cosmos.time, "sleep", calls.append)
    return calls


class TestPointOperations:
    def test_read_returns_none_for_a_missing_document(self, fake_cosmos: FakeCosmos) -> None:
        assert cosmos.read("missing", PK) is None

    def test_read_returns_the_stored_document_with_its_etag(self, fake_cosmos: FakeCosmos) -> None:
        cosmos.create(_doc(n=1))
        doc = cosmos.read("d1", PK)
        assert doc is not None and doc["n"] == 1 and doc["_etag"]

    def test_create_refuses_a_taken_id(self, fake_cosmos: FakeCosmos) -> None:
        cosmos.create(_doc())
        with pytest.raises(cosmos.AlreadyExists):
            cosmos.create(_doc())
        assert cosmos.try_create(_doc()) is False
        assert cosmos.try_create(_doc("d2")) is True

    def test_writes_never_send_system_properties(self, fake_cosmos: FakeCosmos) -> None:
        stored = cosmos.create(_doc())
        stored["n"] = 2
        # A document that came from a read carries _rid/_etag/_ts; those are the
        # server's to set, and an old _etag in a body must not look like intent.
        stored["_etag"] = '"stale"'
        saved = cosmos.upsert(stored)
        assert saved["n"] == 2 and saved["_etag"] != '"stale"'

    def test_delete_reports_whether_the_document_was_there(self, fake_cosmos: FakeCosmos) -> None:
        cosmos.create(_doc())
        assert cosmos.delete("d1", PK) is True
        # A resumed purge repeats deletes; the second is a no-op, not an error.
        assert cosmos.delete("d1", PK) is False


class TestReplaceIfMatch:
    def test_replaces_the_version_it_was_based_on(self, fake_cosmos: FakeCosmos) -> None:
        stored = cosmos.create(_doc(n=1))
        saved = cosmos.replace_if_match({**stored, "n": 2}, stored["_etag"])
        assert saved["n"] == 2 and saved["_etag"] != stored["_etag"]

    def test_refuses_when_someone_wrote_in_between(self, fake_cosmos: FakeCosmos) -> None:
        stored = cosmos.create(_doc(n=1))
        cosmos.patch("d1", PK, [{"op": "set", "path": "/n", "value": 5}])
        with pytest.raises(cosmos.PreconditionFailed):
            cosmos.replace_if_match({**stored, "n": 2}, stored["_etag"])
        assert cosmos.read("d1", PK)["n"] == 5

    def test_not_found_once_deleted(self, fake_cosmos: FakeCosmos) -> None:
        stored = cosmos.create(_doc())
        cosmos.delete("d1", PK)
        with pytest.raises(cosmos.NotFound):
            cosmos.replace_if_match(stored, stored["_etag"])

    def test_an_etag_is_required(self, fake_cosmos: FakeCosmos) -> None:
        with pytest.raises(ValueError):
            cosmos.replace_if_match(_doc(), "")


class TestPatch:
    def test_incr_creates_a_missing_counter(self, fake_cosmos: FakeCosmos) -> None:
        cosmos.create(_doc())
        assert cosmos.patch("d1", PK, [{"op": "incr", "path": "/n", "value": 3}])["n"] == 3
        assert cosmos.patch("d1", PK, [{"op": "incr", "path": "/n", "value": 2}])["n"] == 5

    def test_filter_predicate_guards_the_write(self, fake_cosmos: FakeCosmos) -> None:
        cosmos.create(_doc(status="active"))
        cosmos.patch("d1", PK, [{"op": "set", "path": "/x", "value": 1}], filter_predicate="FROM c WHERE c.status = 'active'")
        with pytest.raises(cosmos.PreconditionFailed):
            cosmos.patch("d1", PK, [{"op": "set", "path": "/x", "value": 2}], filter_predicate="FROM c WHERE c.status = 'revoked'")
        assert cosmos.read("d1", PK)["x"] == 1

    def test_not_found_for_a_missing_document(self, fake_cosmos: FakeCosmos) -> None:
        with pytest.raises(cosmos.NotFound):
            cosmos.patch("nope", PK, [{"op": "set", "path": "/x", "value": 1}])

    def test_more_than_ten_operations_is_a_programming_error(self, fake_cosmos: FakeCosmos) -> None:
        ops = [{"op": "set", "path": f"/f{i}", "value": i} for i in range(11)]
        with pytest.raises(ValueError):
            cosmos.patch("d1", PK, ops)

    def test_an_invalid_operation_is_a_status_error_not_a_crash(self, fake_cosmos: FakeCosmos) -> None:
        cosmos.create(_doc())
        with pytest.raises(cosmos.CosmosError) as caught:
            cosmos.patch("d1", PK, [{"op": "remove", "path": "/absent"}])
        assert caught.value.status == 400
        assert not isinstance(caught.value, (cosmos.NotFound, cosmos.PreconditionFailed, cosmos.Unavailable))


class TestErrorTranslation:
    @pytest.mark.parametrize(
        "error",
        [throttled(), ServiceRequestError("down"), ServiceResponseError("reset"), ClientAuthenticationError("no token")],
    )
    def test_throttling_and_transport_failures_are_unavailable(self, fake_cosmos: FakeCosmos, error: Exception) -> None:
        fake_cosmos.fail("read", error=error)
        with pytest.raises(cosmos.Unavailable):
            cosmos.read("d1", PK)

    def test_a_missing_container_is_unavailable_not_a_missing_document(self, fake_cosmos: FakeCosmos) -> None:
        # Otherwise a deleted container would read as "nobody is a member".
        missing = CosmosHttpResponseError(status_code=404, message="owner resource does not exist")
        missing.headers = {"x-ms-substatus": "1003"}
        missing.sub_status = 1003
        fake_cosmos.fail("read", error=missing)
        with pytest.raises(cosmos.Unavailable):
            cosmos.read("me", PK)

    def test_errors_drop_the_sdk_chain_and_name_no_ids(self, fake_cosmos: FakeCosmos) -> None:
        oid = "a11ce000-0000-4000-8000-000000000001"
        cosmos.create({"id": oid, "pk": "users", "type": "user"})
        with pytest.raises(cosmos.AlreadyExists) as caught:
            cosmos.create({"id": oid, "pk": "users", "type": "user"})
        # A registry document's id is an oid, which must never reach a log via
        # an exception message or a chained SDK error.
        assert oid not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
        assert caught.value.status == 409


class TestQueries:
    def test_news_queries_must_name_a_partition(self, fake_cosmos: FakeCosmos) -> None:
        with pytest.raises(ValueError):
            cosmos.query("SELECT * FROM c")

    def test_the_legacy_container_still_allows_cross_partition(self, fake_cosmos: FakeCosmos) -> None:
        cosmos.upsert({"id": "a", "pk": "src", "type": "source_state"}, container="items")
        cosmos.upsert({"id": "b", "pk": "run", "type": "run"}, container="items")
        rows = cosmos.query("SELECT c.id FROM c", container="items")
        assert sorted(row["id"] for row in rows) == ["a", "b"]

    def test_parameters_top_and_order(self, fake_cosmos: FakeCosmos) -> None:
        for i in range(5):
            cosmos.create({"id": f"i{i}", "pk": "sys", "type": "invite", "created_at": f"2026-10-2{i}T00:00:00Z"})
        cosmos.create({"id": "other", "pk": "sys", "type": "audit", "created_at": "2026-10-29T00:00:00Z"})
        rows = cosmos.query(
            "SELECT TOP @limit c.id FROM c WHERE c.type = @type ORDER BY c.type ASC, c.created_at DESC",
            [{"name": "@limit", "value": 3}, {"name": "@type", "value": "invite"}],
            partition_key="sys",
        )
        assert [row["id"] for row in rows] == ["i4", "i3", "i2"]

    def test_a_filter_on_an_unindexed_path_fails_like_cosmos(self, fake_cosmos: FakeCosmos) -> None:
        # `news` uses opt-in indexing: Cosmos rejects such a query instead of
        # scanning, and the fake does too, so a test catches it before prod.
        with pytest.raises(cosmos.CosmosError) as caught:
            cosmos.query("SELECT * FROM c WHERE c.label = @l", [{"name": "@l", "value": "x"}], partition_key="sys")
        assert caught.value.status == 400

    def test_a_two_property_order_needs_its_composite_index(self, fake_cosmos: FakeCosmos) -> None:
        with pytest.raises(cosmos.CosmosError):
            cosmos.query("SELECT * FROM c ORDER BY c.status ASC, c.created_at DESC", partition_key="sys")

    def test_iter_ids_pages_through_a_partition(self, fake_cosmos: FakeCosmos) -> None:
        for i in range(7):
            cosmos.create(_doc(f"d{i}"))
        cosmos.create({"id": "elsewhere", "pk": "sys", "type": "x"})
        ids = []
        for doc_id in cosmos.iter_ids(PK, page_size=3):
            # Deleting what was already returned must not disturb the walk.
            cosmos.delete(doc_id, PK)
            ids.append(doc_id)
        assert sorted(ids) == [f"d{i}" for i in range(7)]
        assert fake_cosmos.docs(PK) == []


class TestBatch:
    def test_all_or_nothing(self, fake_cosmos: FakeCosmos) -> None:
        cosmos.create(_doc("taken"))
        with pytest.raises(cosmos.BatchFailed) as caught:
            cosmos.batch(PK, [("create", (_doc("new"),)), ("create", (_doc("taken"),))])
        assert (caught.value.index, caught.value.status) == (1, 409)
        assert cosmos.read("new", PK) is None

    def test_a_successful_batch_applies_every_operation(self, fake_cosmos: FakeCosmos) -> None:
        stored = cosmos.create(_doc("m", n=1))
        results = cosmos.batch(
            PK,
            [
                ("create", (_doc("a"),)),
                ("replace", ("m", {**stored, "n": 2}), {"if_match_etag": stored["_etag"]}),
                ("patch", ("a", [{"op": "incr", "path": "/k", "value": 1}]), {"filter_predicate": "FROM c WHERE NOT IS_DEFINED(c.k)"}),
            ],
        )
        assert len(results) == 3
        assert cosmos.read("m", PK)["n"] == 2 and cosmos.read("a", PK)["k"] == 1


class TestRuAccounting:
    def test_metered_sums_every_call_including_failures(self, fake_cosmos: FakeCosmos) -> None:
        with cosmos.metered() as meter:
            cosmos.create(_doc())
            cosmos.read("d1", PK)
            cosmos.read("missing", PK)  # a 404 is still charged
        assert meter.operations == 3
        assert meter.charge == pytest.approx(6.0 + 1.0 + 1.0)

    def test_nested_meters_both_see_every_call(self, fake_cosmos: FakeCosmos) -> None:
        with cosmos.metered() as outer:
            cosmos.create(_doc())
            with cosmos.metered() as inner:
                cosmos.read("d1", PK)
        assert inner.charge == pytest.approx(1.0)
        assert outer.charge == pytest.approx(7.0)

    def test_query_pages_are_each_charged(self, fake_cosmos: FakeCosmos) -> None:
        for i in range(5):
            cosmos.create(_doc(f"d{i}"))
        with cosmos.metered() as meter:
            list(cosmos.iter_ids(PK, page_size=2))
        assert meter.operations == 3


class TestPacer:
    def test_a_burst_within_one_second_does_not_wait(self) -> None:
        slept: list[float] = []
        pacer = cosmos.RuPacer(50, clock=lambda: 0.0, sleep=slept.append)
        pacer.spend(30)
        pacer.spend(20)
        assert slept == []

    def test_overdrawing_waits_exactly_the_debt(self) -> None:
        slept: list[float] = []
        pacer = cosmos.RuPacer(50, clock=lambda: 0.0, sleep=slept.append)
        pacer.spend(50)
        pacer.spend(25)
        assert slept == [pytest.approx(0.5)]

    def test_time_refills_the_bucket_up_to_one_second(self) -> None:
        now = [0.0]
        slept: list[float] = []
        pacer = cosmos.RuPacer(50, clock=lambda: now[0], sleep=slept.append)
        pacer.spend(50)
        now[0] = 10.0  # idle for ten seconds still buys only one second's burst
        pacer.spend(50)
        pacer.spend(50)
        assert slept == [pytest.approx(1.0)]

    def test_sustained_spend_stays_at_the_rate(self) -> None:
        clock = [0.0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        pacer = cosmos.RuPacer(50, clock=lambda: clock[0], sleep=sleep)
        for _ in range(100):
            pacer.spend(10)  # 1,000 RU in total
        # One second of burst, then 950 RU at 50 RU/s.
        assert clock[0] == pytest.approx(19.0)

    def test_paced_feeds_every_call_to_the_pacer(self, fake_cosmos: FakeCosmos, no_sleep: list[float]) -> None:
        with cosmos.paced(10) as pacer:
            for i in range(4):
                cosmos.create(_doc(f"d{i}"))  # 4 x 6 RU at 10 RU/s
        assert sum(no_sleep) == pytest.approx(pacer.slept_s) and pacer.slept_s > 0

    def test_rate_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            cosmos.RuPacer(0)


class TestLocalReadonly:
    def test_every_write_is_refused(self, fake_cosmos: FakeCosmos, env) -> None:
        cosmos.create(_doc())
        env(LOCAL_READONLY="true")
        writes = [
            lambda: cosmos.create(_doc("x")),
            lambda: cosmos.try_create(_doc("x")),
            lambda: cosmos.upsert(_doc("x")),
            lambda: cosmos.replace_if_match(_doc(), '"e"'),
            lambda: cosmos.patch("d1", PK, [{"op": "set", "path": "/n", "value": 1}]),
            lambda: cosmos.delete("d1", PK),
            lambda: cosmos.batch(PK, [("create", (_doc("x"),))]),
            lambda: cosmos.bulk_create([_doc("x")]),
        ]
        for write in writes:
            with pytest.raises(cosmos.WriteBlocked):
                write()
        # Reads are what local runs are for.
        assert cosmos.read("d1", PK) is not None
        assert fake_cosmos.writes()[-1].id == "d1"

    def test_the_override_is_explicit_scoped_and_logged(self, fake_cosmos: FakeCosmos, env, caplog: pytest.LogCaptureFixture) -> None:
        env(LOCAL_READONLY="1")
        with caplog.at_level("WARNING", logger="core.cosmos"):
            with cosmos.allow_prod_writes("migration step 3"):
                cosmos.create(_doc())
        assert "LOCAL_READONLY override: migration step 3" in caplog.text
        with pytest.raises(cosmos.WriteBlocked):
            cosmos.create(_doc("after"))

    def test_the_override_needs_a_reason(self) -> None:
        with pytest.raises(ValueError):
            with cosmos.allow_prod_writes("  "):
                pass

    def test_off_unless_set(self, fake_cosmos: FakeCosmos, env) -> None:
        env(LOCAL_READONLY="no")
        cosmos.create(_doc())


class TestContainers:
    def test_the_default_container_is_news(self, fake_cosmos: FakeCosmos) -> None:
        cosmos.create(_doc())
        assert fake_cosmos.get("d1", PK, container="news") is not None
        assert fake_cosmos.get("d1", PK, container="items") is None

    def test_legacy_writes_name_their_container(self, fake_cosmos: FakeCosmos) -> None:
        cosmos.upsert({"id": "heise", "pk": "src", "type": "source_state"}, container="items")
        assert cosmos.read("heise", "src", container="items") is not None
        assert cosmos.read("heise", "src") is None


class TestSettings:
    """core.config: the Phase 1 settings, read once, failing closed where the
    auth path depends on them."""

    def test_defaults_where_nothing_is_set(self, env) -> None:
        from core.config import get_settings

        env(
            COSMOS_CONTAINER=None, LEGACY_COSMOS_CONTAINER=None, ENTRA_TENANT_ID=None, ENTRA_TENANT_SUBDOMAIN=None,
            ENTRA_API_CLIENT_ID=None, ENTRA_ALLOWED_CLIENT_IDS=None, ENTRA_REQUIRED_SCOPE=None, ENTRA_ADMIN_ROLE=None,
            MYNEWS_HMAC_KEY=None, IDENTITY_DELETE_MODE=None, JOBS_STORAGE__queueServiceUri=None,
            JOBS_STORAGE__credential=None, LOCAL_READONLY=None,
        )
        s = get_settings()
        assert (s.cosmos_container, s.legacy_cosmos_container) == ("news", "items")
        assert (s.entra_tenant_id, s.entra_tenant_subdomain, s.entra_api_client_id, s.entra_allowed_client_ids) == ("", "", "", ())
        assert (s.entra_required_scope, s.entra_admin_role) == ("access_as_user", "admin")
        assert (s.hmac_key, s.identity_delete_mode, s.min_supported_build, s.apple_secret_renew_by) == ("", "manual", 0, "")
        assert (s.jobs_queue_service_uri, s.jobs_credential, s.local_readonly) == ("", "managedidentity", False)

    def test_client_ids_are_a_clean_lower_case_list(self, env) -> None:
        from core.config import get_settings

        env(ENTRA_ALLOWED_CLIENT_IDS=" 33333333-3333-4333-8333-33333333AAAA , ,44444444-4444-4444-8444-444444444444,")
        assert get_settings().entra_allowed_client_ids == (
            "33333333-3333-4333-8333-33333333aaaa",
            "44444444-4444-4444-8444-444444444444",
        )

    def test_an_unresolved_key_vault_reference_is_missing_and_named_not_shown(self, env, caplog: pytest.LogCaptureFixture) -> None:
        from core.config import get_settings

        reference = "@Microsoft.KeyVault(SecretUri=https://kv-mynews.vault.azure.net/secrets/hmac)"
        with caplog.at_level("WARNING", logger="core.config"):
            env(MYNEWS_HMAC_KEY=reference, ENTRA_REQUIRED_SCOPE=reference)
            s = get_settings()
        # Missing, not the default: a broken reference must not quietly fall back.
        assert (s.hmac_key, s.entra_required_scope) == ("", "")
        assert "MYNEWS_HMAC_KEY" in caplog.text and "kv-mynews" not in caplog.text

    def test_the_hmac_key_never_appears_in_a_repr(self) -> None:
        from core.config import get_settings
        from tests.fakes.jwks import HMAC_KEY

        assert get_settings().hmac_key == HMAC_KEY
        assert HMAC_KEY not in repr(get_settings())

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, 0), ("", 0), ("41", 41), (" 7 ", 7), ("abc", 0), ("4.2", 0), ("-3", 0)],
    )
    def test_min_supported_build_never_fails(self, env, raw: str | None, expected: int) -> None:
        from core.config import get_settings, min_supported_build

        env(MIN_SUPPORTED_BUILD=raw)
        assert min_supported_build() == expected
        assert get_settings().min_supported_build == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1", True), ("true", True), ("YES", True), ("True", True), ("0", False), ("false", False), ("no", False), ("", False)],
    )
    def test_local_readonly_values(self, env, raw: str, expected: bool) -> None:
        from core.config import get_settings

        env(LOCAL_READONLY=raw)
        assert get_settings().local_readonly is expected
