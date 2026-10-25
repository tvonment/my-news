#!/usr/bin/env python3
"""The owner's admin CLI over the MyNews admin routes, until the iOS admin area exists.

    backend/.venv/bin/python scripts/mynews_admin.py health
    backend/.venv/bin/python scripts/mynews_admin.py overview
    backend/.venv/bin/python scripts/mynews_admin.py invites create --label "Family wave 1" [--max-uses 1] [--expires-days 30] [--count 1]
    backend/.venv/bin/python scripts/mynews_admin.py invites list [--status all|active|used|expired|revoked]
    backend/.venv/bin/python scripts/mynews_admin.py invites revoke <invite id>
    backend/.venv/bin/python scripts/mynews_admin.py users list
    backend/.venv/bin/python scripts/mynews_admin.py users block <uid> [--reason "..."]
    backend/.venv/bin/python scripts/mynews_admin.py users unblock <uid>
    backend/.venv/bin/python scripts/mynews_admin.py deletions list [--status awaiting_identity_removal|purging|completed|all]
    backend/.venv/bin/python scripts/mynews_admin.py deletions complete <uid>

Every command signs in exactly like scripts/get_token.py (same settings, same
token cache; --profile picks the identity) and calls <base URL>/api/admin/...,
where the base URL is --base-url, else SERVICE_BACKEND_URI from the environment
or the azd env (azd provision sets it). The routes need the `admin` app role on
the token (docs/identity.md); without it every command answers admin_required.
An admin's first call also creates their own membership (docs/api.md §2.4), and
--accept-language seeds its locale and content languages, e.g. 'de-CH,en;q=0.8'.

Output is for people. --json prints the response body exactly as the API sent it
(docs/api.md §6.5; schemas in docs/api/schemas/), and goes to stdout even for an
error, so a script can still read the envelope. Errors also print to stderr as
`code: message` from the error envelope, with its details, any Retry-After and
the request id that finds the call in the Function's logs.

Invite codes appear once, in the output of `invites create`: the server keeps
only a hash and never returns a code again, and this script writes it nowhere.

The script retries nothing: each command is one request, and a 429 or 503 says
how long to wait. Exit status: 0 ok; 1 the API answered with an error, or health
reports degraded; 2 configuration or usage; 3 sign-in or network failure.
"""

from __future__ import annotations

import argparse
import http
import json
import os
import re
import sys
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import get_token  # noqa: E402

USER_AGENT = "mynews-admin/1"
# /admin/health runs its checks one after another, each capped at 5 s.
DEFAULT_TIMEOUT_S = 60.0
# Invite ids and uids share one grammar (docs/api.md §1.5). Checked here so a
# typo reads as a typo, instead of the server's 404 for a malformed path id.
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
INVITE_STATUSES = ("all", "active", "used", "expired", "revoked")
DELETION_STATUSES = ("awaiting_identity_removal", "purging", "completed", "all")
# The one place plain http is allowed: a local `func start`. Anywhere else the
# access token would cross the network unencrypted.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})

EXIT_OK = 0
EXIT_API = 1
EXIT_CONFIG = get_token.EXIT_CONFIG
EXIT_TRANSPORT = get_token.EXIT_SIGN_IN
EXIT_INTERRUPTED = get_token.EXIT_INTERRUPTED

# One line of advice per error code an admin call can meet (docs/api.md §1.4).
HINTS = {
    "admin_required": "this token has no 'admin' role. Just assigned? Run again with --force-refresh:"
    " Entra writes roles into a token when it is issued.",
    "invite_required": "this identity has no membership, so its token lacks the admin role"
    " (admins get a membership on their first call); try --force-refresh, or check the role assignment.",
    "account_blocked": "this identity's own membership is blocked.",
    "account_deleted": "this identity's own account is being deleted, or was.",
    "invalid_token": "the backend rejected the token. Compare `get_token.py --decode` with docs/identity.md"
    " (issuer, audience, azp, scope).",
}


