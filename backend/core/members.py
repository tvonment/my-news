"""Membership: the member gate, creating a membership, and the caller's own
``me`` document (docs/data-model.md sections 4.1 and 4.2).

The existence of ``user|{oid}/me`` *is* the membership. The gate is one point
read of it per request (~1 RU), and the handler reuses the document it read, so
no member route reads ``me`` twice. Only redemption (core.invites.redeem) and
the admin bootstrap create it, and they create the ``users/{oid}`` registry
document first, so a member never lacks one.

Storage is snake_case and is never returned as it is stored: me_to_wire() is
the one mapping to the ``Me`` wire shape, so internal fields (pk, _etag,
consent timestamps, the admin flag) cannot leak into a response.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping

from core import audit, cosmos
from core.config import min_supported_build
from core.errors import ApiError, degraded
from core.models import (
    BRIEF_SIZES,
    CONSENT_VERSION_RE,
    CONTENT_LANGUAGES,
    EMPHASIS,
    HISTORY_RETENTION_DAYS,
    LOCALES,
    ME_ID,
    MEMBER_ACTIVE,
    MEMBER_BLOCKED,
    MEMBER_DELETING,
    PK_SYS,
    PK_USERS,
    SLOTS,
    SYSTEM_PROPERTIES,
    T_ME,
    T_USER,
    invite_doc_id,
    iso,
    tombstone_doc_id,
    user_pk,
    utcnow,
)

if TYPE_CHECKING:
    from core.auth import Principal

log = logging.getLogger(__name__)

# The invite id recorded for a membership the admin role created by itself.
BOOTSTRAP_INVITE_ID = "bootstrap-admin"
# The oldest consent text version that still counts as consent (P3: below it,
# routes that send the member's own text to a model answer consent_required).
CONSENT_MIN_VERSION = "2026-10"
DEFAULT_TIME_ZONE = "Europe/Zurich"
MAX_TIME_ZONE_CHARS = 64
# Bounds the work an Accept-Language header can cause before anything is read.
MAX_ACCEPT_LANGUAGE_CHARS = 512

DEFAULT_SETTINGS: dict[str, Any] = {
    "brief_size": 12,
    "editions": ["morning", "midday", "evening"],
    "something_different": True,
    "emphasis": "balanced",
    "open_in_reader": False,
    "summary_in_list": True,
    "personalised_ranking": True,
    "history_retention_days": 90,
}
SETTINGS_WIRE_TO_DOC = {
    "briefSize": "brief_size",
    "editions": "editions",
    "somethingDifferent": "something_different",
    "emphasis": "emphasis",
    "openInReader": "open_in_reader",
    "summaryInList": "summary_in_list",
    "personalisedRanking": "personalised_ranking",
    "historyRetentionDays": "history_retention_days",
}

_TIME_ZONE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_+-/")


# ---------------------------------------------------------------------------
# Accept-Language
# ---------------------------------------------------------------------------
def _language_ranges(header: str | None) -> list[str]:
    """Primary subtags in preference order (q descending, then header order),
    without q=0 ("not acceptable") and without malformed entries."""
    if not header:
        return []
    ranked: list[tuple[float, int, str]] = []
    for position, entry in enumerate(header[:MAX_ACCEPT_LANGUAGE_CHARS].split(",")):
        tag, _, params = entry.strip().partition(";")
        tag = tag.strip().lower()
        if not tag or tag == "*":
            continue
        weight = 1.0
        params = params.strip()
        if params:
            name, _, value = params.partition("=")
            if name.strip() != "q":
                continue
            try:
                weight = float(value)
            except ValueError:
                continue
            if not 0.0 < weight <= 1.0:
                continue
        ranked.append((weight, position, tag.split("-")[0]))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [primary for _, _, primary in ranked]


def defaults_from_accept_language(header: str | None) -> tuple[str, list[str]]:
    """(locale, languages) to prefill a new membership. German anywhere before
    English means the Swiss German UI; content languages keep the header's
    order; with neither language, English."""
    ranges = [primary for primary in _language_ranges(header) if primary in CONTENT_LANGUAGES]
    if not ranges:
        return "en", ["en"]
    locale = "de-CH" if ranges[0] == "de" else "en"
    languages: list[str] = []
    for primary in ranges:
        if primary not in languages:
            languages.append(primary)
    return locale, languages


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def get_me(oid: str) -> dict[str, Any] | None:
    return cosmos.read(ME_ID, user_pk(oid))


def tombstone_exists(uid: str) -> bool:
    return cosmos.read(tombstone_doc_id(uid), PK_SYS) is not None


def _refuse_for_status(me: Mapping[str, Any], *, allow_blocked: bool, allow_deleting: bool) -> None:
    status = me.get("status")
    if status == MEMBER_ACTIVE:
        return
    if status == MEMBER_BLOCKED:
        if not allow_blocked:
            raise ApiError("account_blocked")
        return
    if status == MEMBER_DELETING:
        if not allow_deleting:
            raise ApiError("account_deleted", details={"state": "deleting"})
        return
    # A status this code never writes: refuse rather than guess.
    log.error("Membership has unknown status %r", status)
    raise ApiError("internal")


def gate(
    principal: "Principal",
    *,
    allow_blocked: bool = False,
    allow_deleting: bool = False,
    accept_language: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The member gate (docs/api.md section 2.2): the caller's ``me``, or the
    error that decides what the app shows."""
    at = now or utcnow()
    me = get_me(principal.oid)
    if me is not None:
        _refuse_for_status(me, allow_blocked=allow_blocked, allow_deleting=allow_deleting)
        if me.get("status") == MEMBER_ACTIVE:
            return touch_active(principal, me, at)
        return me
    if tombstone_exists(principal.uid):
        # Deleted, and the Entra user may still sign in. 410 rather than
        # invite_required, and no bootstrap: a stale device must not resurrect
        # an account its owner just deleted.
        raise ApiError("account_deleted", details={"state": "deleted"})
    if principal.is_admin:
        return bootstrap_admin(principal, accept_language=accept_language, now=at)
    raise ApiError("invite_required")


