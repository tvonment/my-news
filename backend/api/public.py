"""The one public route: GET /health (docs/api.md section 6.1).

Its contract is unchanged since Phase 0. It stays the only handler with the
``public`` access level, which tests/test_route_inventory.py holds.
"""

from __future__ import annotations

import azure.functions as func

from api import http
from core import auth
from core.config import min_supported_build
from core.models import API_VERSION, iso, utcnow

bp = func.Blueprint()


@bp.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.public
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Static liveness: the host is up, and which contract it speaks. No I/O.

    This used to read and query Cosmos, because during the 2026-08-02 outage a
    point-read-only check answered "healthy" on 3 of 8 probes with the whole
    app down. That made the check honest but put the wrong thing on an
    *anonymous* route: every unauthenticated hit spent ~3-4 RU of the free
    tier's 1,000 RU/s that harvest shares, and the response carried the model
    deployment names and, on failure, the raw exception text, to anyone who
    asked.

    Dependency health lives where only the operator can trigger it: the
    ``pipeline-silent`` alert on successful harvest runs, and GET
    /admin/health. This endpoint touches no settings object, no Cosmos and no
    network (``min_supported_build`` reads the environment directly), so it
    has nothing to fail on and nothing to echo. Like every response it carries
    ``X-Request-Id``, which the public decorator adds without any I/O.
    """
    return http.json_response(
        {
            "status": "ok",
            "api": API_VERSION,
            "minBuild": min_supported_build(),
            "time": iso(utcnow()),
        }
    )