def out(line: str = "") -> None:
    print(line)


def err(line: str) -> None:
    print(line, file=sys.stderr)


# --- Where to call ----------------------------------------------------------------


def normalise_base_url(raw: str) -> str:
    """'https://<app>.azurewebsites.net', with no trailing slash and no /api.

    The API answers under /api; accepting either form saves a confusing 404."""
    parsed = urllib.parse.urlsplit(raw.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise get_token.ConfigError(f"the base URL is not an http(s) URL: {raw!r}")
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        raise get_token.ConfigError("refusing plain http to a remote host: every request carries your access token")
    if parsed.query or parsed.fragment:
        raise get_token.ConfigError(f"the base URL takes no query or fragment: {raw!r}")
    path = parsed.path.rstrip("/")
    if path.endswith("/api"):
        path = path[: -len("/api")]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def resolve_base_url(explicit: str | None, env_name: str | None, lookup: get_token.Lookup = get_token.azd_value) -> str:
    raw = explicit or get_token.setting("SERVICE_BACKEND_URI", env_name, lookup)
    if not raw:
        raise get_token.ConfigError(
            "no backend URL: pass --base-url, or set SERVICE_BACKEND_URI (azd provision writes it to the azd env)"
        )
    return normalise_base_url(raw)


# --- Requests -------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    method: str
    path: str  # under /api
    params: Mapping[str, str] | None = None
    body: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Any  # the parsed JSON, or None
    request_id: str
    retry_after: str | None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def envelope(self) -> tuple[str, str, dict[str, Any]] | None:
        """(code, message, details) from {"error": {...}}, or None for a reply
        without the envelope (the Functions host's own 404 for an unknown route)."""
        error = self.body.get("error") if isinstance(self.body, dict) else None
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            return None
        details = error.get("details")
        return error["code"], str(error.get("message") or ""), details if isinstance(details, dict) else {}


class AdminApi:
    """One httpx client per run, with the token in a default header.

    Redirects are not followed: a redirect would carry the Authorization header
    to wherever it points, and the API never redirects."""

    def __init__(self, base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT_S,
                 accept_language: str | None = None, transport: Any = None) -> None:
        import httpx

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": USER_AGENT}
        if accept_language:
            headers["Accept-Language"] = accept_language
        self._client = httpx.Client(base_url=f"{base_url}/api", headers=headers, timeout=timeout,
                                    follow_redirects=False, transport=transport)

    def close(self) -> None:
        self._client.close()

    def send(self, request: Request) -> ApiResponse:
        # Our own request id, in the server's grammar (^[A-Za-z0-9._:-]{8,64}$),
        # so the server echoes it and it can be printed even if no reply arrives.
        request_id = f"mynews-admin-{uuid.uuid4().hex[:16]}"
        headers = {"X-Request-Id": request_id}
        content = None
        if request.body is not None:
            content = json.dumps(request.body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        resp = self._client.request(request.method, request.path, params=request.params, content=content,
                                    headers=headers)
        try:
            body = resp.json() if resp.content else None
        except ValueError:
            body = None
        return ApiResponse(status=resp.status_code, body=body,
                           request_id=resp.headers.get("X-Request-Id") or request_id,
                           retry_after=resp.headers.get("Retry-After"))


# --- Rendering ------------------------------------------------------------------


def short_ts(value: Any) -> str:
    """'2026-10-21T10:00:00Z' as '2026-10-21 10:00' (UTC); null as '-'."""
    if not isinstance(value, str) or not value:
        return "-"
    return value.replace("T", " ")[:16] if value.endswith("Z") and "T" in value else value


def scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(scalar(v) for v in value) or "-"
    return str(value)


def flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested keys joined with dots, so a section of any shape prints as k=v pairs:
    Phase 2 adds checks and overview sections without touching this script."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            flat.update(flatten(value, name + "."))
        else:
            flat[name] = value
    return flat


def pairs(data: Mapping[str, Any]) -> str:
    return " ".join(f"{k}={scalar(v)}" for k, v in flatten(data).items())


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """Left-aligned columns; the last one is left ragged (labels can be long)."""
    widths = [max([len(headers[i])] + [len(r[i]) for r in rows]) for i in range(len(headers) - 1)]
    lines = []
    for row in [list(headers)] + [list(r) for r in rows]:
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])] + [row[-1]]
        lines.append("  ".join(cells).rstrip())
    return lines


