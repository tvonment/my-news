"""Cosmos access.

Two rules this module exists to enforce:

1. **One client, cached for the process lifetime.** A Cosmos account has a fixed
   240 RU/s budget for metadata operations that cannot be raised, so a Function
   that re-resolves the database and container on every invocation can throttle
   on the control plane while the data plane sits idle.
2. **Dedupe by 409, not by query.** ``create_item`` on a content-addressed id
   costs ~1 RU and tells us "already seen" via ``CosmosResourceExistsError``.
   A ``SELECT`` to check first would cost more and race.

Phase 1 adds what the v2 code needs on top of that (docs/data-model.md section 2):

* **Two containers.** ``get_container()`` is ``COSMOS_CONTAINER`` (``news``),
  which all new code uses. The v1 harvest passes ``container=`` with
  ``LEGACY_COSMOS_CONTAINER`` (``items``) on every call, explicitly, so that a
  forgotten argument fails review instead of quietly writing v1 documents into
  ``news``.
* **Typed errors.** Callers catch ``NotFound``, ``AlreadyExists``,
  ``PreconditionFailed`` and ``Unavailable`` instead of SDK classes, so a 412
  from a conditional write and a 429 that outlived the SDK's retries can be told
  apart without reading status codes. SDK exception chains are dropped on
  purpose: their text can carry the request path, and a registry document's id
  is an oid, which must never reach a log. The status, sub-status and activity
  id survive, which is what a support ticket needs.
* **Conditional writes.** ``replace_if_match`` (if-match on ``_etag``) for
  whole-document updates; ``patch`` with a ``filter_predicate`` for counters and
  guarded transitions.
* **RU accounting.** Every call reports its ``x-ms-request-charge`` to the
  ``metered()`` meters and ``paced()`` pacers open in the caller's context.
* **The LOCAL_READONLY guard.** A local run talks to the production account, so
  with ``LOCAL_READONLY`` set every write raises ``WriteBlocked`` unless it runs
  inside ``allow_prod_writes(reason)``, which the scripts open only for
  ``--allow-prod-write``.
"""

from __future__ import annotations

import contextvars
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping, Sequence, TypeVar

from azure.core import MatchConditions
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError, ServiceResponseError
from azure.cosmos import ContainerProxy, CosmosClient
from azure.cosmos.exceptions import (
    CosmosBatchOperationError,
    CosmosClientTimeoutError,
    CosmosHttpResponseError,
)

from core.config import get_settings, require
from core.models import SYSTEM_PROPERTIES

if TYPE_CHECKING:
    from azure.identity import DefaultAzureCredential

log = logging.getLogger(__name__)

T = TypeVar("T")

# Background write budgets in RU/s (docs/data-model.md section 7). On `news`
# (600 RU/s) background work stays at or below 65 %, so interactive requests
# never queue behind a purge. On `items` (400 RU/s) the v1 harvest bursts used
# to reach 550-600; it is paced to 350 until Phase 2 deletes it.
RU_RATE_LEGACY_HARVEST = 350
RU_RATE_PURGE = 50
RU_RATE_OPS = 50

# Cosmos refuses a patch with more than 10 operations and a transactional batch
# with more than 100.
MAX_PATCH_OPERATIONS = 10
MAX_BATCH_OPERATIONS = 100

# Q12 in docs/data-model.md: the purge's id pages. No filter, so no index.
SQL_IDS = "SELECT c.id FROM c"

