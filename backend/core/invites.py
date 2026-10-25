"""Invite codes: minting, listing, revoking and redeeming (docs/api.md section
2.3, docs/data-model.md section 4.4).

A code is 12 symbols of Crockford's Base32 (60 random bits from ``secrets``),
shown as ``XXXX-XXXX-XXXX``. The database never holds a usable code: the
document id is ``invite|`` + the first 32 hex of ``sha256("mynews-invite:" +
code)``, and the plaintext exists once, in the POST /admin/invites response.
Codes are never logged; failed redemptions log the caller's uid only.

Redemption is one conditional patch on the invite: increment ``uses`` and append
``{uid, at}`` to ``redemptions`` only while the invite is active, has a use
left, has not expired, **and does not list this uid yet**. The last condition is
what makes a retry after a partial failure safe: if an earlier attempt got as
far as the patch and then died, the retry's patch fails, the re-read finds the
uid, and redemption simply continues to the membership step instead of spending
a second use or refusing the caller their own code.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Mapping

from core import audit, cosmos, members, quotas
from core.errors import ApiError, invalid, not_found
from core.models import (
    INVITE_ACTIVE,
    INVITE_KEEP_AFTER_EXPIRY,
    INVITE_REVOKED,
    MEMBER_ACTIVE,
    MEMBER_BLOCKED,
    MEMBER_DELETING,
    PK_SYS,
    T_INVITE,
    invite_doc_id,
    iso,
    parse_iso,
    utcnow,
)

if TYPE_CHECKING:
    from core.auth import Principal

log = logging.getLogger(__name__)

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 12
HASH_PREFIX = "mynews-invite:"
MAX_RAW_CODE_CHARS = 64
LABEL_MAX = 40
MAX_USES = (1, 10)
EXPIRES_IN_DAYS = (1, 90)
MINT_COUNT = (1, 20)
LIST_LIMIT = 200
LIST_STATUSES = ("active", "used", "expired", "revoked", "all")
# A redeemed slot's uid once that account is purged.
DELETED_UID = "deleted"

# What people type between groups, or paste from a chat: whitespace (including
# the no-break space messengers insert), hyphens and the dash family.
_STRIP = dict.fromkeys(
    map(ord, " \t\r\n\u00a0-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), None
)
# Crockford's decoding: letters that look like digits read as those digits.
_CONFUSABLES = str.maketrans({"O": "0", "I": "1", "L": "1"})
_ALPHABET_SET = frozenset(ALPHABET)

# Q1 in docs/data-model.md: the newest invites, served by the (type,
# created_at desc) composite. The status filter runs afterwards in Python,
# because "used" and "expired" are derived, not stored.
SQL_INVITES_LIST = (
    "SELECT TOP @limit c.id, c.label, c.status, c.created_at, c.created_by, c.expires_at, "
    "c.max_uses, c.uses, c.last_redeemed_at FROM c WHERE c.type = @type "
    "ORDER BY c.type ASC, c.created_at DESC"
)


def generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def format_code(code: str) -> str:
    return f"{code[0:4]}-{code[4:8]}-{code[8:12]}"


def normalise_code(raw: str) -> str:
    """What the member typed, as the 12 symbols it was minted as, or
    ``400 invite_malformed`` (before any I/O, and before the quota)."""
    if not isinstance(raw, str) or len(raw) > MAX_RAW_CODE_CHARS:
        raise ApiError("invite_malformed")
    code = raw.translate(_STRIP).upper().translate(_CONFUSABLES)
    if len(code) != CODE_LENGTH or not set(code) <= _ALPHABET_SET:
        raise ApiError("invite_malformed")
    return code


def invite_id_for(code: str) -> str:
    """The invite's public id (32 hex) for an already normalised code."""
    return hashlib.sha256((HASH_PREFIX + code).encode("ascii")).hexdigest()[:32]


