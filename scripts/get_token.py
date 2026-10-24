#!/usr/bin/env python3
"""Print an access token for the MyNews API, signed in through the mynews-cli app.

    backend/.venv/bin/python scripts/get_token.py                    # the token, alone, on stdout
    backend/.venv/bin/python scripts/get_token.py --decode           # its header and claims, no signature
    backend/.venv/bin/python scripts/get_token.py --force-refresh    # a new token, e.g. after a role change
    backend/.venv/bin/python scripts/get_token.py --profile test --login     # a second identity
    backend/.venv/bin/python scripts/get_token.py --profile test --sign-out

    TOKEN=$(backend/.venv/bin/python scripts/get_token.py)
    API=$(azd env get-value SERVICE_BACKEND_URI)/api
    curl -sS -H "Authorization: Bearer $TOKEN" "$API/me"

Run it with backend/.venv's Python: msal is in backend/requirements.txt, and the
system python3 has no msal. scripts/mynews_admin.py signs in through the same
functions, so everything below applies to it too.

How it signs in. MSAL's public-client flow (authorization code + PKCE) opens the
system browser on the tenant's hosted page and listens on http://localhost:<free
port> for the answer. Entra ignores the port when it matches a localhost redirect
URI (RFC 8252), which is why mynews-cli registers plain http://localhost.
"Allow public client flows" stays off on that registration: this flow does not
need it, and the flows that would (device code, passwords sent by the app) never
show the hosted page. The authority is https://<ENTRA_TENANT_SUBDOMAIN>.ciamlogin.com/
<ENTRA_TENANT_ID>, the tenant-name host Microsoft documents for apps in external
tenants, and the token is for api://<ENTRA_API_CLIENT_ID>/access_as_user, the one
scope the backend accepts (ENTRA_REQUIRED_SCOPE).

Signing up. External ID tenants reject prompt=create (AADSTS50021, "This is a
self-service sign-up request in CIAM tenant"), so a new identity signs up from the
hosted page's "No account? Create one" link. --login forces that page even while
the browser still holds another identity's session.

Settings come from the environment first, then from the azd environment, read one
key at a time with `azd env get-value` so that MYNEWS_HMAC_KEY is never loaded:
MYNEWS_CLI_CLIENT_ID, ENTRA_TENANT_ID, ENTRA_TENANT_SUBDOMAIN, ENTRA_API_CLIENT_ID,
and optionally ENTRA_REQUIRED_SCOPE. docs/identity.md says where each comes from.

The token cache is one file per profile, ~/.cache/mynews/token-cache-<profile>.json,
0600 in a 0700 directory and outside the repository (which is public). It holds
refresh tokens, which sign in as you for up to 90 days: never copy it, never commit
it. A profile holds exactly one identity; signing in to a profile replaces whoever
it held. --sign-out deletes the file. The browser keeps its own session at
<subdomain>.ciamlogin.com until you sign out there; --login gets past it.

stdout carries the access token and nothing else, so TOKEN=$(...) is safe; every
message goes to stderr. --decode prints the token's header and claims instead,
without the signature, so what it prints is no longer a credential. The token is
never logged, here or by MSAL (whose logging this script leaves unconfigured).

Exit status: 0 ok; 2 configuration or usage; 3 sign-in failed, or was needed but
--silent-only forbade it; 130 interrupted.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path.home() / ".cache" / "mynews"
DEFAULT_PROFILE = "default"
DEFAULT_SCOPE = "access_as_user"

# How long a browser sign-in may take. MSAL's own default waits forever, which
# leaves the command hanging when the tab is simply closed.
INTERACTIVE_TIMEOUT_S = 300
AZD_TIMEOUT_S = 15
# Entra's error_description runs on with trace and correlation ids; the AADSTS
# code and its sentence are what a person needs.
MAX_ERROR_CHARS = 300

GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# The tenant's initial domain label, as in vonmentlennews.ciamlogin.com. Not the
# full vonmentlennews.onmicrosoft.com, and never a URL.
SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SCOPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# A profile name becomes part of a file name, so it is kept to a safe alphabet.
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

EXIT_CONFIG = 2
EXIT_SIGN_IN = 3
EXIT_INTERRUPTED = 130


class ConfigError(Exception):
    """A setting or argument is missing or malformed (exit 2)."""


class SignInError(Exception):
    """No token: the sign-in failed, timed out, or was needed but not allowed (exit 3)."""


@dataclass(frozen=True)
class CliConfig:
    """What a sign-in needs. Every value is an id, none is a secret."""

    tenant_id: str
    subdomain: str
    cli_client_id: str
    api_client_id: str
    scope_name: str = DEFAULT_SCOPE

    @property
    def authority(self) -> str:
        # The tenant id in the path pins MSAL's discovery to this tenant, whose
        # issuer is https://<tid>.ciamlogin.com/<tid>/v2.0 (what core/auth.py checks).
        return f"https://{self.subdomain}.ciamlogin.com/{self.tenant_id}"

    @property
    def scope(self) -> str:
        return f"api://{self.api_client_id}/{self.scope_name}"


# The setting behind each CliConfig field, by its name in core/config.py and
# the azd env. MYNEWS_CLI_CLIENT_ID is the only one the Function App never reads:
# it reaches the backend inside ENTRA_ALLOWED_CLIENT_IDS instead.
SETTING_NAMES = {
    "tenant_id": "ENTRA_TENANT_ID",
    "subdomain": "ENTRA_TENANT_SUBDOMAIN",
    "cli_client_id": "MYNEWS_CLI_CLIENT_ID",
    "api_client_id": "ENTRA_API_CLIENT_ID",
    "scope_name": "ENTRA_REQUIRED_SCOPE",
}
OPTIONAL_FIELDS = frozenset({"scope_name"})

Lookup = Callable[[str, "str | None"], "str | None"]


def note(message: str) -> None:
    """Every human-readable line goes to stderr: stdout is reserved for the token."""
    print(message, file=sys.stderr)


# --- Settings -----------------------------------------------------------------


def azd_value(name: str, env_name: str | None = None) -> str | None:
    """One value from the azd environment, or None when azd or the key is missing.

    `azd env get-value` reads the local .azure/<env>/.env file and makes no Azure
    call. One key per call, on purpose: `azd env get-values` would pull every
    value into this process, MYNEWS_HMAC_KEY included, for nothing. --cwd finds
    the project from any directory; --no-prompt stops azd from asking to create an
    environment when there is none, which would hang a script.
    """
    cmd = ["azd", "env", "get-value", name, "--cwd", str(REPO_ROOT), "--no-prompt"]
    if env_name:
        cmd += ["--environment", env_name]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=AZD_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = done.stdout.strip()
    return value if done.returncode == 0 and value else None


def setting(name: str, env_name: str | None = None, lookup: Lookup = azd_value) -> str | None:
    """The environment wins over the azd env, so one run can point elsewhere."""
    return (os.environ.get(name) or "").strip() or lookup(name, env_name)


def config_problems(cfg: CliConfig) -> list[str]:
    """Setting names whose values cannot be right (names only, like core/auth.py)."""
    problems = []
    for field_name in ("tenant_id", "cli_client_id", "api_client_id"):
        if not GUID_RE.match(getattr(cfg, field_name)):
            problems.append(f"{SETTING_NAMES[field_name]} (not a GUID)")
    if not SUBDOMAIN_RE.match(cfg.subdomain):
        problems.append("ENTRA_TENANT_SUBDOMAIN (the domain label only, e.g. vonmentlennews)")
    if not SCOPE_RE.match(cfg.scope_name):
        problems.append("ENTRA_REQUIRED_SCOPE (a scope name such as access_as_user)")
    return problems


def load_config(env_name: str | None = None, *, lookup: Lookup = azd_value) -> CliConfig:
    """The sign-in settings, validated. ConfigError names what is missing or wrong."""
    values: dict[str, str] = {}
    missing = []
    for field_name, name in SETTING_NAMES.items():
        value = setting(name, env_name, lookup)
        if value:
            values[field_name] = value
        elif field_name not in OPTIONAL_FIELDS:
            missing.append(name)
    if missing:
        raise ConfigError(
            "missing settings: " + ", ".join(missing) + ". Set each in the environment or in the azd env"
            " (azd env set <NAME> <value>); docs/identity.md lists where they come from."
        )
    cfg = CliConfig(
        tenant_id=values["tenant_id"].lower(),
        subdomain=values["subdomain"].lower(),
        cli_client_id=values["cli_client_id"].lower(),
        api_client_id=values["api_client_id"].lower(),
        scope_name=values.get("scope_name", DEFAULT_SCOPE),
    )
    problems = config_problems(cfg)
    if problems:
        raise ConfigError("malformed settings: " + "; ".join(problems))
    return cfg


# --- The token cache file -----------------------------------------------------


def cache_path(profile: str, cache_dir: Path | None = None) -> Path:
    if not PROFILE_RE.match(profile):
        raise ConfigError(f"profile names are 1-32 of a-z, 0-9, '-' and '_', starting with a letter or digit: {profile!r}")
    return (cache_dir or CACHE_DIR) / f"token-cache-{profile}.json"


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        os.chmod(path, 0o700)


def read_private(path: Path) -> str | None:
    """The file's text, or None when absent. A cache others can read is tightened first:
    it holds refresh tokens, and a wider mode can only come from outside this script."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    if mode & 0o077:
        os.chmod(path, 0o600)
        note(f"get_token: {path} was readable by others; it is 0600 now")
    return path.read_text(encoding="utf-8")