def render_error(resp: ApiResponse, write: Callable[[str], None] = err) -> None:
    envelope = resp.envelope()
    if envelope:
        code, message, details = envelope
        write(f"{code}: {message}")
        if details:
            write("  details: " + json.dumps(details, ensure_ascii=False, sort_keys=True))
        hint = hint_for(code, details)
        if hint:
            write("  hint: " + hint)
    else:
        try:
            phrase = http.HTTPStatus(resp.status).phrase
        except ValueError:
            phrase = "unexpected status"
        write(f"http_{resp.status}: {phrase} (no MyNews error envelope: is Phase 1 deployed at this URL?)")
    if resp.retry_after:
        write(f"  retry after: {resp.retry_after} s")
    write(f"  request id: {resp.request_id}")


def hint_for(code: str, details: Mapping[str, Any]) -> str | None:
    if code == "auth_unavailable":
        if details.get("reason") == "config":
            return ("the Function App's ENTRA_* or MYNEWS_HMAC_KEY settings are missing or malformed (their names"
                    " are in the app's log); azd provision writes them.")
        return "the backend could not fetch the tenant's signing keys; try again shortly."
    if code == "invalid_token" and details.get("reason") == "expired":
        return "the token expired on the way; run again."
    return HINTS.get(code)


def render_health(body: Mapping[str, Any]) -> None:
    out(f"status    {body.get('status')}  (checked {body.get('checkedAt')})")
    out(f"api {scalar(body.get('api'))}, minBuild {scalar(body.get('minBuild'))},"
        f" identity removal {scalar(body.get('identityDeleteMode'))},"
        f" HMAC key fingerprint {scalar(body.get('hmacKeyFingerprint'))}")
    checks = body.get("checks") or {}
    width = max((len(name) for name in checks), default=0)
    for name, check in checks.items():
        check = check if isinstance(check, dict) else {}
        state = "ok  " if check.get("ok") is True else "FAIL"
        rest = {k: v for k, v in check.items() if k != "ok"}
        out(f"  {name.ljust(width)}  {state}  {pairs(rest)}".rstrip())


def render_overview(body: Mapping[str, Any]) -> None:
    sections = {k: v for k, v in body.items() if k != "generatedAt"}
    width = max([len("generated")] + [len(k) for k in sections])
    out(f"{'generated'.ljust(width)}  {body.get('generatedAt')}")
    for name, value in sections.items():
        # null means "arrives in a later phase" (spend, suggestions, reports).
        text = pairs(value) if isinstance(value, dict) else scalar(value)
        out(f"{name.ljust(width)}  {text}")


INVITE_HEADERS = ("ID", "STATUS", "USES", "EXPIRES", "CREATED", "LAST REDEEMED", "LABEL")


def invite_row(invite: Mapping[str, Any]) -> list[str]:
    return [scalar(invite.get("id")), scalar(invite.get("status")),
            f"{scalar(invite.get('uses'))}/{scalar(invite.get('maxUses'))}", short_ts(invite.get("expiresAt")),
            short_ts(invite.get("createdAt")), short_ts(invite.get("lastRedeemedAt")), scalar(invite.get("label"))]


def render_invites_created(body: Mapping[str, Any]) -> None:
    invites = body.get("invites") or []
    if not invites:
        out("no invites were created")
        return
    first = invites[0]
    uses = first.get("maxUses")
    out(f"{len(invites)} invite{'s' if len(invites) != 1 else ''} for \"{first.get('label')}\":"
        f" {uses} use{'s' if uses != 1 else ''} each, valid until {short_ts(first.get('expiresAt'))} UTC.")
    out("Each code is shown here once. MyNews keeps only its hash, so copy the codes now.")
    out()
    for invite in invites:
        out(f"  {invite.get('code')}   id {invite.get('id')}")


