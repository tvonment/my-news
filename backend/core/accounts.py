"""Account lifecycle beyond membership: deletion and the purge, the data
export, and the admin views of accounts (docs/api.md sections 6.2 and 6.5,
docs/data-model.md section 8).

**Deletion** is two halves. ``request_deletion`` (DELETE /me) only flips the
account to ``deleting`` and hands the work to the ``account-purge`` queue, so the
member gets an answer at once and from then on every other route answers 410.
``purge_account`` (the queue worker) does the destructive part in an order where
every step is idempotent and a crash at any point leaves enough behind to
resume: the registry document is deleted last, so the maintenance timer can
always find an unfinished deletion, and the tombstone is written before ``me``
is deleted, so there is never a moment in which a deleted account reads as
"never a member" and could redeem a fresh invite.

Admin views address accounts by uid and never return an oid or an email.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Mapping

from core import audit, cosmos, identity_removal, invites, members, quotas
from core.auth import uid_for
from core.config import get_settings
from core.errors import ApiError, degraded, invalid, not_found
from core.jobs import PurgeMessage
from core.models import (
    DELETION_AWAITING,
    DELETION_PURGING,
    ME_ID,
    MEMBER_ACTIVE,
    MEMBER_BLOCKED,
    MEMBER_DELETING,
    PK_CATALOG,
    PK_SYS,
    PK_USERS,
    RUN_PK,
    SLUG_RE,
    SUBS_ID,
    SYSTEM_PROPERTIES,
    T_DELETION,
    T_QUOTA,
    T_TOMBSTONE,
    T_USER,
    UID_RE,
    deletion_doc_id,
    iso,
    parse_iso,
    source_doc_id,
    tombstone_doc_id,
    user_pk,
    utcnow,
)

if TYPE_CHECKING:
    from core.auth import AuthContext, Principal

log = logging.getLogger(__name__)

EXPORT_SCHEMA = "mynews-export/1"
PURGE_RU_PER_S = cosmos.RU_RATE_PURGE
STUCK_AFTER = timedelta(hours=1)
USERS_LIST_LIMIT = 500
# docs_deleted is written back to the deletion record this often, so a long
# purge shows progress and a resumed one reports a true total.
PURGE_CHECKPOINT_EVERY = 100
# A second walk of the partition catches anything written while the first ran.
PURGE_MAX_PASSES = 3
# Subscription states that hold a catalog subscriber_count unit (P3's
# PATCH /me/sources counts with the same rule).
SUBSCRIBER_COUNTED_STATES = ("subscribed", "paused")
APPLE_SECRET_WARN_DAYS = 30
BLOCK_REASON_MAX = 200

SQL_USERS_LIST = (
    "SELECT TOP @limit c.uid, c.status, c.is_admin, c.created_at, c.last_active_at, c.invite_label, "
    "c.locale, c.languages, c.blocked_at FROM c WHERE c.type = @type ORDER BY c.type ASC, c.created_at DESC"
)
SQL_USER_BY_UID = "SELECT * FROM c WHERE c.uid = @uid"
SQL_COUNT_TYPE = "SELECT VALUE COUNT(1) FROM c WHERE c.type = @type"
SQL_COUNT_TYPE_STATUS = "SELECT VALUE COUNT(1) FROM c WHERE c.type = @type AND c.status = @status"
SQL_COUNT_ACTIVE_SINCE = "SELECT VALUE COUNT(1) FROM c WHERE c.type = @type AND c.last_active_at >= @since"
SQL_COUNT_CREATED_SINCE = "SELECT VALUE COUNT(1) FROM c WHERE c.type = @type AND c.created_at >= @since"
SQL_STUCK_DELETING_USERS = (
    "SELECT c.id, c.uid, c.deletion_requested_at FROM c "
    "WHERE c.type = @type AND c.status = @status AND c.deletion_requested_at < @cutoff"
)
SQL_PURGING_DELETIONS = "SELECT c.id, c.uid, c.oid, c.purge_started_at FROM c WHERE c.type = @type AND c.status = @status"
SQL_ALL = "SELECT * FROM c"
# Runs against v1's `items` (pk "run"), where every path is still indexed.
LEGACY_SQL_LAST_HARVEST = (
    "SELECT TOP 1 c.id, c.started_at, c.finished_at, c.ok, c.items_new FROM c "
    "WHERE c.kind = 'harvest' ORDER BY c.started_at DESC"
)


def _params(**values: Any) -> list[dict[str, Any]]:
    return [{"name": f"@{name}", "value": value} for name, value in values.items()]


def _count(sql: str, pk: str, **values: Any) -> int:
    rows = cosmos.query(sql, _params(**values), partition_key=pk)
    return int(rows[0]) if rows else 0


def _replace_with_retry(read: Any, change: Any) -> dict[str, Any] | None:
    """Read a document, let ``change`` return its new version (or None for "no
    write needed"), and replace it if it is still the version read. One re-read
    on a 412; a second is 503 contention. Returns the saved document, or the
    current one when no write was needed, or None when it does not exist."""
    for attempt in range(2):
        doc = read()
        if doc is None:
            return None
        new = change(doc)
        if new is None:
            return doc
        try:
            return cosmos.replace_if_match(new, doc["_etag"])
        except cosmos.NotFound:
            return None
        except cosmos.PreconditionFailed:
            if attempt == 1:
                raise degraded("database", "contention", retry_after=1) from None
    return None


def _without_system(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in SYSTEM_PROPERTIES}


def _member_context(ctx: "AuthContext") -> tuple["Principal", dict[str, Any]]:
    """The caller of a member-level route and the gate's ``me``; a context
    without them is a wiring bug (a handler missing its access decorator)."""
    if ctx.principal is None or ctx.me is None:
        raise RuntimeError("a member route's context carries a principal and the gate's document")
    return ctx.principal, ctx.me


# ---------------------------------------------------------------------------
# DELETE /me
# ---------------------------------------------------------------------------
def _to_deleting(doc: Mapping[str, Any], ts: str) -> dict[str, Any] | None:
    if doc.get("status") == MEMBER_DELETING:
        return None
    new = _without_system(doc)
    # blocked_at and blocked_by describe a block; the account is past that now.
    new.pop("blocked_at", None)
    new.pop("blocked_by", None)
    new.update(status=MEMBER_DELETING, deletion_requested_at=ts, updated_at=ts)
    return new


def request_deletion(ctx: "AuthContext", *, now: datetime) -> tuple[dict[str, Any], str | None]:
    """Mark the account ``deleting`` and return (the 202 body, the purge
    message or None).

    ``me`` first, because that is what the gate reads: from that write on every
    other route answers 410. Then the registry, which is what the maintenance
    timer scans for deletions whose message never arrived. A repeat (the
    account is already deleting) sends no second message, except when it finds
    the registry still unmarked: then the first attempt died between the two
    writes, before its message could be sent, and this one sends it.
    """
    principal, me = _member_context(ctx)
    ts = iso(now)
    marked: dict[str, bool] = {}

    def marker(name: str, requested_at: str) -> Any:
        def mark(doc: Mapping[str, Any]) -> dict[str, Any] | None:
            new = _to_deleting(doc, requested_at)
            marked[name] = new is not None
            return new

        return mark

    if me.get("status") != MEMBER_DELETING:
        # The gate's document (with its _etag) first; a fresh read only after a 412.
        gate_doc = iter([dict(me)])
        saved = _replace_with_retry(lambda: next(gate_doc, None) or members.get_me(principal.oid), marker("me", ts))
        if saved is None:
            raise ApiError("account_deleted", details={"state": "deleted"})
        me = saved

    me_transitioned = marked.get("me", False)
    registry_changed = False
    registry = cosmos.read(principal.oid, PK_USERS)
    if registry is None:
        # Written first and deleted last, so this should not happen; recreate
        # it, because the maintenance timer can only find what is registered.
        log.warning("Registry document missing for uid=%s at deletion; recreating it", principal.uid)
        cosmos.upsert(
            {
                **members.new_registry_doc(
                    principal, me, invite_id=me.get("invite_id", ""), invite_label=None, now=now
                ),
                "status": MEMBER_DELETING,
                "deletion_requested_at": me.get("deletion_requested_at", ts),
            }
        )
        registry_changed = True
    elif registry.get("status") != MEMBER_DELETING:
        _replace_with_retry(
            lambda: cosmos.read(principal.oid, PK_USERS),
            marker("registry", me.get("deletion_requested_at", ts)),
        )
        registry_changed = marked.get("registry", False)

    message = None
    if me_transitioned or registry_changed:
        message = PurgeMessage(oid=principal.oid, requested_at=me.get("deletion_requested_at", ts)).to_json()
    if me_transitioned:
        audit.record(
            "account.deletion_requested",
            actor=principal.uid,
            target_type="user",
            target_id=principal.uid,
            now=now,
        )
    body = {"state": "deleting", "identityRemoval": identity_removal.identity_removal_label()}
    return body, message


# ---------------------------------------------------------------------------
# The purge (account-purge worker)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PurgeOutcome:
    uid: str
    status: Literal["purged", "finalised", "skipped"]
    docs_deleted: int
    reason: str | None


def _decrement_subscriptions(oid: str, uid: str) -> None:
    """Step 3, before anything is deleted: give back each catalog subscriber
    count this account holds. The source id is recorded on the deletion record
    first (a conditional append), then the count is decremented, so a crash can
    leave a count one too high but never one too low and never doubled. ``subs``
    arrives in Phase 3; in Phase 1 there is none and this costs one point read."""
    subs = cosmos.read(SUBS_ID, user_pk(oid))
    if not subs:
        return
    sources = subs.get("sources") or {}
    for source_id in sorted(sources):
        state = (sources[source_id] or {}).get("state")
        if state not in SUBSCRIBER_COUNTED_STATES:
            continue
        if not isinstance(source_id, str) or not SLUG_RE.match(source_id) or len(source_id) > 40:
            # Inlined into a predicate below, so only a catalog slug may pass.
            log.warning("Purge uid=%s skipped a malformed source id in subs", uid)
            continue
        try:
            cosmos.patch(
                deletion_doc_id(uid),
                PK_SYS,
                [{"op": "add", "path": "/decremented_source_ids/-", "value": source_id}],
                filter_predicate=f"FROM c WHERE NOT ARRAY_CONTAINS(c.decremented_source_ids, '{source_id}')",
            )
        except cosmos.PreconditionFailed:
            continue  # recorded by an earlier attempt, which decremented (or died trying)
        try:
            cosmos.patch(
                source_doc_id(source_id),
                PK_CATALOG,
                [{"op": "incr", "path": "/subscriber_count", "value": -1}],
                filter_predicate="FROM c WHERE c.subscriber_count > 0",
            )
        except (cosmos.NotFound, cosmos.PreconditionFailed):
            pass  # a retired source, or a count already at zero


def _checkpoint(uid: str, docs_deleted: int, now: datetime) -> None:
    try:
        cosmos.patch(
            deletion_doc_id(uid),
            PK_SYS,
            [
                {"op": "set", "path": "/docs_deleted", "value": docs_deleted},
                {"op": "set", "path": "/updated_at", "value": iso(now)},
            ],
        )
    except cosmos.CosmosError as exc:
        log.warning("Purge checkpoint failed uid=%s: %s", uid, type(exc).__name__)


def _purge_partition(oid: str, uid: str, *, already_deleted: int, now: datetime) -> int:
    """Step 4: delete every document in ``user|{oid}`` except ``me``, a page of
    ids at a time, paced by the caller. Not the preview delete-by-partition-key:
    it needs an account capability, is not recommended with continuous backup,
    and cannot be paced. Returns the running total of deleted documents."""
    pk = user_pk(oid)
    total = already_deleted
    since_checkpoint = 0
    try:
        for _ in range(PURGE_MAX_PASSES):
            deleted_this_pass = 0
            for doc_id in list(cosmos.iter_ids(pk)):
                if doc_id == ME_ID:
                    continue
                if cosmos.delete(doc_id, pk):
                    total += 1
                    deleted_this_pass += 1
                    since_checkpoint += 1
                if since_checkpoint >= PURGE_CHECKPOINT_EVERY:
                    _checkpoint(uid, total, now)
                    since_checkpoint = 0
            if deleted_this_pass == 0:
                break
    finally:
        # Also on the way out of a failure: a resumed run starts from the
        # recorded total, and finds the documents it already deleted gone.
        if since_checkpoint:
            _checkpoint(uid, total, now)
    return total


def _anonymise(uid: str, oid: str, me: Mapping[str, Any] | None, deletion: Mapping[str, Any]) -> None:
    """Step 5: the traces outside the partition that carry this uid."""
    if not (deletion.get("anonymised") or {}).get("invite"):
        invite_ids = set()
        if me and me.get("invite_id"):
            invite_ids.add(me["invite_id"])
        registry = cosmos.read(oid, PK_USERS)
        if registry and registry.get("invite_id"):
            invite_ids.add(registry["invite_id"])
        for invite_id in sorted(invite_ids - {members.BOOTSTRAP_INVITE_ID, ""}):
            invites.anonymise_redemption(invite_id, uid)
        cosmos.patch(deletion_doc_id(uid), PK_SYS, [{"op": "set", "path": "/anonymised/invite", "value": True}])
    # Phase 3: the member's reports (catalog report|{id}.uid -> "deleted"),
    # recorded in anonymised.reports. There are none before it.


def _tombstone(uid: str, now: datetime) -> None:
    """Step 6. Create-if-absent: a resumed purge keeps the first deleted_at."""
    cosmos.try_create(
        {
            "id": tombstone_doc_id(uid),
            "pk": PK_SYS,
            "type": T_TOMBSTONE,
            "v": 1,
            "uid": uid,
            "deleted_at": iso(now),
            "identity_removed": False,
            # Until the identity is removed: while the Entra user can still sign
            # in, the tombstone is what keeps that oid out of a fresh account.
            "ttl": -1,
        }
    )


def _finalise(uid: str, oid: str, *, docs_deleted: int, now: datetime, record_audit: bool = True) -> None:
    """Steps 8 and 9, which a resumed purge may still owe: the registry goes
    last, then the deletion record moves on to identity removal."""
    cosmos.delete(oid, PK_USERS)
    try:
        identity_removal.mark_purged(uid, docs_deleted=docs_deleted, now=now)
    except cosmos.NotFound:
        log.warning("Purge uid=%s finished without a deletion record", uid)
        return
    if not record_audit:
        return
    audit.record(
        "account.purged",
        actor=audit.SYSTEM_ACTOR,
        target_type="deletion",
        target_id=uid,
        details={"docs_deleted": docs_deleted},
        now=now,
    )


def _record_failure(uid: str, exc: BaseException, now: datetime) -> None:
    try:
        cosmos.patch(
            deletion_doc_id(uid),
            PK_SYS,
            [
                {"op": "set", "path": "/last_error", "value": type(exc).__name__},
                {"op": "set", "path": "/updated_at", "value": iso(now)},
            ],
        )
    except Exception:
        log.warning("Could not record the purge failure for uid=%s", uid)


def purge_account(message: PurgeMessage, *, now: datetime | None = None) -> PurgeOutcome:
    """The account-purge worker's job (docs/data-model.md section 8). Every
    step is idempotent, so a redelivered or re-enqueued message resumes."""
    at = now or utcnow()
    if message.kind != "account":
        return PurgeOutcome(uid="", status="skipped", docs_deleted=0, reason="unsupported_kind")
    oid = message.oid
    uid = uid_for(oid)
    pk = user_pk(oid)

    me = cosmos.read(ME_ID, pk)
    if me is None:
        if members.tombstone_exists(uid):
            # An earlier run got past step 7; only the finish may be missing.
            # A redelivery after a complete run changes nothing and records
            # nothing a second time.
            record = cosmos.read(deletion_doc_id(uid), PK_SYS) or {}
            done = int(record.get("docs_deleted", 0))
            _finalise(uid, oid, docs_deleted=done, now=at, record_audit=record.get("status") == DELETION_PURGING)
            return PurgeOutcome(uid, "finalised", done, None)
        log.info("Purge uid=%s skipped: no membership and no tombstone", uid)
        return PurgeOutcome(uid, "skipped", 0, "no_membership")
    if me.get("status") != MEMBER_DELETING:
        # A queue message alone never deletes an account; only DELETE /me
        # (status deleting) authorises it.
        log.warning("Purge uid=%s refused: status is %s, not deleting", uid, me.get("status"))
        return PurgeOutcome(uid, "skipped", 0, "not_deleting")

    requested_at = me.get("deletion_requested_at") or message.requested_at
    deletion = identity_removal.open_deletion(uid, oid, requested_at=requested_at, now=at)
    try:
        with cosmos.metered() as meter, cosmos.paced(PURGE_RU_PER_S):
            _decrement_subscriptions(oid, uid)
            total = _purge_partition(oid, uid, already_deleted=int(deletion.get("docs_deleted", 0)), now=at)
            _anonymise(uid, oid, me, deletion)
            _tombstone(uid, at)
            cosmos.delete(ME_ID, pk)
            _finalise(uid, oid, docs_deleted=total, now=at)
    except Exception as exc:
        _record_failure(uid, exc, at)
        raise
    log.info("Purge uid=%s complete: %d documents, %.0f RU", uid, total, meter.charge)
    return PurgeOutcome(uid, "purged", total, None)


def stuck_deletions(now: datetime) -> list[PurgeMessage]:
    """What the maintenance timer re-enqueues: registry documents deleting for
    more than an hour (Q10: the message was lost, or the worker never ran) and
    deletion records still purging an hour after their last start (Q11: the
    worker died). One message per oid; purging again is always safe."""
    cutoff = iso(now - STUCK_AFTER)
    messages: dict[str, PurgeMessage] = {}
    for row in cosmos.query(
        SQL_STUCK_DELETING_USERS,
        _params(type=T_USER, status=MEMBER_DELETING, cutoff=cutoff),
        partition_key=PK_USERS,
    ):
        messages[row["id"]] = PurgeMessage(oid=row["id"], requested_at=row.get("deletion_requested_at") or cutoff)
    for row in cosmos.query(
        SQL_PURGING_DELETIONS,
        _params(type=T_DELETION, status=DELETION_PURGING),
        partition_key=PK_SYS,
    ):
        started = row.get("purge_started_at")
        oid = row.get("oid")
        if oid and oid not in messages and isinstance(started, str) and started < cutoff:
            messages[oid] = PurgeMessage(oid=oid, requested_at=started)
    return list(messages.values())


# ---------------------------------------------------------------------------
# GET /me/export
# ---------------------------------------------------------------------------
_EXPORT_DROPPED = SYSTEM_PROPERTIES | {"pk"}


def _export_doc(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in _EXPORT_DROPPED}


def build_export(ctx: "AuthContext", *, now: datetime) -> tuple[dict[str, Any], str]:
    """Everything stored for the caller, as stored: the partition's documents
    (minus quota counters) and the registry document, without Cosmos's system
    properties and pk. Returns (body, attachment filename). Takes an
    ``export`` quota unit first, because reading a large partition is the
    expensive part."""
    principal, gate_me = _member_context(ctx)
    quotas.consume(principal.oid, "export", now=now)
    partition = cosmos.query(SQL_ALL, partition_key=user_pk(principal.oid))
    documents = sorted(
        (_export_doc(doc) for doc in partition if doc.get("type") != T_QUOTA),
        key=lambda doc: (doc.get("id") != ME_ID, str(doc.get("type")), str(doc.get("id"))),
    )
    registry = cosmos.read(principal.oid, PK_USERS)
    if registry is not None:
        documents.append(_export_doc(registry))
    current_me = next((doc for doc in partition if doc.get("id") == ME_ID), gate_me)
    body = {
        "schema": EXPORT_SCHEMA,
        "exportedAt": iso(now),
        "account": members.me_to_wire(current_me, principal),
        "documents": documents,
    }
    return body, f"mynews-export-{now:%Y-%m-%d}.json"


# ---------------------------------------------------------------------------
# Admin: users
# ---------------------------------------------------------------------------
def find_registry_by_uid(uid: str) -> dict[str, Any] | None:
    if not isinstance(uid, str) or not UID_RE.match(uid):
        return None
    rows = cosmos.query(SQL_USER_BY_UID, _params(uid=uid), partition_key=PK_USERS)
    return rows[0] if rows else None


def list_users(*, limit: int = USERS_LIST_LIMIT) -> tuple[list[dict[str, Any]], bool]:
    """The newest ``limit`` members, and whether the cap cut the list (one
    extra row is asked for to tell)."""
    rows = cosmos.query(SQL_USERS_LIST, _params(limit=limit + 1, type=T_USER), partition_key=PK_USERS)
    return rows[:limit], len(rows) > limit


def user_to_wire(doc: Mapping[str, Any]) -> dict[str, Any]:
    """``AdminUser``: no oid, no email."""
    return {
        "uid": doc["uid"],
        "status": doc["status"],
        "admin": bool(doc.get("is_admin", False)),
        "createdAt": doc["created_at"],
        "lastActiveAt": doc.get("last_active_at"),
        "inviteLabel": doc.get("invite_label"),
        "locale": doc.get("locale") or "en",
        "languages": list(doc.get("languages") or ["en"]),
        "blockedAt": doc.get("blocked_at"),
    }


def _blocked_change(target: str, ts: str, actor_uid: str, *, registry: bool) -> Any:
    def change(doc: Mapping[str, Any]) -> dict[str, Any] | None:
        status = doc.get("status")
        if status == MEMBER_DELETING:
            raise ApiError("state_conflict", details={"status": MEMBER_DELETING})
        if status == target:
            return None
        new = _without_system(doc)
        new.update(status=target, updated_at=ts)
        if target == MEMBER_BLOCKED:
            new["blocked_at"] = ts
            if registry:
                new["blocked_by"] = actor_uid
        else:
            new.pop("blocked_at", None)
            new.pop("blocked_by", None)
        return new

    return change


def set_blocked(
    target_uid: str,
    *,
    blocked: bool,
    reason: str | None,
    actor: "Principal",
    now: datetime,
) -> dict[str, Any]:
    """Block or unblock an account; ``AdminUser`` of the result.

    ``me`` first, because the gate reads it (blocking takes effect on the
    member's next request), then the registry, then the audit entry. Into the
    current state is a 200 with no write. Admins cannot block themselves: that
    would lock the only admin out with no route back in.
    """
    if target_uid == actor.uid:
        raise invalid("uid", "self")
    if reason is not None:
        # The reason lands in the audit trail for a year: bounded here too, not
        # only by the route's body validation (AdminBlockRequest: 1-200).
        if not isinstance(reason, str):
            raise invalid("reason", "type")
        if not 1 <= len(reason) <= BLOCK_REASON_MAX:
            raise invalid("reason", "length")
    registry = find_registry_by_uid(target_uid)
    if registry is None:
        raise not_found("user")
    if registry.get("status") == MEMBER_DELETING:
        raise ApiError("state_conflict", details={"status": MEMBER_DELETING})
    target = MEMBER_BLOCKED if blocked else MEMBER_ACTIVE
    oid = registry["id"]
    ts = iso(now)

    saved_me = _replace_with_retry(
        lambda: members.get_me(oid), _blocked_change(target, ts, actor.uid, registry=False)
    )
    if saved_me is None:
        # The registry exists but the membership is gone: a purge is between
        # its last two steps.
        raise ApiError("state_conflict", details={"status": MEMBER_DELETING})
    saved_registry = _replace_with_retry(
        lambda: cosmos.read(oid, PK_USERS), _blocked_change(target, ts, actor.uid, registry=True)
    )
    if saved_registry is None:
        raise not_found("user")
    if registry.get("status") != target:
        audit.record(
            "user.blocked" if blocked else "user.unblocked",
            actor=actor.uid,
            target_type="user",
            target_id=target_uid,
            details={"reason": reason} if reason else {},
            now=now,
        )
    return user_to_wire(saved_registry)


# ---------------------------------------------------------------------------
# Admin: overview
# ---------------------------------------------------------------------------
def fetch_last_v1_harvest() -> dict[str, Any] | None:
    """The newest v1 harvest run from the legacy container, as ``{at, ok}``, or
    None when there is none (or its start time is unreadable). Raises what
    core.cosmos raises, so admin health can classify a failure."""
    rows = cosmos.query(
        LEGACY_SQL_LAST_HARVEST,
        partition_key=RUN_PK,
        container=get_settings().legacy_cosmos_container,
    )
    if not rows:
        return None
    row = rows[0]
    try:
        at = iso(parse_iso(str(row.get("started_at"))))
    except ValueError:
        return None
    return {"at": at, "ok": bool(row.get("ok", False))}


def last_v1_harvest() -> dict[str, Any] | None:
    """fetch_last_v1_harvest(), or None on any error: the overview just shows
    nothing, and admin health reports the failure."""
    try:
        return fetch_last_v1_harvest()
    except Exception as exc:
        log.warning("Reading the last v1 harvest failed: %s", type(exc).__name__)
        return None


def apple_secret_renewal(now: datetime) -> tuple[str | None, int | None]:
    """(APPLE_SECRET_RENEW_BY, days left) or (None, None) when unset or not a
    date. Negative days once past due."""
    raw = get_settings().apple_secret_renew_by
    if not raw:
        return None, None
    try:
        renew_by = date.fromisoformat(raw)
    except ValueError:
        log.warning("APPLE_SECRET_RENEW_BY=%r is not a YYYY-MM-DD date", raw)
        return None, None
    return renew_by.isoformat(), (renew_by - now.date()).days


def overview(now: datetime) -> dict[str, Any]:
    """``AdminOverview``: one screen of counts. Sections owned by later phases
    are null until then."""
    invite_counts = {"active": 0, "used": 0, "expired": 0, "revoked": 0}
    for doc in invites.list_invites(status="all", now=now, limit=invites.LIST_LIMIT):
        invite_counts[invites.wire_status(doc, now)] += 1
    renew_by, days_left = apple_secret_renewal(now)
    return {
        "generatedAt": iso(now),
        "users": {
            "total": _count(SQL_COUNT_TYPE, PK_USERS, type=T_USER),
            "active7d": _count(SQL_COUNT_ACTIVE_SINCE, PK_USERS, type=T_USER, since=iso(now - timedelta(days=7))),
            "new30d": _count(SQL_COUNT_CREATED_SINCE, PK_USERS, type=T_USER, since=iso(now - timedelta(days=30))),
            "blocked": _count(SQL_COUNT_TYPE_STATUS, PK_USERS, type=T_USER, status=MEMBER_BLOCKED),
            "deleting": _count(SQL_COUNT_TYPE_STATUS, PK_USERS, type=T_USER, status=MEMBER_DELETING),
        },
        "invites": invite_counts,
        "deletions": {
            "purging": _count(SQL_COUNT_TYPE_STATUS, PK_SYS, type=T_DELETION, status=DELETION_PURGING),
            "awaitingIdentityRemoval": _count(SQL_COUNT_TYPE_STATUS, PK_SYS, type=T_DELETION, status=DELETION_AWAITING),
            "overdue": identity_removal.count_overdue(now),
        },
        "pipeline": {"lastHarvest": last_v1_harvest(), "lastCycle": None},
        "secrets": {"appleSecretRenewBy": renew_by, "appleSecretDaysLeft": days_left},
        "spend": None,
        "suggestions": None,
        "reports": None,
    }
