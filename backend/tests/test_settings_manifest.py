"""The settings manifest: every setting the app reads is deployed, and nothing
the app does not read is (the plan, "Architecture: canonical choices", item 3).

Three places name the Function App's settings, and only this test keeps them
together:

* the code that reads them: core/config.py's Settings, every other
  ``os.environ`` read, and what the host resolves for the indexed functions
  (``%HARVEST_SCHEDULE%``, the ``JOBS_STORAGE`` connection) and for itself;
* infra/modules/appsettings.bicep, one ``sites/config`` resource that REPLACES
  the whole live collection on every provision. A setting missing there is
  deleted from the app by the next provision; one that only exists there does
  nothing, or worse, looks as if it did (a schedule nobody binds);
* scripts/postprovision.sh, which writes backend/local.settings.json for
  ``func start``.

None of them fails at deploy time when it drifts from the others. A setting the
code reads with a default quietly runs on the default, and the auth settings
close every route (503 auth_unavailable). The host's own settings are the worst
case, because no code reads them at all: without AzureWebJobsStorage__accountName
the timers stop, without APPLICATIONINSIGHTS_CONNECTION_STRING every alert goes
quiet, and a timer whose AzureWebJobs.<function>.Disabled switch is gone comes
back on.

So each list is derived from its source rather than restated here: an AST scan
of the app's code, the bindings of the indexed app, the Bicep text, and the file
postprovision.sh really writes (its Python step runs against a throwaway
directory, with a stand-in value for every variable the shell exports). The few
deliberate differences are tables below, each with its reason.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterator, NamedTuple

import pytest
from azure.functions.decorators.function_app import Function

from tests.test_function_app import indexed_functions

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
APPSETTINGS_BICEP = REPO / "infra" / "modules" / "appsettings.bicep"
DATA_BICEP = REPO / "infra" / "modules" / "data.bicep"
MAIN_BICEP = REPO / "infra" / "main.bicep"
MAIN_PARAMETERS = REPO / "infra" / "main.parameters.json"
POSTPROVISION_SH = REPO / "scripts" / "postprovision.sh"
WHATIF_SH = REPO / "scripts" / "whatif.sh"

# Top-level directories of backend/ that are not the app: local tools run by
# hand (never by the host, and never with the app's settings) and the tests.
NOT_APP_CODE = frozenset({"scripts", "tests"})

# core/config.py's readers; their first argument is the setting's name.
ENV_HELPERS = frozenset({"_env", "_int_env", "_float_env", "_bool_env", "_csv_env"})

# Reads whose name is a variable, by (file, enclosing function): the helpers
# above, which every literal read goes through, and admin health's config
# check, which re-reads names that are literal reads elsewhere (checked in
# test_dynamic_reads_are_the_known_ones).
DYNAMIC_READS = frozenset(
    {
        ("core/config.py", "_env"),
        ("core/config.py", "_float_env"),
        ("core/config.py", "_int_env"),
        ("core/config.py", "_bool_env"),
        ("core/config.py", "_csv_env"),
        ("core/ops_health.py", "config_check"),
    }
)

# Read by the code and deliberately NOT deployed. Each is named in
# appsettings.bicep's header, which says the same; deploying one again means
# deleting it here.
NOT_DEPLOYED: dict[str, str] = {
    # Phase 0 retired v1's digest and profile runs. Only their stages and
    # helpers use these values, and no registered function reaches those
    # (test_index_coverage's RETIRED_V1_MODULES holds that). Phase 2 deploys the
    # model settings again with the cycle.
    "FOUNDRY_ENDPOINT": "retired v1 stages",
    "FOUNDRY_TOKEN_SCOPE": "retired v1 stages; the default is the value",
    "MODEL_SCORER": "retired v1 stages",
    "MODEL_SUMMARIZER": "retired v1 stages",
    "MODEL_EMBEDDING": "retired v1 stages",
    "MODEL_SCORER_ID": "retired v1 stages",
    "MODEL_SUMMARIZER_ID": "retired v1 stages",
    "MODEL_EMBEDDING_ID": "retired v1 stages",
    "REASONING_EFFORT_SCORER": "retired v1 stages",
    "REASONING_EFFORT_SUMMARIZER": "retired v1 stages",
    "DAILY_TOKEN_BUDGET_USD": "retired v1 stages",
    "PUSH_ENABLED": "retired v1 stages",
    "EMBEDDING_DIMENSIONS": "retired v1 stages",
    "SUMMARIZE_THRESHOLD": "retired v1 stages",
    "MAX_SCORED_PER_RUN": "retired v1 stages",
    "CLUSTER_THRESHOLD": "retired v1 stages",
    "EXPLORATION_QUOTA": "retired v1 stages",
    # Tuning knobs of the v1 harvest whose code default is the value.
    "ARTICLE_TTL_DAYS": "the default is the value",
    "MAX_ARTICLE_AGE_DAYS": "the default is the value",
    "USER_AGENT": "the default is the value",
}

# What the host needs for itself, beyond the bindings: its own storage (timer
# leases, keys, scale state), reached by the managed identity, and telemetry,
# which every alert rule reads.
HOST_SETTINGS = frozenset({"AzureWebJobsStorage__accountName", "APPLICATIONINSIGHTS_CONNECTION_STRING"})

# The local file's deliberate differences (postprovision.sh's header says why).
# Azurite instead of the identity-based storage account:
LOCAL_INSTEAD = {"AzureWebJobsStorage__accountName": "AzureWebJobsStorage"}
# Not written at all:
LOCAL_WITHOUT = {"APPLICATIONINSIGHTS_CONNECTION_STRING": "local telemetry stays off"}
# Only `func start` needs these.
LOCAL_ONLY = frozenset({"FUNCTIONS_WORKER_RUNTIME", "AzureWebJobsStorage"})
# Written empty on purpose: a laptop has no Azure queue for admin health to peek.
LOCAL_EMPTY = frozenset({"JOBS_STORAGE__queueServiceUri"})

# main.parameters.json maps these from the azd env too, but whatif.sh leaves
# them out on purpose, as its comments say.
WHATIF_LEFT_OUT = {"hmacKey": "what-if never evaluates a secure parameter; the key stays off the command line"}


# ---------------------------------------------------------------------------
# The code's reads
# ---------------------------------------------------------------------------
class EnvRead(NamedTuple):
    name: str | None  # None when the name is not a string literal
    file: str  # relative to backend/, e.g. "core/config.py"
    function: str | None  # the enclosing function, None at module level
    line: int


def app_modules() -> list[Path]:
    """Every module the Functions host can import: backend/ minus local tools,
    tests, the venv and caches."""
    modules = []
    for path in sorted(BACKEND.rglob("*.py")):
        parts = path.relative_to(BACKEND).parts
        if parts[0] in NOT_APP_CODE or any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        modules.append(path)
    return modules


def _is_environ(node: ast.AST) -> bool:
    """``os.environ``, or ``environ`` imported from os."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _read_key(node: ast.AST) -> ast.AST | None:
    """The name expression of an environment read, or None if ``node`` is none."""
    if isinstance(node, ast.Subscript) and _is_environ(node.value):
        return node.slice
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id in ENV_HELPERS:
        return node.args[0]
    if isinstance(func, ast.Attribute):
        if func.attr in {"get", "setdefault", "pop"} and _is_environ(func.value):
            return node.args[0]
        if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
            return node.args[0]
    return None


