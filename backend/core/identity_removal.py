"""After a purge: removing the Entra user, and the record that tracks it.

``sys/deletion|{uid}`` is both the purge's resume record and the admin's
"pending identity removals" list (docs/data-model.md section 4.5). Its life:

    purging -> awaiting_identity_removal -> completed
    (worker)   (worker, after the purge)    (POST /admin/deletions/{id}/complete)

In ``manual`` mode the owner deletes the user in the Entra admin center (Users ›
Delete, then Deleted users › Delete permanently) within 7 days and then calls
the complete route, which erases the stored oid, starts the tombstone's 30-day
expiry and gives this record 90 days. ``graph`` mode (the worker deletes the
user through Microsoft Graph) is Later: until it ships, a configured ``graph``
behaves as ``manual``, and mode() says so, so that DeleteAccepted, the deletion
records and admin health all describe what actually happens.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Literal, Mapping

from core import audit, cosmos
from core.config import get_settings
from core.errors import ApiError, degraded, invalid, not_found
from core.models import (
    DELETION_AWAITING,
    DELETION_COMPLETED,
    DELETION_PURGING,
    PK_SYS,
    SYSTEM_PROPERTIES,
    T_DELETION,
    TTL_DELETION_AFTER_COMPLETE,
    TTL_TOMBSTONE_AFTER_REMOVAL,
    deletion_doc_id,
    iso,
    tombstone_doc_id,
)

log = logging.getLogger(__name__)

MODES = ("manual", "graph")
OVERDUE_AFTER = timedelta(days=7)
LIST_LIMIT = 200
LIST_STATUSES = (DELETION_PURGING, DELETION_AWAITING, DELETION_COMPLETED, "all")
# Flipped when graph-mode removal ships (it needs the Graph spike to pass).
GRAPH_MODE_AVAILABLE = False

# Q8 in docs/data-model.md: the filter property sits in the ORDER BY so the
# (type, status, created_at desc) composite serves the query.
SQL_DELETIONS_BY_STATUS = (
    "SELECT TOP @limit * FROM c WHERE c.type = @type AND c.status = @status "
    "ORDER BY c.type ASC, c.status ASC, c.created_at DESC"
)
SQL_DELETIONS_ALL = "SELECT TOP @limit * FROM c WHERE c.type = @type ORDER BY c.type ASC, c.created_at DESC"


_WARNED_MODES: set[str] = set()


def mode() -> Literal["manual", "graph"]:
    """The identity-removal mode in effect. A configured value this release
    cannot honour is said once per process, not on every call."""
    configured = get_settings().identity_delete_mode
    if configured == "graph" and GRAPH_MODE_AVAILABLE:
        return "graph"
    if configured != "manual" and configured not in _WARNED_MODES:
        _WARNED_MODES.add(configured)
        if configured == "graph":
            log.warning("IDENTITY_DELETE_MODE=graph is not available in this release; identity removal stays manual")
        else:
            log.warning("IDENTITY_DELETE_MODE=%r is not a mode; identity removal stays manual", configured)
    return "manual"


def identity_removal_label() -> Literal["manual", "automatic"]:
    """DeleteAccepted.identityRemoval: what the app tells the member."""
    return "automatic" if mode() == "graph" else "manual"


def open_deletion(uid: str, oid: str, *, requested_at: str, now: datetime) -> dict[str, Any]:
    """Create the deletion record on the purge's first run; on a resumed run,
    count the attempt and restamp ``purge_started_at``, which the maintenance
    timer measures "stuck for more than an hour" from."""
    ts = iso(now)
    doc = {
        "id": deletion_doc_id(uid),
        "pk": PK_SYS,
        "type": T_DELETION,
        "v": 1,
        "uid": uid,
        # Kept only until the identity is removed: the owner needs it to find
        # the user in Entra.
        "oid": oid,
        "status": DELETION_PURGING,
        "identity_mode": mode(),
        "created_at": requested_at,
        "purge_started_at": ts,
        "purged_at": None,
        "identity_removed_at": None,
        "identity_removed_by": None,
        "decremented_source_ids": [],
        "anonymised": {"invite": False, "reports": False},
        "docs_deleted": 0,
        "attempts": 1,
        "last_error": None,
        "updated_at": ts,
        "ttl": -1,
    }
    try:
        return cosmos.create(doc)
    except cosmos.AlreadyExists:
        operations: list[dict[str, Any]] = [
            {"op": "incr", "path": "/attempts", "value": 1},
            {"op": "set", "path": "/updated_at", "value": ts},
        ]
        try:
            return cosmos.patch(
                deletion_doc_id(uid),
                PK_SYS,
                operations + [{"op": "set", "path": "/purge_started_at", "value": ts}],
                filter_predicate=f"FROM c WHERE c.status = '{DELETION_PURGING}'",
            )
        except cosmos.PreconditionFailed:
            # Already past the purge (a duplicate message after it finished):
            # count the attempt, leave the timestamps alone.
            return cosmos.patch(deletion_doc_id(uid), PK_SYS, operations)


def mark_purged(uid: str, *, docs_deleted: int, now: datetime) -> dict[str, Any]:
    """The purge finished: hand the record to identity removal. Idempotent."""
    ts = iso(now)
    operations: list[dict[str, Any]] = [
        {"op": "set", "path": "/status", "value": DELETION_AWAITING},
        {"op": "set", "path": "/purged_at", "value": ts},
        {"op": "set", "path": "/docs_deleted", "value": docs_deleted},
        {"op": "set", "path": "/last_error", "value": None},
        {"op": "set", "path": "/updated_at", "value": ts},
    ]
    try:
        return cosmos.patch(
            deletion_doc_id(uid),
            PK_SYS,
            operations,
            filter_predicate=f"FROM c WHERE c.status = '{DELETION_PURGING}'",
        )
    except cosmos.PreconditionFailed:
        current = cosmos.read(deletion_doc_id(uid), PK_SYS)
        if current is None:
            raise
        return current


def list_deletions(*, status: str, now: datetime, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """Newest request first, at most ``limit``."""
    if status not in LIST_STATUSES:
        raise invalid("status", "enum")
    params: list[dict[str, Any]] = [{"name": "@limit", "value": limit}, {"name": "@type", "value": T_DELETION}]
    if status == "all":
        return cosmos.query(SQL_DELETIONS_ALL, params, partition_key=PK_SYS)
    params.append({"name": "@status", "value": status})
    return cosmos.query(SQL_DELETIONS_BY_STATUS, params, partition_key=PK_SYS)


def is_overdue(doc: Mapping[str, Any], now: datetime) -> bool:
    purged_at = doc.get("purged_at")
    return (
        doc.get("status") == DELETION_AWAITING
        and isinstance(purged_at, str)
        and purged_at < iso(now - OVERDUE_AFTER)
    )


def count_overdue(now: datetime) -> int:
    """Identity removals waiting more than 7 days, among the newest 200 that
    wait at all: what the overview, admin health and the maintenance timer's
    ``identity_removal_overdue`` alert count."""
    return sum(1 for doc in list_deletions(status=DELETION_AWAITING, now=now) if is_overdue(doc, now))


def deletion_to_wire(doc: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """``AdminDeletion``. The id is the account's uid; the oid is shown only
    until the identity is removed."""
    return {
        "id": doc["uid"],
        "status": doc["status"],
        "identityMode": doc.get("identity_mode", "manual"),
        "oid": doc.get("oid"),
        "requestedAt": doc["created_at"],
        "purgedAt": doc.get("purged_at"),
        "identityRemovedAt": doc.get("identity_removed_at"),
        "docsDeleted": int(doc.get("docs_deleted", 0)),
        "overdue": is_overdue(doc, now),
    }


def complete(uid: str, *, actor_uid: str, now: datetime) -> dict[str, Any]:
    """POST /admin/deletions/{id}/complete: the owner removed the Entra user.

    The tombstone is patched first, so a failure in between leaves the record
    awaiting and the call can simply be repeated; a repeat on a completed record
    answers it unchanged.
    """
    doc_id = deletion_doc_id(uid)
    for attempt in range(2):
        doc = cosmos.read(doc_id, PK_SYS)
        if doc is None:
            raise not_found("deletion")
        if doc.get("status") == DELETION_COMPLETED:
            return deletion_to_wire(doc, now)
        if doc.get("status") != DELETION_AWAITING:
            raise ApiError("state_conflict", details={"status": str(doc.get("status"))})
        try:
            cosmos.patch(
                tombstone_doc_id(uid),
                PK_SYS,
                [
                    {"op": "set", "path": "/identity_removed", "value": True},
                    {"op": "set", "path": "/ttl", "value": TTL_TOMBSTONE_AFTER_REMOVAL},
                ],
            )
        except cosmos.NotFound:
            # The purge writes the tombstone before it can reach "awaiting", so
            # this is a document someone deleted by hand. Nothing to expire.
            log.warning("Tombstone missing for deletion uid=%s", uid)
        ts = iso(now)
        updated = {k: v for k, v in doc.items() if k not in SYSTEM_PROPERTIES and k != "oid"}
        updated.update(
            status=DELETION_COMPLETED,
            identity_removed_at=ts,
            identity_removed_by=actor_uid,
            updated_at=ts,
            ttl=TTL_DELETION_AFTER_COMPLETE,
        )
        try:
            saved = cosmos.replace_if_match(updated, doc["_etag"])
        except cosmos.PreconditionFailed:
            if attempt == 1:
                raise degraded("database", "contention", retry_after=1) from None
            continue
        audit.record("identity.removed", actor=actor_uid, target_type="deletion", target_id=uid, now=now)
        return deletion_to_wire(saved, now)
    raise degraded("database", "contention", retry_after=1)
