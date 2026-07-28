"""Cosmos access.

Two rules this module exists to enforce:

1. **One client, cached for the process lifetime.** A Cosmos account has a fixed
   240 RU/s budget for metadata operations that cannot be raised, so a Function
   that re-resolves the database and container on every invocation can throttle
   on the control plane while the data plane sits idle.
2. **Dedupe by 409, not by query.** ``create_item`` on a content-addressed id
   costs ~1 RU and tells us "already seen" via ``CosmosResourceExistsError``.
   A ``SELECT`` to check first would cost more and race.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Iterable

from azure.cosmos import CosmosClient, ContainerProxy
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError
from azure.identity import DefaultAzureCredential

from core.config import get_settings, require

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


@lru_cache(maxsize=1)
def get_container() -> ContainerProxy:
    """The single type-discriminated container. Cached for the process lifetime."""
    settings = get_settings()
    endpoint = require(settings.cosmos_endpoint, "COSMOS_ENDPOINT")
    client = CosmosClient(endpoint, credential=_credential())
    database = client.get_database_client(require(settings.cosmos_database, "COSMOS_DATABASE"))
    return database.get_container_client(require(settings.cosmos_container, "COSMOS_CONTAINER"))


def try_create(doc: dict[str, Any]) -> bool:
    """Create a document, returning False if it already exists.

    This is the dedupe primitive: the caller does not need to know whether the
    article is new, only whether it should do further work on it.
    """
    try:
        get_container().create_item(body=doc)
        return True
    except CosmosResourceExistsError:
        return False


def upsert(doc: dict[str, Any]) -> None:
    get_container().upsert_item(body=doc)


def read(doc_id: str, pk: str) -> dict[str, Any] | None:
    try:
        return get_container().read_item(item=doc_id, partition_key=pk)
    except CosmosResourceNotFoundError:
        return None


def query(
    sql: str,
    parameters: list[dict[str, Any]] | None = None,
    partition_key: str | None = None,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    """Run a query, scoped to a partition whenever the caller can manage it.

    Cross-partition queries fan out to every physical partition, and for vector
    queries specifically the cost is roughly 45 RU *per partition hit* — so an
    unscoped ranking query gets more expensive as the archive grows.
    """
    kwargs: dict[str, Any] = {
        "query": sql,
        "parameters": parameters or [],
    }
    if partition_key is not None:
        kwargs["partition_key"] = partition_key
    else:
        kwargs["enable_cross_partition_query"] = True
    if max_items is not None:
        kwargs["max_item_count"] = max_items

    return list(get_container().query_items(**kwargs))


def bulk_create(docs: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Create many documents, counting new vs already-seen.

    Deliberately sequential. The free tier is a hard 1,000 RU/s ceiling, and
    firing hundreds of unbounded concurrent upserts just converts throughput into
    429s that the SDK then has to retry — the run takes longer, not less time.
    """
    created = skipped = 0
    for doc in docs:
        if try_create(doc):
            created += 1
        else:
            skipped += 1
    return created, skipped
