"""MyNews Azure Functions app (Python v2 programming model).

v1 was retired on 2026-10-21. The digest and profile timers are gone, and so is
every HTTP route except ``health``: feed, feedback, profile, categories, runs,
refresh and digest. Nothing left in this app calls a model. Two functions
remain:

* ``harvest_timer`` — every 30-60 min. Fetch, dedupe, store. No LLM, ~$0.
  Frequent because several feeds carry only 10 items and would otherwise roll
  over between runs. It keeps running until v2 replaces it: per the v2 plan,
  the v1 harvest is deleted in the same release that ships harvest v2
  (Phase 2), and until then the ``pipeline-silent`` alert keeps watching it.
* ``health`` — anonymous, static, no I/O. Its docstring says why it stopped
  probing Cosmos.

The v1 stages under ``pipeline/`` and ``core/`` (gate, score, summarize,
cluster, digest, profile) are no longer reachable from here but stay in place;
Phase 2 deletes them together with the v1 harvest. Phase 1 splits this file into
``api/*`` blueprints and ``jobs/*``.

Schedules are UTC: Flex Consumption does not support WEBSITE_TIME_ZONE.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import azure.functions as func

from core.models import iso, utcnow

app = func.FunctionApp()
log = logging.getLogger(__name__)

# Third-party loggers that speak at INFO on every request. The Functions worker
# forwards whatever the root logger lets through, and one busy harvest's worth
# of request lines blows straight through host.json's 5-items/second sampling
# cap and takes the lines that matter with it — the "Harvest complete" summary
# was being dropped from exactly the runs that did the most work. Errors still
# surface: they travel as exceptions, which sampling excludes.
#
# The previous version of this block silenced azure-core's HTTP logger and
# azure.identity, and missed the one that cost money. The Cosmos client does not
# log through azure-core's logger: its pipeline carries CosmosHttpLoggingPolicy,
# which runs azure-core's request/response dump (URL, method, every header,
# status, every response header) under its own name,
# `azure.cosmos._cosmos_http_logging_policy` — a sibling of the logger that was
# silenced, not a child of it. Every Cosmos call kept dumping at INFO: 941k
# traces and ~1.8 GB of ingestion in 30 days, almost all of September's
# CHF 5.09 Log Analytics line.
#
# `azure` quiets the whole SDK, including loggers nobody has met yet. The two
# HTTP policies are also pinned by name so that lowering `azure` to debug
# something else cannot reopen the expensive pair. httpx logs one INFO line per
# fetch; httpcore speaks only at DEBUG. trafilatura warns on routine extraction
# misses that harvest already treats as non-errors (the article keeps the
# feed's own lead), hence ERROR. test_logging_config.py holds these levels.
_QUIET_LOGGERS = {
    "azure": logging.WARNING,
    "azure.cosmos._cosmos_http_logging_policy": logging.WARNING,
    "azure.core.pipeline.policies.http_logging_policy": logging.WARNING,
    "azure.identity": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "trafilatura": logging.ERROR,
}
for _name, _level in _QUIET_LOGGERS.items():
    logging.getLogger(_name).setLevel(_level)


def _json(payload: dict[str, Any], status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False, default=str),
        status_code=status,
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
# Version of the HTTP contract this backend serves; docs/api.md (Phase 1)
# defines it.
API_VERSION = 1


def _min_supported_build() -> int:
    """The oldest iOS build the backend still serves; 0 means every build.

    Unset until Phase 1's settings manifest adds MIN_SUPPORTED_BUILD. A value
    that is not an integer degrades to 0 with a warning instead of failing the
    request: a typo in an app setting must not turn the one public route into a
    500, and an unparsable floor cannot be enforced anyway.
    """
    raw = os.environ.get("MIN_SUPPORTED_BUILD", "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        log.warning("MIN_SUPPORTED_BUILD=%r is not an integer; serving minBuild=0", raw)
        return 0


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Static liveness: the host is up, and which contract it speaks. No I/O.

    This used to read and query Cosmos, because during the 2026-08-02 outage a
    point-read-only check answered "healthy" on 3 of 8 probes with the whole
    app down. That made the check honest but put the wrong thing on an
    *anonymous* route: every unauthenticated hit spent ~3-4 RU of the free
    tier's 1,000 RU/s that harvest shares, and the response carried the model
    deployment names and, on failure, the raw exception text, to anyone who
    asked.

    Dependency health now lives where only the operator can trigger it: the
    ``pipeline-silent`` alert on successful harvest runs, and from Phase 1 the
    admin health route. This endpoint touches no settings object, no Cosmos and
    no network, so it has nothing to fail on and nothing to echo.
    """
    return _json(
        {
            "status": "ok",
            "api": API_VERSION,
            "minBuild": _min_supported_build(),
            "time": iso(utcnow()),
        }
    )


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------
@app.timer_trigger(
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
