"""The functions that are not HTTP routes: the account-purge and ops-jobs queue
workers, the maintenance timer, and the v1 harvest they and the harvest timer
run (now in the legacy container, paced).

The queue workers get messages the way the host delivers them
(azure.functions.queue.QueueMessage with a dequeue count), and output bindings
are a stand-in with set()/get(), like the worker's. The purge's own order and
idempotence are core.accounts' tests; these hold the wiring: DELETE /me ->
message -> worker -> 410, the resume after a failed attempt, the last attempt's
alert, the maintenance re-enqueue, and that nothing in a log names an oid.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
from azure.functions.queue import QueueMessage

from core import cosmos, identity_removal
from core.jobs import OpsJobMessage, PurgeMessage
from core.models import RunRecord, iso, utcnow
from tests.fakes import jwks as ids
from tests.fakes.cosmos import FakeCosmos, throttled
from tests.test_function_app import indexed_functions
from tests.test_http_contract import ADMIN, Client, Out

ALICE_PK = f"user|{ids.OID_ALICE}"
BOB_PK = f"user|{ids.OID_BOB}"
HOST_JSON = Path(__file__).resolve().parents[1] / "host.json"


@pytest.fixture
def client(jwks: Any, fake_cosmos: FakeCosmos) -> Client:
    return Client(jwks)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Pacing sleeps are recorded, never taken."""
    calls: list[float] = []
    monkeypatch.setattr(cosmos.time, "sleep", calls.append)
    return calls


def _run(name: str, *args: Any, **kwargs: Any) -> Any:
    return indexed_functions()[name].get_user_function()(*args, **kwargs)


def _message(body: str | bytes, *, dequeue_count: int = 1) -> QueueMessage:
    return QueueMessage(
        id="msg-0001", body=body.encode() if isinstance(body, str) else body, dequeue_count=dequeue_count
    )


class Timer:
    past_due = False


def _join(client: Client, oid: str) -> dict[str, Any]:
    (invite,) = client.call("admin_invites_create", **ADMIN, body={"label": "Wave"}).body["invites"]
    resp = client.call("me_redeem", oid=oid, body={"code": invite["code"]})
    assert resp.status == 201
    return resp.body


def _seed(fake: FakeCosmos, pk: str, n: int) -> None:
    for i in range(n):
        fake.put({"id": f"fb|{i:04d}", "pk": pk, "type": "fb", "signal": "open", "created_at": "2026-10-20T00:00:00Z"})


def _delete(client: Client, oid: str = ids.OID_ALICE) -> str:
    resp = client.call("me_delete", oid=oid)
    assert resp.status == 202
    message = client.outputs["purge"].get()
    assert message is not None
    return message


