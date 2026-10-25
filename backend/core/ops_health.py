"""GET /admin/health: deep checks of everything the backend depends on
(docs/api.md section 6.5, schema AdminHealth).

The rules, and why:

* **Always an answer.** The route answers 200 with ``status: degraded`` when a
  check fails; a health route that fails or hangs during an outage tells the
  owner nothing. So every check catches its own exceptions, and the checks
  that do I/O run side by side under one deadline: a Cosmos endpoint that
  stops answering would otherwise hold the request in SDK retries for minutes.
  A check still running at the deadline is reported as ``timeout`` (its thread
  finishes in the background; every check is read-only).
* **No exception text, no values.** A failed check reports an error class
  (``timeout``, ``unavailable``, ``forbidden``, ``not_found``, ``throttled``,
  ``misconfigured``, ``failed``). Configuration problems are reported by setting
  *name*. The HMAC key is shown only as its fingerprint.
* **Cheap.** About four queries and four queue peeks (storage, not RU).

In practice the auth settings never show up under ``config``: when they are
broken, auth fails closed (503 auth_unavailable) before this route runs, and
the names go to the log instead. The check still covers them for callers that
reach deep_health() some other way.
"""

from __future__ import annotations

import concurrent.futures
import logging
import math
import os
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from core import accounts, auth, cosmos, identity_removal, jobs
from core.config import get_settings, min_supported_build, unresolved_keyvault_ref
from core.models import API_VERSION, PK_USERS, T_USER, iso, parse_iso

log = logging.getLogger(__name__)

CHECK_TIMEOUT_S = 5.0
# Every I/O check runs at once; this bounds the whole route.
DEADLINE_S = 8.0
HARVEST_MAX_AGE = timedelta(hours=3)
QUEUE_MAX_AGE = timedelta(minutes=10)
APPLE_SECRET_WARN_DAYS = accounts.APPLE_SECRET_WARN_DAYS

# The queues whose state means something in Phase 1. `suggestions` has no
# worker before Phase 3, so anything waiting there is expected.
WATCHED_QUEUES = (jobs.QUEUE_ACCOUNT_PURGE, jobs.QUEUE_OPS_JOBS)

# What the config check looks at, beyond the auth set: the database endpoint
# and the queue endpoint (without which purges stop).
EXTRA_REQUIRED_SETTINGS = ("COSMOS_ENDPOINT", "JOBS_STORAGE__queueServiceUri")


# ---------------------------------------------------------------------------
# Error classes
# ---------------------------------------------------------------------------
def _status_class(status: int | None) -> str | None:
    if status in (401, 403):
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "throttled"
    if status in (408,):
        return "timeout"
    if status is not None and status >= 500:
        return "unavailable"
    return None


def classify(exc: BaseException) -> str:
    """The CheckError class of an exception from a check."""
    if isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError)):
        return "timeout"
    if isinstance(exc, cosmos.CosmosError):
        if isinstance(exc, cosmos.Unavailable) and exc.status == 404:
            return "not_found"  # the container or database itself is missing
        by_status = _status_class(exc.status)
        if by_status is not None:
            return by_status
        return "unavailable" if isinstance(exc, cosmos.Unavailable) else "failed"
    if isinstance(exc, RuntimeError) and "Required setting" in str(exc):
        # core.config.require(): the endpoint or database name is unset.
        return "misconfigured"
    return "failed"


def _timed(fn: Callable[[], Any]) -> tuple[Any, int, str | None]:
    """(result, elapsed ms, error class). Slower than CHECK_TIMEOUT_S counts as
    a timeout even when it answered."""
    started = time.monotonic()
    try:
        result = fn()
        error = None
    except Exception as exc:  # noqa: BLE001 - classified, never echoed
        log.warning("Health check failed: %s", type(exc).__name__)
        result, error = None, classify(exc)
    elapsed = time.monotonic() - started
    if error is None and elapsed > CHECK_TIMEOUT_S:
        error = "timeout"
    return result, int(elapsed * 1000), error


