"""Per-account quotas: one table, one conditional increment.

Counters live in the caller's own partition, one document per UTC day
(``quota|2026-10-21``) or ISO week (``quota|w2026-W43``), one field per quota
key (docs/data-model.md section 4.3). Taking a unit is a single patch,

    incr /{key} by n   WHERE NOT IS_DEFINED(c.{key}) OR c.{key} <= limit - n

so two concurrent requests cannot both take the last unit: Cosmos applies the
predicate and the increment atomically, and the loser gets a 412, which becomes
``429 quota_exceeded``. No read precedes the write, so the common case costs one
patch (~10 RU) and nothing races between a check and a write.

Quota keys come from QUOTAS, never from a request, which is what makes inlining
them into the predicate safe. A key outside the table is a KeyError: a
programming error, not a 400.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from core import cosmos
from core.errors import ApiError, degraded
from core.models import (
    T_QUOTA,
    TTL_QUOTA_DAY,
    TTL_QUOTA_WEEK,
    iso,
    quota_day_doc_id,
    quota_week_doc_id,
    user_pk,
    utcnow,
)


@dataclass(frozen=True)
class Quota:
    key: str
    limit: int
    period: Literal["day", "week"]


# docs/api.md section 1.10. Days reset at 00:00 UTC, weeks at Monday 00:00 UTC.
QUOTAS: dict[str, Quota] = {
    # Each *new* feedback event (P3); duplicates are free.
    "feedback": Quota("feedback", 2000, "day"),
    # Each describe call (P3): the one route that sends a member's text to a model.
    "describe": Quota("describe", 5, "day"),
    "reports": Quota("reports", 20, "day"),
    "suggestions": Quota("suggestions", 3, "week"),
    "export": Quota("export", 2, "day"),
    # Each well-formed code that reaches the invite lookup. With 60-bit codes,
    # 10 a day per account bounds guessing to nothing.
    "redeem": Quota("redeem", 10, "day"),
}


def quota_doc_id(quota: Quota, now: datetime) -> str:
    return quota_day_doc_id(now) if quota.period == "day" else quota_week_doc_id(now)


def ttl_for(quota: Quota) -> int:
    """Long enough to outlive its period from any write inside it (TTL counts
    from the last write), short enough that old counters clean themselves up."""
    return TTL_QUOTA_DAY if quota.period == "day" else TTL_QUOTA_WEEK


def reset_at(quota: Quota, now: datetime) -> datetime:
    """When the counter's period ends: the next 00:00 UTC, or the next Monday
    00:00 UTC (ISO weeks, matching quota_week_doc_id)."""
    start_of_day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if quota.period == "day":
        return start_of_day + timedelta(days=1)
    return start_of_day + timedelta(days=7 - start_of_day.weekday())


def _exceeded(quota: Quota, now: datetime) -> ApiError:
    resets = reset_at(quota, now)
    wait = max(1, math.ceil((resets - now).total_seconds()))
    return ApiError(
        "quota_exceeded",
        details={"quota": quota.key, "limit": quota.limit, "period": quota.period, "resetsAt": iso(resets)},
        retry_after=wait,
    )


def _predicate(quota: Quota, n: int) -> str:
    return f"FROM c WHERE NOT IS_DEFINED(c.{quota.key}) OR c.{quota.key} <= {quota.limit - n}"


def consume(oid: str, key: str, n: int = 1, *, now: datetime | None = None) -> int:
    """Take ``n`` units of quota ``key`` for this account; returns the new count.

    ``429 quota_exceeded`` (with Retry-After until the reset) when fewer than
    ``n`` remain. Taking units is not undone if the request later fails: the
    quota bounds attempts, not successes.
    """
    quota = QUOTAS[key]
    if n < 1:
        raise ValueError("n must be at least 1")
    at = now or utcnow()
    if n > quota.limit:
        raise _exceeded(quota, at)
    doc_id = quota_doc_id(quota, at)
    pk = user_pk(oid)
    operations = [{"op": "incr", "path": f"/{quota.key}", "value": n}]
    for _ in range(2):
        try:
            doc = cosmos.patch(doc_id, pk, operations, filter_predicate=_predicate(quota, n))
            return int(doc[quota.key])
        except cosmos.PreconditionFailed:
            raise _exceeded(quota, at) from None
        except cosmos.NotFound:
            # First unit of the period. A 409 means a concurrent request created
            # the document first; go round once more and increment it instead.
            try:
                cosmos.create(
                    {"id": doc_id, "pk": pk, "type": T_QUOTA, "v": 1, quota.key: n, "ttl": ttl_for(quota)}
                )
                return n
            except cosmos.AlreadyExists:
                continue
    # Created and gone again between two attempts: only a TTL sweep at the very
    # edge of a period can do that. Ask the client to retry.
    raise degraded("database", "contention", retry_after=1)


def batch_patch_operation(key: str, n: int, now: datetime) -> tuple:
    """The same increment and predicate as one transactional-batch operation,
    for writers that take quota and write their documents atomically (P3
    feedback). The counter document must already exist: a batch cannot create
    it on a 404, so the caller creates it first when the batch reports one."""
    quota = QUOTAS[key]
    if n < 1:
        raise ValueError("n must be at least 1")
    return (
        "patch",
        (quota_doc_id(quota, now), [{"op": "incr", "path": f"/{quota.key}", "value": n}]),
        {"filter_predicate": _predicate(quota, n)},
    )
