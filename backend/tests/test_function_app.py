"""The Functions surface: what function_app.py registers, and the two functions
whose contracts predate Phase 1 (``health`` and ``harvest_timer``).

Read from the app object the worker indexes rather than from the source text: a
string check passes whenever the text appears anywhere, even in a comment, while
the indexed app is what actually gets deployed. The HTTP routes' levels, methods
and paths are test_route_inventory.py's; this module holds the whole set, the
blueprint layout, and the unchanged behaviour of health and the harvest timer.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import azure.functions as func
import pytest
from azure.functions.decorators.function_app import Function

import function_app

# Kept on the app object, so every test module shares one index (see below).
_INDEX_ATTR = "_mynews_test_index"

PHASE_1_FUNCTIONS = {
    # api/public.py
    "health",
    # api/account.py
    "me_redeem",
    "me_get",
    "me_patch",
    "me_delete",
    "me_export",
    # api/admin.py
    "admin_overview",
    "admin_health",
    "admin_invites_list",
    "admin_invites_create",
    "admin_invite_revoke",
    "admin_users_list",
    "admin_user_block",
    "admin_deletions_list",
    "admin_deletion_complete",
    # jobs/timers.py
    "harvest_timer",
    "maintenance_timer",
    # jobs/queues.py
    "account_purge_worker",
    "ops_jobs_worker",
}
BLUEPRINT_MODULES = {"api.public", "api.account", "api.admin", "jobs.timers", "jobs.queues"}


def indexed_functions() -> dict[str, Function]:
    """The functions the worker would index, by name.

    Indexed once per process, the way the worker does it: azure-functions keeps
    the names it has seen on the app object, so a second get_functions() in the
    same process reports every function as a duplicate of itself. The index is
    therefore cached on that same app object, which every test module imports
    as the one ``function_app``, so the modules share it whatever pytest's
    import mode.
    """
    index = getattr(function_app.app, _INDEX_ATTR, None)
    if index is None:
        index = {f.get_function_name(): f for f in function_app.app.get_functions()}
        setattr(function_app.app, _INDEX_ATTR, index)
    return index


@pytest.fixture(scope="session")
def functions() -> dict[str, Function]:
    return indexed_functions()


def _get_health(functions: dict[str, Function], headers: dict[str, str] | None = None) -> func.HttpResponse:
    req = func.HttpRequest(method="GET", url="http://localhost/api/health", headers=headers or {}, body=b"")
    return functions["health"].get_user_function()(req)


@pytest.fixture
def cosmos_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Make any Cosmos access raise, and remember it.

    Raising alone is not enough: the old health handler caught every exception,
    so a swallowed call would still come back 200. The list catches that.
    """
    import core.cosmos

    calls: list[tuple] = []

    def refuse(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("health must not touch Cosmos")

    monkeypatch.setattr(core.cosmos, "get_container", refuse)
    return calls


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------
def test_exactly_the_phase_1_functions_are_registered(functions: dict[str, Function]) -> None:
    # A later phase's function (cycle_timer, suggestions_worker, the content
    # routes) has to be added here and to docs/api.md section 5 on purpose.
    assert set(functions) == PHASE_1_FUNCTIONS


def test_function_app_only_registers_blueprints(functions: dict[str, Function]) -> None:
    # function_app.py holds the app, the log levels and the registrations; no
    # function is defined in it, so nothing can bypass the blueprints' layout.
    modules = {f.get_user_function().__module__ for f in functions.values()}
    assert modules == BLUEPRINT_MODULES
    assert isinstance(function_app.app, func.FunctionApp)
    # Anonymous at the host, for the routes and as the app default: auth is
    # core.auth's job, and a function key is nothing a client can keep secret.
    assert function_app.app.auth_level == func.AuthLevel.ANONYMOUS


def test_registering_does_not_import_the_heavy_stages() -> None:
    # Cold starts: the harvest's trafilatura (lxml) and feedparser, the v1
    # model stages, and the clients only the admin and owner paths need
    # (azure.identity, msal, httpx) load inside the handlers that use them,
    # never at indexing time. Checked in a fresh interpreter, because this test
    # process has imported everything already.
    heavy = (
        "trafilatura",
        "feedparser",
        "lxml",
        "pipeline.harvest",
        "numpy",
        "openai",
        "azure.identity",
        "msal",
        "httpx",
        "requests",
        "yaml",
    )
    script = f"import sys, function_app; print(','.join(m for m in {heavy!r} if m in sys.modules))"
    backend = Path(function_app.__file__).resolve().parent
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=backend, capture_output=True, text=True, timeout=120, check=True
    )
    assert result.stdout.strip() == ""


