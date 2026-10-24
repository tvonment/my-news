"""The wire contract (docs/api.md section 1): status semantics, the error
registry, headers, body limits, and every Phase 1 response checked against its
JSON Schema in docs/api/schemas.

Schemas are checked with jsonschema when it is installed, and always with the
small draft 2020-12 validator below. jsonschema never ships (like pytest it is
not in requirements.txt) and CI installs only the lock, so without the fallback
every schema check would skip in CI. The fallback covers exactly the keywords
the schema files use and refuses any other, so a schema that starts using one
fails loudly here instead of being checked by half; where both validators run,
they must agree on every body.

Every route call in this module goes through ``Client.call``, which checks the
rules that hold for every response:

* ``X-Request-Id`` on every response, errors included;
* ``Cache-Control: no-store`` (no Phase 1 route is ETag-cached) and
  ``Content-Type: application/json; charset=utf-8`` on every body;
* an error is the envelope, its code is registered with exactly the status
  sent, 401 carries ``WWW-Authenticate``, 429 and 503 carry an integer
  ``Retry-After``, and 410 is only ``account_deleted``;
* a success body matches the response schema the route index names.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, NamedTuple
from urllib.parse import urldefrag, urljoin

import azure.functions as func
import jwt
import pytest

from api import http
from core import auth, invites
from core.errors import ERROR_STATUS, NOT_FOUND_RESOURCES, VALIDATION_REASONS, ApiError
from core.models import iso, utcnow
from tests.fakes import jwks as ids
from tests.fakes.cosmos import FakeCosmos, throttled
from tests.test_function_app import indexed_functions

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
API_MD = ROOT / "docs" / "api.md"
SCHEMA_DIR = ROOT / "docs" / "api" / "schemas"
ADMIN_UID = "f" * 32

P1_SCHEMAS = (
    "Common",
    "Error",
    "Health",
    "Me",
    "MePatch",
    "Redeem",
    "DeleteAccepted",
    "Export",
    "Invite",
    "InviteList",
    "InviteCreateRequest",
    "InviteCreated",
    "AdminUser",
    "AdminUserList",
    "AdminBlockRequest",
    "AdminDeletion",
    "AdminDeletionList",
    "AdminOverview",
    "AdminHealth",
)


# ===========================================================================
# A JSON Schema (draft 2020-12) subset validator
# ===========================================================================
_ANNOTATIONS = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "$defs",
        "title",
        "description",
        "examples",
        "default",
        "x-phase",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)
_KEYWORDS = frozenset(
    {
        "$ref",
        "type",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "uniqueItems",
        "properties",
        "patternProperties",
        "additionalProperties",
        "required",
        "propertyNames",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "unevaluatedProperties",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
    }
)
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


def _canonical(value: Any) -> Any:
    """JSON equality: true is not 1, 1 is 1.0, objects ignore key order."""
    if isinstance(value, bool) or value is None:
        return ("literal", value)
    if isinstance(value, (int, float)):
        return ("number", float(value))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_canonical(item) for item in value))
    if isinstance(value, dict):
        return ("object", tuple(sorted((key, _canonical(item)) for key, item in value.items())))
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def _is_type(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer()
        )
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    raise NotImplementedError(f"type {name!r}")


def _format_ok(value: str, name: str) -> bool:
    if name == "date-time":
        if not _RFC3339.match(value):
            return False
        try:
            datetime.fromisoformat(value.replace("z", "Z").replace("t", "T").replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
    if name == "date":
        try:
            return re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None and date.fromisoformat(value) is not None
        except ValueError:
            return False
    if name == "uri":
        return re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:\S+$", value) is not None
    raise NotImplementedError(f"format {name!r}")


class MiniValidator:
    """Validates instances against the schema files, resolving ``$ref`` by each
    file's ``$id``. ``errors()`` lists every violation as ``path: message``."""

    def __init__(self, documents: dict[str, dict[str, Any]]):
        self.by_id = {doc["$id"]: doc for doc in documents.values()}
        self.by_name = documents

    def errors(self, name: str, instance: Any) -> list[str]:
        document = self.by_name[name]
        problems, _ = self._check(document, instance, document["$id"], "(root)")
        return problems

    def _resolve(self, ref: str, base: str) -> tuple[Any, str]:
        url, fragment = urldefrag(urljoin(base, ref))
        node: Any = self.by_id[url]
        for part in [p for p in fragment.split("/") if p]:
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node, url

    def _check(self, schema: Any, value: Any, base: str, path: str) -> tuple[list[str], set[str]]:
        """(problems, the property names this schema evaluated), the second for
        unevaluatedProperties, which sees through in-place applicators."""
        if schema is True:
            return [], set()
        if schema is False:
            return [f"{path}: no value is allowed here"], set()
        unknown = set(schema) - _KEYWORDS - _ANNOTATIONS
        if unknown:
            raise NotImplementedError(f"the test validator does not implement {sorted(unknown)}")
        problems: list[str] = []
        evaluated: set[str] = set()

        def sub(subschema: Any, subvalue: Any, subpath: str) -> tuple[list[str], set[str]]:
            return self._check(subschema, subvalue, base, subpath)

        if "$ref" in schema:
            target, target_base = self._resolve(schema["$ref"], base)
            found, seen = self._check(target, value, target_base, path)
            problems += found
            if not found:
                evaluated |= seen
        if "type" in schema:
            names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_is_type(value, name) for name in names):
                problems.append(f"{path}: {value!r} is not of type {names}")
        if "enum" in schema and _canonical(value) not in {_canonical(v) for v in schema["enum"]}:
            problems.append(f"{path}: {value!r} is not one of {schema['enum']}")
        if "const" in schema and _canonical(value) != _canonical(schema["const"]):
            problems.append(f"{path}: {value!r} is not {schema['const']!r}")

        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                problems.append(f"{path}: shorter than {schema['minLength']}")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                problems.append(f"{path}: longer than {schema['maxLength']}")
            if "pattern" in schema and re.search(schema["pattern"], value) is None:
                problems.append(f"{path}: {value!r} does not match {schema['pattern']}")
            if "format" in schema and not _format_ok(value, schema["format"]):
                problems.append(f"{path}: {value!r} is not a {schema['format']}")
        if _is_type(value, "number"):
            if "minimum" in schema and value < schema["minimum"]:
                problems.append(f"{path}: below {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                problems.append(f"{path}: above {schema['maximum']}")
            if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
                problems.append(f"{path}: not above {schema['exclusiveMinimum']}")
            if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
                problems.append(f"{path}: not below {schema['exclusiveMaximum']}")
            if "multipleOf" in schema and (value / schema["multipleOf"]) % 1:
                problems.append(f"{path}: not a multiple of {schema['multipleOf']}")
        if isinstance(value, list):
            prefix = schema.get("prefixItems", [])
            for index, item in enumerate(value):
                if index < len(prefix):
                    problems += sub(prefix[index], item, f"{path}/{index}")[0]
                elif "items" in schema:
                    problems += sub(schema["items"], item, f"{path}/{index}")[0]
            if "minItems" in schema and len(value) < schema["minItems"]:
                problems.append(f"{path}: fewer than {schema['minItems']} items")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                problems.append(f"{path}: more than {schema['maxItems']} items")
            if schema.get("uniqueItems") and len({_canonical(item) for item in value}) != len(value):
                problems.append(f"{path}: items are not unique")
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            for key, item in value.items():
                matched = False
                if key in properties:
                    matched = True
                    problems += sub(properties[key], item, f"{path}/{key}")[0]
                for pattern, pattern_schema in schema.get("patternProperties", {}).items():
                    if re.search(pattern, key):
                        matched = True
                        problems += sub(pattern_schema, item, f"{path}/{key}")[0]
                if matched:
                    evaluated.add(key)
                elif "additionalProperties" in schema:
                    evaluated.add(key)
                    if schema["additionalProperties"] is False:
                        problems.append(f"{path}: unexpected property {key!r}")
                    else:
                        problems += sub(schema["additionalProperties"], item, f"{path}/{key}")[0]
                if "propertyNames" in schema:
                    problems += sub(schema["propertyNames"], key, f"{path}/{key} (name)")[0]
            for key in schema.get("required", []):
                if key not in value:
                    problems.append(f"{path}: {key!r} is required")
            if "minProperties" in schema and len(value) < schema["minProperties"]:
                problems.append(f"{path}: fewer than {schema['minProperties']} properties")
            if "maxProperties" in schema and len(value) > schema["maxProperties"]:
                problems.append(f"{path}: more than {schema['maxProperties']} properties")
            for key, needed in schema.get("dependentRequired", {}).items():
                if key in value:
                    problems += [f"{path}: {key!r} requires {other!r}" for other in needed if other not in value]

        for subschema in schema.get("allOf", []):
            found, seen = sub(subschema, value, path)
            problems += found
            if not found:
                evaluated |= seen
        if "anyOf" in schema:
            results = [sub(subschema, value, path) for subschema in schema["anyOf"]]
            passing = [seen for found, seen in results if not found]
            if not passing:
                problems.append(
                    f"{path}: matches none of anyOf: " + " | ".join("; ".join(found) for found, _ in results)
                )
            for seen in passing:
                evaluated |= seen
        if "oneOf" in schema:
            results = [sub(subschema, value, path) for subschema in schema["oneOf"]]
            passing = [seen for found, seen in results if not found]
            if len(passing) != 1:
                problems.append(f"{path}: matches {len(passing)} of oneOf, not exactly one")
            for seen in passing:
                evaluated |= seen
        if "not" in schema and not sub(schema["not"], value, path)[0]:
            problems.append(f"{path}: must not match {schema['not']}")
        if "if" in schema:
            failed_if, seen_if = sub(schema["if"], value, path)
            branch = "then" if not failed_if else "else"
            if branch in schema:
                found, seen = sub(schema[branch], value, path)
                problems += found
                if not found:
                    evaluated |= seen
            if not failed_if:
                evaluated |= seen_if

        if "unevaluatedProperties" in schema and isinstance(value, dict):
            for key in sorted(set(value) - evaluated):
                if schema["unevaluatedProperties"] is False:
                    problems.append(f"{path}: unevaluated property {key!r}")
                else:
                    problems += sub(schema["unevaluatedProperties"], value[key], f"{path}/{key}")[0]
            evaluated |= set(value)
        return problems, evaluated


@functools.cache
def _schema_documents() -> dict[str, dict[str, Any]]:
    return {
        path.name.removesuffix(".schema.json"): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    }


@functools.cache
def _validators() -> tuple[MiniValidator, Callable[[str, Any], list[str]] | None]:
    documents = _schema_documents()
    mini = MiniValidator(documents)
    try:
        import jsonschema
        import referencing
        from referencing.jsonschema import DRAFT202012
    except ImportError:
        return mini, None
    registry = referencing.Registry().with_resources(
        (doc["$id"], referencing.Resource.from_contents(doc, default_specification=DRAFT202012))
        for doc in documents.values()
    )
    checkers = {
        name: jsonschema.Draft202012Validator(doc, registry=registry, format_checker=jsonschema.FormatChecker())
        for name, doc in documents.items()
    }

    def full(name: str, instance: Any) -> list[str]:
        return [
            f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"
            for e in checkers[name].iter_errors(instance)
        ]

    return mini, full


def schema_problems(name: str, instance: Any) -> list[str]:
    """The violations of ``instance`` against schema ``name``; where jsonschema
    is installed, both validators must reach the same verdict."""
    mini, full = _validators()
    problems = mini.errors(name, instance)
    if full is not None:
        reference = full(name, instance)
        assert bool(problems) == bool(reference), (
            f"validators disagree on {name}: test={problems} jsonschema={reference}"
        )
    return problems


def assert_schema(name: str, instance: Any) -> None:
    problems = schema_problems(name, instance)
    assert not problems, f"{name}:\n  " + "\n  ".join(problems)


# ===========================================================================
# Calling routes the way the host does
# ===========================================================================
NO_BODY = object()


class Out:
    """An output binding as the worker passes it: set() once, get() back."""

    def __init__(self) -> None:
        self.values: list[Any] = []

    def set(self, value: Any) -> None:
        self.values.append(value)

    def get(self) -> Any:
        return self.values[-1] if self.values else None


class Resp(NamedTuple):
    status: int
    headers: dict[str, str]
    body: Any
    raw: bytes

    @property
    def code(self) -> str | None:
        return self.body["error"]["code"] if self.status >= 400 else None


def documented_response_schemas() -> dict[str, str]:
    """function name -> the response schema docs/api.md section 5 names for
    it, for every registered (P0 and P1) row."""
    text = API_MD.read_text(encoding="utf-8")
    section = text[text.index("## 5. Route index") : text.index("## 6. Routes")]
    schemas: dict[str, str] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 6 or cells[0] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            continue
        phase, name, shapes = cells[3].replace("*", ""), cells[4].strip("`"), cells[5]
        if phase.split()[0] not in ("P0", "P1"):
            continue
        response = shapes.split("→")[-1]
        (schema,) = re.findall(r"`([A-Za-z]+)`", response)
        schemas[name] = schema
    return schemas


RESPONSE_SCHEMAS = documented_response_schemas()


def check_contract(name: str, resp: Resp) -> None:
    """The rules every response follows (docs/api.md sections 1.3-1.6)."""
    request_id = resp.headers.get("x-request-id")
    assert request_id and http.REQUEST_ID_RE.fullmatch(request_id), (name, resp.headers)
    assert resp.headers.get("cache-control") == "no-store", (name, resp.headers)
    assert "etag" not in resp.headers, name
    if resp.raw:
        assert resp.headers.get("content-type") == "application/json; charset=utf-8", name
    if resp.status >= 400:
        assert_schema("Error", resp.body)
        code = resp.body["error"]["code"]
        assert ERROR_STATUS[code] == resp.status, (name, code, resp.status)
        if resp.status == 410:
            assert code == "account_deleted"
        if resp.status == 401:
            expected = 'Bearer realm="mynews"' + (', error="invalid_token"' if code == "invalid_token" else "")
            assert resp.headers.get("www-authenticate") == expected, name
        else:
            assert "www-authenticate" not in resp.headers, name
        if resp.status in (429, 503):
            assert resp.headers.get("retry-after", "").isdigit(), (name, resp.headers)
        else:
            assert "retry-after" not in resp.headers, name
    else:
        assert_schema(RESPONSE_SCHEMAS[name], resp.body)


class Client:
    """Calls the indexed functions with requests shaped like the host's, and
    checks every response against the contract."""

    def __init__(self, jwks: Any):
        self.jwks = jwks
        self.functions = indexed_functions()
        self.seen: list[tuple[str, int]] = []
        self.outputs: dict[str, Out] = {}

    def call(
        self,
        name: str,
        *,
        oid: str | None = ids.OID_ALICE,
        roles: tuple[str, ...] = (),
        headers: dict[str, str] | None = None,
        body: Any = NO_BODY,
        raw: bytes | None = None,
        params: dict[str, str] | None = None,
        route_params: dict[str, str] | None = None,
    ) -> Resp:
        fn = self.functions[name]
        trigger = fn.get_trigger()
        request_headers: dict[str, str] = {}
        if oid is not None:
            request_headers.update(self.jwks.bearer(oid, roles=list(roles)))
        data = b""
        if raw is not None:
            data = raw
        elif body is not NO_BODY:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        route = trigger.route
        for key, value in (route_params or {}).items():
            route = route.replace("{" + key + "}", value)
        req = func.HttpRequest(
            method=trigger.methods[0].value,
            url=f"http://localhost/api/{route}",
            headers=request_headers,
            params=params or {},
            route_params=route_params or {},
            body=data,
        )
        kwargs: dict[str, Any] = {"req": req}
        self.outputs = {}
        for binding in fn.get_bindings():
            if binding.type == "queue":
                kwargs[binding.name] = self.outputs.setdefault(binding.name, Out())
        response = fn.get_user_function()(**kwargs)
        raw_body = response.get_body() or b""
        resp = Resp(
            response.status_code,
            {key.lower(): value for key, value in response.headers.items()},
            json.loads(raw_body) if raw_body else None,
            raw_body,
        )
        check_contract(name, resp)
        self.seen.append((name, resp.status))
        return resp


@pytest.fixture
def client(jwks: Any, fake_cosmos: FakeCosmos) -> Client:
    return Client(jwks)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # The purge is paced; its sleeps are recorded by core.cosmos, never taken.
    from core import cosmos

    monkeypatch.setattr(cosmos.time, "sleep", lambda seconds: None)


def _mint(*, max_uses: int = 1, days: int = 30) -> invites.MintedInvite:
    return invites.mint(
        label="Wave", max_uses=max_uses, expires_in_days=days, count=1, actor_uid=ADMIN_UID, now=utcnow()
    )[0]


def _join(client: Client, oid: str) -> Resp:
    resp = client.call("me_redeem", oid=oid, body={"code": _mint().code})
    assert resp.status == 201, resp
    return resp


ADMIN = {"oid": ids.OID_ADMIN, "roles": ("admin",)}


# ===========================================================================
# The registry
# ===========================================================================
class TestRegistry:
    def test_the_registry_is_the_documented_table(self) -> None:
        text = API_MD.read_text(encoding="utf-8")
        section = text[text.index("### 1.4") : text.index("### 1.5")]
        documented = {
            code: int(status) for status, code in re.findall(r"^\|\s*(\d{3})\s*\|\s*`([a-z_]+)`\s*\|", section, re.M)
        }
        assert documented == ERROR_STATUS

    def test_410_is_only_account_deleted(self) -> None:
        assert [code for code, status in ERROR_STATUS.items() if status == 410] == ["account_deleted"]

    def test_phase_1_bodies_are_checked_against_final_schemas(self) -> None:
        # The route index names a schema for every registered route, and each
        # is a Phase 0/1 file (final), not a later phase's draft.
        documents = _schema_documents()
        assert set(RESPONSE_SCHEMAS.values()) | {"Error"} <= set(P1_SCHEMAS)
        for name in P1_SCHEMAS:
            assert documents[name]["x-phase"] in ("P0", "P1"), name

    def test_the_schema_knows_exactly_the_registered_codes(self) -> None:
        common = _schema_documents()["Common"]
        assert set(common["$defs"]["ErrorCode"]["enum"]) == set(ERROR_STATUS)
        assert set(common["$defs"]["ValidationReason"]["enum"]) == set(VALIDATION_REASONS)

    def test_every_error_raised_in_the_code_is_registered(self) -> None:
        # Static, so a branch no test reaches cannot hide an unregistered code,
        # a reason outside the list or a resource the schema does not know.
        sources = {
            path: path.read_text(encoding="utf-8")
            for d in ("api", "core", "jobs")
            for path in (BACKEND / d).glob("*.py")
        }
        checks = [
            (r'ApiError\(\s*"([a-z_]+)"', set(ERROR_STATUS)),
            (r'\binvalid\(\s*"[^"]*",\s*"([a-z_]+)"\)', set(VALIDATION_REASONS)),
            (r'\binvalid_body\(\s*"([a-z_]+)"\)', set(VALIDATION_REASONS)),
            (r'\bnot_found\(\s*"([a-z_]+)"\)', set(NOT_FOUND_RESOURCES)),
            (r'\bdegraded\(\s*"([a-z_]+)"', {"database", "queue", "pool", "model"}),
        ]
        found = 0
        for pattern, allowed in checks:
            for path, text in sources.items():
                for value in re.findall(pattern, text):
                    found += 1
                    assert value in allowed, f"{path.name}: {value!r} is not registered"
        assert found > 40  # the scan sees the code it guards

    def test_handlers_never_build_a_response_themselves(self) -> None:
        # One place sets the headers clients rely on: api/http.py. A handler
        # that built its own HttpResponse could forget Cache-Control, or send a
        # status the registry does not fix.
        for path in [*(BACKEND / "api").glob("*.py"), *(BACKEND / "jobs").glob("*.py")]:
            if path.name == "http.py":
                continue
            text = path.read_text(encoding="utf-8")
            assert "HttpResponse(" not in text and "status_code" not in text, path.name


# ===========================================================================
# api/http.py
# ===========================================================================
def _req(headers: dict[str, str] | None = None, body: bytes = b"", **kwargs: Any) -> func.HttpRequest:
    return func.HttpRequest(method="POST", url="http://localhost/api/x", headers=headers or {}, body=body, **kwargs)


def _json_req(body: bytes, content_type: str | None = "application/json", **headers: str) -> func.HttpRequest:
    all_headers = dict(headers)
    if content_type is not None:
        all_headers["Content-Type"] = content_type
    return _req(all_headers, body)


def _raised(fn: Callable[..., Any], *args: Any) -> ApiError:
    with pytest.raises(ApiError) as caught:
        fn(*args)
    return caught.value


class TestHelpers:
    @pytest.mark.parametrize("value", ["req-12345678", "a" * 64, "2026.10.21:x_y-z"])
    def test_a_usable_request_id_is_echoed(self, value: str) -> None:
        assert http.request_id_for(_req({"X-Request-Id": value})) == value

    @pytest.mark.parametrize("value", ["", "short", "a" * 65, "has space12", "comma,separated,ids", "line\nbreak123"])
    def test_anything_else_gets_a_fresh_uuid(self, value: str) -> None:
        issued = http.request_id_for(_req({"X-Request-Id": value}))
        assert issued != value and re.fullmatch(r"[0-9a-f-]{36}", issued)

    def test_json_bodies(self) -> None:
        resp = http.json_response({"text": "Zürich ✓", "n": 1}, 201, headers={"X-Extra": "1"})
        assert resp.status_code == 201
        assert resp.get_body() == '{"text":"Zürich ✓","n":1}'.encode()
        assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["X-Extra"] == "1"

    @pytest.mark.parametrize("payload", [{"at": datetime(2026, 10, 21)}, {"x": float("nan")}, {"s": {1, 2}}])
    def test_values_json_cannot_carry_fail_instead_of_being_stringified(self, payload: Any) -> None:
        with pytest.raises((TypeError, ValueError)):
            http.dumps(payload)

    def test_errors_carry_their_status_and_headers(self) -> None:
        cases = [
            (ApiError("unauthenticated"), {"WWW-Authenticate": 'Bearer realm="mynews"'}),
            (ApiError("invalid_token"), {"WWW-Authenticate": 'Bearer realm="mynews", error="invalid_token"'}),
            (ApiError("quota_exceeded", retry_after=3600), {"Retry-After": "3600"}),
            (ApiError("degraded"), {"Retry-After": "5"}),
            (ApiError("auth_unavailable", retry_after=60), {"Retry-After": "60"}),
            (ApiError("not_found", details={"resource": "story"}), {}),
        ]
        for error, expected in cases:
            resp = http.error_response(error)
            assert resp.status_code == error.status
            assert json.loads(resp.get_body()) == error.body()
            for header, value in expected.items():
                assert resp.headers[header] == value
            assert ("Retry-After" in resp.headers) == (error.status in (429, 503))
            assert ("WWW-Authenticate" in resp.headers) == (error.status == 401)
            assert resp.headers["Cache-Control"] == "no-store"

    def test_validation_errors_are_bounded_where_they_are_rendered(self) -> None:
        fields = [{"field": "x" * 300, "reason": "unknown_field"}] * 70
        error = ApiError("validation_failed", details={"fields": fields})
        rendered = json.loads(http.error_response(error).get_body())["error"]["details"]["fields"]
        assert len(rendered) == 50 and {len(entry["field"]) for entry in rendered} == {120}
        assert len(error.details["fields"]) == 70  # the raised error itself is left alone

    def test_an_error_may_add_headers(self) -> None:
        resp = http.error_response(ApiError("not_found", details={"resource": "story"}, headers={"X-Hint": "1"}))
        assert resp.headers["X-Hint"] == "1"

    def test_with_request_id_sets_the_header(self) -> None:
        resp = http.with_request_id(http.no_content(), "req-abcdefgh")
        assert resp.status_code == 204 and resp.get_body() == b""
        assert resp.headers["X-Request-Id"] == "req-abcdefgh"
        assert resp.headers["Cache-Control"] == "no-store"

    def test_attachments(self) -> None:
        resp = http.attachment_response({"a": 1}, "mynews-export-2026-10-21.json")
        assert resp.headers["Content-Disposition"] == 'attachment; filename="mynews-export-2026-10-21.json"'
        assert resp.headers["Cache-Control"] == "no-store"
        with pytest.raises(ValueError):
            http.attachment_response({}, 'evil".json')

    def test_etags_and_304(self) -> None:
        payload = {"brief": [1, 2, 3]}
        first = http.etag_response(_req(), payload)
        etag = first.headers["ETag"]
        assert first.status_code == 200 and re.fullmatch(r'"[0-9a-f]{32}"', etag)
        assert first.headers["Cache-Control"] == "private, no-cache"
        for header in (etag, f"W/{etag}", f'"other", {etag}', "*"):
            resp = http.etag_response(_req({"If-None-Match": header}), payload)
            assert (resp.status_code, resp.get_body()) == (304, b"")
            assert resp.headers["ETag"] == etag and resp.headers["Cache-Control"] == "private, no-cache"
        changed = http.etag_response(_req({"If-None-Match": etag}), {"brief": [1, 2]})
        assert changed.status_code == 200 and changed.headers["ETag"] != etag

    @pytest.mark.parametrize(
        "content_type",
        [
            "application/json",
            "application/json; charset=utf-8",
            "Application/JSON;charset=UTF-8",
            'application/json; charset="utf-8"',
        ],
    )
    def test_json_content_types(self, content_type: str) -> None:
        assert http.read_json(_json_req(b'{"a": 1}', content_type)) == {"a": 1}

    @pytest.mark.parametrize(
        "content_type",
        [
            None,
            "",
            "text/plain",
            "application/json; charset=latin-1",
            "application/merge-patch+json",
            "application/json, text/plain",
        ],
    )
    def test_anything_else_is_415(self, content_type: str | None) -> None:
        error = _raised(lambda: http.read_json(_json_req(b'{"a": 1}', content_type)))
        assert (error.status, error.details) == (415, {"expected": "application/json"})

    def test_the_type_is_checked_before_the_size(self) -> None:
        assert _raised(lambda: http.read_json(_json_req(b"x" * 70000, None))).status == 415

    def test_64_kb_is_the_limit_by_bytes_and_by_content_length(self) -> None:
        fits = b'{"a":"' + b"x" * (http.MAX_BODY_BYTES - 8) + b'"}'
        assert len(fits) == http.MAX_BODY_BYTES
        assert http.read_json(_json_req(fits))["a"].startswith("x")
        for request in (_json_req(fits + b" "), _json_req(b"{}", **{"Content-Length": str(http.MAX_BODY_BYTES + 1)})):
            error = _raised(http.read_json, request)
            assert (error.status, error.details) == (413, {"limitBytes": 65536})

    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            (b"", "invalid_json"),
            (b"{", "invalid_json"),
            (b"\xff\xfe{}", "invalid_json"),
            ("﻿{}".encode(), "invalid_json"),
            (b'{"a": NaN}', "invalid_json"),
            (b'{"blocked": false, "blocked": true}', "invalid_json"),
            (b"[" * 50000, "invalid_json"),
            (b"[]", "not_object"),
            (b'"text"', "not_object"),
            (b"null", "not_object"),
        ],
    )
    def test_bodies_that_are_not_one_json_object(self, body: bytes, reason: str) -> None:
        error = _raised(lambda: http.read_json(_json_req(body)))
        assert (error.code, error.details) == ("validation_failed", {"reason": reason})

    def test_query_parameters(self) -> None:
        req = _req(params={"status": " active ", "empty": "  "})
        assert http.query_param(req, "status") == "active"
        assert http.query_param(req, "empty") is None
        assert http.query_param(req, "missing") is None

    def test_path_ids_follow_their_grammar_or_are_404(self) -> None:
        from core.models import UID_RE

        good = "a" * 32
        assert http.path_param(_req(route_params={"uid": good}), "uid", UID_RE, "user") == good
        for bad in ("A" * 32, "a" * 31, good + "\n", "../" + good, ids.OID_BOB):
            error = _raised(http.path_param, _req(route_params={"uid": bad}), "uid", UID_RE, "user")
            assert (error.status, error.details) == (404, {"resource": "user"})
        assert _raised(lambda: http.path_param(_req(), "uid", UID_RE, "user")).status == 404

    def test_unknown_fields_are_named(self) -> None:
        error = _raised(lambda: http.reject_unknown_fields({"code": 1, "zz": 2, "x" * 500: 3}, frozenset({"code"})))
        assert error.details == {
            "fields": [{"field": "x" * 120, "reason": "unknown_field"}, {"field": "zz", "reason": "unknown_field"}]
        }
        http.reject_unknown_fields({"code": 1}, frozenset({"code"}))


