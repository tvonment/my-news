"""The owner's admin routes (docs/api.md section 6.5): overview, deep health,
invites, users, and the pending identity removals.

Every handler is ``@auth.admin``: the role comes from the token and is checked
before any I/O (a non-admin gets ``403 admin_required`` without a database
read), then the member gate runs, so an admin's first admin call bootstraps
the membership. Accounts are addressed by ``uid``, never by oid, and no
response carries an email address; the only oid an admin route returns is the
one in the pending identity-removal list, which the owner needs to find the
user in Entra. Every mutation writes an audit entry with the admin's uid (in
core, next to the write it describes).

Path ids are checked against their grammar before the handler touches the
resource: a malformed id is ``404 not_found``, not a 400, because an id in a
path either names something or it does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import azure.functions as func

from api import http
from core import accounts, auth, identity_removal, invites, ops_health
from core.errors import invalid
from core.models import DELETION_AWAITING, INVITE_ID_RE, UID_RE, utcnow

if TYPE_CHECKING:
    from core.auth import Principal

bp = func.Blueprint()

# InviteCreateRequest.schema.json, with its defaults.
INVITE_CREATE_FIELDS = frozenset({"label", "maxUses", "expiresInDays", "count"})
DEFAULT_MAX_USES = 1
DEFAULT_EXPIRES_IN_DAYS = 30
DEFAULT_MINT_COUNT = 1
# AdminBlockRequest.schema.json.
BLOCK_FIELDS = frozenset({"blocked", "reason"})
# GET /admin/invites lists every status unless asked; GET /admin/deletions
# defaults to the owner's to-do list.
DEFAULT_INVITES_STATUS = "all"
DEFAULT_DELETIONS_STATUS = DELETION_AWAITING


def _admin() -> "Principal":
    principal = auth.current().principal
    if principal is None or not principal.is_admin:
        # The admin decorator answered 403 before this could run.
        raise RuntimeError("an admin handler runs with an admin principal")
    return principal


@bp.route(route="admin/overview", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.admin
def admin_overview(req: func.HttpRequest) -> func.HttpResponse:
    """One screen of counts; the Phase 2 and 3 sections are null until then."""
    return http.json_response(accounts.overview(utcnow()))


@bp.route(route="admin/health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.admin
def admin_health(req: func.HttpRequest) -> func.HttpResponse:
    """Deep checks, always 200: ``status`` says ``degraded`` when one fails, and
    a failed check reports an error class, never exception text. A health
    route that failed during an outage would tell the owner nothing."""
    return http.json_response(ops_health.deep_health(utcnow()))


@bp.route(route="admin/invites", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.admin
def admin_invites_list(req: func.HttpRequest) -> func.HttpResponse:
    """The newest 200 invites, optionally of one derived status. Codes are
    never returned again after the mint."""
    now = utcnow()
    status = http.query_param(req, "status") or DEFAULT_INVITES_STATUS
    # list_invites refuses a status outside its list (400) before any I/O.
    docs = invites.list_invites(status=status, now=now)
    return http.json_response({"invites": [invites.invite_to_wire(doc, now) for doc in docs]})


@bp.route(route="admin/invites", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.admin
def admin_invites_create(req: func.HttpRequest) -> func.HttpResponse:
    """Mint 1-20 codes sharing a label, an expiry and a use limit. Not
    idempotent: every call mints new codes. This response is the only place a
    plaintext code ever appears, and nothing on this path logs it."""
    body = http.read_json(req)
    http.reject_unknown_fields(body, INVITE_CREATE_FIELDS)
    if "label" not in body:
        raise invalid("label", "required")
    # Types and ranges are core.invites.mint's to check, with these wire names.
    minted = invites.mint(
        label=body["label"],
        max_uses=body.get("maxUses", DEFAULT_MAX_USES),
        expires_in_days=body.get("expiresInDays", DEFAULT_EXPIRES_IN_DAYS),
        count=body.get("count", DEFAULT_MINT_COUNT),
        actor_uid=_admin().uid,
        now=utcnow(),
    )
    return http.json_response({"invites": [invite.to_wire() for invite in minted]}, 201)


@bp.route(route="admin/invites/{id}/revoke", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.admin
def admin_invite_revoke(req: func.HttpRequest) -> func.HttpResponse:
    """Revoke an invite; revoking again answers it unchanged. The holder of a
    revoked code then sees ``invite_not_found``, so revocation is not
    revealed."""
    invite_id = http.path_param(req, "id", INVITE_ID_RE, "invite")
    now = utcnow()
    doc = invites.revoke(invite_id, actor_uid=_admin().uid, now=now)
    return http.json_response(invites.invite_to_wire(doc, now))


@bp.route(route="admin/users", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.admin
def admin_users_list(req: func.HttpRequest) -> func.HttpResponse:
    """The newest 500 members by uid, with ``truncated`` when the cap cut the
    list. No oid, no email."""
    docs, truncated = accounts.list_users()
    return http.json_response({"users": [accounts.user_to_wire(doc) for doc in docs], "truncated": truncated})


@bp.route(route="admin/users/{uid}/block", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.admin
def admin_user_block(req: func.HttpRequest) -> func.HttpResponse:
    """Block (``{"blocked": true, "reason"?}``) or unblock an account.
    Blocking into the current state answers 200; admins cannot block
    themselves; an account being deleted is a 409."""
    uid = http.path_param(req, "uid", UID_RE, "user")
    body = http.read_json(req)
    http.reject_unknown_fields(body, BLOCK_FIELDS)
    if "blocked" not in body:
        raise invalid("blocked", "required")
    if not isinstance(body["blocked"], bool):
        raise invalid("blocked", "type")
    # Present means a string: null is not "no reason", it is a wrong type.
    # Its length (1-200) is checked by set_blocked, which guards the audit log.
    if "reason" in body and not isinstance(body["reason"], str):
        raise invalid("reason", "type")
    user = accounts.set_blocked(uid, blocked=body["blocked"], reason=body.get("reason"), actor=_admin(), now=utcnow())
    return http.json_response(user)


@bp.route(route="admin/deletions", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.admin
def admin_deletions_list(req: func.HttpRequest) -> func.HttpResponse:
    """The pending identity removals (by default), newest request first. Each
    carries the oid until the identity is removed, because the owner needs it
    to find the user in the Entra admin center."""
    now = utcnow()
    status = http.query_param(req, "status") or DEFAULT_DELETIONS_STATUS
    docs = identity_removal.list_deletions(status=status, now=now)
    return http.json_response({"deletions": [identity_removal.deletion_to_wire(doc, now) for doc in docs]})


@bp.route(route="admin/deletions/{id}/complete", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@auth.admin
def admin_deletion_complete(req: func.HttpRequest) -> func.HttpResponse:
    """Record that the owner removed the Entra user: erases the stored oid and
    starts the tombstone's 30-day expiry. Again on a completed deletion: 200
    unchanged. While the purge still runs: 409 ``state_conflict``."""
    uid = http.path_param(req, "id", UID_RE, "deletion")
    return http.json_response(identity_removal.complete(uid, actor_uid=_admin().uid, now=utcnow()))
