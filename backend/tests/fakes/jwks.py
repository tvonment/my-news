"""Entra test identity: tenant and client ids, an RSA signing key, a fake
PyJWKClient and a token factory.

Nothing here talks to a network. ``core.auth._jwks_client`` is the seam: the
``jwks`` fixture (tests/conftest.py) replaces it with a ``FakeJwksClient`` that
serves this module's public key, so tokens minted by ``TokenFactory`` verify
exactly the way a real access token from the "von Mentlen News" tenant would,
and every claim can be overridden or removed to build the negative matrix.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

# Obviously-fake ids in the GUID grammar core.auth.config_problems() requires.
TENANT_ID = "11111111-1111-4111-8111-111111111111"
API_CLIENT_ID = "22222222-2222-4222-8222-222222222222"
IOS_CLIENT_ID = "33333333-3333-4333-8333-333333333333"
CLI_CLIENT_ID = "44444444-4444-4444-8444-444444444444"
OTHER_CLIENT_ID = "55555555-5555-4555-8555-555555555555"
HMAC_KEY = "tests-only-hmac-key-not-a-secret-0123456789"
KID = "test-signing-key-1"

# Members used across the tests.
OID_ALICE = "a11ce000-0000-4000-8000-000000000001"
OID_BOB = "b0b00000-0000-4000-8000-000000000002"
OID_ADMIN = "ad000000-0000-4000-8000-000000000003"

ENTRA_TEST_ENV = {
    "ENTRA_TENANT_ID": TENANT_ID,
    "ENTRA_TENANT_SUBDOMAIN": "vonmentlennews",
    "ENTRA_API_CLIENT_ID": API_CLIENT_ID,
    "ENTRA_ALLOWED_CLIENT_IDS": f"{IOS_CLIENT_ID},{CLI_CLIENT_ID}",
    "ENTRA_REQUIRED_SCOPE": "access_as_user",
    "ENTRA_ADMIN_ROLE": "admin",
    "MYNEWS_HMAC_KEY": HMAC_KEY,
}

# Pass as a claim value to leave the claim out of the token entirely.
REMOVE = object()


@functools.lru_cache(maxsize=4)
def private_key(name: str = "primary") -> rsa.RSAPrivateKey:
    """A 2048-bit RSA key, generated once per test session per name."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unsigned_token(header: dict[str, Any], claims: dict[str, Any]) -> str:
    """An ``alg: none`` token: header.payload. with an empty signature."""
    return f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}."


def raw_token(header: dict[str, Any], claims: dict[str, Any], *, hmac_secret: str | None = None) -> str:
    """A token PyJWT would refuse to mint: any header (a non-string kid, an alg
    that lies about the signature), signed with HMAC-SHA256 when a secret is
    given, else with a junk signature segment."""
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    if hmac_secret is None:
        return f"{signing_input}.{_b64(b'junk-signature')}"
    digest = hmac.new(hmac_secret.encode(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(digest)}"


class FakeJwksClient:
    """Stands in for jwt.PyJWKClient: get_signing_key(kid) returns an object
    with ``.key``; an unknown kid raises PyJWKClientError, as the real client
    does after its one refetch; ``error`` makes every lookup raise it."""

    def __init__(self, keys: dict[str, Any] | None = None, *, error: Exception | None = None):
        self.keys = dict(keys) if keys is not None else {KID: private_key().public_key()}
        self.error = error
        self.lookups: list[str] = []

    def get_signing_key(self, kid: str) -> SimpleNamespace:
        self.lookups.append(kid)
        if self.error is not None:
            raise self.error
        if kid not in self.keys:
            raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
        return SimpleNamespace(key=self.keys[kid], key_id=kid)

    def get_signing_keys(self, refresh: bool = False) -> list[SimpleNamespace]:
        if self.error is not None:
            raise self.error
        return [SimpleNamespace(key=key, key_id=kid) for kid, key in self.keys.items()]


@dataclass
class TokenFactory:
    """Mints access tokens shaped like the tenant's v2 tokens for mynews-api."""

    tenant_id: str = TENANT_ID
    audience: str = API_CLIENT_ID
    azp: str = IOS_CLIENT_ID
    kid: str = KID
    signing_key: Any = field(default_factory=private_key)

    def claims(self, oid: str = OID_ALICE, *, roles: tuple[str, ...] | list[str] = (), now: int | None = None) -> dict[str, Any]:
        issued = int(time.time()) if now is None else now
        claims: dict[str, Any] = {
            "aud": self.audience,
            "iss": f"https://{self.tenant_id}.ciamlogin.com/{self.tenant_id}/v2.0",
            "iat": issued - 30,
            "nbf": issued - 30,
            "exp": issued + 3600,
            "ver": "2.0",
            "tid": self.tenant_id,
            "oid": oid,
            "sub": "pairwise-subject-" + oid[:8],
            "azp": self.azp,
            "azpacr": "0",
            "scp": "access_as_user",
            "name": "never read by the backend",
            "preferred_username": "never.read@example.com",
        }
        if roles:
            claims["roles"] = list(roles)
        return claims

    def token(
        self,
        oid: Any = OID_ALICE,
        *,
        roles: Any = (),
        header: dict[str, Any] | None = None,
        algorithm: str = "RS256",
        key: Any = None,
        **overrides: Any,
    ) -> str:
        """A signed token. Claim overrides by keyword (``exp=...``,
        ``scp=REMOVE``), and ``oid``/``roles`` take any value, REMOVE included,
        for the negative matrix; ``header`` overrides JOSE header fields;
        ``algorithm`` and ``key`` choose the actual signature, e.g.
        ``algorithm="HS256", key="secret"``."""
        claims = self.claims(oid if isinstance(oid, str) else OID_ALICE)
        if oid is REMOVE:
            claims.pop("oid")
        elif not isinstance(oid, str):
            claims["oid"] = oid
        if isinstance(roles, (list, tuple)):
            if roles:
                claims["roles"] = list(roles)
        elif roles is not REMOVE:
            claims["roles"] = roles
        for name, value in overrides.items():
            if value is REMOVE:
                claims.pop(name, None)
            else:
                claims[name] = value
        headers: dict[str, Any] = {"kid": self.kid, "typ": "JWT"}
        for name, value in (header or {}).items():
            if value is REMOVE:
                headers.pop(name, None)
            else:
                headers[name] = value
        if algorithm == "none":
            return unsigned_token({**headers, "alg": "none"}, claims)
        signing_key = key if key is not None else self.signing_key
        return jwt.encode(claims, signing_key, algorithm=algorithm, headers=headers)