def _ttl(expires_at: datetime, now: datetime) -> int:
    """Seconds from this write until expiry + 90 days. TTL counts from the last
    write, so every write to an invite sets it again."""
    return max(1, int((expires_at + timedelta(seconds=INVITE_KEEP_AFTER_EXPIRY) - now).total_seconds()))


@dataclass(frozen=True)
class MintedInvite:
    invite_id: str
    # Plaintext, formatted. Exists only in the mint response, and out of repr so
    # that logging a MintedInvite cannot leak it.
    code: str = field(repr=False)
    label: str
    expires_at: str
    max_uses: int

    def to_wire(self) -> dict[str, Any]:
        """One item of ``InviteCreated``: the only body that ever carries a code."""
        return {
            "id": self.invite_id,
            "code": self.code,
            "label": self.label,
            "expiresAt": self.expires_at,
            "maxUses": self.max_uses,
        }


def _int_in(value: Any, bounds: tuple[int, int], field_name: str) -> ApiError | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return invalid(field_name, "type")
    if not bounds[0] <= value <= bounds[1]:
        return invalid(field_name, "range")
    return None


def _validated_label(label: Any) -> str:
    if not isinstance(label, str):
        raise invalid("label", "type")
    label = label.strip()
    if not 1 <= len(label) <= LABEL_MAX:
        raise invalid("label", "length")
    return label


def mint(
    *,
    label: str,
    max_uses: int,
    expires_in_days: int,
    count: int,
    actor_uid: str,
    now: datetime,
) -> list[MintedInvite]:
    """Create ``count`` invites sharing a label, expiry and use limit. Not
    idempotent: every call mints new codes. One audit entry per invite."""
    clean_label = _validated_label(label)
    for value, bounds, name in (
        (max_uses, MAX_USES, "maxUses"),
        (expires_in_days, EXPIRES_IN_DAYS, "expiresInDays"),
        (count, MINT_COUNT, "count"),
    ):
        error = _int_in(value, bounds, name)
        if error is not None:
            raise error
    expires = now + timedelta(days=expires_in_days)
    minted: list[MintedInvite] = []
    for _ in range(count):
        for attempt in range(2):
            code = generate_code()
            invite_id = invite_id_for(code)
            doc = {
                "id": invite_doc_id(invite_id),
                "pk": PK_SYS,
                "type": T_INVITE,
                "v": 1,
                "label": clean_label,
                "created_at": iso(now),
                "created_by": actor_uid,
                "expires_at": iso(expires),
                "max_uses": max_uses,
                "uses": 0,
                "status": INVITE_ACTIVE,
                "redemptions": [],
                "last_redeemed_at": None,
                "ttl": _ttl(expires, now),
            }
            try:
                cosmos.create(doc)
                break
            except cosmos.AlreadyExists:
                # 60 bits make a collision vanishingly rare; two in a row would
                # mean the generator is broken, which must not pass quietly.
                if attempt == 1:
                    raise
        audit.record(
            "invite.created",
            actor=actor_uid,
            target_type="invite",
            target_id=invite_id,
            details={"max_uses": max_uses, "expires_at": iso(expires)},
            now=now,
        )
        minted.append(MintedInvite(invite_id, format_code(code), clean_label, iso(expires), max_uses))
    return minted


def wire_status(doc: Mapping[str, Any], now: datetime) -> str:
    """revoked, else used (no uses left), else expired, else active."""
    if doc.get("status") == INVITE_REVOKED:
        return "revoked"
    if int(doc.get("uses", 0)) >= int(doc.get("max_uses", 1)):
        return "used"
    if str(doc.get("expires_at", "")) <= iso(now):
        return "expired"
    return "active"


def invite_id_of(doc: Mapping[str, Any]) -> str:
    return str(doc["id"]).removeprefix("invite|")


