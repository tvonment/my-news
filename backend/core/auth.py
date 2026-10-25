"""Who is calling: Entra External ID access tokens, access levels, and the
per-request context.

Every check here fails closed (docs/api.md section 2.1). In order:

0. **Configuration.** ENTRA_TENANT_ID, ENTRA_API_CLIENT_ID,
   ENTRA_ALLOWED_CLIENT_IDS, ENTRA_REQUIRED_SCOPE, ENTRA_ADMIN_ROLE and
   MYNEWS_HMAC_KEY must be present, resolved and (the ids) GUIDs. Otherwise
   every non-public route answers 503 auth_unavailable, so until the tenant's
   ids are deployed the API is closed by construction, never open.
1. **Header.** Exactly one ``Authorization: Bearer <jwt>``.
2. **JOSE header.** ``alg`` must be RS256 and ``kid`` present. The algorithm is
   the server's choice, never the token's: a token that says ``none`` or
   ``HS256`` is refused before any key is looked up.
3. **Signing key** from the tenant's JWKS through one PyJWKClient per process,
   cached in memory for 12 h and never persisted. A fetch failure is a 503
   (the token may be fine; the client should back off, not re-authenticate).
4. **Signature and claims**: exact ``iss``, single ``aud``, ``exp``/``nbf``/
   ``iat`` with 60 s leeway, ``ver`` 2.0, ``tid``, a GUID ``oid``, an allowed
   ``azp``, and the required scope in ``scp`` (app-only tokens have none).

The token is never logged. Failures log the check or exception class name, and
the uid once the oid is verified. ``sub``, ``name``, ``email`` and
``preferred_username`` are never read.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import hmac
import logging
import re
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, TypeVar
from urllib.parse import urlparse

import jwt

from core import audit, cosmos, members
from core.config import Settings, get_settings, unresolved_keyvault_ref
from core.errors import ApiError, degraded
from core.models import OID_RE

if TYPE_CHECKING:
    import azure.functions as func

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

LEEWAY_SECONDS = 60
MAX_AUTH_HEADER_BYTES = 8192
JWKS_TIMEOUT_SECONDS = 5
JWKS_LIFESPAN_SECONDS = 43200
MAX_KID_CHARS = 128

AUTH_LEVELS = ("public", "token", "member", "admin")
AUTH_MARKER = "__mynews_auth__"
ALLOW_BLOCKED_MARKER = "__mynews_allow_blocked__"
ALLOW_DELETING_MARKER = "__mynews_allow_deleting__"

# The settings the auth path cannot work without, by env var name: the names
# (never the values) are what config_problems() reports and the log carries.
AUTH_SETTING_NAMES = (
    "ENTRA_TENANT_ID",
    "ENTRA_API_CLIENT_ID",
    "ENTRA_ALLOWED_CLIENT_IDS",
    "ENTRA_REQUIRED_SCOPE",
    "ENTRA_ADMIN_ROLE",
    "MYNEWS_HMAC_KEY",
)
REQUIRED_CLAIMS = ["exp", "nbf", "iat", "iss", "aud", "ver", "tid", "oid", "azp"]

_B64URL_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
# A scope or role is one token without spaces: ENTRA_REQUIRED_SCOPE="a b" would
# never match an element of scp.split(), which reads as "nobody may call".
_SPACELESS = re.compile(r"^\S+$")


def issuer_for(tenant_id: str) -> str:
    """The exact v2 issuer. The tenant GUID appears in host and path alike,
    even when tokens are requested through the <sub>.ciamlogin.com host."""
    return f"https://{tenant_id}.ciamlogin.com/{tenant_id}/v2.0"


def jwks_uri_for(tenant_id: str) -> str:
    return f"https://{tenant_id}.ciamlogin.com/{tenant_id}/discovery/v2.0/keys"


def _missing(value: str) -> bool:
    return not value or unresolved_keyvault_ref(value)


def config_problems(settings: Settings | None = None) -> tuple[str, ...]:
    """The auth settings (by env var name) that are missing, unresolved Key Vault
    references, or malformed. Empty means auth can run."""
    s = settings or get_settings()
    problems: list[str] = []
    if _missing(s.entra_tenant_id) or not OID_RE.match(s.entra_tenant_id):
        problems.append("ENTRA_TENANT_ID")
    if _missing(s.entra_api_client_id) or not OID_RE.match(s.entra_api_client_id):
        problems.append("ENTRA_API_CLIENT_ID")
    if not s.entra_allowed_client_ids or not all(OID_RE.match(c) for c in s.entra_allowed_client_ids):
        problems.append("ENTRA_ALLOWED_CLIENT_IDS")
    if _missing(s.entra_required_scope) or not _SPACELESS.match(s.entra_required_scope):
        problems.append("ENTRA_REQUIRED_SCOPE")
    if _missing(s.entra_admin_role) or not _SPACELESS.match(s.entra_admin_role):
        problems.append("ENTRA_ADMIN_ROLE")
    if _missing(s.hmac_key):
        problems.append("MYNEWS_HMAC_KEY")
    return tuple(problems)


def uid_for(oid: str, key: str | None = None) -> str:
    """The pseudonymous id that logs, audit entries and admin routes use.

    HMAC rather than a plain hash: oids are not secret (Graph lists them), so a
    bare sha256 could be reversed by hashing candidate oids. Without the key no
    uid can be computed, so an empty key closes auth instead of producing uids
    that every later key would orphan.
    """
    secret = get_settings().hmac_key if key is None else key
    if _missing(secret):
        raise ApiError("auth_unavailable", details={"reason": "config"}, retry_after=60)
    return hmac.new(secret.encode("utf-8"), oid.lower().encode("ascii"), hashlib.sha256).hexdigest()[:32]


def hmac_key_fingerprint(key: str) -> str:
    """Identifies the HMAC key without revealing it: admin health shows it and
    postprovision.sh compares it (``printf '%s' "mynews-hmac-fp:$KEY" | shasum
    -a 256 | cut -c1-12``), so a regenerated key is noticed before it orphans
    every uid."""
    return hashlib.sha256(("mynews-hmac-fp:" + key).encode("utf-8")).hexdigest()[:12]


@functools.lru_cache(maxsize=4)
def _jwks_client(uri: str) -> jwt.PyJWKClient:
    """One client per JWKS URI for the process lifetime (the test seam).

    The key set is cached for 12 h (Entra rotates keys on a scale of weeks and
    publishes new ones well before use), keys are cached by kid, an unknown kid
    triggers at most one refetch per 30 s cooldown, and a fetch gives up after
    5 s. Nothing is written to disk: a cold instance fetches once.
    """
    return jwt.PyJWKClient(
        uri,
        cache_jwk_set=True,
        lifespan=JWKS_LIFESPAN_SECONDS,
        cache_keys=True,
        timeout=JWKS_TIMEOUT_SECONDS,
    )


class _Throttle:
    """At most one event per interval per process, for log lines that would
    otherwise repeat on every request during an outage."""

    def __init__(self, interval_s: float):
        self.interval_s = interval_s
        self._last: float | None = None
        self._lock = threading.Lock()

    def ready(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._last is not None and now - self._last < self.interval_s:
                return False
            self._last = now
            return True


_JWKS_ALERTS = _Throttle(300)
_CONFIG_LOGS = _Throttle(300)


def _check_error(exc: BaseException) -> str:
    """Classify a JWKS fetch failure for admin health, by its cause."""
    cause = exc.__cause__ or exc
    if isinstance(cause, urllib.error.HTTPError):
        return {403: "forbidden", 404: "not_found", 429: "throttled"}.get(cause.code, "unavailable")
    if isinstance(cause, TimeoutError) or isinstance(getattr(cause, "reason", None), TimeoutError):
        return "timeout"
    return "unavailable"


def _jwks_unavailable(exc: BaseException, uri: str) -> ApiError:
    if _JWKS_ALERTS.ready():
        audit.alert("jwks_fetch_failed", error=type(exc).__name__, failure=_check_error(exc), host=urlparse(uri).hostname)
    return ApiError("auth_unavailable", details={"reason": "jwks"}, retry_after=5)


def jwks_status(timeout_s: float = 5.0) -> tuple[bool, int | None, str | None]:
    """(ok, key count, error class) for admin health, through the same cached
    client that validates tokens, so a fresh cache answers without I/O. A check
    slower than ``timeout_s`` fails as "timeout" even if it got its answer."""
    settings = get_settings()
    if _missing(settings.entra_tenant_id) or not OID_RE.match(settings.entra_tenant_id):
        return False, None, "misconfigured"
    started = time.monotonic()
    try:
        keys = _jwks_client(jwks_uri_for(settings.entra_tenant_id)).get_signing_keys()
    except jwt.PyJWKClientConnectionError as exc:
        return False, None, _check_error(exc)
    except (jwt.PyJWTError, ValueError, OSError):
        return False, None, "failed"
    if time.monotonic() - started > timeout_s:
        return False, len(keys), "timeout"
    return True, len(keys), None


@dataclass(frozen=True)
class Principal:
    """A caller whose token passed every check."""

    # Out of repr: a logged Principal must not carry the oid.
    oid: str = field(repr=False)
    uid: str
    tid: str
    azp: str
    scopes: frozenset[str]
    # The token's roles claim, sorted, exactly as Entra issued it.
    roles: tuple[str, ...]
    expires_at: int
    is_admin: bool


def _invalid(check: str, uid: str | None = None) -> ApiError:
    log.warning("Token rejected: %s%s", check, f" uid={uid}" if uid else "")
    return ApiError("invalid_token")


def _signing_key(kid: str, settings: Settings) -> Any:
    uri = jwks_uri_for(settings.entra_tenant_id)
    try:
        return _jwks_client(uri).get_signing_key(kid)
    except jwt.PyJWKClientConnectionError as exc:
        raise _jwks_unavailable(exc, uri) from None
    except jwt.PyJWKClientError as exc:
        # PyJWT has one class for "no key has this kid" (after the one
        # rate-limited refetch) and for "the endpoint did not return a usable
        # key set". Only the first is the token's fault. PyJWT is pinned
        # exactly, and test_auth pins this message against the real client.
        if str(exc).startswith("Unable to find a signing key"):
            raise _invalid("unknown kid") from None
        raise _jwks_unavailable(exc, uri) from None
    except (jwt.PyJWTError, ValueError, OSError) as exc:
        # A key set that parses to no usable key, or a body that is not JSON
        # (an HTML error page answering 200): the provider's fault.
        raise _jwks_unavailable(exc, uri) from None


def validate_token(raw_token: str, settings: Settings | None = None) -> Principal:
    """Every check from step 2 on; 401 invalid_token on any failure, with
    ``details.reason`` "expired" only for a correctly signed, expired token."""
    s = settings or get_settings()
    try:
        header = jwt.get_unverified_header(raw_token)
    except jwt.PyJWTError as exc:
        raise _invalid(type(exc).__name__) from None
    kid = header.get("kid")
    if header.get("alg") != "RS256":
        raise _invalid("alg")
    if not isinstance(kid, str) or not kid or len(kid) > MAX_KID_CHARS:
        raise _invalid("kid")

    signing_key = _signing_key(kid, s)
    try:
        claims = jwt.decode(
            raw_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=s.entra_api_client_id,
            issuer=issuer_for(s.entra_tenant_id),
            leeway=LEEWAY_SECONDS,
            options={"require": REQUIRED_CLAIMS, "strict_aud": True},
        )
    except jwt.ExpiredSignatureError:
        # Routine: the client refreshes and retries. Not a warning.
        log.info("Token rejected: expired")
        raise ApiError("invalid_token", details={"reason": "expired"}) from None
    except jwt.PyJWTError as exc:
        raise _invalid(type(exc).__name__) from None

    if claims.get("ver") != "2.0":
        raise _invalid("ver")
    tid = claims.get("tid")
    if not isinstance(tid, str) or tid.lower() != s.entra_tenant_id:
        raise _invalid("tid")
    raw_oid = claims.get("oid")
    oid = raw_oid.lower() if isinstance(raw_oid, str) else ""
    if not OID_RE.match(oid):
        raise _invalid("oid")
    # From here the oid is verified, so failures can name the account.
    uid = uid_for(oid, s.hmac_key)
    azp = claims.get("azp")
    azp = azp.lower() if isinstance(azp, str) else ""
    if azp not in s.entra_allowed_client_ids:
        raise _invalid("azp", uid)
    scp = claims.get("scp")
    scopes = frozenset(scp.split()) if isinstance(scp, str) else frozenset()
    if s.entra_required_scope not in scopes:
        raise _invalid("scp", uid)
    raw_roles = claims.get("roles")
    if raw_roles is None:
        raw_roles = []
    if not isinstance(raw_roles, list) or not all(isinstance(r, str) for r in raw_roles):
        raise _invalid("roles", uid)
    roles = tuple(sorted(set(raw_roles)))
    return Principal(
        oid=oid,
        uid=uid,
        tid=tid.lower(),
        azp=azp,
        scopes=scopes,
        roles=roles,
        expires_at=int(claims["exp"]),
        is_admin=s.entra_admin_role in roles,
    )


def _bearer_token(value: str | None) -> str:
    if value is None or not value.strip():
        raise ApiError("unauthenticated")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_AUTH_HEADER_BYTES:
        raise _invalid("header too large")
    # Repeated headers reach the worker joined with ", ": exactly one is allowed,
    # and a JWT never contains a comma.
    if "," in value:
        raise _invalid("several Authorization headers")
    parts = value.split()
    if parts[0].lower() != "bearer":
        # Another scheme is "no bearer token", not a malformed one.
        raise ApiError("unauthenticated")
    if len(parts) != 2:
        raise _invalid("header")
    segments = parts[1].split(".")
    if len(segments) != 3 or not all(_B64URL_SEGMENT.match(segment) for segment in segments):
        raise _invalid("token shape")
    return parts[1]


def authenticate(req: "func.HttpRequest") -> Principal:
    """Configuration, then the header, then the token. Tokens in query
    strings or cookies are not looked at."""
    settings = get_settings()
    problems = config_problems(settings)
    if problems:
        if _CONFIG_LOGS.ready():
            log.error("Auth is closed: settings missing or invalid: %s", ", ".join(problems))
        raise ApiError("auth_unavailable", details={"reason": "config"}, retry_after=60)
    return validate_token(_bearer_token(req.headers.get("Authorization")), settings)


# ---------------------------------------------------------------------------
# Request context and access levels
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuthContext:
    request_id: str
    level: str
    principal: Principal | None
    # The member gate's `me` document, including `_etag` (member and admin).
    me: dict[str, Any] | None


_CONTEXT: contextvars.ContextVar[AuthContext | None] = contextvars.ContextVar("mynews_auth", default=None)


def current() -> AuthContext:
    """The context of the request being handled."""
    ctx = _CONTEXT.get()
    if ctx is None:
        raise RuntimeError("no request context")
    return ctx


def _protect(fn: F, level: str, *, allow_blocked: bool = False, allow_deleting: bool = False) -> F:
    if level not in AUTH_LEVELS:
        raise ValueError(f"unknown auth level {level!r}")

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Imported here rather than at module level: api/ packages import this
        # module, so a top-level import of api.http could close a cycle.
        from api import http

        req = kwargs["req"] if "req" in kwargs else args[0]
        request_id = http.request_id_for(req)
        token = _CONTEXT.set(AuthContext(request_id, level, None, None))
        try:
            if level != "public":
                principal = authenticate(req)
                if level == "admin" and not principal.is_admin:
                    # The role is in the token: answered with no database read.
                    raise ApiError("admin_required")
                me = None
                if level in ("member", "admin"):
                    me = members.gate(
                        principal,
                        allow_blocked=allow_blocked,
                        allow_deleting=allow_deleting,
                        accept_language=req.headers.get("Accept-Language"),
                    )
                _CONTEXT.set(AuthContext(request_id, level, principal, me))
            response = fn(*args, **kwargs)
        except ApiError as exc:
            response = http.error_response(exc)
        except cosmos.Unavailable:
            response = http.error_response(degraded("database"))
        except Exception:
            log.exception("Unhandled error in %s request_id=%s", fn.__name__, request_id)
            response = http.error_response(ApiError("internal"))
        finally:
            _CONTEXT.reset(token)
        return http.with_request_id(response, request_id)

    setattr(wrapper, AUTH_MARKER, level)
    setattr(wrapper, ALLOW_BLOCKED_MARKER, allow_blocked)
    setattr(wrapper, ALLOW_DELETING_MARKER, allow_deleting)
    return wrapper  # type: ignore[return-value]


def public(fn: F) -> F:
    """No credential. Only GET /health (test_route_inventory holds that)."""
    return _protect(fn, "public")


def token(fn: F) -> F:
    """A valid token; no membership needed (POST /me/redeem)."""
    return _protect(fn, "token")


def member(fn: F | None = None, *, allow_blocked: bool = False, allow_deleting: bool = False) -> Any:
    """A valid token and a membership. ``@member`` or, for the routes a blocked
    or deleting member may still use, ``@member(allow_blocked=True, ...)``."""

    def decorate(f: F) -> F:
        return _protect(f, "member", allow_blocked=allow_blocked, allow_deleting=allow_deleting)

    return decorate(fn) if fn is not None else decorate


def admin(fn: F) -> F:
    """A member whose token carries the admin role. An admin's first admin call
    bootstraps the membership."""
    return _protect(fn, "admin")