# ---------------------------------------------------------------------------
# Checks without I/O
# ---------------------------------------------------------------------------
def config_check() -> dict[str, Any]:
    """Setting names that are missing or invalid, and those still holding an
    unresolved Key Vault reference (which App Service passes through as text)."""
    names = (*auth.AUTH_SETTING_NAMES, *EXTRA_REQUIRED_SETTINGS)
    unresolved = [name for name in names if unresolved_keyvault_ref(os.environ.get(name, ""))]
    problems = set(auth.config_problems())
    settings = get_settings()
    if not settings.cosmos_endpoint:
        problems.add("COSMOS_ENDPOINT")
    if not settings.jobs_queue_service_uri.startswith("https://"):
        problems.add("JOBS_STORAGE__queueServiceUri")
    missing = [name for name in names if name in problems and name not in unresolved]
    return {"ok": not missing and not unresolved, "missing": missing, "unresolvedKeyVaultRefs": unresolved}


def secrets_check(now: datetime) -> dict[str, Any]:
    """The Sign in with Apple client secret's renewal date: failing from 30
    days before it. Unset is fine (nothing to renew yet); unparsable is not."""
    raw = get_settings().apple_secret_renew_by
    renew_by, days_left = accounts.apple_secret_renewal(now)
    if not raw:
        return {"ok": True, "appleSecretRenewBy": None, "daysLeft": None}
    if renew_by is None or days_left is None:
        return {"ok": False, "appleSecretRenewBy": None, "daysLeft": None}
    return {"ok": days_left > APPLE_SECRET_WARN_DAYS, "appleSecretRenewBy": renew_by, "daysLeft": days_left}


# ---------------------------------------------------------------------------
# Checks with I/O (run in worker threads)
# ---------------------------------------------------------------------------
def _cosmos_check() -> dict[str, Any]:
    _, ms, error = _timed(
        lambda: cosmos.query(
            accounts.SQL_COUNT_TYPE, [{"name": "@type", "value": T_USER}], partition_key=PK_USERS
        )
    )
    return {"ok": error is None, "ms": ms, "error": error}


def _legacy_check() -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    """(legacyCosmos check, the last v1 harvest, whether it could be read)."""
    run, ms, error = _timed(accounts.fetch_last_v1_harvest)
    return {"ok": error is None, "ms": ms, "error": error}, run, error is None


