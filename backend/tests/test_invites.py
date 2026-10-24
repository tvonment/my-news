"""core.invites: codes, minting, listing, revoking, and the redemption
lifecycle with its failure and race cases."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from core import invites, members
from core.errors import ApiError
from tests.fakes import jwks as ids
from tests.fakes.cosmos import FakeCosmos

NOW = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
ADMIN_UID = "f" * 32


@pytest.fixture
def alice(make_principal):
    return make_principal(ids.OID_ALICE)


@pytest.fixture
def bob(make_principal):
    return make_principal(ids.OID_BOB)


def _mint(fake: FakeCosmos, *, max_uses: int = 1, days: int = 30, count: int = 1, now: datetime = NOW) -> list[invites.MintedInvite]:
    return invites.mint(label="Family wave 1", max_uses=max_uses, expires_in_days=days, count=count, actor_uid=ADMIN_UID, now=now)


def _invite_doc(fake: FakeCosmos, minted: invites.MintedInvite) -> dict:
    return fake.get(f"invite|{minted.invite_id}", "sys")


def _redeem_error(principal, code: str, now: datetime = NOW) -> ApiError:
    with pytest.raises(ApiError) as caught:
        invites.redeem(principal, code, accept_language=None, now=now)
    return caught.value


def _audit(fake: FakeCosmos, action: str) -> list[dict]:
    return [d for d in fake.docs("sys") if d["type"] == "audit" and d["action"] == action]


class TestCodes:
    @pytest.mark.parametrize(
        "typed",
        [
            "K7QM-2HXA-9RTV",
            "k7qm-2hxa-9rtv",
            "K7QM 2HXA 9RTV",
            " k7qm2hxa9rtv\n",
            "K7QM–2HXA—9RTV",  # en and em dashes from a chat app
            "K7QM 2HXA 9RTV",  # no-break spaces
            "K7QM\t2HXA−9RTV",
        ],
    )
    def test_normalisation(self, typed: str) -> None:
        assert invites.normalise_code(typed) == "K7QM2HXA9RTV"

    def test_lookalike_letters_read_as_digits(self) -> None:
        assert invites.normalise_code("OIL0-I1LO-oil0") == "011011100110"

    @pytest.mark.parametrize(
        "typed",
        ["", "K7QM-2HXA-9RT", "K7QM-2HXA-9RTVX", "K7QM-2HXA-9RTU", "K7QM_2HXA_9RTV", "x" * 65, "K7QM-2HXA-9RTé", None, 12],
    )
    def test_anything_else_is_malformed(self, typed: object) -> None:
        with pytest.raises(ApiError) as caught:
            invites.normalise_code(typed)  # type: ignore[arg-type]
        assert (caught.value.code, caught.value.status) == ("invite_malformed", 400)

    def test_the_id_is_the_documented_hash(self) -> None:
        expected = hashlib.sha256(b"mynews-invite:K7QM2HXA9RTV").hexdigest()[:32]
        assert invites.invite_id_for("K7QM2HXA9RTV") == expected

    def test_generated_codes_use_the_alphabet(self) -> None:
        codes = {invites.generate_code() for _ in range(200)}
        assert len(codes) == 200
        assert all(len(c) == 12 and set(c) <= set(invites.ALPHABET) for c in codes)
        assert invites.format_code("K7QM2HXA9RTV") == "K7QM-2HXA-9RTV"


class TestMint:
    def test_documents_hold_no_code(self, fake_cosmos: FakeCosmos) -> None:
        minted = _mint(fake_cosmos, max_uses=3, days=30, count=2)
        assert len(minted) == 2
        for item in minted:
            assert item.code == invites.format_code(invites.normalise_code(item.code))
            doc = _invite_doc(fake_cosmos, item)
            assert doc["id"] == "invite|" + invites.invite_id_for(invites.normalise_code(item.code))
            assert (doc["label"], doc["max_uses"], doc["uses"], doc["status"], doc["redemptions"]) == (
                "Family wave 1", 3, 0, "active", [],
            )
            assert (doc["created_by"], doc["expires_at"], doc["last_redeemed_at"]) == (ADMIN_UID, "2026-11-20T10:00:00Z", None)
            # Expiry plus 90 days, counted from this write.
            assert doc["ttl"] == (30 + 90) * 86400
            stored = json.dumps(fake_cosmos.docs("sys"))
            assert invites.normalise_code(item.code) not in stored and item.code not in stored

    def test_one_audit_entry_per_invite_without_the_code(self, fake_cosmos: FakeCosmos, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("DEBUG"):
            minted = _mint(fake_cosmos, count=3)
        entries = _audit(fake_cosmos, "invite.created")
        assert sorted(e["target"]["id"] for e in entries) == sorted(m.invite_id for m in minted)
        for item in minted:
            assert item.code not in caplog.text and invites.normalise_code(item.code) not in caplog.text

    def test_an_id_collision_mints_another_code(self, fake_cosmos: FakeCosmos) -> None:
        from tests.fakes.cosmos import conflict

        fake_cosmos.fail("create", error=conflict())
        assert len(_mint(fake_cosmos)) == 1

    @pytest.mark.parametrize(
        ("kwargs", "field", "reason"),
        [
            ({"label": ""}, "label", "length"),
            ({"label": "   "}, "label", "length"),
            ({"label": "x" * 41}, "label", "length"),
            ({"label": 7}, "label", "type"),
            ({"max_uses": 0}, "maxUses", "range"),
            ({"max_uses": 11}, "maxUses", "range"),
            ({"max_uses": True}, "maxUses", "type"),
            ({"expires_in_days": 0}, "expiresInDays", "range"),
            ({"expires_in_days": 91}, "expiresInDays", "range"),
            ({"count": 21}, "count", "range"),
            ({"count": 1.5}, "count", "type"),
        ],
    )
    def test_bounds(self, fake_cosmos: FakeCosmos, kwargs: dict, field: str, reason: str) -> None:
        args = {"label": "Wave", "max_uses": 1, "expires_in_days": 30, "count": 1, **kwargs}
        with pytest.raises(ApiError) as caught:
            invites.mint(**args, actor_uid=ADMIN_UID, now=NOW)
        assert caught.value.details == {"fields": [{"field": field, "reason": reason}]}
        assert fake_cosmos.writes() == []


class TestListAndStatus:
    def test_status_is_derived_in_the_documented_order(self, fake_cosmos: FakeCosmos, bob) -> None:
        active, used, expiring, revoked = (_mint(fake_cosmos)[0] for _ in range(4))
        invites.redeem(bob, used.code, accept_language=None, now=NOW)
        invites.revoke(revoked.invite_id, actor_uid=ADMIN_UID, now=NOW)
        later = NOW + timedelta(days=31)
        by_id = {invites.invite_id_of(d): invites.wire_status(d, later) for d in fake_cosmos.docs("sys") if d["type"] == "invite"}
        assert by_id[active.invite_id] == "expired"
        assert by_id[used.invite_id] == "used"  # used wins over expired
        assert by_id[revoked.invite_id] == "revoked"
        assert invites.wire_status(_invite_doc(fake_cosmos, expiring), NOW) == "active"

    def test_newest_first_and_filtered(self, fake_cosmos: FakeCosmos) -> None:
        first = _mint(fake_cosmos, now=NOW)[0]
        second = _mint(fake_cosmos, now=NOW + timedelta(minutes=1))[0]
        invites.revoke(first.invite_id, actor_uid=ADMIN_UID, now=NOW)
        assert [invites.invite_id_of(d) for d in invites.list_invites(status="all", now=NOW)] == [second.invite_id, first.invite_id]
        assert [invites.invite_id_of(d) for d in invites.list_invites(status="revoked", now=NOW)] == [first.invite_id]
        assert [invites.invite_id_of(d) for d in invites.list_invites(status="active", now=NOW)] == [second.invite_id]

    def test_an_unknown_status_is_a_400(self, fake_cosmos: FakeCosmos) -> None:
        with pytest.raises(ApiError) as caught:
            invites.list_invites(status="pending", now=NOW)
        assert caught.value.details == {"fields": [{"field": "status", "reason": "enum"}]}

    def test_the_wire_shape(self, fake_cosmos: FakeCosmos) -> None:
        minted = _mint(fake_cosmos)[0]
        row = invites.list_invites(status="all", now=NOW)[0]
        assert invites.invite_to_wire(row, NOW) == {
            "id": minted.invite_id,
            "label": "Family wave 1",
            "status": "active",
            "createdAt": "2026-10-21T10:00:00Z",
            "createdBy": ADMIN_UID,
            "expiresAt": "2026-11-20T10:00:00Z",
            "maxUses": 1,
            "uses": 0,
            "lastRedeemedAt": None,
        }


class TestWireSchemas:
    def test_lists_and_revocations_match_the_published_schemas(self, fake_cosmos: FakeCosmos, bob, wire_schema) -> None:
        _, used, revoked = (_mint(fake_cosmos, max_uses=1)[0] for _ in range(3))
        invites.redeem(bob, used.code, accept_language=None, now=NOW)
        rows = invites.list_invites(status="all", now=NOW)
        wire_schema("InviteList", {"invites": [invites.invite_to_wire(row, NOW) for row in rows]})
        after_revoke = invites.revoke(revoked.invite_id, actor_uid=ADMIN_UID, now=NOW)
        wire_schema("Invite", invites.invite_to_wire(after_revoke, NOW))
        assert {invites.invite_to_wire(row, NOW)["status"] for row in invites.list_invites(status="all", now=NOW)} == {
            "active", "used", "revoked",
        }

    def test_the_mint_response_matches_the_published_schema(self, fake_cosmos: FakeCosmos, wire_schema) -> None:
        minted = _mint(fake_cosmos, max_uses=5, days=60, count=3)
        wire_schema("InviteCreated", {"invites": [item.to_wire() for item in minted]})

    def test_a_minted_invite_never_shows_its_code_in_a_log_line(self, fake_cosmos: FakeCosmos) -> None:
        minted = _mint(fake_cosmos)[0]
        assert minted.code not in repr(minted) and minted.code not in str(minted)


class TestRevoke:
    def test_revoke_is_idempotent_and_audited_once(self, fake_cosmos: FakeCosmos) -> None:
        minted = _mint(fake_cosmos)[0]
        later = NOW + timedelta(days=2)
        doc = invites.revoke(minted.invite_id, actor_uid=ADMIN_UID, now=later)
        assert (doc["status"], doc["revoked_at"], doc["revoked_by"]) == ("revoked", "2026-10-23T10:00:00Z", ADMIN_UID)
        assert doc["ttl"] == (28 + 90) * 86400  # still expiry + 90 days, from this write
        again = invites.revoke(minted.invite_id, actor_uid=ADMIN_UID, now=later + timedelta(hours=1))
        assert again["revoked_at"] == "2026-10-23T10:00:00Z"
        assert len(_audit(fake_cosmos, "invite.revoked")) == 1

    def test_an_unknown_invite_is_not_found(self, fake_cosmos: FakeCosmos) -> None:
        with pytest.raises(ApiError) as caught:
            invites.revoke("0" * 32, actor_uid=ADMIN_UID, now=NOW)
        assert (caught.value.code, caught.value.details) == ("not_found", {"resource": "invite"})


class TestRedeem:
    def test_the_happy_path(self, fake_cosmos: FakeCosmos, alice) -> None:
        minted = _mint(fake_cosmos)[0]
        me, created = invites.redeem(alice, minted.code.lower(), accept_language="de-CH", now=NOW)
        assert created is True
        assert (me["status"], me["invite_id"], me["locale"]) == ("active", minted.invite_id, "de-CH")
        doc = _invite_doc(fake_cosmos, minted)
        assert doc["uses"] == 1
        assert doc["redemptions"] == [{"uid": alice.uid, "at": "2026-10-21T10:00:00Z"}]
        assert doc["last_redeemed_at"] == "2026-10-21T10:00:00Z"
        registry = fake_cosmos.get(ids.OID_ALICE, "users")
        assert (registry["invite_id"], registry["invite_label"]) == (minted.invite_id, "Family wave 1")
        assert [e["actor"] for e in _audit(fake_cosmos, "invite.redeemed")] == [alice.uid]
        assert fake_cosmos.get("quota|2026-10-21", f"user|{ids.OID_ALICE}")["redeem"] == 1
        # The registry is written before the membership.
        writes = [(op.op, op.pk, op.id) for op in fake_cosmos.writes()]
        assert writes.index(("upsert", "users", ids.OID_ALICE)) < writes.index(("create", f"user|{ids.OID_ALICE}", "me"))

    def test_an_existing_member_consumes_nothing(self, fake_cosmos: FakeCosmos, alice) -> None:
        minted = _mint(fake_cosmos, max_uses=2)
        invites.redeem(alice, minted[0].code, accept_language=None, now=NOW)
        before = len(fake_cosmos.writes())
        me, created = invites.redeem(alice, minted[0].code, accept_language=None, now=NOW)
        assert created is False and me["status"] == "active"
        assert len(fake_cosmos.writes()) == before
        assert _invite_doc(fake_cosmos, minted[0])["uses"] == 1

    def test_a_malformed_code_costs_no_io(self, fake_cosmos: FakeCosmos, alice) -> None:
        assert _redeem_error(alice, "NOT-A-CODE").code == "invite_malformed"
        assert fake_cosmos.journal == []

    def test_an_unknown_code(self, fake_cosmos: FakeCosmos, alice, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("INFO", logger="core.invites"):
            error = _redeem_error(alice, "K7QM-2HXA-9RTV")
        assert (error.code, error.status) == ("invite_not_found", 404)
        assert "K7QM" not in caplog.text and alice.uid in caplog.text
        # A well-formed attempt counts against the quota.
        assert fake_cosmos.get("quota|2026-10-21", f"user|{ids.OID_ALICE}")["redeem"] == 1

    def test_a_used_code(self, fake_cosmos: FakeCosmos, alice, bob) -> None:
        minted = _mint(fake_cosmos)[0]
        invites.redeem(bob, minted.code, accept_language=None, now=NOW)
        error = _redeem_error(alice, minted.code)
        assert (error.code, error.status) == ("invite_used", 409)
        assert fake_cosmos.get("me", f"user|{ids.OID_ALICE}") is None

    def test_an_expired_code(self, fake_cosmos: FakeCosmos, alice) -> None:
        minted = _mint(fake_cosmos, days=1)[0]
        error = _redeem_error(alice, minted.code, now=NOW + timedelta(days=1))
        assert (error.code, error.status) == ("invite_expired", 409)

    def test_a_revoked_code_reads_as_unknown(self, fake_cosmos: FakeCosmos, alice) -> None:
        minted = _mint(fake_cosmos)[0]
        invites.revoke(minted.invite_id, actor_uid=ADMIN_UID, now=NOW)
        assert _redeem_error(alice, minted.code).code == "invite_not_found"

    def test_a_multi_use_code(self, fake_cosmos: FakeCosmos, alice, bob, make_principal) -> None:
        minted = _mint(fake_cosmos, max_uses=2)[0]
        invites.redeem(alice, minted.code, accept_language=None, now=NOW)
        invites.redeem(bob, minted.code, accept_language=None, now=NOW)
        assert _invite_doc(fake_cosmos, minted)["uses"] == 2
        carol = make_principal("c0000000-0000-4000-8000-000000000004")
        assert _redeem_error(carol, minted.code).code == "invite_used"

    @pytest.mark.parametrize(("status", "code"), [("blocked", "account_blocked"), ("deleting", "account_deleted")])
    def test_blocked_and_deleting_accounts_cannot_redeem(self, fake_cosmos: FakeCosmos, alice, status: str, code: str) -> None:
        minted = _mint(fake_cosmos, count=2)
        invites.redeem(alice, minted[0].code, accept_language=None, now=NOW)
        stored = fake_cosmos.get("me", f"user|{ids.OID_ALICE}")
        fake_cosmos.put({**{k: v for k, v in stored.items() if not k.startswith("_")}, "status": status})
        assert _redeem_error(alice, minted[1].code).code == code

    def test_a_deleted_account_cannot_come_back_with_a_new_code(self, fake_cosmos: FakeCosmos, alice) -> None:
        fake_cosmos.put({"id": f"tombstone|{alice.uid}", "pk": "sys", "type": "tombstone", "uid": alice.uid})
        minted = _mint(fake_cosmos)[0]
        error = _redeem_error(alice, minted.code)
        assert (error.code, error.details) == ("account_deleted", {"state": "deleted"})
        assert _invite_doc(fake_cosmos, minted)["uses"] == 0

    def test_an_admin_is_bootstrapped_and_the_code_left_alone(self, fake_cosmos: FakeCosmos, make_principal) -> None:
        admin = make_principal(ids.OID_ADMIN, roles=("admin",))
        minted = _mint(fake_cosmos)[0]
        me, created = invites.redeem(admin, minted.code, accept_language=None, now=NOW)
        assert created is True and me["invite_id"] == "bootstrap-admin"
        assert _invite_doc(fake_cosmos, minted)["uses"] == 0

    def test_ten_attempts_a_day(self, fake_cosmos: FakeCosmos, alice) -> None:
        for _ in range(10):
            assert _redeem_error(alice, "K7QM-2HXA-9RTV").code == "invite_not_found"
        minted = _mint(fake_cosmos)[0]
        error = _redeem_error(alice, minted.code)
        assert (error.code, error.status) == ("quota_exceeded", 429)
        assert _invite_doc(fake_cosmos, minted)["uses"] == 0


class TestRedeemFailures:
    def test_retry_after_the_membership_write_failed(self, fake_cosmos: FakeCosmos, alice) -> None:
        from core import cosmos

        minted = _mint(fake_cosmos)[0]
        fake_cosmos.fail("create", id="me")  # the invite patch landed, then the store went away
        with pytest.raises(cosmos.Unavailable):
            invites.redeem(alice, minted.code, accept_language=None, now=NOW)
        assert _invite_doc(fake_cosmos, minted)["uses"] == 1
        assert fake_cosmos.get("me", f"user|{ids.OID_ALICE}") is None

        # The single-use code is not "used" against its own redeemer.
        me, created = invites.redeem(alice, minted.code, accept_language=None, now=NOW + timedelta(seconds=5))
        assert created is True and me["status"] == "active"
        doc = _invite_doc(fake_cosmos, minted)
        assert doc["uses"] == 1 and len(doc["redemptions"]) == 1

    def test_retry_after_a_patch_that_landed_but_timed_out(self, fake_cosmos: FakeCosmos, alice) -> None:
        from azure.core.exceptions import ServiceResponseError

        from core import cosmos

        minted = _mint(fake_cosmos)[0]
        fake_cosmos.fail("patch", id=f"invite|{minted.invite_id}", after=True, error=ServiceResponseError("timeout"))
        with pytest.raises(cosmos.Unavailable):
            invites.redeem(alice, minted.code, accept_language=None, now=NOW)
        me, _ = invites.redeem(alice, minted.code, accept_language=None, now=NOW)
        assert me["status"] == "active"
        assert _invite_doc(fake_cosmos, minted)["uses"] == 1

    def test_two_people_racing_for_the_last_use(self, fake_cosmos: FakeCosmos, alice, bob) -> None:
        minted = _mint(fake_cosmos)[0]

        def bob_redeems_first(fake: FakeCosmos) -> None:
            invites.redeem(bob, minted.code, accept_language=None, now=NOW)

        fake_cosmos.before("patch", bob_redeems_first, id=f"invite|{minted.invite_id}")
        error = _redeem_error(alice, minted.code)
        assert error.code == "invite_used"
        doc = _invite_doc(fake_cosmos, minted)
        assert doc["uses"] == 1 and [r["uid"] for r in doc["redemptions"]] == [bob.uid]
        assert fake_cosmos.get("me", f"user|{ids.OID_BOB}") is not None
        assert fake_cosmos.get("me", f"user|{ids.OID_ALICE}") is None

    def test_the_same_person_racing_themselves(self, fake_cosmos: FakeCosmos, alice) -> None:
        # A double tap, or a retry fired while the first request still runs.
        minted = _mint(fake_cosmos)[0]

        def first_attempt(fake: FakeCosmos) -> None:
            invites.redeem(alice, minted.code, accept_language=None, now=NOW)

        fake_cosmos.before("patch", first_attempt, id=f"invite|{minted.invite_id}")
        me, _ = invites.redeem(alice, minted.code, accept_language=None, now=NOW + timedelta(seconds=1))
        assert me["status"] == "active"
        doc = _invite_doc(fake_cosmos, minted)
        assert doc["uses"] == 1 and len(doc["redemptions"]) == 1
        assert len(_audit(fake_cosmos, "invite.redeemed")) == 1

    def test_revoked_during_the_attempt(self, fake_cosmos: FakeCosmos, alice) -> None:
        minted = _mint(fake_cosmos)[0]

        def admin_revokes(fake: FakeCosmos) -> None:
            invites.revoke(minted.invite_id, actor_uid=ADMIN_UID, now=NOW)

        fake_cosmos.before("patch", admin_revokes, id=f"invite|{minted.invite_id}")
        assert _redeem_error(alice, minted.code).code == "invite_not_found"


class TestAnonymise:
    def test_the_slot_stays_the_uid_goes(self, fake_cosmos: FakeCosmos, alice, bob) -> None:
        minted = _mint(fake_cosmos, max_uses=2)[0]
        invites.redeem(alice, minted.code, accept_language=None, now=NOW)
        invites.redeem(bob, minted.code, accept_language=None, now=NOW)
        assert invites.anonymise_redemption(minted.invite_id, alice.uid) is True
        doc = _invite_doc(fake_cosmos, minted)
        assert [r["uid"] for r in doc["redemptions"]] == ["deleted", bob.uid]
        assert doc["uses"] == 2
        assert invites.anonymise_redemption(minted.invite_id, alice.uid) is False

    def test_an_invite_that_expired_away_is_nothing_to_do(self, fake_cosmos: FakeCosmos, alice) -> None:
        assert invites.anonymise_redemption("0" * 32, alice.uid) is False

    def test_members_are_unaffected(self, fake_cosmos: FakeCosmos, alice) -> None:
        minted = _mint(fake_cosmos)[0]
        invites.redeem(alice, minted.code, accept_language=None, now=NOW)
        invites.anonymise_redemption(minted.invite_id, alice.uid)
        # Anonymising is a purge step; on its own it removes nobody.
        assert members.get_me(ids.OID_ALICE)["status"] == "active"