# ---------------------------------------------------------------------------
# account-purge
# ---------------------------------------------------------------------------
class TestPurgeWorker:
    def test_delete_then_purge_then_410(
        self, client: Client, fake_cosmos: FakeCosmos, caplog: pytest.LogCaptureFixture
    ) -> None:
        alice = _join(client, ids.OID_ALICE)
        bob = _join(client, ids.OID_BOB)
        _seed(fake_cosmos, ALICE_PK, 7)
        _seed(fake_cosmos, BOB_PK, 3)
        before_bob = fake_cosmos.docs(BOB_PK)

        with caplog.at_level(logging.INFO):
            assert _run("account_purge_worker", _message(_delete(client))) is None

        # The partition and the registry document are gone; the tombstone and
        # the deletion record remain, keyed by uid.
        assert fake_cosmos.docs(ALICE_PK) == []
        assert fake_cosmos.get(ids.OID_ALICE, "users") is None
        assert fake_cosmos.get(f"tombstone|{alice['uid']}", "sys")["identity_removed"] is False
        record = fake_cosmos.get(f"deletion|{alice['uid']}", "sys")
        # 7 signals, the redeem quota counter and me.
        assert (record["status"], record["docs_deleted"]) == ("awaiting_identity_removal", 8)
        gone = client.call("me_get")
        assert (gone.status, gone.body["error"]["details"]) == (410, {"state": "deleted"})
        # Bob is untouched.
        assert fake_cosmos.docs(BOB_PK) == before_bob
        assert client.call("me_get", oid=ids.OID_BOB).body["uid"] == bob["uid"]
        # The log names the uid, never the oid.
        assert alice["uid"] in caplog.text and ids.OID_ALICE not in caplog.text

    def test_a_failed_attempt_resumes_on_redelivery(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        alice = _join(client, ids.OID_ALICE)
        _seed(fake_cosmos, ALICE_PK, 6)
        message = _delete(client)
        fake_cosmos.fail("delete", id="fb|0003", pk=ALICE_PK, error=throttled)

        with pytest.raises(cosmos.Unavailable):
            _run("account_purge_worker", _message(message, dequeue_count=1))
        record = fake_cosmos.get(f"deletion|{alice['uid']}", "sys")
        assert (record["status"], record["attempts"], record["last_error"]) == ("purging", 1, "Unavailable")
        # The quota counter and fb|0000-0002 went before fb|0003 failed, and
        # the count was checkpointed on the way out.
        assert record["docs_deleted"] == 4
        assert fake_cosmos.get("me", ALICE_PK) is not None  # me goes last

        _run("account_purge_worker", _message(message, dequeue_count=2))
        record = fake_cosmos.get(f"deletion|{alice['uid']}", "sys")
        assert (record["status"], record["attempts"], record["last_error"]) == ("awaiting_identity_removal", 2, None)
        assert record["docs_deleted"] == 7  # 6 signals and the quota counter, each counted once
        assert fake_cosmos.docs(ALICE_PK) == []

    def test_a_redelivery_after_success_changes_nothing(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client, ids.OID_ALICE)
        message = _delete(client)
        _run("account_purge_worker", _message(message))
        writes = len(fake_cosmos.writes())
        _run("account_purge_worker", _message(message, dequeue_count=1))
        audit = [d for d in fake_cosmos.docs("sys") if d["type"] == "audit" and d["action"] == "account.purged"]
        assert len(audit) == 1
        # At most the attempt counter and the (no-op) registry delete moved.
        assert len(fake_cosmos.writes()) - writes <= 2

    def test_the_last_attempt_alerts_before_the_poison_queue(
        self, client: Client, fake_cosmos: FakeCosmos, caplog: pytest.LogCaptureFixture
    ) -> None:
        alice = _join(client, ids.OID_ALICE)
        _seed(fake_cosmos, ALICE_PK, 2)
        message = _delete(client)
        fake_cosmos.fail("delete", pk=ALICE_PK, error=throttled, times=10)
        with caplog.at_level(logging.ERROR):
            with pytest.raises(cosmos.Unavailable):
                _run("account_purge_worker", _message(message, dequeue_count=2))
            assert "ALERT purge_failed" not in caplog.text
            with pytest.raises(cosmos.Unavailable):
                _run("account_purge_worker", _message(message, dequeue_count=3))
        (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ALERT purge_failed")]
        assert json.loads(line.split(" ", 2)[2]) == {"attempts": 3, "uid": alice["uid"]}
        assert ids.OID_ALICE not in caplog.text

    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"\xff\xfe",
            '{"v": 2, "kind": "account", "oid": "a11ce000-0000-4000-8000-000000000001", "requestedAt": "2026-10-21T10:00:00Z"}',
            '{"v": 1, "kind": "everything", "oid": "a11ce000-0000-4000-8000-000000000001", "requestedAt": "2026-10-21T10:00:00Z"}',
            '{"v": 1, "kind": "account", "oid": "A11CE000-0000-4000-8000-000000000001", "requestedAt": "2026-10-21T10:00:00Z"}',
            '{"v": 1, "kind": "account", "oid": "a11ce000-0000-4000-8000-000000000001", "requestedAt": "yesterday"}',
            "[]",
        ],
    )
    def test_an_unreadable_message_is_acknowledged_with_a_log_line(
        self, fake_cosmos: FakeCosmos, caplog: pytest.LogCaptureFixture, body: str | bytes
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="jobs.queues"):
            assert _run("account_purge_worker", _message(body)) is None
        assert "acknowledged an unreadable message id=msg-0001" in caplog.text
        assert fake_cosmos.journal == []
        assert "A11CE000" not in caplog.text and "a11ce000" not in caplog.text

    def test_a_history_purge_waits_for_phase_3(self, fake_cosmos: FakeCosmos, caplog: pytest.LogCaptureFixture) -> None:
        message = PurgeMessage(oid=ids.OID_ALICE, requested_at=iso(utcnow()), kind="history").to_json()
        with caplog.at_level(logging.WARNING, logger="jobs.queues"):
            _run("account_purge_worker", _message(message))
        assert "not handled before Phase 3" in caplog.text
        assert fake_cosmos.journal == []

    def test_a_message_alone_never_deletes_an_active_account(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client, ids.OID_BOB)
        _seed(fake_cosmos, BOB_PK, 3)
        before = fake_cosmos.docs(BOB_PK)
        forged = PurgeMessage(oid=ids.OID_BOB, requested_at=iso(utcnow())).to_json()
        _run("account_purge_worker", _message(forged))
        assert fake_cosmos.docs(BOB_PK) == before
        assert client.call("me_get", oid=ids.OID_BOB).status == 200