def write_private(path: Path, text: str) -> None:
    """Replace path atomically with a 0600 file. mkstemp creates 0600 whatever the
    umask, so there is no moment in which the tokens sit in a wider file."""
    _private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def sign_out(profile: str, cache_dir: Path | None = None) -> bool:
    """Forget the profile's identity. Deleting the file drops every token in it."""
    try:
        cache_path(profile, cache_dir).unlink()
    except FileNotFoundError:
        return False
    return True


# --- Signing in ---------------------------------------------------------------


@contextlib.contextmanager
def stdout_to_stderr() -> Iterator[None]:
    """Route whatever is written to stdout during a sign-in to stderr instead.

    `TOKEN=$(get_token.py)` must capture the token and nothing else. MSAL prints
    nothing itself, but launching the browser runs helper processes that inherit
    file descriptor 1, so the swap is made on the descriptor as well as on
    sys.stdout. sys.__stdout__ is flushed before fd 1 comes back, so not even its
    buffer can carry a stray line onto the real stdout.
    """
    sys.stdout.flush()
    try:
        saved = os.dup(1)
    except OSError:  # no usable fd 1 (an unusual embedding); sys.stdout is still swapped
        saved = None
    try:
        if saved is not None:
            os.dup2(2, 1)
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        if saved is not None:
            with contextlib.suppress(Exception):
                if sys.__stdout__ is not None:
                    sys.__stdout__.flush()
            os.dup2(saved, 1)
            os.close(saved)