# ---------------------------------------------------------------------------
# Creating a membership
# ---------------------------------------------------------------------------
def new_me_doc(principal: "Principal", *, invite_id: str, accept_language: str | None, now: datetime) -> dict[str, Any]:
    locale, languages = defaults_from_accept_language(accept_language)
    ts = iso(now)
    return {
        "id": ME_ID,
        # The oid lives only in the partition key, never in a field.
        "pk": user_pk(principal.oid),
        "type": T_ME,
        "v": 1,
        "status": MEMBER_ACTIVE,
        "created_at": ts,
        "updated_at": ts,
        "invite_id": invite_id,
        "locale": locale,
        "languages": languages,
        "time_zone": DEFAULT_TIME_ZONE,
        "settings": copy.deepcopy(DEFAULT_SETTINGS),
        "consent": {"ai_processing": {"granted": False, "version": None, "at": None}},
        "onboarding": {"completed_at": None},
        "last_active_at": ts,
        "is_admin": principal.is_admin,
    }


def new_registry_doc(
    principal: "Principal",
    me: Mapping[str, Any],
    *,
    invite_id: str,
    invite_label: str | None,
    now: datetime,
) -> dict[str, Any]:
    return {
        "id": principal.oid,
        "pk": PK_USERS,
        "type": T_USER,
        "v": 1,
        "uid": principal.uid,
        "status": me.get("status", MEMBER_ACTIVE),
        "is_admin": principal.is_admin,
        "created_at": me["created_at"],
        "invite_id": invite_id,
        "invite_label": invite_label,
        "locale": me["locale"],
        "languages": list(me["languages"]),
        "last_active_at": me.get("last_active_at", me["created_at"]),
        "updated_at": iso(now),
    }


def _invite_label(invite_id: str) -> str | None:
    if invite_id == BOOTSTRAP_INVITE_ID:
        return None
    doc = cosmos.read(invite_doc_id(invite_id), PK_SYS)
    return doc.get("label") if doc else None


