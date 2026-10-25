"""The error-code registry and the one exception the API answers with.

Every error this app sends has the body ``{"error": {"code", "message",
"details"}}`` (docs/api.md section 1.3), and clients route on ``code``. This
module is the registry behind it: ``ERROR_STATUS`` fixes the HTTP status of every
code, so a handler cannot pick a status of its own, and ``ApiError`` refuses a
code that is not registered (a KeyError is a programming error, caught by the
tests, not a 500 waiting in production). test_http_contract.py checks the table
against docs/api.md section 1.4.

Two rules the table exists to hold:

* **410 is only ``account_deleted``.** It is the one answer that makes a client
  wipe its local data, so nothing else may ever map to it: an expired invite is
  a 409 and an aged-out story a 404.
* **Every 429 and 503 carries Retry-After.** An ``ApiError`` with one of those
  statuses gets a default of 5 seconds when the raiser did not choose one.

No azure.functions import: core code raises these, and only api/http.py turns
them into responses. ``message`` is English for logs and developers, never
shown to users, and never exception text, a token, an oid, an email address or
an invite code: the default messages below are fixed strings for that reason.
"""

from __future__ import annotations

from typing import Any, Mapping

# docs/api.md section 1.4, in its order.
ERROR_STATUS: dict[str, int] = {
    "validation_failed": 400,
    "invite_malformed": 400,
    "unauthenticated": 401,
    "invalid_token": 401,
    "invite_required": 403,
    "account_blocked": 403,
    "admin_required": 403,
    "consent_required": 403,
    "not_found": 404,
    "invite_not_found": 404,
    "invite_used": 409,
    "invite_expired": 409,
    "duplicate": 409,
    "limit_reached": 409,
    "state_conflict": 409,
    "account_deleted": 410,
    "payload_too_large": 413,
    "unsupported_media_type": 415,
    "quota_exceeded": 429,
    "internal": 500,
    "auth_unavailable": 503,
    "degraded": 503,
}

# The one list every details.fields[].reason and details.reason comes from.
VALIDATION_REASONS = frozenset(
    {
        "required",
        "type",
        "enum",
        "format",
        "range",
        "length",
        "unknown_field",
        "duplicate",
        "self",
        "invalid_json",
        "not_object",
        "empty",
    }
)

# details.resource of a 404 not_found (Error.schema.json enumerates them).
NOT_FOUND_RESOURCES = frozenset(
    {"story", "edition", "section", "tag", "source", "saved", "suggestion", "invite", "user", "deletion", "report"}
)

# degraded's details.dependency.
DEGRADED_DEPENDENCIES = frozenset({"database", "queue", "pool", "model"})

# Statuses whose answers always carry Retry-After (docs/api.md section 1.6).
RETRY_AFTER_STATUSES = frozenset({429, 503})
DEFAULT_RETRY_AFTER_SECONDS = 5

# English, for logs and developers; the app never shows them. Where docs/api.md
# has an example message, it is this text, so the golden fixtures and the
# examples agree.
DEFAULT_MESSAGES: dict[str, str] = {
    "validation_failed": "Request validation failed.",
    "invite_malformed": "The invite code is not in the expected format.",
    "unauthenticated": "A bearer token is required.",
    "invalid_token": "The access token was rejected.",
    "invite_required": "An invite code is required to use MyNews.",
    "account_blocked": "This account is blocked.",
    "admin_required": "This route needs the admin role.",
    "consent_required": "This feature needs consent to AI processing.",
    "not_found": "The resource does not exist.",
    "invite_not_found": "The invite code is not valid.",
    "invite_used": "The invite code has already been used.",
    "invite_expired": "The invite code has expired.",
    "duplicate": "The resource already exists.",
    "limit_reached": "A total limit is reached.",
    "state_conflict": "The resource is in a state that does not allow this action.",
    "account_deleted": "This account was deleted.",
    "payload_too_large": "The request body is larger than 64 KB.",
    "unsupported_media_type": "The request body must be application/json.",
    "quota_exceeded": "A usage limit is reached until the reset.",
    "internal": "An unexpected error occurred.",
    "auth_unavailable": "Authentication is unavailable.",
    "degraded": "A dependency is unavailable; retry later.",
}


class ApiError(Exception):
    """An answer for the client, raised anywhere below the handler.

    The access-level decorators (core.auth) catch it and render the envelope
    through api.http.error_response, so core code never builds a response.
    """

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
        retry_after: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        # KeyError for an unregistered code, on purpose: a code clients cannot
        # route on must not reach one.
        self.status: int = ERROR_STATUS[code]
        self.code = code
        self.message = message if message is not None else DEFAULT_MESSAGES[code]
        self.details: dict[str, Any] = dict(details or {})
        if retry_after is None and self.status in RETRY_AFTER_STATUSES:
            retry_after = DEFAULT_RETRY_AFTER_SECONDS
        self.retry_after = retry_after
        self.headers: dict[str, str] = dict(headers or {})
        super().__init__(f"{code}: {self.message}")

    def body(self) -> dict[str, Any]:
        """The envelope (docs/api.md section 1.3)."""
        return {"error": {"code": self.code, "message": self.message, "details": dict(self.details)}}

    def __repr__(self) -> str:
        return f"ApiError({self.code!r}, status={self.status}, details={self.details!r})"


def invalid(field: str, reason: str) -> ApiError:
    """One field failed: ``field`` is the wire path in dot notation
    (``settings.briefSize``, ``events[3].at``)."""
    if reason not in VALIDATION_REASONS:
        raise ValueError(f"unknown validation reason {reason!r}")
    return ApiError("validation_failed", details={"fields": [{"field": field, "reason": reason}]})


def invalid_body(reason: str) -> ApiError:
    """The whole body failed (``invalid_json``, ``not_object``, ``empty``)."""
    if reason not in VALIDATION_REASONS:
        raise ValueError(f"unknown validation reason {reason!r}")
    return ApiError("validation_failed", details={"reason": reason})


def not_found(resource: str) -> ApiError:
    """``404 not_found`` naming what kind of thing was not there. Also the
    answer for a path id that fails its grammar, before any I/O."""
    if resource not in NOT_FOUND_RESOURCES:
        raise ValueError(f"unknown not_found resource {resource!r}")
    return ApiError("not_found", details={"resource": resource})


def degraded(dependency: str, reason: str | None = None, retry_after: int = DEFAULT_RETRY_AFTER_SECONDS) -> ApiError:
    """``503 degraded``: a dependency is down or out of budget. ``reason`` is a
    short code (``contention`` with Retry-After 1 after a second 412,
    ``pool_not_ready`` with 300), never exception text."""
    if dependency not in DEGRADED_DEPENDENCIES:
        raise ValueError(f"unknown degraded dependency {dependency!r}")
    details: dict[str, Any] = {"dependency": dependency}
    if reason is not None:
        details["reason"] = reason
    return ApiError("degraded", details=details, retry_after=retry_after)
