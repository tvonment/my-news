"""A Cosmos SQL subset, enough to run the backend's own query strings.

The fake container (tests/fakes/cosmos.py) evaluates the production SQL
constants and patch predicates with this, rather than mapping each known string
to a Python stand-in, so a test exercises the query text that ships. Supported:

    SELECT [TOP n|@p] (* | VALUE expr | expr [AS name], ...) FROM c
        [WHERE expr] [ORDER BY c.path [ASC|DESC], ...] [OFFSET n LIMIT m]
    FROM c WHERE expr                      (a patch filter_predicate)

with AND/OR/NOT, = != <> < <= > >=, IN (...), parameters, string/number/bool/
null literals, object and array literals, paths with .name and [index], and the
functions COUNT, IS_DEFINED, IS_NULL, ARRAY_CONTAINS (with the partial-match
flag), ARRAY_LENGTH, STARTSWITH and LOWER.

Semantics follow Cosmos where the backend depends on them: a missing property is
*undefined*, not null; comparing values of different types (or anything with
undefined) is undefined; AND/OR use three-valued logic; WHERE keeps a document
only when the condition is exactly true. ``NOT IS_DEFINED(c.x) OR c.x <= 9`` on a
document without ``x`` is therefore true, and ``c.x <= 9`` alone is not.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class SqlError(ValueError):
    """The text is outside the supported subset, or a parameter is missing."""


class _Undefined:
    _instance: "_Undefined | None" = None

    def __new__(cls) -> "_Undefined":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "undefined"


UNDEFINED = _Undefined()

_KEYWORDS = {
    "SELECT", "TOP", "VALUE", "FROM", "WHERE", "ORDER", "BY", "ASC", "DESC", "AND", "OR",
    "NOT", "IN", "AS", "OFFSET", "LIMIT", "TRUE", "FALSE", "NULL", "UNDEFINED",
}
_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<param>@[A-Za-z_][A-Za-z0-9_]*)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|!=|<>|=|<|>)
  | (?P<punct>[(),.\[\]{}:*])
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    value: str


def _tokenize(text: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            raise SqlError(f"unexpected character {text[pos]!r} at {pos} in {text!r}")
        pos = match.end()
        kind = match.lastgroup or ""
        value = match.group()
        if kind == "ws":
            continue
        if kind == "ident" and value.upper() in _KEYWORDS:
            tokens.append(_Tok("kw", value.upper()))
        else:
            tokens.append(_Tok(kind, value))
    return tokens


# --- AST ----------------------------------------------------------------------
@dataclass(frozen=True)
class Path:
    """``c`` followed by property names and array indexes."""

    parts: tuple[str | int, ...]

    @property
    def root(self) -> str | None:
        return self.parts[0] if self.parts and isinstance(self.parts[0], str) else None


@dataclass(frozen=True)
class Literal:
    value: Any


@dataclass(frozen=True)
class Param:
    name: str


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class Unary:
    op: str
    operand: Any


@dataclass(frozen=True)
class Binary:
    op: str
    left: Any
    right: Any


@dataclass(frozen=True)
class InList:
    needle: Any
    options: tuple[Any, ...]


@dataclass(frozen=True)
class ObjectLit:
    items: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class ArrayLit:
    items: tuple[Any, ...]


@dataclass
class Query:
    top: Any = None
    star: bool = False
    value: Any = None
    projections: list[tuple[str, Any]] = field(default_factory=list)
    where: Any = None
    order_by: list[tuple[Path, str]] = field(default_factory=list)
    offset: Any = None
    limit: Any = None


# --- parser -------------------------------------------------------------------
class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.pos = 0

    def peek(self, offset: int = 0) -> _Tok | None:
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def take(self) -> _Tok:
        tok = self.peek()
        if tok is None:
            raise SqlError(f"unexpected end of {self.text!r}")
        self.pos += 1
        return tok

    def accept(self, kind: str, value: str | None = None) -> _Tok | None:
        tok = self.peek()
        if tok is not None and tok.kind == kind and (value is None or tok.value == value):
            self.pos += 1
            return tok
        return None

    def expect(self, kind: str, value: str | None = None) -> _Tok:
        tok = self.accept(kind, value)
        if tok is None:
            raise SqlError(f"expected {value or kind} at token {self.pos} in {self.text!r}, got {self.peek()}")
        return tok

    def done(self) -> None:
        if self.peek() is not None:
            raise SqlError(f"unexpected {self.peek()} in {self.text!r}")

    # query := SELECT ... FROM c [WHERE] [ORDER BY] [OFFSET LIMIT]
    def query(self) -> Query:
        q = Query()
        self.expect("kw", "SELECT")
        if self.accept("kw", "TOP"):
            q.top = self.scalar_operand()
        if self.accept("punct", "*"):
            q.star = True
        elif self.accept("kw", "VALUE"):
            q.value = self.expr()
        else:
            while True:
                expr = self.expr()
                alias = None
                if self.accept("kw", "AS"):
                    alias = self.expect("ident").value
                if alias is None:
                    if isinstance(expr, Path) and expr.parts and isinstance(expr.parts[-1], str):
                        alias = expr.parts[-1]
                    else:
                        alias = f"${len(q.projections) + 1}"
                q.projections.append((alias, expr))
                if not self.accept("punct", ","):
                    break
        self.from_c()
        if self.accept("kw", "WHERE"):
            q.where = self.expr()
        if self.accept("kw", "ORDER"):
            self.expect("kw", "BY")
            while True:
                path = self.primary()
                if not isinstance(path, Path):
                    raise SqlError("ORDER BY takes property paths only")
                direction = "ASC"
                tok = self.accept("kw", "ASC") or self.accept("kw", "DESC")
                if tok is not None:
                    direction = tok.value
                q.order_by.append((path, direction))
                if not self.accept("punct", ","):
                    break
        if self.accept("kw", "OFFSET"):
            q.offset = self.scalar_operand()
            self.expect("kw", "LIMIT")
            q.limit = self.scalar_operand()
        self.done()
        return q

    def predicate(self) -> Any:
        self.from_c()
        self.expect("kw", "WHERE")
        expr = self.expr()
        self.done()
        return expr

    def from_c(self) -> None:
        self.expect("kw", "FROM")
        root = self.expect("ident")
        if root.value != "c":
            raise SqlError("only FROM c is supported")

    def scalar_operand(self) -> Any:
        tok = self.take()
        if tok.kind == "number":
            return Literal(int(tok.value))
        if tok.kind == "param":
            return Param(tok.value)
        raise SqlError(f"expected a number or a parameter, got {tok}")

    def expr(self) -> Any:
        left = self.and_expr()
        while self.accept("kw", "OR"):
            left = Binary("OR", left, self.and_expr())
        return left

    def and_expr(self) -> Any:
        left = self.not_expr()
        while self.accept("kw", "AND"):
            left = Binary("AND", left, self.not_expr())
        return left

    def not_expr(self) -> Any:
        if self.accept("kw", "NOT"):
            return Unary("NOT", self.not_expr())
        return self.comparison()

    def comparison(self) -> Any:
        left = self.primary()
        tok = self.accept("op")
        if tok is not None:
            op = "!=" if tok.value == "<>" else tok.value
            return Binary(op, left, self.primary())
        if self.accept("kw", "IN"):
            self.expect("punct", "(")
            options = [self.expr()]
            while self.accept("punct", ","):
                options.append(self.expr())
            self.expect("punct", ")")
            return InList(left, tuple(options))
        return left

    def primary(self) -> Any:
        tok = self.take()
        if tok.kind == "number":
            return Literal(float(tok.value) if "." in tok.value else int(tok.value))
        if tok.kind == "string":
            return Literal(_unquote(tok.value))
        if tok.kind == "param":
            return Param(tok.value)
        if tok.kind == "kw" and tok.value in {"TRUE", "FALSE", "NULL", "UNDEFINED"}:
            return Literal({"TRUE": True, "FALSE": False, "NULL": None, "UNDEFINED": UNDEFINED}[tok.value])
        if tok.kind == "punct" and tok.value == "(":
            inner = self.expr()
            self.expect("punct", ")")
            return inner
        if tok.kind == "punct" and tok.value == "{":
            items: list[tuple[str, Any]] = []
            if not self.accept("punct", "}"):
                while True:
                    key_tok = self.take()
                    if key_tok.kind == "string":
                        key = _unquote(key_tok.value)
                    elif key_tok.kind == "ident":
                        key = key_tok.value
                    else:
                        raise SqlError(f"bad object key {key_tok}")
                    self.expect("punct", ":")
                    items.append((key, self.expr()))
                    if not self.accept("punct", ","):
                        break
                self.expect("punct", "}")
            return ObjectLit(tuple(items))
        if tok.kind == "punct" and tok.value == "[":
            elements: list[Any] = []
            if not self.accept("punct", "]"):
                while True:
                    elements.append(self.expr())
                    if not self.accept("punct", ","):
                        break
                self.expect("punct", "]")
            return ArrayLit(tuple(elements))
        if tok.kind == "ident":
            if self.accept("punct", "("):
                args: list[Any] = []
                if not self.accept("punct", ")"):
                    while True:
                        args.append(self.expr())
                        if not self.accept("punct", ","):
                            break
                    self.expect("punct", ")")
                return Call(tok.value.upper(), tuple(args))
            if tok.value != "c":
                raise SqlError(f"unknown identifier {tok.value!r}")
            parts: list[str | int] = []
            while True:
                if self.accept("punct", "."):
                    parts.append(self.expect("ident").value)
                elif self.accept("punct", "["):
                    index_tok = self.take()
                    if index_tok.kind == "number":
                        parts.append(int(index_tok.value))
                    elif index_tok.kind == "string":
                        parts.append(_unquote(index_tok.value))
                    else:
                        raise SqlError(f"bad index {index_tok}")
                    self.expect("punct", "]")
                else:
                    break
            return Path(tuple(parts))
        raise SqlError(f"unexpected {tok} in {self.text!r}")


def _unquote(raw: str) -> str:
    body = raw[1:-1]
    return re.sub(r"\\(.)", lambda m: m.group(1), body)


@functools.lru_cache(maxsize=256)
def parse_query(text: str) -> Query:
    return _Parser(text).query()


@functools.lru_cache(maxsize=256)
def parse_predicate(text: str) -> Any:
    return _Parser(text).predicate()


# --- evaluation ---------------------------------------------------------------
def _type_rank(value: Any) -> int:
    """Cosmos's cross-type sort order: undefined < null < bool < number <
    string < array < object."""
    if value is UNDEFINED:
        return 0
    if value is None:
        return 1
    if isinstance(value, bool):
        return 2
    if isinstance(value, (int, float)):
        return 3
    if isinstance(value, str):
        return 4
    if isinstance(value, list):
        return 5
    return 6


def _comparable(left: Any, right: Any) -> bool:
    return (
        left is not UNDEFINED
        and right is not UNDEFINED
        and _type_rank(left) == _type_rank(right)
    )


def _resolve(doc: Any, path: Path) -> Any:
    value = doc
    for part in path.parts:
        if isinstance(part, int):
            if not isinstance(value, list) or not 0 <= part < len(value):
                return UNDEFINED
            value = value[part]
        else:
            if not isinstance(value, dict) or part not in value:
                return UNDEFINED
            value = value[part]
    return value


def _contains(container: Any, needle: Any, partial: bool) -> bool:
    if partial and isinstance(needle, dict):
        return isinstance(container, dict) and all(
            key in container and container[key] == val for key, val in needle.items()
        )
    return container == needle


class Evaluator:
    def __init__(self, params: Mapping[str, Any]):
        self.params = params

    def eval(self, node: Any, doc: Mapping[str, Any]) -> Any:
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, Param):
            if node.name not in self.params:
                raise SqlError(f"parameter {node.name} was not supplied")
            return self.params[node.name]
        if isinstance(node, Path):
            return _resolve(doc, node)
        if isinstance(node, ObjectLit):
            out = {}
            for key, value_node in node.items:
                value = self.eval(value_node, doc)
                if value is not UNDEFINED:
                    out[key] = value
            return out
        if isinstance(node, ArrayLit):
            return [v for v in (self.eval(item, doc) for item in node.items) if v is not UNDEFINED]
        if isinstance(node, Unary):
            operand = self.eval(node.operand, doc)
            return (not operand) if isinstance(operand, bool) else UNDEFINED
        if isinstance(node, Binary):
            return self._binary(node, doc)
        if isinstance(node, InList):
            needle = self.eval(node.needle, doc)
            if needle is UNDEFINED:
                return UNDEFINED
            return any(_comparable(needle, opt) and needle == opt for opt in (self.eval(o, doc) for o in node.options))
        if isinstance(node, Call):
            return self._call(node, doc)
        raise SqlError(f"cannot evaluate {node!r}")

    def _binary(self, node: Binary, doc: Mapping[str, Any]) -> Any:
        if node.op in {"AND", "OR"}:
            left = self.eval(node.left, doc)
            right = self.eval(node.right, doc)
            values = [v if isinstance(v, bool) else UNDEFINED for v in (left, right)]
            if node.op == "AND":
                if False in values:
                    return False
                return UNDEFINED if UNDEFINED in values else True
            if True in values:
                return True
            return UNDEFINED if UNDEFINED in values else False
        left = self.eval(node.left, doc)
        right = self.eval(node.right, doc)
        if not _comparable(left, right):
            return UNDEFINED
        if node.op == "=":
            return left == right
        if node.op == "!=":
            return left != right
        if isinstance(left, (list, dict)) or isinstance(left, bool) or left is None:
            return UNDEFINED
        return {
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
        }[node.op]

    def _call(self, node: Call, doc: Mapping[str, Any]) -> Any:
        name = node.name
        if name == "IS_DEFINED":
            return self.eval(node.args[0], doc) is not UNDEFINED
        if name == "IS_NULL":
            return self.eval(node.args[0], doc) is None
        if name == "ARRAY_CONTAINS":
            array = self.eval(node.args[0], doc)
            needle = self.eval(node.args[1], doc)
            partial = bool(self.eval(node.args[2], doc)) if len(node.args) > 2 else False
            if not isinstance(array, list) or needle is UNDEFINED:
                return UNDEFINED
            return any(_contains(item, needle, partial) for item in array)
        if name == "ARRAY_LENGTH":
            array = self.eval(node.args[0], doc)
            return len(array) if isinstance(array, list) else UNDEFINED
        if name == "STARTSWITH":
            text = self.eval(node.args[0], doc)
            prefix = self.eval(node.args[1], doc)
            if not isinstance(text, str) or not isinstance(prefix, str):
                return UNDEFINED
            return text.startswith(prefix)
        if name == "LOWER":
            text = self.eval(node.args[0], doc)
            return text.lower() if isinstance(text, str) else UNDEFINED
        raise SqlError(f"function {name} is not supported by the fake")


def matches(predicate: Any, doc: Mapping[str, Any], params: Mapping[str, Any] | None = None) -> bool:
    return Evaluator(params or {}).eval(predicate, doc) is True


def _order_key(order_by: list[tuple[Path, str]]) -> Callable[[Any, Any], int]:
    def compare(a: Mapping[str, Any], b: Mapping[str, Any]) -> int:
        for path, direction in order_by:
            left, right = _resolve(a, path), _resolve(b, path)
            rank_l, rank_r = _type_rank(left), _type_rank(right)
            if rank_l != rank_r:
                result = -1 if rank_l < rank_r else 1
            elif left is UNDEFINED or left == right:
                result = 0
            else:
                result = -1 if left < right else 1
            if result:
                return -result if direction == "DESC" else result
        return 0

    return compare


def execute(query: Query, docs: list[dict[str, Any]], params: Mapping[str, Any]) -> list[Any]:
    """Run a parsed query over ``docs`` (already scoped to a partition)."""
    evaluator = Evaluator(params)
    rows = [doc for doc in docs if query.where is None or evaluator.eval(query.where, doc) is True]
    if query.order_by:
        rows.sort(key=functools.cmp_to_key(_order_key(query.order_by)))
    if isinstance(query.value, Call) and query.value.name == "COUNT":
        return [len(rows)]
    if query.offset is not None:
        offset = int(evaluator.eval(query.offset, {}))
        limit = int(evaluator.eval(query.limit, {}))
        rows = rows[offset : offset + limit]
    if query.top is not None:
        rows = rows[: int(evaluator.eval(query.top, {}))]
    if query.star:
        return rows
    if query.value is not None:
        values = [evaluator.eval(query.value, row) for row in rows]
        return [v for v in values if v is not UNDEFINED]
    out = []
    for row in rows:
        projected = {}
        for alias, expr in query.projections:
            value = evaluator.eval(expr, row)
            if value is not UNDEFINED:
                projected[alias] = value
        out.append(projected)
    return out


def referenced_paths(node: Any) -> list[Path]:
    """Every property path an expression reads (for the index-policy check)."""
    found: list[Path] = []

    def walk(item: Any) -> None:
        if isinstance(item, Path):
            found.append(item)
        elif isinstance(item, (Unary,)):
            walk(item.operand)
        elif isinstance(item, Binary):
            walk(item.left)
            walk(item.right)
        elif isinstance(item, InList):
            walk(item.needle)
            for option in item.options:
                walk(option)
        elif isinstance(item, Call):
            for arg in item.args:
                walk(arg)
        elif isinstance(item, ObjectLit):
            for _, value in item.items:
                walk(value)
        elif isinstance(item, ArrayLit):
            for value in item.items:
                walk(value)

    walk(node)
    return found