def invite_to_wire(doc: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """The ``Invite`` admins see. Never a code, never a redeemer."""
    return {
        "id": invite_id_of(doc),
        "label": doc["label"],
        "status": wire_status(doc, now),
        "createdAt": doc["created_at"],
        "createdBy": doc["created_by"],
        "expiresAt": doc["expires_at"],
        "maxUses": doc["max_uses"],
        "uses": doc.get("uses", 0),
        "lastRedeemedAt": doc.get("last_redeemed_at"),
    }


def list_invites(*, status: str, now: datetime, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """The newest ``limit`` invites, then those whose derived status matches."""
    if status not in LIST_STATUSES:
        raise invalid("status", "enum")
    docs = cosmos.query(
        SQL_INVITES_LIST,
        [{"name": "@limit", "value": limit}, {"name": "@type", "value": T_INVITE}],
        partition_key=PK_SYS,
    )
    if status == "all":
        return docs
    return [doc for doc in docs if wire_status(doc, now) == status]


def revoke(invite_id: str, *, actor_uid: str, now: datetime) -> dict[str, Any]:
    """Revoke an invite. Idempotent: revoking again answers the revoked invite
    unchanged, with no second audit entry. A revoked code then redeems as
    ``404 invite_not_found``, so revocation is not revealed to the holder."""
    doc_id = invite_doc_id(invite_id)
    doc = cosmos.read(doc_id, PK_SYS)
    if doc is None:
        raise not_found("invite")
    if doc.get("status") == INVITE_REVOKED:
        return doc
    operations: list[dict[str, Any]] = [
        {"op": "set", "path": "/status", "value": INVITE_REVOKED},
        {"op": "set", "path": "/revoked_at", "value": iso(now)},
        {"op": "set", "path": "/revoked_by", "value": actor_uid},
        {"op": "set", "path": "/ttl", "value": _ttl(parse_iso(doc["expires_at"]), now)},
    ]
    try:
        updated = cosmos.patch(doc_id, PK_SYS, operations, filter_predicate=f"FROM c WHERE c.status = '{INVITE_ACTIVE}'")
    except cosmos.NotFound:
        raise not_found("invite") from None
    except cosmos.PreconditionFailed:
        # A concurrent revoke won; it wrote the audit entry.
        current = cosmos.read(doc_id, PK_SYS)
        if current is None:
            raise not_found("invite") from None
        return current
    audit.record("invite.revoked", actor=actor_uid, target_type="invite", target_id=invite_id, now=now)
    return updated


def _redeemed_by(doc: Mapping[str, Any], uid: str) -> bool:
    return any(entry.get("uid") == uid for entry in doc.get("redemptions") or [])


def _refusal(doc: Mapping[str, Any], ts: str) -> tuple[str, str] | None:
    """Why this invite cannot be redeemed now, as (log reason, error code).
    Revoked reads as unknown, so a revoked code reveals nothing."""
    if doc.get("status") == INVITE_REVOKED:
        return "revoked", "invite_not_found"
    if str(doc.get("expires_at", "")) <= ts:
        return "expired", "invite_expired"
    if int(doc.get("uses", 0)) >= int(doc.get("max_uses", 1)):
        return "used", "invite_used"
    return None


def _refused(principal: "Principal", reason: str, code: str) -> ApiError:
    # The uid and the reason, never the code.
    log.info("Invite redemption refused uid=%s reason=%s", principal.uid, reason)
    return ApiError(code)


def redeem(
    principal: "Principal",
    raw_code: str,
    *,
    accept_language: str | None,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    """POST /me/redeem (docs/api.md section 2.3). Returns (me, created):
    created is False only for a caller who already was an active member, who
    consumes neither a code nor a quota unit."""
    code = normalise_code(raw_code)

    me = members.get_me(principal.oid)
    if me is not None:
        status = me.get("status")
        if status == MEMBER_ACTIVE:
            return me, False
        if status == MEMBER_BLOCKED:
            raise ApiError("account_blocked")
        if status == MEMBER_DELETING:
            raise ApiError("account_deleted", details={"state": "deleting"})
        log.error("Membership has unknown status %r", status)
        raise ApiError("internal")
    if members.tombstone_exists(principal.uid):
        raise ApiError("account_deleted", details={"state": "deleted"})
    if principal.is_admin:
        # The admin role needs no invite (section 2.4); the code is left alone.
        return members.bootstrap_admin(principal, accept_language=accept_language, now=now), True

    quotas.consume(principal.oid, "redeem", now=now)

    invite_id = invite_id_for(code)
    doc_id = invite_doc_id(invite_id)
    ts = iso(now)
    # Read first: the patch must reset the TTL from expires_at, and a code that
    # is plainly spent needs no conditional write to say so.
    invite = cosmos.read(doc_id, PK_SYS)
    if invite is None:
        raise _refused(principal, "unknown", "invite_not_found")
    if not _redeemed_by(invite, principal.uid):
        refusal = _refusal(invite, ts)
        if refusal is not None:
            raise _refused(principal, *refusal)
        # Every inlined value is server-generated: the status constant, our own
        # timestamp, and a uid that is 32 hex characters by construction.
        predicate = (
            f"FROM c WHERE c.status = '{INVITE_ACTIVE}' AND c.uses < c.max_uses "
            f"AND c.expires_at > '{ts}' "
            f'AND NOT ARRAY_CONTAINS(c.redemptions, {{"uid": "{principal.uid}"}}, true)'
        )
        operations: list[dict[str, Any]] = [
            {"op": "incr", "path": "/uses", "value": 1},
            {"op": "add", "path": "/redemptions/-", "value": {"uid": principal.uid, "at": ts}},
            {"op": "set", "path": "/last_redeemed_at", "value": ts},
            {"op": "set", "path": "/ttl", "value": _ttl(parse_iso(invite["expires_at"]), now)},
        ]
        try:
            cosmos.patch(doc_id, PK_SYS, operations, filter_predicate=predicate)
        except cosmos.NotFound:
            raise _refused(principal, "unknown", "invite_not_found") from None
        except cosmos.PreconditionFailed:
            # Someone changed the invite since the read: a concurrent redeemer
            # took the last use, an admin revoked it, or this caller's own
            # earlier attempt landed after all.
            current = cosmos.read(doc_id, PK_SYS)
            if current is None:
                raise _refused(principal, "unknown", "invite_not_found") from None
            if not _redeemed_by(current, principal.uid):
                reason, error = _refusal(current, ts) or ("used", "invite_used")
                raise _refused(principal, reason, error) from None
            invite = current

    me, created = members.create_member(
        principal,
        invite_id=invite_id,
        invite_label=invite.get("label"),
        accept_language=accept_language,
        now=now,
    )
    if created:
        audit.record("invite.redeemed", actor=principal.uid, target_type="invite", target_id=invite_id, now=now)
    return me, True


def anonymise_redemption(invite_id: str, uid: str) -> bool:
    """Purge step: this account's redemption entry keeps its slot (``uses``
    still counts it) but its uid becomes "deleted". Idempotent; False when
    there is nothing (left) to change."""
    doc_id = invite_doc_id(invite_id)
    doc = cosmos.read(doc_id, PK_SYS)
    if doc is None:
        return False
    indexes = [i for i, entry in enumerate(doc.get("redemptions") or []) if entry.get("uid") == uid]
    if not indexes:
        return False
    now = utcnow()
    # One redemption per uid is all the redeem predicate allows; the slice keeps
    # a corrupted document within one patch's 10 operations.
    operations: list[dict[str, Any]] = [
        {"op": "set", "path": f"/redemptions/{i}/uid", "value": DELETED_UID} for i in indexes[:9]
    ]
    operations.append({"op": "set", "path": "/ttl", "value": _ttl(parse_iso(doc["expires_at"]), now)})
    # Entries are only ever appended, so an index read here still names the
    # same entry; the predicate makes that a guarantee rather than a belief.
    predicate = f"FROM c WHERE c.redemptions[{indexes[0]}].uid = '{uid}'"
    try:
        cosmos.patch(doc_id, PK_SYS, operations, filter_predicate=predicate)
    except (cosmos.NotFound, cosmos.PreconditionFailed):
        return False
    return True
