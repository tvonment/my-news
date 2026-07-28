"""MyNews Azure Functions app (Python v2 programming model).

Two timers by design:

* ``harvest_timer`` — every 30-60 min. Fetch, dedupe, store. No LLM, ~$0.
  Frequent because several feeds carry only 10 items and would otherwise roll
  over between runs.
* ``digest_timer`` — 3x/day. Embed, score, summarize, push. All the cost lives
  here. Schedules are UTC: Flex Consumption does not support WEBSITE_TIME_ZONE.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import azure.functions as func

from core import cosmos
from core.config import get_settings
from core.models import USER_PK, RUN_PK

app = func.FunctionApp()
log = logging.getLogger(__name__)


def _json(payload: dict[str, Any], status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False, default=str),
        status_code=status,
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Liveness plus a real dependency check.

    Anonymous on purpose so it can be probed without a key — it returns no data,
    only whether configuration and Cosmos are reachable.
    """
    checks: dict[str, Any] = {}
    healthy = True

    try:
        settings = get_settings()
        checks["config"] = "ok"
        checks["models"] = {
            "scorer": settings.model_scorer,
            "summarizer": settings.model_summarizer,
            "embedding": settings.model_embedding,
        }
    except Exception as exc:
        healthy = False
        checks["config"] = f"error: {exc}"
        return _json({"status": "unhealthy", "checks": checks}, status=503)

    try:
        # Point read of a document that may not exist: ~1 RU, proves both the
        # managed-identity token and the data-plane role assignment work.
        cosmos.read("settings", USER_PK)
        checks["cosmos"] = "ok"
    except Exception as exc:
        healthy = False
        checks["cosmos"] = f"error: {exc}"

    return _json({"status": "healthy" if healthy else "unhealthy", "checks": checks},
                 status=200 if healthy else 503)


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


@app.route(route="refresh", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def refresh(req: func.HttpRequest) -> func.HttpResponse:
    """Trigger a harvest on demand.

    Returns a summary rather than enqueuing, because a harvest of the seed list
    finishes well inside the 230-second HTTP ceiling. The digest pipeline will
    not — that stays on its timer.
    """
    from pipeline.harvest import run_harvest

    source_id = req.params.get("source")
    try:
        record = run_harvest(only_source=source_id)
    except Exception as exc:
        log.exception("Manual refresh failed")
        return _json({"error": str(exc)}, status=500)

    return _json(
        {
            "run": record.id,
            "feeds_polled": record.feeds_polled,
            "feeds_unchanged": record.feeds_unchanged,
            "feeds_failed": record.feeds_failed,
            "items_seen": record.items_seen,
            "items_new": record.items_new,
            "ok": record.ok,
        }
    )


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------
@app.route(route="feed", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def feed(req: func.HttpRequest) -> func.HttpResponse:
    """Ranked feed for the app.

    Until the scoring stage lands (M2) this returns the newest harvested items so
    the client has something real to render.
    """
    try:
        limit = min(int(req.params.get("limit", "50")), 200)
    except ValueError:
        return _json({"error": "limit must be an integer"}, status=400)

    category = req.params.get("category")

    sql = "SELECT * FROM c WHERE c.type = 'article'"
    parameters: list[dict[str, Any]] = []
    if category:
        sql += " AND ARRAY_CONTAINS(c.categories, @category)"
        parameters.append({"name": "@category", "value": category})
    sql += " ORDER BY c.published_at DESC OFFSET 0 LIMIT @limit"
    parameters.append({"name": "@limit", "value": limit})

    try:
        items = cosmos.query(sql, parameters, max_items=limit)
    except Exception as exc:
        log.exception("Feed query failed")
        return _json({"error": str(exc)}, status=500)

    return _json(
        {
            "count": len(items),
            "items": [
                {
                    "id": item["id"],
                    "title": item.get("title"),
                    "source": item.get("source_name"),
                    "url": item.get("url"),
                    "published_at": item.get("published_at"),
                    "lead": item.get("lead", "")[:400],
                    "relevance": item.get("relevance"),
                    "why": item.get("why"),
                    "summary": item.get("summary", []),
                    "paywalled": item.get("paywalled", False),
                }
                for item in items
            ],
        }
    )


# ---------------------------------------------------------------------------
# Runs (operational visibility)
# ---------------------------------------------------------------------------
@app.route(route="runs", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def runs(req: func.HttpRequest) -> func.HttpResponse:
    """Recent pipeline runs with their real token spend.

    This is what keeps the cost model honest: every run records what it actually
    consumed, so docs/cost.md can be reconciled against reality.
    """
    items = cosmos.query(
        "SELECT * FROM c WHERE c.type = 'run' ORDER BY c.started_at DESC OFFSET 0 LIMIT 20",
        partition_key=RUN_PK,
    )
    return _json({"count": len(items), "runs": items})
