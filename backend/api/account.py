"""The caller's own account (docs/api.md section 6.2): redeeming an invite,
reading and changing ``/me``, deleting the account, and the data export.

Every handler works on the caller's own data only, derived from the validated
token: no route takes an oid, a uid or a partition key from the client
(docs/api.md section 2.5), and the member gate has already read the caller's
``me`` and hands it over through ``auth.current()``, so no handler reads it a
second time. The handlers are thin on purpose: the rules (who may redeem what,
which fields PATCH accepts, the deletion order) live in core, where the tests
exercise them without HTTP, and this module only maps requests and answers.

Decorator order, on every handler: ``@bp.route`` outermost, then any output
binding, then the core.auth level innermost, directly above ``def``, so that
the function the Functions host indexes is the one carrying the level marker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import azure.functions as func

from api import http
from core import accounts, auth, invites, members
from core.errors import invalid
from core.jobs import JOBS_CONNECTION, QUEUE_ACCOUNT_PURGE
from core.models import utcnow

if TYPE_CHECKING:
    from core.auth import Principal

bp = func.Blueprint()

# Redeem.schema.json: the body is {"code"} and nothing else.
REDEEM_FIELDS = frozenset({"code"})


def _principal() -> "Principal":
    principal = auth.current().principal
    if principal is None:
        # Only a handler without its access decorator gets here, and
        # test_route_inventory refuses those.
        raise RuntimeError("a token-level handler runs with a validated principal")
    return principal


def _member() -> tuple["Principal", dict[str, Any]]:
    ctx = auth.current()
    if ctx.principal is None or ctx.me is None:
        raise RuntimeError("a member-level handler runs with a principal and the gate's document")
    return ctx.principal, ctx.me


@bp.route(route="me/redeem", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.token
def me_redeem(req: func.HttpRequest) -> func.HttpResponse:
    """POST /me/redeem: 201 with ``Me`` when the membership was created now (by
    the code, or by the admin bootstrap), 200 when the caller already was an
    active member, who then spends neither the code nor a quota unit.

    The token level, not member: a caller without a membership is exactly who
    this route is for. A body without a ``code`` string is the client's bug
    (``validation_failed``); any string, however mangled, is the member's typing
    and goes to core.invites, which answers ``invite_malformed`` for anything
    that does not normalise to 12 Crockford symbols, before any I/O.
    """
    body = http.read_json(req)
    http.reject_unknown_fields(body, REDEEM_FIELDS)
    if "code" not in body:
        raise invalid("code", "required")
    if not isinstance(body["code"], str):
        raise invalid("code", "type")
    principal = _principal()
    me, created = invites.redeem(
        principal,
        body["code"],
        # Prefills locale and languages if a membership is created now; ignored
        # afterwards, when me.locale decides.
        accept_language=req.headers.get("Accept-Language"),
        now=utcnow(),
    )
    return http.json_response(members.me_to_wire(me, principal), 201 if created else 200)


@bp.route(route="me", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.member
def me_get(req: func.HttpRequest) -> func.HttpResponse:
    """GET /me: the account and its settings, with ``roles`` copied from the
    validated token (they gate the admin area). The app calls it on launch to
    decide its phase; the gate has already answered 403 or 410 for an account
    that is not an active member. Not ETag-cached."""
    principal, me = _member()
    return http.json_response(members.me_to_wire(me, principal))


@bp.route(route="me", methods=["PATCH"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.member
def me_patch(req: func.HttpRequest) -> func.HttpResponse:
    """PATCH /me: any subset of the whitelisted fields (MePatch, strictly), then
    the full ``Me``. Idempotent: an unchanged PATCH writes nothing."""
    body = http.read_json(req)
    principal, me = _member()
    saved = members.save_me_patch(principal, me, body, utcnow())
    return http.json_response(members.me_to_wire(saved, principal))


@bp.route(route="me", methods=["DELETE"], auth_level=func.AuthLevel.ANONYMOUS)
@bp.queue_output(arg_name="purge", queue_name=QUEUE_ACCOUNT_PURGE, connection=JOBS_CONNECTION)
@auth.member(allow_blocked=True, allow_deleting=True)
def me_delete(req: func.HttpRequest, purge: func.Out[str]) -> func.HttpResponse:
    """DELETE /me: mark the account ``deleting`` and answer 202 at once; the
    ``account-purge`` worker does the destructive part (docs/data-model.md
    section 8). From the first write on, every other route answers 410.

    The queue message goes out through the output binding, which the host
    writes only after this function returned, so a failed request sends none.
    A repeat answers 202 again without a second message; a message that never
    arrives is re-sent by the daily maintenance timer, which finds the account
    by its registry document. Blocked members may delete, and so may an account
    already deleting (that is the repeat).
    """
    body, message = accounts.request_deletion(auth.current(), now=utcnow())
    if message is not None:
        purge.set(message)
    return http.json_response(body, 202)


@bp.route(route="me/export", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.member(allow_blocked=True)
def me_export(req: func.HttpRequest) -> func.HttpResponse:
    """GET /me/export: everything stored for the caller, as a JSON attachment.
    A blocked member may still take their data with them. Quota: 2 a day."""
    body, filename = accounts.build_export(auth.current(), now=utcnow())
    return http.attachment_response(body, filename)
