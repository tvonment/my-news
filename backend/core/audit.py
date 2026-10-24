"""The audit trail and the alert line.

``record()`` writes one ``sys/audit|...`` document per state change an admin or
the system makes (docs/data-model.md section 4.7). It is best-effort on purpose:
an audit entry describes a change that already happened, so failing the request
because the description could not be stored would leave the change in place and
tell the caller it failed. The failure is logged instead.

``alert()`` is the one log line the alert rules match on: ``ALERT <kind> {json}``
at ERROR. A kind outside ALERT_KINDS raises, because an alert nobody's rule
matches is an alert nobody receives.

Neither ever carries an oid, an email address, an invite code, a token or a
member's own text: entries are keyed by the pseudonymous uid and kept a year.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from typing import Any, Mapping

from core import cosmos
from core.models import PK_SYS, T_AUDIT, TTL_AUDIT, audit_doc_id, iso, utcnow

log = logging.getLogger(__name__)

PHASE1_ACTIONS = frozenset(
    {
        "member.bootstrapped",
        "invite.created",
        "invite.revoked",
        "invite.redeemed",
        "user.blocked",
        "user.unblocked",
        "account.deletion_requested",
        "account.purged",
        "identity.removed",
    }
)
# Phases 2 and 3 add catalog.updated, run.requested, suggestion.approved,
# suggestion.rejected and report.resolved here, in the phase that writes them.
ACTIONS = PHASE1_ACTIONS

TARGET_TYPES = frozenset({"user", "invite", "deletion", "source", "suggestion", "report", "run"})

ALERT_KINDS = frozenset(
    {
        "budget_brake",
        "jwks_fetch_failed",
        "graph_delete_failed",
        "secret_expiry",
        "robots_changed",
        "identity_removal_overdue",
        "purge_failed",
    }
)

SYSTEM_ACTOR = "system"


def _current_request_id() -> str | None:
    # Imported here: core.auth imports this module (via core.members).
    from core import auth

    try:
        return auth.current().request_id
    except RuntimeError:
        return None


def record(
    action: str,
    *,
    actor: str,
    target_type: str,
    target_id: str,
    details: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Write one audit entry; never raises.

    ``actor`` is the acting admin's uid or "system". ``request_id`` defaults to
    the X-Request-Id of the request being handled, when there is one.
    """
    if action not in ACTIONS or target_type not in TARGET_TYPES:
        # A programming error, caught by the tests; still never fails the request.
        log.error("Audit entry refused: unknown action %r or target type %r", action, target_type)
        return
    at = now or utcnow()
    doc = {
        "id": audit_doc_id(at, secrets.token_hex(4)),
        "pk": PK_SYS,
        "type": T_AUDIT,
        "v": 1,
        "created_at": iso(at),
        "actor": actor,
        "action": action,
        "target": {"type": target_type, "id": target_id},
        "details": dict(details or {}),
        "request_id": request_id if request_id is not None else _current_request_id(),
        "ttl": TTL_AUDIT,
    }
    try:
        cosmos.create(doc)
    except Exception:
        log.exception("Audit entry %s for %s %s could not be written", action, target_type, target_id)


def alert(kind: str, **fields: Any) -> None:
    """Log ``ALERT <kind> {fields}`` at ERROR, for the log alert rules."""
    if kind not in ALERT_KINDS:
        raise ValueError(f"unknown alert kind {kind!r}")
    log.error("ALERT %s %s", kind, json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str))
