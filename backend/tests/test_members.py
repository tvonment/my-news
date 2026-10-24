"""core.members: the gate, creating a membership (and racing to), the admin
bootstrap, the daily touch, the Me mapping and PATCH /me."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from core import members
from core.errors import ApiError
from tests.fakes import jwks as ids
from tests.fakes.cosmos import FakeCosmos

NOW = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
ALICE_PK = f"user|{ids.OID_ALICE}"

ME_KEYS = {
    "oid", "uid", "roles", "createdAt", "locale", "languages", "timeZone", "settings", "consent",
    "onboarding", "minBuild",
}
SETTINGS_KEYS = {
    "briefSize", "editions", "somethingDifferent", "emphasis", "openInReader", "summaryInList",
    "personalisedRanking", "historyRetentionDays",
}


@pytest.fixture
def alice(make_principal):
    return make_principal(ids.OID_ALICE)


@pytest.fixture
def admin(make_principal):
    return make_principal(ids.OID_ADMIN, roles=("admin",))


def _member(fake: FakeCosmos, principal, *, status: str = "active", **fields: Any) -> dict:
    me, _ = members.create_member(principal, invite_id="i" * 32, invite_label="Wave 1", accept_language="de-CH", now=NOW)
    if status != "active" or fields:
        fake.put({**{k: v for k, v in me.items() if not k.startswith("_")}, "status": status, **fields})
    return fake.get("me", f"user|{principal.oid}")


class TestAcceptLanguage:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (None, ("en", ["en"])),
            ("", ("en", ["en"])),
            ("de-CH", ("de-CH", ["de"])),
            ("de-CH,de;q=0.9,en;q=0.8", ("de-CH", ["de", "en"])),
            ("en-GB,en;q=0.9,de;q=0.5", ("en", ["en", "de"])),
            ("fr-CH, fr;q=0.9, de;q=0.8, en;q=0.7", ("de-CH", ["de", "en"])),
            ("en;q=0.4, de;q=0.6", ("de-CH", ["de", "en"])),
            ("fr-CH", ("en", ["en"])),
            ("de;q=0, en", ("en", ["en"])),
            ("*", ("en", ["en"])),
            ("de;q=banana, en", ("en", ["en"])),
        ],
    )
    def test_prefill(self, header: str | None, expected: tuple[str, list[str]]) -> None:
        assert members.defaults_from_accept_language(header) == expected


class TestGate:
    def test_a_stranger_needs_an_invite_and_nothing_is_written(self, fake_cosmos: FakeCosmos, alice) -> None:
        with pytest.raises(ApiError) as caught:
            members.gate(alice, now=NOW)
        assert (caught.value.code, caught.value.status) == ("invite_required", 403)
        assert fake_cosmos.writes() == []

    def test_an_active_member_passes_with_the_document(self, fake_cosmos: FakeCosmos, alice) -> None:
        _member(fake_cosmos, alice)
        me = members.gate(alice, now=NOW)
        assert me["status"] == "active" and me["_etag"]

    def test_blocked_is_refused_unless_allowed(self, fake_cosmos: FakeCosmos, alice) -> None:
        _member(fake_cosmos, alice, status="blocked", blocked_at="2026-10-20T00:00:00Z")
        with pytest.raises(ApiError) as caught:
            members.gate(alice, now=NOW)
        assert (caught.value.code, caught.value.status) == ("account_blocked", 403)
        assert members.gate(alice, allow_blocked=True, now=NOW)["status"] == "blocked"

    def test_deleting_is_410_unless_allowed(self, fake_cosmos: FakeCosmos, alice) -> None:
        _member(fake_cosmos, alice, status="deleting")
        with pytest.raises(ApiError) as caught:
            members.gate(alice, allow_blocked=True, now=NOW)
        assert (caught.value.code, caught.value.status, caught.value.details) == ("account_deleted", 410, {"state": "deleting"})
        assert members.gate(alice, allow_deleting=True, now=NOW)["status"] == "deleting"

    def test_a_tombstone_answers_410_even_for_an_admin(self, fake_cosmos: FakeCosmos, admin) -> None:
        fake_cosmos.put({"id": f"tombstone|{admin.uid}", "pk": "sys", "type": "tombstone", "uid": admin.uid})
        with pytest.raises(ApiError) as caught:
            members.gate(admin, now=NOW)
        assert caught.value.details == {"state": "deleted"}
        # No resurrection: a stale device must not recreate the account.
        assert fake_cosmos.get("me", f"user|{ids.OID_ADMIN}") is None

    def test_an_admin_bootstraps_registry_first_then_me(self, fake_cosmos: FakeCosmos, admin) -> None:
        me = members.gate(admin, accept_language="en-GB", now=NOW)
        assert me["invite_id"] == "bootstrap-admin" and me["is_admin"] is True
        writes = [(op.op, op.pk) for op in fake_cosmos.writes()]
        assert writes[:2] == [("upsert", "users"), ("create", f"user|{ids.OID_ADMIN}")]
        registry = fake_cosmos.get(ids.OID_ADMIN, "users")
        assert (registry["uid"], registry["invite_id"], registry["invite_label"]) == (admin.uid, "bootstrap-admin", None)
        audit = [d for d in fake_cosmos.docs("sys") if d["type"] == "audit"]
        assert [(d["action"], d["actor"], d["target"]) for d in audit] == [
            ("member.bootstrapped", admin.uid, {"type": "user", "id": admin.uid})
        ]

    def test_concurrent_first_admin_calls_make_one_membership(self, fake_cosmos: FakeCosmos, admin) -> None:
        def other_first_call(fake: FakeCosmos) -> None:
            members.gate(admin, now=NOW)

        fake_cosmos.before("create", other_first_call, id="me")
        me = members.gate(admin, now=NOW + timedelta(seconds=1))
        assert me["created_at"] == "2026-10-21T10:00:00Z"
        assert len([d for d in fake_cosmos.docs("sys") if d["type"] == "audit"]) == 1
        # The registry agrees with the membership that won.
        assert fake_cosmos.get(ids.OID_ADMIN, "users")["created_at"] == me["created_at"]

    def test_an_unknown_status_fails_closed(self, fake_cosmos: FakeCosmos, alice) -> None:
        _member(fake_cosmos, alice, status="paused")
        with pytest.raises(ApiError) as caught:
            members.gate(alice, now=NOW)
        assert caught.value.code == "internal"


class TestTouch:
    def test_once_per_utc_day_on_me_and_the_registry(self, fake_cosmos: FakeCosmos, alice) -> None:
        _member(fake_cosmos, alice)
        tomorrow = NOW + timedelta(days=1)
        before = len(fake_cosmos.writes())
        members.gate(alice, now=tomorrow)
        members.gate(alice, now=tomorrow + timedelta(hours=3))
        touched = fake_cosmos.writes()[before:]
        assert [(op.op, op.pk) for op in touched] == [("patch", ALICE_PK), ("patch", "users")]
        assert fake_cosmos.get("me", ALICE_PK)["last_active_at"] == "2026-10-22T10:00:00Z"
        assert fake_cosmos.get(ids.OID_ALICE, "users")["last_active_at"] == "2026-10-22T10:00:00Z"

    def test_same_day_costs_only_the_read(self, fake_cosmos: FakeCosmos, alice) -> None:
        _member(fake_cosmos, alice)
        before = len(fake_cosmos.writes())
        members.gate(alice, now=NOW + timedelta(hours=1))
        assert len(fake_cosmos.writes()) == before

    def test_a_role_change_is_recorded_the_same_day(self, fake_cosmos: FakeCosmos, alice, make_principal) -> None:
        _member(fake_cosmos, alice)
        promoted = make_principal(ids.OID_ALICE, roles=("admin",))
        members.gate(promoted, now=NOW + timedelta(minutes=5))
        assert fake_cosmos.get(ids.OID_ALICE, "users")["is_admin"] is True

    def test_returns_the_new_etag(self, fake_cosmos: FakeCosmos, alice) -> None:
        _member(fake_cosmos, alice)
        me = members.gate(alice, now=NOW + timedelta(days=1))
        assert me["_etag"] == fake_cosmos.get("me", ALICE_PK)["_etag"]

    def test_a_failed_touch_never_fails_the_request(self, fake_cosmos: FakeCosmos, alice) -> None:
        _member(fake_cosmos, alice)
        fake_cosmos.fail("patch", id="me")
        assert members.gate(alice, now=NOW + timedelta(days=1))["status"] == "active"

    def test_a_missing_registry_is_recreated(self, fake_cosmos: FakeCosmos, alice) -> None:
        _member(fake_cosmos, alice)
        fake_cosmos.get_container().docs.pop(("users", ids.OID_ALICE))
        members.gate(alice, now=NOW + timedelta(days=1))
        assert fake_cosmos.get(ids.OID_ALICE, "users")["uid"] == alice.uid


class TestCreateMember:
    def test_documents_have_no_oid_field_and_no_email(self, fake_cosmos: FakeCosmos, alice) -> None:
        me = _member(fake_cosmos, alice)
        stored = json.dumps(fake_cosmos.get("me", ALICE_PK))
        assert ids.OID_ALICE not in stored.replace(ALICE_PK, "")
        assert "@" not in stored
        assert me["locale"] == "de-CH" and me["languages"] == ["de"]
        assert me["settings"] == members.DEFAULT_SETTINGS
        assert me["consent"] == {"ai_processing": {"granted": False, "version": None, "at": None}}

    def test_a_race_with_another_invite_leaves_the_registry_matching_the_winner(self, fake_cosmos: FakeCosmos, alice) -> None:
        fake_cosmos.put({"id": "invite|" + "a" * 32, "pk": "sys", "type": "invite", "label": "Winner"})

        def winner(fake: FakeCosmos) -> None:
            members.create_member(alice, invite_id="a" * 32, invite_label="Winner", accept_language=None, now=NOW)

        fake_cosmos.before("create", winner, id="me")
        me, created = members.create_member(alice, invite_id="b" * 32, invite_label="Loser", accept_language=None, now=NOW)
        assert created is False and me["invite_id"] == "a" * 32
        registry = fake_cosmos.get(ids.OID_ALICE, "users")
        assert (registry["invite_id"], registry["invite_label"]) == ("a" * 32, "Winner")


class TestMeToWire:
    def test_shape(self, fake_cosmos: FakeCosmos, alice, env) -> None:
        env(MIN_SUPPORTED_BUILD="41")
        wire = members.me_to_wire(_member(fake_cosmos, alice), alice)
        assert set(wire) == ME_KEYS
        assert set(wire["settings"]) == SETTINGS_KEYS
        assert wire["oid"] == ids.OID_ALICE and wire["uid"] == alice.uid
        assert wire["roles"] == []
        assert wire["consent"] == {"aiProcessing": {"granted": False, "version": None}}
        assert wire["onboarding"] == {"completedAt": None}
        assert wire["minBuild"] == 41
        assert wire["createdAt"] == "2026-10-21T10:00:00Z"

    def test_roles_come_from_the_token_not_storage(self, fake_cosmos: FakeCosmos, make_principal) -> None:
        reader = make_principal(ids.OID_BOB, roles=("reader", "admin"))
        me, _ = members.create_member(reader, invite_id="x", invite_label=None, accept_language=None, now=NOW)
        assert members.me_to_wire(me, reader)["roles"] == ["admin", "reader"]

    def test_settings_added_later_read_as_defaults(self, fake_cosmos: FakeCosmos, alice) -> None:
        me = _member(fake_cosmos, alice)
        me["settings"] = {"brief_size": 15}
        settings = members.me_to_wire(me, alice)["settings"]
        assert settings["briefSize"] == 15 and settings["historyRetentionDays"] == 90
        assert settings["editions"] == ["morning", "midday", "evening"]

    def test_no_internal_field_leaks(self, fake_cosmos: FakeCosmos, alice) -> None:
        text = json.dumps(members.me_to_wire(_member(fake_cosmos, alice), alice))
        for internal in ("_etag", "pk", "is_admin", "invite_id", "last_active_at", '"at"', "ttl"):
            assert internal not in text

    def test_matches_the_published_schema(self, fake_cosmos: FakeCosmos, alice, admin, wire_schema) -> None:
        wire_schema("Me", members.me_to_wire(_member(fake_cosmos, alice), alice))
        # An answered consent, a finished onboarding and the admin role too.
        me = members.gate(admin, now=NOW)
        body = {
            "consent": {"aiProcessing": {"granted": True, "version": "2026-10"}},
            "onboarding": {"completed": True},
            "settings": {"emphasis": "big_stories", "editions": ["evening"]},
        }
        wire_schema("Me", members.me_to_wire(members.save_me_patch(admin, me, body, NOW), admin))


def _patch_errors(me: dict, body: Any) -> dict:
    with pytest.raises(ApiError) as caught:
        members.apply_me_patch(me, body, NOW)
    assert (caught.value.code, caught.value.status) == ("validation_failed", 400)
    return caught.value.details


class TestApplyPatch:
    @pytest.fixture
    def me(self, fake_cosmos: FakeCosmos, alice) -> dict:
        return _member(fake_cosmos, alice)

    def test_the_documented_example(self, me: dict) -> None:
        doc, changed = members.apply_me_patch(
            me,
            {
                "timeZone": "Europe/Zurich",
                "languages": ["de", "en"],
                "settings": {"briefSize": 15, "editions": ["morning", "evening"]},
                "consent": {"aiProcessing": {"granted": True, "version": "2026-10"}},
                "onboarding": {"completed": True},
            },
            NOW,
        )
        assert changed == {"languages", "settings", "consent", "onboarding"}
        assert doc["settings"]["brief_size"] == 15 and doc["settings"]["editions"] == ["morning", "evening"]
        assert doc["consent"]["ai_processing"] == {"granted": True, "version": "2026-10", "at": "2026-10-21T10:00:00Z"}
        assert doc["onboarding"]["completed_at"] == "2026-10-21T10:00:00Z"
        assert "_etag" not in doc

    def test_an_empty_body_and_an_empty_settings_object(self, me: dict) -> None:
        assert _patch_errors(me, {}) == {"reason": "empty"}
        assert _patch_errors(me, {"settings": {}}) == {"fields": [{"field": "settings", "reason": "empty"}]}

    @pytest.mark.parametrize("field", ["oid", "uid", "roles", "status", "createdAt", "minBuild"])
    def test_anything_outside_the_whitelist_is_unknown(self, me: dict, field: str) -> None:
        assert _patch_errors(me, {field: "x"}) == {"fields": [{"field": field, "reason": "unknown_field"}]}

    @pytest.mark.parametrize(
        ("body", "field", "reason"),
        [
            ({"locale": "fr"}, "locale", "enum"),
            ({"locale": 1}, "locale", "type"),
            ({"languages": []}, "languages", "empty"),
            ({"languages": "de"}, "languages", "type"),
            ({"languages": ["de", "fr"]}, "languages[1]", "enum"),
            ({"languages": ["de", "de"]}, "languages", "duplicate"),
            ({"timeZone": ""}, "timeZone", "length"),
            ({"timeZone": "Europe/" + "x" * 60}, "timeZone", "length"),
            ({"timeZone": "Europe Zurich"}, "timeZone", "format"),
            ({"timeZone": "Europe//Zurich"}, "timeZone", "format"),
            ({"settings": {"briefSize": 11}}, "settings.briefSize", "enum"),
            ({"settings": {"briefSize": "12"}}, "settings.briefSize", "type"),
            ({"settings": {"briefSize": True}}, "settings.briefSize", "type"),
            ({"settings": {"historyRetentionDays": 365}}, "settings.historyRetentionDays", "enum"),
            ({"settings": {"editions": ["noon"]}}, "settings.editions[0]", "enum"),
            ({"settings": {"emphasis": "bigStories"}}, "settings.emphasis", "enum"),
            ({"settings": {"openInReader": 1}}, "settings.openInReader", "type"),
            ({"settings": {"brief_size": 12}}, "settings.brief_size", "unknown_field"),
            ({"consent": {"aiProcessing": {"granted": True}}}, "consent.aiProcessing.version", "required"),
            ({"consent": {"aiProcessing": {"granted": "yes", "version": "2026-10"}}}, "consent.aiProcessing.granted", "type"),
            ({"consent": {"aiProcessing": {"granted": True, "version": "2026-13"}}}, "consent.aiProcessing.version", "format"),
            ({"consent": {"aiProcessing": {"granted": True, "version": "2026-10", "at": "x"}}}, "consent.aiProcessing.at", "unknown_field"),
            ({"consent": {}}, "consent.aiProcessing", "required"),
            ({"consent": {"terms": {}}}, "consent.aiProcessing", "required"),
            ({"onboarding": {"completed": "yes"}}, "onboarding.completed", "type"),
            ({"onboarding": {}}, "onboarding.completed", "required"),
            ({"onboarding": {"completed": True, "completedAt": "x"}}, "onboarding.completedAt", "unknown_field"),
        ],
    )
    def test_field_errors_name_the_wire_path(self, me: dict, body: dict, field: str, reason: str) -> None:
        assert {"field": field, "reason": reason} in _patch_errors(me, body)["fields"]

    def test_every_problem_is_reported_at_once(self, me: dict) -> None:
        details = _patch_errors(me, {"locale": "fr", "settings": {"briefSize": 11, "colour": "red"}, "x": 1})
        assert details["fields"] == [
            {"field": "locale", "reason": "enum"},
            {"field": "settings.briefSize", "reason": "enum"},
            {"field": "settings.colour", "reason": "unknown_field"},
            {"field": "x", "reason": "unknown_field"},
        ]

    def test_a_replay_changes_nothing(self, me: dict) -> None:
        body = {"settings": {"briefSize": 15}, "consent": {"aiProcessing": {"granted": True, "version": "2026-10"}}}
        first, _ = members.apply_me_patch(me, body, NOW)
        second, changed = members.apply_me_patch(first, body, NOW + timedelta(hours=2))
        assert changed == frozenset()
        # The consent time is the first answer's, not the replay's.
        assert second["consent"]["ai_processing"]["at"] == "2026-10-21T10:00:00Z"

    def test_onboarding_completes_once_and_can_be_reset(self, me: dict) -> None:
        done, _ = members.apply_me_patch(me, {"onboarding": {"completed": True}}, NOW)
        again, changed = members.apply_me_patch(done, {"onboarding": {"completed": True}}, NOW + timedelta(days=1))
        assert changed == frozenset() and again["onboarding"]["completed_at"] == "2026-10-21T10:00:00Z"
        reset, changed = members.apply_me_patch(again, {"onboarding": {"completed": False}}, NOW)
        assert changed == {"onboarding"} and reset["onboarding"]["completed_at"] is None

    def test_a_changed_consent_is_restamped(self, me: dict) -> None:
        granted, _ = members.apply_me_patch(me, {"consent": {"aiProcessing": {"granted": True, "version": "2026-10"}}}, NOW)
        later = NOW + timedelta(days=3)
        withdrawn, changed = members.apply_me_patch(granted, {"consent": {"aiProcessing": {"granted": False, "version": "2026-10"}}}, later)
        assert changed == {"consent"}
        assert withdrawn["consent"]["ai_processing"] == {"granted": False, "version": "2026-10", "at": "2026-10-24T10:00:00Z"}

    def test_not_an_object(self, me: dict) -> None:
        assert _patch_errors(me, ["locale"]) == {"reason": "not_object"}


class TestSavePatch:
    def test_saves_and_mirrors_locale_to_the_registry(self, fake_cosmos: FakeCosmos, alice) -> None:
        me = _member(fake_cosmos, alice)
        saved = members.save_me_patch(alice, me, {"locale": "en", "languages": ["en", "de"]}, NOW)
        assert (saved["locale"], saved["languages"]) == ("en", ["en", "de"])
        registry = fake_cosmos.get(ids.OID_ALICE, "users")
        assert (registry["locale"], registry["languages"]) == ("en", ["en", "de"])

    def test_settings_alone_leave_the_registry_alone(self, fake_cosmos: FakeCosmos, alice) -> None:
        me = _member(fake_cosmos, alice)
        before = len(fake_cosmos.writes())
        members.save_me_patch(alice, me, {"settings": {"briefSize": 10}}, NOW)
        assert [op.op for op in fake_cosmos.writes()[before:]] == ["replace"]

    def test_a_no_op_patch_writes_nothing(self, fake_cosmos: FakeCosmos, alice) -> None:
        me = _member(fake_cosmos, alice)
        before = len(fake_cosmos.writes())
        members.save_me_patch(alice, me, {"settings": {"briefSize": 12}}, NOW)
        assert len(fake_cosmos.writes()) == before

    def test_a_concurrent_write_is_re_read_and_re_applied(self, fake_cosmos: FakeCosmos, alice) -> None:
        me = _member(fake_cosmos, alice)

        def concurrent_touch(fake: FakeCosmos) -> None:
            from core import cosmos

            cosmos.patch("me", ALICE_PK, [{"op": "set", "path": "/last_active_at", "value": "2026-10-21T10:00:01Z"}])

        fake_cosmos.before("replace", concurrent_touch, id="me")
        saved = members.save_me_patch(alice, me, {"settings": {"briefSize": 15}}, NOW)
        stored = fake_cosmos.get("me", ALICE_PK)
        # Both writes survive: ours and the one that raced it.
        assert stored["settings"]["brief_size"] == 15 and stored["last_active_at"] == "2026-10-21T10:00:01Z"
        assert saved["_etag"] == stored["_etag"]

    def test_persistent_contention_is_a_503_never_a_412(self, fake_cosmos: FakeCosmos, alice) -> None:
        me = _member(fake_cosmos, alice)
        bump = 0

        def keep_writing(fake: FakeCosmos) -> None:
            nonlocal bump
            bump += 1
            from core import cosmos

            cosmos.patch("me", ALICE_PK, [{"op": "set", "path": "/bump", "value": bump}])

        fake_cosmos.before("replace", keep_writing, id="me", times=2)
        with pytest.raises(ApiError) as caught:
            members.save_me_patch(alice, me, {"settings": {"briefSize": 15}}, NOW)
        assert (caught.value.code, caught.value.details, caught.value.retry_after) == (
            "degraded",
            {"dependency": "database", "reason": "contention"},
            1,
        )

    def test_a_block_that_lands_first_wins(self, fake_cosmos: FakeCosmos, alice) -> None:
        me = _member(fake_cosmos, alice)

        def admin_blocks(fake: FakeCosmos) -> None:
            from core import cosmos

            cosmos.patch("me", ALICE_PK, [{"op": "set", "path": "/status", "value": "blocked"}])

        fake_cosmos.before("replace", admin_blocks, id="me")
        with pytest.raises(ApiError) as caught:
            members.save_me_patch(alice, me, {"settings": {"briefSize": 15}}, NOW)
        assert caught.value.code == "account_blocked"
        assert fake_cosmos.get("me", ALICE_PK)["settings"]["brief_size"] == 12
