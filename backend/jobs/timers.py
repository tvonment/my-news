"""Timer functions. Schedules are UTC: Flex Consumption does not support
WEBSITE_TIME_ZONE.

* ``harvest_timer`` — moved verbatim from function_app.py (same name,
  schedule and binding). Every 30-60 min: fetch, dedupe, store. No LLM, ~$0.
  Frequent because several feeds carry only 10 items and would otherwise roll
  over between runs. It keeps running until v2 replaces it: the v1 harvest is
  deleted in the same release that ships harvest v2 (Phase 2), and until then
  the ``pipeline-silent`` alert keeps watching it.
* ``maintenance_timer`` — daily at 02:15 UTC (docs/data-model.md section 8).
  Re-enqueues account deletions stuck for more than an hour, raises
  ``ALERT identity_removal_overdue`` while an identity removal has waited more
  than 7 days, and ``ALERT secret_expiry`` every day from 30 days before the
  Sign in with Apple client secret must be renewed. 02:15 is outside the
  cycle hours (05:05, 10:05, 16:05 from Phase 2), so a re-enqueued purge does
  not compete with a cycle for the container's throughput.
"""

from __future__ import annotations

import logging
import typing
from datetime import datetime

import azure.functions as func

from core import accounts, audit, identity_removal
from core.config import get_settings
from core.jobs import JOBS_CONNECTION, QUEUE_ACCOUNT_PURGE
from core.models import utcnow

log = logging.getLogger(__name__)

bp = func.Blueprint()

MAINTENANCE_SCHEDULE = "0 15 2 * * *"
# The renewal reminder starts this many days ahead; admin health fails from
# the same day (core.ops_health.secrets_check).
APPLE_SECRET_WARN_DAYS = accounts.APPLE_SECRET_WARN_DAYS


@bp.timer_trigger(
    schedule="%HARVEST_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def harvest_timer(timer: func.TimerRequest) -> None:
    from pipeline.harvest import run_harvest

    if timer.past_due:
        log.warning("Harvest timer is past due")
    record = run_harvest()
    log.info("Harvest run %s: %d new items", record.id, record.items_new)


def _check_apple_secret(now: datetime) -> None:
    """The Sign in with Apple client secret expires after at most 6 months and
    is renewed by hand in the Entra Apple IdP blade. Nothing in Azure warns
    before it lapses, and when it does Apple sign-in simply stops working, so
    this alerts daily from 30 days ahead (and after the date, until renewed).
    A value that is set but not a date alerts too: a typo must not silence the
    one reminder there is."""
    if not get_settings().apple_secret_renew_by:
        return  # Apple is not configured yet: nothing to renew.
    renew_by, days_left = accounts.apple_secret_renewal(now)
    if renew_by is None or days_left is None:
        audit.alert("secret_expiry", secret="apple", renewBy=None, daysLeft=None, problem="not_a_date")
    elif days_left <= APPLE_SECRET_WARN_DAYS:
        audit.alert("secret_expiry", secret="apple", renewBy=renew_by, daysLeft=days_left)


@bp.timer_trigger(
    schedule=MAINTENANCE_SCHEDULE,
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
@bp.queue_output(arg_name="purge", queue_name=QUEUE_ACCOUNT_PURGE, connection=JOBS_CONNECTION)
def maintenance_timer(timer: func.TimerRequest, purge: func.Out[typing.List[str]]) -> None:
    """The daily sweep. ``typing.List``, not ``list``: the worker checks an
    output binding's type with azure.functions' own typing_inspect, which does
    not recognise the builtin generic, and would refuse to index the app.

    The order is chosen for failure: the secret check needs no database, so it
    runs first and cannot be lost to an outage; a failed overdue count is
    logged and the sweep goes on; the stuck-deletion query runs last, so if it
    fails the run fails (visibly) with no messages lost, because the output
    binding is written only when the function returns normally.
    """
    if timer.past_due:
        log.warning("Maintenance timer is past due")
    now = utcnow()
    _check_apple_secret(now)

    try:
        overdue = identity_removal.count_overdue(now)
    except Exception:
        log.exception("Maintenance could not count overdue identity removals")
    else:
        if overdue:
            # The owner removes the Entra user by hand within 7 days of the
            # purge (docs/identity.md section 8.1); this repeats daily until
            # that is done and recorded.
            audit.alert("identity_removal_overdue", count=overdue)

    # Purging again is always safe (every step is idempotent), so a deletion
    # that is only slow, not stuck, costs a redundant pass and nothing else.
    messages = accounts.stuck_deletions(now)
    if messages:
        purge.set([message.to_json() for message in messages])
        log.warning("Maintenance re-enqueued %d stuck account deletion(s)", len(messages))
    else:
        log.info("Maintenance found no stuck account deletions")
