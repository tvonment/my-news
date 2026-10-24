"""core.accounts and core.identity_removal: deletion request, the purge order
and its idempotent resume, the export, the admin user views and overview, and
manual identity removal. Also the queue contract (core.jobs) and the deep
admin health check (core.ops_health), which watch the same deletions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from core import accounts, cosmos, identity_removal, invites, members
from core.auth import AuthContext
from core.errors import ApiError
from core.jobs import PurgeMessage
from tests.fakes import jwks as ids
from tests.fakes.cosmos import FakeCosmos

NOW = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
ALICE_PK = f"user|{ids.OID_ALICE}"
ADMIN_UID = "f" * 32


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The purge is paced at 50 RU/s; record its sleeps instead of taking them."""
    calls: list[float] = []
    monkeypatch.setattr(cosmos.time, "sleep", calls.append)
    return calls


@pytest.fixture
def alice(make_principal):
    return make_principal(ids.OID_ALICE)


@pytest.fixture
def admin(make_principal):
    return make_principal(ids.OID_ADMIN, roles=("admin",))


def _join(fake: FakeCosmos, principal, *, max_uses: int = 1) -> invites.MintedInvite:
    minted = invites.mint(label="Wave", max_uses=max_uses, expires_in_days=30, count=1, actor_uid=ADMIN_UID, now=NOW)[0]
    invites.redeem(principal, minted.code, accept_language="de-CH", now=NOW)
    return minted


def _ctx(fake: FakeCosmos, principal, *, allow: bool = True) -> AuthContext:
    me = members.gate(principal, allow_blocked=allow, allow_deleting=allow, now=NOW)
    return AuthContext("req-test-0001", "member", principal, me)


def _seed_partition(fake: FakeCosmos, oid: str, n: int) -> None:
    pk = f"user|{oid}"
    for i in range(n):
        fake.put({"id": f"fb|{i:04d}", "pk": pk, "type": "fb", "signal": "open", "created_at": "2026-10-20T00:00:00Z"})
    fake.put({"id": "save|s20261020abcdefghijkl", "pk": pk, "type": "save", "created_at": "2026-10-20T00:00:00Z"})


def _seed_subs(fake: FakeCosmos, oid: str, sources: dict[str, str]) -> None:
    fake.put({"id": "subs", "pk": f"user|{oid}", "type": "subs", "sources": {k: {"state": v} for k, v in sources.items()}})
    for source_id in sources:
        fake.put({"id": f"src|{source_id}", "pk": "catalog", "type": "source", "subscriber_count": 3})


def _purge_message(fake: FakeCosmos, principal) -> PurgeMessage:
    _, message = accounts.request_deletion(_ctx(fake, principal), now=NOW)
    assert message is not None
    return PurgeMessage.from_json(message)


def _audit(fake: FakeCosmos, action: str) -> list[dict]:
    return [d for d in fake.docs("sys") if d["type"] == "audit" and d["action"] == action]


