"""core.auth: configuration fails closed, the negative token matrix, the JWKS
failure modes, and the access-level decorators.

Tokens are real RS256 JWTs signed with a test key; only the key endpoint is
fake (tests/fakes/jwks.py), so every check runs through PyJWT exactly as it
does in production.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import inspect
import json
import shutil
import subprocess
import time
import typing
import urllib.error
from typing import Any

import azure.functions as func
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from core import auth
from core.errors import ApiError
from tests.fakes import jwks as ids
from tests.fakes.cosmos import FakeCosmos
from tests.fakes.jwks import REMOVE, TokenFactory, raw_token


@pytest.fixture(autouse=True)
def _fresh_throttles(monkeypatch: pytest.MonkeyPatch) -> None:
    # The per-process "once per 5 minutes" throttles must not carry state
    # from one test into the next.
    monkeypatch.setattr(auth, "_JWKS_ALERTS", auth._Throttle(300))
    monkeypatch.setattr(auth, "_CONFIG_LOGS", auth._Throttle(300))


def _request(headers: dict[str, str] | None = None, *, url: str = "http://localhost/api/me", method: str = "GET") -> func.HttpRequest:
    return func.HttpRequest(method=method, url=url, headers=headers or {}, body=b"")


def _rejection(token: str) -> ApiError:
    with pytest.raises(ApiError) as caught:
        auth.validate_token(token)
    return caught.value


class TestConfiguration:
    def test_the_test_environment_is_complete(self) -> None:
        assert auth.config_problems() == ()

    @pytest.mark.parametrize("name", auth.AUTH_SETTING_NAMES)
    def test_each_missing_setting_is_named(self, env, name: str) -> None:
        env(**{name: None})
        problems = auth.config_problems()
        # Scope and role have defaults, so deleting them is not a problem...
        if name in ("ENTRA_REQUIRED_SCOPE", "ENTRA_ADMIN_ROLE"):
            assert problems == ()
        else:
            assert problems == (name,)

    @pytest.mark.parametrize("name", auth.AUTH_SETTING_NAMES)
    def test_an_unresolved_key_vault_reference_counts_as_missing(self, env, name: str) -> None:
        # ...but a reference that did not resolve never falls back to a default.
        env(**{name: "@Microsoft.KeyVault(SecretUri=https://kv.vault.azure.net/secrets/x)"})
        assert auth.config_problems() == (name,)

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("ENTRA_TENANT_ID", "vonmentlennews"),
            ("ENTRA_API_CLIENT_ID", "api://mynews"),
            ("ENTRA_ALLOWED_CLIENT_IDS", f"{ids.IOS_CLIENT_ID},not-a-guid"),
            ("ENTRA_ALLOWED_CLIENT_IDS", " , "),
            ("ENTRA_REQUIRED_SCOPE", "access_as_user other"),
        ],
    )
    def test_malformed_values_are_problems(self, env, name: str, value: str) -> None:
        env(**{name: value})
        assert auth.config_problems() == (name,)

    def test_ids_are_compared_case_blind(self, env) -> None:
        env(ENTRA_TENANT_ID=ids.TENANT_ID.upper(), ENTRA_ALLOWED_CLIENT_IDS=ids.IOS_CLIENT_ID.upper())
        assert auth.config_problems() == ()

    def test_every_non_public_route_fails_closed(self, env, caplog: pytest.LogCaptureFixture) -> None:
        env(ENTRA_TENANT_ID=None, MYNEWS_HMAC_KEY=None)
        with caplog.at_level("ERROR", logger="core.auth"), pytest.raises(ApiError) as caught:
            auth.authenticate(_request({"Authorization": "Bearer a.b.c"}))
        assert caught.value.code == "auth_unavailable"
        assert caught.value.status == 503
        assert caught.value.details == {"reason": "config"}
        assert caught.value.retry_after == 60
        # Names, never values.
        assert "ENTRA_TENANT_ID" in caplog.text and "MYNEWS_HMAC_KEY" in caplog.text
        assert ids.API_CLIENT_ID not in caplog.text and ids.IOS_CLIENT_ID not in caplog.text

    def test_the_config_error_is_logged_once_per_interval(self, env, caplog: pytest.LogCaptureFixture) -> None:
        env(ENTRA_TENANT_ID=None)
        with caplog.at_level("ERROR", logger="core.auth"):
            for _ in range(5):
                with pytest.raises(ApiError):
                    auth.authenticate(_request())
        assert caplog.text.count("Auth is closed") == 1


class TestUid:
    def test_is_the_documented_hmac(self) -> None:
        oid = ids.OID_ALICE
        expected = hmac.new(ids.HMAC_KEY.encode(), oid.encode(), hashlib.sha256).hexdigest()[:32]
        assert auth.uid_for(oid) == expected
        assert auth.uid_for(oid.upper()) == expected

    def test_depends_on_the_key(self) -> None:
        assert auth.uid_for(ids.OID_ALICE, "another key") != auth.uid_for(ids.OID_ALICE)

    @pytest.mark.parametrize("key", ["", "@Microsoft.KeyVault(SecretUri=https://kv/secrets/k)"])
    def test_no_key_closes_auth(self, key: str) -> None:
        with pytest.raises(ApiError) as caught:
            auth.uid_for(ids.OID_ALICE, key)
        assert (caught.value.code, caught.value.details, caught.value.retry_after) == ("auth_unavailable", {"reason": "config"}, 60)

    def test_fingerprint_matches_the_documented_recipe(self) -> None:
        expected = hashlib.sha256(("mynews-hmac-fp:" + ids.HMAC_KEY).encode()).hexdigest()[:12]
        assert auth.hmac_key_fingerprint(ids.HMAC_KEY) == expected

    @pytest.mark.skipif(shutil.which("shasum") is None, reason="needs shasum")
    def test_fingerprint_matches_postprovision_sh(self) -> None:
        # The exact pipeline scripts/postprovision.sh runs.
        shell = subprocess.run(
            ["sh", "-c", 'printf \'%s\' "mynews-hmac-fp:$KEY" | shasum -a 256 | cut -c1-12'],
            env={"KEY": ids.HMAC_KEY, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert shell.stdout.strip() == auth.hmac_key_fingerprint(ids.HMAC_KEY)


class TestValidToken:
    def test_a_good_token_yields_the_principal(self, jwks) -> None:
        principal = auth.validate_token(jwks.token(ids.OID_ALICE, roles=["reader", "admin", "admin"]))
        assert principal.oid == ids.OID_ALICE
        assert principal.uid == auth.uid_for(ids.OID_ALICE)
        assert principal.tid == ids.TENANT_ID
        assert principal.azp == ids.IOS_CLIENT_ID
        assert principal.scopes == frozenset({"access_as_user"})
        assert principal.roles == ("admin", "reader")
        assert principal.is_admin is True
        assert jwks.uris == [f"https://{ids.TENANT_ID}.ciamlogin.com/{ids.TENANT_ID}/discovery/v2.0/keys"]

    def test_no_roles_is_not_admin(self, jwks) -> None:
        principal = auth.validate_token(jwks.token())
        assert principal.roles == () and principal.is_admin is False

    def test_the_cli_client_is_allowed_too(self, jwks) -> None:
        assert auth.validate_token(jwks.token(azp=ids.CLI_CLIENT_ID)).azp == ids.CLI_CLIENT_ID

    def test_an_upper_case_oid_is_normalised(self, jwks) -> None:
        assert auth.validate_token(jwks.token(ids.OID_ALICE.upper())).oid == ids.OID_ALICE

    def test_extra_scopes_are_fine(self, jwks) -> None:
        principal = auth.validate_token(jwks.token(scp="openid access_as_user profile"))
        assert "access_as_user" in principal.scopes

    def test_clock_skew_inside_the_leeway_is_accepted(self, jwks) -> None:
        now = int(time.time())
        auth.validate_token(jwks.token(exp=now - 30, nbf=now + 30, iat=now + 30))

    def test_the_principal_repr_hides_the_oid(self, jwks) -> None:
        assert ids.OID_ALICE not in repr(auth.validate_token(jwks.token()))

    def test_the_admin_role_name_is_a_setting(self, jwks, env) -> None:
        env(ENTRA_ADMIN_ROLE="owner")
        assert auth.validate_token(jwks.token(roles=["admin"])).is_admin is False
        assert auth.validate_token(jwks.token(roles=["owner"])).is_admin is True


def _other_key() -> rsa.RSAPrivateKey:
    return ids.private_key("attacker")


NOW = int(time.time())
NEGATIVE_MATRIX: list[tuple[str, dict[str, Any]]] = [
    # iss: exact, and only the tenant-id form.
    ("iss of another tenant", {"iss": "https://99999999-9999-4999-8999-999999999999.ciamlogin.com/99999999-9999-4999-8999-999999999999/v2.0"}),
    ("iss through the name host", {"iss": f"https://vonmentlennews.ciamlogin.com/{ids.TENANT_ID}/v2.0"}),
    ("iss v1 form", {"iss": f"https://sts.windows.net/{ids.TENANT_ID}/"}),
    ("iss with a trailing slash", {"iss": f"https://{ids.TENANT_ID}.ciamlogin.com/{ids.TENANT_ID}/v2.0/"}),
    ("iss missing", {"iss": REMOVE}),
    # aud: one string, the API's client id.
    ("aud of another app", {"aud": ids.OTHER_CLIENT_ID}),
    ("aud as the App ID URI", {"aud": f"api://{ids.API_CLIENT_ID}"}),
    ("aud as a list", {"aud": [ids.API_CLIENT_ID]}),
    ("aud missing", {"aud": REMOVE}),
    # exp / nbf / iat.
    ("exp past the leeway", {"exp": NOW - 120}),
    ("exp missing", {"exp": REMOVE}),
    ("nbf in the future", {"nbf": NOW + 600}),
    ("nbf missing", {"nbf": REMOVE}),
    ("iat in the future", {"iat": NOW + 600}),
    ("iat missing", {"iat": REMOVE}),
    # ver / tid / oid.
    ("ver 1.0", {"ver": "1.0"}),
    ("ver missing", {"ver": REMOVE}),
    ("tid of another tenant", {"tid": "99999999-9999-4999-8999-999999999999"}),
    ("tid missing", {"tid": REMOVE}),
    ("oid missing", {"oid": REMOVE}),
    ("oid not a guid", {"oid": "not-a-guid"}),
    ("oid not a string", {"oid": 12345}),
    # azp.
    ("azp not allowed", {"azp": ids.OTHER_CLIENT_ID}),
    ("azp of the api itself", {"azp": ids.API_CLIENT_ID}),
    ("azp missing", {"azp": REMOVE}),
    # scp: delegated access_as_user only; app-only tokens have no scp.
    ("scp missing (app-only token)", {"scp": REMOVE}),
    ("scp without the scope", {"scp": "openid profile"}),
    ("scp with a lookalike", {"scp": "access_as_user.write"}),
    ("scp not a string", {"scp": ["access_as_user"]}),
    ("roles not a list", {"roles": "admin"}),
]


class TestNegativeMatrix:
    @pytest.mark.parametrize(("case", "overrides"), NEGATIVE_MATRIX, ids=[c for c, _ in NEGATIVE_MATRIX])
    def test_claim_problems_are_invalid_token(self, jwks, case: str, overrides: dict[str, Any]) -> None:
        error = _rejection(jwks.token(**overrides))
        assert error.code == "invalid_token" and error.status == 401
        # "expired" is the only detail ever revealed, and only for expiry.
        assert error.details == ({"reason": "expired"} if case == "exp past the leeway" else {})

    def test_alg_none_is_refused_before_any_key_lookup(self, jwks) -> None:
        error = _rejection(jwks.token(algorithm="none"))
        assert error.code == "invalid_token"
        assert jwks.client.lookups == []

    def test_hs256_is_refused_before_any_key_lookup(self, jwks) -> None:
        # The classic confusion attack signs HS256 with the public key as the
        # secret; the algorithm is never taken from the token.
        error = _rejection(jwks.token(algorithm="HS256", key="a-shared-secret-of-sufficient-length"))
        assert error.code == "invalid_token"
        assert jwks.client.lookups == []

    def test_rs512_is_refused(self, jwks) -> None:
        assert _rejection(jwks.token(algorithm="RS512")).code == "invalid_token"

    def test_a_header_that_lies_about_its_alg_is_refused(self, jwks) -> None:
        # RS256 in the header, an HMAC in the signature: the signature is
        # checked with the algorithm the server chose, against the RSA key.
        token = raw_token(
            {"alg": "RS256", "kid": ids.KID, "typ": "JWT"},
            jwks.tokens.claims(),
            hmac_secret="a-shared-secret-of-sufficient-length",
        )
        assert _rejection(token).code == "invalid_token"
        assert jwks.client.lookups == [ids.KID]

    @pytest.mark.parametrize("kid", [REMOVE, "", "k" * 129])
    def test_kid_must_be_present_and_sane(self, jwks, kid: Any) -> None:
        assert _rejection(jwks.token(header={"kid": kid})).code == "invalid_token"

    def test_a_non_string_kid_is_refused(self, jwks) -> None:
        token = raw_token({"alg": "RS256", "kid": 7, "typ": "JWT"}, jwks.tokens.claims())
        assert _rejection(token).code == "invalid_token"
        assert jwks.client.lookups == []

    def test_an_unknown_kid_is_invalid_token(self, jwks) -> None:
        assert _rejection(jwks.token(header={"kid": "rotated-away"})).code == "invalid_token"

    def test_a_signature_by_another_key_is_refused(self, jwks) -> None:
        forged = TokenFactory(signing_key=_other_key()).token()
        assert _rejection(forged).code == "invalid_token"

    def test_a_tampered_payload_is_refused(self, jwks) -> None:
        header, payload, signature = jwks.token().split(".")
        claims = json.loads(jwt.utils.base64url_decode(payload))
        claims["roles"] = ["admin"]
        forged_payload = jwt.utils.base64url_encode(json.dumps(claims).encode()).decode()
        assert _rejection(f"{header}.{forged_payload}.{signature}").code == "invalid_token"

    def test_expired_and_forged_reveals_nothing(self, jwks) -> None:
        forged = TokenFactory(signing_key=_other_key()).token(exp=NOW - 3600)
        assert _rejection(forged).details == {}

    def test_garbage_is_invalid_token(self, jwks) -> None:
        assert _rejection("not.a.jwt").code == "invalid_token"

    def test_the_token_is_never_logged(self, jwks, caplog: pytest.LogCaptureFixture) -> None:
        token = jwks.token(azp=ids.OTHER_CLIENT_ID)
        with caplog.at_level("DEBUG"):
            _rejection(token)
        assert token not in caplog.text
        assert token.split(".")[1] not in caplog.text
        assert ids.OID_ALICE not in caplog.text
        assert "azp" in caplog.text


class TestHeader:
    def test_missing_is_unauthenticated(self, jwks) -> None:
        with pytest.raises(ApiError) as caught:
            auth.authenticate(_request())
        assert caught.value.code == "unauthenticated"

    def test_a_token_in_the_query_string_is_ignored(self, jwks) -> None:
        with pytest.raises(ApiError) as caught:
            auth.authenticate(_request(url=f"http://localhost/api/me?access_token={jwks.token()}"))
        assert caught.value.code == "unauthenticated"

    def test_another_scheme_is_unauthenticated(self, jwks) -> None:
        with pytest.raises(ApiError) as caught:
            auth.authenticate(_request({"Authorization": "Basic dXNlcjpwYXNz"}))
        assert caught.value.code == "unauthenticated"

    @pytest.mark.parametrize(
        "value",
        [
            "Bearer",
            "Bearer a.b",
            "Bearer a.b.c.d",
            "Bearer a..c",
            "Bearer x y",
            "Bearer a.b.c, Bearer d.e.f",
            "Bearer " + "a" * 8200 + ".b.c",
        ],
    )
    def test_malformed_is_invalid_token(self, jwks, value: str) -> None:
        with pytest.raises(ApiError) as caught:
            auth.authenticate(_request({"Authorization": value}))
        assert caught.value.code == "invalid_token"

    def test_the_scheme_is_case_blind(self, jwks) -> None:
        assert auth.authenticate(_request({"Authorization": f"bearer {jwks.token()}"})).oid == ids.OID_ALICE


class TestJwksFailures:
    def test_a_fetch_failure_is_auth_unavailable_and_alerts_once(self, jwks, caplog: pytest.LogCaptureFixture) -> None:
        jwks.client.error = jwt.PyJWKClientConnectionError("Fail to fetch data from the url")
        with caplog.at_level("ERROR"):
            for _ in range(3):
                error = _rejection(jwks.token())
                assert (error.code, error.status, error.details, error.retry_after) == (
                    "auth_unavailable",
                    503,
                    {"reason": "jwks"},
                    5,
                )
        assert caplog.text.count("ALERT jwks_fetch_failed") == 1

    @pytest.mark.parametrize(
        "error",
        [
            jwt.PyJWKClientError("The JWKS endpoint did not return a JSON object"),
            jwt.PyJWKSetError("The JWK Set did not contain any usable keys"),
            ValueError("Expecting value: line 1 column 1 (char 0)"),
        ],
    )
    def test_an_unusable_key_set_is_the_providers_fault(self, jwks, error: Exception) -> None:
        jwks.client.error = error
        assert _rejection(jwks.token()).details == {"reason": "jwks"}

    def test_the_real_client_unknown_kid_message_is_what_auth_expects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pins PyJWT 2.15.1's wording, which _signing_key() relies on to tell
        # "no such kid" (the token's fault) from "no usable key set" (Entra's).
        public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(ids.private_key().public_key(), as_dict=True)

        class OfflineClient(jwt.PyJWKClient):
            def fetch_data(self) -> Any:
                data = {"keys": [{**public_jwk, "kid": ids.KID, "use": "sig"}]}
                self.jwk_set_cache.put(data)
                self._last_successful_fetch = time.monotonic()
                return data

        client = OfflineClient(auth.jwks_uri_for(ids.TENANT_ID), cache_keys=True, lifespan=60)
        monkeypatch.setattr(auth, "_jwks_client", lambda uri: client)
        assert auth.validate_token(ids_token()).oid == ids.OID_ALICE
        error = _rejection(TokenFactory(kid="rotated-away").token())
        assert error.code == "invalid_token"

    def test_jwks_status_reports_keys(self, jwks) -> None:
        assert auth.jwks_status() == (True, 1, None)

    @pytest.mark.parametrize(
        ("cause", "kind"),
        [
            (TimeoutError("timed out"), "timeout"),
            (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
            (urllib.error.HTTPError("u", 403, "Forbidden", None, None), "forbidden"),  # type: ignore[arg-type]
            (urllib.error.HTTPError("u", 429, "Too Many", None, None), "throttled"),  # type: ignore[arg-type]
            (urllib.error.URLError("no route"), "unavailable"),
        ],
    )
    def test_jwks_status_classifies_failures(self, jwks, cause: Exception, kind: str) -> None:
        error = jwt.PyJWKClientConnectionError("Fail to fetch data from the url")
        error.__cause__ = cause
        jwks.client.error = error
        assert auth.jwks_status() == (False, None, kind)

    def test_jwks_status_without_a_tenant_is_misconfigured(self, jwks, env) -> None:
        env(ENTRA_TENANT_ID=None)
        assert auth.jwks_status() == (False, None, "misconfigured")


def ids_token(**overrides: Any) -> str:
    return TokenFactory().token(**overrides)


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
def _body(resp: func.HttpResponse) -> dict[str, Any]:
    return json.loads(resp.get_body())


@auth.public
def _public_handler(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("ok")


@auth.token
def _token_handler(req: func.HttpRequest) -> func.HttpResponse:
    ctx = auth.current()
    return func.HttpResponse(json.dumps({"uid": ctx.principal.uid, "level": ctx.level, "me": ctx.me}))


@auth.member
def _member_handler(req: func.HttpRequest) -> func.HttpResponse:
    ctx = auth.current()
    return func.HttpResponse(json.dumps({"status": ctx.me["status"], "etag": bool(ctx.me.get("_etag"))}))


@auth.member(allow_blocked=True, allow_deleting=True)
def _lenient_member_handler(req: func.HttpRequest, purge: Any = None) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"status": auth.current().me["status"]}))


@auth.admin
def _admin_handler(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"admin": auth.current().principal.is_admin}))


def _seed_member(fake: FakeCosmos, oid: str, status: str = "active") -> None:
    fake.put(
        {
            "id": "me",
            "pk": f"user|{oid}",
            "type": "me",
            "v": 1,
            "status": status,
            "created_at": "2026-10-01T10:00:00Z",
            "updated_at": "2026-10-01T10:00:00Z",
            "locale": "en",
            "languages": ["en"],
            "last_active_at": "2026-10-01T10:00:00Z",
            "is_admin": False,
        }
    )


def _api_http_exists() -> bool:
    """Whether api/http.py (the response helpers the decorators render with)
    is on the path. It is written alongside core/ by the API work; until it
    exists the decorators cannot answer, so their tests wait for it. Once the
    file exists, an error inside it fails these tests instead of skipping them."""
    try:
        return importlib.util.find_spec("api.http") is not None
    except ModuleNotFoundError:  # no `api` package at all
        return False


needs_api_http = pytest.mark.skipif(not _api_http_exists(), reason="api/http.py does not exist yet")


class TestDecorators:
    def test_markers_and_signatures(self) -> None:
        expectations = [
            (_public_handler, "public", False, False),
            (_token_handler, "token", False, False),
            (_member_handler, "member", False, False),
            (_lenient_member_handler, "member", True, True),
            (_admin_handler, "admin", False, False),
        ]
        for handler, level, blocked, deleting in expectations:
            assert getattr(handler, auth.AUTH_MARKER) == level
            assert getattr(handler, auth.ALLOW_BLOCKED_MARKER) is blocked
            assert getattr(handler, auth.ALLOW_DELETING_MARKER) is deleting
        # The Functions worker binds parameters by the visible signature, and
        # reads their types from the hints, which (with postponed annotations)
        # must resolve in the handler's module, not in core.auth.
        assert list(inspect.signature(_lenient_member_handler).parameters) == ["req", "purge"]
        assert typing.get_type_hints(_lenient_member_handler) == {
            "req": func.HttpRequest,
            "purge": Any,
            "return": func.HttpResponse,
        }
        assert _member_handler.__name__ == "_member_handler"

    @needs_api_http
    def test_public_needs_nothing_and_echoes_a_valid_request_id(self, env) -> None:
        env(ENTRA_TENANT_ID=None)  # closed auth does not close the public route
        resp = _public_handler(_request({"X-Request-Id": "req-12345678"}))
        assert resp.status_code == 200
        assert resp.headers["X-Request-Id"] == "req-12345678"

    @needs_api_http
    def test_an_unusable_request_id_is_replaced(self) -> None:
        resp = _public_handler(_request({"X-Request-Id": "bad id with spaces"}))
        assert resp.headers["X-Request-Id"] != "bad id with spaces"
        assert len(resp.headers["X-Request-Id"]) == 36

    @needs_api_http
    def test_token_level_401_is_an_envelope(self, jwks) -> None:
        resp = _token_handler(_request())
        assert resp.status_code == 401
        assert _body(resp)["error"]["code"] == "unauthenticated"
        assert resp.headers["WWW-Authenticate"].startswith('Bearer realm="mynews"')
        assert resp.headers["X-Request-Id"]

    @needs_api_http
    def test_token_level_sets_the_context_and_resets_it(self, jwks, fake_cosmos: FakeCosmos) -> None:
        resp = _token_handler(req=_request(jwks.bearer()))
        assert resp.status_code == 200
        assert _body(resp) == {"uid": auth.uid_for(ids.OID_ALICE), "level": "token", "me": None}
        # No membership needed, and none read.
        assert fake_cosmos.journal == []
        with pytest.raises(RuntimeError):
            auth.current()

    @needs_api_http
    def test_closed_config_is_503_on_every_non_public_level(self, jwks, env, fake_cosmos: FakeCosmos) -> None:
        env(ENTRA_API_CLIENT_ID=None)
        for handler in (_token_handler, _member_handler, _admin_handler):
            resp = handler(_request(jwks.bearer()))
            assert resp.status_code == 503
            assert _body(resp)["error"] == {
                "code": "auth_unavailable",
                "message": _body(resp)["error"]["message"],
                "details": {"reason": "config"},
            }
            assert resp.headers["Retry-After"] == "60"

    @needs_api_http
    def test_member_level_without_membership_is_invite_required(self, jwks, fake_cosmos: FakeCosmos) -> None:
        resp = _member_handler(_request(jwks.bearer()))
        assert resp.status_code == 403
        assert _body(resp)["error"]["code"] == "invite_required"

    @needs_api_http
    def test_member_level_hands_the_gate_document_to_the_handler(self, jwks, fake_cosmos: FakeCosmos) -> None:
        _seed_member(fake_cosmos, ids.OID_ALICE)
        resp = _member_handler(_request(jwks.bearer()))
        assert _body(resp) == {"status": "active", "etag": True}

    @needs_api_http
    @pytest.mark.parametrize(("status", "code", "http"), [("blocked", "account_blocked", 403), ("deleting", "account_deleted", 410)])
    def test_blocked_and_deleting_members_are_refused(self, jwks, fake_cosmos: FakeCosmos, status: str, code: str, http: int) -> None:
        _seed_member(fake_cosmos, ids.OID_ALICE, status)
        resp = _member_handler(_request(jwks.bearer()))
        assert (resp.status_code, _body(resp)["error"]["code"]) == (http, code)
        # ...unless the route allows them (export, DELETE /me).
        assert _body(_lenient_member_handler(_request(jwks.bearer())))["status"] == status

    @needs_api_http
    def test_admin_level_refuses_a_non_admin_without_any_read(self, jwks, fake_cosmos: FakeCosmos) -> None:
        resp = _admin_handler(_request(jwks.bearer()))
        assert (resp.status_code, _body(resp)["error"]["code"]) == (403, "admin_required")
        assert fake_cosmos.journal == []

    @needs_api_http
    def test_admin_level_bootstraps_the_admins_membership(self, jwks, fake_cosmos: FakeCosmos) -> None:
        resp = _admin_handler(_request(jwks.bearer(ids.OID_ADMIN, roles=["admin"])))
        assert resp.status_code == 200
        assert fake_cosmos.get("me", f"user|{ids.OID_ADMIN}")["invite_id"] == "bootstrap-admin"

    @needs_api_http
    def test_handler_errors_become_envelopes(self, jwks, fake_cosmos: FakeCosmos, caplog: pytest.LogCaptureFixture) -> None:
        from core import cosmos

        @auth.token
        def raises_api_error(req: func.HttpRequest) -> func.HttpResponse:
            raise ApiError("not_found", details={"resource": "story"})

        @auth.token
        def raises_unavailable(req: func.HttpRequest) -> func.HttpResponse:
            raise cosmos.Unavailable("read: HTTP 429")

        @auth.token
        def crashes(req: func.HttpRequest) -> func.HttpResponse:
            raise RuntimeError("secret internals /dbs/mynews/colls/news")

        req = _request({**jwks.bearer(), "X-Request-Id": "trace-abcdef12"})
        assert raises_api_error(req).status_code == 404
        degraded = raises_unavailable(req)
        assert degraded.status_code == 503
        assert _body(degraded)["error"]["details"] == {"dependency": "database"}
        assert degraded.headers["Retry-After"]
        with caplog.at_level("ERROR", logger="core.auth"):
            crashed = crashes(req)
        assert crashed.status_code == 500
        assert _body(crashed)["error"]["code"] == "internal"
        assert "secret internals" not in crashed.get_body().decode()
        assert "trace-abcdef12" in caplog.text  # the log carries the request id
        assert crashed.headers["X-Request-Id"] == "trace-abcdef12"

    @needs_api_http
    def test_jwks_outage_is_503_with_retry_after(self, jwks, fake_cosmos: FakeCosmos) -> None:
        jwks.client.error = jwt.PyJWKClientConnectionError("down")
        resp = _member_handler(_request(jwks.bearer()))
        assert resp.status_code == 503
        assert _body(resp)["error"]["details"] == {"reason": "jwks"}
        assert resp.headers["Retry-After"] == "5"


# ---------------------------------------------------------------------------
# The error registry (core.errors), which every rejection above goes through
# ---------------------------------------------------------------------------
class TestErrorRegistry:
    def test_every_code_has_a_status_and_a_message(self) -> None:
        from core import errors

        assert set(errors.ERROR_STATUS) == set(errors.DEFAULT_MESSAGES)
        for code, message in errors.DEFAULT_MESSAGES.items():
            assert message and len(message) <= 300, code

    def test_410_is_only_account_deleted(self) -> None:
        from core import errors

        assert [code for code, status in errors.ERROR_STATUS.items() if status == 410] == ["account_deleted"]

    def test_the_documented_statuses(self) -> None:
        from core import errors

        by_status: dict[int, set[str]] = {}
        for code, status in errors.ERROR_STATUS.items():
            by_status.setdefault(status, set()).add(code)
        assert by_status == {
            400: {"validation_failed", "invite_malformed"},
            401: {"unauthenticated", "invalid_token"},
            403: {"invite_required", "account_blocked", "admin_required", "consent_required"},
            404: {"not_found", "invite_not_found"},
            409: {"invite_used", "invite_expired", "duplicate", "limit_reached", "state_conflict"},
            410: {"account_deleted"},
            413: {"payload_too_large"},
            415: {"unsupported_media_type"},
            429: {"quota_exceeded"},
            500: {"internal"},
            503: {"auth_unavailable", "degraded"},
        }

    def test_an_unregistered_code_is_a_programming_error(self) -> None:
        with pytest.raises(KeyError):
            ApiError("teapot")

    def test_429_and_503_always_carry_retry_after(self) -> None:
        assert ApiError("quota_exceeded").retry_after == 5
        assert ApiError("degraded").retry_after == 5
        assert ApiError("auth_unavailable", retry_after=60).retry_after == 60
        assert ApiError("not_found").retry_after is None

    def test_the_envelope(self) -> None:
        error = ApiError("invite_required")
        assert error.status == 403
        assert error.body() == {
            "error": {"code": "invite_required", "message": "An invite code is required to use MyNews.", "details": {}}
        }
        # The details are copied, so a caller cannot mutate a raised error's body.
        error.body()["error"]["details"]["x"] = 1
        assert error.details == {}

    def test_helpers(self) -> None:
        from core import errors

        assert errors.invalid("settings.briefSize", "enum").details == {
            "fields": [{"field": "settings.briefSize", "reason": "enum"}]
        }
        assert errors.invalid_body("invalid_json").details == {"reason": "invalid_json"}
        assert errors.not_found("story").details == {"resource": "story"}
        contention = errors.degraded("database", "contention", retry_after=1)
        assert (contention.status, contention.details, contention.retry_after) == (
            503,
            {"dependency": "database", "reason": "contention"},
            1,
        )
        assert errors.degraded("queue").details == {"dependency": "queue"}

    @pytest.mark.parametrize(
        "call",
        [
            lambda e: e.invalid("x", "camelCase"),
            lambda e: e.invalid_body("bad"),
            lambda e: e.not_found("planet"),
            lambda e: e.degraded("disk"),
        ],
    )
    def test_helpers_refuse_values_outside_the_registry(self, call: Any) -> None:
        from core import errors

        with pytest.raises(ValueError):
            call(errors)

    def test_every_code_renders_a_valid_error_body(self, wire_schema) -> None:
        from datetime import datetime, timezone

        from core import errors, members, quotas

        quota_error = quotas._exceeded(quotas.QUOTAS["redeem"], datetime(2026, 10, 21, 10, tzinfo=timezone.utc))
        samples = [
            errors.invalid("settings.briefSize", "enum"),
            errors.invalid_body("invalid_json"),
            ApiError("invite_malformed"),
            ApiError("unauthenticated"),
            ApiError("invalid_token"),
            ApiError("invalid_token", details={"reason": "expired"}),
            ApiError("invite_required"),
            ApiError("account_blocked"),
            ApiError("admin_required"),
            ApiError("consent_required", details={"consentVersion": members.CONSENT_MIN_VERSION}),
            errors.not_found("invite"),
            ApiError("invite_not_found"),
            ApiError("invite_used"),
            ApiError("invite_expired"),
            ApiError("duplicate", details={"resource": "source", "id": "heise"}),
            ApiError("limit_reached", details={"resource": "saved", "limit": 500}),
            ApiError("state_conflict", details={"status": "purging"}),
            ApiError("account_deleted", details={"state": "deleting"}),
            ApiError("account_deleted", details={"state": "deleted"}),
            ApiError("payload_too_large", details={"limitBytes": 65536}),
            ApiError("unsupported_media_type", details={"expected": "application/json"}),
            quota_error,
            ApiError("internal"),
            ApiError("auth_unavailable", details={"reason": "config"}, retry_after=60),
            ApiError("auth_unavailable", details={"reason": "jwks"}, retry_after=5),
            errors.degraded("database", "contention", retry_after=1),
        ]
        assert {sample.code for sample in samples} == set(errors.ERROR_STATUS)
        for sample in samples:
            wire_schema("Error", sample.body())

    def test_the_errors_auth_raises_are_valid_bodies(self, jwks, env, wire_schema) -> None:
        raised: list[ApiError] = [_rejection(jwks.token(exp=NOW - 3600)), _rejection(jwks.token(azp=ids.OTHER_CLIENT_ID))]
        jwks.client.error = jwt.PyJWKClientConnectionError("down")
        raised.append(_rejection(jwks.token()))
        env(ENTRA_TENANT_ID=None)
        with pytest.raises(ApiError) as caught:
            auth.authenticate(_request({"Authorization": "Bearer a.b.c"}))
        raised.append(caught.value)
        for error in raised:
            wire_schema("Error", error.body())