def render_invite_list(body: Mapping[str, Any]) -> None:
    invites = body.get("invites") or []
    if not invites:
        out("no invites")
        return
    for line in table(INVITE_HEADERS, [invite_row(i) for i in invites]):
        out(line)


def render_invite(body: Mapping[str, Any]) -> None:
    for line in table(INVITE_HEADERS, [invite_row(body)]):
        out(line)


USER_HEADERS = ("UID", "STATUS", "ADMIN", "CREATED", "LAST ACTIVE", "BLOCKED", "LOCALE", "LANGS", "INVITE")


def user_row(user: Mapping[str, Any]) -> list[str]:
    return [scalar(user.get("uid")), scalar(user.get("status")), "yes" if user.get("admin") else "no",
            short_ts(user.get("createdAt")), short_ts(user.get("lastActiveAt")), short_ts(user.get("blockedAt")),
            scalar(user.get("locale")), scalar(user.get("languages")), scalar(user.get("inviteLabel"))]


def render_user_list(body: Mapping[str, Any]) -> None:
    users = body.get("users") or []
    if not users:
        out("no accounts")
    else:
        for line in table(USER_HEADERS, [user_row(u) for u in users]):
            out(line)
    if body.get("truncated"):
        err("(truncated: the list holds the newest accounts only; --json shows the same)")


def render_user(body: Mapping[str, Any]) -> None:
    for line in table(USER_HEADERS, [user_row(body)]):
        out(line)


DELETION_HEADERS = ("UID", "STATUS", "MODE", "REQUESTED", "PURGED", "IDENTITY REMOVED", "DOCS", "OVERDUE", "OID")
# docs/identity.md, "Manual identity removal", in two lines.
REMOVAL_STEPS = (
    "Awaiting identity removal: in the Entra admin center (von Mentlen News), Users > search the oid > Delete,",
    "then Users > Deleted users > the same user > Delete permanently; then: mynews_admin.py deletions complete <uid>",
)


def deletion_row(deletion: Mapping[str, Any]) -> list[str]:
    return [scalar(deletion.get("id")), scalar(deletion.get("status")), scalar(deletion.get("identityMode")),
            short_ts(deletion.get("requestedAt")), short_ts(deletion.get("purgedAt")),
            short_ts(deletion.get("identityRemovedAt")), scalar(deletion.get("docsDeleted")),
            "OVERDUE" if deletion.get("overdue") else "-", scalar(deletion.get("oid"))]


def render_deletion_list(body: Mapping[str, Any]) -> None:
    deletions = body.get("deletions") or []
    if not deletions:
        out("no deletions")
        return
    for line in table(DELETION_HEADERS, [deletion_row(d) for d in deletions]):
        out(line)
    if any(d.get("status") == "awaiting_identity_removal" for d in deletions):
        out()
        for line in REMOVAL_STEPS:
            out(line)


def render_deletion(body: Mapping[str, Any]) -> None:
    for line in table(DELETION_HEADERS, [deletion_row(body)]):
        out(line)


# --- Commands -------------------------------------------------------------------


def hex32(kind: str) -> Callable[[str], str]:
    """An argparse type for an invite id or a uid: 32 hex characters, lower-cased."""

    def parse(value: str) -> str:
        normalised = value.strip().lower()
        if not HEX32_RE.match(normalised):
            raise argparse.ArgumentTypeError(f"{kind} must be 32 hex characters (see the list command): {value!r}")
        return normalised

    return parse