def _harvest_check(run: dict[str, Any] | None, readable: bool, now: datetime) -> dict[str, Any]:
    """The newest v1 run must be under 3 hours old and have succeeded. A
    container that cannot be read proves nothing either way: not ok."""
    if not readable or run is None:
        return {"ok": False, "lastRunAt": None, "lastRunOk": None, "ageMinutes": None}
    age = now - parse_iso(run["at"])
    age_minutes = max(0, int(age.total_seconds() // 60))
    ok = bool(run["ok"]) and age <= HARVEST_MAX_AGE
    return {"ok": ok, "lastRunAt": run["at"], "lastRunOk": bool(run["ok"]), "ageMinutes": age_minutes}


def _jwks_check() -> dict[str, Any]:
    started = time.monotonic()
    try:
        ok, keys, error = auth.jwks_status(timeout_s=CHECK_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - jwks_status classifies; this is a backstop
        ok, keys, error = False, None, classify(exc)
    ms = int((time.monotonic() - started) * 1000)
    return {"ok": ok and error is None, "ms": ms, "error": error, "keys": keys}


def _minutes_since(timestamp: str | None, now: datetime) -> int | None:
    if timestamp is None:
        return None
    return max(0, math.floor((now - parse_iso(timestamp)).total_seconds() / 60))


def _queues_check(now: datetime) -> dict[str, Any]:
    """Fails when a peek fails, a poison queue holds anything, or a main
    queue's oldest message has waited more than 10 minutes (the workers poll
    every 5 seconds while an instance is up)."""
    started = time.monotonic()
    poison: dict[str, int | None] = {}
    oldest: dict[str, int | None] = {}
    errors: list[str] = []
    ok = True
    for queue in WATCHED_QUEUES:
        main = jobs.peek(queue, timeout_s=CHECK_TIMEOUT_S)
        dead = jobs.peek(jobs.poison_queue(queue), timeout_s=CHECK_TIMEOUT_S)
        waited = _minutes_since(main.oldest_inserted_at, now) if main.ok else None
        oldest[queue] = waited
        poison[jobs.poison_queue(queue)] = dead.count if dead.ok else None
        for peeked in (main, dead):
            if not peeked.ok:
                ok = False
                errors.append(peeked.error or "failed")
        if dead.ok and (dead.count or 0) > 0:
            ok = False
        if waited is not None and waited > QUEUE_MAX_AGE.total_seconds() / 60:
            ok = False
    ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": ok,
        "ms": ms,
        "error": errors[0] if errors else None,
        "poison": poison,
        "oldestMessageMinutes": oldest,
    }


def _deletions_check(now: datetime) -> dict[str, Any]:
    """Purges stuck for more than an hour (a lost message, or a worker that
    died: what the maintenance timer re-enqueues) and identity removals
    waiting more than 7 days."""
    stuck = len(accounts.stuck_deletions(now))
    overdue = identity_removal.count_overdue(now)
    return {"ok": stuck == 0 and overdue == 0, "stuckPurging": stuck, "overdueIdentityRemovals": overdue}


def _failed_shape(name: str, error: str) -> dict[str, Any]:
    """What a check reports when it raised past its own handling or missed the
    deadline. Shapes follow AdminHealth; counts that cannot be null are 0."""
    if name in ("cosmos", "legacyCosmos"):
        return {"ok": False, "ms": None, "error": error}
    if name == "jwks":
        return {"ok": False, "ms": None, "error": error, "keys": None}
    if name == "queues":
        return {
            "ok": False,
            "ms": None,
            "error": error,
            "poison": {jobs.poison_queue(q): None for q in WATCHED_QUEUES},
            "oldestMessageMinutes": {q: None for q in WATCHED_QUEUES},
        }
    if name == "deletions":
        return {"ok": False, "stuckPurging": 0, "overdueIdentityRemovals": 0}
    raise KeyError(name)


# ---------------------------------------------------------------------------
# The route body
# ---------------------------------------------------------------------------
def deep_health(now: datetime) -> dict[str, Any]:
    """The AdminHealth body. Always renders; ``status`` is "ok" only when every
    check is."""
    settings = get_settings()
    io_checks: dict[str, Callable[[], Any]] = {
        "cosmos": _cosmos_check,
        "legacyCosmos": _legacy_check,
        "jwks": _jwks_check,
        "queues": lambda: _queues_check(now),
        "deletions": lambda: _deletions_check(now),
    }
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(io_checks), thread_name_prefix="mynews-health")
    try:
        futures = {name: pool.submit(fn) for name, fn in io_checks.items()}
        concurrent.futures.wait(futures.values(), timeout=DEADLINE_S)
        results: dict[str, Any] = {}
        for name, future in futures.items():
            if not future.done():
                future.cancel()
                log.warning("Health check %s missed the %.0f s deadline", name, DEADLINE_S)
                results[name] = None
                continue
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001 - each check is caught here too
                log.warning("Health check %s raised: %s", name, type(exc).__name__)
                results[name] = exc
    finally:
        # Never wait for a straggler: its answer would come too late anyway.
        pool.shutdown(wait=False, cancel_futures=True)

    checks: dict[str, Any] = {"config": config_check()}
    for name in ("cosmos", "legacyCosmos", "jwks", "queues", "deletions"):
        result = results[name]
        if result is None:
            checks[name] = _failed_shape(name, "timeout")
        elif isinstance(result, BaseException):
            checks[name] = _failed_shape(name, classify(result))
        elif name == "legacyCosmos":
            checks[name] = result[0]
        else:
            checks[name] = result

    legacy = results["legacyCosmos"]
    if isinstance(legacy, tuple):
        _, run, readable = legacy
        checks["harvest"] = _harvest_check(run, readable, now)
    else:
        checks["harvest"] = _harvest_check(None, False, now)
    checks["secrets"] = secrets_check(now)

    return {
        "status": "ok" if all(check["ok"] for check in checks.values()) else "degraded",
        "checkedAt": iso(now),
        "api": API_VERSION,
        "minBuild": min_supported_build(),
        "identityDeleteMode": identity_removal.mode(),
        "hmacKeyFingerprint": auth.hmac_key_fingerprint(settings.hmac_key),
        "checks": checks,
    }
