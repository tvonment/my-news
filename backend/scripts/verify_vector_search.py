#!/usr/bin/env python
"""M0 gate: does vector search actually work on a FREE-TIER Cosmos account?

SUPERSEDED — this script no longer describes the deployed system, and running it
against it will fail. Kept because the question it asks is the one you would ask
again before re-introducing a vector policy.

The fallback this script's exit-1 branch recommends is what shipped, though for
a reason it never anticipated. On 2026-08-02 the Cosmos data plane stopped being
able to deserialize the container's own `dataType: 'float16'` vector policy and
returned InternalServerError for every query on it. The policy was dropped from
infra/modules/data.bicep to recover, and not re-added: nothing in the pipeline
ever issued a VectorDistance query. All similarity is NumPy over embeddings read
out of Cosmos (pipeline/gate.py, pipeline/cluster.py).

So the storage design is now exactly the "fall back" case below — vectors are
ordinary document properties, excluded from the index, compared in-process.

If you ever do re-add a vector policy, run this first, and use `float32`.

    cd backend && .venv/bin/python scripts/verify_vector_search.py
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import cosmos  # noqa: E402
from core.config import get_settings  # noqa: E402

PROBE_PK = "vector-probe"


def unit_vector(dims: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    raw = [rng.gauss(0, 1) for _ in range(dims)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def main() -> int:
    settings = get_settings()
    dims = settings.embedding_dimensions
    container = cosmos.get_container()

    # Fail loudly and correctly rather than letting Cosmos reject VectorDistance
    # with a message that reads like a query bug.
    if not container.read().get("vectorEmbeddingPolicy", {}).get("vectorEmbeddings"):
        print("This container has no vector policy — see the module docstring.")
        print("Nothing to verify; VectorDistance would be rejected outright.")
        return 1

    print(f"Cosmos:     {settings.cosmos_endpoint}")
    print(f"Container:  {settings.cosmos_database}/{settings.cosmos_container}")
    print(f"Dimensions: {dims}\n")

    print("1. Writing probe documents with embeddings...")
    docs = []
    for i in range(3):
        doc = {
            "id": f"vector-probe-{i}",
            "pk": PROBE_PK,
            "type": "config",
            "title": f"probe {i}",
            "embedding": unit_vector(dims, seed=i),
            "ttl": 3600,
        }
        container.upsert_item(doc)
        docs.append(doc)
    print(f"   wrote {len(docs)} documents\n")

    print("2. Running a VectorDistance query...")
    query_vector = docs[1]["embedding"]
    try:
        results = list(
            container.query_items(
                query=(
                    "SELECT TOP 3 c.id, VectorDistance(c.embedding, @v) AS score "
                    "FROM c WHERE c.pk = @pk ORDER BY VectorDistance(c.embedding, @v)"
                ),
                parameters=[
                    {"name": "@v", "value": query_vector},
                    {"name": "@pk", "value": PROBE_PK},
                ],
                partition_key=PROBE_PK,
            )
        )
    except Exception as exc:
        print(f"   FAILED: {type(exc).__name__}: {exc}\n")
        print("VERDICT: vector search does NOT work on this account.")
        print("Fall back to packed-blob + NumPy similarity and drop the vector")
        print("policy from infra/modules/data.bicep.")
        return 1

    for row in results:
        print(f"   {row['id']:<18} score={row['score']:.4f}")

    if not results:
        print("\nVERDICT: query returned nothing — investigate before building on this.")
        return 1

    # The vector identical to the query must rank first. Note quantizedFlat
    # silently full-scans below 1,000 vectors, so this proves the feature works,
    # not that the index is being used.
    if results[0]["id"] != "vector-probe-1":
        print(f"\nVERDICT: nearest neighbour was {results[0]['id']}, expected vector-probe-1.")
        return 1

    print("\n3. Cleaning up probe documents...")
    for doc in docs:
        container.delete_item(item=doc["id"], partition_key=PROBE_PK)

    print("\nVERDICT: vector search works on the free-tier account. Plan §4 holds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