def create_member(
    principal: "Principal",
    *,
    invite_id: str,
    invite_label: str | None,
    accept_language: str | None,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    """Create the registry document, then ``me``. Returns (me, created).

    Safe to repeat and to race: a 409 on ``me`` means a concurrent request (or
    an earlier attempt) created the membership, and its document is used. The
    registry is then rewritten from that winning ``me``, because the loser's
    upsert may have landed last with its own created_at or invite id, and the
    purge reads the invite id from the registry to anonymise the redemption.
    """
    me = new_me_doc(principal, invite_id=invite_id, accept_language=accept_language, now=now)
    cosmos.upsert(new_registry_doc(principal, me, invite_id=invite_id, invite_label=invite_label, now=now))
    try:
        return cosmos.create(me), True
    except cosmos.AlreadyExists:
        existing = get_me(principal.oid)
        if existing is None:
            # Created and already purged in between; nothing sensible to return.
            raise degraded("database", "contention", retry_after=1) from None
        winner_invite = existing.get("invite_id", invite_id)
        label = invite_label if winner_invite == invite_id else _invite_label(winner_invite)
        cosmos.upsert(new_registry_doc(principal, existing, invite_id=winner_invite, invite_label=label, now=now))
        return existing, False


def bootstrap_admin(principal: "Principal", *, accept_language: str | None, now: datetime) -> dict[str, Any]:
    """A membership for an admin-role token, without an invite (docs/api.md
    section 2.4). Concurrent first calls are safe: one create wins."""
    me, created = create_member(
        principal,
        invite_id=BOOTSTRAP_INVITE_ID,
        invite_label=None,
        accept_language=accept_language,
        now=now,
    )
    if created:
        audit.record(
            "member.bootstrapped",
            actor=principal.uid,
            target_type="user",
            target_id=principal.uid,
            now=now,
        )
    return me


def touch_active(principal: "Principal", me: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Refresh ``last_active_at`` and ``is_admin`` at most once per UTC day, on
    ``me`` and on the registry. Best-effort: a failed touch never fails the
    request, it only means the admin list is a day staler. Returns the newest
    ``me`` (with its new ``_etag``)."""
    ts = iso(now)
    if str(me.get("last_active_at", ""))[:10] == ts[:10] and me.get("is_admin") == principal.is_admin:
        return me
    operations: list[dict[str, Any]] = [
        {"op": "set", "path": "/last_active_at", "value": ts},
        {"op": "set", "path": "/is_admin", "value": principal.is_admin},
        {"op": "set", "path": "/updated_at", "value": ts},
    ]
    try:
        me = cosmos.patch(ME_ID, user_pk(principal.oid), operations)
    except cosmos.CosmosError as exc:
        log.warning("Daily touch of me failed uid=%s: %s", principal.uid, type(exc).__name__)
        return me
    try:
        cosmos.patch(principal.oid, PK_USERS, operations)
    except cosmos.NotFound:
        # A member without a registry document should not exist (it is written
        # first and deleted last). Recreate it so the admin list and the
        # maintenance timer can see the account again.
        log.warning("Registry document missing for uid=%s; recreating it", principal.uid)
        try:
            cosmos.upsert(
                new_registry_doc(principal, me, invite_id=me.get("invite_id", ""), invite_label=None, now=now)
            )
        except cosmos.CosmosError as exc:
            log.warning("Registry recreate failed uid=%s: %s", principal.uid, type(exc).__name__)
    except cosmos.CosmosError as exc:
        log.warning("Daily touch of the registry failed uid=%s: %s", principal.uid, type(exc).__name__)
    return me


# ---------------------------------------------------------------------------
# The wire shape
# ---------------------------------------------------------------------------
def me_to_wire(me: Mapping[str, Any], principal: "Principal") -> dict[str, Any]:
    """The ``Me`` response. Every settings key is present: keys added after a
    document was written read as their defaults."""
    settings = {**DEFAULT_SETTINGS, **(me.get("settings") or {})}
    ai = (me.get("consent") or {}).get("ai_processing") or {}
    return {
        "oid": principal.oid,
        "uid": principal.uid,
        "roles": list(principal.roles),
        "createdAt": me["created_at"],
        "locale": me.get("locale") or "en",
        "languages": list(me.get("languages") or ["en"]),
        "timeZone": me.get("time_zone") or DEFAULT_TIME_ZONE,
        "settings": {wire: copy.deepcopy(settings[doc]) for wire, doc in SETTINGS_WIRE_TO_DOC.items()},
        "consent": {"aiProcessing": {"granted": bool(ai.get("granted", False)), "version": ai.get("version")}},
        "onboarding": {"completedAt": (me.get("onboarding") or {}).get("completed_at")},
        "minBuild": min_supported_build(),
    }


# ---------------------------------------------------------------------------
# PATCH /me
# ---------------------------------------------------------------------------
class _Errors:
    """Collects every field problem, so one 400 names them all."""

    def __init__(self) -> None:
        self.fields: list[dict[str, str]] = []

    def add(self, field_path: str, reason: str) -> None:
        if len(self.fields) < 50:
            self.fields.append({"field": field_path, "reason": reason})

    def raise_if_any(self) -> None:
        if self.fields:
            raise ApiError("validation_failed", details={"fields": self.fields})


def _is_int(value: Any) -> bool:
    # JSON true is a Python bool, and bool is an int: not here.
    return isinstance(value, int) and not isinstance(value, bool)


def _string_set(value: Any, allowed: tuple[str, ...], path: str, errors: _Errors) -> list[str] | None:
    """A non-empty array of distinct values from ``allowed``, in the order sent."""
    if not isinstance(value, list):
        errors.add(path, "type")
        return None
    if not value:
        errors.add(path, "empty")
        return None
    ok = True
    for index, item in enumerate(value):
        if not isinstance(item, str):
            errors.add(f"{path}[{index}]", "type")
            ok = False
        elif item not in allowed:
            errors.add(f"{path}[{index}]", "enum")
            ok = False
    if ok and len(set(value)) != len(value):
        errors.add(path, "duplicate")
        ok = False
    return list(value) if ok else None


def _valid_time_zone(value: str) -> bool:
    # The IANA name grammar (Common.schema.json TimeZone); not a lookup in a
    # tz database, which the Functions image is not guaranteed to carry.
    if not 1 <= len(value) <= MAX_TIME_ZONE_CHARS or not set(value) <= _TIME_ZONE_CHARS:
        return False
    return all(part for part in value.split("/"))


def _validate_settings(raw: Any, errors: _Errors) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        errors.add("settings", "type")
        return None
    if not raw:
        errors.add("settings", "empty")
        return None
    out: dict[str, Any] = {}
    for key, value in raw.items():
        path = f"settings.{key}"
        doc_key = SETTINGS_WIRE_TO_DOC.get(key)
        if doc_key is None:
            errors.add(path, "unknown_field")
        elif key in ("briefSize", "historyRetentionDays"):
            allowed = BRIEF_SIZES if key == "briefSize" else HISTORY_RETENTION_DAYS
            if not _is_int(value):
                errors.add(path, "type")
            elif value not in allowed:
                errors.add(path, "enum")
            else:
                out[doc_key] = value
        elif key == "editions":
            editions = _string_set(value, SLOTS, path, errors)
            if editions is not None:
                out[doc_key] = editions
        elif key == "emphasis":
            if not isinstance(value, str):
                errors.add(path, "type")
            elif value not in EMPHASIS:
                errors.add(path, "enum")
            else:
                out[doc_key] = value
        else:
            if not isinstance(value, bool):
                errors.add(path, "type")
            else:
                out[doc_key] = value
    return out


def _validate_patch(body: Mapping[str, Any]) -> dict[str, Any]:
    """MePatch, strictly: unknown fields are 400s, so a client typo fails loudly."""
    if not isinstance(body, Mapping):
        raise ApiError("validation_failed", details={"reason": "not_object"})
    if not body:
        raise ApiError("validation_failed", details={"reason": "empty"})
    errors = _Errors()
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key == "locale":
            if not isinstance(value, str):
                errors.add(key, "type")
            elif value not in LOCALES:
                errors.add(key, "enum")
            else:
                out[key] = value
        elif key == "languages":
            languages = _string_set(value, CONTENT_LANGUAGES, key, errors)
            if languages is not None:
                out[key] = languages
        elif key == "timeZone":
            if not isinstance(value, str):
                errors.add(key, "type")
            elif not 1 <= len(value) <= MAX_TIME_ZONE_CHARS:
                errors.add(key, "length")
            elif not _valid_time_zone(value):
                errors.add(key, "format")
            else:
                out[key] = value
        elif key == "settings":
            settings = _validate_settings(value, errors)
            if settings:
                out[key] = settings
        elif key == "consent":
            consent = _validate_consent(value, errors)
            if consent is not None:
                out[key] = consent
        elif key == "onboarding":
            if not isinstance(value, dict):
                errors.add(key, "type")
                continue
            for extra in sorted(value.keys() - {"completed"}):
                errors.add(f"onboarding.{extra}", "unknown_field")
            if "completed" not in value:
                errors.add("onboarding.completed", "required")
            elif not isinstance(value["completed"], bool):
                errors.add("onboarding.completed", "type")
            else:
                out[key] = value["completed"]
        else:
            errors.add(key, "unknown_field")
    errors.raise_if_any()
    return out


def _validate_consent(value: Any, errors: _Errors) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.add("consent", "type")
        return None
    for extra in sorted(value.keys() - {"aiProcessing"}):
        errors.add(f"consent.{extra}", "unknown_field")
    ai = value.get("aiProcessing")
    if ai is None:
        errors.add("consent.aiProcessing", "required")
        return None
    if not isinstance(ai, dict):
        errors.add("consent.aiProcessing", "type")
        return None
    for extra in sorted(ai.keys() - {"granted", "version"}):
        errors.add(f"consent.aiProcessing.{extra}", "unknown_field")
    granted, version = ai.get("granted"), ai.get("version")
    ok = True
    if "granted" not in ai:
        errors.add("consent.aiProcessing.granted", "required")
        ok = False
    elif not isinstance(granted, bool):
        errors.add("consent.aiProcessing.granted", "type")
        ok = False
    if "version" not in ai:
        errors.add("consent.aiProcessing.version", "required")
        ok = False
    elif not isinstance(version, str):
        errors.add("consent.aiProcessing.version", "type")
        ok = False
    elif not CONSENT_VERSION_RE.match(version):
        errors.add("consent.aiProcessing.version", "format")
        ok = False
    return {"granted": granted, "version": version} if ok else None


def apply_me_patch(me: Mapping[str, Any], body: Mapping[str, Any], now: datetime) -> tuple[dict[str, Any], frozenset[str]]:
    """Validate ``body`` and apply it to a copy of ``me``.

    Returns the new document and the top-level wire keys whose stored value
    actually changed: an unchanged PATCH writes nothing, which is what makes
    replays free and idempotent.
    """
    patch = _validate_patch(body)
    doc = {k: copy.deepcopy(v) for k, v in me.items() if k not in SYSTEM_PROPERTIES}
    changed: set[str] = set()
    ts = iso(now)

    if "locale" in patch and doc.get("locale") != patch["locale"]:
        doc["locale"] = patch["locale"]
        changed.add("locale")
    if "languages" in patch and doc.get("languages") != patch["languages"]:
        doc["languages"] = patch["languages"]
        changed.add("languages")
    if "timeZone" in patch and doc.get("time_zone") != patch["timeZone"]:
        doc["time_zone"] = patch["timeZone"]
        changed.add("timeZone")
    if "settings" in patch:
        current = {**DEFAULT_SETTINGS, **(doc.get("settings") or {})}
        merged = {**current, **patch["settings"]}
        if merged != current:
            doc["settings"] = merged
            changed.add("settings")
    if "consent" in patch:
        consent = doc.setdefault("consent", {})
        ai = consent.get("ai_processing") or {}
        wanted = patch["consent"]
        if ai.get("granted") != wanted["granted"] or ai.get("version") != wanted["version"]:
            # The server stamps the answer's time; a replay of the same answer
            # keeps the first stamp.
            consent["ai_processing"] = {"granted": wanted["granted"], "version": wanted["version"], "at": ts}
            changed.add("consent")
    if "onboarding" in patch:
        onboarding = doc.setdefault("onboarding", {"completed_at": None})
        if patch["onboarding"] and not onboarding.get("completed_at"):
            onboarding["completed_at"] = ts
            changed.add("onboarding")
        elif not patch["onboarding"] and onboarding.get("completed_at"):
            onboarding["completed_at"] = None
            changed.add("onboarding")
    if changed:
        doc["updated_at"] = ts
    return doc, frozenset(changed)


def save_me_patch(principal: "Principal", me: Mapping[str, Any], body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """PATCH /me: apply, then replace ``me`` if it is still the version the gate
    read. On a 412 (a concurrent write, say the daily touch or an admin block)
    re-read, re-check the gate's verdict and re-apply once; a second 412 is
    ``503 degraded`` with reason "contention" (a 412 never reaches a client).
    ``locale`` and ``languages`` are mirrored to the registry, which the cycle
    reads to know which summary languages are in use."""
    current: Mapping[str, Any] = me
    for attempt in range(2):
        new_doc, changed = apply_me_patch(current, body, now)
        if not changed:
            return dict(current)
        try:
            saved = cosmos.replace_if_match(new_doc, current["_etag"])
            break
        except cosmos.NotFound:
            raise ApiError("account_deleted", details={"state": "deleted"}) from None
        except cosmos.PreconditionFailed:
            if attempt == 1:
                raise degraded("database", "contention", retry_after=1) from None
            fresh = get_me(principal.oid)
            if fresh is None:
                raise ApiError("account_deleted", details={"state": "deleted"}) from None
            _refuse_for_status(fresh, allow_blocked=False, allow_deleting=False)
            current = fresh
    if changed & {"locale", "languages"}:
        try:
            cosmos.patch(
                principal.oid,
                PK_USERS,
                [
                    {"op": "set", "path": "/locale", "value": saved["locale"]},
                    {"op": "set", "path": "/languages", "value": saved["languages"]},
                    {"op": "set", "path": "/updated_at", "value": iso(now)},
                ],
            )
        except cosmos.CosmosError as exc:
            # The member's own document is authoritative; the mirror catches up
            # on the next locale change. Not worth failing a saved PATCH for.
            log.warning("Registry locale mirror failed uid=%s: %s", principal.uid, type(exc).__name__)
    return saved
