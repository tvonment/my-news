"""The Functions surface after v1's retirement on 2026-10-21.

Read from the app object the worker indexes rather than from the source text: a
string check passes whenever the text appears anywhere, even in a comment, while
the indexed app is what actually gets deployed.
"""

from __future__ import annotations

import functools
import json
from datetime import datetime, timedelta, timezone

import azure.functions as func
import pytest
from azure.functions.decorators.function_app import Function

import function_app


@functools.cache
def _functions() -> dict[str, Function]:
    # Index once, the way the worker does. azure-functions keeps the names it
    # has seen on the app object, so a second get_functions() call in the same
    # process reports every function as a duplicate of itself.
    return {f.get_function_name(): f for f in function_app.app.get_functions()}


def _get_health() -> func.HttpResponse:
    req = func.HttpRequest(method="GET", url="http://localhost/api/health", body=b"")
    return _functions()["health"].get_user_function()(req)


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


def test_only_health_and_harvest_remain() -> None:
    # Every v1 route and the digest and profile timers are gone. A function
    # added before Phase 1's route inventory exists has to be added here too,
    # on purpose.
    assert set(_functions()) == {"health", "harvest_timer"}


def test_health_is_an_anonymous_get() -> None:
    # The one public route: a client probes it before it has any credential.
    trigger = _functions()["health"].get_trigger()
    assert trigger.route == "health"
    assert trigger.methods == [func.HttpMethod.GET]
    assert trigger.auth_level == func.AuthLevel.ANONYMOUS


def test_harvest_timer_binding_is_unchanged() -> None:
    # The schedule stays an app setting, and a cold start or scale-out must not
    # fire an extra harvest.
    trigger = _functions()["harvest_timer"].get_trigger()
    assert trigger.schedule == "%HARVEST_SCHEDULE%"
    assert trigger.run_on_startup is False
    assert trigger.use_monitor is True


def test_health_is_static_and_never_touches_cosmos(
    cosmos_calls: list[tuple], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MIN_SUPPORTED_BUILD", raising=False)

    resp = _get_health()

    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 0), ("412", 412), (" 7 ", 7), ("not-a-number", 0)],
)
def test_min_build_comes_from_the_app_setting(
    raw: str, expected: int, cosmos_calls: list[tuple], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bad value degrades to 0 instead of failing, and nothing about the
    # failure reaches the response.
    monkeypatch.setenv("MIN_SUPPORTED_BUILD", raw)

    resp = _get_health()

    assert resp.status_code == 200
    body = json.loads(resp.get_body())
    assert set(body) == {"status", "api", "minBuild", "time"}
    assert body["minBuild"] == expected
    assert cosmos_calls == []


def test_harvest_timer_still_runs_the_harvest(monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.harvest
    from core.models import RunRecord

    calls: list[tuple] = []

    def fake_run_harvest(*args: object, **kwargs: object) -> RunRecord:
        calls.append((args, kwargs))
        return RunRecord(id="harvest-test", items_new=3)

    monkeypatch.setattr(pipeline.harvest, "run_harvest", fake_run_harvest)

    class Timer:
        past_due = True

    _functions()["harvest_timer"].get_user_function()(Timer())

    # No source filter: that argument was only ever passed by the retired
    # /api/refresh.
    assert calls == [((), {})]