def env_reads(path: Path) -> Iterator[EnvRead]:
    rel = path.relative_to(BACKEND).as_posix()

    def walk(node: ast.AST, function: str | None) -> Iterator[EnvRead]:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            key = _read_key(child)
            if key is not None:
                name = key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None
                yield EnvRead(name, rel, function, getattr(child, "lineno", 0))
            yield from walk(child, inner)

    yield from walk(ast.parse(path.read_text(encoding="utf-8")), None)


@pytest.fixture(scope="module")
def reads() -> list[EnvRead]:
    found = [read for path in app_modules() for read in env_reads(path)]
    # The scan must see core/config.py's readers, or it is scanning nothing.
    assert any(read.name == "COSMOS_ENDPOINT" for read in found)
    return found


@pytest.fixture(scope="module")
def code_settings(reads: list[EnvRead]) -> set[str]:
    return {read.name for read in reads if read.name is not None}


# ---------------------------------------------------------------------------
# The indexed app: bindings and timers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def functions() -> dict[str, Function]:
    return indexed_functions()


class Bindings(NamedTuple):
    expressions: set[str]  # %NAME% app-setting expressions
    connections: set[str]  # storage connection names
    queues: set[str]  # queue names, triggers and outputs alike
    timers: set[str]  # timer-triggered function names


@pytest.fixture(scope="module")
def bindings(functions: dict[str, Function]) -> Bindings:
    found = Bindings(set(), set(), set(), set())
    for name, function in functions.items():
        for binding in function.get_bindings():
            spec = binding.get_dict_repr()
            for value in spec.values():
                if isinstance(value, str):
                    found.expressions.update(re.findall(r"%([^%\s]+)%", value))
            if spec.get("connection"):
                found.connections.add(spec["connection"])
            if spec.get("queueName"):
                found.queues.add(spec["queueName"])
            if spec.get("type") == "timerTrigger":
                found.timers.add(name)
    assert found.timers, "no timer found: the binding shapes changed under this test"
    return found


