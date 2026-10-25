"""The route inventory: every HTTP function, its access level, method and path,
held against docs/api.md section 5 (the route index) and against this file's
own table.

Why both: the table below is what a reviewer reads in a diff when a route
changes, and the parsed document is what the iOS app and the fixtures are
written against. A route that is registered but not documented, documented as
P1 but missing, or registered with another level than its row says fails here.

What it holds, beyond the set itself:

* every HTTP function carries exactly one core.auth level marker, and only
  ``health`` is public (an unmarked handler would serve without any check);
* the level decorator sits directly above ``def`` (the indexed function is the
  marked one) and the HTTP parameter is ``req``;
* every route is anonymous at the host, and its template has no ``api/``
  prefix (the host adds it);
* no Phase 2 or 3 function is registered before its phase.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import NamedTuple

import azure.functions as func
import pytest
from azure.functions.decorators.function_app import Function

from core import auth
from tests.test_function_app import indexed_functions

API_MD = Path(__file__).resolve().parents[2] / "docs" / "api.md"


class Route(NamedTuple):
    method: str
    route: str
    level: str
    allow_blocked: bool = False
    allow_deleting: bool = False


# The registered HTTP surface: docs/api.md section 5, rows P0 and P1.
PHASE_1_ROUTES: dict[str, Route] = {
    "health": Route("GET", "health", "public"),
    "me_redeem": Route("POST", "me/redeem", "token"),
    "me_get": Route("GET", "me", "member"),
    "me_patch": Route("PATCH", "me", "member"),
    "me_delete": Route("DELETE", "me", "member", allow_blocked=True, allow_deleting=True),
    "me_export": Route("GET", "me/export", "member", allow_blocked=True),
    "admin_overview": Route("GET", "admin/overview", "admin"),
    "admin_health": Route("GET", "admin/health", "admin"),
    "admin_invites_list": Route("GET", "admin/invites", "admin"),
    "admin_invites_create": Route("POST", "admin/invites", "admin"),
    "admin_invite_revoke": Route("POST", "admin/invites/{id}/revoke", "admin"),
    "admin_users_list": Route("GET", "admin/users", "admin"),
    "admin_user_block": Route("POST", "admin/users/{uid}/block", "admin"),
    "admin_deletions_list": Route("GET", "admin/deletions", "admin"),
    "admin_deletion_complete": Route("POST", "admin/deletions/{id}/complete", "admin"),
}
NON_HTTP_FUNCTIONS = {"harvest_timer", "maintenance_timer", "account_purge_worker", "ops_jobs_worker"}

# The route index rows: | GET | `/me` | member | **P1** | `me_get` | ... |
_ROW = re.compile(
    r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`/([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*`([a-z_]+)`\s*\|"
)


class DocRow(NamedTuple):
    route: Route
    phase: str


def _level(cell: str) -> tuple[str, bool, bool]:
    """``member (blocked, deleting allowed)`` -> ("member", True, True)."""
    level, _, qualifier = cell.partition("(")
    return level.strip(), "blocked" in qualifier, "deleting" in qualifier


def documented_routes() -> dict[str, DocRow]:
    text = API_MD.read_text(encoding="utf-8")
    section = text[text.index("## 5. Route index") : text.index("## 6. Routes")]
    rows: dict[str, DocRow] = {}
    for line in section.splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        method, path, level_cell, phase_cell, name = match.groups()
        level, blocked, deleting = _level(level_cell)
        phase = phase_cell.replace("*", "").split()[0]  # "**P1**" -> P1, "P0 (kept)" -> P0
        assert name not in rows, f"{name} appears twice in the route index"
        rows[name] = DocRow(Route(method, path, level, blocked, deleting), phase)
    return rows


@pytest.fixture(scope="session")
def functions() -> dict[str, Function]:
    return indexed_functions()


@pytest.fixture(scope="session")
def http_functions(functions: dict[str, Function]) -> dict[str, Function]:
    return {name: fn for name, fn in functions.items() if fn.is_http_function()}


def _registered(fn: Function) -> Route:
    trigger = fn.get_trigger()
    user = fn.get_user_function()
    (method,) = trigger.methods
    return Route(
        method.value,
        trigger.route,
        getattr(user, auth.AUTH_MARKER),
        getattr(user, auth.ALLOW_BLOCKED_MARKER),
        getattr(user, auth.ALLOW_DELETING_MARKER),
    )


# ---------------------------------------------------------------------------
# The set
# ---------------------------------------------------------------------------
def test_the_document_parses_into_the_whole_index() -> None:
    # A parser that silently skipped rows would make every comparison below
    # vacuous, so the parsed rows are counted against the table's own lines.
    text = API_MD.read_text(encoding="utf-8")
    section = text[text.index("## 5. Route index") : text.index("## 6. Routes")]
    table_lines = [line for line in section.splitlines() if re.match(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|", line)]
    rows = documented_routes()
    assert len(rows) == len(table_lines) > len(PHASE_1_ROUTES)
    assert {row.phase for row in rows.values()} == {"P0", "P1", "P2", "P3"}


def test_the_registered_http_functions_are_the_phase_1_rows(http_functions: dict[str, Function]) -> None:
    registered = {name: _registered(fn) for name, fn in http_functions.items()}
    assert registered == PHASE_1_ROUTES


def test_this_table_is_the_documented_one() -> None:
    documented = {name: row.route for name, row in documented_routes().items() if row.phase in ("P0", "P1")}
    assert documented == PHASE_1_ROUTES


def test_no_later_phase_function_is_registered(functions: dict[str, Function]) -> None:
    later = {name for name, row in documented_routes().items() if row.phase in ("P2", "P3")}
    assert later and not later & set(functions)
    # Nor the later workers and timers, which have no row: their phases add them.
    assert not {"cycle_timer", "suggestions_worker"} & set(functions)


def test_the_non_http_functions(functions: dict[str, Function]) -> None:
    assert {name for name, fn in functions.items() if not fn.is_http_function()} == NON_HTTP_FUNCTIONS


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------
def test_every_http_function_carries_one_valid_level(http_functions: dict[str, Function]) -> None:
    for name, fn in http_functions.items():
        user = fn.get_user_function()
        level = getattr(user, auth.AUTH_MARKER, None)
        assert level in auth.AUTH_LEVELS, f"{name} has no access-level decorator"
        # Exactly one: a second level decorator would wrap the first, and the
        # outer marker would hide what the inner one checks.
        inner = getattr(user, "__wrapped__", None)
        assert inner is not None and getattr(inner, auth.AUTH_MARKER, None) is None, f"{name} is decorated twice"
        # The lenient flags exist only on member routes.
        if level != "member":
            assert not getattr(user, auth.ALLOW_BLOCKED_MARKER) and not getattr(user, auth.ALLOW_DELETING_MARKER), name


def test_only_health_is_public(http_functions: dict[str, Function]) -> None:
    public = {
        name for name, fn in http_functions.items() if getattr(fn.get_user_function(), auth.AUTH_MARKER) == "public"
    }
    assert public == {"health"}


def test_admin_routes_are_admin_and_member_routes_are_under_me(http_functions: dict[str, Function]) -> None:
    for name, fn in http_functions.items():
        route = fn.get_trigger().route
        level = getattr(fn.get_user_function(), auth.AUTH_MARKER)
        if route.startswith("admin/"):
            assert level == "admin", name
        elif route.startswith("me"):
            assert level in ("member", "token"), name
        else:
            assert route == "health", name


def test_non_http_functions_carry_no_level(functions: dict[str, Function]) -> None:
    # A level decorator on a timer or a queue worker would try to read an
    # Authorization header from a TimerRequest.
    for name in NON_HTTP_FUNCTIONS:
        assert not hasattr(functions[name].get_user_function(), auth.AUTH_MARKER), name


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def test_every_route_is_anonymous_at_the_host_and_unprefixed(http_functions: dict[str, Function]) -> None:
    for name, fn in http_functions.items():
        trigger = fn.get_trigger()
        assert trigger.auth_level == func.AuthLevel.ANONYMOUS, name
        assert trigger.name == "req", name
        assert not trigger.route.startswith(("api/", "/")), name
        assert len(trigger.methods) == 1, name


def test_the_level_decorator_is_innermost_and_keeps_the_signature(http_functions: dict[str, Function]) -> None:
    for name, fn in http_functions.items():
        user = fn.get_user_function()
        # The worker binds by the visible signature: functools.wraps keeps the
        # handler's own, so `req` (and an output binding's name) stay visible.
        parameters = list(inspect.signature(user).parameters)
        assert parameters[0] == "req", name
        assert user.__name__ == name
        bound_names = {b.name for b in fn.get_bindings() if b.name != "$return"}
        assert bound_names == set(parameters), name


def test_the_only_http_output_binding_is_the_purge_message(http_functions: dict[str, Function]) -> None:
    outputs = {
        name: [
            (b.type, b.name, b.get_dict_repr().get("queueName"), b.get_dict_repr().get("connection"))
            for b in fn.get_bindings()
            if b.type == "queue"
        ]
        for name, fn in http_functions.items()
    }
    assert {name: bindings for name, bindings in outputs.items() if bindings} == {
        "me_delete": [("queue", "purge", "account-purge", "JOBS_STORAGE")]
    }


def test_the_worker_and_timer_bindings(functions: dict[str, Function]) -> None:
    def bindings(name: str) -> list[dict]:
        return sorted(
            ({k: v for k, v in b.get_dict_repr().items() if k != "direction"} for b in functions[name].get_bindings()),
            key=lambda b: b["type"],
        )

    assert bindings("maintenance_timer") == [
        {"type": "queue", "name": "purge", "queueName": "account-purge", "connection": "JOBS_STORAGE"},
        {
            "type": "timerTrigger",
            "name": "timer",
            "schedule": "0 15 2 * * *",
            "runOnStartup": False,
            "useMonitor": True,
        },
    ]
    assert bindings("account_purge_worker") == [
        {"type": "queueTrigger", "name": "msg", "queueName": "account-purge", "connection": "JOBS_STORAGE"}
    ]
    assert bindings("ops_jobs_worker") == [
        {"type": "queueTrigger", "name": "msg", "queueName": "ops-jobs", "connection": "JOBS_STORAGE"}
    ]