def build_request(args: argparse.Namespace) -> Request:
    command = args.command
    if command == "health":
        return Request("GET", "/admin/health")
    if command == "overview":
        return Request("GET", "/admin/overview")
    if command == "invites create":
        # Only the label is required by the API; the others are sent only when
        # given, so the server's defaults (1 use, 30 days, 1 code) stay the defaults.
        body: dict[str, Any] = {"label": args.label}
        for key, value in (("maxUses", args.max_uses), ("expiresInDays", args.expires_days), ("count", args.count)):
            if value is not None:
                body[key] = value
        return Request("POST", "/admin/invites", body=body)
    if command == "invites list":
        return Request("GET", "/admin/invites", params={"status": args.status})
    if command == "invites revoke":
        return Request("POST", f"/admin/invites/{args.invite_id}/revoke")
    if command == "users list":
        return Request("GET", "/admin/users")
    if command == "users block":
        body = {"blocked": True}
        if args.reason:
            body["reason"] = args.reason
        return Request("POST", f"/admin/users/{args.uid}/block", body=body)
    if command == "users unblock":
        return Request("POST", f"/admin/users/{args.uid}/block", body={"blocked": False})
    if command == "deletions list":
        return Request("GET", "/admin/deletions", params={"status": args.status})
    if command == "deletions complete":
        return Request("POST", f"/admin/deletions/{args.uid}/complete")
    raise get_token.ConfigError(f"unknown command: {command}")


RENDERERS: dict[str, Callable[[Mapping[str, Any]], None]] = {
    "health": render_health,
    "overview": render_overview,
    "invites create": render_invites_created,
    "invites list": render_invite_list,
    "invites revoke": render_invite,
    "users list": render_user_list,
    "users block": render_user,
    "users unblock": render_user,
    "deletions list": render_deletion_list,
    "deletions complete": render_deletion,
}
CONFIRMATIONS = {
    "invites revoke": "revoked: the code now redeems as invite_not_found",
    "users block": "blocked: the account answers account_blocked from its next request",
    "users unblock": "unblocked",
    "deletions complete": "completed: the stored oid is erased and the tombstone expires in 30 days",
}