# ===========================================================================
# The validator itself
# ===========================================================================
class TestValidator:
    def test_every_schema_example_validates(self) -> None:
        checked = 0
        for name, document in _schema_documents().items():
            for example in document.get("examples", []):
                assert_schema(name, example)
                checked += 1
        assert checked >= 30

    def test_every_schema_uses_only_implemented_keywords(self) -> None:
        # Every subschema of every file, not only the ones a body happens to
        # reach: a keyword the validator does not implement fails here, before
        # any body is checked by half.
        maps_of_schemas = {"properties", "patternProperties", "$defs", "dependentSchemas"}
        single_schemas = {
            "items",
            "not",
            "if",
            "then",
            "else",
            "additionalProperties",
            "unevaluatedProperties",
            "propertyNames",
        }
        lists_of_schemas = {"allOf", "anyOf", "oneOf", "prefixItems"}
        seen: set[str] = set()

        def walk(node: Any, where: str) -> None:
            if isinstance(node, bool):
                return
            assert isinstance(node, dict), where
            for key, value in node.items():
                seen.add(key)
                assert key in _KEYWORDS | _ANNOTATIONS, f"{where}: {key!r} is not implemented by the test validator"
                if key in maps_of_schemas:
                    for name, item in value.items():
                        walk(item, f"{where}/{key}/{name}")
                elif key in single_schemas:
                    walk(value, f"{where}/{key}")
                elif key in lists_of_schemas:
                    for index, item in enumerate(value):
                        walk(item, f"{where}/{key}/{index}")

        for name, document in _schema_documents().items():
            walk(document, name)
        assert {"$ref", "unevaluatedProperties", "if", "anyOf", "propertyNames"} <= seen

    @pytest.mark.parametrize(
        ("name", "body"),
        [
            ("Health", {"status": "ok", "api": 1, "minBuild": 0, "time": "2026-10-21T10:00:00Z", "extra": 1}),
            ("Health", {"status": "ok", "api": 1, "minBuild": 0, "time": "2026-10-21T10:00:00.5Z"}),
            ("Health", {"status": "ok", "api": 1, "minBuild": True, "time": "2026-10-21T10:00:00Z"}),
            ("Health", {"status": "ok", "api": 1, "time": "2026-10-21T10:00:00Z"}),
            ("Error", {"error": {"code": "teapot", "message": "x", "details": {}}}),
            ("Error", {"error": {"code": "not_found", "message": "x", "details": {}}}),
            ("Error", {"error": {"code": "invite_required", "message": "x", "details": {"oid": "x"}}}),
            (
                "Error",
                {
                    "error": {
                        "code": "validation_failed",
                        "message": "x",
                        "details": {"fields": [{"field": "a", "reason": "camelCase"}]},
                    }
                },
            ),
            ("DeleteAccepted", {"state": "deleted", "identityRemoval": "manual"}),
            (
                "AdminUser",
                {
                    "uid": "a" * 32,
                    "status": "active",
                    "admin": False,
                    "createdAt": "2026-10-21T10:00:00Z",
                    "lastActiveAt": None,
                    "inviteLabel": None,
                    "locale": "en",
                    "languages": ["en", "en"],
                    "blockedAt": None,
                },
            ),
        ],
    )
    def test_bodies_that_break_the_contract_fail(self, name: str, body: Any) -> None:
        assert schema_problems(name, body)

    def test_export_documents_cannot_carry_system_properties(self) -> None:
        example = _schema_documents()["Export"]["examples"][0]
        for leaked in ("_etag", "pk", "_ts"):
            body = json.loads(json.dumps(example))
            body["documents"][0][leaked] = "x"
            assert schema_problems("Export", body), leaked


