"""Isolation (docs/api.md section 2.5): every route, run as account A with
account B's identifiers wherever a client could put them, answers A's own data
or 403/404, and never B's data.

No route accepts an oid or a partition key from a client; member routes read
and write only the caller's partition, derived from the validated token; admin
routes address accounts by uid and need the admin role. So the cases below
plant Bob's oid, uid, partition key and invite in the path, the query string,
the headers (including the ones Easy Auth would set) and the body, and then
check three things: Alice's request answers for Alice (or is refused), no
response anywhere carries one of Bob's identifiers or a value only Bob's
documents hold, and Bob's documents are byte-for-byte what they were.

Every registered HTTP function must have a case here: a new route that is not
added fails test_every_route_has_an_isolation_case.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from core import cosmos
from tests.fakes import jwks as ids
from tests.fakes.cosmos import FakeCosmos
from tests.test_function_app import indexed_functions
from tests.test_http_contract import ADMIN, Client, Resp

ALICE_PK = f"user|{ids.OID_ALICE}"
BOB_PK = f"user|{ids.OID_BOB}"
# Values only Bob's documents hold, to look for in Alice's answers.
BOB_TIME_ZONE = "Pacific/Chatham"
BOB_SAVE_ID = "save|s20261020BobsOwnSave1"
BOB_SAVE_TITLE = "Only Bob saved this"


@dataclass
class World:
    alice: dict[str, Any]
    bob: dict[str, Any]
    bob_invite_id: str
    bob_code: str

    @property
    def bob_markers(self) -> tuple[str, ...]:
        return (ids.OID_BOB, self.bob["uid"], BOB_PK, BOB_TIME_ZONE, BOB_SAVE_ID, BOB_SAVE_TITLE)

    @property
    def query(self) -> dict[str, str]:
        """Query parameters a client might try (all are ignored)."""
        return {"oid": ids.OID_BOB, "uid": self.bob["uid"], "pk": BOB_PK, "status": "all"}

    @property
    def headers(self) -> dict[str, str]:
        """Identity headers a client might try. The Easy Auth ones are set by
        App Service authentication, which this app does not use, so a client
        can send them verbatim; they must mean nothing."""
        return {
            "X-MS-CLIENT-PRINCIPAL-ID": ids.OID_BOB,
            "X-MS-CLIENT-PRINCIPAL-NAME": "bob@example.com",
            "X-MS-CLIENT-PRINCIPAL": "eyJvaWQiOiJib2IifQ==",
            "X-MyNews-Oid": ids.OID_BOB,
            "X-MyNews-Uid": self.bob["uid"],
        }


@pytest.fixture
def client(jwks: Any, fake_cosmos: FakeCosmos) -> Client:
    return Client(jwks)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cosmos.time, "sleep", lambda seconds: None)


@pytest.fixture
def world(client: Client, fake_cosmos: FakeCosmos) -> World:
    # One wave of two uses: Bob redeems first, then Alice the same code, so
    # the invite document holds both of their redemptions side by side.
    (wave,) = client.call("admin_invites_create", **ADMIN, body={"label": "Wave", "maxUses": 2}).body["invites"]
    bob = client.call("me_redeem", oid=ids.OID_BOB, body={"code": wave["code"]}).body
    client.call(
        "me_patch",
        oid=ids.OID_BOB,
        body={
            "timeZone": BOB_TIME_ZONE,
            "settings": {"briefSize": 15},
            "consent": {"aiProcessing": {"granted": True, "version": "2026-10"}},
        },
    )
    fake_cosmos.put(
        {"id": BOB_SAVE_ID, "pk": BOB_PK, "type": "save", "created_at": "2026-10-20T00:00:00Z", "title": BOB_SAVE_TITLE}
    )
    alice = client.call("me_redeem", oid=ids.OID_ALICE, body={"code": wave["code"]}).body
    return World(alice=alice, bob=bob, bob_invite_id=wave["id"], bob_code=wave["code"])


def _bob_state(fake: FakeCosmos) -> str:
    """Everything stored for Bob, including the system properties: an etag
    that moved means something wrote to the document."""
    docs = [*sorted(fake.docs(BOB_PK), key=lambda d: d["id"]), fake.get(ids.OID_BOB, "users")]
    return json.dumps(docs, sort_keys=True)


def _bob_route_params(name: str, world: World) -> dict[str, str]:
    route = indexed_functions()[name].get_trigger().route
    values = {"uid": world.bob["uid"], "id": world.bob["uid"]}
    if name == "admin_invite_revoke":
        values["id"] = world.bob_invite_id
    return {key: values[key] for key in ("uid", "id") if "{" + key + "}" in route}


# Each case calls one route as Alice with Bob's identifiers and returns the
# response and the status that proves isolation. me_delete runs last: it ends
# Alice's membership.
Case = Callable[[Client, World], tuple[Resp, int]]


def _as_alice(name: str, status: int, **kwargs: Any) -> Case:
    def run(client: Client, world: World) -> tuple[Resp, int]:
        resp = client.call(
            name,
            oid=ids.OID_ALICE,
            params=world.query,
            headers=world.headers,
            route_params=_bob_route_params(name, world),
            **kwargs,
        )
        return resp, status

    return run


def _redeem_bobs_code(client: Client, world: World) -> tuple[Resp, int]:
    # Alice is a member already: 200 with her own Me, whoever's code it is.
    return client.call("me_redeem", body={"code": world.bob_code}, params=world.query, headers=world.headers), 200


CASES: dict[str, Case] = {
    "health": lambda client, world: (client.call("health", oid=None, params=world.query, headers=world.headers), 200),
    "me_redeem": _redeem_bobs_code,
    "me_get": _as_alice("me_get", 200),
    "me_patch": _as_alice("me_patch", 200, body={"locale": "de-CH", "settings": {"briefSize": 10}}),
    "me_export": _as_alice("me_export", 200),
    "admin_overview": _as_alice("admin_overview", 403),
    "admin_health": _as_alice("admin_health", 403),
    "admin_invites_list": _as_alice("admin_invites_list", 403),
    "admin_invites_create": _as_alice("admin_invites_create", 403, body={"label": "Alice's own"}),
    "admin_invite_revoke": _as_alice("admin_invite_revoke", 403),
    "admin_users_list": _as_alice("admin_users_list", 403),
    "admin_user_block": _as_alice("admin_user_block", 403, body={"blocked": True}),
    "admin_deletions_list": _as_alice("admin_deletions_list", 403),
    "admin_deletion_complete": _as_alice("admin_deletion_complete", 403),
    "me_delete": _as_alice("me_delete", 202),
}


def test_every_route_has_an_isolation_case() -> None:
    http_functions = {name for name, fn in indexed_functions().items() if fn.is_http_function()}
    assert set(CASES) == http_functions
    assert list(CASES)[-1] == "me_delete"


def test_every_route_as_alice_with_bobs_ids_never_reaches_bob(
    client: Client, world: World, fake_cosmos: FakeCosmos
) -> None:
    before = _bob_state(fake_cosmos)
    for name, case in CASES.items():
        resp, expected = case(client, world)
        assert resp.status == expected, (name, resp.status, resp.body)
        if expected == 403:
            assert resp.code == "admin_required", name
        text = resp.raw.decode("utf-8")
        for marker in world.bob_markers:
            assert marker not in text, (name, marker)
        if name.startswith("me_") and resp.status in (200, 201) and "oid" in (resp.body or {}):
            assert (resp.body["oid"], resp.body["uid"]) == (ids.OID_ALICE, world.alice["uid"]), name
    assert _bob_state(fake_cosmos) == before
    # What Alice's requests did change was Alice's.
    alice = fake_cosmos.get("me", ALICE_PK)
    assert (alice["locale"], alice["settings"]["brief_size"], alice["status"]) == ("de-CH", 10, "deleting")
    # The purge message names Alice only.
    assert json.loads(client.outputs["purge"].get())["oid"] == ids.OID_ALICE


def test_alices_export_holds_only_alices_documents(client: Client, world: World, fake_cosmos: FakeCosmos) -> None:
    resp = client.call("me_export", headers=world.headers, params=world.query)
    ids_exported = {(doc["type"], doc["id"]) for doc in resp.body["documents"]}
    assert ("user", ids.OID_ALICE) in ids_exported and ("user", ids.OID_BOB) not in ids_exported
    assert all(doc["id"] != BOB_SAVE_ID for doc in resp.body["documents"])
    # The shared registry partition is read by Alice's own oid only.
    assert world.bob["uid"] not in resp.raw.decode()


def test_a_body_cannot_name_another_account(client: Client, world: World, fake_cosmos: FakeCosmos) -> None:
    before = _bob_state(fake_cosmos)
    for body in ({"oid": ids.OID_BOB}, {"uid": world.bob["uid"]}, {"pk": BOB_PK, "locale": "en"}):
        resp = client.call("me_patch", body=body)
        assert (
            resp.code == "validation_failed" and resp.body["error"]["details"]["fields"][0]["reason"] == "unknown_field"
        )
    redeem = client.call("me_redeem", body={"code": world.bob_code, "oid": ids.OID_BOB})
    assert redeem.code == "validation_failed"
    assert _bob_state(fake_cosmos) == before


def test_identity_headers_never_authenticate(client: Client, world: World, fake_cosmos: FakeCosmos) -> None:
    # Without a bearer token, Bob's oid in every header a platform might set is
    # still nobody.
    resp = client.call("me_get", oid=None, headers=world.headers)
    assert (resp.status, resp.code) == (401, "unauthenticated")
    assert ids.OID_BOB not in resp.raw.decode()


def test_bobs_used_code_tells_a_stranger_nothing_about_bob(client: Client, world: World) -> None:
    # A caller without a membership tries the wave's code, now used up.
    carol = "c0c00000-0000-4000-8000-000000000004"
    resp = client.call("me_redeem", oid=carol, body={"code": world.bob_code})
    assert resp.code == "invite_used"
    assert resp.body["error"]["details"] == {}
    for marker in world.bob_markers:
        assert marker not in resp.raw.decode()


def test_admin_actions_on_bob_leave_alice_alone(client: Client, world: World, fake_cosmos: FakeCosmos) -> None:
    alice_before = fake_cosmos.get("me", ALICE_PK)
    blocked = client.call("admin_user_block", **ADMIN, route_params={"uid": world.bob["uid"]}, body={"blocked": True})
    assert blocked.body["uid"] == world.bob["uid"] and blocked.body["status"] == "blocked"
    assert client.call("me_get", oid=ids.OID_BOB).code == "account_blocked"
    assert client.call("me_get").status == 200
    assert fake_cosmos.get("me", ALICE_PK)["status"] == alice_before["status"] == "active"
    # Admin views carry uids, never oids, outside the identity-removal list.
    for name in ("admin_users_list", "admin_overview", "admin_invites_list"):
        text = client.call(name, **ADMIN).raw.decode()
        assert ids.OID_BOB not in text and ids.OID_ALICE not in text, name


def test_alices_deletion_purges_alice_only(client: Client, world: World, fake_cosmos: FakeCosmos) -> None:
    from core.accounts import purge_account
    from core.jobs import PurgeMessage

    before = _bob_state(fake_cosmos)
    client.call("me_delete")
    purge_account(PurgeMessage.from_json(client.outputs["purge"].get()))
    assert fake_cosmos.docs(ALICE_PK) == [] and fake_cosmos.get(ids.OID_ALICE, "users") is None
    assert _bob_state(fake_cosmos) == before
    # Alice's redemption of the shared wave is anonymised; Bob's is untouched.
    invite = fake_cosmos.get(f"invite|{world.bob_invite_id}", "sys")
    assert [entry["uid"] for entry in invite["redemptions"]] == [world.bob["uid"], "deleted"]
    assert invite["uses"] == 2
    assert client.call("me_get", oid=ids.OID_BOB).status == 200