def _add_common(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """The global options, accepted before or after the command.

    argparse lets a subcommand's defaults overwrite values given before it, so the
    subcommands' copies default to SUPPRESS and only ever set what was typed."""

    def default(value: Any) -> Any:
        return argparse.SUPPRESS if suppress else value

    parser.add_argument("--json", action="store_true", default=default(False),
                        help="print the response body as JSON, as the API sent it")
    parser.add_argument("--base-url", default=default(None), metavar="URL",
                        help="backend root, e.g. https://<app>.azurewebsites.net (default: SERVICE_BACKEND_URI)")
    parser.add_argument("--env", default=default(os.environ.get("AZURE_ENV_NAME") or None), metavar="NAME",
                        help="azd environment to read settings from (default: azd's default environment)")
    # The defaults are spelled out in the help text: %(default)s would print
    # argparse's SUPPRESS marker in each subcommand's help.
    parser.add_argument("--profile", default=default(get_token.DEFAULT_PROFILE),
                        help=f"token cache profile, as in get_token.py (default: {get_token.DEFAULT_PROFILE})")
    parser.add_argument("--force-refresh", action="store_true", default=default(False),
                        help="use the refresh token instead of a cached access token (picks up a new role)")
    parser.add_argument("--timeout", type=float, default=default(DEFAULT_TIMEOUT_S), metavar="SECONDS",
                        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_S:g})")
    parser.add_argument("--accept-language", default=default(None), metavar="TAGS",
                        help="Accept-Language for the admin bootstrap, e.g. 'de-CH,en;q=0.8'")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mynews_admin.py", description="MyNews admin routes from the command line.")
    _add_common(parser, suppress=False)
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, suppress=True)
    commands = parser.add_subparsers(dest="group", required=True, metavar="COMMAND")

    commands.add_parser("health", parents=[common], help="deep dependency checks (exit 1 when degraded)")
    commands.add_parser("overview", parents=[common], help="counts: users, invites, deletions, harvest, secrets")

    invites = commands.add_parser("invites", help="mint, list and revoke invite codes").add_subparsers(
        dest="action", required=True, metavar="ACTION")
    create = invites.add_parser("create", parents=[common], help="mint codes (each shown once)")
    create.add_argument("--label", required=True, help="who or which wave the codes are for (1-40 characters)")
    create.add_argument("--max-uses", type=int, help="redemptions per code, 1-10 (server default 1)")
    create.add_argument("--expires-days", type=int, help="validity in days, 1-90 (server default 30)")
    create.add_argument("--count", type=int, help="how many codes, 1-20 (server default 1)")
    listing = invites.add_parser("list", parents=[common], help="the newest 200 invites")
    listing.add_argument("--status", choices=INVITE_STATUSES, default="all")
    revoke = invites.add_parser("revoke", parents=[common], help="revoke an invite (idempotent)")
    revoke.add_argument("invite_id", type=hex32("an invite id"), metavar="INVITE_ID")

    users = commands.add_parser("users", help="list, block and unblock accounts").add_subparsers(
        dest="action", required=True, metavar="ACTION")
    users.add_parser("list", parents=[common], help="the newest 500 accounts (no emails, no oids)")
    block = users.add_parser("block", parents=[common], help="block an account (takes effect at once)")
    block.add_argument("uid", type=hex32("a uid"), metavar="UID")
    block.add_argument("--reason", help="kept in the audit log (1-200 characters)")
    unblock = users.add_parser("unblock", parents=[common], help="unblock an account")
    unblock.add_argument("uid", type=hex32("a uid"), metavar="UID")

    deletions = commands.add_parser("deletions", help="pending identity removals").add_subparsers(
        dest="action", required=True, metavar="ACTION")
    dlist = deletions.add_parser("list", parents=[common], help="account deletions, newest request first")
    dlist.add_argument("--status", choices=DELETION_STATUSES, default="awaiting_identity_removal")
    complete = deletions.add_parser("complete", parents=[common],
                                    help="record that the Entra user is deleted (after removing it by hand)")
    complete.add_argument("uid", type=hex32("a deletion id (the account's uid)"), metavar="UID")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    # One key per command ("invites create"), which build_request, RENDERERS
    # and CONFIRMATIONS share.
    args.command = f"{args.group} {args.action}" if getattr(args, "action", None) else args.group
    return args


def run(args: argparse.Namespace, api: AdminApi) -> int:
    """Send the one request, print the outcome, and choose the exit status."""
    resp = api.send(build_request(args))
    if args.json and resp.body is not None:
        out(json.dumps(resp.body, indent=2, ensure_ascii=False))
    if not resp.ok:
        render_error(resp)
        return EXIT_API
    body = resp.body if isinstance(resp.body, dict) else {}
    if not args.json:
        RENDERERS[args.command](body)
        if args.command in CONFIRMATIONS:
            err(CONFIRMATIONS[args.command])
    if args.command == "health" and body.get("status") != "ok":
        return EXIT_API
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        base_url = resolve_base_url(args.base_url, args.env)
        cfg = get_token.load_config(args.env)
        token = get_token.acquire_token(cfg, profile=args.profile, force_refresh=args.force_refresh)
    except get_token.ConfigError as exc:
        err(f"mynews_admin: {exc}")
        return EXIT_CONFIG
    except get_token.SignInError as exc:
        err(f"mynews_admin: {exc}")
        return EXIT_TRANSPORT
    except KeyboardInterrupt:
        err("mynews_admin: interrupted")
        return EXIT_INTERRUPTED

    import httpx

    api = AdminApi(base_url, token, timeout=args.timeout, accept_language=args.accept_language)
    try:
        return run(args, api)
    except httpx.HTTPError as exc:
        # httpx's messages name the URL and the failure, never the headers.
        err(f"mynews_admin: network: {type(exc).__name__}: {exc}")
        return EXIT_TRANSPORT
    except KeyboardInterrupt:
        err("mynews_admin: interrupted")
        return EXIT_INTERRUPTED
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
