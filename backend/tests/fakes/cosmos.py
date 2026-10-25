"""An in-memory Cosmos account for tests: the ContainerProxy subset the backend
calls, raising the SDK's own exceptions so core.cosmos's translation runs too.

Use it through the ``fake_cosmos`` fixture (tests/conftest.py), which replaces
``core.cosmos.get_container``. What it reproduces, because production code
depends on it:

* point reads, creates (409 on a taken id), upserts, deletes (404 when gone);
* ``_etag`` that changes on every write, and ``replace_item`` with
  ``match_condition=IfNotModified`` failing with 412 when it moved;
* ``patch_item`` with add/set/replace/remove/incr on JSON-pointer paths,
  applied atomically, and ``filter_predicate`` evaluated on the stored
  document (412 when false);
* queries in the SQL subset of tests/fakes/sql.py, paged by ``max_item_count``,
  and, on container ``news``, the opt-in index policy: a filter or ORDER BY on a
  path the policy does not index is a 400, as it is in Cosmos, and an ORDER BY
  over two or more paths needs a matching composite index;
* transactional batches (all or nothing, CosmosBatchOperationError naming the
  failed operation);
* ``x-ms-request-charge`` on every response, through ``response_hook``.

For failure and race tests:

* ``fail(op, ...)`` raises an error before an operation runs, or after it ran
  (``after=True``: the write landed but the caller saw a timeout);
* ``before(op, callback, ...)`` runs a callback (say, a concurrent request)
  just before a matching operation;
* ``journal`` records every operation in order, for asserting write order.
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path as FsPath
from typing import Any, Callable, Iterator, Mapping

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosBatchOperationError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from tests.fakes import sql

# Rough per-operation charges (docs/data-model.md section 7). They only need to
# be non-zero and plausible: tests assert that charges are *accounted*, not
# Cosmos's exact prices.
CHARGES = {
    "read": 1.0,
    "create": 6.0,
    "upsert": 8.0,
    "replace": 10.0,
    "patch": 10.0,
    "delete": 6.0,
    "query_page": 2.5,
    "query_row": 0.05,
}

_POLICY_PATH = FsPath(__file__).resolve().parents[3] / "infra" / "modules" / "cosmos-index-policy.json"
# The policy as docs/data-model.md section 6.1 states it, for a checkout where
# the infra file is missing.
_DOCUMENTED_POLICY: dict[str, Any] = {
    "includedPaths": [
        {"path": f"/{name}/?"}
        for name in (
            "type", "status", "kind", "signal", "uid", "created_at", "last_active_at",
            "deletion_requested_at", "enrich_status", "first_seen_at", "last_pub_at",
        )
    ]
    + [{"path": "/source_ids/[]/?"}],
    "compositeIndexes": [
        [{"path": "/type", "order": "ascending"}, {"path": "/created_at", "order": "descending"}],
        [
            {"path": "/type", "order": "ascending"},
            {"path": "/status", "order": "ascending"},
            {"path": "/created_at", "order": "descending"},
        ],
        [
            {"path": "/type", "order": "ascending"},
            {"path": "/kind", "order": "ascending"},
            {"path": "/created_at", "order": "descending"},
        ],
        [
            {"path": "/type", "order": "ascending"},
            {"path": "/signal", "order": "ascending"},
            {"path": "/created_at", "order": "descending"},
        ],
        [{"path": "/enrich_status", "order": "ascending"}, {"path": "/first_seen_at", "order": "descending"}],
    ],
}


def news_index_policy() -> dict[str, Any]:
    if _POLICY_PATH.exists():
        return json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    return _DOCUMENTED_POLICY


def _error(cls: type, status: int, message: str, charge: float = 0.0) -> Exception:
    exc = cls(status_code=status, message=message)
    exc.headers = {"x-ms-request-charge": str(charge), "x-ms-activity-id": str(uuid.uuid4())}
    return exc


def not_found(charge: float = 1.0) -> Exception:
    return _error(CosmosResourceNotFoundError, 404, "Entity with the specified id does not exist in the system.", charge)


def conflict(charge: float = 1.0) -> Exception:
    return _error(CosmosResourceExistsError, 409, "Entity with the specified id already exists in the system.", charge)


def precondition_failed(charge: float = 1.0) -> Exception:
    return _error(CosmosAccessConditionFailedError, 412, "One of the specified pre-condition is not met.", charge)


def bad_request(message: str) -> Exception:
    return _error(CosmosHttpResponseError, 400, message)


def throttled() -> Exception:
    """What the SDK raises once its own 429 retries are exhausted."""
    return _error(CosmosHttpResponseError, 429, "Request rate is large.")


class _PatchError(Exception):
    pass


def _pointer(path: str) -> list[str]:
    if not path.startswith("/") or path == "/":
        raise _PatchError(f"bad patch path {path!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _parent(doc: Any, parts: list[str]) -> Any:
    node = doc
    for part in parts[:-1]:
        if isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                raise _PatchError("path does not exist")
            node = node[int(part)]
        elif isinstance(node, dict):
            if part not in node:
                raise _PatchError("path does not exist")
            node = node[part]
        else:
            raise _PatchError("path does not exist")
    return node


def apply_patch(doc: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Cosmos patch semantics on a copy; raises _PatchError like a 400."""
    out = copy.deepcopy(doc)
    for operation in operations:
        op = operation.get("op")
        parts = _pointer(operation.get("path", ""))
        if parts[0] in {"id", "pk", "_rid", "_self", "_etag", "_ts", "_attachments"}:
            raise _PatchError(f"cannot patch {parts[0]}")
        parent = _parent(out, parts)
        key = parts[-1]
        value = copy.deepcopy(operation.get("value"))
        if isinstance(parent, list):
            if op == "add" and key == "-":
                parent.append(value)
                continue
            if not key.isdigit():
                raise _PatchError("array index expected")
            index = int(key)
            if op == "add":
                if index > len(parent):
                    raise _PatchError("index out of range")
                parent.insert(index, value)
            elif op in {"set", "replace"}:
                if index >= len(parent):
                    raise _PatchError("index out of range")
                parent[index] = value
            elif op == "remove":
                if index >= len(parent):
                    raise _PatchError("index out of range")
                del parent[index]
            elif op == "incr":
                if index >= len(parent) or not _is_number(parent[index]):
                    raise _PatchError("incr needs a number")
                parent[index] += value
            else:
                raise _PatchError(f"unsupported op {op!r}")
            continue
        if not isinstance(parent, dict):
            raise _PatchError("path does not exist")
        if op in {"add", "set"}:
            parent[key] = value
        elif op == "replace":
            if key not in parent:
                raise _PatchError("replace target does not exist")
            parent[key] = value
        elif op == "remove":
            if key not in parent:
                raise _PatchError("remove target does not exist")
            del parent[key]
        elif op == "incr":
            if not _is_number(value):
                raise _PatchError("incr value must be a number")
            if key not in parent:
                parent[key] = value
            elif not _is_number(parent[key]):
                raise _PatchError("incr needs a number")
            else:
                parent[key] += value
        else:
            raise _PatchError(f"unsupported op {op!r}")
    return out


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass
class Op:
    """One journal entry: an operation that was attempted, in order."""

    op: str
    container: str
    pk: str | None
    id: str | None
    ok: bool = True
    status: int | None = None
    sql: str | None = None


