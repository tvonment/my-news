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
from core.models import USER_PK, RUN_PK, iso, utcnow

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
        # And a query, because a point read alone is not a health check. During
        # the 2026-08-02 outage every query on this container returned
        # InternalServerError while point reads kept mostly working, so this
        # endpoint answered "healthy" on 3 of 8 probes with the whole app down.
        # Single-partition and counting only, so it stays ~2-3 RU.
        cosmos.query("SELECT VALUE COUNT(1) FROM c", partition_key=USER_PK)
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
# Digest — gate, score, summarize. All model spend happens here.
# ---------------------------------------------------------------------------
@app.timer_trigger(
    schedule="%DIGEST_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def digest_timer(timer: func.TimerRequest) -> None:
    from pipeline.digest import run_digest

    if timer.past_due:
        log.warning("Digest timer is past due")
    record = run_digest()
    log.info("Digest run %s cost $%.4f", record.id, record.estimated_usd)


@app.route(route="digest", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def digest(req: func.HttpRequest) -> func.HttpResponse:
    """Run a digest on demand.

    Note the 230-second platform ceiling on HTTP responses: a large backlog will
    outlast it. The run continues server-side, and `GET /api/runs` shows the
    result — but for routine use, let the timer do it.
    """
    from pipeline.digest import run_digest

    try:
        record = run_digest()
    except Exception as exc:
        log.exception("Manual digest failed")
        return _json({"error": str(exc)}, status=500)

    return _json(
        {
            "run": record.id,
            "gated": record.items_gated,
            "scored": record.items_scored,
            "summarized": record.items_summarized,
            "estimated_usd": record.estimated_usd,
            "budget_exhausted": record.budget_exhausted,
            "ok": record.ok,
            "error": record.error,
        }
    )


# ---------------------------------------------------------------------------
# Taste profile — the slow, legible half of the learning loop
# ---------------------------------------------------------------------------
@app.timer_trigger(
    schedule="%PROFILE_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def profile_timer(timer: func.TimerRequest) -> None:
    from pipeline.profile import rewrite_profile

    result = rewrite_profile()
    if result.rewritten:
        log.info("Profile now at v%d: %s", result.version, result.changelog)
    else:
        log.info("Profile unchanged: %s", result.reason)


@app.route(route="profile", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def get_profile(req: func.HttpRequest) -> func.HttpResponse:
    """What the filter currently believes about you, and how it got there."""
    from pipeline.profile import load_profile_doc

    doc = load_profile_doc()
    return _json(
        {
            "text": doc.get("text", ""),
            "version": doc.get("version", 0),
            "changelog": doc.get("changelog", ""),
            "source": doc.get("source", "default"),
            "updatedAt": doc.get("updated_at"),
            "history": doc.get("history", []),
        }
    )


@app.route(route="profile", methods=["PUT"], auth_level=func.AuthLevel.FUNCTION)
def put_profile(req: func.HttpRequest) -> func.HttpResponse:
    """Hand-edit the profile.

    A manual edit is versioned like any other and is used verbatim by the next
    scoring run — this is the steering wheel for when the automatic rewrite
    drifts somewhere you don't want.
    """
    from pipeline.profile import save_profile

    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "body must be JSON"}, status=400)

    text = (body.get("text") or "").strip()
    if len(text) < 100:
        return _json({"error": "profile text must be at least 100 characters"}, status=400)

    version = save_profile(text, body.get("changelog") or "Edited by hand.", source="manual")
    return _json({"ok": True, "version": version})


@app.route(route="profile/rewrite", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def rewrite_profile_now(req: func.HttpRequest) -> func.HttpResponse:
    """Trigger the rewrite on demand rather than waiting for the weekly timer."""
    from pipeline.profile import rewrite_profile

    result = rewrite_profile()
    return _json(
        {
            "rewritten": result.rewritten,
            "reason": result.reason,
            "version": result.version,
            "changelog": result.changelog,
            "estimated_usd": round(result.cost_usd, 6),
        }
    )


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------
@app.route(route="feedback", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def feedback(req: func.HttpRequest) -> func.HttpResponse:
    """Record a vote and fold it into the centroids immediately.

    The centroid update is what makes a vote take effect on the very next run
    without any LLM involvement. The weekly profile rewrite is the slow, legible
    half of the loop; this is the fast, numeric half.
    """
    from pipeline.gate import update_centroid

    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "body must be JSON"}, status=400)

    article_id = body.get("articleId")
    signal = body.get("signal")
    if not article_id or signal not in ("like", "dislike", "open"):
        return _json({"error": "articleId and signal (like|dislike|open) are required"}, status=400)

    article = None
    for row in cosmos.query(
        "SELECT * FROM c WHERE c.type = 'article' AND c.id = @id OFFSET 0 LIMIT 1",
        [{"name": "@id", "value": article_id}],
    ):
        article = row
        break

    if article is None:
        return _json({"error": "unknown articleId"}, status=404)

    # `iso()` rather than a bare `isoformat()`: the latter ends in "+00:00", and
    # `core/models.py` exists to keep "+" out of document ids because Cosmos hits
    # known SDK bugs on them. Seconds precision is enough — the id also carries
    # the article, so the only thing it collapses is a double-tap on one card.
    now = iso(utcnow())
    cosmos.upsert(
        {
            "id": f"evt_{now}_{article_id}",
            "pk": USER_PK,
            "type": "feedback",
            "article_id": article_id,
            "signal": signal,
            "dwell_ms": int(body.get("dwellMs", 0) or 0),
            "title": article.get("title"),
            "categories": article.get("categories", []),
            "why": article.get("why"),
            "created_at": now,
            "ttl": 60 * 60 * 24 * 400,
        }
    )

    embedding = article.get("embedding")
    if signal in ("like", "dislike") and embedding:
        update_centroid(signal, embedding)
        # A liked article is worth keeping past the 21-day TTL.
        if signal == "like":
            article["ttl"] = -1
            cosmos.upsert(article)

    return _json({"ok": True, "articleId": article_id, "signal": signal})


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------
@app.route(route="feed", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def feed(req: func.HttpRequest) -> func.HttpResponse:
    """Ranked feed for the app.

    Ordered by relevance, not recency — the whole point of the pipeline is that
    what matters most rises, and the recency preference is already folded into
    the gate score. Only items the scorer has judged are returned; anything still
    `harvested` or `gated` is invisible until the next digest run rates it.
    """
    try:
        limit = min(int(req.params.get("limit", "60")), 200)
        min_relevance = int(req.params.get("min_relevance", "0"))
    except ValueError:
        return _json({"error": "limit and min_relevance must be integers"}, status=400)

    category = req.params.get("category")
    since = req.params.get("since")

    # Collapsed cluster members are excluded: only the primary of each story is
    # shown. They keep their own record and stay searchable — they just don't
    # occupy a card repeating what the primary already says.
    sql = (
        "SELECT * FROM c WHERE c.type = 'article' AND IS_DEFINED(c.relevance) "
        "AND c.relevance >= @min_relevance "
        "AND (NOT IS_DEFINED(c.cluster_id) OR IS_NULL(c.cluster_id) OR c.cluster_id = c.id)"
    )
    parameters: list[dict[str, Any]] = [{"name": "@min_relevance", "value": min_relevance}]
    if category:
        sql += " AND ARRAY_CONTAINS(c.categories, @category)"
        parameters.append({"name": "@category", "value": category})
    if since:
        sql += " AND c.published_at >= @since"
        parameters.append({"name": "@since", "value": since})
    sql += " ORDER BY c.relevance DESC OFFSET 0 LIMIT @limit"
    parameters.append({"name": "@limit", "value": limit})

    try:
        items = cosmos.query(sql, parameters, max_items=limit)
        # Shaping stays inside the `try`: `_feed_item` subscripts `item["id"]`,
        # so a malformed document would otherwise escape as a bare 500 past the
        # handler written to prevent exactly that.
        payload = [_feed_item(i) for i in items]
    except Exception as exc:
        log.exception("Feed query failed")
        return _json({"error": str(exc)}, status=500)

    return _json({"count": len(payload), "items": payload})


def _feed_item(item: dict[str, Any]) -> dict[str, Any]:
    """Shape one article for the client.

    Deliberately omits `embedding` — 512 floats per article would dominate the
    payload and the client has no use for it.
    """
    return {
        "id": item["id"],
        "title": item.get("title"),
        "source": item.get("source_name"),
        "url": item.get("url"),
        "publishedAt": item.get("published_at"),
        "lead": (item.get("lead") or "")[:400],
        "relevance": item.get("relevance"),
        "why": item.get("why"),
        "categories": item.get("categories", []),
        "summary": item.get("summary", []),
        "entities": item.get("entities", []),
        "readingMinutes": item.get("reading_minutes"),
        "paywalled": item.get("paywalled", False),
        "satire": item.get("satire", False),
        "alsoReportedBy": item.get("also_reported_by", []),
        "clusterSize": item.get("cluster_size", 1),
        "state": item.get("state"),
    }


@app.route(route="categories", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def categories(req: func.HttpRequest) -> func.HttpResponse:
    """The fixed taxonomy plus how many unread-ish items sit in each bucket.

    Served from the backend rather than hardcoded in the app so the two can
    never disagree about what the categories are.
    """
    from core.taxonomy import CATEGORIES, CATEGORY_LABELS

    # Counted server-side with JOIN + GROUP BY rather than by pulling every
    # scored article back and tallying in Python. The old form had no LIMIT and
    # no max_items, so `cosmos.query`'s `list()` materialised the entire archive
    # on every call — and the app now hits this on every foreground.
    #
    # The result set is one row per distinct tag, not per article. That includes
    # the ~213 free-text tags the scorer invented before the taxonomy closed;
    # they are dropped below, same as before.
    rows = cosmos.query(
        "SELECT c.name AS name, COUNT(1) AS n FROM ("
        "SELECT VALUE {'name': cat} FROM a JOIN cat IN a.categories "
        "WHERE a.type = 'article' AND IS_DEFINED(a.relevance) "
        "AND (NOT IS_DEFINED(a.cluster_id) OR IS_NULL(a.cluster_id) OR a.cluster_id = a.id)"
        ") AS c GROUP BY c.name"
    )
    counts: dict[str, int] = {name: 0 for name in CATEGORIES}
    for row in rows:
        name = row.get("name")
        if name in counts:
            counts[name] = row.get("n", 0)

    return _json(
        {
            "categories": [
                {"id": name, "label": CATEGORY_LABELS[name], "count": counts[name]}
                for name in CATEGORIES
            ]
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