# ===========================================================================
# Every Phase 1 route, end to end, through the contract checks
# ===========================================================================
def test_every_phase_1_route_answers_its_documented_shapes(client: Client, fake_cosmos: FakeCosmos) -> None:
    """One account's whole life, every answer checked by Client.call. The last
    assertion is that every registered route was seen succeeding and failing."""
    from core.accounts import purge_account
    from core.jobs import PurgeMessage

    assert client.call("health", oid=None).status == 200
    # Before membership.
    assert client.call("me_get").code == "invite_required"
    assert client.call("me_patch", body={"locale": "en"}).code == "invite_required"
    assert client.call("me_delete").code == "invite_required"
    assert client.call("me_export").code == "invite_required"
    assert client.call("me_redeem", body={"code": "nope"}).code == "invite_malformed"
    assert client.call("me_redeem", body={}).code == "validation_failed"
    assert client.call("me_redeem", body={"code": "0000-0000-0000"}).code == "invite_not_found"
    # The admin bootstraps on the first admin call and mints a code.
    created = client.call("admin_invites_create", **ADMIN, body={"label": "Family wave 1", "count": 2})
    assert created.status == 201 and len(created.body["invites"]) == 2
    assert client.call("admin_invites_create", **ADMIN, body={"label": ""}).code == "validation_failed"
    code = created.body["invites"][0]["code"]
    redeemed = client.call(
        "me_redeem", body={"code": code.lower().replace("-", " ")}, headers={"Accept-Language": "de-CH, en;q=0.5"}
    )
    assert redeemed.status == 201 and redeemed.body["locale"] == "de-CH"
    assert client.call("me_redeem", body={"code": code}).status == 200
    assert client.call("me_redeem", oid=ids.OID_BOB, body={"code": code}).code == "invite_used"

    me = client.call("me_get")
    assert me.status == 200 and me.body["oid"] == ids.OID_ALICE and me.body["roles"] == []
    patched = client.call(
        "me_patch",
        body={
            "settings": {"briefSize": 15},
            "consent": {"aiProcessing": {"granted": True, "version": "2026-10"}},
            "onboarding": {"completed": True},
        },
    )
    assert patched.status == 200 and patched.body["settings"]["briefSize"] == 15
    assert client.call("me_patch", body={"oid": ids.OID_BOB}).code == "validation_failed"
    assert client.call("me_patch", raw=b"{}", headers={"Content-Type": "text/plain"}).code == "unsupported_media_type"
    exported = client.call("me_export")
    assert exported.status == 200 and exported.headers["content-disposition"].startswith(
        'attachment; filename="mynews-export-'
    )

    # Admin views.
    assert client.call("admin_overview", **ADMIN).body["users"]["total"] == 2
    assert client.call("admin_overview").code == "admin_required"
    health = client.call("admin_health", **ADMIN)
    assert health.status == 200 and health.body["status"] in ("ok", "degraded")
    assert client.call("admin_health").code == "admin_required"
    listed = client.call("admin_invites_list", **ADMIN, params={"status": "used"})
    assert [invite["status"] for invite in listed.body["invites"]] == ["used"]
    assert client.call("admin_invites_list", **ADMIN, params={"status": "bogus"}).code == "validation_failed"
    other = created.body["invites"][1]["id"]
    assert client.call("admin_invite_revoke", **ADMIN, route_params={"id": other}).body["status"] == "revoked"
    assert client.call("admin_invite_revoke", **ADMIN, route_params={"id": "x"}).code == "not_found"
    users = client.call("admin_users_list", **ADMIN)
    assert client.call("admin_users_list").code == "admin_required"
    alice_uid = me.body["uid"]
    assert alice_uid in {user["uid"] for user in users.body["users"]}
    blocked = client.call(
        "admin_user_block", **ADMIN, route_params={"uid": alice_uid}, body={"blocked": True, "reason": "test"}
    )
    assert blocked.body["status"] == "blocked"
    assert client.call("me_get").code == "account_blocked"
    assert client.call("me_export").status == 200  # a blocked member keeps their data
    assert (
        client.call("admin_user_block", **ADMIN, route_params={"uid": alice_uid}, body={"blocked": False}).body[
            "status"
        ]
        == "active"
    )
    assert (
        client.call("admin_user_block", **ADMIN, route_params={"uid": "0" * 32}, body={"blocked": True}).code
        == "not_found"
    )

    # Deletion.
    deleted = client.call("me_delete")
    assert deleted.status == 202 and deleted.body == {"state": "deleting", "identityRemoval": "manual"}
    message = client.outputs["purge"].get()
    assert client.call("me_get").body["error"]["details"] == {"state": "deleting"}
    assert client.call("me_delete").status == 202 and client.outputs["purge"].get() is None
    assert client.call("admin_deletion_complete", **ADMIN, route_params={"id": "1" * 32}).code == "not_found"
    purge_account(PurgeMessage.from_json(message))
    gone = client.call("me_get")
    assert (gone.status, gone.body["error"]["details"]) == (410, {"state": "deleted"})
    assert client.call("me_delete").code == "account_deleted"
    pending = client.call("admin_deletions_list", **ADMIN)
    assert [d["id"] for d in pending.body["deletions"]] == [alice_uid] and pending.body["deletions"][0][
        "oid"
    ] == ids.OID_ALICE
    completed = client.call("admin_deletion_complete", **ADMIN, route_params={"id": alice_uid})
    assert completed.body["status"] == "completed" and completed.body["oid"] is None
    assert (
        client.call("admin_deletions_list", **ADMIN, params={"status": "completed"}).body["deletions"][0]["oid"] is None
    )
    assert client.call("admin_deletions_list", **ADMIN, params={"status": "pending"}).code == "validation_failed"

    successes = {name for name, status in client.seen if status < 400}
    failures = {name for name, status in client.seen if status >= 400}
    http_functions = {name for name, fn in client.functions.items() if fn.is_http_function()}
    assert successes == http_functions
    assert failures == http_functions - {"health"}