@dataclass
class _Rule:
    op: str
    container: str | None
    id: str | None
    pk: str | None
    times: int
    error: Exception | Callable[[], Exception] | None = None
    after: bool = False
    callback: Callable[["FakeCosmos"], None] | None = None

    def matches(self, op: str, container: str, pk: str | None, doc_id: str | None) -> bool:
        return (
            self.times > 0
            and self.op == op
            and (self.container is None or self.container == container)
            and (self.id is None or self.id == doc_id)
            and (self.pk is None or self.pk == pk)
        )


class FakeContainer:
    """One container. Its documents live in the owning FakeCosmos."""

    def __init__(self, account: "FakeCosmos", name: str, index_policy: Mapping[str, Any] | None):
        self.account = account
        self.name = name
        self.index_policy = index_policy
        self.docs: dict[tuple[str, str], dict[str, Any]] = {}

    # -- helpers ----------------------------------------------------------------
    def _stamp(self, body: dict[str, Any], *, rid: str | None = None) -> dict[str, Any]:
        doc = copy.deepcopy(body)
        # Increasing, so a partition reads back in insertion order, as Cosmos
        # returns an unordered single-partition query.
        doc["_rid"] = rid or f"{next(self.account._rids):016x}"
        doc["_self"] = f"dbs/fake/colls/{self.name}/docs/{doc['_rid']}/"
        doc["_etag"] = f'"{next(self.account._etags):08x}-0000-0000-0000-000000000000"'
        doc["_attachments"] = "attachments/"
        doc["_ts"] = int(self.account.clock())
        return doc

    @staticmethod
    def _key(body: Mapping[str, Any]) -> tuple[str, str]:
        if not isinstance(body.get("id"), str) or not body["id"]:
            raise bad_request("The input content is invalid because the required properties - 'id; ' - are missing")
        if "pk" not in body:
            raise bad_request("PartitionKey extracted from document doesn't match the one specified in the header.")
        return body["pk"], body["id"]

    def _respond(self, hook: Callable | None, charge: float, result: Any) -> None:
        if hook is not None:
            hook({"x-ms-request-charge": f"{charge:.2f}", "x-ms-activity-id": str(uuid.uuid4())}, result)

    def _run(self, op: str, pk: str | None, doc_id: str | None, fn: Callable[[], Any], *, sql_text: str | None = None) -> Any:
        account = self.account
        with account.lock:
            for rule in list(account.rules):
                if rule.callback is not None and rule.matches(op, self.name, pk, doc_id):
                    rule.times -= 1
                    # Released while the callback runs, so it can call back in.
                    account.lock.release()
                    try:
                        rule.callback(account)
                    finally:
                        account.lock.acquire()
            failure = account._take_failure(op, self.name, pk, doc_id, after=False)
            if failure is not None:
                account.journal.append(Op(op, self.name, pk, doc_id, ok=False, sql=sql_text))
                raise failure
            try:
                result = fn()
            except CosmosHttpResponseError as exc:
                account.journal.append(Op(op, self.name, pk, doc_id, ok=False, status=exc.status_code, sql=sql_text))
                raise
            except CosmosBatchOperationError as exc:
                account.journal.append(Op(op, self.name, pk, doc_id, ok=False, status=exc.status_code))
                raise
            account.journal.append(Op(op, self.name, pk, doc_id, ok=True, sql=sql_text))
            failure = account._take_failure(op, self.name, pk, doc_id, after=True)
            if failure is not None:
                raise failure
            return result

    # -- the ContainerProxy subset ------------------------------------------------
    def read_item(self, item: str, partition_key: str, *, response_hook: Callable | None = None, **_: Any) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            doc = self.docs.get((partition_key, item))
            if doc is None:
                raise not_found()
            result = copy.deepcopy(doc)
            self._respond(response_hook, CHARGES["read"], result)
            return result

        return self._run("read", partition_key, item, go)

    def create_item(self, body: dict[str, Any], *, response_hook: Callable | None = None, **_: Any) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            key = self._key(body)
            if key in self.docs:
                raise conflict()
            self.docs[key] = self._stamp(body)
            result = copy.deepcopy(self.docs[key])
            self._respond(response_hook, CHARGES["create"], result)
            return result

        return self._run("create", body.get("pk"), body.get("id"), go)

    def upsert_item(self, body: dict[str, Any], *, response_hook: Callable | None = None, **_: Any) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            key = self._key(body)
            existing = self.docs.get(key)
            self.docs[key] = self._stamp(body, rid=existing["_rid"] if existing else None)
            result = copy.deepcopy(self.docs[key])
            self._respond(response_hook, CHARGES["upsert"], result)
            return result

        return self._run("upsert", body.get("pk"), body.get("id"), go)

    def replace_item(
        self,
        item: str,
        body: dict[str, Any],
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
        response_hook: Callable | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            key = self._key(body)
            if key[1] != item:
                raise bad_request("The id in the body does not match the id of the item")
            existing = self.docs.get(key)
            if existing is None:
                raise not_found()
            if match_condition == MatchConditions.IfNotModified and existing["_etag"] != etag:
                raise precondition_failed()
            self.docs[key] = self._stamp(body, rid=existing["_rid"])
            result = copy.deepcopy(self.docs[key])
            self._respond(response_hook, CHARGES["replace"], result)
            return result

        return self._run("replace", body.get("pk"), item, go)

    def patch_item(
        self,
        item: str,
        partition_key: str,
        patch_operations: list[dict[str, Any]],
        *,
        filter_predicate: str | None = None,
        response_hook: Callable | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            key = (partition_key, item)
            existing = self.docs.get(key)
            if existing is None:
                raise not_found()
            if len(patch_operations) > 10:
                raise bad_request("Patch request has more than 10 operations")
            if filter_predicate is not None and not sql.matches(sql.parse_predicate(filter_predicate), existing):
                raise precondition_failed()
            try:
                patched = apply_patch(existing, patch_operations)
            except _PatchError as exc:
                raise bad_request(str(exc)) from None
            self.docs[key] = self._stamp(patched, rid=existing["_rid"])
            result = copy.deepcopy(self.docs[key])
            self._respond(response_hook, CHARGES["patch"], result)
            return result

        return self._run("patch", partition_key, item, go)

    def delete_item(self, item: str, partition_key: str, *, response_hook: Callable | None = None, **_: Any) -> None:
        def go() -> None:
            if self.docs.pop((partition_key, item), None) is None:
                raise not_found()
            self._respond(response_hook, CHARGES["delete"], None)

        return self._run("delete", partition_key, item, go)

    def query_items(
        self,
        query: str,
        parameters: list[dict[str, Any]] | None = None,
        *,
        partition_key: str | None = None,
        enable_cross_partition_query: bool | None = None,
        max_item_count: int | None = None,
        response_hook: Callable | None = None,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        if kwargs.get("enable_scan_in_query"):
            raise AssertionError("enable_scan_in_query is never used (docs/data-model.md section 6.2)")
        params = {p["name"]: p["value"] for p in (parameters or [])}
        parsed = sql.parse_query(query)
        self._check_index(parsed, query)

        def go() -> list[Any]:
            docs = [
                copy.deepcopy(doc)
                for (pk, _), doc in sorted(self.docs.items(), key=lambda kv: kv[1]["_rid"])
                if partition_key is None or pk == partition_key
            ]
            return sql.execute(parsed, docs, params)

        rows = self._run("query", partition_key, None, go, sql_text=query)
        page = max_item_count if max_item_count and max_item_count > 0 else 100

        def pages() -> Iterator[dict[str, Any]]:
            for start in range(0, max(len(rows), 1), page):
                chunk = rows[start : start + page]
                self._respond(response_hook, CHARGES["query_page"] + CHARGES["query_row"] * len(chunk), {"Documents": chunk})
                yield from chunk

        return pages()

    def execute_item_batch(
        self,
        batch_operations: list[tuple],
        partition_key: str,
        *,
        response_hook: Callable | None = None,
        **_: Any,
    ) -> list[dict[str, Any]]:
        def go() -> list[dict[str, Any]]:
            staged = {k: v for k, v in self.docs.items() if k[0] == partition_key}
            responses: list[dict[str, Any]] = []
            charge = 0.0
            for index, operation in enumerate(batch_operations):
                kind, args = operation[0].lower(), operation[1]
                kwargs = operation[2] if len(operation) > 2 else {}
                try:
                    status, body, cost = self._batch_op(staged, partition_key, kind, args, kwargs)
                except _BatchOpFailed as failed:
                    responses.append({"statusCode": failed.status})
                    responses.extend({"statusCode": 424} for _ in batch_operations[index + 1 :])
                    for earlier in responses[:index]:
                        earlier["statusCode"] = 424
                    raise CosmosBatchOperationError(
                        error_index=index,
                        headers={"x-ms-request-charge": f"{charge:.2f}"},
                        status_code=failed.status,
                        message=f"operation {index} failed",
                        operation_responses=responses,
                    ) from None
                charge += cost
                responses.append({"statusCode": status, "resourceBody": body, "requestCharge": cost})
            for key in [k for k in self.docs if k[0] == partition_key]:
                del self.docs[key]
            self.docs.update(staged)
            self._respond(response_hook, charge, responses)
            return responses

        return self._run("batch", partition_key, None, go)

    def _batch_op(self, staged: dict, pk: str, kind: str, args: tuple, kwargs: dict) -> tuple[int, Any, float]:
        if kind in {"create", "upsert"}:
            body = args[0]
            if body.get("pk") != pk:
                raise _BatchOpFailed(400)
            key = (pk, body["id"])
            if kind == "create" and key in staged:
                raise _BatchOpFailed(409)
            existing = staged.get(key)
            staged[key] = self._stamp(body, rid=existing["_rid"] if existing else None)
            return (201 if kind == "create" or existing is None else 200), copy.deepcopy(staged[key]), CHARGES[kind]
        if kind == "replace":
            doc_id, body = args
            key = (pk, doc_id)
            existing = staged.get(key)
            if existing is None:
                raise _BatchOpFailed(404)
            if kwargs.get("if_match_etag") is not None and existing["_etag"] != kwargs["if_match_etag"]:
                raise _BatchOpFailed(412)
            staged[key] = self._stamp(body, rid=existing["_rid"])
            return 200, copy.deepcopy(staged[key]), CHARGES["replace"]
        if kind == "patch":
            doc_id, operations = args
            key = (pk, doc_id)
            existing = staged.get(key)
            if existing is None:
                raise _BatchOpFailed(404)
            predicate = kwargs.get("filter_predicate")
            if predicate is not None and not sql.matches(sql.parse_predicate(predicate), existing):
                raise _BatchOpFailed(412)
            try:
                patched = apply_patch(existing, operations)
            except _PatchError:
                raise _BatchOpFailed(400) from None
            staged[key] = self._stamp(patched, rid=existing["_rid"])
            return 200, copy.deepcopy(staged[key]), CHARGES["patch"]
        if kind == "delete":
            key = (pk, args[0])
            if staged.pop(key, None) is None:
                raise _BatchOpFailed(404)
            return 204, None, CHARGES["delete"]
        if kind == "read":
            key = (pk, args[0])
            if key not in staged:
                raise _BatchOpFailed(404)
            return 200, copy.deepcopy(staged[key]), CHARGES["read"]
        raise _BatchOpFailed(400)

    def _check_index(self, parsed: sql.Query, text: str) -> None:
        """Opt-in indexing: Cosmos rejects a filter or sort on an excluded path
        instead of scanning (scans are never enabled here)."""
        if self.index_policy is None:
            return
        included = {
            entry["path"].strip("/").split("/")[0]
            for entry in self.index_policy.get("includedPaths", [])
        } | {"id", "_ts"}
        used = sql.referenced_paths(parsed.where) + [path for path, _ in parsed.order_by]
        for path in used:
            if path.root not in included:
                raise bad_request(f"path /{path.root} is excluded from indexing; query: {text}")
        if len(parsed.order_by) >= 2:
            wanted = [("/" + "/".join(str(p) for p in path.parts), direction) for path, direction in parsed.order_by]
            for composite in self.index_policy.get("compositeIndexes", []):
                same = [(e["path"], "ASC" if e.get("order", "ascending") == "ascending" else "DESC") for e in composite]
                inverted = [(p, "DESC" if d == "ASC" else "ASC") for p, d in same]
                if wanted in (same, inverted):
                    return
            raise bad_request(f"the ORDER BY needs a composite index: {text}")


class _BatchOpFailed(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


class FakeCosmos:
    """The account: containers by name, one journal, one set of fault rules."""

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.containers: dict[str, FakeContainer] = {}
        self.journal: list[Op] = []
        self.rules: list[_Rule] = []
        self.lock = threading.RLock()
        self._etags = itertools.count(1)
        self._rids = itertools.count(1)

    # What core.cosmos.get_container is replaced with.
    def get_container(self, name: str | None = None) -> FakeContainer:
        from core.config import get_settings

        settings = get_settings()
        resolved = name or settings.cosmos_container
        with self.lock:
            if resolved not in self.containers:
                # Only v2's container has opt-in indexing; v1's indexes everything.
                policy = news_index_policy() if resolved == settings.cosmos_container else None
                self.containers[resolved] = FakeContainer(self, resolved, policy)
            return self.containers[resolved]

    # -- seeding and inspection -------------------------------------------------
    def put(self, doc: dict[str, Any], *, container: str | None = None) -> dict[str, Any]:
        """Store a document directly (no journal entry, no rules)."""
        target = self.get_container(container)
        key = target._key(doc)
        existing = target.docs.get(key)
        target.docs[key] = target._stamp(doc, rid=existing["_rid"] if existing else None)
        return copy.deepcopy(target.docs[key])

    def get(self, doc_id: str, pk: str, *, container: str | None = None) -> dict[str, Any] | None:
        doc = self.get_container(container).docs.get((pk, doc_id))
        return copy.deepcopy(doc) if doc is not None else None

    def docs(self, pk: str | None = None, *, container: str | None = None) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(doc)
            for (doc_pk, _), doc in self.get_container(container).docs.items()
            if pk is None or doc_pk == pk
        ]

    def writes(self) -> list[Op]:
        """Successful writes, in order."""
        return [op for op in self.journal if op.ok and op.op in {"create", "upsert", "replace", "patch", "delete", "batch"}]

    def queries(self) -> list[Op]:
        return [op for op in self.journal if op.op == "query"]

    # -- fault injection --------------------------------------------------------
    def fail(
        self,
        op: str,
        *,
        id: str | None = None,
        pk: str | None = None,
        container: str | None = None,
        error: Exception | Callable[[], Exception] | None = None,
        after: bool = False,
        times: int = 1,
    ) -> None:
        """Raise ``error`` (default: a transport failure) on the next ``times``
        matching operations. ``after=True`` applies the operation first."""
        self.rules.append(_Rule(op, container, id, pk, times, error=error, after=after))

    def before(
        self,
        op: str,
        callback: Callable[["FakeCosmos"], None],
        *,
        id: str | None = None,
        pk: str | None = None,
        container: str | None = None,
        times: int = 1,
    ) -> None:
        """Run ``callback`` just before the next matching operation: the way to
        stage a concurrent request between a read and its conditional write."""
        self.rules.append(_Rule(op, container, id, pk, times, callback=callback))

    def _take_failure(self, op: str, container: str, pk: str | None, doc_id: str | None, *, after: bool) -> Exception | None:
        for rule in self.rules:
            if rule.callback is None and rule.after == after and rule.matches(op, container, pk, doc_id):
                rule.times -= 1
                error = rule.error
                if error is None:
                    from azure.core.exceptions import ServiceResponseError

                    return ServiceResponseError("injected transport failure")
                return error() if callable(error) and not isinstance(error, BaseException) else error
        return None
