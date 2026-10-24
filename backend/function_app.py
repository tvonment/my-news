"""MyNews Azure Functions app (Python v2 programming model).

This file only creates the app, quiets the chatty third-party loggers and
registers the blueprints; every function lives in one of them:

* ``api/public.py``: ``health`` (anonymous, static, no I/O).
* ``api/account.py``: the caller's own account — ``me_redeem``, ``me_get``,
  ``me_patch``, ``me_delete``, ``me_export``.
* ``api/admin.py``: the owner's admin routes, ``admin_*``.
* ``jobs/timers.py``: ``harvest_timer`` (the v1 harvest, until Phase 2) and
  ``maintenance_timer`` (daily 02:15 UTC).
* ``jobs/queues.py``: ``account_purge_worker`` and ``ops_jobs_worker``.

docs/api.md section 5 is the route index, and tests/test_route_inventory.py
holds the registered set against it, so a later phase's route cannot appear
early by accident.

Every HTTP route is anonymous at the host (``AuthLevel.ANONYMOUS``, also the
app default here): authentication is the job of core.auth's access-level
decorators, which validate the Entra access token and fail closed. A function
key would add nothing a client can keep secret.

Heavy imports (the harvest's httpx and trafilatura, and from Phase 2 numpy and
openai) stay inside the handlers that need them, so an HTTP cold start does
not pay for them. Schedules are UTC: Flex Consumption does not support
WEBSITE_TIME_ZONE.
"""

from __future__ import annotations

import logging

import azure.functions as func

from api import account, admin, public
from jobs import queues, timers

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

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

app.register_blueprint(public.bp)
app.register_blueprint(account.bp)
app.register_blueprint(admin.bp)
app.register_blueprint(timers.bp)
app.register_blueprint(queues.bp)
