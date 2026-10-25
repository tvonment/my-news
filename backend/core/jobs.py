"""The queue contract: names, message shapes, and a read-only peek for health.

Three queues on the Function App's storage account, each with a ``-poison``
twin the host fills after ``maxDequeueCount`` (3) failures (docs/data-model.md
section 9). Producers write through ``@bp.queue_output`` bindings on the
``JOBS_STORAGE`` connection and workers read through ``@bp.queue_trigger``, so
this module never sends anything: it only fixes what a message looks like, in
one place both sides import.

Messages carry **ids only** (an oid, a job id), camelCase like the API, and a
``v`` so a worker can refuse a shape it does not know. Parsing is strict about
what a worker acts on: an oid that is not a GUID never reaches a partition key,
and a kind this release cannot run is a ``ValueError`` the worker logs and
acknowledges, rather than a guess.

``peek()`` is for GET /admin/health only. It calls the Storage REST API's Peek
Messages operation with httpx and a managed-identity token, so the queue SDK
(azure-storage-queue) does not come back as a dependency for one read. Peek
needs the messages/read data action, which the identity's Storage Queue Data
Contributor role includes; a 403 would show as ``forbidden``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal, Mapping

from core.config import get_settings
from core.models import EVENT_ID_RE, OID_RE, SLUG_MAX, SLUG_RE, UID_RE, iso

if TYPE_CHECKING:
    from azure.identity import DefaultAzureCredential

log = logging.getLogger(__name__)

JOBS_CONNECTION = "JOBS_STORAGE"
QUEUE_ACCOUNT_PURGE = "account-purge"
QUEUE_SUGGESTIONS = "suggestions"
QUEUE_OPS_JOBS = "ops-jobs"
POISON_SUFFIX = "-poison"
MESSAGE_VERSION = 1

PURGE_KINDS = ("account", "history")
# Phase 2 adds "cycle" and "republish", in the release whose worker runs them.
OPS_JOB_KINDS = ("harvest",)
SYSTEM_REQUESTER = "system"

# The one timestamp shape core.models.iso() writes.
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# Azure's queue-name rules: 3-63 lower-case letters, digits and single hyphens,
# starting and ending with a letter or digit.
_QUEUE_NAME_RE = re.compile(r"^[a-z0-9](?!.*--)[a-z0-9-]{1,61}[a-z0-9]$")

# Peek Messages returns at most 32; that is also what "count" can report, so a
# poison queue reads 0 (fine) or 1-32 ("32" meaning "32 or more").
PEEK_MAX_MESSAGES = 32
STORAGE_SCOPE = "https://storage.azure.com/.default"
# The first REST version that accepts Entra tokens is 2017-11-09; this one is
# recent and stable.
STORAGE_API_VERSION = "2021-12-02"


def poison_queue(queue: str) -> str:
    return queue + POISON_SUFFIX


def _decode(raw: str | bytes) -> dict[str, Any]:
    """A message body as a JSON object; ValueError otherwise. The host hands
    the trigger the decoded text (bytes from ``msg.get_body()``)."""
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    if not isinstance(text, str):
        raise ValueError("a queue message is text")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("a queue message is a JSON object")
    if data.get("v") != MESSAGE_VERSION or isinstance(data.get("v"), bool):
        raise ValueError(f"unknown message version {data.get('v')!r}")
    return data


def _timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
        raise ValueError(f"{name} must be a UTC timestamp like 2026-10-21T10:00:00Z")
    return value


# ---------------------------------------------------------------------------
# account-purge
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PurgeMessage:
    """``{"v": 1, "kind": "account", "oid": "<guid>", "requestedAt": "<ts>"}``.

    ``kind`` "history" (clear opens and dwell signals only) is Phase 3's; the
    shape accepts it now so the worker can acknowledge one with a log line
    instead of poisoning it. The oid is validated on construction: a message is
    the one input that reaches ``user|{oid}`` without a token, and only a GUID
    may address a partition.
    """

    oid: str
    requested_at: str
    kind: Literal["account", "history"] = "account"
    v: int = MESSAGE_VERSION

    def __post_init__(self) -> None:
        if self.v != MESSAGE_VERSION:
            raise ValueError(f"unknown message version {self.v!r}")
        if self.kind not in PURGE_KINDS:
            raise ValueError(f"unknown purge kind {self.kind!r}")
        if not isinstance(self.oid, str) or not OID_RE.match(self.oid):
            raise ValueError("oid must be a lower-case GUID")
        _timestamp(self.requested_at, "requestedAt")

    def to_json(self) -> str:
        return json.dumps(
            {"v": self.v, "kind": self.kind, "oid": self.oid, "requestedAt": self.requested_at},
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "PurgeMessage":
        """Parse a message body; ValueError for a version, kind, oid or
        timestamp this release does not accept. Unknown keys are ignored, so a
        later producer can add one within version 1."""
        data = _decode(raw)
        kind = data.get("kind")
        if kind not in PURGE_KINDS:
            raise ValueError(f"unknown purge kind {kind!r}")
        oid = data.get("oid")
        if not isinstance(oid, str):
            raise ValueError("oid must be a lower-case GUID")
        return cls(oid=oid, requested_at=data.get("requestedAt"), kind=kind)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ops-jobs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OpsJobMessage:
    """``{"v": 1, "jobId", "kind": "harvest", "force", "sourceId", "requestedBy",
    "requestedAt"}``. Phase 1's only kind is a forced v1 harvest, optionally of
    one source; ``requestedBy`` is the admin's uid or "system"."""

    job_id: str
    kind: Literal["harvest"]
    requested_by: str
    requested_at: str
    force: bool = True
    source_id: str | None = None
    v: int = MESSAGE_VERSION

    def __post_init__(self) -> None:
        if self.v != MESSAGE_VERSION:
            raise ValueError(f"unknown message version {self.v!r}")
        if self.kind not in OPS_JOB_KINDS:
            raise ValueError(f"unknown ops job kind {self.kind!r}")
        if not isinstance(self.job_id, str) or not EVENT_ID_RE.match(self.job_id) or self.job_id != self.job_id.lower():
            raise ValueError("jobId must be a lower-case UUID")
        if self.requested_by != SYSTEM_REQUESTER and not (
            isinstance(self.requested_by, str) and UID_RE.match(self.requested_by)
        ):
            raise ValueError('requestedBy must be a uid or "system"')
        _timestamp(self.requested_at, "requestedAt")
        if not isinstance(self.force, bool):
            raise ValueError("force must be a boolean")
        if self.source_id is not None and not (
            isinstance(self.source_id, str) and len(self.source_id) <= SLUG_MAX and SLUG_RE.match(self.source_id)
        ):
            raise ValueError("sourceId must be a source slug or null")

    @classmethod
    def new(
        cls,
        kind: Literal["harvest"],
        *,
        requested_by: str,
        now: datetime,
        force: bool = True,
        source_id: str | None = None,
    ) -> "OpsJobMessage":
        """A fresh job with a random id (Phase 2's POST /admin/runs/*)."""
        return cls(
            job_id=str(uuid.uuid4()),
            kind=kind,
            requested_by=requested_by,
            requested_at=iso(now),
            force=force,
            source_id=source_id,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": self.v,
                "jobId": self.job_id,
                "kind": self.kind,
                "force": self.force,
                "sourceId": self.source_id,
                "requestedBy": self.requested_by,
                "requestedAt": self.requested_at,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "OpsJobMessage":
        data = _decode(raw)
        kind = data.get("kind")
        if kind not in OPS_JOB_KINDS:
            raise ValueError(f"unknown ops job kind {kind!r}")
        job_id = data.get("jobId")
        if isinstance(job_id, str):
            job_id = job_id.lower()
        return cls(
            job_id=job_id,  # type: ignore[arg-type]
            kind=kind,
            requested_by=data.get("requestedBy"),  # type: ignore[arg-type]
            requested_at=data.get("requestedAt"),  # type: ignore[arg-type]
            force=data.get("force", True),
            source_id=data.get("sourceId"),
        )


# ---------------------------------------------------------------------------
# Peek (admin health)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QueuePeek:
    """What one peek saw. ``count`` is the number of visible messages, at most
    32; ``oldest_inserted_at`` is the oldest of those (None when empty);
    ``error`` is a CheckError class, never exception text."""

    queue: str
    ok: bool
    count: int | None
    oldest_inserted_at: str | None
    error: str | None


@lru_cache(maxsize=1)
def _credential() -> "DefaultAzureCredential":
    # Imported here: azure.identity pulls in msal and requests, which no HTTP
    # route but this admin check needs. The credential caches its token.
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def _http_error(status: int) -> str:
    if status in (401, 403):
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "throttled"
    if status >= 500:
        return "unavailable"
    return "failed"


def _parse_peek(body: bytes) -> tuple[int, str | None]:
    """(message count, oldest InsertionTime as an iso() timestamp) from a
    Peek Messages response (``<QueueMessagesList><QueueMessage>...``)."""
    import xml.etree.ElementTree as ElementTree

    root = ElementTree.fromstring(body)
    oldest: datetime | None = None
    count = 0
    for message in root.iter("QueueMessage"):
        count += 1
        text = message.findtext("InsertionTime")
        if not text:
            continue
        # RFC 1123, e.g. "Fri, 09 Oct 2009 21:04:30 GMT".
        inserted = parsedate_to_datetime(text)
        if inserted.tzinfo is None:
            inserted = inserted.replace(tzinfo=timezone.utc)
        if oldest is None or inserted < oldest:
            oldest = inserted
    return count, (iso(oldest) if oldest is not None else None)


def peek(queue: str, *, timeout_s: float = 5.0) -> QueuePeek:
    """Peek up to 32 messages of ``queue`` without dequeuing them. Never raises:
    every failure is a QueuePeek with ``ok`` False and an error class."""

    def failed(error: str) -> QueuePeek:
        return QueuePeek(queue=queue, ok=False, count=None, oldest_inserted_at=None, error=error)

    if not isinstance(queue, str) or not _QUEUE_NAME_RE.match(queue):
        return failed("misconfigured")
    base = get_settings().jobs_queue_service_uri
    if not base.startswith("https://"):
        # Unset (a local run on Azurite has only JOBS_STORAGE) or not an https
        # endpoint: a bearer token is never sent anywhere else.
        return failed("misconfigured")

    try:
        import httpx
        from azure.core.exceptions import ClientAuthenticationError
        from azure.identity import CredentialUnavailableError
    except ImportError:  # pragma: no cover - both ship in requirements.txt
        return failed("misconfigured")

    started = time.monotonic()
    try:
        token = _credential().get_token(STORAGE_SCOPE).token
    except CredentialUnavailableError:
        return failed("misconfigured")
    except ClientAuthenticationError:
        return failed("forbidden")
    except Exception as exc:  # noqa: BLE001 - classified, never echoed
        log.warning("Queue peek of %s could not get a token: %s", queue, type(exc).__name__)
        return failed("failed")

    remaining = max(0.5, timeout_s - (time.monotonic() - started))
    url = f"{base.rstrip('/')}/{queue}/messages"
    headers: Mapping[str, str] = {
        "Authorization": f"Bearer {token}",
        "x-ms-version": STORAGE_API_VERSION,
        # RFC 1123 in English whatever the process locale (strftime's %a/%b are not).
        "x-ms-date": formatdate(usegmt=True),
        "Accept": "application/xml",
    }
    try:
        with httpx.Client(timeout=remaining, follow_redirects=False) as client:
            response = client.get(
                url,
                params={"peekonly": "true", "numofmessages": str(PEEK_MAX_MESSAGES)},
                headers=headers,
            )
    except httpx.TimeoutException:
        return failed("timeout")
    except httpx.HTTPError as exc:
        log.warning("Queue peek of %s failed: %s", queue, type(exc).__name__)
        return failed("unavailable")
    if response.status_code != 200:
        log.warning("Queue peek of %s answered HTTP %d", queue, response.status_code)
        return failed(_http_error(response.status_code))
    try:
        count, oldest = _parse_peek(response.content)
    except Exception as exc:  # noqa: BLE001 - a body we cannot read is a failed check
        log.warning("Queue peek of %s returned an unreadable body: %s", queue, type(exc).__name__)
        return failed("failed")
    if time.monotonic() - started > timeout_s:
        return QueuePeek(queue=queue, ok=False, count=count, oldest_inserted_at=oldest, error="timeout")
    return QueuePeek(queue=queue, ok=True, count=count, oldest_inserted_at=oldest, error=None)