# ===========================================================================
# Status semantics across every route
# ===========================================================================
def _non_public(client: Client) -> list[str]:
    return sorted(
        name
        for name, fn in client.functions.items()
        if fn.is_http_function() and getattr(fn.get_user_function(), auth.AUTH_MARKER) != "public"
    )


def _route_params(name: str) -> dict[str, str]:
    route = indexed_functions()[name].get_trigger().route
    return {key: "a" * 32 for key in re.findall(r"\{([a-z]+)\}", route)}


class TestStatusSemantics:
    def test_missing_auth_settings_close_every_route_but_health(
        self, client: Client, env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        env(
            ENTRA_TENANT_ID=None,
            ENTRA_API_CLIENT_ID="not-a-guid",
            MYNEWS_HMAC_KEY="@Microsoft.KeyVault(SecretUri=https://kv/x)",
        )
        # A fresh "once per 5 minutes" throttle, so this test sees the log line.
        monkeypatch.setattr(auth, "_CONFIG_LOGS", auth._Throttle(300))
        with caplog.at_level(logging.ERROR, logger="core.auth"):
            for name in _non_public(client):
                resp = client.call(name, roles=("admin",), route_params=_route_params(name), body={})
                assert (resp.status, resp.code) == (503, "auth_unavailable"), name
                assert resp.body["error"]["details"] == {"reason": "config"}
                assert resp.headers["retry-after"] == "60"
        assert client.call("health", oid=None).status == 200
        # Names in the log, never values.
        assert "ENTRA_TENANT_ID" in caplog.text and "MYNEWS_HMAC_KEY" in caplog.text
        assert "KeyVault(SecretUri" not in caplog.text and "not-a-guid" not in caplog.text

    def test_no_token_is_401_unauthenticated_everywhere(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        for name in _non_public(client):
            resp = client.call(name, oid=None, route_params=_route_params(name), body={})
            assert (resp.status, resp.code) == (401, "unauthenticated"), name
        assert fake_cosmos.journal == []  # rejected without a database read

    def test_token_problems_are_401_invalid_token(self, client: Client, jwks: Any) -> None:
        expired = jwks.tokens.token(
            ids.OID_ALICE,
            exp=int(utcnow().timestamp()) - 3600,
            iat=int(utcnow().timestamp()) - 7200,
            nbf=int(utcnow().timestamp()) - 7200,
        )
        cases = {
            "expired": (expired, {"reason": "expired"}),
            "wrong audience": (jwks.tokens.token(ids.OID_ALICE, aud=ids.OTHER_CLIENT_ID), {}),
            "foreign client": (jwks.tokens.token(ids.OID_ALICE, azp=ids.OTHER_CLIENT_ID), {}),
            "no scope": (jwks.tokens.token(ids.OID_ALICE, scp="openid"), {}),
            "garbage": ("a.b.c", {}),
        }
        for label, (token, details) in cases.items():
            resp = client.call("me_get", oid=None, headers={"Authorization": f"Bearer {token}"})
            assert (resp.status, resp.code, resp.body["error"]["details"]) == (401, "invalid_token", details), label

    def test_an_unreachable_key_set_is_503_with_a_short_retry(self, client: Client, jwks: Any) -> None:
        jwks.client.error = jwt.PyJWKClientConnectionError("down")
        resp = client.call("me_get")
        assert (resp.status, resp.code, resp.headers["retry-after"]) == (503, "auth_unavailable", "5")
        assert resp.body["error"]["details"] == {"reason": "jwks"}

    def test_a_database_outage_is_503_degraded(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        _join(client, ids.OID_ALICE)
        fake_cosmos.fail("read", error=throttled)  # a 429 that outlived the SDK's retries
        resp = client.call("me_get")
        assert (resp.status, resp.code) == (503, "degraded")
        assert resp.body["error"]["details"] == {"dependency": "database"}

    def test_412_never_reaches_a_client(self, client: Client, fake_cosmos: FakeCosmos) -> None:
        # Two concurrent writes land between the gate's read and each replace:
        # the server re-reads and retries once, then answers 503 contention.
        _join(client, ids.OID_ALICE)
        pk = f"user|{ids.OID_ALICE}"

        def concurrent_write(fake: FakeCosmos) -> None:
            doc = fake.get("me", pk)
            fake.put({k: v for k, v in doc.items() if not k.startswith("_")})

        fake_cosmos.before("replace", concurrent_write, id="me", pk=pk, times=2)
        resp = client.call("me_patch", body={"locale": "de-CH"})
        assert (resp.status, resp.code, resp.headers["retry-after"]) == (503, "degraded", "1")
        assert resp.body["error"]["details"] == {"dependency": "database", "reason": "contention"}

    def test_an_unexpected_failure_is_500_without_its_text(
        self, client: Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from core import accounts

        _join(client, ids.OID_ALICE)

        def broken(now: datetime) -> dict:
            raise RuntimeError("secret internals /dbs/mynews/colls/news")

        monkeypatch.setattr(accounts, "overview", broken)
        with caplog.at_level(logging.ERROR):
            resp = client.call("admin_overview", **ADMIN, headers={"X-Request-Id": "trace-0123456789"})
        assert (resp.status, resp.code) == (500, "internal")
        assert b"secret internals" not in resp.raw
        assert "trace-0123456789" in caplog.text

    def test_every_body_route_has_the_same_body_rules(self, client: Client) -> None:
        bodies = {"me_redeem": {}, "me_patch": {}, "admin_invites_create": ADMIN, "admin_user_block": ADMIN}
        _join(client, ids.OID_ALICE)
        for name, identity in bodies.items():
            caller = {"oid": ids.OID_ALICE, **identity}
            route_params = _route_params(name)
            no_type = client.call(name, **caller, raw=b"{}", route_params=route_params)
            assert no_type.code == "unsupported_media_type", name
            assert no_type.body["error"]["details"] == {"expected": "application/json"}
            large = client.call(
                name,
                **caller,
                raw=b" " * 70000,
                headers={"Content-Type": "application/json"},
                route_params=route_params,
            )
            assert (large.status, large.body["error"]["details"]) == (413, {"limitBytes": 65536}), name
            for raw, reason in ((b"{oops", "invalid_json"), (b"[1]", "not_object")):
                resp = client.call(
                    name, **caller, raw=raw, headers={"Content-Type": "application/json"}, route_params=route_params
                )
                assert (resp.code, resp.body["error"]["details"]) == ("validation_failed", {"reason": reason}), name
            unknown = client.call(name, **caller, body={"surprise": 1}, route_params=route_params)
            assert {"field": "surprise", "reason": "unknown_field"} in unknown.body["error"]["details"]["fields"], name

    def test_a_validation_error_stays_inside_the_envelope_bounds(self, client: Client) -> None:
        # Field paths echo the client's own keys: 60 unknown keys, one of them
        # 200 characters long, still answer an envelope Error.schema.json
        # accepts (Client.call checks it): 50 entries, paths cut at 120.
        _join(client, ids.OID_ALICE)
        body = {"k" * 200: 1, **{f"key{i:02d}": i for i in range(60)}}
        resp = client.call("me_patch", body=body)
        fields = resp.body["error"]["details"]["fields"]
        assert len(fields) == 50 and max(len(entry["field"]) for entry in fields) == 120
        assert all(entry["reason"] == "unknown_field" for entry in fields)

    def test_routes_without_a_body_ignore_one(self, client: Client) -> None:
        _join(client, ids.OID_ALICE)
        assert client.call("me_get", raw=b"not json at all").status == 200
        invite = _mint()
        resp = client.call("admin_invite_revoke", **ADMIN, raw=b"\xff", route_params={"id": invite.invite_id})
        assert resp.status == 200

    def test_malformed_path_ids_are_404_before_the_resource_is_read(
        self, client: Client, fake_cosmos: FakeCosmos
    ) -> None:
        client.call("admin_overview", **ADMIN)  # bootstrap the admin first
        cases = {
            "admin_invite_revoke": ("id", "invite"),
            "admin_user_block": ("uid", "user"),
            "admin_deletion_complete": ("id", "deletion"),
        }
        for name, (param, resource) in cases.items():
            for bad in ("x", "A" * 32, ids.OID_BOB):
                before = len(fake_cosmos.journal)
                resp = client.call(name, **ADMIN, route_params={param: bad}, body={"blocked": True})
                assert (resp.status, resp.body["error"]["details"]) == (404, {"resource": resource}), (name, bad)
                touched = {op.pk for op in fake_cosmos.journal[before:]}
                assert touched <= {f"user|{ids.OID_ADMIN}"}, (name, touched)  # the gate's own read only

    def test_quota_answers_429_with_the_reset(self, client: Client) -> None:
        for _ in range(10):
            assert client.call("me_redeem", body={"code": "0000-0000-0000"}).code == "invite_not_found"
        resp = client.call("me_redeem", body={"code": "0000-0000-0000"})
        assert (resp.status, resp.code) == (429, "quota_exceeded")
        details = resp.body["error"]["details"]
        tomorrow = (utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
        assert details == {"quota": "redeem", "limit": 10, "period": "day", "resetsAt": f"{tomorrow}T00:00:00Z"}
        assert 0 < int(resp.headers["retry-after"]) <= 86400

    def test_the_request_id_is_echoed_on_errors_and_successes(self, client: Client) -> None:
        assert client.call("me_get", headers={"X-Request-Id": "app-req-0001"}).headers["x-request-id"] == "app-req-0001"
        _join(client, ids.OID_ALICE)
        assert client.call("me_get", headers={"X-Request-Id": "app-req-0002"}).headers["x-request-id"] == "app-req-0002"
        assert (
            client.call("me_get", oid=None, headers={"X-Request-Id": "app-req-0003"}).headers["x-request-id"]
            == "app-req-0003"
        )


def test_timestamps_on_the_wire_are_iso_seconds_z(client: Client) -> None:
    # The one format (core.models.iso), which the schemas' Timestamp pattern
    # enforces on every response checked above; spot-checked here directly.
    joined = _join(client, ids.OID_ALICE)
    assert joined.body["createdAt"] == iso(datetime.fromisoformat(joined.body["createdAt"].replace("Z", "+00:00")))
