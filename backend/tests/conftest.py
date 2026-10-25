from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

# The Functions app root is the import root at runtime; mirror that for tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import jwks as fake_identity  # noqa: E402
from tests.fakes.cosmos import FakeCosmos  # noqa: E402

_TEST_ENV = {
    "COSMOS_ENDPOINT": "https://example.documents.azure.com:443/",
    "COSMOS_DATABASE": "mynews",
    "COSMOS_CONTAINER": "news",
    "LEGACY_COSMOS_CONTAINER": "items",
    "FOUNDRY_ENDPOINT": "https://example.openai.azure.com",
    "MODEL_SCORER": "scorer",
    "MODEL_SUMMARIZER": "summarizer",
    "MODEL_EMBEDDING": "embedding",
    **fake_identity.ENTRA_TEST_ENV,
    "IDENTITY_DELETE_MODE": "manual",
    "JOBS_STORAGE__queueServiceUri": "https://example.queue.core.windows.net/",
    "JOBS_STORAGE__credential": "managedidentity",
    "LOCAL_READONLY": "false",
}
# Read by code under test, and left unset unless a test sets them.
_UNSET_ENV = ("MIN_SUPPORTED_BUILD", "APPLE_SECRET_RENEW_BY")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Forced rather than setdefault: a shell that has the azd environment loaded
    # carries the real tenant id, HMAC key and LOCAL_READONLY, and none of
    # those may decide a test's outcome. monkeypatch restores them afterwards.
    for key, value in _TEST_ENV.items():
        monkeypatch.setenv(key, value)
    for key in _UNSET_ENV:
        monkeypatch.delenv(key, raising=False)
    from core.config import get_settings

    get_settings.cache_clear()


class _NoCredential:
    """What core.jobs gets instead of DefaultAzureCredential: the real chain
    would try the managed-identity endpoint and the developer's Azure CLI."""

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        from azure.identity import CredentialUnavailableError

        raise CredentialUnavailableError("no credentials in tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never leave the machine. The JWKS fetch fails the way an
    unreachable tenant does (the ``jwks`` fixture replaces the client entirely),
    and the queue peek's token never comes."""
    import jwt

    import core.jobs

    def no_fetch(self: Any) -> Any:
        raise jwt.PyJWKClientConnectionError("network access is disabled in tests")

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", no_fetch)
    monkeypatch.setattr(core.jobs, "_credential", lambda: _NoCredential())


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Set (or, with None, delete) environment variables and re-read settings:
    ``env(ENTRA_TENANT_ID=None, LOCAL_READONLY="true")``."""
    from core.config import get_settings

    def apply(**values: str | None) -> None:
        for key, value in values.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


@pytest.fixture
def fake_cosmos(monkeypatch: pytest.MonkeyPatch) -> FakeCosmos:
    """An in-memory Cosmos account behind core.cosmos.get_container."""
    import core.cosmos

    fake = FakeCosmos()
    monkeypatch.setattr(core.cosmos, "get_container", fake.get_container)
    return fake


class JwksFixture:
    """The fake key endpoint plus the factory for tokens it verifies."""

    def __init__(self, client: fake_identity.FakeJwksClient, tokens: fake_identity.TokenFactory):
        self.client = client
        self.tokens = tokens
        self.uris: list[str] = []

    def token(self, oid: str = fake_identity.OID_ALICE, **kwargs: Any) -> str:
        return self.tokens.token(oid, **kwargs)

    def bearer(self, oid: str = fake_identity.OID_ALICE, **kwargs: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(oid, **kwargs)}"}


@pytest.fixture
def jwks(monkeypatch: pytest.MonkeyPatch) -> JwksFixture:
    """core.auth's JWKS client replaced by one serving the test key."""
    import core.auth

    fixture = JwksFixture(fake_identity.FakeJwksClient(), fake_identity.TokenFactory())

    def client_for(uri: str) -> fake_identity.FakeJwksClient:
        fixture.uris.append(uri)
        return fixture.client

    monkeypatch.setattr(core.auth, "_jwks_client", client_for)
    return fixture


_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "docs" / "api" / "schemas"


@pytest.fixture(scope="session")
def wire_schema() -> Callable[[str, Any], None]:
    """``wire_schema("Me", body)`` asserts that a response body matches
    docs/api/schemas/Me.schema.json (draft 2020-12, shared $defs resolved by
    $id), listing every violation. Skips where jsonschema is not installed: like
    pytest it never ships, so it is not in requirements.txt."""
    import json

    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    from referencing.jsonschema import DRAFT202012

    documents = {
        path.name.removesuffix(".schema.json"): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(_SCHEMA_DIR.glob("*.schema.json"))
    }
    registry = referencing.Registry().with_resources(
        (doc["$id"], referencing.Resource.from_contents(doc, default_specification=DRAFT202012))
        for doc in documents.values()
    )

    def validate(name: str, body: Any) -> None:
        validator = jsonschema.Draft202012Validator(
            documents[name], registry=registry, format_checker=jsonschema.FormatChecker()
        )
        problems = sorted(validator.iter_errors(body), key=lambda e: [str(p) for p in e.absolute_path])
        assert not problems, f"{name}:\n" + "\n".join(
            f"  {'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in problems
        )

    return validate


@pytest.fixture
def make_principal() -> Callable[..., Any]:
    """A validated caller without a token round trip:
    ``make_principal(OID_BOB, roles=("admin",))``."""
    from core.auth import Principal, uid_for

    def build(oid: str = fake_identity.OID_ALICE, *, roles: tuple[str, ...] = ()) -> Principal:
        sorted_roles = tuple(sorted(set(roles)))
        return Principal(
            oid=oid,
            uid=uid_for(oid),
            tid=fake_identity.TENANT_ID,
            azp=fake_identity.IOS_CLIENT_ID,
            scopes=frozenset({"access_as_user"}),
            roles=sorted_roles,
            expires_at=2_000_000_000,
            is_admin="admin" in sorted_roles,
        )

    return build
