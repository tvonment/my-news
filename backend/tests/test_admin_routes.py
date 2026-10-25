"""The admin routes (docs/api.md section 6.5) through their HTTP handlers:
overview, deep health, invites, users, and the pending identity removals.

Every call goes through test_http_contract's Client, so each response is also
checked against its schema and the header rules. What these tests hold is
admin-specific: the role check before any I/O, the uid-only addressing, codes
appearing once and never in a log, and each action's status semantics.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import pytest

from core import auth, cosmos, invites
from core.models import iso, utcnow
from tests.fakes import jwks as ids
from tests.fakes.cosmos import FakeCosmos
from tests.test_function_app import indexed_functions
from tests.test_http_contract import ADMIN, Client

ALICE_PK = f"user|{ids.OID_ALICE}"


@pytest.fixture
def client(jwks: Any, fake_cosmos: FakeCosmos) -> Client:
    return Client(jwks)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cosmos.time, "sleep", lambda seconds: None)


def _create(client: Client, **body: Any) -> list[dict[str, Any]]:
    resp = client.call("admin_invites_create", **ADMIN, body={"label": "Wave", **body})
    assert resp.status == 201, resp
    return resp.body["invites"]


def _join(client: Client, oid: str) -> dict[str, Any]:
    (invite,) = _create(client)
    resp = client.call("me_redeem", oid=oid, body={"code": invite["code"]})
    assert resp.status == 201, resp
    return resp.body


def _audit(fake: FakeCosmos, action: str) -> list[dict[str, Any]]:
    return [doc for doc in fake.docs("sys") if doc["type"] == "audit" and doc["action"] == action]


def _admin_routes() -> list[str]:
    return sorted(
        name for name, fn in indexed_functions().items() if fn.is_http_function() and name.startswith("admin_")
    )


# ---------------------------------------------------------------------------
# The admin level
# ---------------------------------------------------------------------------
def test_a_member_without_the_role_is_refused_before_any_read(client: Client, fake_cosmos: FakeCosmos) -> None:
    _join(client, ids.OID_ALICE)
    journal = len(fake_cosmos.journal)
    for name in _admin_routes():
        route = indexed_functions()[name].get_trigger().route
        params = {key: "a" * 32 for key in re.findall(r"\{([a-z]+)\}", route)}
        resp = client.call(name, route_params=params, body={"blocked": True, "label": "x"})
        assert (resp.status, resp.code) == (403, "admin_required"), name
    assert len(fake_cosmos.journal) == journal


def test_an_admins_first_admin_call_bootstraps_the_membership(client: Client, fake_cosmos: FakeCosmos) -> None:
    assert fake_cosmos.get("me", f"user|{ids.OID_ADMIN}") is None
    assert client.call("admin_users_list", **ADMIN).status == 200
    me = fake_cosmos.get("me", f"user|{ids.OID_ADMIN}")
    assert (me["invite_id"], me["is_admin"]) == ("bootstrap-admin", True)
    assert client.call("me_get", **ADMIN).body["roles"] == ["admin"]


def test_a_blocked_admin_is_blocked_on_admin_routes_too(client: Client, fake_cosmos: FakeCosmos) -> None:
    client.call("admin_overview", **ADMIN)
    doc = fake_cosmos.get("me", f"user|{ids.OID_ADMIN}")
    doc["status"] = "blocked"
    fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
    assert client.call("admin_overview", **ADMIN).code == "account_blocked"


# ---------------------------------------------------------------------------
# Overview and health
# ---------------------------------------------------------------------------
def test_the_overview_counts(client: Client, fake_cosmos: FakeCosmos, env) -> None:
    env(APPLE_SECRET_RENEW_BY=f"{(utcnow() + timedelta(days=40)).date():%Y-%m-%d}")
    _join(client, ids.OID_ALICE)
    bob = _join(client, ids.OID_BOB)
    client.call("admin_user_block", **ADMIN, route_params={"uid": bob["uid"]}, body={"blocked": True})
    _create(client, count=2)
    expired = _create(client)[0]["id"]
    doc = fake_cosmos.get(f"invite|{expired}", "sys")
    doc["expires_at"] = iso(utcnow() - timedelta(days=1))
    fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
    fake_cosmos.put(
        {
            "id": "harvest-20261021T093004Z",
            "pk": "run",
            "type": "run",
            "kind": "harvest",
            "started_at": "2026-10-21T09:30:04Z",
            "ok": True,
            "items_new": 4,
        },
        container="items",
    )

    body = client.call("admin_overview", **ADMIN).body
    assert body["users"] == {"total": 3, "active7d": 3, "new30d": 3, "blocked": 1, "deleting": 0}
    assert body["invites"] == {"active": 2, "used": 2, "expired": 1, "revoked": 0}
    assert body["deletions"] == {"purging": 0, "awaitingIdentityRemoval": 0, "overdue": 0}
    assert body["pipeline"] == {"lastHarvest": {"at": "2026-10-21T09:30:04Z", "ok": True}, "lastCycle": None}
    assert body["secrets"]["appleSecretDaysLeft"] == 40
    assert (body["spend"], body["suggestions"], body["reports"]) == (None, None, None)


def test_health_is_200_and_names_failures_by_class_only(client: Client, jwks: Any) -> None:
    resp = client.call("admin_health", **ADMIN)
    assert resp.status == 200
    body = resp.body
    # In tests the queues have no credential: degraded, and said so by class.
    assert body["status"] == "degraded"
    assert body["checks"]["queues"]["ok"] is False and body["checks"]["queues"]["error"] == "misconfigured"
    assert body["checks"]["jwks"]["ok"] is True and body["checks"]["cosmos"]["ok"] is True
    assert body["hmacKeyFingerprint"] == auth.hmac_key_fingerprint(ids.HMAC_KEY)
    assert body["identityDeleteMode"] == "manual"
    assert ids.HMAC_KEY not in resp.raw.decode()


def test_health_reports_a_database_outage_without_its_text(client: Client, fake_cosmos: FakeCosmos) -> None:
    client.call("admin_overview", **ADMIN)
    fake_cosmos.fail("query", pk="users", times=10)
    resp = client.call("admin_health", **ADMIN)
    assert resp.status == 200 and resp.body["status"] == "degraded"
    assert resp.body["checks"]["cosmos"] == {
        "ok": False,
        "ms": resp.body["checks"]["cosmos"]["ms"],
        "error": "unavailable",
    }
    assert "injected" not in resp.raw.decode()


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------
class TestInvites:
    def test_create_with_defaults(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        resp = client.call("admin_invites_create", **ADMIN, body={"label": "  Family wave 1  "})
        (invite,) = resp.body["invites"]
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}", invite["code"])
        assert (invite["label"], invite["maxUses"]) == ("Family wave 1", 1)
        expires = invite["expiresAt"]
        assert expires[:10] == f"{(utcnow() + timedelta(days=30)).date():%Y-%m-%d}"
        # The id is the hash of the code; the code itself is stored nowhere.
        assert invite["id"] == invites.invite_id_for(invite["code"].replace("-", ""))
        stored = fake_cosmos.get(f"invite|{invite['id']}", "sys")
        flat = repr(stored)
        assert invite["code"] not in flat and invite["code"].replace("-", "") not in flat
        assert [e["target"]["id"] for e in _audit(fake_cosmos, "invite.created")] == [invite["id"]]

    def test_create_many_with_their_own_limits(self, client: Client) -> None:
        minted = _create(client, maxUses=5, expiresInDays=60, count=3)
        assert len(minted) == 3 and len({invite["code"] for invite in minted}) == 3
        assert {invite["maxUses"] for invite in minted} == {5}

    def test_codes_are_never_logged(self, client: Client, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            minted = _create(client, count=2)
            client.call("me_redeem", body={"code": minted[0]["code"]})
            client.call("me_redeem", oid=ids.OID_BOB, body={"code": minted[0]["code"]})  # refused: used
        for invite in minted:
            assert invite["code"] not in caplog.text and invite["code"].replace("-", "") not in caplog.text

    @pytest.mark.parametrize(
        ("body", "field", "reason"),
        [
            ({}, "label", "required"),
            ({"label": ""}, "label", "length"),
            ({"label": "x" * 41}, "label", "length"),
            ({"label": 7}, "label", "type"),
            ({"label": "x", "maxUses": 0}, "maxUses", "range"),
            ({"label": "x", "maxUses": 11}, "maxUses", "range"),
            ({"label": "x", "maxUses": "2"}, "maxUses", "type"),
            ({"label": "x", "maxUses": True}, "maxUses", "type"),
            ({"label": "x", "expiresInDays": 91}, "expiresInDays", "range"),
            ({"label": "x", "count": 21}, "count", "range"),
            ({"label": "x", "count": None}, "count", "type"),
            ({"label": "x", "code": "K7QM-2HXA-9RTV"}, "code", "unknown_field"),
        ],
    )
    def test_create_validates_strictly(
        self, client: Client, fake_cosmos: FakeCosmos, body: dict, field: str, reason: str
    ) -> None:
        resp = client.call("admin_invites_create", **ADMIN, body=body)
        assert resp.code == "validation_failed"
        assert resp.body["error"]["details"]["fields"] == [{"field": field, "reason": reason}]
        assert not [doc for doc in fake_cosmos.docs("sys") if doc["type"] == "invite"]

    def test_list_derives_the_status_and_never_shows_a_code(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        used = _create(client)[0]
        client.call("me_redeem", body={"code": used["code"]})
        active = _create(client)[0]
        revoked = _create(client)[0]
        client.call("admin_invite_revoke", **ADMIN, route_params={"id": revoked["id"]})
        expired = _create(client)[0]
        doc = fake_cosmos.get(f"invite|{expired['id']}", "sys")
        doc["expires_at"] = iso(utcnow() - timedelta(seconds=1))
        fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})

        everything = client.call("admin_invites_list", **ADMIN)
        statuses = {invite["id"]: invite["status"] for invite in everything.body["invites"]}
        assert statuses == {
            used["id"]: "used",
            active["id"]: "active",
            revoked["id"]: "revoked",
            expired["id"]: "expired",
        }
        for invite in (used, active, revoked, expired):
            assert invite["code"] not in everything.raw.decode()
        for status in ("active", "used", "expired", "revoked"):
            only = client.call("admin_invites_list", **ADMIN, params={"status": status}).body["invites"]
            assert [invite["status"] for invite in only] == [status]
        assert client.call("admin_invites_list", **ADMIN, params={"status": "all"}).body == everything.body
        bad = client.call("admin_invites_list", **ADMIN, params={"status": "Active"})
        assert bad.body["error"]["details"] == {"fields": [{"field": "status", "reason": "enum"}]}

    def test_list_is_newest_first(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        older = _create(client)[0]
        doc = fake_cosmos.get(f"invite|{older['id']}", "sys")
        doc["created_at"] = iso(utcnow() - timedelta(days=2))
        fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
        newer = _create(client)[0]
        assert [i["id"] for i in client.call("admin_invites_list", **ADMIN).body["invites"]] == [
            newer["id"],
            older["id"],
        ]

    def test_revoke_is_idempotent_and_hides_the_revocation(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        invite = _create(client)[0]
        first = client.call("admin_invite_revoke", **ADMIN, route_params={"id": invite["id"]})
        again = client.call("admin_invite_revoke", **ADMIN, route_params={"id": invite["id"]})
        assert first.body["status"] == again.body["status"] == "revoked"
        assert first.body == again.body
        assert len(_audit(fake_cosmos, "invite.revoked")) == 1
        assert client.call("me_redeem", body={"code": invite["code"]}).code == "invite_not_found"

    def test_revoke_of_an_unknown_invite_is_404(self, client: Client) -> None:
        resp = client.call("admin_invite_revoke", **ADMIN, route_params={"id": "0" * 32})
        assert (resp.status, resp.body["error"]["details"]) == (404, {"resource": "invite"})


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class TestUsers:
    def test_the_list_is_by_uid_without_oids(self, client: Client) -> None:
        alice = _join(client, ids.OID_ALICE)
        resp = client.call("admin_users_list", **ADMIN)
        users = {user["uid"]: user for user in resp.body["users"]}
        assert alice["uid"] in users and resp.body["truncated"] is False
        assert users[alice["uid"]]["inviteLabel"] == "Wave" and users[alice["uid"]]["admin"] is False
        for oid in (ids.OID_ALICE, ids.OID_ADMIN):
            assert oid not in resp.raw.decode()

    def test_the_list_says_when_it_was_cut(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        # 501 members: the newest 500 are listed, and the list says it is cut.
        start = utcnow() - timedelta(days=1)
        for n in range(501):
            created = iso(start + timedelta(seconds=n))
            fake_cosmos.put(
                {
                    "id": f"00000000-0000-4000-8000-{n:012d}",
                    "pk": "users",
                    "type": "user",
                    "v": 1,
                    "uid": f"{n:032x}",
                    "status": "active",
                    "is_admin": False,
                    "created_at": created,
                    "invite_id": "x",
                    "invite_label": None,
                    "locale": "en",
                    "languages": ["en"],
                    "last_active_at": created,
                    "updated_at": created,
                }
            )
        resp = client.call("admin_users_list", **ADMIN)
        assert resp.body["truncated"] is True and len(resp.body["users"]) == 500
        # Newest first: the bootstrapped admin, then member 500 down to 2.
        assert resp.body["users"][1]["uid"] == f"{500:032x}"
        assert f"{0:032x}" not in {user["uid"] for user in resp.body["users"]}

    def test_block_and_unblock(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        alice = _join(client, ids.OID_ALICE)
        blocked = client.call(
            "admin_user_block", **ADMIN, route_params={"uid": alice["uid"]}, body={"blocked": True, "reason": "spam"}
        )
        assert blocked.status == 200 and blocked.body["status"] == "blocked" and blocked.body["blockedAt"]
        # me first (the gate reads it), then the registry.
        assert fake_cosmos.get("me", ALICE_PK)["status"] == "blocked"
        assert fake_cosmos.get(ids.OID_ALICE, "users")["blocked_by"] == client.call("me_get", **ADMIN).body["uid"]
        assert client.call("me_get").code == "account_blocked"
        assert [e["details"] for e in _audit(fake_cosmos, "user.blocked")] == [{"reason": "spam"}]
        # Into the current state: 200, no second write, no second entry.
        again = client.call("admin_user_block", **ADMIN, route_params={"uid": alice["uid"]}, body={"blocked": True})
        assert again.body == blocked.body and len(_audit(fake_cosmos, "user.blocked")) == 1
        unblocked = client.call(
            "admin_user_block", **ADMIN, route_params={"uid": alice["uid"]}, body={"blocked": False}
        )
        assert (unblocked.body["status"], unblocked.body["blockedAt"]) == ("active", None)
        assert client.call("me_get").status == 200
        assert len(_audit(fake_cosmos, "user.unblocked")) == 1

    def test_an_admin_cannot_block_themselves(self, client: Client) -> None:
        admin_uid = client.call("me_get", **ADMIN).body["uid"]
        resp = client.call("admin_user_block", **ADMIN, route_params={"uid": admin_uid}, body={"blocked": True})
        assert resp.body["error"]["details"] == {"fields": [{"field": "uid", "reason": "self"}]}

    def test_unknown_and_deleting_accounts(self, client: Client) -> None:
        missing = client.call("admin_user_block", **ADMIN, route_params={"uid": "0" * 32}, body={"blocked": True})
        assert (missing.status, missing.body["error"]["details"]) == (404, {"resource": "user"})
        alice = _join(client, ids.OID_ALICE)
        client.call("me_delete")
        deleting = client.call("admin_user_block", **ADMIN, route_params={"uid": alice["uid"]}, body={"blocked": True})
        assert (deleting.status, deleting.code, deleting.body["error"]["details"]) == (
            409,
            "state_conflict",
            {"status": "deleting"},
        )

    @pytest.mark.parametrize(
        ("body", "field", "reason"),
        [
            ({}, "blocked", "required"),
            ({"blocked": "yes"}, "blocked", "type"),
            ({"blocked": 1}, "blocked", "type"),
            ({"blocked": True, "reason": None}, "reason", "type"),
            ({"blocked": True, "reason": ""}, "reason", "length"),
            ({"blocked": True, "reason": "x" * 201}, "reason", "length"),
            ({"blocked": True, "until": "2027-01-01T00:00:00Z"}, "until", "unknown_field"),
        ],
    )
    def test_the_block_body_is_strict(
        self, client: Client, fake_cosmos: FakeCosmos, body: dict, field: str, reason: str
    ) -> None:
        alice = _join(client, ids.OID_ALICE)
        resp = client.call("admin_user_block", **ADMIN, route_params={"uid": alice["uid"]}, body=body)
        assert resp.body["error"]["details"] == {"fields": [{"field": field, "reason": reason}]}
        assert fake_cosmos.get("me", ALICE_PK)["status"] == "active"


# ---------------------------------------------------------------------------
# Deletions
# ---------------------------------------------------------------------------
def _purged(client: Client, oid: str) -> str:
    from core.accounts import purge_account
    from core.jobs import PurgeMessage

    me = _join(client, oid)
    client.call("me_delete", oid=oid)
    purge_account(PurgeMessage.from_json(client.outputs["purge"].get()))
    return me["uid"]


class TestDeletions:
    def test_the_pending_list_carries_the_oid_until_the_identity_is_removed(
        self, client: Client, fake_cosmos: FakeCosmos
    ) -> None:
        uid = _purged(client, ids.OID_ALICE)
        (pending,) = client.call("admin_deletions_list", **ADMIN).body["deletions"]
        assert (pending["id"], pending["status"], pending["oid"], pending["overdue"]) == (
            uid,
            "awaiting_identity_removal",
            ids.OID_ALICE,
            False,
        )
        assert pending["docsDeleted"] >= 0 and pending["purgedAt"]

        done = client.call("admin_deletion_complete", **ADMIN, route_params={"id": uid})
        assert (done.body["status"], done.body["oid"]) == ("completed", None)
        assert done.body["identityRemovedAt"]
        tombstone = fake_cosmos.get(f"tombstone|{uid}", "sys")
        assert tombstone["identity_removed"] is True and tombstone["ttl"] == 30 * 24 * 3600
        assert "oid" not in fake_cosmos.get(f"deletion|{uid}", "sys")
        assert len(_audit(fake_cosmos, "identity.removed")) == 1
        # Again: 200 unchanged, no second entry.
        assert client.call("admin_deletion_complete", **ADMIN, route_params={"id": uid}).body == done.body
        assert len(_audit(fake_cosmos, "identity.removed")) == 1
        assert client.call("admin_deletions_list", **ADMIN).body["deletions"] == []
        assert client.call("admin_deletions_list", **ADMIN, params={"status": "completed"}).body["deletions"] == [
            done.body
        ]

    def test_status_filters(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        uid = _purged(client, ids.OID_ALICE)
        assert [
            d["id"] for d in client.call("admin_deletions_list", **ADMIN, params={"status": "all"}).body["deletions"]
        ] == [uid]
        assert client.call("admin_deletions_list", **ADMIN, params={"status": "purging"}).body["deletions"] == []
        bad = client.call("admin_deletions_list", **ADMIN, params={"status": "done"})
        assert bad.body["error"]["details"] == {"fields": [{"field": "status", "reason": "enum"}]}

    def test_a_removal_waiting_more_than_7_days_is_overdue(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        uid = _purged(client, ids.OID_ALICE)
        doc = fake_cosmos.get(f"deletion|{uid}", "sys")
        doc["purged_at"] = iso(utcnow() - timedelta(days=8))
        fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
        assert client.call("admin_deletions_list", **ADMIN).body["deletions"][0]["overdue"] is True
        assert client.call("admin_overview", **ADMIN).body["deletions"]["overdue"] == 1

    def test_complete_waits_for_the_purge(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        from core import identity_removal

        alice = _join(client, ids.OID_ALICE)
        client.call("me_delete")
        identity_removal.open_deletion(alice["uid"], ids.OID_ALICE, requested_at=iso(utcnow()), now=utcnow())
        resp = client.call("admin_deletion_complete", **ADMIN, route_params={"id": alice["uid"]})
        assert (resp.status, resp.code, resp.body["error"]["details"]) == (409, "state_conflict", {"status": "purging"})

    def test_complete_of_an_unknown_deletion_is_404(self, client: Client) -> None:
        resp = client.call("admin_deletion_complete", **ADMIN, route_params={"id": "0" * 32})
        assert (resp.status, resp.body["error"]["details"]) == (404, {"resource": "deletion"})