def describe_error(result: dict[str, Any]) -> str:
    """'<error>: <first line of error_description>', short: MSAL's errors carry the
    AADSTS code a person can look up, then trace ids nobody needs on screen."""
    error = str(result.get("error") or "unknown_error")
    description = str(result.get("error_description") or "").strip().splitlines()
    first = description[0].split(" Trace ID:")[0].strip() if description else ""
    text = f"{error}: {first}" if first else error
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."


def _import_msal() -> Any:
    try:
        import msal
    except ImportError:
        raise ConfigError(
            "msal is not installed for this Python. Run the script with backend/.venv/bin/python"
            " (uv pip install --python backend/.venv/bin/python -r backend/requirements.txt)."
        ) from None
    return msal


def _silent(app: Any, cfg: CliConfig, *, force_refresh: bool) -> dict[str, Any] | None:
    """A token from the cache, or from the refresh token; None means 'sign in'.

    force_refresh skips the cached access token and spends the refresh token: that
    is how a new role assignment reaches the token (Entra stamps roles at issue),
    and how the refresh soak in docs/identity.md tests the refresh token itself.
    """
    accounts = app.get_accounts()
    if not accounts:
        return None
    result = app.acquire_token_silent_with_error([cfg.scope], account=accounts[0], force_refresh=force_refresh)
    if result and "access_token" in result:
        return result
    if result:
        # Worth seeing: an expired or revoked refresh token says so here, and that
        # line is the result the refresh soak records.
        note(f"get_token: silent refresh failed: {describe_error(result)}")
    return None


def _keep_only(app: Any, result: dict[str, Any]) -> None:
    """A profile is one identity: forget any other account its cache still holds."""
    oid = (result.get("id_token_claims") or {}).get("oid")
    if not oid:
        return
    for account in app.get_accounts():
        if account.get("local_account_id") != oid:
            app.remove_account(account)
            note("get_token: the profile's previous identity was replaced")


def _interactive(app: Any, cfg: CliConfig, *, profile: str, login: bool) -> dict[str, Any]:
    # select_account shows the hosted page's account picker even when the browser
    # could sign in silently, so a fresh profile never binds to whichever identity
    # the browser happens to hold. login asks for credentials outright.
    prompt = "login" if login else "select_account"

    def announce(ui: str = "browser", **_: Any) -> None:
        note(f"get_token: opening the {ui} to sign in to {cfg.subdomain}.ciamlogin.com (profile '{profile}')")

    with stdout_to_stderr():
        result = app.acquire_token_interactive(
            [cfg.scope], prompt=prompt, timeout=INTERACTIVE_TIMEOUT_S, on_before_launching_ui=announce
        )
    if "access_token" not in result:
        raise SignInError(f"sign-in failed: {describe_error(result)}")
    _keep_only(app, result)
    return result


