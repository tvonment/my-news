"""Index coverage: every query the app sends to container ``news`` is one its
opt-in index policy serves (infra/modules/cosmos-index-policy.json; the rules
are docs/data-model.md section 6.2).

``news`` indexes twelve paths and excludes everything else (``/*``), so a write
pays for those paths only. The price: Cosmos REJECTS a query that filters or
sorts on any other path, because scans are never enabled, and an ORDER BY over
two or more properties needs a composite index that matches it exactly (or
with every direction inverted). Neither shows up at deploy time, and the fake
container in the other tests only sees the queries those tests happen to run.
So this module reads every SQL string in the source and checks it against the
policy file itself.

What is checked, and what is exempt:

* Every string literal that starts with ``SELECT`` in the app's modules
  (backend/ minus tests, so api, core, jobs, pipeline, fetch and, from Phase 3,
  selection) and in the local tools (backend/scripts, scripts/). SQL must be a
  literal: one built with an f-string, ``+`` or ``.format`` fails here, because
  values go in as ``@parameters``.
* Exempt: conditional-patch predicates (``FROM c WHERE ...``), evaluated
  against one document without an index, never collected because they do not
  start with SELECT; constants named ``LEGACY_SQL_*``, which run against
  ``items`` (index-everything) and are held to exactly that use below; v1's
  retired stages, which no registered function reaches (held below too); and
  the local tools that never query ``news`` (NOT_NEWS_TOOLS, with reasons).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Iterator, NamedTuple

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
POLICY_PATH = REPO / "infra" / "modules" / "cosmos-index-policy.json"
DATA_BICEP = REPO / "infra" / "modules" / "data.bicep"
DATA_MODEL_MD = REPO / "docs" / "data-model.md"

LEGACY_PREFIX = "LEGACY_SQL_"
# id and _ts are always indexed, whatever the policy says.
ALWAYS_INDEXED = frozenset({"id", "_ts"})

# v1's model stages and their helpers, retired in Phase 0 and deleted or
# rewritten in Phase 2. Their queries (gate, digest, profile) were written for
# `items`, where every path is indexed, and their writes go to the default
# container, which is now `news`. Nothing registered imports them
# (test_retired_v1_stages_are_unreachable), so none of it can run; that is
# also why their settings are not deployed (test_settings_manifest's
# NOT_DEPLOYED). A module Phase 2 brings back leaves this list in the same
# change that deploys its settings again.
RETIRED_V1_MODULES = frozenset(
    {
        "pipeline.gate",
        "pipeline.digest",
        "pipeline.profile",
        "pipeline.score",
        "pipeline.summarize",
        "pipeline.cluster",
        "core.budget",
        "core.foundry",
    }
)

# Local tools whose SQL never reaches `news`, by path from the repo root.
NOT_NEWS_TOOLS = {
    "backend/scripts/verify_vector_search.py": (
        "superseded M0 probe: it stops before any query on a container without a vector policy, and `news` has none"
    ),
}

# Where enable_cross_partition_query may appear: core.cosmos sets it only for the
# legacy container (TestQueries in test_cosmos_helpers holds that), and the
# rescue export reads a whole container on purpose.
CROSS_PARTITION_ALLOWED = frozenset({"backend/core/cosmos.py", "backend/scripts/export_all.py"})


# ---------------------------------------------------------------------------
# Finding the SQL
# ---------------------------------------------------------------------------
def _scanned_files() -> list[Path]:
    files = []
    for path in sorted([*BACKEND.rglob("*.py"), *(REPO / "scripts").glob("*.py")]):
        parts = path.relative_to(REPO).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if parts[:2] == ("backend", "tests"):
            continue
        files.append(path)
    return files


def _module_name(path: Path) -> str | None:
    """The dotted import name of a backend module (backend/ is the import root)."""
    try:
        rel = path.relative_to(BACKEND)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _repo_path(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


_SELECT = re.compile(r"\s*SELECT\b", re.I)
_FROM_C = re.compile(r"\bFROM\s+c\b", re.I)


class SqlString(NamedTuple):
    sql: str
    file: str  # from the repo root
    line: int
    constant: str | None  # the name it is assigned to, if any

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}"


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def sql_strings(path: Path) -> tuple[list[SqlString], list[str]]:
    """(the SELECT literals in one file, the SQL that is built instead of written)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = _parents(tree)
    file = _repo_path(path)
    found: list[SqlString] = []
    built: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if isinstance(head, ast.Constant) and isinstance(head.value, str) and _SELECT.match(head.value):
                built.append(f"{file}:{node.lineno}: an f-string query; pass values as @parameters")
            continue
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if not (_SELECT.match(node.value) and _FROM_C.search(node.value)):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.JoinedStr):
            continue  # reported above
        if isinstance(parent, ast.BinOp) or (isinstance(parent, ast.Attribute) and parent.attr == "format"):
            built.append(f"{file}:{node.lineno}: a query assembled at run time; write it whole, values as @parameters")
        constant = None
        if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
            constant = parent.targets[0].id
        elif isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
            constant = parent.target.id
        found.append(SqlString(node.value, file, node.lineno, constant))
    return found, built


