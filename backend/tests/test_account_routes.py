"""The account routes (docs/api.md section 6.2) through their HTTP handlers:
POST /me/redeem, GET/PATCH/DELETE /me and GET /me/export.

Requests carry real RS256 tokens from the test tenant (tests/fakes/jwks.py) and
reach an in-memory Cosmos account, so each test runs the access-level
decorator, the member gate and the core module the way a request in production
does. Every response also passes test_http_contract's checks (envelope,
headers, the route's response schema), because every call goes through its
Client. The rules themselves (races, partial failures) are tested in core's own
modules; these tests hold what the handlers add: the body checks, the status
each outcome answers with, and the wiring of the purge message.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from core import cosmos, invites
from core.models import iso, utcnow
from tests.fakes import jwks as ids
from tests.fakes.cosmos import FakeCosmos
from tests.test_http_contract import ADMIN, ADMIN_UID, Client

ALICE_PK = f"user|{ids.OID_ALICE}"


@pytest.fixture
def client(jwks: Any, fake_cosmos: FakeCosmos) -> Client:
    return Client(jwks)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cosmos.time, "sleep", lambda seconds: None)


def _mint(*, max_uses: int = 1, days: int = 30, label: str = "Wave") -> invites.MintedInvite:
    return invites.mint(
        label=label, max_uses=max_uses, expires_in_days=days, count=1, actor_uid=ADMIN_UID, now=utcnow()
    )[0]


def _join(client: Client, oid: str = ids.OID_ALICE, **kwargs: Any) -> dict[str, Any]:
    resp = client.call("me_redeem", oid=oid, body={"code": _mint().code}, **kwargs)
    assert resp.status == 201, resp
    return resp.body


def _quota(fake: FakeCosmos, oid: str = ids.OID_ALICE) -> dict[str, Any] | None:
    return fake.get(f"quota|{utcnow():%Y-%m-%d}", f"user|{oid}")


def _audit(fake: FakeCosmos, action: str) -> list[dict[str, Any]]:
    return [doc for doc in fake.docs("sys") if doc["type"] == "audit" and doc["action"] == action]


# ---------------------------------------------------------------------------
# Before a membership
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "kwargs"),
    [("me_get", {}), ("me_patch", {"body": {"locale": "en"}}), ("me_delete", {}), ("me_export", {})],
)
def test_without_a_membership_every_member_route_is_invite_required(
    client: Client, fake_cosmos: FakeCosmos, name: str, kwargs: dict
) -> None:
    resp = client.call(name, **kwargs)
    assert (resp.status, resp.code) == (403, "invite_required")
    # The gate read me and the tombstone, and wrote nothing.
    assert fake_cosmos.writes() == []
    assert client.outputs.get("purge") is None or client.outputs["purge"].get() is None


# ---------------------------------------------------------------------------
# POST /me/redeem
# ---------------------------------------------------------------------------
class TestRedeem:
    def test_a_code_creates_the_membership(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        invite = _mint(label="Family wave 1")
        # Typed the way people type: lower case, spaces, an O for a zero.
        typed = invite.code.lower().replace("-", " ").replace("0", "o")
        resp = client.call("me_redeem", body={"code": typed}, headers={"Accept-Language": "de-CH,de;q=0.9,en;q=0.8"})
        assert resp.status == 201
        me = resp.body
        assert (me["oid"], me["roles"], me["locale"], me["languages"]) == (ids.OID_ALICE, [], "de-CH", ["de", "en"])
        assert me["consent"] == {"aiProcessing": {"granted": False, "version": None}}
        assert me["onboarding"] == {"completedAt": None}
        # The registry first, then me; the invite records the redemption.
        stored = fake_cosmos.get("me", ALICE_PK)
        registry = fake_cosmos.get(ids.OID_ALICE, "users")
        assert (stored["status"], stored["invite_id"]) == ("active", invite.invite_id)
        assert (registry["uid"], registry["invite_label"]) == (me["uid"], "Family wave 1")
        doc = fake_cosmos.get(f"invite|{invite.invite_id}", "sys")
        assert doc["uses"] == 1 and [r["uid"] for r in doc["redemptions"]] == [me["uid"]]
        assert [e["actor"] for e in _audit(fake_cosmos, "invite.redeemed")] == [me["uid"]]
        assert client.call("me_get").body == me

    def test_a_member_redeeming_again_gets_200_and_spends_nothing(
        self, client: Client, fake_cosmos: FakeCosmos
    ) -> None:
        _join(client)
        used = _quota(fake_cosmos)["redeem"]
        other = _mint()
        resp = client.call("me_redeem", body={"code": other.code})
        assert resp.status == 200 and resp.body["oid"] == ids.OID_ALICE
        assert _quota(fake_cosmos)["redeem"] == used
        assert fake_cosmos.get(f"invite|{other.invite_id}", "sys")["uses"] == 0

    @pytest.mark.parametrize(
        ("body", "field", "reason"),
        [
            ({}, "code", "required"),
            ({"code": 12}, "code", "type"),
            ({"code": None}, "code", "type"),
            ({"code": "x", "invite": "y"}, "invite", "unknown_field"),
        ],
    )
    def test_a_body_without_a_code_string_is_validation_failed(
        self, client: Client, fake_cosmos: FakeCosmos, body: dict, field: str, reason: str
    ) -> None:
        resp = client.call("me_redeem", body=body)
        assert resp.code == "validation_failed"
        assert {"field": field, "reason": reason} in resp.body["error"]["details"]["fields"]
        assert fake_cosmos.journal == []

    @pytest.mark.parametrize(
        "code", ["", "K7QM-2HXA", "K7QM-2HXA-9RTU", "K7QM 2HXA 9RTV 1", "x" * 65, "K7QM_2HXA_9RTV"]
    )
    def test_a_string_that_is_not_a_code_is_invite_malformed_before_any_io(
        self, client: Client, fake_cosmos: FakeCosmos, code: str
    ) -> None:
        resp = client.call("me_redeem", body={"code": code})
        assert (resp.status, resp.code) == (400, "invite_malformed")
        assert fake_cosmos.journal == []  # no read, and no quota unit

    def test_unknown_and_revoked_codes_read_the_same(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        revoked = _mint()
        invites.revoke(revoked.invite_id, actor_uid=ADMIN_UID, now=utcnow())
        unknown = client.call("me_redeem", body={"code": "0000-0000-0000"})
        hidden = client.call("me_redeem", body={"code": revoked.code})
        assert unknown.code == hidden.code == "invite_not_found"
        assert unknown.body == hidden.body  # revocation is not revealed
        assert fake_cosmos.get("me", ALICE_PK) is None

    def test_used_and_expired_codes(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        spent = _mint()
        _join_with = client.call("me_redeem", oid=ids.OID_BOB, body={"code": spent.code})
        assert _join_with.status == 201
        assert client.call("me_redeem", body={"code": spent.code}).code == "invite_used"

        lapsed = _mint()
        doc = fake_cosmos.get(f"invite|{lapsed.invite_id}", "sys")
        doc["expires_at"] = iso(utcnow() - timedelta(minutes=1))
        fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
        assert client.call("me_redeem", body={"code": lapsed.code}).code == "invite_expired"
        assert fake_cosmos.get("me", ALICE_PK) is None

    def test_ten_well_formed_attempts_a_day(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        good = _mint()
        for _ in range(10):
            assert client.call("me_redeem", body={"code": "0000-0000-0000"}).code == "invite_not_found"
        # Malformed codes never reach the lookup, so they cost nothing.
        assert client.call("me_redeem", body={"code": "nope"}).code == "invite_malformed"
        limited = client.call("me_redeem", body={"code": good.code})
        assert (limited.status, limited.code) == (429, "quota_exceeded")
        assert limited.body["error"]["details"]["quota"] == "redeem"
        # The good code was not spent by the refused attempt.
        assert fake_cosmos.get(f"invite|{good.invite_id}", "sys")["uses"] == 0
        # Bob's quota is his own.
        assert client.call("me_redeem", oid=ids.OID_BOB, body={"code": good.code}).status == 201

    def test_an_admin_token_bootstraps_instead_of_spending_the_code(
        self, client: Client, fake_cosmos: FakeCosmos
    ) -> None:
        invite = _mint()
        resp = client.call("me_redeem", **ADMIN, body={"code": invite.code})
        assert resp.status == 201 and resp.body["roles"] == ["admin"]
        assert fake_cosmos.get("me", f"user|{ids.OID_ADMIN}")["invite_id"] == "bootstrap-admin"
        assert fake_cosmos.get(f"invite|{invite.invite_id}", "sys")["uses"] == 0
        assert len(_audit(fake_cosmos, "member.bootstrapped")) == 1

    def test_a_deleted_account_cannot_come_back_with_a_fresh_code(
        self, client: Client, fake_cosmos: FakeCosmos
    ) -> None:
        me = _join(client)
        fake_cosmos.put(
            {
                "id": f"tombstone|{me['uid']}",
                "pk": "sys",
                "type": "tombstone",
                "v": 1,
                "uid": me["uid"],
                "deleted_at": iso(utcnow()),
                "identity_removed": False,
                "ttl": -1,
            }
        )
        fake_cosmos.get_container().docs.pop((ALICE_PK, "me"))
        resp = client.call("me_redeem", body={"code": _mint().code})
        assert (resp.status, resp.body["error"]["details"]) == (410, {"state": "deleted"})

    @pytest.mark.parametrize(
        ("status", "code", "http"), [("blocked", "account_blocked", 403), ("deleting", "account_deleted", 410)]
    )
    def test_a_blocked_or_deleting_member_cannot_redeem_around_it(
        self, client: Client, fake_cosmos: FakeCosmos, status: str, code: str, http: int
    ) -> None:
        _join(client)
        doc = fake_cosmos.get("me", ALICE_PK)
        doc["status"] = status
        fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
        resp = client.call("me_redeem", body={"code": _mint().code})
        assert (resp.status, resp.code) == (http, code)


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------
class TestGetMe:
    def test_roles_are_copied_from_the_token_sorted(self, client: Client) -> None:
        _join(client)
        assert client.call("me_get", roles=("zeta", "admin", "alpha")).body["roles"] == ["admin", "alpha", "zeta"]
        assert client.call("me_get").body["roles"] == []

    def test_min_build_comes_from_the_setting(self, client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
        _join(client)
        monkeypatch.setenv("MIN_SUPPORTED_BUILD", "412")
        assert client.call("me_get").body["minBuild"] == 412

    def test_settings_added_later_read_as_defaults(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client)
        doc = fake_cosmos.get("me", ALICE_PK)
        doc["settings"] = {"brief_size": 15}  # an older document
        fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
        settings = client.call("me_get").body["settings"]
        assert (
            settings["briefSize"] == 15
            and settings["historyRetentionDays"] == 90
            and settings["emphasis"] == "balanced"
        )

    @pytest.mark.parametrize(
        ("status", "http", "code", "details"),
        [("blocked", 403, "account_blocked", {}), ("deleting", 410, "account_deleted", {"state": "deleting"})],
    )
    def test_the_gate_decides_what_the_app_shows(
        self, client: Client, fake_cosmos: FakeCosmos, status: str, http: int, code: str, details: dict
    ) -> None:
        _join(client)
        doc = fake_cosmos.get("me", ALICE_PK)
        doc["status"] = status
        fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
        resp = client.call("me_get")
        assert (resp.status, resp.code, resp.body["error"]["details"]) == (http, code, details)

    def test_the_first_request_of_a_day_touches_the_registry(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client)
        for doc_id, pk in (("me", ALICE_PK), (ids.OID_ALICE, "users")):
            doc = fake_cosmos.get(doc_id, pk)
            doc["last_active_at"] = "2026-01-01T00:00:00Z"
            fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
        client.call("me_get")
        assert fake_cosmos.get(ids.OID_ALICE, "users")["last_active_at"][:10] == f"{utcnow():%Y-%m-%d}"


# ---------------------------------------------------------------------------
# PATCH /me
# ---------------------------------------------------------------------------
class TestPatchMe:
    def test_whitelisted_fields_change_and_the_full_me_answers(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client)
        body = {
            "timeZone": "America/Argentina/Buenos_Aires",
            "locale": "de-CH",
            "languages": ["de"],
            "settings": {"briefSize": 15, "editions": ["morning", "evening"], "emphasis": "big_stories"},
            "consent": {"aiProcessing": {"granted": True, "version": "2026-10"}},
            "onboarding": {"completed": True},
        }
        resp = client.call("me_patch", body=body)
        assert resp.status == 200
        me = resp.body
        assert (me["timeZone"], me["locale"], me["languages"]) == ("America/Argentina/Buenos_Aires", "de-CH", ["de"])
        assert me["settings"]["editions"] == ["morning", "evening"] and me["settings"]["somethingDifferent"] is True
        assert me["consent"] == {"aiProcessing": {"granted": True, "version": "2026-10"}}
        assert me["onboarding"]["completedAt"] is not None
        stored = fake_cosmos.get("me", ALICE_PK)
        # Stored snake_case; the consent time is stamped by the server, kept
        # and exported, but not echoed.
        assert stored["settings"]["brief_size"] == 15 and stored["time_zone"] == "America/Argentina/Buenos_Aires"
        assert stored["consent"]["ai_processing"]["at"] is not None
        assert "at" not in json.dumps(me["consent"])
        # Locale and languages are mirrored to the registry.
        registry = fake_cosmos.get(ids.OID_ALICE, "users")
        assert (registry["locale"], registry["languages"]) == ("de-CH", ["de"])

    @pytest.mark.parametrize(
        ("body", "field", "reason"),
        [
            ({"oid": ids.OID_BOB}, "oid", "unknown_field"),
            ({"roles": ["admin"]}, "roles", "unknown_field"),
            ({"status": "active"}, "status", "unknown_field"),
            ({"settings": {"briefSize": 11}}, "settings.briefSize", "enum"),
            ({"settings": {"theme": "dark"}}, "settings.theme", "unknown_field"),
            ({"settings": {}}, "settings", "empty"),
            ({"languages": []}, "languages", "empty"),
            ({"languages": ["de", "de"]}, "languages", "duplicate"),
            ({"locale": "fr"}, "locale", "enum"),
            ({"timeZone": "Not a zone!"}, "timeZone", "format"),
            ({"consent": {"aiProcessing": {"granted": True}}}, "consent.aiProcessing.version", "required"),
            ({"onboarding": {"completedAt": "2026-10-21T10:00:00Z"}}, "onboarding.completedAt", "unknown_field"),
        ],
    )
    def test_anything_else_is_400_and_writes_nothing(
        self, client: Client, fake_cosmos: FakeCosmos, body: dict, field: str, reason: str
    ) -> None:
        _join(client)
        before = fake_cosmos.get("me", ALICE_PK)
        resp = client.call("me_patch", body=body)
        assert resp.code == "validation_failed"
        assert {"field": field, "reason": reason} in resp.body["error"]["details"]["fields"]
        assert fake_cosmos.get("me", ALICE_PK) == before

    def test_an_empty_body_is_400_empty(self, client: Client) -> None:
        _join(client)
        resp = client.call("me_patch", body={})
        assert (resp.code, resp.body["error"]["details"]) == ("validation_failed", {"reason": "empty"})

    def test_a_replay_writes_nothing(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client)
        body = {"settings": {"briefSize": 10}, "onboarding": {"completed": True}}
        first = client.call("me_patch", body=body)
        writes = len(fake_cosmos.writes())
        second = client.call("me_patch", body=body)
        assert second.body == first.body
        assert len(fake_cosmos.writes()) == writes

    def test_onboarding_keeps_its_first_time_and_false_clears_it(self, client: Client) -> None:
        _join(client)
        stamped = client.call("me_patch", body={"onboarding": {"completed": True}}).body["onboarding"]["completedAt"]
        assert (
            client.call("me_patch", body={"onboarding": {"completed": True}}).body["onboarding"]["completedAt"]
            == stamped
        )
        assert (
            client.call("me_patch", body={"onboarding": {"completed": False}}).body["onboarding"]["completedAt"] is None
        )

    def test_a_blocked_member_cannot_patch(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client)
        doc = fake_cosmos.get("me", ALICE_PK)
        doc["status"] = "blocked"
        fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
        assert client.call("me_patch", body={"locale": "de-CH"}).code == "account_blocked"


# ---------------------------------------------------------------------------
# DELETE /me
# ---------------------------------------------------------------------------
class TestDeleteMe:
    def test_202_marks_the_account_and_queues_one_purge(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        me = _join(client)
        resp = client.call("me_delete")
        assert (resp.status, resp.body) == (202, {"state": "deleting", "identityRemoval": "manual"})
        (message,) = client.outputs["purge"].values
        parsed = json.loads(message)
        assert parsed["v"] == 1 and parsed["kind"] == "account" and parsed["oid"] == ids.OID_ALICE
        for doc in (fake_cosmos.get("me", ALICE_PK), fake_cosmos.get(ids.OID_ALICE, "users")):
            assert doc["status"] == "deleting" and doc["deletion_requested_at"] == parsed["requestedAt"]
        assert [e["actor"] for e in _audit(fake_cosmos, "account.deletion_requested")] == [me["uid"]]

    def test_from_then_on_every_other_route_answers_410(self, client: Client) -> None:
        _join(client)
        client.call("me_delete")
        for name, kwargs in (
            ("me_get", {}),
            ("me_patch", {"body": {"locale": "en"}}),
            ("me_export", {}),
            ("me_redeem", {"body": {"code": "0000-0000-0000"}}),
        ):
            resp = client.call(name, **kwargs)
            assert (resp.status, resp.body["error"]["details"]) == (410, {"state": "deleting"}), name

    def test_a_repeat_answers_202_without_a_second_message(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client)
        client.call("me_delete")
        writes = len(fake_cosmos.writes())
        again = client.call("me_delete")
        assert again.status == 202 and again.body["state"] == "deleting"
        assert client.outputs["purge"].values == []
        assert len(fake_cosmos.writes()) == writes

    def test_a_blocked_member_may_delete(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client)
        doc = fake_cosmos.get("me", ALICE_PK)
        doc["status"] = "blocked"
        fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
        assert client.call("me_delete").status == 202
        assert len(client.outputs["purge"].values) == 1

    def test_a_failed_request_sends_no_message(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client)
        fake_cosmos.fail("replace", pk=ALICE_PK)
        resp = client.call("me_delete")
        assert (resp.status, resp.code) == (503, "degraded")
        assert client.outputs["purge"].values == []
        assert fake_cosmos.get("me", ALICE_PK)["status"] == "active"

    def test_after_the_purge_it_is_410_deleted(self, client: Client) -> None:
        from core.accounts import purge_account
        from core.jobs import PurgeMessage

        _join(client)
        client.call("me_delete")
        purge_account(PurgeMessage.from_json(client.outputs["purge"].get()))
        resp = client.call("me_delete")
        assert (resp.status, resp.body["error"]["details"]) == (410, {"state": "deleted"})


# ---------------------------------------------------------------------------
# GET /me/export
# ---------------------------------------------------------------------------
class TestExport:
    def test_everything_stored_for_the_caller_as_an_attachment(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        me = _join(client)
        fake_cosmos.put(
            {
                "id": "save|s20261020abcdefghijkl",
                "pk": ALICE_PK,
                "type": "save",
                "created_at": "2026-10-20T00:00:00Z",
                "title": "A story",
            }
        )
        resp = client.call("me_export")
        assert resp.status == 200
        assert resp.headers["content-disposition"] == f'attachment; filename="mynews-export-{utcnow():%Y-%m-%d}.json"'
        assert resp.body["account"] == client.call("me_get").body
        documents = {(doc["type"], doc["id"]) for doc in resp.body["documents"]}
        # Me, the save, and the registry document; the quota counter is not
        # the member's data and stays out.
        assert documents == {("me", "me"), ("save", "save|s20261020abcdefghijkl"), ("user", ids.OID_ALICE)}
        for doc in resp.body["documents"]:
            assert not {"_rid", "_self", "_etag", "_attachments", "_ts", "pk"} & set(doc)
        registry = next(doc for doc in resp.body["documents"] if doc["type"] == "user")
        assert registry["uid"] == me["uid"]

    def test_two_exports_a_day(self, client: Client) -> None:
        _join(client)
        assert client.call("me_export").status == 200
        assert client.call("me_export").status == 200
        third = client.call("me_export")
        assert (third.status, third.code) == (429, "quota_exceeded")
        details = third.body["error"]["details"]
        assert (details["quota"], details["limit"], details["period"]) == ("export", 2, "day")

    def test_a_blocked_member_keeps_their_data(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client)
        doc = fake_cosmos.get("me", ALICE_PK)
        doc["status"] = "blocked"
        fake_cosmos.put({k: v for k, v in doc.items() if not k.startswith("_")})
        resp = client.call("me_export")
        assert resp.status == 200
        assert next(d for d in resp.body["documents"] if d["type"] == "me")["status"] == "blocked"