@pytest.fixture(scope="module")
def host_needs(bindings: Bindings) -> set[str]:
    """Every setting the host resolves itself, in Azure."""
    return (
        set(HOST_SETTINGS)
        | bindings.expressions
        # Identity-based queue connections: <connection>__queueServiceUri.
        | {f"{connection}__queueServiceUri" for connection in bindings.connections}
        | {f"AzureWebJobs.{timer}.Disabled" for timer in bindings.timers}
    )


# ---------------------------------------------------------------------------
# appsettings.bicep
# ---------------------------------------------------------------------------
def _bicep_timers(text: str) -> list[str]:
    match = re.search(r"^var timerFunctions = \[(?P<body>.*?)^\]", text, re.M | re.S)
    assert match, "appsettings.bicep has no `var timerFunctions = [...]` list"
    return re.findall(r"'([^']+)'", match.group("body"))


@pytest.fixture(scope="module")
def bicep_settings() -> set[str]:
    """The names in appsettings.bicep's `settings` object, plus one switch per
    entry of `timerFunctions`."""
    text = APPSETTINGS_BICEP.read_text(encoding="utf-8")
    match = re.search(r"^var settings = union\(\s*\{(?P<body>.*?)^\s*\},\s*timerSwitches\s*\)", text, re.M | re.S)
    assert match, "appsettings.bicep no longer builds `var settings = union({...}, timerSwitches)`; update this parser"
    body = match.group("body")
    names = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", body, re.M))
    names |= set(re.findall(r"^\s*'([^']+)'\s*:", body, re.M))
    # The switches are built from the list; hold the template to that shape.
    assert "name => 'AzureWebJobs.${name}.Disabled'" in text
    return names | {f"AzureWebJobs.{timer}.Disabled" for timer in _bicep_timers(text)}


# ---------------------------------------------------------------------------
# postprovision.sh, run
# ---------------------------------------------------------------------------
class LocalFile(NamedTuple):
    values: dict[str, str]
    mode: int
    timers: list[str]  # TIMERS="..."
    exported: set[str]  # the `export` line
    fetched: set[str]  # NAME="$(get NAME)", i.e. read from the azd env
    script: str  # the Python step


def _sentinel(name: str) -> str:
    return f"<from-azd-env:{name}>"


