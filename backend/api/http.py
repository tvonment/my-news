"""The response and request helpers behind every route (docs/api.md section 1).

Every answer this app sends is built here, so the headers clients and caches
depend on are set in one place rather than per handler:

* ``Content-Type: application/json; charset=utf-8`` on every body;
* ``Cache-Control: no-store`` everywhere, except ``private, no-cache`` on the
  three ETag routes (brief, topics, catalog; Phase 3);
* ``Retry-After`` (integer seconds) on every 429 and 503, and
  ``WWW-Authenticate`` on every 401;
* ``X-Request-Id`` on every response, errors included. The access-level
  decorators (core.auth) add it through ``with_request_id`` after the handler
  returned, so a handler cannot forget it.

No blueprint lives here, and nothing here imports core.auth: the decorators
render their answers with these helpers, so an import the other way would
close a cycle. The allowed imports are core.errors, core.models, the standard
library and azure.functions.

Request bodies are strict (docs/api.md section 1.7): at most 64 KB, declared
as JSON, one UTF-8 JSON object. ``read_json`` checks in that order, so a client
bug gets the most specific answer: 415 before 413 before 400.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Mapping

import azure.functions as func

from core.errors import DEFAULT_RETRY_AFTER_SECONDS, RETRY_AFTER_STATUSES, ApiError, invalid_body, not_found

MAX_BODY_BYTES = 65536
REQUEST_ID_HEADER = "X-Request-Id"
# What a client may send as its own request id; anything else (including the
# ", "-joined value of a repeated header) is replaced by a server-issued UUID,
# so a log line can always be found by the id the client saw.
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")
JSON_MEDIA_TYPE = "application/json"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
NO_STORE = "no-store"
# The ETag routes: a client cache may keep the body but must revalidate it.
ETAG_CACHE_CONTROL = "private, no-cache"
# The challenge every 401 carries (RFC 6750 section 3).
BEARER_REALM = 'Bearer realm="mynews"'
# A Content-Disposition filename is server-built; this keeps it a token that
# needs no quoting rules beyond the surrounding quotes.
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# Error.schema.json bounds a validation_failed body: at most 50 field entries,
# each field path at most 120 characters.
MAX_FIELD_ERRORS = 50
MAX_FIELD_PATH_CHARS = 120


# ---------------------------------------------------------------------------
# Request ids
# ---------------------------------------------------------------------------
def request_id_for(req: func.HttpRequest) -> str:
    """The client's ``X-Request-Id`` when it is usable, else a fresh UUID.
    Never anything else: the value is echoed and logged."""
    value = (req.headers.get(REQUEST_ID_HEADER) or "").strip()
    if REQUEST_ID_RE.fullmatch(value):
        return value
    return str(uuid.uuid4())


def with_request_id(resp: func.HttpResponse, request_id: str) -> func.HttpResponse:
    resp.headers[REQUEST_ID_HEADER] = request_id
    return resp


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------
def dumps(payload: Any) -> bytes:
    """Compact UTF-8 JSON. Deliberately no ``default=``: a value JSON cannot
    carry (a datetime, a set) is a mapping bug, and it must fail as a 500 in
    tests rather than reach a client as an unparsable string. NaN and
    Infinity are refused for the same reason: they are not JSON."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _bytes_response(
    body: bytes,
    status: int,
    *,
    headers: Mapping[str, str] | None = None,
    cache_control: str = NO_STORE,
) -> func.HttpResponse:
    merged = {"Content-Type": JSON_CONTENT_TYPE, "Cache-Control": cache_control, **(headers or {})}
    # mimetype as well as the header: the worker only falls back to mimetype
    # when no Content-Type header is set, but callers (and tests) read it.
    return func.HttpResponse(body=body, status_code=status, headers=merged, mimetype=JSON_MEDIA_TYPE, charset="utf-8")


def json_response(
    payload: Any,
    status: int = 200,
    *,
    headers: Mapping[str, str] | None = None,
    cache_control: str = NO_STORE,
) -> func.HttpResponse:
    return _bytes_response(dumps(payload), status, headers=headers, cache_control=cache_control)


def etag_for(body: bytes) -> str:
    """A strong ETag over the whole body (docs/api.md section 1.11)."""
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def _if_none_match(header: str | None, etag: str) -> bool:
    """Whether an If-None-Match list names ``etag``. ``W/`` prefixes are
    accepted (the weak comparison RFC 9110 prescribes for this header), and
    ``*`` matches any current representation."""
    if not header:
        return False
    for candidate in header.split(","):
        tag = candidate.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


def etag_response(req: func.HttpRequest, payload: Any) -> func.HttpResponse:
    """200 with an ETag, or 304 with no body when the client already holds this
    exact body. The tag covers per-user state too (read marks), so it changes
    whenever anything the user would see changes."""
    body = dumps(payload)
    etag = etag_for(body)
    if _if_none_match(req.headers.get("If-None-Match"), etag):
        return func.HttpResponse(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": ETAG_CACHE_CONTROL},
            mimetype=JSON_MEDIA_TYPE,
            charset="utf-8",
        )
    return _bytes_response(body, 200, headers={"ETag": etag}, cache_control=ETAG_CACHE_CONTROL)