def _exempt_file(path: Path) -> bool:
    return _module_name(path) in RETIRED_V1_MODULES or _repo_path(path) in NOT_NEWS_TOOLS


@pytest.fixture(scope="module")
def all_sql() -> list[SqlString]:
    return [sql for path in _scanned_files() for sql in sql_strings(path)[0]]


@pytest.fixture(scope="module")
def news_sql(all_sql: list[SqlString]) -> list[SqlString]:
    exempt_files = {_repo_path(path) for path in _scanned_files() if _exempt_file(path)}
    queries = [
        sql for sql in all_sql if sql.file not in exempt_files and not (sql.constant or "").startswith(LEGACY_PREFIX)
    ]
    # The scan must see Phase 1's queries, or it is checking nothing.
    names = {sql.constant for sql in queries}
    assert {"SQL_INVITES_LIST", "SQL_USERS_LIST", "SQL_DELETIONS_BY_STATUS", "SQL_STUCK_DELETING_USERS"} <= names
    return queries


# ---------------------------------------------------------------------------
# Reading one query
# ---------------------------------------------------------------------------
_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_PATH_TAIL = r"((?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])+)"
_PATH = re.compile(r"(?<![\w.@])c" + _PATH_TAIL)
_ARRAY_ARG = re.compile(r"\bARRAY_CONTAINS\s*\(\s*c" + _PATH_TAIL + r"\s*,", re.I)
_EQUALITY = re.compile(r"(?<![\w.@])c\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:@\w+|''|-?\d|true\b|false\b)", re.I)
_ORDER_ITEM = re.compile(r"\s*c" + _PATH_TAIL + r"(?:\s+(ASC|DESC))?\s*", re.I)

Path_ = tuple[str, ...]  # ("type",), ("source_ids", "[]")