class TestRequestDeletion:
    def test_marks_me_then_the_registry_and_queues_one_message(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        before = len(fake_cosmos.writes())
        body, message = accounts.request_deletion(_ctx(fake_cosmos, alice), now=NOW)
        assert body == {"state": "deleting", "identityRemoval": "manual"}
        assert json.loads(message) == {"v": 1, "kind": "account", "oid": ids.OID_ALICE, "requestedAt": "2026-10-21T10:00:00Z"}
        writes = [(op.op, op.pk) for op in fake_cosmos.writes()[before:]]
        assert writes[:2] == [("replace", ALICE_PK), ("replace", "users")]
        for doc in (fake_cosmos.get("me", ALICE_PK), fake_cosmos.get(ids.OID_ALICE, "users")):
            assert (doc["status"], doc["deletion_requested_at"]) == ("deleting", "2026-10-21T10:00:00Z")
        assert [e["actor"] for e in _audit(fake_cosmos, "account.deletion_requested")] == [alice.uid]
        # From now on every other route answers 410.
        with pytest.raises(ApiError) as caught:
            members.gate(alice, now=NOW)
        assert (caught.value.status, caught.value.details) == (410, {"state": "deleting"})

    def test_a_repeat_sends_no_second_message(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        accounts.request_deletion(_ctx(fake_cosmos, alice), now=NOW)
        before = len(fake_cosmos.writes())
        body, message = accounts.request_deletion(_ctx(fake_cosmos, alice), now=NOW + timedelta(minutes=1))
        assert body["state"] == "deleting" and message is None
        assert len(fake_cosmos.writes()) == before
        assert len(_audit(fake_cosmos, "account.deletion_requested")) == 1

    def test_a_repeat_finishes_what_a_crashed_attempt_left(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        fake_cosmos.fail("replace", pk="users")  # died between the two writes, before its message
        with pytest.raises(cosmos.Unavailable):
            accounts.request_deletion(_ctx(fake_cosmos, alice), now=NOW)
        assert fake_cosmos.get(ids.OID_ALICE, "users")["status"] == "active"
        _, message = accounts.request_deletion(_ctx(fake_cosmos, alice), now=NOW + timedelta(minutes=1))
        assert message is not None and json.loads(message)["requestedAt"] == "2026-10-21T10:00:00Z"
        assert fake_cosmos.get(ids.OID_ALICE, "users")["status"] == "deleting"

    def test_a_blocked_member_may_delete_and_the_block_marks_go(self, fake_cosmos: FakeCosmos, alice, admin) -> None:
        _join(fake_cosmos, alice)
        members.gate(admin, now=NOW)
        accounts.set_blocked(alice.uid, blocked=True, reason="spam", actor=admin, now=NOW)
        accounts.request_deletion(_ctx(fake_cosmos, alice), now=NOW)
        assert "blocked_at" not in fake_cosmos.get("me", ALICE_PK)
        registry = fake_cosmos.get(ids.OID_ALICE, "users")
        assert "blocked_at" not in registry and "blocked_by" not in registry

    def test_graph_mode_is_not_promised_before_it_exists(self, fake_cosmos: FakeCosmos, alice, env) -> None:
        env(IDENTITY_DELETE_MODE="graph")
        _join(fake_cosmos, alice)
        body, _ = accounts.request_deletion(_ctx(fake_cosmos, alice), now=NOW)
        assert body["identityRemoval"] == "manual"


class TestPurge:
    def test_the_documented_order(self, fake_cosmos: FakeCosmos, alice, make_principal) -> None:
        bob = make_principal(ids.OID_BOB)
        minted = _join(fake_cosmos, alice, max_uses=2)
        invites.redeem(bob, minted.code, accept_language=None, now=NOW)
        _seed_partition(fake_cosmos, ids.OID_ALICE, 5)
        _seed_subs(fake_cosmos, ids.OID_ALICE, {"heise": "subscribed", "srf": "paused", "nzz": "removed"})
        message = _purge_message(fake_cosmos, alice)
        journal_start = len(fake_cosmos.journal)

        outcome = accounts.purge_account(message, now=NOW + timedelta(minutes=1))

        assert outcome == accounts.PurgeOutcome(alice.uid, "purged", 8, None)  # 5 fb + save + subs + quota
        journal = fake_cosmos.journal[journal_start:]

        def first(op: str, **match: str) -> int:
            return next(i for i, e in enumerate(journal) if e.op == op and e.ok and all(getattr(e, k) == v for k, v in match.items()))

        def last(op: str, **match: str) -> int:
            return max(i for i, e in enumerate(journal) if e.op == op and e.ok and all(getattr(e, k) == v for k, v in match.items()))

        partition_deletes = [i for i, e in enumerate(journal) if e.op == "delete" and e.ok and e.pk == ALICE_PK and e.id != "me"]
        # subs is read before anything is deleted, and each source is recorded
        # before its count is decremented.
        assert first("read", id="subs") < min(partition_deletes)
        assert first("patch", id=f"deletion|{alice.uid}") < first("patch", id="src|heise")
        # The tombstone exists before me goes; the registry goes last of all.
        assert max(partition_deletes) < first("create", id=f"tombstone|{alice.uid}")
        assert first("create", id=f"tombstone|{alice.uid}") < first("delete", id="me")
        assert first("delete", id="me") < first("delete", id=ids.OID_ALICE) == last("delete")

        assert fake_cosmos.docs(ALICE_PK) == []
        assert fake_cosmos.get(ids.OID_ALICE, "users") is None
        tombstone = fake_cosmos.get(f"tombstone|{alice.uid}", "sys")
        assert (tombstone["uid"], tombstone["identity_removed"], tombstone["ttl"]) == (alice.uid, False, -1)
        deletion = fake_cosmos.get(f"deletion|{alice.uid}", "sys")
        assert (deletion["status"], deletion["docs_deleted"], deletion["purged_at"]) == (
            "awaiting_identity_removal", 8, "2026-10-21T10:01:00Z",
        )
        assert deletion["oid"] == ids.OID_ALICE and deletion["created_at"] == "2026-10-21T10:00:00Z"
        assert deletion["decremented_source_ids"] == ["heise", "srf"]
        assert deletion["anonymised"]["invite"] is True
        # Counts given back once, for subscribed and paused, not removed.
        counts = {s: fake_cosmos.get(f"src|{s}", "catalog")["subscriber_count"] for s in ("heise", "srf", "nzz")}
        assert counts == {"heise": 2, "srf": 2, "nzz": 3}
        # The invite keeps the used slot; only the uid is gone.
        doc = fake_cosmos.get(f"invite|{minted.invite_id}", "sys")
        assert [r["uid"] for r in doc["redemptions"]] == ["deleted", bob.uid] and doc["uses"] == 2
        # Bob is untouched.
        assert fake_cosmos.get("me", f"user|{ids.OID_BOB}")["status"] == "active"
        # After the purge the account reads as deleted, not as a stranger.
        with pytest.raises(ApiError) as caught:
            members.gate(alice, now=NOW)
        assert (caught.value.status, caught.value.details) == (410, {"state": "deleted"})

    def test_no_oid_reaches_the_audit_trail(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        accounts.purge_account(_purge_message(fake_cosmos, alice), now=NOW)
        trail = json.dumps([d for d in fake_cosmos.docs("sys") if d["type"] == "audit"])
        assert ids.OID_ALICE not in trail
        assert [e["target"] for e in _audit(fake_cosmos, "account.purged")] == [{"type": "deletion", "id": alice.uid}]

    def test_the_purge_is_paced(self, fake_cosmos: FakeCosmos, alice, no_sleep: list[float]) -> None:
        _join(fake_cosmos, alice)
        _seed_partition(fake_cosmos, ids.OID_ALICE, 40)
        accounts.purge_account(_purge_message(fake_cosmos, alice), now=NOW)
        # ~40 deletes at ~6 RU is far more than one second's 50 RU burst.
        assert sum(no_sleep) > 3

    @pytest.mark.parametrize(
        ("op", "match"),
        [
            ("read", {"id": "subs"}),
            ("patch", {"id": "src|heise"}),
            ("delete", {"id": "fb|0002"}),
            ("patch", {"id": "invite|*"}),
            ("create", {"id": "tombstone|*"}),
            ("delete", {"id": "me"}),
            ("delete", {"pk": "users"}),
            ("patch", {"id": "deletion|*", "after": True}),
        ],
    )
    def test_a_crash_at_any_step_resumes_to_the_same_end(self, fake_cosmos: FakeCosmos, alice, op: str, match: dict) -> None:
        minted = _join(fake_cosmos, alice)
        _seed_partition(fake_cosmos, ids.OID_ALICE, 5)
        _seed_subs(fake_cosmos, ids.OID_ALICE, {"heise": "subscribed"})
        message = _purge_message(fake_cosmos, alice)
        kwargs = {k: v for k, v in match.items() if k != "after"}
        for key, value in list(kwargs.items()):
            if value.endswith("|*"):
                kwargs[key] = {"invite|*": f"invite|{minted.invite_id}", "tombstone|*": f"tombstone|{alice.uid}",
                               "deletion|*": f"deletion|{alice.uid}"}[value]
        fake_cosmos.fail(op, after=match.get("after", False), **kwargs)

        with pytest.raises(cosmos.Unavailable):
            accounts.purge_account(message, now=NOW)
        # The maintenance timer, or the queue's own retry, runs it again.
        outcome = accounts.purge_account(message, now=NOW + timedelta(hours=2))

        assert outcome.status in ("purged", "finalised")
        assert fake_cosmos.docs(ALICE_PK) == []
        assert fake_cosmos.get(ids.OID_ALICE, "users") is None
        assert fake_cosmos.get(f"tombstone|{alice.uid}", "sys") is not None
        deletion = fake_cosmos.get(f"deletion|{alice.uid}", "sys")
        assert deletion["status"] == "awaiting_identity_removal"
        assert deletion["docs_deleted"] == 8  # 5 fb + save + subs + quota, counted once
        # Never decremented twice. A crash between recording a source and
        # decrementing it (the decrement failed, or the record landed but its
        # answer was lost) leaves the count one too high, by design.
        between = bool(match.get("after")) or match.get("id") == "src|heise"
        assert fake_cosmos.get("src|heise", "catalog")["subscriber_count"] == (3 if between else 2)
        assert len(_audit(fake_cosmos, "account.purged")) == 1

    def test_a_crash_between_recording_and_decrementing_never_doubles(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        _seed_subs(fake_cosmos, ids.OID_ALICE, {"heise": "subscribed"})
        message = _purge_message(fake_cosmos, alice)
        fake_cosmos.fail("patch", id="src|heise")
        with pytest.raises(cosmos.Unavailable):
            accounts.purge_account(message, now=NOW)
        accounts.purge_account(message, now=NOW)
        assert fake_cosmos.get("src|heise", "catalog")["subscriber_count"] == 3  # one too high, by design

    def test_resumed_attempts_are_counted(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        message = _purge_message(fake_cosmos, alice)
        fake_cosmos.fail("delete", id="me")
        with pytest.raises(cosmos.Unavailable):
            accounts.purge_account(message, now=NOW)
        deletion = fake_cosmos.get(f"deletion|{alice.uid}", "sys")
        assert (deletion["attempts"], deletion["last_error"], deletion["status"]) == (1, "Unavailable", "purging")
        accounts.purge_account(message, now=NOW)
        deletion = fake_cosmos.get(f"deletion|{alice.uid}", "sys")
        assert (deletion["attempts"], deletion["last_error"]) == (2, None)

    def test_a_message_alone_never_deletes_an_active_account(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        outcome = accounts.purge_account(PurgeMessage(oid=ids.OID_ALICE, requested_at="2026-10-21T10:00:00Z"), now=NOW)
        assert (outcome.status, outcome.reason) == ("skipped", "not_deleting")
        assert fake_cosmos.get("me", ALICE_PK)["status"] == "active"

    def test_nothing_to_do_without_membership_or_tombstone(self, fake_cosmos: FakeCosmos) -> None:
        outcome = accounts.purge_account(PurgeMessage(oid=ids.OID_BOB, requested_at="2026-10-21T10:00:00Z"), now=NOW)
        assert (outcome.status, outcome.reason) == ("skipped", "no_membership")
        assert fake_cosmos.writes() == []

    def test_a_redelivery_after_completion_records_nothing_new(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        message = _purge_message(fake_cosmos, alice)
        accounts.purge_account(message, now=NOW)
        outcome = accounts.purge_account(message, now=NOW + timedelta(days=1))
        assert outcome.status == "finalised"
        assert len(_audit(fake_cosmos, "account.purged")) == 1
        assert fake_cosmos.get(f"deletion|{alice.uid}", "sys")["purged_at"] == "2026-10-21T10:00:00Z"

    def test_history_purges_wait_for_phase_3(self, fake_cosmos: FakeCosmos) -> None:
        message = PurgeMessage(oid=ids.OID_ALICE, requested_at="2026-10-21T10:00:00Z", kind="history")
        outcome = accounts.purge_account(message, now=NOW)
        assert (outcome.status, outcome.reason) == ("skipped", "unsupported_kind")


class TestStuckDeletions:
    def test_finds_lost_messages_and_dead_workers_once_each(self, fake_cosmos: FakeCosmos, alice, make_principal) -> None:
        bob = make_principal(ids.OID_BOB)
        carol = make_principal("c0000000-0000-4000-8000-000000000004")
        for person in (alice, bob, carol):
            _join(fake_cosmos, person)
        # Alice asked two hours ago and her message was lost.
        accounts.request_deletion(_ctx(fake_cosmos, alice), now=NOW - timedelta(hours=2))
        # Bob asked ten minutes ago: not stuck yet.
        accounts.request_deletion(_ctx(fake_cosmos, bob), now=NOW - timedelta(minutes=10))
        # Carol's worker started two hours ago and died after the registry went.
        message = _purge_message(fake_cosmos, carol)
        fake_cosmos.fail("delete", id="me", pk=f"user|{carol.oid}")
        with pytest.raises(cosmos.Unavailable):
            accounts.purge_account(message, now=NOW - timedelta(hours=2))
        found = accounts.stuck_deletions(NOW)
        assert sorted(m.oid for m in found) == sorted([alice.oid, carol.oid])
        assert all(m.kind == "account" for m in found)


class TestExport:
    def test_everything_as_stored_minus_system_fields_and_quotas(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        _seed_partition(fake_cosmos, ids.OID_ALICE, 2)
        body, filename = accounts.build_export(_ctx(fake_cosmos, alice), now=NOW)
        assert filename == "mynews-export-2026-10-21.json"
        assert (body["schema"], body["exportedAt"]) == ("mynews-export/1", "2026-10-21T10:00:00Z")
        assert body["account"] == members.me_to_wire(fake_cosmos.get("me", ALICE_PK), alice)
        types = [(d["type"], d["id"]) for d in body["documents"]]
        assert types[0] == ("me", "me") and types[-1] == ("user", ids.OID_ALICE)
        assert ("quota", "quota|2026-10-21") not in types
        for doc in body["documents"]:
            assert not {"_rid", "_self", "_etag", "_attachments", "_ts", "pk"} & set(doc)

    def test_two_a_day(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        ctx = _ctx(fake_cosmos, alice)
        accounts.build_export(ctx, now=NOW)
        accounts.build_export(ctx, now=NOW)
        with pytest.raises(ApiError) as caught:
            accounts.build_export(ctx, now=NOW)
        assert (caught.value.code, caught.value.details["quota"]) == ("quota_exceeded", "export")


class TestAdminUsers:
    def test_list_newest_first_with_truncation(self, fake_cosmos: FakeCosmos, make_principal) -> None:
        people = [make_principal(f"0000000{i}-0000-4000-8000-00000000000{i}") for i in range(1, 4)]
        for offset, person in enumerate(people):
            minted = invites.mint(label="W", max_uses=1, expires_in_days=30, count=1, actor_uid=ADMIN_UID, now=NOW)[0]
            invites.redeem(person, minted.code, accept_language=None, now=NOW + timedelta(minutes=offset))
        rows, truncated = accounts.list_users(limit=2)
        assert truncated is True
        assert [r["uid"] for r in rows] == [people[2].uid, people[1].uid]
        wire = accounts.user_to_wire(rows[0])
        assert set(wire) == {"uid", "status", "admin", "createdAt", "lastActiveAt", "inviteLabel", "locale", "languages", "blockedAt"}
        assert ids.OID_ALICE not in json.dumps(wire)
        assert accounts.list_users()[1] is False

    def test_find_by_uid(self, fake_cosmos: FakeCosmos, alice) -> None:
        _join(fake_cosmos, alice)
        assert accounts.find_registry_by_uid(alice.uid)["id"] == ids.OID_ALICE
        assert accounts.find_registry_by_uid("0" * 32) is None
        assert accounts.find_registry_by_uid("not-a-uid") is None


class TestBlock:
    def test_block_writes_me_first_and_takes_effect_at_once(self, fake_cosmos: FakeCosmos, alice, admin) -> None:
        _join(fake_cosmos, alice)
        before = len(fake_cosmos.writes())
        wire = accounts.set_blocked(alice.uid, blocked=True, reason="abuse", actor=admin, now=NOW)
        assert (wire["status"], wire["blockedAt"]) == ("blocked", "2026-10-21T10:00:00Z")
        assert [op.pk for op in fake_cosmos.writes()[before:]][:2] == [ALICE_PK, "users"]
        assert fake_cosmos.get(ids.OID_ALICE, "users")["blocked_by"] == admin.uid
        with pytest.raises(ApiError) as caught:
            members.gate(alice, now=NOW)
        assert caught.value.code == "account_blocked"
        entry = _audit(fake_cosmos, "user.blocked")[0]
        assert (entry["actor"], entry["target"], entry["details"]) == (admin.uid, {"type": "user", "id": alice.uid}, {"reason": "abuse"})

    def test_unblock_clears_the_marks(self, fake_cosmos: FakeCosmos, alice, admin) -> None:
        _join(fake_cosmos, alice)
        accounts.set_blocked(alice.uid, blocked=True, reason=None, actor=admin, now=NOW)
        wire = accounts.set_blocked(alice.uid, blocked=False, reason=None, actor=admin, now=NOW)
        assert (wire["status"], wire["blockedAt"]) == ("active", None)
        assert "blocked_at" not in fake_cosmos.get("me", ALICE_PK)
        assert members.gate(alice, now=NOW)["status"] == "active"
        assert len(_audit(fake_cosmos, "user.unblocked")) == 1

    def test_into_the_current_state_is_a_quiet_200(self, fake_cosmos: FakeCosmos, alice, admin) -> None:
        _join(fake_cosmos, alice)
        before = len(fake_cosmos.writes())
        assert accounts.set_blocked(alice.uid, blocked=False, reason=None, actor=admin, now=NOW)["status"] == "active"
        assert len(fake_cosmos.writes()) == before

    def test_refusals(self, fake_cosmos: FakeCosmos, alice, admin) -> None:
        _join(fake_cosmos, alice)
        with pytest.raises(ApiError) as caught:
            accounts.set_blocked(admin.uid, blocked=True, reason=None, actor=admin, now=NOW)
        assert caught.value.details == {"fields": [{"field": "uid", "reason": "self"}]}
        with pytest.raises(ApiError) as caught:
            accounts.set_blocked("0" * 32, blocked=True, reason=None, actor=admin, now=NOW)
        assert caught.value.details == {"resource": "user"}
        accounts.request_deletion(_ctx(fake_cosmos, alice), now=NOW)
        with pytest.raises(ApiError) as caught:
            accounts.set_blocked(alice.uid, blocked=True, reason=None, actor=admin, now=NOW)
        assert (caught.value.code, caught.value.status, caught.value.details) == ("state_conflict", 409, {"status": "deleting"})


class TestOverview:
    def test_counts(self, fake_cosmos: FakeCosmos, alice, make_principal, env) -> None:
        env(APPLE_SECRET_RENEW_BY="2027-04-01")
        bob = make_principal(ids.OID_BOB)
        _join(fake_cosmos, alice)
        _join(fake_cosmos, bob)
        invites.mint(label="Spare", max_uses=1, expires_in_days=30, count=2, actor_uid=ADMIN_UID, now=NOW)
        accounts.request_deletion(_ctx(fake_cosmos, bob), now=NOW)
        fake_cosmos.put(
            {"id": "harvest-20261021T093004Z", "pk": "run", "type": "run", "kind": "harvest",
             "started_at": "2026-10-21T09:30:04Z", "ok": True, "items_new": 4},
            container="items",
        )
        body = accounts.overview(NOW)
        assert body["users"] == {"total": 2, "active7d": 2, "new30d": 2, "blocked": 0, "deleting": 1}
        assert body["invites"] == {"active": 2, "used": 2, "expired": 0, "revoked": 0}
        assert body["deletions"] == {"purging": 0, "awaitingIdentityRemoval": 0, "overdue": 0}
        assert body["pipeline"] == {"lastHarvest": {"at": "2026-10-21T09:30:04Z", "ok": True}, "lastCycle": None}
        assert body["secrets"] == {"appleSecretRenewBy": "2027-04-01", "appleSecretDaysLeft": 162}
        assert (body["spend"], body["suggestions"], body["reports"]) == (None, None, None)

    def test_missing_or_broken_inputs_are_nulls(self, fake_cosmos: FakeCosmos, env) -> None:
        env(APPLE_SECRET_RENEW_BY="next spring")
        fake_cosmos.fail("query", container="items")
        body = accounts.overview(NOW)
        assert body["pipeline"]["lastHarvest"] is None
        assert body["secrets"] == {"appleSecretRenewBy": None, "appleSecretDaysLeft": None}


class TestIdentityRemoval:
    def _purged(self, fake: FakeCosmos, principal, *, at: datetime = NOW) -> None:
        _join(fake, principal)
        accounts.purge_account(_purge_message(fake, principal), now=at)

    def test_modes(self, env) -> None:
        assert identity_removal.mode() == "manual"
        env(IDENTITY_DELETE_MODE="graph")
        assert identity_removal.mode() == "manual"  # not available yet
        env(IDENTITY_DELETE_MODE="nonsense")
        assert identity_removal.mode() == "manual"
        assert identity_removal.identity_removal_label() == "manual"

    def test_the_pending_list_shows_the_oid_until_removal(self, fake_cosmos: FakeCosmos, alice, admin) -> None:
        self._purged(fake_cosmos, alice)
        rows = identity_removal.list_deletions(status="awaiting_identity_removal", now=NOW)
        assert identity_removal.deletion_to_wire(rows[0], NOW) == {
            "id": alice.uid,
            "status": "awaiting_identity_removal",
            "identityMode": "manual",
            "oid": ids.OID_ALICE,
            "requestedAt": "2026-10-21T10:00:00Z",
            "purgedAt": "2026-10-21T10:00:00Z",
            "identityRemovedAt": None,
            "docsDeleted": 1,  # the redeem quota counter; me is not counted
            "overdue": False,
        }
        assert identity_removal.deletion_to_wire(rows[0], NOW + timedelta(days=8))["overdue"] is True
        assert identity_removal.list_deletions(status="purging", now=NOW) == []
        assert len(identity_removal.list_deletions(status="all", now=NOW)) == 1

    def test_complete_erases_the_oid_and_starts_the_expiries(self, fake_cosmos: FakeCosmos, alice, admin) -> None:
        self._purged(fake_cosmos, alice)
        later = NOW + timedelta(days=2)
        wire = identity_removal.complete(alice.uid, actor_uid=admin.uid, now=later)
        assert (wire["status"], wire["oid"], wire["identityRemovedAt"]) == ("completed", None, "2026-10-23T10:00:00Z")
        deletion = fake_cosmos.get(f"deletion|{alice.uid}", "sys")
        assert "oid" not in deletion and deletion["ttl"] == 90 * 86400 and deletion["identity_removed_by"] == admin.uid
        tombstone = fake_cosmos.get(f"tombstone|{alice.uid}", "sys")
        assert (tombstone["identity_removed"], tombstone["ttl"]) == (True, 30 * 86400)
        assert [e["target"] for e in _audit(fake_cosmos, "identity.removed")] == [{"type": "deletion", "id": alice.uid}]
        # Again: unchanged, no second entry.
        assert identity_removal.complete(alice.uid, actor_uid=admin.uid, now=later + timedelta(days=1)) == wire
        assert len(_audit(fake_cosmos, "identity.removed")) == 1

    def test_complete_waits_for_the_purge(self, fake_cosmos: FakeCosmos, alice, admin) -> None:
        _join(fake_cosmos, alice)
        message = _purge_message(fake_cosmos, alice)
        fake_cosmos.fail("delete", id="me")
        with pytest.raises(cosmos.Unavailable):
            accounts.purge_account(message, now=NOW)
        with pytest.raises(ApiError) as caught:
            identity_removal.complete(alice.uid, actor_uid=admin.uid, now=NOW)
        assert (caught.value.code, caught.value.details) == ("state_conflict", {"status": "purging"})

    def test_complete_of_an_unknown_deletion(self, fake_cosmos: FakeCosmos, admin) -> None:
        with pytest.raises(ApiError) as caught:
            identity_removal.complete("0" * 32, actor_uid=admin.uid, now=NOW)
        assert caught.value.details == {"resource": "deletion"}

    def test_a_bad_list_status(self, fake_cosmos: FakeCosmos) -> None:
        with pytest.raises(ApiError) as caught:
            identity_removal.list_deletions(status="pending", now=NOW)
        assert caught.value.details == {"fields": [{"field": "status", "reason": "enum"}]}

    def test_overdue_counts_in_the_overview(self, fake_cosmos: FakeCosmos, alice) -> None:
        self._purged(fake_cosmos, alice, at=NOW - timedelta(days=9))
        assert accounts.overview(NOW)["deletions"] == {"purging": 0, "awaitingIdentityRemoval": 1, "overdue": 1}


# ---------------------------------------------------------------------------
# Wire shapes against the published schemas
# ---------------------------------------------------------------------------
class TestWireSchemas:
    def test_account_and_admin_bodies(self, fake_cosmos: FakeCosmos, alice, admin, make_principal, env, wire_schema) -> None:
        env(APPLE_SECRET_RENEW_BY="2027-04-01")
        bob = make_principal(ids.OID_BOB)
        _join(fake_cosmos, alice)
        _join(fake_cosmos, bob)
        members.gate(admin, now=NOW)
        _seed_partition(fake_cosmos, ids.OID_ALICE, 2)
        fake_cosmos.put(
            {"id": "harvest-20261021T093004Z", "pk": "run", "type": "run", "kind": "harvest",
             "started_at": "2026-10-21T09:30:04Z", "ok": True, "items_new": 4},
            container="items",
        )
        accounts.set_blocked(bob.uid, blocked=True, reason="spam", actor=admin, now=NOW)

        export, _ = accounts.build_export(_ctx(fake_cosmos, alice), now=NOW)
        wire_schema("Export", export)
        rows, truncated = accounts.list_users()
        wire_schema("AdminUserList", {"users": [accounts.user_to_wire(r) for r in rows], "truncated": truncated})
        assert {accounts.user_to_wire(r)["status"] for r in rows} == {"active", "blocked"}

        accepted, message = accounts.request_deletion(_ctx(fake_cosmos, alice), now=NOW)
        wire_schema("DeleteAccepted", accepted)
        wire_schema("AdminUser", accounts.user_to_wire(accounts.find_registry_by_uid(alice.uid)))
        accounts.purge_account(PurgeMessage.from_json(message), now=NOW)
        # Bob's purge dies half-way, so one deletion is still purging.
        _, bob_message = accounts.request_deletion(_ctx(fake_cosmos, bob), now=NOW)
        fake_cosmos.fail("delete", id="me", pk=f"user|{ids.OID_BOB}")
        with pytest.raises(cosmos.Unavailable):
            accounts.purge_account(PurgeMessage.from_json(bob_message), now=NOW)

        rows = identity_removal.list_deletions(status="all", now=NOW)
        assert {r["status"] for r in rows} == {"purging", "awaiting_identity_removal"}
        wire_schema("AdminDeletionList", {"deletions": [identity_removal.deletion_to_wire(r, NOW) for r in rows]})
        wire_schema("AdminDeletion", identity_removal.complete(alice.uid, actor_uid=admin.uid, now=NOW))
        wire_schema("AdminOverview", accounts.overview(NOW))


# ---------------------------------------------------------------------------
# core.jobs: queue messages
# ---------------------------------------------------------------------------
class TestQueueMessages:
    def test_the_purge_message_round_trips(self) -> None:
        from core import jobs

        message = PurgeMessage(oid=ids.OID_ALICE, requested_at="2026-10-21T10:00:00Z")
        assert json.loads(message.to_json()) == {
            "v": 1, "kind": "account", "oid": ids.OID_ALICE, "requestedAt": "2026-10-21T10:00:00Z",
        }
        # The trigger hands the worker bytes.
        assert PurgeMessage.from_json(message.to_json().encode("utf-8")) == message
        assert (jobs.QUEUE_ACCOUNT_PURGE, jobs.poison_queue(jobs.QUEUE_ACCOUNT_PURGE)) == ("account-purge", "account-purge-poison")
        assert (jobs.QUEUE_OPS_JOBS, jobs.QUEUE_SUGGESTIONS, jobs.JOBS_CONNECTION) == ("ops-jobs", "suggestions", "JOBS_STORAGE")

    def test_unknown_keys_are_ignored_within_version_1(self) -> None:
        raw = json.dumps({"v": 1, "kind": "history", "oid": ids.OID_BOB, "requestedAt": "2026-10-21T10:00:00Z", "extra": 1})
        assert PurgeMessage.from_json(raw).kind == "history"

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "[]",
            b"\xff\xfe",
            json.dumps({"v": 2, "kind": "account", "oid": ids.OID_ALICE, "requestedAt": "2026-10-21T10:00:00Z"}),
            json.dumps({"v": True, "kind": "account", "oid": ids.OID_ALICE, "requestedAt": "2026-10-21T10:00:00Z"}),
            json.dumps({"kind": "account", "oid": ids.OID_ALICE, "requestedAt": "2026-10-21T10:00:00Z"}),
            json.dumps({"v": 1, "kind": "everything", "oid": ids.OID_ALICE, "requestedAt": "2026-10-21T10:00:00Z"}),
            json.dumps({"v": 1, "kind": "account", "oid": ids.OID_ALICE.upper(), "requestedAt": "2026-10-21T10:00:00Z"}),
            json.dumps({"v": 1, "kind": "account", "oid": "user|x", "requestedAt": "2026-10-21T10:00:00Z"}),
            json.dumps({"v": 1, "kind": "account", "requestedAt": "2026-10-21T10:00:00Z"}),
            json.dumps({"v": 1, "kind": "account", "oid": ids.OID_ALICE, "requestedAt": "2026-10-21T10:00:00.123Z"}),
            json.dumps({"v": 1, "kind": "account", "oid": ids.OID_ALICE}),
        ],
    )
    def test_a_purge_message_this_release_cannot_act_on_is_a_value_error(self, raw: str | bytes) -> None:
        with pytest.raises(ValueError):
            PurgeMessage.from_json(raw)

    def test_the_ops_job_message_round_trips(self) -> None:
        from core.jobs import OpsJobMessage

        job = OpsJobMessage.new("harvest", requested_by=ADMIN_UID, now=NOW, source_id="heise-developer")
        wire = json.loads(job.to_json())
        assert wire == {
            "v": 1, "jobId": job.job_id, "kind": "harvest", "force": True, "sourceId": "heise-developer",
            "requestedBy": ADMIN_UID, "requestedAt": "2026-10-21T10:00:00Z",
        }
        assert OpsJobMessage.from_json(job.to_json()) == job
        # A job id in capitals (another producer) is read lower-case.
        assert OpsJobMessage.from_json(job.to_json().replace(job.job_id, job.job_id.upper())) == job
        system = OpsJobMessage.new("harvest", requested_by="system", now=NOW)
        assert OpsJobMessage.from_json(system.to_json()).source_id is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("kind", "cycle"),  # Phase 2's
            ("jobId", "job-1"),
            ("requestedBy", ids.OID_ALICE),
            ("requestedAt", "yesterday"),
            ("force", "yes"),
            ("sourceId", "Heise Online"),
            ("sourceId", "x" * 41),
            ("v", 3),
        ],
    )
    def test_an_ops_job_this_release_cannot_run_is_a_value_error(self, field: str, value: object) -> None:
        from core.jobs import OpsJobMessage

        wire = json.loads(OpsJobMessage.new("harvest", requested_by="system", now=NOW).to_json())
        wire[field] = value
        with pytest.raises(ValueError):
            OpsJobMessage.from_json(json.dumps(wire))


# ---------------------------------------------------------------------------
# core.jobs.peek against a fake Storage endpoint
# ---------------------------------------------------------------------------
PEEK_EMPTY = b'<?xml version="1.0" encoding="utf-8"?><QueueMessagesList />'
PEEK_TWO = (
    b'<?xml version="1.0" encoding="utf-8"?><QueueMessagesList>'
    b"<QueueMessage><MessageId>1</MessageId><InsertionTime>Wed, 21 Oct 2026 09:50:00 GMT</InsertionTime>"
    b"<ExpirationTime>Wed, 28 Oct 2026 09:50:00 GMT</ExpirationTime><DequeueCount>0</DequeueCount>"
    b"<MessageText>eyJ2IjoxfQ==</MessageText></QueueMessage>"
    b"<QueueMessage><MessageId>2</MessageId><InsertionTime>Wed, 21 Oct 2026 09:40:00 GMT</InsertionTime>"
    b"<ExpirationTime>Wed, 28 Oct 2026 09:40:00 GMT</ExpirationTime><DequeueCount>1</DequeueCount>"
    b"<MessageText>eyJ2IjoxfQ==</MessageText></QueueMessage>"
    b"</QueueMessagesList>"
)


class _Storage:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.scopes: list[tuple[str, ...]] = []
        # queue -> (status, body), or an exception to raise
        self.answers: dict[str, Any] = {}


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> _Storage:
    import httpx

    from core import jobs

    fake = _Storage()

    def handler(request: httpx.Request) -> httpx.Response:
        fake.requests.append(request)
        queue = request.url.path.strip("/").split("/")[0]
        answer = fake.answers.get(queue, (200, PEEK_EMPTY))
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, content=body)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))

    class Credential:
        def get_token(self, *scopes: str, **_: Any) -> Any:
            fake.scopes.append(scopes)
            return type("AccessToken", (), {"token": "fake-storage-token"})()

    monkeypatch.setattr(jobs, "_credential", lambda: Credential())
    return fake


class TestPeek:
    def test_an_empty_queue(self, storage: _Storage) -> None:
        from core import jobs

        assert jobs.peek("account-purge") == jobs.QueuePeek("account-purge", True, 0, None, None)
        request = storage.requests[0]
        assert str(request.url) == (
            "https://example.queue.core.windows.net/account-purge/messages?peekonly=true&numofmessages=32"
        )
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer fake-storage-token"
        assert request.headers["x-ms-version"] == "2021-12-02"
        assert request.headers["x-ms-date"].endswith(" GMT")
        assert storage.scopes == [("https://storage.azure.com/.default",)]

    def test_messages_report_the_count_and_the_oldest(self, storage: _Storage) -> None:
        from core import jobs

        storage.answers["ops-jobs"] = (200, PEEK_TWO)
        assert jobs.peek("ops-jobs") == jobs.QueuePeek("ops-jobs", True, 2, "2026-10-21T09:40:00Z", None)

    @pytest.mark.parametrize(
        ("status", "error"),
        [(403, "forbidden"), (401, "forbidden"), (404, "not_found"), (429, "throttled"), (503, "unavailable"), (400, "failed")],
    )
    def test_http_failures_are_classified(self, storage: _Storage, status: int, error: str) -> None:
        from core import jobs

        storage.answers["account-purge-poison"] = (status, b"<Error><Message>secret details</Message></Error>")
        peeked = jobs.peek("account-purge-poison")
        assert (peeked.ok, peeked.count, peeked.error) == (False, None, error)

    def test_transport_failures_are_classified(self, storage: _Storage) -> None:
        import httpx

        from core import jobs

        storage.answers["account-purge"] = httpx.ConnectTimeout("slow")
        storage.answers["ops-jobs"] = httpx.ConnectError("refused")
        assert jobs.peek("account-purge").error == "timeout"
        assert jobs.peek("ops-jobs").error == "unavailable"

    def test_an_unreadable_body_is_a_failed_check(self, storage: _Storage) -> None:
        from core import jobs

        storage.answers["ops-jobs"] = (200, b"<html>gateway says hello")
        assert jobs.peek("ops-jobs").error == "failed"

    def test_without_a_token_nothing_is_sent(self) -> None:
        from core import jobs

        # conftest's credential never produces one (no network in tests).
        assert jobs.peek("account-purge").error == "misconfigured"

    @pytest.mark.parametrize("uri", [None, "http://example.queue.core.windows.net/", "@Microsoft.KeyVault(SecretUri=x)"])
    def test_without_an_https_endpoint_nothing_is_sent(self, storage: _Storage, env, uri: str | None) -> None:
        from core import jobs

        env(JOBS_STORAGE__queueServiceUri=uri)
        assert jobs.peek("account-purge").error == "misconfigured"
        assert storage.requests == [] and storage.scopes == []

    @pytest.mark.parametrize("queue", ["", "Account-Purge", "a--b", "../x", "x" * 64])
    def test_a_bad_queue_name_is_refused(self, storage: _Storage, queue: str) -> None:
        from core import jobs

        assert jobs.peek(queue).error == "misconfigured"
        assert storage.requests == []

    def test_the_token_is_never_logged(self, storage: _Storage, caplog: pytest.LogCaptureFixture) -> None:
        from core import jobs

        storage.answers["ops-jobs"] = (403, b"")
        with caplog.at_level("DEBUG"):
            jobs.peek("ops-jobs")
        assert "fake-storage-token" not in caplog.text


# ---------------------------------------------------------------------------
# core.ops_health: GET /admin/health
# ---------------------------------------------------------------------------
@pytest.fixture
def queues(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """core.jobs.peek replaced: every queue empty unless a test says otherwise."""
    from core import jobs

    answers: dict[str, Any] = {}

    def fake_peek(queue: str, *, timeout_s: float = 5.0) -> jobs.QueuePeek:
        return answers.get(queue) or jobs.QueuePeek(queue, True, 0, None, None)

    monkeypatch.setattr(jobs, "peek", fake_peek)
    return answers


def _harvest_run(fake: FakeCosmos, *, at: str = "2026-10-21T09:30:04Z", ok: bool = True) -> None:
    fake.put(
        {"id": "harvest-" + at.replace(":", "").replace("-", ""), "pk": "run", "type": "run", "kind": "harvest",
         "started_at": at, "ok": ok, "items_new": 4},
        container="items",
    )


class TestDeepHealth:
    @pytest.fixture(autouse=True)
    def _defaults(self, fake_cosmos: FakeCosmos, jwks, queues) -> None:
        _harvest_run(fake_cosmos)

    def _health(self) -> dict[str, Any]:
        from core import ops_health

        return ops_health.deep_health(NOW)

    def test_all_green(self) -> None:
        from core import auth

        body = self._health()
        assert body["status"] == "ok", body
        assert (body["checkedAt"], body["api"], body["minBuild"], body["identityDeleteMode"]) == (
            "2026-10-21T10:00:00Z", 1, 0, "manual",
        )
        assert body["hmacKeyFingerprint"] == auth.hmac_key_fingerprint(ids.HMAC_KEY)
        checks = body["checks"]
        assert set(checks) == {"config", "cosmos", "legacyCosmos", "jwks", "queues", "harvest", "deletions", "secrets"}
        assert checks["config"] == {"ok": True, "missing": [], "unresolvedKeyVaultRefs": []}
        assert checks["cosmos"]["ok"] and checks["cosmos"]["error"] is None and checks["cosmos"]["ms"] >= 0
        assert checks["legacyCosmos"]["ok"] and checks["jwks"]["keys"] == 1
        assert checks["queues"]["poison"] == {"account-purge-poison": 0, "ops-jobs-poison": 0}
        assert checks["queues"]["oldestMessageMinutes"] == {"account-purge": None, "ops-jobs": None}
        assert checks["harvest"] == {"ok": True, "lastRunAt": "2026-10-21T09:30:04Z", "lastRunOk": True, "ageMinutes": 29}
        assert checks["deletions"] == {"ok": True, "stuckPurging": 0, "overdueIdentityRemovals": 0}
        assert checks["secrets"] == {"ok": True, "appleSecretRenewBy": None, "daysLeft": None}

    @pytest.mark.parametrize(
        ("queue", "peeked", "ok"),
        [
            ("account-purge-poison", ("account-purge-poison", True, 2, "2026-10-21T08:00:00Z", None), False),
            ("ops-jobs", ("ops-jobs", True, 1, "2026-10-21T09:49:00Z", None), False),  # waited 11 minutes
            ("ops-jobs", ("ops-jobs", True, 1, "2026-10-21T09:51:00Z", None), True),  # 9 minutes: fine
            ("ops-jobs-poison", ("ops-jobs-poison", False, None, None, "forbidden"), False),
        ],
    )
    def test_queue_problems(self, queues: dict[str, Any], queue: str, peeked: tuple, ok: bool) -> None:
        from core import jobs

        queues[queue] = jobs.QueuePeek(*peeked)
        body = self._health()
        check = body["checks"]["queues"]
        assert check["ok"] is ok and body["status"] == ("ok" if ok else "degraded")
        if queue == "ops-jobs-poison":
            assert check["error"] == "forbidden" and check["poison"]["ops-jobs-poison"] is None
        if queue == "ops-jobs":
            assert check["oldestMessageMinutes"]["ops-jobs"] == (9 if ok else 11)

    def test_a_cosmos_outage(self, fake_cosmos: FakeCosmos) -> None:
        _cosmos_outage(fake_cosmos)
        body = self._health()
        assert body["status"] == "degraded"
        assert (body["checks"]["cosmos"]["ok"], body["checks"]["cosmos"]["error"]) == (False, "throttled")
        assert body["checks"]["deletions"]["ok"] is False
        assert body["checks"]["legacyCosmos"]["ok"] is True  # the other container is fine

    def test_an_unreadable_legacy_container_proves_no_harvest(self, fake_cosmos: FakeCosmos) -> None:
        fake_cosmos.fail("query", container="items", times=5)
        body = self._health()
        assert (body["checks"]["legacyCosmos"]["ok"], body["checks"]["legacyCosmos"]["error"]) == (False, "unavailable")
        assert body["checks"]["harvest"] == {"ok": False, "lastRunAt": None, "lastRunOk": None, "ageMinutes": None}

    @pytest.mark.parametrize(
        ("at", "ok", "expected_ok", "age"),
        [
            ("2026-10-21T07:00:00Z", True, True, 180),  # exactly 3 hours
            ("2026-10-21T06:59:00Z", True, False, 181),
            ("2026-10-21T09:30:00Z", False, False, 30),  # recent but failed
        ],
    )
    def test_the_harvest_must_be_recent_and_successful(
        self, fake_cosmos: FakeCosmos, at: str, ok: bool, expected_ok: bool, age: int
    ) -> None:
        fake_cosmos.get_container("items").docs.clear()
        _harvest_run(fake_cosmos, at=at, ok=ok)
        check = self._health()["checks"]["harvest"]
        assert (check["ok"], check["lastRunOk"], check["ageMinutes"]) == (expected_ok, ok, age)

    def test_no_harvest_at_all(self, fake_cosmos: FakeCosmos) -> None:
        fake_cosmos.get_container("items").docs.clear()
        body = self._health()
        assert body["checks"]["legacyCosmos"]["ok"] is True
        assert body["checks"]["harvest"]["ok"] is False

    def test_a_jwks_outage(self, jwks) -> None:
        _jwks_outage(jwks)
        jwks_check = self._health()["checks"]["jwks"]
        assert (jwks_check["ok"], jwks_check["error"], jwks_check["keys"]) == (False, "unavailable", None)

    def test_configuration_names_never_values(self, env) -> None:
        _broken_config(env)
        body = self._health()
        assert body["checks"]["config"] == {
            "ok": False,
            "missing": ["COSMOS_ENDPOINT"],
            "unresolvedKeyVaultRefs": ["JOBS_STORAGE__queueServiceUri"],
        }
        assert "kv-mynews" not in json.dumps(body)

    @pytest.mark.parametrize(
        ("renew_by", "ok", "days_left"),
        [
            ("2027-04-01", True, 162),
            ("2026-11-21", True, 31),
            ("2026-11-20", False, 30),  # the first day of the 30-day warning
            ("2026-10-01", False, -20),  # past due
        ],
    )
    def test_the_apple_secret(self, env, renew_by: str, ok: bool, days_left: int) -> None:
        env(APPLE_SECRET_RENEW_BY=renew_by)
        assert self._health()["checks"]["secrets"] == {"ok": ok, "appleSecretRenewBy": renew_by, "daysLeft": days_left}

    def test_an_unreadable_renewal_date_fails(self, env) -> None:
        env(APPLE_SECRET_RENEW_BY="next spring")
        assert self._health()["checks"]["secrets"] == {"ok": False, "appleSecretRenewBy": None, "daysLeft": None}

    def test_stuck_and_overdue_deletions(self, fake_cosmos: FakeCosmos, make_principal) -> None:
        _stuck_and_overdue(fake_cosmos, make_principal)
        assert self._health()["checks"]["deletions"] == {"ok": False, "stuckPurging": 1, "overdueIdentityRemovals": 1}

    def test_a_hung_check_is_a_timeout_and_does_not_hold_the_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        from core import ops_health

        release = _hang_queues(monkeypatch)
        started = time.monotonic()
        try:
            body = ops_health.deep_health(NOW)
        finally:
            release.set()
        assert time.monotonic() - started < 2
        assert body["checks"]["queues"] == {
            "ok": False, "ms": None, "error": "timeout",
            "poison": {"account-purge-poison": None, "ops-jobs-poison": None},
            "oldestMessageMinutes": {"account-purge": None, "ops-jobs": None},
        }
        assert body["checks"]["cosmos"]["ok"] is True

    def test_exception_text_is_never_echoed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken() -> None:
            raise RuntimeError("password=hunter2 at /dbs/mynews/colls/items")

        monkeypatch.setattr(accounts, "fetch_last_v1_harvest", broken)
        body = self._health()
        assert body["checks"]["legacyCosmos"]["error"] == "failed"
        assert "hunter2" not in json.dumps(body)

    def test_error_classes(self) -> None:
        from core import ops_health

        assert ops_health.classify(RuntimeError("Required setting 'COSMOS_ENDPOINT' is missing.")) == "misconfigured"
        assert ops_health.classify(cosmos.Unavailable("read: HTTP 404/1003", status=404, sub_status=1003)) == "not_found"
        assert ops_health.classify(cosmos.CosmosError("query: HTTP 403", status=403)) == "forbidden"
        assert ops_health.classify(cosmos.Unavailable("query: HTTP 429", status=429)) == "throttled"
        assert ops_health.classify(cosmos.Unavailable("read: ServiceRequestError")) == "unavailable"
        assert ops_health.classify(ValueError("x")) == "failed"

    @pytest.mark.parametrize(
        "scenario",
        ["green", "poison", "peek_forbidden", "cosmos_outage", "legacy_unreadable", "jwks_outage", "config",
         "secret_due", "deletions", "hung_queues"],
    )
    def test_every_answer_matches_the_published_schema(
        self, fake_cosmos: FakeCosmos, jwks, queues: dict[str, Any], env, make_principal,
        monkeypatch: pytest.MonkeyPatch, wire_schema, scenario: str,
    ) -> None:
        from core import jobs, ops_health

        release = None
        if scenario == "poison":
            queues["account-purge-poison"] = jobs.QueuePeek("account-purge-poison", True, 3, "2026-10-21T08:00:00Z", None)
        elif scenario == "peek_forbidden":
            queues["ops-jobs"] = jobs.QueuePeek("ops-jobs", False, None, None, "forbidden")
        elif scenario == "cosmos_outage":
            _cosmos_outage(fake_cosmos)
        elif scenario == "legacy_unreadable":
            fake_cosmos.fail("query", container="items", times=5)
        elif scenario == "jwks_outage":
            _jwks_outage(jwks)
        elif scenario == "config":
            _broken_config(env)
        elif scenario == "secret_due":
            env(APPLE_SECRET_RENEW_BY="2026-10-01")
        elif scenario == "deletions":
            _stuck_and_overdue(fake_cosmos, make_principal)
        elif scenario == "hung_queues":
            release = _hang_queues(monkeypatch)
        try:
            body = ops_health.deep_health(NOW)
        finally:
            if release is not None:
                release.set()
        assert body["status"] == ("ok" if scenario == "green" else "degraded")
        wire_schema("AdminHealth", body)


def _cosmos_outage(fake: FakeCosmos) -> None:
    from tests.fakes.cosmos import throttled

    fake.fail("query", container="news", error=throttled(), times=50)


def _jwks_outage(jwks) -> None:
    import jwt

    jwks.client.error = jwt.PyJWKClientConnectionError("down")


def _broken_config(env) -> None:
    reference = "@Microsoft.KeyVault(SecretUri=https://kv-mynews.vault.azure.net/secrets/queue-uri)"
    env(COSMOS_ENDPOINT=None, JOBS_STORAGE__queueServiceUri=reference)


def _stuck_and_overdue(fake: FakeCosmos, make_principal) -> None:
    """Alice's purge message was lost two hours ago; Bob was purged nine days
    ago and his Entra user is still there."""
    alice, bob = make_principal(ids.OID_ALICE), make_principal(ids.OID_BOB)
    _join(fake, alice)
    _join(fake, bob)
    accounts.request_deletion(_ctx(fake, alice), now=NOW - timedelta(hours=2))
    accounts.purge_account(_purge_message(fake, bob), now=NOW - timedelta(days=9))


def _hang_queues(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The queue check blocks until the returned event is set; the deadline is
    cut to 0.2 s so the test does not wait for it.

    The whole check is replaced, not jobs.peek: deep_health never waits for a
    straggler, so the check's thread outlives the test, and once teardown has
    put the real jobs.peek (and the real credential chain) back, the peeks it
    still had to make would leave the machine for the Storage endpoint."""
    import threading

    from core import ops_health

    release = threading.Event()

    def hanging_check(now: datetime) -> dict[str, Any]:
        release.wait(10)
        return {"ok": True, "ms": 0, "error": None, "poison": {}, "oldestMessageMinutes": {}}

    monkeypatch.setattr(ops_health, "_queues_check", hanging_check)
    monkeypatch.setattr(ops_health, "DEADLINE_S", 0.2)
    return release