def acquire_token(
    cfg: CliConfig,
    *,
    profile: str = DEFAULT_PROFILE,
    login: bool = False,
    force_refresh: bool = False,
    silent_only: bool = False,
    cache_dir: Path | None = None,
) -> str:
    """An access token for the MyNews API: cached, refreshed, or from a browser sign-in."""
    if login and silent_only:
        raise ConfigError("--login and --silent-only contradict each other")
    msal = _import_msal()
    path = cache_path(profile, cache_dir)
    cache = msal.SerializableTokenCache()
    text = read_private(path)
    if text:
        try:
            cache.deserialize(text)
        except ValueError:
            note(f"get_token: {path} is not a token cache; starting a new one")
    try:
        # Construction fetches the tenant's OpenID configuration, so this is
        # where an unreachable or misnamed tenant shows up.
        app = msal.PublicClientApplication(cfg.cli_client_id, authority=cfg.authority, token_cache=cache)
    except Exception as exc:  # requests' errors, or msal's ValueError for an unknown authority
        raise SignInError(f"cannot reach the tenant at {cfg.authority} ({type(exc).__name__})") from None
    try:
        result = None if login else _silent(app, cfg, force_refresh=force_refresh)
        if result is None:
            if silent_only:
                raise SignInError(f"profile '{profile}' has no sign-in that refreshes silently; run without --silent-only")
            result = _interactive(app, cfg, profile=profile, login=login)
        return str(result["access_token"])
    finally:
        if cache.has_state_changed:
            write_private(path, cache.serialize())


# --- Reading a token ----------------------------------------------------------


def _b64url_json(segment: str) -> Any:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))


def decode_unverified(token: str, *, now: float | None = None) -> dict[str, Any]:
    """A JWT's header and claims, for reading only. The signature is dropped and
    never checked: verifying is the backend's job (core/auth.py)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise SignInError("the access token is not a JWT (it should have three segments)")
    try:
        decoded: dict[str, Any] = {"header": _b64url_json(parts[0]), "claims": _b64url_json(parts[1])}
    except (ValueError, UnicodeError) as exc:  # binascii.Error and JSONDecodeError are ValueErrors
        raise SignInError(f"the access token does not decode ({type(exc).__name__})") from None
    exp = decoded["claims"].get("exp") if isinstance(decoded["claims"], dict) else None
    if isinstance(exp, int):
        decoded["expiresInSeconds"] = exp - int(time.time() if now is None else now)
    return decoded


# --- Command line -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="get_token.py",
        description="Print an access token for the MyNews API (stdout carries only the token).",
        epilog="Settings: MYNEWS_CLI_CLIENT_ID, ENTRA_TENANT_ID, ENTRA_TENANT_SUBDOMAIN and ENTRA_API_CLIENT_ID,"
        " from the environment or the azd env. See docs/identity.md.",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE,
                        help="which cached identity to use, e.g. 'test' for a non-admin account (default: %(default)s)")
    parser.add_argument("--env", default=os.environ.get("AZURE_ENV_NAME") or None, metavar="NAME",
                        help="azd environment to read settings from (default: azd's default environment)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login", action="store_true",
                      help="sign in through the browser even if the profile holds a token (prompt=login)")
    mode.add_argument("--silent-only", action="store_true",
                      help="never open the browser: exit 3 if the cache and refresh token cannot produce a token")
    mode.add_argument("--sign-out", action="store_true", help="delete the profile's token cache and exit")
    parser.add_argument("--force-refresh", action="store_true",
                        help="skip the cached access token and use the refresh token (picks up new roles)")
    parser.add_argument("--decode", action="store_true",
                        help="print the token's header and claims as JSON instead of the token (no signature)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.sign_out:
            removed = sign_out(args.profile)
            note(f"get_token: profile '{args.profile}' " + ("signed out (token cache deleted)" if removed else "held no sign-in"))
            return 0
        cfg = load_config(args.env)
        token = acquire_token(cfg, profile=args.profile, login=args.login,
                              force_refresh=args.force_refresh, silent_only=args.silent_only)
        output = json.dumps(decode_unverified(token), indent=2, sort_keys=True) if args.decode else token
    except ConfigError as exc:
        note(f"get_token: {exc}")
        return EXIT_CONFIG
    except SignInError as exc:
        note(f"get_token: {exc}")
        return EXIT_SIGN_IN
    except KeyboardInterrupt:
        note("get_token: interrupted")
        return EXIT_INTERRUPTED
    sys.stdout.write(output + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