# Statuses that mean "the service could not do it now, try later": request
# timeout, throttled after the SDK's own retries, a retried write conflict, and
# service unavailable.
_UNAVAILABLE_STATUSES = frozenset({408, 429, 449, 503})
# 404 sub-status 1003 is "owner resource not found": the container or database
# itself is missing. Treating that as a missing document would make every member
# look like a stranger (403 invite_required) instead of reporting an outage.
_SUB_STATUS_OWNER_MISSING = 1003


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class CosmosError(Exception):
    """Base of every error this module raises. ``status`` is the HTTP status
    Cosmos answered, or None when no response arrived."""

    def __init__(self, message: str, *, status: int | None = None, sub_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.sub_status = sub_status


class NotFound(CosmosError):
    """404: the document does not exist (``read`` returns None instead)."""


class AlreadyExists(CosmosError):
    """409 on create: the id is taken."""


class PreconditionFailed(CosmosError):
    """412: the etag moved, or a patch's ``filter_predicate`` did not hold."""


class Unavailable(CosmosError):
    """No answer worth acting on: 408/429/449/503 after the SDK's retries, a
    transport failure, no token from the managed identity, or a missing
    container. The API answers 503 degraded."""


class WriteBlocked(CosmosError):
    """A write from a LOCAL_READONLY process outside ``allow_prod_writes()``."""


class BatchFailed(CosmosError):
    """A transactional batch failed as a whole. ``index`` is the operation that
    failed and ``status`` its status (409 for a create that collided, 412 for a
    failed if-match or predicate); every other operation was rolled back."""

    def __init__(self, message: str, *, index: int, status: int):
        super().__init__(message, status=status)
        self.index = index


def _status_error(op: str, status: int, sub_status: int | None, headers: Mapping[str, Any] | None) -> CosmosError:
    activity = (headers or {}).get("x-ms-activity-id")
    detail = f"{op}: HTTP {status}" + (f"/{sub_status}" if sub_status else "")
    if activity:
        detail += f" (activity {activity})"
    if status == 404 and sub_status == _SUB_STATUS_OWNER_MISSING:
        return Unavailable(detail + ": container or database missing", status=status, sub_status=sub_status)
    if status == 404:
        return NotFound(detail, status=status, sub_status=sub_status)
    if status == 409:
        return AlreadyExists(detail, status=status, sub_status=sub_status)
    if status == 412:
        return PreconditionFailed(detail, status=status, sub_status=sub_status)
    if status in _UNAVAILABLE_STATUSES:
        return Unavailable(detail, status=status, sub_status=sub_status)
    return CosmosError(detail, status=status, sub_status=sub_status)


@contextmanager
def _translated(op: str) -> Iterator[None]:
    """Turn SDK and transport errors into this module's, charging failed calls
    too: a 404 or a 412 still costs RU, and the pacer must see it."""
    try:
        yield
    except CosmosBatchOperationError as exc:
        _account(exc.headers)
        index = exc.error_index if isinstance(exc.error_index, int) else -1
        status = int(exc.status_code or 0)
        raise BatchFailed(f"{op}: operation {index} failed with HTTP {status}", index=index, status=status) from None
    except CosmosHttpResponseError as exc:
        _account(exc.headers)
        raise _status_error(op, int(exc.status_code or 0), exc.sub_status, exc.headers) from None
    except (ServiceRequestError, ServiceResponseError, CosmosClientTimeoutError, ClientAuthenticationError) as exc:
        # The last is the managed identity failing to produce a token
        # (CredentialUnavailableError is one): the database is unreachable to
        # us, which is an outage to report, not a bug to 500 on.
        raise Unavailable(f"{op}: {type(exc).__name__}") from None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _credential() -> "DefaultAzureCredential":
    # Imported here, not at module level: azure.identity pulls in msal and
    # requests, and /health shares this process without ever needing them.
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


@lru_cache(maxsize=1)
def _client() -> CosmosClient:
    """The one client (rule 1). Both containers share it."""
    endpoint = require(get_settings().cosmos_endpoint, "COSMOS_ENDPOINT")
    return CosmosClient(endpoint, credential=_credential())


@lru_cache(maxsize=4)
def get_container(name: str | None = None) -> ContainerProxy:
    """A container of the one client, cached for the process lifetime.

    ``None`` is ``COSMOS_CONTAINER`` (``news``). Tests replace this function with
    one that returns an in-memory container, so every helper below resolves it
    at call time.
    """
    settings = get_settings()
    database = _client().get_database_client(require(settings.cosmos_database, "COSMOS_DATABASE"))
    return database.get_container_client(require(name or settings.cosmos_container, "COSMOS_CONTAINER"))


# ---------------------------------------------------------------------------
# RU accounting and pacing
# ---------------------------------------------------------------------------
@dataclass
class RuMeter:
    """What the calls inside one ``metered()`` block cost."""

    charge: float = 0.0
    operations: int = 0


@dataclass
class RuPacer:
    """A token bucket over request units.

    Cosmos reports a call's cost only in its response, so the pacer charges after
    the fact: a call that overdraws the bucket is paid back by sleeping before
    the next one. Over any stretch longer than a second the spend therefore
    stays at ``rate_per_s``, with at most one second's worth as a burst. The
    bucket lives in this process only. Flex runs each function on its own
    instances, which is why the per-writer rates are fixed so that their *sum*
    stays under 65 % of the container's throughput.
    """

    rate_per_s: float
    # None means time.monotonic and time.sleep, looked up at call time so a
    # test can replace them without reaching into every pacer.
    clock: Callable[[], float] | None = field(default=None, repr=False)
    sleep: Callable[[float], None] | None = field(default=None, repr=False)
    tokens: float = field(init=False)
    slept_s: float = field(init=False, default=0.0)
    _last: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.rate_per_s > 0:
            raise ValueError("rate_per_s must be positive")
        self.tokens = float(self.rate_per_s)
        self._last = self._now()

    def _now(self) -> float:
        return (self.clock or time.monotonic)()

    def spend(self, charge: float) -> None:
        now = self._now()
        self.tokens = min(float(self.rate_per_s), self.tokens + (now - self._last) * self.rate_per_s)
        self._last = now
        self.tokens -= max(float(charge), 0.0)
        if self.tokens < 0:
            wait = -self.tokens / self.rate_per_s
            (self.sleep or time.sleep)(wait)
            self.slept_s += wait
            self.tokens = 0.0
            self._last = self._now()


_METERS: contextvars.ContextVar[tuple[RuMeter, ...]] = contextvars.ContextVar("mynews_ru_meters", default=())
_PACERS: contextvars.ContextVar[tuple[RuPacer, ...]] = contextvars.ContextVar("mynews_ru_pacers", default=())


def _request_charge(headers: Mapping[str, Any] | None) -> float:
    if not headers:
        return 0.0
    try:
        return float(headers.get("x-ms-request-charge") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _account(headers: Mapping[str, Any] | None) -> None:
    charge = _request_charge(headers)
    for meter in _METERS.get():
        meter.charge += charge
        meter.operations += 1
    for pacer in _PACERS.get():
        pacer.spend(charge)


def _on_response(headers: Mapping[str, Any], _result: Any) -> None:
    # The SDK calls this once per successful response, and once per page of a
    # query. Failed calls are charged in _translated() from the error's headers.
    _account(headers)


@contextmanager
def metered() -> Iterator[RuMeter]:
    """Measure the RU of every call made inside the block. Nested meters all
    see every call, so a purge can meter itself inside a metered request."""
    meter = RuMeter()
    token = _METERS.set(_METERS.get() + (meter,))
    try:
        yield meter
    finally:
        _METERS.reset(token)


@contextmanager
def paced(rate_per_s: float) -> Iterator[RuPacer]:
    """Hold every call inside the block to ``rate_per_s`` RU/s (RU_RATE_*)."""
    pacer = RuPacer(rate_per_s)
    token = _PACERS.set(_PACERS.get() + (pacer,))
    try:
        yield pacer
    finally:
        _PACERS.reset(token)


# ---------------------------------------------------------------------------
# LOCAL_READONLY
# ---------------------------------------------------------------------------
_PROD_WRITES_ALLOWED: contextvars.ContextVar[bool] = contextvars.ContextVar("mynews_prod_writes", default=False)


@contextmanager
def allow_prod_writes(reason: str) -> Iterator[None]:
    """Let the block write although LOCAL_READONLY is set, and say so in the log.

    Scoped to the calling context rather than the process, so an override opened
    for one migration step cannot leak into unrelated work. A thread started
    inside the block does not inherit it unless it copies the context.
    """
    if not reason or not reason.strip():
        raise ValueError("allow_prod_writes() needs a reason; it is logged")
    log.warning("LOCAL_READONLY override: %s", reason)
    token = _PROD_WRITES_ALLOWED.set(True)
    try:
        yield
    finally:
        _PROD_WRITES_ALLOWED.reset(token)


def _guard_write(op: str) -> None:
    if get_settings().local_readonly and not _PROD_WRITES_ALLOWED.get():
        raise WriteBlocked(f"{op} refused: LOCAL_READONLY is set and no allow_prod_writes() is open")


def _body(doc: Mapping[str, Any]) -> dict[str, Any]:
    """A document as written: system properties are the server's to set."""
    return {k: v for k, v in doc.items() if k not in SYSTEM_PROPERTIES}


# ---------------------------------------------------------------------------
# Point operations
# ---------------------------------------------------------------------------
def read(doc_id: str, pk: str, *, container: str | None = None) -> dict[str, Any] | None:
    """Point read (~1 RU for a small document); None when it does not exist.
    The document carries ``_etag`` for a later ``replace_if_match``."""
    try:
        with _translated("read"):
            return get_container(container).read_item(item=doc_id, partition_key=pk, response_hook=_on_response)
    except NotFound:
        return None


def create(doc: dict[str, Any], *, container: str | None = None) -> dict[str, Any]:
    """Create a document; ``AlreadyExists`` when its id is taken."""
    _guard_write("create")
    with _translated("create"):
        return get_container(container).create_item(body=_body(doc), response_hook=_on_response)


def try_create(doc: dict[str, Any], *, container: str | None = None) -> bool:
    """Create a document, returning False if it already exists.

    This is the dedupe primitive: the caller does not need to know whether the
    article is new, only whether it should do further work on it.
    """
    try:
        create(doc, container=container)
        return True
    except AlreadyExists:
        return False


def upsert(doc: dict[str, Any], *, container: str | None = None) -> dict[str, Any]:
    _guard_write("upsert")
    with _translated("upsert"):
        return get_container(container).upsert_item(body=_body(doc), response_hook=_on_response)


def replace_if_match(doc: dict[str, Any], etag: str, *, container: str | None = None) -> dict[str, Any]:
    """Replace a document only if it is still the version ``etag`` names.

    ``PreconditionFailed`` means someone wrote in between: re-read, re-apply,
    retry. ``NotFound`` means it was deleted.
    """
    if not etag:
        raise ValueError("replace_if_match() needs the _etag of the version the change was based on")
    _guard_write("replace")
    body = _body(doc)
    with _translated("replace"):
        return get_container(container).replace_item(
            item=body["id"],
            body=body,
            etag=etag,
            match_condition=MatchConditions.IfNotModified,
            response_hook=_on_response,
        )


def patch(
    doc_id: str,
    pk: str,
    operations: Sequence[Mapping[str, Any]],
    *,
    filter_predicate: str | None = None,
    container: str | None = None,
) -> dict[str, Any]:
    """Partial update, atomically, optionally only while ``filter_predicate``
    (``FROM c WHERE ...``) holds on the stored document.

    ``incr`` creates a missing field, which is what makes a counter a single
    write. ``NotFound`` when the document is missing; ``PreconditionFailed`` when
    the predicate is false. Values inlined into a predicate must be
    server-generated (timestamps, uids, table keys), never request input.
    """
    if not operations or len(operations) > MAX_PATCH_OPERATIONS:
        raise ValueError(f"a patch takes 1-{MAX_PATCH_OPERATIONS} operations, got {len(operations)}")
    _guard_write("patch")
    kwargs: dict[str, Any] = {}
    if filter_predicate is not None:
        kwargs["filter_predicate"] = filter_predicate
    with _translated("patch"):
        return get_container(container).patch_item(
            item=doc_id,
            partition_key=pk,
            patch_operations=[dict(operation) for operation in operations],
            response_hook=_on_response,
            **kwargs,
        )


def delete(doc_id: str, pk: str, *, container: str | None = None) -> bool:
    """Delete a document; False when it was already gone, so a resumed purge
    can repeat any step."""
    _guard_write("delete")
    try:
        with _translated("delete"):
            get_container(container).delete_item(item=doc_id, partition_key=pk, response_hook=_on_response)
        return True
    except NotFound:
        return False


def batch(pk: str, operations: list[tuple], *, container: str | None = None) -> list[dict[str, Any]]:
    """A transactional batch in one partition: all operations or none.

    Operations use the SDK's tuple form, e.g. ``("create", (doc,))``,
    ``("replace", (id, doc), {"if_match_etag": etag})`` or
    ``("patch", (id, ops), {"filter_predicate": "FROM c WHERE ..."})``.
    ``BatchFailed`` names the operation that failed.
    """
    if not operations:
        return []
    if len(operations) > MAX_BATCH_OPERATIONS:
        raise ValueError(f"a batch takes at most {MAX_BATCH_OPERATIONS} operations, got {len(operations)}")
    _guard_write("batch")
    with _translated("batch"):
        return list(
            get_container(container).execute_item_batch(
                batch_operations=operations, partition_key=pk, response_hook=_on_response
            )
        )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def query(
    sql: str,
    parameters: list[dict[str, Any]] | None = None,
    partition_key: str | None = None,
    max_items: int | None = None,
    *,
    container: str | None = None,
) -> list[Any]:
    """Run a query, scoped to a partition whenever the caller can manage it.

    Cross-partition queries fan out to every physical partition, and for vector
    queries specifically the cost is roughly 45 RU *per partition hit* — so an
    unscoped ranking query gets more expensive as the archive grows.

    Against ``news`` a partition key is mandatory (docs/data-model.md section 2):
    every v2 list is designed to live in one partition, and a query that forgot
    its key would silently fan out. Only the legacy container still allows a
    cross-partition query, for v1 code. ``max_items`` is the page size, not a
    total; use ``TOP`` to bound a result. Rows are documents, projections or,
    for ``SELECT VALUE``, scalars.
    """
    settings = get_settings()
    target = container or settings.cosmos_container
    kwargs: dict[str, Any] = {"query": sql, "parameters": parameters or []}
    if partition_key is not None:
        kwargs["partition_key"] = partition_key
    elif target == settings.legacy_cosmos_container:
        kwargs["enable_cross_partition_query"] = True
    else:
        raise ValueError(f"queries against container {target!r} must name a partition_key")
    if max_items is not None:
        kwargs["max_item_count"] = max_items
    with _translated("query"):
        return list(get_container(container).query_items(response_hook=_on_response, **kwargs))


def iter_ids(pk: str, *, container: str | None = None, page_size: int = 100) -> Iterator[str]:
    """Every document id in one partition, a page at a time (the purge's walk).

    Deleting ids already yielded while the walk continues is safe: the
    continuation resumes after the last id returned.
    """
    with _translated("query"):
        items = get_container(container).query_items(
            query=SQL_IDS,
            parameters=[],
            partition_key=pk,
            max_item_count=page_size,
            response_hook=_on_response,
        )
        for row in items:
            yield row["id"]


def bulk_create(docs: Iterable[dict[str, Any]], *, container: str | None = None) -> tuple[int, int]:
    """Create many documents, counting new vs already-seen.

    Deliberately sequential. The free tier is a hard 1,000 RU/s ceiling, and
    firing hundreds of unbounded concurrent upserts just converts throughput into
    429s that the SDK then has to retry — the run takes longer, not less time.
    """
    created = skipped = 0
    for doc in docs:
        if try_create(doc, container=container):
            created += 1
        else:
            skipped += 1
    return created, skipped