def _parts(tail: str) -> Path_:
    """``.a.b[0]`` -> ("a", "b", "[]"): array positions index as ``[]``."""
    return tuple(name or "[]" for name, _ in re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|(\[\d+\])", tail))


class Clauses(NamedTuple):
    filters: list[Path_]  # every path the WHERE reads, arrays as (..., "[]")
    equalities: set[str]  # top-level properties the WHERE compares with = to a value
    order_by: list[tuple[Path_, str]]  # (path, "ascending" | "descending")
    problems: list[str]


def clauses(sql: str) -> Clauses:
    text = _LITERAL.sub("''", sql)  # a string value can say anything, keywords included
    where = re.search(r"\bWHERE\b(?P<w>.*?)(?=\bORDER\s+BY\b|\bOFFSET\b|\bGROUP\s+BY\b|$)", text, re.I | re.S)
    order = re.search(r"\bORDER\s+BY\b(?P<o>.*?)(?=\bOFFSET\b|$)", text, re.I | re.S)
    filters: list[Path_] = []
    equalities: set[str] = set()
    problems: list[str] = []
    if where:
        body = where.group("w")
        arrays = {match.group(1) for match in _ARRAY_ARG.finditer(body)}
        for match in _PATH.finditer(body):
            parts = _parts(match.group(1))
            filters.append((*parts, "[]") if match.group(1) in arrays else parts)
        equalities = set(_EQUALITY.findall(body))
    order_by: list[tuple[Path_, str]] = []
    if order:
        for item in order.group("o").split(","):
            sort = _ORDER_ITEM.fullmatch(item)
            if sort is None:
                problems.append(f"ORDER BY {item.strip()!r} is not a property path")
                continue
            direction = "descending" if (sort.group(2) or "").upper() == "DESC" else "ascending"
            order_by.append((_parts(sort.group(1)), direction))
    return Clauses(filters, equalities, order_by, problems)


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def policy() -> dict[str, Any]:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def _rule_match(rule: str, parts: Path_) -> tuple[int, int] | None:
    """How precisely an index path rule matches a property path, or None. Cosmos
    lets the more precise of an included and an excluded rule win; a leaf rule
    (``/a/?``) is more precise than a wildcard (``/a/*``) over the same prefix."""
    segments = tuple(rule.strip("/").split("/"))
    prefix, last = segments[:-1], segments[-1]
    if last == "?":
        return (len(prefix), 1) if parts == prefix else None
    if last == "*":
        return (len(prefix), 0) if parts[: len(prefix)] == prefix else None
    raise AssertionError(f"index path {rule!r} ends in neither /? nor /*")


def covered(parts: Path_, policy: dict[str, Any]) -> bool:
    """Whether a filter or single-property sort on this path is served."""
    if len(parts) == 1 and parts[0] in ALWAYS_INDEXED:
        return True
    best: tuple[tuple[int, int], bool] | None = None
    for included, entries in ((True, policy.get("includedPaths", [])), (False, policy.get("excludedPaths", []))):
        for entry in entries:
            rank = _rule_match(entry["path"], parts)
            # On a tie the exclusion wins: being wrong that way fails here,
            # never in production.
            if rank is not None and (best is None or rank > best[0] or (rank == best[0] and not included)):
                best = (rank, included)
    return best is not None and best[1]


def composite_for(order_by: list[tuple[Path_, str]], policy: dict[str, Any]) -> bool:
    """Whether an ORDER BY over two or more paths matches a composite index
    exactly, in the same directions or with every direction inverted."""
    wanted = [("/" + "/".join(parts), direction) for parts, direction in order_by]
    flip = {"ascending": "descending", "descending": "ascending"}
    for composite in policy.get("compositeIndexes", []):
        same = [(entry["path"], entry.get("order", "ascending")) for entry in composite]
        if wanted in (same, [(path, flip[direction]) for path, direction in same]):
            return True
    return False


def check(sql: str, policy: dict[str, Any]) -> list[str]:
    """Every rule of docs/data-model.md section 6.2 this query breaks."""
    parsed = clauses(sql)
    problems = list(parsed.problems)
    for parts in parsed.filters:
        if not covered(parts, policy):
            problems.append(f"the filter on /{'/'.join(parts)} is not indexed")
    if len(parsed.order_by) == 1:
        parts = parsed.order_by[0][0]
        if not covered(parts, policy):
            problems.append(f"ORDER BY /{'/'.join(parts)} has no range index")
    elif len(parsed.order_by) >= 2 and not composite_for(parsed.order_by, policy):
        problems.append("the ORDER BY over several paths matches no composite index")
    if parsed.order_by:
        # Rule 3: a filtered list puts its filters first in the ORDER BY
        # (c.type ASC, ..., c.created_at DESC), which is what makes Cosmos use
        # the composite for the filter as well as the sort.
        leading = {"/".join(parts) for parts, _ in parsed.order_by[:-1]}
        for name in sorted(parsed.equalities - leading):
            problems.append(f"filtered on c.{name} but ordered without it ahead of the sort key")
    return problems


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
def test_every_news_query_is_served_by_the_index_policy(news_sql: list[SqlString], policy: dict[str, Any]) -> None:
    problems = [f"{sql.where}: {problem}\n    {sql.sql}" for sql in news_sql for problem in check(sql.sql, policy)]
    assert problems == [], "\n".join(problems)


def test_queries_are_literals_with_parameters() -> None:
    built = [problem for path in _scanned_files() if not _exempt_file(path) for problem in sql_strings(path)[1]]
    assert built == []


def _query_calls(tree: ast.AST) -> Iterator[ast.Call]:
    """Every ``cosmos.query(...)`` call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "query" and isinstance(node.func.value, ast.Name) and node.func.value.id == "cosmos":
            yield node


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _named(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def test_legacy_queries_run_only_against_the_legacy_container() -> None:
    # The LEGACY_SQL_* exemption is only honest while every such query really
    # goes to `items`: a legacy constant used against `news` would fail there,
    # and a query sent to `items` without the prefix would hide among the v2
    # ones. So the prefix and container=...legacy_cosmos_container travel
    # together, both ways, and no legacy constant is left unused.
    defined: set[str] = set()
    used: set[str] = set()
    problems: list[str] = []
    for path in _scanned_files():
        if _exempt_file(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        file = _repo_path(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                defined |= {t.id for t in node.targets if isinstance(t, ast.Name) and t.id.startswith(LEGACY_PREFIX)}
        first_args: set[ast.AST] = set()
        for call in _query_calls(tree):
            sql = call.args[0] if call.args else _keyword(call, "sql")
            container = _keyword(call, "container")
            legacy = container is not None and "legacy_cosmos_container" in ast.unparse(container)
            name = _named(sql)
            if sql is not None:
                first_args.add(sql)
            if legacy and not (name or "").startswith(LEGACY_PREFIX):
                problems.append(f"{file}:{call.lineno}: a legacy-container query needs a {LEGACY_PREFIX}* constant")
            if (name or "").startswith(LEGACY_PREFIX):
                used.add(name or "")
                if not legacy:
                    problems.append(f"{file}:{call.lineno}: {name} must pass container=...legacy_cosmos_container")
            if not legacy and len(call.args) < 3 and _keyword(call, "partition_key") is None:
                # Rule 4: no cross-partition query against `news` (cosmos.query
                # refuses one at run time; this finds the path no test runs).
                problems.append(f"{file}:{call.lineno}: a query against news names no partition_key")
        for node in ast.walk(tree):
            name = _named(node) if isinstance(node, (ast.Name, ast.Attribute)) else None
            loaded = isinstance(getattr(node, "ctx", None), ast.Load)
            if name and name.startswith(LEGACY_PREFIX) and loaded and node not in first_args:
                line = getattr(node, "lineno", 0)
                problems.append(f"{file}:{line}: {name} is used outside a legacy cosmos.query call")
    assert problems == []
    assert defined, "no LEGACY_SQL_* constant found: the scan is broken"
    assert sorted(defined - used) == [], "LEGACY_SQL_* constants nothing runs"


def test_no_scans_and_no_cross_partition_queries() -> None:
    # Rule 4. A scan would turn the opt-in policy's "rejected" into "slow and
    # expensive", which is exactly what it exists to prevent.
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8")
        assert "enable_scan_in_query" not in text, _repo_path(path)
        if "enable_cross_partition_query" in text:
            assert _repo_path(path) in CROSS_PARTITION_ALLOWED, _repo_path(path)


# ---------------------------------------------------------------------------
# The exemptions stay true
# ---------------------------------------------------------------------------
def _module_file(name: str) -> Path | None:
    parts = name.split(".")
    module = BACKEND.joinpath(*parts).with_suffix(".py")
    if module.is_file():
        return module
    package = BACKEND.joinpath(*parts, "__init__.py")
    return package if package.is_file() else None


def _imports(path: Path) -> set[str]:
    """Every backend module a file imports, at module level or inside a
    function (the handlers import the harvest lazily)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{_repo_path(path)}:{node.lineno}: relative import; backend/ is the import root"
            if node.module:
                names.add(node.module)
                names |= {f"{node.module}.{alias.name}" for alias in node.names}
    return {name for name in names if _module_file(name) is not None}


def reachable_from(root: str) -> set[str]:
    seen: set[str] = set()
    todo = [root]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        parts = name.split(".")
        todo += [".".join(parts[:i]) for i in range(1, len(parts))]  # importing a.b runs a/__init__
        module = _module_file(name)
        if module is not None:
            todo += sorted(_imports(module))
    return seen


def test_retired_v1_stages_are_unreachable() -> None:
    # They are exempt only because they cannot run: their queries (and their
    # writes, into the default container, which is now `news`) would otherwise
    # be live. Phase 2 deletes them.
    reachable = reachable_from("function_app")
    assert {"api.account", "jobs.queues", "pipeline.harvest", "core.accounts"} <= reachable  # the walk works
    assert sorted(RETIRED_V1_MODULES & reachable) == []
    for name in RETIRED_V1_MODULES:
        assert _module_file(name) is not None, f"{name} is gone: drop it from RETIRED_V1_MODULES"


def test_not_news_tools_exist() -> None:
    for path in NOT_NEWS_TOOLS:
        assert (REPO / path).is_file(), f"{path} is gone: drop it from NOT_NEWS_TOOLS"


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------
def test_query_field_constants_are_the_indexed_paths(policy: dict[str, Any]) -> None:
    # Rule 6: the F_* names in core/models.py are the included paths, exactly.
    from core import models

    fields = {value for name, value in vars(models).items() if name.startswith("F_")}
    assert set(models.QUERY_FIELDS) == fields
    included = {entry["path"] for entry in policy["includedPaths"]}
    assert {path.strip("/").split("/")[0] for path in included} == fields
    for field in fields:
        assert f"/{field}/?" in included or f"/{field}/[]/?" in included, field
    # Opt-in: everything else is excluded, and nothing is indexed twice.
    assert {"/*", "/_etag/?"} <= {entry["path"] for entry in policy["excludedPaths"]}
    assert len(included) == len(policy["includedPaths"])
    # Every composite sorts on indexed paths.
    for composite in policy["compositeIndexes"]:
        assert len(composite) >= 2
        for entry in composite:
            assert entry["path"].strip("/") in fields, entry


def test_the_policy_file_is_what_news_is_created_with_and_what_the_docs_quote(policy: dict[str, Any]) -> None:
    bicep = DATA_BICEP.read_text(encoding="utf-8")
    news = re.search(r"^resource newsContainer .*?^\}", bicep, re.M | re.S)
    assert news and "indexingPolicy: loadJsonContent('cosmos-index-policy.json')" in news.group(0)
    section = DATA_MODEL_MD.read_text(encoding="utf-8").split("### 6.1 The policy", 1)[1]
    quoted = re.search(r"```json\n(.*?)\n```", section, re.S)
    assert quoted and json.loads(quoted.group(1)) == policy, "docs/data-model.md section 6.1 must quote the policy"


# The Phase 2 and 3 queries as docs/data-model.md section 6.3 drafts them. Their
# paths are in the policy from creation, because adding one later re-indexes in
# the background, and queries on it return incomplete results until that ends.
DRAFT_QUERIES = {
    "D1 enrich": "SELECT TOP @limit * FROM c WHERE c.enrich_status = @s "
    "ORDER BY c.enrich_status ASC, c.first_seen_at DESC",
    "D2 stories": "SELECT * FROM c WHERE c.type = @type AND c.last_pub_at >= @since",
    "D3 summaries": "SELECT * FROM c WHERE c.type = @type AND c.status = @status",
    "D4 downgrade": "SELECT c.id FROM c WHERE c.type = @type AND ARRAY_CONTAINS(c.source_ids, @source)",
    "D5 runs": "SELECT TOP @limit * FROM c WHERE c.type = @type AND c.kind = @kind "
    "ORDER BY c.type ASC, c.kind ASC, c.created_at DESC",
    "D6 catalog": "SELECT * FROM c WHERE c.type = @type",
    "D7 review lists": "SELECT TOP @limit * FROM c WHERE c.type = @type AND c.status = @status "
    "ORDER BY c.type ASC, c.status ASC, c.created_at DESC",
    "D8 anonymise reports": "SELECT c.id FROM c WHERE c.type = @type AND c.uid = @uid",
    "D9 audit": "SELECT * FROM c WHERE c.type = @type ORDER BY c.type ASC, c.created_at DESC OFFSET @o LIMIT @n",
    "D10 prebuild": "SELECT c.id, c.locale, c.languages FROM c "
    "WHERE c.type = @type AND c.status = @status AND c.last_active_at >= @since",
    "D12 history": "SELECT * FROM c WHERE c.type = @type AND c.signal = @signal AND c.created_at >= @since "
    "ORDER BY c.type ASC, c.signal ASC, c.created_at DESC",
    "D13 card state": "SELECT c.story_id, c.signal, c.created_at, c.undone_at FROM c "
    "WHERE c.type = @type AND c.created_at >= @since",
    "D14 duplicates": "SELECT VALUE c.id FROM c WHERE ARRAY_CONTAINS(@ids, c.id)",
}


@pytest.mark.parametrize("sql", DRAFT_QUERIES.values(), ids=DRAFT_QUERIES.keys())
def test_the_drafted_later_phase_queries_are_served_already(sql: str, policy: dict[str, Any]) -> None:
    assert check(sql, policy) == []


# ---------------------------------------------------------------------------
# The checker, on queries it must refuse
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "problem"),
    [
        ("SELECT * FROM c WHERE c.label = @label", "the filter on /label is not indexed"),
        ("SELECT * FROM c WHERE c.type = @type AND c.redemptions[0].uid = @uid", "/redemptions/[]/uid is not indexed"),
        ("SELECT * FROM c WHERE ARRAY_CONTAINS(c.languages, @lang)", "/languages/[] is not indexed"),
        ("SELECT * FROM c ORDER BY c.expires_at DESC", "ORDER BY /expires_at has no range index"),
        ("SELECT * FROM c WHERE c.type = @t ORDER BY c.type ASC, c.created_at ASC", "matches no composite"),
        ("SELECT * FROM c WHERE c.type = @t ORDER BY c.created_at DESC", "filtered on c.type"),
        ("SELECT * FROM c WHERE c.type = @t ORDER BY LOWER(c.label)", "is not a property path"),
        # A string value cannot smuggle a clause past the reader.
        ("SELECT * FROM c WHERE c.type = 'x ORDER BY c.type' AND c.label = @l", "/label is not indexed"),
    ],
)
def test_the_checker_refuses(sql: str, problem: str, policy: dict[str, Any]) -> None:
    assert any(problem in found for found in check(sql, policy)), check(sql, policy)


def test_the_checker_accepts_the_inverted_composite(policy: dict[str, Any]) -> None:
    # Cosmos serves an ORDER BY with every direction inverted from the same index.
    assert check("SELECT * FROM c WHERE c.type = @t ORDER BY c.type DESC, c.created_at ASC", policy) == []
    assert covered(("id",), policy) and covered(("_ts",), policy)
    assert not covered(("_etag",), policy)