def attachment_response(payload: Any, filename: str) -> func.HttpResponse:
    """A JSON body the client saves as a file (GET /me/export)."""
    if not _FILENAME_RE.fullmatch(filename):
        raise ValueError("an attachment filename is [A-Za-z0-9._-], at most 100 characters")
    return json_response(payload, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def no_content() -> func.HttpResponse:
    return func.HttpResponse(status_code=204, headers={"Cache-Control": NO_STORE})


def _bounded(body: dict[str, Any]) -> dict[str, Any]:
    """The envelope within Error.schema.json's bounds: a validation error names
    at most 50 fields, each path at most 120 characters. Field paths can echo
    a client's own keys, and every raiser (core.members' PATCH validation
    among them) would otherwise have to remember the bound; this is the one
    place every error passes."""
    fields = body["error"]["details"].get("fields")
    if body["error"]["code"] == "validation_failed" and isinstance(fields, list):
        body["error"]["details"]["fields"] = [
            {**entry, "field": field_path(str(entry.get("field", "")))} for entry in fields[:MAX_FIELD_ERRORS]
        ]
    return body


def error_response(err: ApiError) -> func.HttpResponse:
    """The envelope ``{"error": {"code", "message", "details"}}`` with the
    status the registry fixes for the code, and the headers its status needs."""
    headers: dict[str, str] = {}
    if err.status in RETRY_AFTER_STATUSES:
        seconds = err.retry_after if err.retry_after is not None else DEFAULT_RETRY_AFTER_SECONDS
        # Integer seconds, never an HTTP date (docs/api.md section 1.6).
        headers["Retry-After"] = str(max(0, int(seconds)))
    if err.status == 401:
        # error="invalid_token" tells an RFC 6750 client to refresh; a missing
        # token gets the bare challenge.
        headers["WWW-Authenticate"] = BEARER_REALM + (', error="invalid_token"' if err.code == "invalid_token" else "")
    headers.update(err.headers)
    return json_response(_bounded(err.body()), err.status, headers=headers)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
def _is_json_content_type(value: str | None) -> bool:
    """``application/json``, with ``charset=utf-8`` if any charset at all.
    Case-insensitive, parameters in any order. A repeated header arrives
    joined with ", " and fails, which is right: one body has one type."""
    if not value:
        return False
    media, _, params = value.partition(";")
    if media.strip().lower() != JSON_MEDIA_TYPE:
        return False
    for param in params.split(";"):
        name, _, raw = param.partition("=")
        if name.strip().lower() == "charset" and raw.strip().strip('"').lower() != "utf-8":
            return False
    return True


def _too_large() -> ApiError:
    return ApiError("payload_too_large", details={"limitBytes": MAX_BODY_BYTES})


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse an object that names a key twice. Python keeps the last one;
    other parsers keep the first. A body two parsers read differently is not
    a body this API acts on."""
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_constants(name: str) -> Any:
    # NaN, Infinity and -Infinity: Python accepts them, JSON does not.
    raise ValueError(f"{name} is not JSON")


def read_json(req: func.HttpRequest) -> dict[str, Any]:
    """The request body as one JSON object, or the error that says why not.

    415 ``unsupported_media_type`` without ``Content-Type: application/json``;
    413 ``payload_too_large`` over 64 KB, by ``Content-Length`` or by the bytes
    actually received; 400 ``validation_failed`` with reason ``invalid_json``
    (not UTF-8, not JSON, a repeated key) or ``not_object``.
    """
    if not _is_json_content_type(req.headers.get("Content-Type")):
        raise ApiError("unsupported_media_type", details={"expected": JSON_MEDIA_TYPE})
    declared = (req.headers.get("Content-Length") or "").strip()
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _too_large()
    body = req.get_body() or b""
    if len(body) > MAX_BODY_BYTES:
        raise _too_large()
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError, RecursionError):
        # JSONDecodeError is a ValueError; RecursionError is a 64 KB body of
        # nothing but opening brackets. Neither message reaches the client.
        raise invalid_body("invalid_json") from None
    if not isinstance(data, dict):
        raise invalid_body("not_object")
    return data


def field_path(path: str) -> str:
    """A wire field path as an error may name it: Error.schema.json caps
    ``details.fields[].field`` at 120 characters, and an unknown key is
    whatever the client sent."""
    return path[:MAX_FIELD_PATH_CHARS]


def reject_unknown_fields(body: Mapping[str, Any], allowed: frozenset[str]) -> None:
    """Request bodies are strict (docs/api.md section 1.2): a key the route
    does not take is ``400 validation_failed`` with reason ``unknown_field``,
    so a client typo fails loudly instead of being silently dropped. Every
    unknown key is named, up to the envelope's 50."""
    unknown = sorted(key for key in body if key not in allowed)
    if unknown:
        fields = [{"field": field_path(key), "reason": "unknown_field"} for key in unknown[:MAX_FIELD_ERRORS]]
        raise ApiError("validation_failed", details={"fields": fields})


def query_param(req: func.HttpRequest, name: str) -> str | None:
    """A query parameter, stripped; absent and empty are both None."""
    value = (req.params or {}).get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def path_param(req: func.HttpRequest, name: str, pattern: re.Pattern[str], resource: str) -> str:
    """A path segment that must match its id grammar. One that does not is a
    ``404 not_found`` naming ``resource``, raised before the handler does any
    I/O for it (docs/api.md section 1.4): an id in a path either names a known
    resource or it does not."""
    value = (req.route_params or {}).get(name)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise not_found(resource)
    return value