@pytest.fixture(scope="module")
def local_file(tmp_path_factory: pytest.TempPathFactory) -> LocalFile:
    text = POSTPROVISION_SH.read_text(encoding="utf-8")
    script = re.search(r"^python3 - <<'PY'\n(?P<code>.*?)^PY$", text, re.M | re.S)
    timers = re.search(r'^TIMERS="([^"]*)"$', text, re.M)
    export = re.search(r"^export ((?:[^\n]*\\\n)*[^\n]*)$", text, re.M)
    assert script and timers and export, "postprovision.sh changed shape; update this test's parser"
    exported = set(export.group(1).replace("\\\n", " ").split())
    fetched = set(re.findall(r'^([A-Z][A-Z0-9_]*)="\$\(get \1\)"$', text, re.M))

    root = tmp_path_factory.mktemp("postprovision")
    (root / "backend").mkdir()
    out = root / "backend" / "local.settings.json"
    env = {"PATH": os.environ.get("PATH", ""), "OUT": str(out), "TIMERS": timers.group(1)}
    env.update({name: _sentinel(name) for name in exported - {"OUT", "TIMERS"}})
    # -I: no user site, no PYTHON* variables; the step gets exactly this env.
    result = subprocess.run(
        [sys.executable, "-I", "-c", script.group("code")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["IsEncrypted"] is False
    return LocalFile(
        values=document["Values"],
        mode=stat.S_IMODE(out.stat().st_mode),
        timers=timers.group(1).split(),
        exported=exported,
        fetched=fetched,
        script=script.group("code"),
    )


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------
def test_every_setting_the_code_reads_is_deployed_and_written_locally(
    code_settings: set[str], bicep_settings: set[str], local_file: LocalFile
) -> None:
    wanted = code_settings - set(NOT_DEPLOYED)
    assert sorted(wanted - bicep_settings) == [], "read by the code, missing from infra/modules/appsettings.bicep"
    assert sorted(wanted - set(local_file.values)) == [], "read by the code, not written by scripts/postprovision.sh"


def test_every_setting_the_host_needs_is_deployed_and_has_its_local_copy(
    host_needs: set[str], bindings: Bindings, bicep_settings: set[str], local_file: LocalFile
) -> None:
    assert sorted(host_needs - bicep_settings) == [], "needed by the host, missing from appsettings.bicep"
    local = set(local_file.values)
    for name in sorted(host_needs):
        if name in LOCAL_WITHOUT:
            assert name not in local, name
            continue
        if name.endswith("__queueServiceUri") and name.removesuffix("__queueServiceUri") in bindings.connections:
            # Locally the connection is an Azurite connection string under the
            # bare name, which makes the host ignore the identity settings.
            assert name.removesuffix("__queueServiceUri") in local, name
            continue
        assert LOCAL_INSTEAD.get(name, name) in local, name


def test_nothing_is_deployed_that_nothing_reads(
    code_settings: set[str], host_needs: set[str], bicep_settings: set[str]
) -> None:
    # appsettings.bicep replaces the live collection, so an extra entry is not
    # harmless clutter: it is a setting an operator tunes and nothing obeys.
    # A schedule must be bound as %NAME% by its trigger to count.
    assert sorted(bicep_settings - code_settings - host_needs) == []


def test_nothing_is_written_locally_that_nothing_reads(
    code_settings: set[str], host_needs: set[str], bindings: Bindings, local_file: LocalFile
) -> None:
    known = code_settings | {LOCAL_INSTEAD.get(name, name) for name in host_needs} | LOCAL_ONLY | bindings.connections
    assert sorted(set(local_file.values) - known) == []


def test_timer_switches_cover_exactly_the_registered_timers(bindings: Bindings, local_file: LocalFile) -> None:
    # One switch per timer, in both places: a timer added in code without one
    # cannot be switched off for mynews-dev, and runs locally on `func start`.
    bicep_timers = _bicep_timers(APPSETTINGS_BICEP.read_text(encoding="utf-8"))
    assert sorted(bicep_timers) == sorted(bindings.timers)
    assert sorted(local_file.timers) == sorted(bindings.timers)
    for timer in bindings.timers:
        # A local host never harvests or runs maintenance against production.
        assert local_file.values[f"AzureWebJobs.{timer}.Disabled"] == "true", timer


def test_the_local_differences_are_the_documented_ones(local_file: LocalFile, bindings: Bindings) -> None:
    text = APPSETTINGS_BICEP.read_text(encoding="utf-8")
    # The write guard: off in Azure, on for every local run.
    assert re.search(r"^\s*LOCAL_READONLY: 'false'$", text, re.M)
    assert local_file.values["LOCAL_READONLY"] == "true"
    # Host state and the job queues stay on this machine.
    assert local_file.values["AzureWebJobsStorage"] == "UseDevelopmentStorage=true"
    for connection in bindings.connections:
        assert local_file.values[connection] == "UseDevelopmentStorage=true", connection
    # Each omission is explained where the file is written.
    script = POSTPROVISION_SH.read_text(encoding="utf-8")
    for name in LOCAL_WITHOUT:
        assert name in script, f"postprovision.sh should say why {name} is not written"


def test_local_values_come_from_the_azd_env(local_file: LocalFile, code_settings: set[str]) -> None:
    for name, value in local_file.values.items():
        if value.startswith("<from-azd-env:"):
            # Exported to the Python step, and read from the azd env first.
            assert value == _sentinel(name), (name, value)
            assert name in local_file.fetched, f'{name} is exported but never read with NAME="$(get NAME)"'
    # A setting the code reads gets a value: one the shell forgot to export
    # would arrive empty and silently fall back to the code's default.
    empty = {name for name in code_settings & set(local_file.values) if not local_file.values[name]}
    assert empty == set(LOCAL_EMPTY)


def test_the_local_file_is_private(local_file: LocalFile) -> None:
    # It holds MYNEWS_HMAC_KEY.
    assert local_file.mode == 0o600


def test_the_local_step_runs_on_the_system_python(local_file: LocalFile) -> None:
    # postprovision.sh runs `python3`, which on macOS is the system 3.9.
    ast.parse(local_file.script, feature_version=(3, 9))


def test_dynamic_reads_are_the_known_ones(reads: list[EnvRead], code_settings: set[str]) -> None:
    dynamic = {(read.file, read.function) for read in reads if read.name is None}
    assert dynamic == DYNAMIC_READS
    # Admin health's config check re-reads these by name; each must also be a
    # literal read, so the checks above hold it to the manifest.
    from core import auth, ops_health

    assert sorted({*auth.AUTH_SETTING_NAMES, *ops_health.EXTRA_REQUIRED_SETTINGS} - code_settings) == []


def test_the_not_deployed_list_is_current_and_explained(
    code_settings: set[str], bicep_settings: set[str], local_file: LocalFile
) -> None:
    header = APPSETTINGS_BICEP.read_text(encoding="utf-8")
    wildcards = {match.group(1) for match in re.finditer(r"\b([A-Z][A-Z0-9_]*_)\*", header)}
    for name in NOT_DEPLOYED:
        assert name in code_settings, f"{name} is no longer read: drop it from NOT_DEPLOYED"
        assert name not in bicep_settings, f"{name} is deployed after all: drop it from NOT_DEPLOYED"
        assert name not in local_file.values, name
        named = re.search(rf"\b{re.escape(name)}\b", header) or any(name.startswith(w) for w in wildcards)
        assert named, f"appsettings.bicep's header should say why {name} is not deployed"


# ---------------------------------------------------------------------------
# The values' path: azd env -> main.parameters.json -> main.bicep -> settings
# ---------------------------------------------------------------------------
def _module_params(text: str, module: str) -> set[str]:
    match = re.search(rf"^module {module} '[^']+' = \{{.*?^  params: \{{(?P<body>.*?)^  \}}", text, re.M | re.S)
    assert match, f"main.bicep has no module {module} with params"
    return set(re.findall(r"^\s+([A-Za-z_][A-Za-z0-9_]*):", match.group("body"), re.M))


def _bicep_params(text: str) -> set[str]:
    return set(re.findall(r"^param ([A-Za-z_][A-Za-z0-9_]*) ", text, re.M))


def test_main_bicep_hands_every_setting_parameter_to_the_settings_module() -> None:
    # The settings parameters all have defaults, so one that main.bicep forgets
    # to pass compiles fine and deploys the default: an empty tenant id closes
    # the API, and so does an empty HMAC key.
    main = MAIN_BICEP.read_text(encoding="utf-8")
    shared = _bicep_params(APPSETTINGS_BICEP.read_text(encoding="utf-8")) & _bicep_params(main)
    assert shared, "no parameter shared between main.bicep and appsettings.bicep: the parser is broken"
    assert sorted(shared - _module_params(main, "appSettings")) == []


def test_whatif_previews_the_parameters_azd_provisions() -> None:
    # whatif.sh runs before every provision and must preview what azd will
    # deploy: a parameter it leaves out previews at its Bicep default, and
    # appsettings.bicep's checks on that value never run in the what-if.
    mapped = {
        name: re.fullmatch(r"\$\{([A-Z0-9_]+)(=[^}]*)?\}", spec["value"])
        for name, spec in json.loads(MAIN_PARAMETERS.read_text(encoding="utf-8"))["parameters"].items()
    }
    assert all(mapped.values()), "main.parameters.json maps every parameter from one azd env variable"
    text = WHATIF_SH.read_text(encoding="utf-8")
    always = set(re.findall(r'^\s+([a-z][A-Za-z0-9]*)="\$[A-Z_]+"\s*\\$', text, re.M))
    loop = re.search(r"^for pair in \\\n(?P<body>.*?)^do$", text, re.M | re.S)
    assert loop, "whatif.sh no longer collects the optional parameters in a `for pair in` loop"
    optional = dict(re.findall(r"^\s+([a-z][A-Za-z0-9]*)=([A-Z][A-Z0-9_]*)\s*\\?$", loop.group("body"), re.M))
    assert not always & set(optional)
    assert sorted(set(mapped) - always - set(optional) - set(WHATIF_LEFT_OUT)) == [], "not previewed by whatif.sh"
    assert sorted((always | set(optional)) - set(mapped)) == [], "passed by whatif.sh, unknown to main.parameters.json"
    for name, variable in optional.items():
        match = mapped[name]
        assert match is not None
        # The same azd variable, and only parameters azd defaults when unset
        # are skipped when empty.
        assert match.group(1) == variable, name
        assert match.group(2) is not None, f"{name} has no default in main.parameters.json, so it is never optional"
    for name in WHATIF_LEFT_OUT:
        assert name in text, f"whatif.sh should say why it leaves out {name}"


def test_every_bound_queue_is_provisioned(bindings: Bindings) -> None:
    # data.bicep declares each job queue with its -poison twin; admin health
    # peeks both, and a binding to an undeclared queue would only exist once
    # the host first wrote to it.
    match = re.search(r"^var jobQueueNames = \[(?P<body>.*?)^\]", DATA_BICEP.read_text(encoding="utf-8"), re.M | re.S)
    assert match, "data.bicep has no `var jobQueueNames = [...]` list"
    declared = set(re.findall(r"'([^']+)'", match.group("body")))
    assert sorted(bindings.queues - declared) == []