# ---------------------------------------------------------------------------
# maintenance_timer
# ---------------------------------------------------------------------------
def _later(monkeypatch: pytest.MonkeyPatch, delta: timedelta) -> datetime:
    import jobs.timers

    at = utcnow() + delta
    monkeypatch.setattr(jobs.timers, "utcnow", lambda: at)
    return at


class TestMaintenance:
    def test_a_deletion_whose_message_was_lost_is_re_enqueued_after_an_hour(
        self, client: Client, fake_cosmos: FakeCosmos, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _join(client, ids.OID_ALICE)
        _delete(client)  # ...and the message never arrives

        early = Out()
        _later(monkeypatch, timedelta(minutes=30))
        _run("maintenance_timer", Timer(), purge=early)
        assert early.values == []  # not stuck yet: the worker may still be on it

        late = Out()
        _later(monkeypatch, timedelta(hours=2))
        _run("maintenance_timer", Timer(), purge=late)
        (batch,) = late.values
        (message,) = batch
        assert PurgeMessage.from_json(message).oid == ids.OID_ALICE

        _run("account_purge_worker", _message(message))
        assert fake_cosmos.docs(ALICE_PK) == []
        assert client.call("me_get").body["error"]["details"] == {"state": "deleted"}

    def test_a_purge_that_died_is_re_enqueued_once_per_account(
        self, client: Client, fake_cosmos: FakeCosmos, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _join(client, ids.OID_ALICE)
        _seed(fake_cosmos, ALICE_PK, 2)
        message = _delete(client)
        fake_cosmos.fail("delete", pk=ALICE_PK, error=throttled)
        with pytest.raises(cosmos.Unavailable):
            _run("account_purge_worker", _message(message))
        out = Out()
        _later(monkeypatch, timedelta(hours=3))
        _run("maintenance_timer", Timer(), purge=out)
        # Stuck both ways (registry deleting, record purging): one message.
        assert [PurgeMessage.from_json(m).oid for m in out.get()] == [ids.OID_ALICE]

    def test_overdue_identity_removals_alert(
        self, client: Client, fake_cosmos: FakeCosmos, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _join(client, ids.OID_ALICE)
        _run("account_purge_worker", _message(_delete(client)))
        _later(monkeypatch, timedelta(days=8))
        with caplog.at_level(logging.ERROR, logger="core.audit"):
            _run("maintenance_timer", Timer(), purge=Out())
        assert 'ALERT identity_removal_overdue {"count": 1}' in caplog.text
        assert ids.OID_ALICE not in caplog.text

    @pytest.mark.parametrize(
        ("days", "alerts"),
        [(40, False), (31, False), (30, True), (3, True), (-2, True), (None, False)],
    )
    def test_the_apple_secret_reminder(
        self, fake_cosmos: FakeCosmos, env, caplog: pytest.LogCaptureFixture, days: int | None, alerts: bool
    ) -> None:
        renew_by = None if days is None else f"{(utcnow() + timedelta(days=days)).date():%Y-%m-%d}"
        env(APPLE_SECRET_RENEW_BY=renew_by)
        with caplog.at_level(logging.ERROR, logger="core.audit"):
            _run("maintenance_timer", Timer(), purge=Out())
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ALERT secret_expiry")]
        if not alerts:
            assert lines == []
        else:
            (line,) = lines
            assert json.loads(line.split(" ", 2)[2]) == {"secret": "apple", "renewBy": renew_by, "daysLeft": days}

    def test_a_renewal_date_that_is_not_a_date_alerts_too(
        self, fake_cosmos: FakeCosmos, env, caplog: pytest.LogCaptureFixture
    ) -> None:
        env(APPLE_SECRET_RENEW_BY="2027-13-01")
        with caplog.at_level(logging.ERROR, logger="core.audit"):
            _run("maintenance_timer", Timer(), purge=Out())
        assert '"problem": "not_a_date"' in caplog.text

    def test_a_database_outage_fails_the_run_without_losing_the_reminder(
        self, fake_cosmos: FakeCosmos, env, caplog: pytest.LogCaptureFixture
    ) -> None:
        env(APPLE_SECRET_RENEW_BY=f"{(utcnow() + timedelta(days=5)).date():%Y-%m-%d}")
        fake_cosmos.fail("query", error=throttled, times=10)
        out = Out()
        with caplog.at_level(logging.ERROR):
            with pytest.raises(cosmos.Unavailable):
                _run("maintenance_timer", Timer(), purge=out)
        assert "ALERT secret_expiry" in caplog.text
        assert out.values == []

    def test_a_failed_overdue_count_does_not_stop_the_sweep(
        self, client: Client, fake_cosmos: FakeCosmos, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _join(client, ids.OID_ALICE)
        _delete(client)

        def broken(now: datetime) -> int:
            raise cosmos.Unavailable("query: HTTP 503")

        monkeypatch.setattr(identity_removal, "count_overdue", broken)
        out = Out()
        _later(monkeypatch, timedelta(hours=2))
        with caplog.at_level(logging.ERROR, logger="jobs.timers"):
            _run("maintenance_timer", Timer(), purge=out)
        assert "could not count overdue identity removals" in caplog.text
        assert len(out.get()) == 1

    def test_nothing_to_do_sets_no_output(self, fake_cosmos: FakeCosmos) -> None:
        out = Out()
        _run("maintenance_timer", Timer(), purge=out)
        assert out.values == []


# ---------------------------------------------------------------------------
# ops-jobs
# ---------------------------------------------------------------------------
class TestOpsWorker:
    def test_a_harvest_job_runs_a_forced_harvest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pipeline.harvest

        calls: list[dict[str, Any]] = []

        def fake_run_harvest(**kwargs: Any) -> RunRecord:
            calls.append(kwargs)
            return RunRecord(id="harvest-test", items_new=2)

        monkeypatch.setattr(pipeline.harvest, "run_harvest", fake_run_harvest)
        job = OpsJobMessage.new("harvest", requested_by="system", now=utcnow(), source_id="heise")
        _run("ops_jobs_worker", _message(job.to_json()))
        everything = OpsJobMessage.new("harvest", requested_by="f" * 32, now=utcnow(), force=False)
        _run("ops_jobs_worker", _message(everything.to_json()))
        assert calls == [{"only_source": "heise", "force": True}, {"only_source": None, "force": False}]

    @pytest.mark.parametrize("body", ["{}", '{"v": 1, "kind": "cycle", "jobId": "x"}', "not json"])
    def test_an_unreadable_job_is_acknowledged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, body: str
    ) -> None:
        import pipeline.harvest

        monkeypatch.setattr(pipeline.harvest, "run_harvest", lambda **kwargs: pytest.fail("must not run"))
        with caplog.at_level(logging.ERROR, logger="jobs.queues"):
            _run("ops_jobs_worker", _message(body))
        assert "ops-jobs: acknowledged an unreadable message" in caplog.text

    def test_a_failed_harvest_is_retried_by_the_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pipeline.harvest

        def broken(**kwargs: Any) -> RunRecord:
            raise cosmos.Unavailable("upsert: HTTP 503")

        monkeypatch.setattr(pipeline.harvest, "run_harvest", broken)
        with pytest.raises(cosmos.Unavailable):
            _run(
                "ops_jobs_worker", _message(OpsJobMessage.new("harvest", requested_by="system", now=utcnow()).to_json())
            )


# ---------------------------------------------------------------------------
# The v1 harvest: the legacy container, paced
# ---------------------------------------------------------------------------
def _feed(n: int, now: datetime) -> bytes:
    items = "".join(
        f"<item><title>Story {i}</title><link>https://example.com/story-{i}</link>"
        f"<description>{'A lead long enough to keep. ' * 10}</description>"
        f"<pubDate>{format_datetime(now - timedelta(minutes=i + 1))}</pubDate></item>"
        for i in range(n)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Example</title>'
        f"<link>https://example.com/</link><description>Example feed</description>{items}</channel></rss>"
    ).encode()


@pytest.fixture
def harvest(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """One example source whose fetch returns a feed of ``state["items"]``
    items, recording the pacers open at each fetch."""
    import pipeline.harvest
    from core.sources import Source
    from fetch.http import FetchResult

    state: dict[str, Any] = {"items": 3, "fetches": 0, "rates": []}
    source = Source(id="example", name="Example", url="https://example.com/feed", poll_minutes=60)

    def fetch(url: str, source_state: Any, client: Any = None) -> FetchResult:
        state["fetches"] += 1
        state["rates"].append([pacer.rate_per_s for pacer in cosmos._PACERS.get()])
        return FetchResult(
            ok=True,
            status=200,
            body=_feed(state["items"], datetime.now(timezone.utc)),
            body_sha256=f"sha-{state['fetches']}",
        )

    monkeypatch.setattr(pipeline.harvest, "load_sources", lambda: [source])
    monkeypatch.setattr(pipeline.harvest, "fetch_feed", fetch)
    return state


class TestLegacyHarvest:
    def test_every_call_goes_to_the_legacy_container(self, fake_cosmos: FakeCosmos, harvest: dict[str, Any]) -> None:
        from pipeline.harvest import run_harvest

        record = run_harvest()
        assert (record.items_new, record.ok) == (3, True)
        assert {op.container for op in fake_cosmos.journal} == {"items"}
        assert [d["type"] for d in fake_cosmos.docs("src", container="items")] == ["source_state"]
        assert [d["id"] for d in fake_cosmos.docs("run", container="items")] == [record.id]
        assert len([d for d in fake_cosmos.docs(container="items") if d["type"] == "article"]) == 3
        assert "news" not in fake_cosmos.containers or fake_cosmos.docs() == []

    def test_the_run_is_paced_at_350_ru_per_second(
        self, fake_cosmos: FakeCosmos, harvest: dict[str, Any], sleeps: list[float]
    ) -> None:
        from pipeline.harvest import run_harvest

        harvest["items"] = 100  # 100 creates at ~6 RU each: more than a second's budget
        run_harvest()
        assert harvest["rates"] == [[cosmos.RU_RATE_LEGACY_HARVEST]] and cosmos.RU_RATE_LEGACY_HARVEST == 350
        assert sleeps and sum(sleeps) > 0

    def test_force_skips_the_schedule(self, fake_cosmos: FakeCosmos, harvest: dict[str, Any]) -> None:
        from pipeline.harvest import run_harvest

        run_harvest()
        assert harvest["fetches"] == 1
        run_harvest()  # polled a moment ago: not due for another hour
        assert harvest["fetches"] == 1
        record = run_harvest(force=True)
        assert harvest["fetches"] == 2 and record.feeds_polled == 1
        assert run_harvest(only_source="other", force=True).feeds_polled == 0

    def test_the_source_list_lives_in_the_legacy_container(self, fake_cosmos: FakeCosmos) -> None:
        from core.sources import load_sources

        seeded = load_sources()
        assert seeded and {op.container for op in fake_cosmos.journal} == {"items"}
        assert fake_cosmos.get("sources", "user|me", container="items")["type"] == "config"
        assert [s.id for s in load_sources()] == [s.id for s in seeded]


# ---------------------------------------------------------------------------
# host.json
# ---------------------------------------------------------------------------
def test_host_json_queue_settings_match_the_workers() -> None:
    from jobs import queues

    host = json.loads(HOST_JSON.read_text(encoding="utf-8"))
    assert host["functionTimeout"] == "00:20:00"
    assert host["extensions"]["queues"] == {
        "batchSize": 4,
        "newBatchThreshold": 2,
        "maxDequeueCount": queues.MAX_DEQUEUE_COUNT,
        "visibilityTimeout": "00:00:30",
        "maxPollingInterval": "00:00:05",
    }
