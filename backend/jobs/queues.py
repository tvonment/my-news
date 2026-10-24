"""Queue workers (docs/data-model.md section 9).

* ``account_purge_worker`` (``account-purge``): the destructive half of
  DELETE /me, in core.accounts.purge_account's idempotent order, so a
  redelivered or re-enqueued message resumes where the last attempt stopped.
* ``ops_jobs_worker`` (``ops-jobs``): Phase 1's only job is a forced v1
  harvest, optionally of one source. Nothing produces these before Phase 2's
  POST /admin/runs/harvest; the worker is registered now so that route ships
  against a worker that already runs in production.

There is no suggestions worker before Phase 3: a registered worker that cannot
do its job would only move every message into the poison queue.

**Failures.** A message this release cannot read is logged and acknowledged:
retrying cannot make it readable, and the poison queue would only page the
owner for a producer bug the log already shows. A message whose *work* fails
is re-raised, so the host retries it (``maxDequeueCount`` 3 in host.json) and
then moves it to ``-poison``. The attempt that sees dequeue count 3 is the last
one, so it logs ``ALERT purge_failed`` first. Logs and alerts carry the uid,
never the oid.
"""

from __future__ import annotations

import logging

import azure.functions as func

from core import accounts, audit
from core.auth import uid_for
from core.errors import ApiError
from core.jobs import JOBS_CONNECTION, QUEUE_ACCOUNT_PURGE, QUEUE_OPS_JOBS, OpsJobMessage, PurgeMessage

log = logging.getLogger(__name__)

bp = func.Blueprint()

# host.json extensions.queues.maxDequeueCount; tests/test_jobs.py holds the
# two together. The attempt with this dequeue count is the last before poison.
MAX_DEQUEUE_COUNT = 3


def _unreadable(queue: str, msg: func.QueueMessage, exc: ValueError) -> None:
    # core.jobs words its ValueErrors without the values that failed (an oid is
    # never echoed), so the reason is safe to log; it is capped all the same.
    log.error(
        "%s: acknowledged an unreadable message id=%s dequeue_count=%s: %s: %s",
        queue,
        msg.id,
        msg.dequeue_count,
        type(exc).__name__,
        str(exc)[:200],
    )


def _uid(oid: str) -> str | None:
    """The uid for an alert, or None when it cannot be computed (no HMAC key,
    which is also why the purge itself failed)."""
    try:
        return uid_for(oid)
    except ApiError:
        return None


@bp.queue_trigger(arg_name="msg", queue_name=QUEUE_ACCOUNT_PURGE, connection=JOBS_CONNECTION)
def account_purge_worker(msg: func.QueueMessage) -> None:
    try:
        message = PurgeMessage.from_json(msg.get_body())
    except ValueError as exc:  # json.JSONDecodeError and UnicodeDecodeError are ValueErrors
        _unreadable(QUEUE_ACCOUNT_PURGE, msg, exc)
        return
    if message.kind != "account":
        # Phase 3's "history" purge (clear opens and dwell signals only). Its
        # producer, DELETE /me/history, does not exist yet; acknowledged rather
        # than guessed at.
        log.warning(
            "%s: kind %s is not handled before Phase 3; acknowledged id=%s", QUEUE_ACCOUNT_PURGE, message.kind, msg.id
        )
        return
    try:
        outcome = accounts.purge_account(message)
    except Exception:
        attempts = msg.dequeue_count or 0
        if attempts >= MAX_DEQUEUE_COUNT:
            audit.alert("purge_failed", uid=_uid(message.oid), attempts=attempts)
        raise
    log.info(
        "%s: uid=%s %s, %d documents deleted%s",
        QUEUE_ACCOUNT_PURGE,
        outcome.uid,
        outcome.status,
        outcome.docs_deleted,
        f" ({outcome.reason})" if outcome.reason else "",
    )


@bp.queue_trigger(arg_name="msg", queue_name=QUEUE_OPS_JOBS, connection=JOBS_CONNECTION)
def ops_jobs_worker(msg: func.QueueMessage) -> None:
    try:
        job = OpsJobMessage.from_json(msg.get_body())
    except ValueError as exc:
        _unreadable(QUEUE_OPS_JOBS, msg, exc)
        return
    # Imported here, not at module level: the harvest pulls in httpx and
    # trafilatura, which no HTTP route should pay for at cold start.
    from pipeline.harvest import run_harvest

    # Phase 2 adds the "cycle" and "republish" kinds here, together with the
    # core.jobs release that accepts them; until then from_json refuses them.
    log.info(
        "%s: job %s (%s) requested by %s at %s",
        QUEUE_OPS_JOBS,
        job.job_id,
        job.kind,
        job.requested_by,
        job.requested_at,
    )
    record = run_harvest(only_source=job.source_id, force=job.force)
    log.info(
        "%s: job %s finished harvest run %s: ok=%s, %d new items",
        QUEUE_OPS_JOBS,
        job.job_id,
        record.id,
        record.ok,
        record.items_new,
    )