def _imports(path: Path, *, top_level_only: bool = False) -> set[str]:
    """Absolute imports of a module as dotted names (``from api import http``
    is ``api.http``). ``top_level_only`` leaves out imports inside functions and
    under ``if TYPE_CHECKING``, which cost nothing at import time."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = tree.body if top_level_only else list(ast.walk(tree))
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names |= {
                f"{node.module}.{alias.name}" if node.module in ("api", "jobs", "core") else node.module
                for alias in node.names
            }
    return names


def test_import_rules() -> None:
    # The layout docs/api.md and the Phase 1 contract fix, held statically so a
    # branch no test reaches cannot break them either.
    backend = Path(function_app.__file__).resolve().parent
    stdlib = set(sys.stdlib_module_names)

    # api/http.py: helpers only, and never core.auth (core.auth renders its
    # answers with these helpers; the other direction would be a cycle).
    for name in _imports(backend / "api" / "http.py"):
        assert name.split(".")[0] in stdlib or name in {"azure.functions", "core.errors", "core.models"}, name

    # core never reaches up into api, except core.auth into api.http.
    for path in sorted((backend / "core").glob("*.py")):
        upward = {name for name in _imports(path) if name == "api" or name.startswith(("api.", "jobs."))}
        assert upward <= ({"api.http"} if path.name == "auth.py" else set()), (path.name, upward)

    # No heavy import at module level on the HTTP and job paths.
    heavy = {"pipeline", "numpy", "openai", "trafilatura", "feedparser", "msal", "httpx", "azure.identity"}
    for path in [backend / "function_app.py", *(backend / "api").glob("*.py"), *(backend / "jobs").glob("*.py")]:
        for name in _imports(path, top_level_only=True):
            assert not any(name == h or name.startswith(h + ".") for h in heavy), (path.name, name)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
def test_health_is_an_anonymous_get(functions: dict[str, Function]) -> None:
    # The one public route: a client probes it before it has any credential.
    trigger = functions["health"].get_trigger()
    assert trigger.route == "health"
    assert trigger.methods == [func.HttpMethod.GET]
    assert trigger.auth_level == func.AuthLevel.ANONYMOUS


def test_health_is_static_and_never_touches_cosmos(
    functions: dict[str, Function], cosmos_calls: list[tuple], monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.config import get_settings

    monkeypatch.delenv("MIN_SUPPORTED_BUILD", raising=False)
    settings_reads = get_settings.cache_info()

    resp = _get_health(functions)

    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
    assert resp.headers["Cache-Control"] == "no-store"
    body = json.loads(resp.get_body())
    assert set(body) == {"status", "api", "minBuild", "time"}
    assert body["status"] == "ok"
    assert body["api"] == 1
    assert body["minBuild"] == 0
    # ISO-8601 in UTC with a literal Z, the format every other timestamp uses.
    assert body["time"].endswith("Z")
    when = datetime.fromisoformat(body["time"].replace("Z", "+00:00"))
    assert abs(datetime.now(timezone.utc) - when) < timedelta(minutes=1)
    assert cosmos_calls == []
    # Not even the settings object: nothing to fail on.
    after = get_settings.cache_info()
    assert (after.hits, after.misses) == (settings_reads.hits, settings_reads.misses)


def test_health_answers_with_a_request_id_even_without_auth_settings(
    functions: dict[str, Function], cosmos_calls: list[tuple], env
) -> None:
    # Closed auth (no tenant yet) closes every route but this one.
    env(ENTRA_TENANT_ID=None, MYNEWS_HMAC_KEY=None)
    resp = _get_health(functions, {"X-Request-Id": "probe-12345678"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"] == "probe-12345678"
    generated = _get_health(functions).headers["X-Request-Id"]
    assert len(generated) == 36  # a server-issued UUID when the client sent none


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 0), ("412", 412), (" 7 ", 7), ("not-a-number", 0), ("-3", 0)],
)
def test_min_build_comes_from_the_app_setting(
    raw: str, expected: int, functions: dict[str, Function], cosmos_calls: list[tuple], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bad value degrades to 0 instead of failing, and nothing about the
    # failure reaches the response.
    monkeypatch.setenv("MIN_SUPPORTED_BUILD", raw)

    resp = _get_health(functions)

    assert resp.status_code == 200
    body = json.loads(resp.get_body())
    assert set(body) == {"status", "api", "minBuild", "time"}
    assert body["minBuild"] == expected
    assert cosmos_calls == []


# ---------------------------------------------------------------------------
# The v1 harvest timer, moved verbatim
# ---------------------------------------------------------------------------
def test_harvest_timer_binding_is_unchanged(functions: dict[str, Function]) -> None:
    # The schedule stays an app setting, and a cold start or scale-out must not
    # fire an extra harvest.
    trigger = functions["harvest_timer"].get_trigger()
    assert trigger.schedule == "%HARVEST_SCHEDULE%"
    assert trigger.run_on_startup is False
    assert trigger.use_monitor is True
    assert trigger.name == "timer"
    assert [b.type for b in functions["harvest_timer"].get_bindings()] == ["timerTrigger"]


def test_harvest_timer_still_runs_the_harvest(functions: dict[str, Function], monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.harvest
    from core.models import RunRecord

    calls: list[tuple] = []

    def fake_run_harvest(*args: object, **kwargs: object) -> RunRecord:
        calls.append((args, kwargs))
        return RunRecord(id="harvest-test", items_new=3)

    monkeypatch.setattr(pipeline.harvest, "run_harvest", fake_run_harvest)

    class Timer:
        past_due = True

    functions["harvest_timer"].get_user_function()(Timer())

    # No source filter and no force: the timer's run follows each feed's own
    # schedule. Both arguments are only ever passed by the ops-jobs worker.
    assert calls == [((), {})]
