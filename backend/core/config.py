"""Runtime configuration, read once from environment variables.

Everything that could plausibly need tuning without a code change lives here so
it can be moved to app settings. Model deployment names in particular are
config, not constants: Foundry runs OpenAI models on a 12-month GA lifecycle, so
this project will outlive at least one model swap.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache

log = logging.getLogger(__name__)

# What an app setting still holds when its Key Vault reference did not resolve:
# App Service passes the literal "@Microsoft.KeyVault(SecretUri=...)" text through
# to the process instead of failing the start. Read as a value, that text is a
# perfectly good non-empty string, so an HMAC key would hash with it and a client
# id would compare against it. It is treated as missing instead.
KEYVAULT_REF_PREFIX = "@Microsoft.KeyVault("


def unresolved_keyvault_ref(value: str) -> bool:
    """True for a Key Vault reference App Service failed to resolve."""
    return value.strip().startswith(KEYVAULT_REF_PREFIX)


def _env(name: str, default: str = "") -> str:
    raw = os.environ.get(name) or ""
    if unresolved_keyvault_ref(raw):
        # Missing, not the default: a reference that failed to resolve is a
        # broken deployment, and settings the auth path needs must fail closed
        # on it (core.auth.config_problems) rather than quietly fall back to a
        # default such as ENTRA_REQUIRED_SCOPE=access_as_user. Name only: the
        # reference text identifies the vault and secret.
        log.warning("Setting %s is an unresolved Key Vault reference; treating it as missing", name)
        return ""
    return raw or default


def require(value: str, name: str) -> str:
    """Validate a setting at the point of use, not at import time.

    Loading configuration must never fail for a setting the caller does not
    need — otherwise fetching a feed requires a Cosmos endpoint, and tooling
    like scripts/probe_feeds.py cannot run without a full Azure environment.
    """
    if not value:
        raise RuntimeError(
            f"Required setting {name!r} is missing. "
            "Locally it belongs in backend/local.settings.json; in Azure it is an app setting."
        )
    return value


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else int(raw)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name).strip().lower()
    return default if not raw else raw in {"1", "true", "yes"}


def _csv_env(name: str) -> tuple[str, ...]:
    """A comma list, lower-cased: client ids are GUIDs, which compare case-blind."""
    return tuple(part.strip().lower() for part in _env(name).split(",") if part.strip())


def min_supported_build() -> int:
    """The oldest iOS build the backend still serves; 0 means every build.

    Read straight from the environment rather than through Settings, because the
    anonymous /health route reports it and promises no settings object and
    nothing to fail on. A value that is not an integer degrades to 0 with a
    warning instead of raising: a typo in an app setting must not turn the one
    public route into a 500, and an unparsable floor cannot be enforced anyway
    (docs/api.md section 4: the server never enforces it).
    """
    raw = os.environ.get("MIN_SUPPORTED_BUILD", "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        log.warning("MIN_SUPPORTED_BUILD=%r is not an integer; serving minBuild=0", raw)
        return 0
    if value < 0:
        # Health and Me promise a non-negative minBuild (their schemas say so).
        log.warning("MIN_SUPPORTED_BUILD=%r is negative; serving minBuild=0", raw)
        return 0
    return value


@dataclass(frozen=True)
class Settings:
    cosmos_endpoint: str
    cosmos_database: str
    cosmos_container: str

    foundry_endpoint: str
    # Deployment names — what the API is called with.
    model_scorer: str
    model_summarizer: str
    model_embedding: str
    # Underlying model ids — what token usage is priced against. A deployment
    # name is just a label and carries no cost information.
    model_scorer_id: str
    model_summarizer_id: str
    model_embedding_id: str
    # Reasoning effort is per-model: 'minimal' is accepted only by the original
    # GPT-5 family (gpt-5-nano), 'none' only by gpt-5.1 and later (gpt-5.4).
    # Sending the wrong value is rejected by the API, so these are not
    # interchangeable and must travel with the model choice.
    reasoning_effort_scorer: str
    reasoning_effort_summarizer: str

    # 512 dims via the `dimensions` parameter (Matryoshka truncation). This MUST
    # match the container's vectorEmbeddingPolicy — a vector policy cannot be
    # edited in place, so a mismatch means recreating the container.
    embedding_dimensions: int

    # Abort LLM stages once the day's estimated spend crosses this. Azure OpenAI
    # has no hard spend limit, so this is the only in-process brake.
    daily_budget_usd: float

    # Relevance 0-100 from the scorer; at or above this an item gets summarized.
    summarize_threshold: int
    # Max items handed to the scorer per digest run, after the embedding gate.
    max_scored_per_run: int
    # Cosine similarity at or above which two articles are the same story.
    cluster_threshold: float
    # Fraction of feed slots reserved for items outside the taste profile, so
    # the feed cannot collapse onto a single topic.
    exploration_quota: float

    article_ttl_days: int
    # Don't ingest anything published longer ago than this. Several feeds carry
    # a deep archive — openai.com/news ships 1,051 items spanning years, Hugging
    # Face 831 — and without a cutoff every one is written to Cosmos on first
    # sight, costing RU and storage for articles that can never reach the feed.
    max_article_age_days: int
    user_agent: str
    push_enabled: bool

    # v1's container, which only the v1 harvest (pipeline/harvest.py,
    # core/sources.py) and the Phase 1 admin reads of v1 runs touch, always by
    # passing this name explicitly. Everything else gets cosmos_container.
    legacy_cosmos_container: str

    # Entra External ID ("von Mentlen News"). Empty until the tenant's ids are
    # deployed, and core.auth fails closed on any of the auth set being empty,
    # an unresolved Key Vault reference or (for the ids) not a GUID: every
    # non-public route answers 503 auth_unavailable. Never open.
    entra_tenant_id: str
    # The tenant's initial domain ("vonmentlennews"): the clients' MSAL authority
    # host <sub>.ciamlogin.com. Token validation does not use it, because the
    # issuer and the JWKS live under the tenant id.
    entra_tenant_subdomain: str
    # The mynews-api app registration: the only accepted `aud`.
    entra_api_client_id: str
    # The calling apps (`azp`) allowed to use the API: the iOS and CLI clients.
    entra_allowed_client_ids: tuple[str, ...]
    entra_required_scope: str
    entra_admin_role: str
    # Keys the pseudonymous uid (HMAC of the oid) that telemetry, audit and the
    # admin routes use instead of the oid. Generated once and never regenerated:
    # a new key orphans every tombstone and audit entry keyed by the old uid.
    # Kept out of repr so a logged Settings object cannot carry it.
    hmac_key: str = field(repr=False)
    # manual | graph. How the Entra user is removed after a purge.
    identity_delete_mode: str
    min_supported_build: int
    # "YYYY-MM-DD" by which the Sign in with Apple client secret must be renewed
    # in the Entra Apple IdP blade (Apple caps it at 6 months), or "".
    apple_secret_renew_by: str
    # The storage queues (account-purge, ops-jobs, ...) through the managed
    # identity. The bindings read these through the JOBS_STORAGE connection
    # prefix; the admin health check peeks the queues with the same URI.
    jobs_queue_service_uri: str
    jobs_credential: str
    # Blocks every Cosmos write from this process unless the call is inside
    # core.cosmos.allow_prod_writes(): local.settings.json sets it, because a
    # local run talks to the production account. Azure sets "false".
    local_readonly: bool

    # Per-host User-Agent overrides. developer.apple.com returns HTTP 500 to
    # curl, NetNewsWire and Chrome UAs — only a Safari UA gets 200.
    user_agent_overrides: dict[str, str] = field(default_factory=dict)


SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        cosmos_endpoint=_env("COSMOS_ENDPOINT"),
        cosmos_database=_env("COSMOS_DATABASE", "mynews"),
        # v2 code reads and writes container `news`; see legacy_cosmos_container.
        cosmos_container=_env("COSMOS_CONTAINER", "news"),
        foundry_endpoint=_env("FOUNDRY_ENDPOINT"),
        model_scorer=_env("MODEL_SCORER", "scorer"),
        model_summarizer=_env("MODEL_SUMMARIZER", "summarizer"),
        model_embedding=_env("MODEL_EMBEDDING", "embedding"),
        model_scorer_id=_env("MODEL_SCORER_ID", "gpt-5-nano"),
        model_summarizer_id=_env("MODEL_SUMMARIZER_ID", "gpt-5.4"),
        model_embedding_id=_env("MODEL_EMBEDDING_ID", "text-embedding-3-small"),
        reasoning_effort_scorer=_env("REASONING_EFFORT_SCORER", "minimal"),
        reasoning_effort_summarizer=_env("REASONING_EFFORT_SUMMARIZER", "none"),
        embedding_dimensions=_int_env("EMBEDDING_DIMENSIONS", 512),
        daily_budget_usd=_float_env("DAILY_TOKEN_BUDGET_USD", 1.50),
        summarize_threshold=_int_env("SUMMARIZE_THRESHOLD", 65),
        max_scored_per_run=_int_env("MAX_SCORED_PER_RUN", 200),
        cluster_threshold=_float_env("CLUSTER_THRESHOLD", 0.80),
        exploration_quota=_float_env("EXPLORATION_QUOTA", 0.10),
        article_ttl_days=_int_env("ARTICLE_TTL_DAYS", 21),
        max_article_age_days=_int_env("MAX_ARTICLE_AGE_DAYS", 7),
        user_agent=_env(
            "USER_AGENT",
            # Deliberately honest and un-AI-sounding. derStandard, Spiegel, ORF,
            # Zeit and FAZ all Disallow ClaudeBot/GPTBot/anthropic-ai/CCBot by
            # name in robots.txt while leaving `User-agent: *` open.
            "MyNewsBot/0.1 (+https://github.com/thomas/mynews; personal reader)",
        ),
        push_enabled=_env("PUSH_ENABLED", "false").lower() == "true",
        legacy_cosmos_container=_env("LEGACY_COSMOS_CONTAINER", "items"),
        # GUIDs are lower-cased once here: Entra writes lower-case ids into iss,
        # tid and aud, and every comparison against them is exact.
        entra_tenant_id=_env("ENTRA_TENANT_ID").strip().lower(),
        entra_tenant_subdomain=_env("ENTRA_TENANT_SUBDOMAIN").strip().lower(),
        entra_api_client_id=_env("ENTRA_API_CLIENT_ID").strip().lower(),
        entra_allowed_client_ids=_csv_env("ENTRA_ALLOWED_CLIENT_IDS"),
        entra_required_scope=_env("ENTRA_REQUIRED_SCOPE", "access_as_user").strip(),
        entra_admin_role=_env("ENTRA_ADMIN_ROLE", "admin").strip(),
        hmac_key=_env("MYNEWS_HMAC_KEY"),
        identity_delete_mode=_env("IDENTITY_DELETE_MODE", "manual").strip().lower(),
        min_supported_build=min_supported_build(),
        apple_secret_renew_by=_env("APPLE_SECRET_RENEW_BY").strip(),
        jobs_queue_service_uri=_env("JOBS_STORAGE__queueServiceUri").strip(),
        jobs_credential=_env("JOBS_STORAGE__credential", "managedidentity").strip(),
        local_readonly=_bool_env("LOCAL_READONLY"),
        user_agent_overrides={"developer.apple.com": SAFARI_UA},
    )
