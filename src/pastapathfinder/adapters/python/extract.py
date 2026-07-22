"""The AST walk and the call-resolution ladder (design.md §3.5, D16).

design.md §3.5 (`extract`, both halves), §4.1 (node-ID grammar), §4.2 (DDL, reserved
`attrs` keys), §4.3 (`Diag`), D1, D15, D16, D22, R3; requirements FR-12, FR-14 (AC-14.1,
AC-14.2), FR-21, FR-22, FR-37 (AC-37.1, AC-37.2, AC-37.3), FR-40 (AC-40.1, AC-40.2).

One module's typed AST in, one file's graph rows out: the `file` node, the module-body
node (`kind='module'`, D16), a node per function/method/class/lambda with its span, the
`contains` edges that hold them together, `imports` edges to the other analyzed files this
one imports, and — through the second half of this module — the `calls` edges the engine's
resolution supports, each attached to the scope D16 makes its `src`.

**Why the walker is hand-rolled** (`FINDINGS-mypy.md` §2 trap 1). The idiomatic way to
walk a mypy AST is to subclass `mypy.traverser.TraverserVisitor`, which is impossible: the
shipped wheel is mypyc-compiled, so that class is a compiled `@trait` and subclassing it
from interpreted Python raises `TypeError: interpreted classes cannot inherit from
compiled traits` — the `@mypyc_attr(allow_interpreted_subclasses=True)` decoration does
not lift the restriction. `_CHILDREN` below therefore replicates that class's
child-visiting map, method for method, and `_LEAVES` records the node types it bottoms
out on. Two disciplines make the copy safe rather than merely equivalent:

* **Structural children only.** A `.node`, `.info`, `.type` or `.fullname` is a *semantic*
  pointer into another module's tree; following one would attribute another file's code to
  this file. Nothing in `_CHILDREN` reads one, so the walk cannot leave the module it was
  given, and a test asserts exactly that by object identity.
* **Total coverage, checked.** A test asserts every concrete `mypy.nodes` / `mypy.patterns`
  node type appears in `_CHILDREN` or `_LEAVES`, so a mypy upgrade that adds a node type
  fails loudly here (D1a) instead of silently dropping whatever hangs beneath it.

Measured against the pinned Django benchmark, the walk reaches ≥ 99.9 % of the call sites
a stdlib-`ast` enumeration finds (`tests/unit/test_extract_benchmark.py`; the prototype's
reference is 37,207 / 37,218).

**Scopes: lexical for `contains`, executed for call sites.** They are two different
questions about the same stack. A definition is *contained* by its nearest enclosing
definition — file for top-level defs, class for methods, function for nested defs and
lambdas — while a call site's `src` is its nearest enclosing *executed* scope, which at
the top level is the module-body node rather than the file (D16, normative). Argument
defaults, decorator expressions, base-class expressions and class keywords are executed by
the *enclosing* scope, not by the thing they decorate, so the walk visits them before
descending, and the two answers agree on every case without a special rule.

**The ladder answers in fullnames; the index speaks node IDs.** `TargetIndex` is the
translation, and it is deliberately built from *rows* (an engine module list plus
`NodeRow`s) rather than from live trees: on an incremental run only the rechecked modules
have trees, so a resolver keyed on tree object identity could not name a target in a file
this run never re-read. Rows come equally from this run's extractions or from the index
(task 4.1). Two consequences worth stating, because both are visible in the output:

* **A fullname can name more than one node.** `@overload` groups, conditional definitions
  and property/setter pairs all put several definitions under one qualname, which §4.1
  disambiguates with `@line`. The engine's own definition line picks the intended one where
  it can; where it cannot, every member of the group gets an edge and all of them are
  flagged ambiguous, because FR-14 would rather over-approximate than drop the real target.
* **A bare fullname is a nested definition.** mypy names a function nested inside another
  function by its bare name (`inner`, not `pkg.mod.outer.inner`), so such a target has no
  module to look up. It is resolved against the *caller's own* file by name and definition
  line, and only for a statically bound `NameExpr`/`MemberExpr` — a name bound at a call
  site is in scope there, so the definition is in that file. Value-flow resolutions never
  take this path, which is what keeps a returned closure from another module out of it.

**Recall is a measured property, not a promise** (design.md R3, requirements C-11). mypy is
a soundness-oriented checker: on unannotated legacy code it assigns `Any` and offers no
target, so roughly a third of real call sites resolve to nothing. Those sites are not
dropped — each one becomes an `unresolved_call` diagnostic carrying its position and the
callee's source text, which is the audit trail the deferred dispatch layer (backlog B-22)
would consume. `tests/unit/test_extract_benchmark.py` records the rate on the pinned
Django benchmark.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from mypy.nodes import (
    REVEAL_TYPE,
    Argument,
    AssertStmt,
    AssertTypeExpr,
    AssignmentExpr,
    AssignmentStmt,
    AwaitExpr,
    Block,
    BreakStmt,
    BytesExpr,
    CallExpr,
    CastExpr,
    ClassDef,
    ComparisonExpr,
    ComplexExpr,
    ConditionalExpr,
    ContinueStmt,
    Decorator,
    DelStmt,
    DictExpr,
    DictionaryComprehension,
    EllipsisExpr,
    EnumCallExpr,
    ExpressionStmt,
    FakeExpression,
    FakeInfo,
    FloatExpr,
    ForStmt,
    FuncDef,
    GeneratorExpr,
    GlobalDecl,
    IfStmt,
    Import,
    ImportAll,
    ImportBase,
    ImportFrom,
    IndexExpr,
    IntExpr,
    LambdaExpr,
    ListComprehension,
    ListExpr,
    MatchStmt,
    MemberExpr,
    MypyFile,
    NamedTupleExpr,
    NameExpr,
    NewTypeExpr,
    Node,
    NonlocalDecl,
    OperatorAssignmentStmt,
    OpExpr,
    OverloadedFuncDef,
    ParamSpecExpr,
    PassStmt,
    PlaceholderNode,
    PromoteExpr,
    RaiseStmt,
    ReturnStmt,
    RevealExpr,
    SetComprehension,
    SetExpr,
    SliceExpr,
    StarExpr,
    StrExpr,
    SuperExpr,
    TemplateStrExpr,
    TempNode,
    TryStmt,
    TupleExpr,
    TypeAlias,
    TypeAliasExpr,
    TypeAliasStmt,
    TypeApplication,
    TypedDictExpr,
    TypeFormExpr,
    TypeInfo,
    TypeVarExpr,
    TypeVarTupleExpr,
    UnaryExpr,
    Var,
    WhileStmt,
    WithStmt,
    YieldExpr,
    YieldFromExpr,
)
from mypy.patterns import (
    AsPattern,
    ClassPattern,
    MappingPattern,
    OrPattern,
    SequencePattern,
    SingletonPattern,
    StarredPattern,
    ValuePattern,
)
from mypy.types import (
    CallableType,
    Instance,
    Overloaded,
    ProperType,
    TypeType,
    UnionType,
    get_proper_type,
)

from pastapathfinder.adapters.python import normalize
from pastapathfinder.adapters.python.mypy_driver import EngineSource
from pastapathfinder.schema import LANGUAGE_PYTHON, Diag, EdgeRow, NodeRow

# ---------------------------------------------------------------------------
# The child-visiting map (a copy of `mypy.traverser.TraverserVisitor`'s)
# ---------------------------------------------------------------------------

#: Node type → its structural children, in `TraverserVisitor`'s own visiting order. Values
#: may be nodes, lists, tuples or dicts; `_flatten()` reduces them to nodes and drops
#: everything else (the strings in a `TemplateStrExpr`, an absent `else_body`, …).
_CHILDREN: dict[type, Callable[[Any], Iterable[object]]] = {
    MypyFile: lambda o: o.defs,
    Block: lambda o: o.body,
    # visit_func: argument initializers, then the body.
    FuncDef: lambda o: (o.arguments, o.body),
    LambdaExpr: lambda o: (o.arguments, o.body),
    # `Argument` has no visit method of its own; `visit_func` reaches its initializer.
    Argument: lambda o: (o.initializer,),
    OverloadedFuncDef: lambda o: (o.items, o.impl),
    ClassDef: lambda o: (
        o.decorators,
        o.base_type_exprs,
        o.metaclass,
        o.keywords,
        o.defs,
        o.analyzed,
    ),
    Decorator: lambda o: (o.func, o.var, o.decorators),
    ExpressionStmt: lambda o: (o.expr,),
    AssignmentStmt: lambda o: (o.rvalue, o.lvalues),
    OperatorAssignmentStmt: lambda o: (o.rvalue, o.lvalue),
    WhileStmt: lambda o: (o.expr, o.body, o.else_body),
    ForStmt: lambda o: (o.index, o.expr, o.body, o.else_body),
    ReturnStmt: lambda o: (o.expr,),
    AssertStmt: lambda o: (o.expr, o.msg),
    DelStmt: lambda o: (o.expr,),
    IfStmt: lambda o: (o.expr, o.body, o.else_body),
    RaiseStmt: lambda o: (o.expr, o.from_expr),
    TryStmt: lambda o: (o.body, o.types, o.handlers, o.vars, o.else_body, o.finally_body),
    WithStmt: lambda o: (o.expr, o.target, o.body),
    MatchStmt: lambda o: (o.subject, o.patterns, o.guards, o.bodies),
    TypeAliasStmt: lambda o: (o.name, o.value),
    MemberExpr: lambda o: (o.expr,),
    YieldFromExpr: lambda o: (o.expr,),
    YieldExpr: lambda o: (o.expr,),
    CallExpr: lambda o: (o.callee, o.args, o.analyzed),
    OpExpr: lambda o: (o.left, o.right, o.analyzed),
    ComparisonExpr: lambda o: (o.operands,),
    SliceExpr: lambda o: (o.begin_index, o.end_index, o.stride),
    CastExpr: lambda o: (o.expr,),
    # visit_type_form_expr is a `pass` in the traverser: the expression is a type, not code.
    TypeFormExpr: lambda o: (),
    AssertTypeExpr: lambda o: (o.expr,),
    # A `reveal_locals()` has no inner expression; only `reveal_type()` carries one.
    RevealExpr: lambda o: (o.expr,) if o.kind == REVEAL_TYPE else (),
    AssignmentExpr: lambda o: (o.target, o.value),
    UnaryExpr: lambda o: (o.expr,),
    ListExpr: lambda o: (o.items,),
    TupleExpr: lambda o: (o.items,),
    SetExpr: lambda o: (o.items,),
    DictExpr: lambda o: (o.items,),
    TemplateStrExpr: lambda o: (o.items,),
    IndexExpr: lambda o: (o.base, o.index, o.analyzed),
    GeneratorExpr: lambda o: (o.sequences, o.indices, o.condlists, o.left_expr),
    DictionaryComprehension: lambda o: (o.sequences, o.indices, o.condlists, o.key, o.value),
    ListComprehension: lambda o: (o.generator,),
    SetComprehension: lambda o: (o.generator,),
    ConditionalExpr: lambda o: (o.cond, o.if_expr, o.else_expr),
    TypeApplication: lambda o: (o.expr,),
    StarExpr: lambda o: (o.expr,),
    AwaitExpr: lambda o: (o.expr,),
    SuperExpr: lambda o: (o.call,),
    AsPattern: lambda o: (o.pattern, o.name),
    OrPattern: lambda o: (o.patterns,),
    ValuePattern: lambda o: (o.expr,),
    SequencePattern: lambda o: (o.patterns,),
    StarredPattern: lambda o: (o.capture,),
    MappingPattern: lambda o: (o.keys, o.values, o.rest),
    ClassPattern: lambda o: (o.class_ref, o.positionals, o.keyword_values),
    Import: lambda o: (o.assignments,),
    ImportFrom: lambda o: (o.assignments,),
}

#: Node types the traverser bottoms out on. The last six are semantic nodes — a symbol
#: table's contents rather than a statement's children — which a structural walk never
#: reaches at all; they are listed so the coverage test can be total.
_LEAVES: frozenset[type] = frozenset(
    {
        NameExpr,
        StrExpr,
        IntExpr,
        FloatExpr,
        BytesExpr,
        ComplexExpr,
        EllipsisExpr,
        ContinueStmt,
        PassStmt,
        BreakStmt,
        TempNode,
        NonlocalDecl,
        GlobalDecl,
        ImportAll,
        TypeVarExpr,
        ParamSpecExpr,
        TypeVarTupleExpr,
        TypeAliasExpr,
        NamedTupleExpr,
        TypedDictExpr,
        NewTypeExpr,
        PromoteExpr,
        EnumCallExpr,
        SingletonPattern,
        Var,
        TypeAlias,
        TypeInfo,
        PlaceholderNode,
        FakeInfo,
        FakeExpression,
    }
)


def _flatten(value: object) -> Iterator[Node]:
    """The nodes inside a `_CHILDREN` entry's value, in order."""
    if value is None:
        return
    if isinstance(value, Node):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _flatten(item)


def children(node: Node) -> list[Node]:
    """The structural children of `node`, per `TraverserVisitor`'s child-visiting map.

    An unknown node type is treated as a leaf: a mypy upgrade that adds one costs nothing
    at run time, and the coverage test is what makes the omission visible (D1a).
    """
    accessor = _CHILDREN.get(type(node))
    return [] if accessor is None else list(_flatten(tuple(accessor(node))))


# ---------------------------------------------------------------------------
# What the walk produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallSite:
    """One `CallExpr` the walk found, already attached to its D16 `src` scope.

    `expr` is the mypy node task 2.3's resolution ladder consumes; nothing in this task
    reads it. `scope_id` is the node ID of the nearest enclosing executed scope, and
    `line`/`col` are the site's own position (0-based column, as mypy reports it).
    """

    expr: Any
    scope_id: str
    line: int | None
    col: int | None


@dataclass(frozen=True, slots=True)
class FileExtraction:
    """One analyzed file's contribution to the graph, minus its `calls` edges."""

    file_id: str
    module_id: str
    nodes: tuple[NodeRow, ...] = ()
    edges: tuple[EdgeRow, ...] = ()
    diagnostics: tuple[Diag, ...] = ()
    call_sites: tuple[CallSite, ...] = ()


@dataclass(slots=True)
class _Def:
    """A definition found by the walk, before IDs and qualnames are assigned."""

    kind: str  # 'module' | 'function' | 'class'
    name: str | None  # None for a lambda: its segment is assigned in source order
    line: int | None
    end_line: int | None
    column: int | None
    parent: int | None  # index of the enclosing definition; None means the file node
    attrs: dict[str, Any] = field(default_factory=dict)
    qualname: str = ""


# ---------------------------------------------------------------------------
# Spans (FR-37)
# ---------------------------------------------------------------------------


def _line(value: object) -> int | None:
    """A mypy line number, or None where mypy has none (it writes `-1`, and sometimes 0)."""
    return value if isinstance(value, int) and value >= 1 else None


def _descendant_end(node: Node) -> int | None:
    """The last line any structural descendant of `node` reaches, or None.

    mypy leaves `end_line` unset on a `LambdaExpr` (measured on 2.3.0: `lambda x: x + 1`
    has `line` but neither `end_line` nor `end_column`), so a lambda's extent has to come
    from the body it holds. Reading it off that body is a *determination* from the AST, not
    the fabrication AC-37.3 forbids — the end of the last thing inside a definition is
    where that definition ends.
    """
    last: int | None = None
    stack = children(node)
    while stack:
        current = stack.pop()
        for candidate in (_line(getattr(current, "end_line", None)), _line(current.line)):
            if candidate is not None and (last is None or candidate > last):
                last = candidate
        stack.extend(children(current))
    return last


def _span(node: Node) -> tuple[int | None, int | None]:
    """`(start, end)` for a definition, or `(None, None)` when either end is unknown.

    mypy writes `-1` for a position it does not have. AC-37.3 wants the *span* omitted in
    that case, not half of one, so an undeterminable end discards the start too — a start
    line with no end is not a span.

    **Where this actually happens on real code.** On the pinned Django benchmark the only
    span-less definitions are the 20 methods mypy's dataclass plugin *synthesizes* into a
    `@dataclass` body — `__init__`, `__replace__`, `__mypy-replace`, `__mypy-post_init`.
    They have no position because they have no source. AC-37.3 is written for exactly this
    shape ("an element it can otherwise identify"), so they are indexed with their file
    path and a `span_missing` diagnostic rather than dropped: a dataclass constructor call
    resolves through the MRO to that synthesized `__init__` (task 2.3), and dropping the
    node would trade an auditable span omission for a missing call edge, which FR-14 is
    much less willing to accept.
    """
    start = _line(getattr(node, "line", None))
    if start is None:
        return (None, None)
    end = _line(getattr(node, "end_line", None))
    if end is None:
        end = _descendant_end(node)
    if end is None or end < start:
        return (None, None)
    return (start, end)


def _module_span(tree: MypyFile) -> tuple[int, int]:
    """The module body's extent: line 1 through the last line its statements reach.

    `MypyFile` carries no `end_line`, so the end is read off the body it holds. This is the
    AST's own extent, not a guess at the file's length: trailing comments and blank lines
    are not part of the executed body and do not belong in its span.
    """
    ends = [statement.end_line for statement in tree.defs if isinstance(statement.end_line, int)]
    return (1, max([1, *ends]))


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


class _Walk:
    """One module's traversal: definitions, their nesting, and the call sites between."""

    def __init__(self, module: str, tree: MypyFile) -> None:
        start, end = _module_span(tree)
        self.defs: list[_Def] = [
            _Def(
                kind="module",
                name=module,
                line=start,
                end_line=end,
                column=0,
                parent=None,
                attrs={"python_role": normalize.PYTHON_ROLE_MODULE_BODY},
            )
        ]
        #: Enclosing *definitions*, innermost last. Empty means "directly in the file".
        self._enclosing: list[int] = []
        #: `(CallExpr, scope index)` — resolved to node IDs once the IDs exist.
        self.calls: list[tuple[Any, int]] = []

    # -- scope bookkeeping --------------------------------------------------

    @property
    def _lexical_parent(self) -> int | None:
        """What `contains` this definition: the innermost enclosing def, else the file."""
        return self._enclosing[-1] if self._enclosing else None

    @property
    def _executed_scope(self) -> int:
        """What a call site here belongs to: the innermost def, else the module body."""
        return self._enclosing[-1] if self._enclosing else 0

    def _add(self, kind: str, name: str | None, node: object) -> int:
        start, end = _span(node)
        self.defs.append(
            _Def(
                kind=kind,
                name=name,
                line=start,
                end_line=end,
                column=getattr(node, "column", None),
                parent=self._lexical_parent,
            )
        )
        return len(self.defs) - 1

    # -- the traversal ------------------------------------------------------

    def run(self, tree: MypyFile) -> None:
        for statement in tree.defs:
            self._visit(statement)

    def _visit(self, node: Node) -> None:
        kind = type(node)
        if kind is ClassDef:
            self._class_def(node)
        elif kind is FuncDef:
            self._func_item(node, "function", node.name)
        elif kind is LambdaExpr:
            self._func_item(node, "function", None)
        elif kind is Decorator:
            self._decorator(node)
        else:
            if kind is CallExpr:
                self.calls.append((node, self._executed_scope))
            for child in children(node):
                self._visit(child)

    def _decorator(self, node: Decorator) -> None:
        """A decorated def. The decorator expressions run in the *enclosing* scope (D16)."""
        for decorator in node.decorators:
            self._visit(decorator)
        self._func_item(node.func, "function", node.func.name)

    def _func_item(self, node: Any, kind: str, name: str | None) -> None:
        """A `FuncDef` or a `LambdaExpr`: defaults outside, body inside."""
        index = self._add(kind, name, node)
        for argument in node.arguments or ():
            if argument.initializer is not None:
                self._visit(argument.initializer)
        self._enclosing.append(index)
        self._visit(node.body)
        self._enclosing.pop()

    def _class_def(self, node: ClassDef) -> None:
        """A class. Decorators, bases and keywords run in the enclosing scope; the body
        is its own executed scope, which is where class-level call sites attach (D16)."""
        for decorator in node.decorators:
            self._visit(decorator)
        for base in node.base_type_exprs:
            self._visit(base)
        if node.metaclass is not None:
            self._visit(node.metaclass)
        for keyword in node.keywords.values():
            self._visit(keyword)
        index = self._add("class", node.name, node)
        self._enclosing.append(index)
        self._visit(node.defs)
        self._enclosing.pop()
        if node.analyzed is not None:
            self._visit(node.analyzed)


# ---------------------------------------------------------------------------
# Naming: qualnames, lambda counters, IDs
# ---------------------------------------------------------------------------


def _assign_qualnames(defs: list[_Def], module: str) -> None:
    """Fill in every definition's qualname, numbering lambdas per enclosing scope.

    Lambdas are numbered by source position rather than by walk order. The two agree
    almost everywhere, but not quite — `a[lambda: 1] = lambda: 2` is visited rvalue-first
    — and an ID that depends on traversal order is an ID a mypy upgrade can renumber
    (FR-44). Position is stable; the walk order is not promised to be.
    """
    lambda_numbers: dict[int, int] = {}
    by_parent: dict[int | None, list[int]] = {}
    for index, definition in enumerate(defs):
        if index == 0 or definition.name is not None:
            continue
        by_parent.setdefault(definition.parent, []).append(index)
    for indexes in by_parent.values():
        ordered = sorted(indexes, key=lambda i: (defs[i].line or 0, defs[i].column or 0, i))
        for number, index in enumerate(ordered):
            lambda_numbers[index] = number

    defs[0].qualname = normalize.module_body_qualname(module)
    for index, definition in enumerate(defs):
        if index == 0:
            continue
        prefix = module if definition.parent is None else defs[definition.parent].qualname
        segment = (
            definition.name
            if definition.name is not None
            else normalize.lambda_segment(lambda_numbers[index])
        )
        definition.qualname = normalize.child_qualname(prefix, segment)
        if definition.name is None:
            definition.name = segment


# ---------------------------------------------------------------------------
# `imports` edges (design.md §3.5: file→file, restricted to analyzed files)
# ---------------------------------------------------------------------------


def _absolute_module(importer: str, is_package: bool, relative: int, target: str) -> str | None:
    """The absolute module name a (possibly relative) import statement names.

    `importer` is the *engine's* module name for the importing file, because that is the
    name the import statements in it are written against; the result is looked up in the
    analyzed set and only then translated into a §4.1 path-derived ID.
    """
    if not relative:
        return target or None
    parts = importer.split(".")
    if not is_package:
        parts = parts[:-1]
    ascend = relative - 1
    if ascend:
        if ascend > len(parts):
            return None
        parts = parts[: len(parts) - ascend]
    if target:
        parts.append(target)
    return ".".join(part for part in parts if part) or None


def _imported_modules(tree: MypyFile, importer: str, is_package: bool) -> list[str]:
    """Every module name the file's import statements name, in source order.

    `from pkg import name` names both `pkg` and, when `name` is a submodule rather than an
    attribute, `pkg.name`; both are offered and the analyzed-set lookup decides which
    exists. Nothing here guesses: a name that is not an analyzed module simply matches
    nothing.
    """
    named: list[str] = []
    for statement in tree.imports:
        if isinstance(statement, Import):
            named.extend(module for module, _ in statement.ids)
        elif isinstance(statement, ImportFrom):
            base = _absolute_module(importer, is_package, statement.relative, statement.id)
            if base is None:
                continue
            named.append(base)
            named.extend(f"{base}.{name}" for name, _ in statement.names)
        elif isinstance(statement, ImportAll):
            base = _absolute_module(importer, is_package, statement.relative, statement.id)
            if base is not None:
                named.append(base)
        elif not isinstance(statement, ImportBase):  # pragma: no cover - defensive
            continue
    return named


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def module_index(sources: Iterable[EngineSource]) -> dict[str, str]:
    """`{engine module name: root-relative path}` for the analyzed set.

    The engine names a module `django.db.models.query`; every artifact names the same file
    `db/models/query.py` (design.md §3.5 trap 2). This table is the one place the two
    meet, and it is what restricts `imports` edges to analyzed files.
    """
    return {source.module: source.relpath for source in sources}


def extract_file(
    source: EngineSource, tree: MypyFile, analyzed: Mapping[str, str] | None = None
) -> FileExtraction:
    """Extract one analyzed file's nodes, `contains` edges and `imports` edges.

    `analyzed` is `module_index()`'s table; an empty one simply yields no `imports` edges,
    which is what a single-file analysis should produce.
    """
    relpath = source.relpath
    module = normalize.module_name(relpath)
    file_id = normalize.file_node_id(relpath)
    diagnostics: list[Diag] = []

    walk = _Walk(module, tree)
    walk.run(tree)
    defs = walk.defs
    _assign_qualnames(defs, module)

    ids = normalize.code_node_ids([(definition.qualname, definition.line) for definition in defs])

    nodes: list[NodeRow] = [
        NodeRow(id=file_id, kind="file", name=relpath, language=LANGUAGE_PYTHON, file_path=relpath)
    ]
    edges: list[EdgeRow] = []
    kept: dict[int, str] = {}  # def index → its node ID, for the ones actually emitted
    seen: dict[str, int] = {}

    for index, definition in enumerate(defs):
        node_id = ids[index]
        if definition.line is None:
            # AC-37.3: the element is identified, the span is not, and the omission is on
            # the record. The node still carries its file path (AC-37.1's other half).
            diagnostics.append(
                Diag(
                    kind="span_missing",
                    path=relpath,
                    message=(
                        f"no source span for {definition.kind} {definition.qualname!r}; "
                        f"the node carries its file path with the span omitted"
                    ),
                    extra={"node_id": node_id},
                )
            )
        first = seen.get(node_id)
        if first is not None:
            # Two same-named definitions that both lack a span cannot be told apart by the
            # §4.1 grammar (its only disambiguator is the start line). Keeping the first is
            # arbitrary but stable; dropping the second silently would not be.
            diagnostics.append(
                Diag(
                    kind="span_missing",
                    path=relpath,
                    message=(
                        f"{definition.kind} {definition.qualname!r} could not be "
                        f"distinguished from an earlier definition of the same name "
                        f"without a source span; it is not in the index"
                    ),
                    extra={"node_id": node_id},
                )
            )
            continue
        seen[node_id] = index
        kept[index] = node_id
        nodes.append(
            NodeRow(
                id=node_id,
                kind=definition.kind,
                name=definition.name or definition.qualname,
                language=LANGUAGE_PYTHON,
                file_path=relpath,
                start_line=definition.line,
                end_line=definition.end_line,
                attrs=dict(definition.attrs),
            )
        )
        parent_id = file_id if definition.parent is None else kept.get(definition.parent)
        if parent_id is not None:
            edges.append(EdgeRow(src=parent_id, dst=node_id, kind="contains", src_file=relpath))

    module_id = kept[0]
    is_package = relpath.endswith("__init__.py")
    targets = {
        analyzed[name]
        for name in _imported_modules(tree, source.module, is_package)
        if analyzed and name in analyzed
    }
    for target in sorted(targets - {relpath}):
        edges.append(
            EdgeRow(
                src=file_id,
                dst=normalize.file_node_id(target),
                kind="imports",
                src_file=relpath,
            )
        )

    call_sites = tuple(
        CallSite(
            expr=expr,
            scope_id=kept.get(scope, module_id),
            line=getattr(expr, "line", None) if getattr(expr, "line", -1) > 0 else None,
            col=getattr(expr, "column", None) if getattr(expr, "column", -1) >= 0 else None,
        )
        for expr, scope in walk.calls
    )

    return FileExtraction(
        file_id=file_id,
        module_id=module_id,
        nodes=tuple(nodes),
        edges=tuple(edges),
        diagnostics=tuple(diagnostics),
        call_sites=call_sites,
    )


# ---------------------------------------------------------------------------
# The call-resolution ladder (design.md §3.5, normative; FR-12, FR-14, FR-40)
# ---------------------------------------------------------------------------

#: Node kinds a `calls` edge may point at. `file` is excluded by construction: a file is a
#: namespace, not something that runs (D16). `entry_point` nodes are the detectors' (task
#: 3.1) and are never call targets.
_TARGET_KINDS: frozenset[str] = frozenset({"module", "function", "class"})

#: A code node's ID minus its language namespace and its optional `@line` suffix (§4.1).
_CODE_ID_RE = re.compile(rf"{LANGUAGE_PYTHON}:(?P<qualname>.+?)(?:@(?P<line>\d+))?$")


class _Candidate(NamedTuple):
    """One target the ladder proposes for a call site, before it is looked up.

    `lines` are the definition lines the engine gave for the symbol — the disambiguator
    when a qualname names several nodes. `local` marks a candidate that came from a
    statically bound name, the only kind that may be resolved against the caller's own file
    when the fullname is bare (a nested definition).
    """

    fullname: str
    lines: tuple[int, ...] = ()
    local: bool = False


@dataclass(frozen=True, slots=True)
class _Definition:
    """One analyzed definition, as the target index sees it."""

    node_id: str
    start_line: int | None
    name: str


@dataclass(frozen=True, slots=True)
class ExternalCall:
    """A call whose target resolved outside the analyzed set — `externals.py`'s input.

    D15 source (a): mypy named the target, and the name belongs to typeshed, to a package
    that is not being analyzed, or to a file this run excluded or skipped. The edge cannot
    be written here because its `dst` node does not exist yet; task 2.4 mints the external
    leaf node (deduplicated by `qualified_name`) and turns this record into that edge, which
    is why the record already carries the collapsed call sites and the ambiguity flag.
    """

    src: str
    qualified_name: str
    src_file: str
    call_sites: tuple[tuple[int, int], ...] = ()
    is_ambiguous: int = 0


@dataclass(frozen=True, slots=True)
class CallResolution:
    """One file's `calls` edges, its unresolved sites, and its external hand-offs."""

    edges: tuple[EdgeRow, ...] = ()
    diagnostics: tuple[Diag, ...] = ()
    external_calls: tuple[ExternalCall, ...] = ()


@dataclass(slots=True)
class _Aggregate:
    """Call sites collapsing onto one edge (§4.2: one edge per `(src, dst)`)."""

    sites: set[tuple[int, int]] = field(default_factory=set)
    is_ambiguous: bool = False

    def add(self, position: tuple[int, int] | None, ambiguous: bool) -> None:
        if position is not None:
            self.sites.add(position)
        # An edge aggregating several sites is ambiguous if *any* of them was: the flag
        # says "this edge is one of several possible targets somewhere", and over-claiming
        # ambiguity is the direction FR-14/FR-40 tolerate.
        self.is_ambiguous = self.is_ambiguous or ambiguous

    def attrs(self) -> dict[str, Any]:
        return {"call_sites": [[line, col] for line, col in sorted(self.sites)]}


def _qualname_of(node_id: str) -> str | None:
    """A code node's qualname, or None when the ID is not a code node (a `file:` ID)."""
    match = _CODE_ID_RE.fullmatch(node_id)
    if match is None:
        return None
    qualname = match.group("qualname")
    return None if qualname.startswith("file:") or qualname.startswith("entry:") else qualname


class TargetIndex:
    """The analyzed set, addressable by the fullnames the engine resolves calls to.

    Two lookups, both by name rather than by tree identity (see the module docstring):

    * `lookup()` — an engine fullname (`django.db.models.query.QuerySet.filter`) → the node
      IDs of the analyzed definitions it names, or the fact that it names nothing analyzed.
      The engine's module names and the §4.1 path-derived ones are different strings for the
      same file (design.md §3.5 trap 2), and this is where the two are reconciled.
    * `local()` — a bare name plus a definition line → a node in one specific file, for the
      nested definitions mypy leaves unqualified.
    """

    __slots__ = ("_modules", "_by_tail", "_by_local")

    def __init__(
        self,
        modules: Mapping[str, str],
        by_tail: Mapping[str, Mapping[str, tuple[_Definition, ...]]],
        by_local: Mapping[str, Mapping[tuple[str, int], tuple[str, ...]]],
    ) -> None:
        self._modules = dict(modules)
        self._by_tail = by_tail
        self._by_local = by_local

    @classmethod
    def build(cls, sources: Iterable[EngineSource], nodes: Iterable[NodeRow]) -> TargetIndex:
        """Index the analyzed set: engine module names from `sources`, targets from `nodes`.

        `nodes` is every node of every analyzed file — this run's extractions on a full run,
        or the index's own rows where a file was not re-extracted (task 4.1).
        """
        by_tail: dict[str, dict[str, list[_Definition]]] = {}
        by_local: dict[str, dict[tuple[str, int], list[str]]] = {}
        for node in nodes:
            if node.kind not in _TARGET_KINDS or node.is_external or node.file_path is None:
                continue
            qualname = _qualname_of(node.id)
            if qualname is None:
                continue
            prefix = f"{normalize.module_name(node.file_path)}."
            if not qualname.startswith(prefix):  # pragma: no cover - defensive
                continue
            tail = qualname[len(prefix) :]
            name = tail.rpartition(".")[2]
            by_tail.setdefault(node.file_path, {}).setdefault(tail, []).append(
                _Definition(node_id=node.id, start_line=node.start_line, name=name)
            )
            if node.start_line is not None:
                by_local.setdefault(node.file_path, {}).setdefault(
                    (name, node.start_line), []
                ).append(node.id)
        return cls(
            modules={source.module: source.relpath for source in sources},
            by_tail={
                relpath: {tail: tuple(defs) for tail, defs in tails.items()}
                for relpath, tails in by_tail.items()
            },
            by_local={
                relpath: {key: tuple(ids) for key, ids in entries.items()}
                for relpath, entries in by_local.items()
            },
        )

    def _locate(self, fullname: str) -> tuple[str, str, bool] | None:
        """`(relpath, tail, names something)` for the analyzed module a fullname names.

        A fullname is a module name and a qualname run together with no marker between
        them, so the split is found by trying every prefix, longest first, and preferring
        one whose tail actually names a definition. That preference is what keeps an
        *un*analyzed submodule of an analyzed package (`pkg.excluded.hidden`, where only
        `pkg/__init__.py` is an input) from being read as a definition inside the package's
        `__init__.py`.
        """
        fallback: tuple[str, str, bool] | None = None
        parts = fullname.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            relpath = self._modules.get(".".join(parts[:cut]))
            if relpath is None:
                continue
            tail = ".".join(parts[cut:])
            if self._by_tail.get(relpath, {}).get(tail):
                return relpath, tail, True
            if fallback is None:
                fallback = (relpath, tail, False)
        return fallback

    def module_of(self, fullname: str) -> tuple[str, str] | None:
        """`(relpath, tail)` for a fullname inside the analyzed set, else None."""
        located = self._locate(fullname)
        return None if located is None else (located[0], located[1])

    def lookup(self, fullname: str, lines: Sequence[int] = ()) -> tuple[str, tuple[str, ...]]:
        """`(disposition, node ids)` for one engine fullname.

        Disposition is `internal` (node IDs follow), `external` (the name is outside the
        analyzed set — task 2.4's business), or `unknown` (the name's module *is* analyzed
        but names no indexed definition, or the name is bare; the caller records the loss).
        """
        located = self._locate(fullname)
        if located is None:
            return ("external", (fullname,)) if "." in fullname else ("unknown", ())
        relpath, tail, named = located
        if not named:
            # The prefix is an analyzed module that does not carry this name. Inside a plain
            # module that is a definition the index lacks — a loss worth recording. Inside a
            # package it is an ordinary unanalyzed submodule, which is what an exclusion or a
            # skip leaves behind (AC-12.2, AC-36.2), so it leaves the set like any external.
            return ("external", (fullname,)) if relpath.endswith("__init__.py") else ("unknown", ())
        defs = self._by_tail[relpath][tail]
        if lines:
            narrowed = tuple(d.node_id for d in defs if d.start_line in lines)
            if narrowed:
                return ("internal", narrowed)
        # No line, or a line the index does not carry: every definition under that qualname
        # is a possible target. FR-14 keeps them all rather than picking one.
        return ("internal", tuple(d.node_id for d in defs))

    def local(self, relpath: str, name: str, lines: Sequence[int]) -> tuple[str, ...]:
        """Node IDs in one file for a bare name defined at one of `lines`."""
        table = self._by_local.get(relpath, {})
        found: list[str] = []
        for line in lines:
            found.extend(table.get((name, line), ()))
        return tuple(dict.fromkeys(found))


def _definition_lines(node: object) -> tuple[int, ...]:
    """The source lines of the definitions a symbol names.

    An `@overload` group is one symbol with several definitions, so all of them are
    returned: the group's members are the statically possible targets of a call through
    that name, and dropping the implementation to keep the first stub would point every
    trace at a body that is three dots.
    """
    if isinstance(node, Decorator):
        return _definition_lines(node.func)
    if isinstance(node, OverloadedFuncDef):
        lines: list[int] = []
        for item in node.items:
            lines.extend(_definition_lines(item))
        if node.impl is not None:
            lines.extend(_definition_lines(node.impl))
        return tuple(dict.fromkeys(lines))
    line = getattr(node, "line", None)
    return (line,) if isinstance(line, int) and line >= 1 else ()


def _named(node: object, local: bool = False) -> list[_Candidate]:
    """A candidate for a symbol node that carries a fullname, or nothing."""
    fullname = getattr(node, "fullname", None)
    if not isinstance(fullname, str) or not fullname:
        return []
    return [_Candidate(fullname, _definition_lines(node), local)]


def _constructor_candidates(info: TypeInfo, targets: TargetIndex) -> list[_Candidate]:
    """`C()` → the `__init__` the real MRO reaches (design.md §3.5; C3-correct via mypy).

    A class outside the analyzed set is named by *the class*, not by the `__init__` its
    stubs inherit: AC-36.1 asks for a leaf node for the imported symbol, and
    `collections.OrderedDict` is the symbol the caller wrote, where `builtins.dict.__init__`
    is a typeshed implementation detail of it. Inside the analyzed set the MRO walk is the
    point — it is what makes an inherited constructor resolve to the base class's `__init__`
    — and a class whose MRO offers only `object.__init__` resolves to that typeshed name,
    which leaves the set and becomes an external target like any other.
    """
    class_fullname = getattr(info, "fullname", None)
    if not isinstance(class_fullname, str) or not class_fullname:
        return []
    if targets.lookup(class_fullname)[0] == "external":
        return [_Candidate(class_fullname)]
    symbol = info.get("__init__")
    node = getattr(symbol, "node", None) if symbol is not None else None
    return _named(node)


def _member_candidates(
    receiver: ProperType | None, name: str, targets: TargetIndex
) -> list[_Candidate]:
    """An instance member through the receiver's own type — the union case included.

    `TypeInfo.get()` walks the real MRO, so an inherited method resolves to the class that
    defines it. A union receiver is FR-14's canonical over-approximation: every member of
    the union contributes its own candidate, and the caller flags them all ambiguous.
    """
    if isinstance(receiver, UnionType):
        found: list[_Candidate] = []
        for item in receiver.items:
            found.extend(_member_candidates(get_proper_type(item), name, targets))
        return list(dict.fromkeys(found))
    if isinstance(receiver, TypeType):
        # `type[C]`: an attribute reached through the class object rather than an instance.
        return _member_candidates(get_proper_type(receiver.item), name, targets)
    if not isinstance(receiver, Instance):
        return []
    symbol = receiver.type.get(name)
    node = getattr(symbol, "node", None) if symbol is not None else None
    if isinstance(node, TypeInfo):
        return _constructor_candidates(node, targets)
    return _named(node)


def _callable_candidates(declared: ProperType | None, targets: TargetIndex) -> list[_Candidate]:
    """The definitions a callee expression's own type evaluates to (value flow).

    This is the rung that follows a callable through a variable, a return value, or a
    collection element — `CallableType.definition` is mypy's own record of which `FuncDef`
    a callable type came from. An overloaded type contributes one candidate per item, and a
    union one per member, both of which the caller flags ambiguous.
    """
    if isinstance(declared, CallableType):
        if declared.is_type_obj():
            return _constructor_candidates(declared.type_object(), targets)
        return _named(declared.definition)
    if isinstance(declared, Overloaded):
        found: list[_Candidate] = []
        for item in declared.items:
            found.extend(_callable_candidates(item, targets))
        return list(dict.fromkeys(found))
    if isinstance(declared, UnionType):
        found = []
        for item in declared.items:
            found.extend(_callable_candidates(get_proper_type(item), targets))
        return list(dict.fromkeys(found))
    if isinstance(declared, Instance):
        # A callable object: `obj()` dispatches to its `__call__`.
        symbol = declared.type.get("__call__")
        return _named(getattr(symbol, "node", None) if symbol is not None else None)
    return []


def _candidates(callee: Any, types: Mapping[Any, Any], targets: TargetIndex) -> list[_Candidate]:
    """design.md §3.5's ladder, in its normative order.

    Constructor first, because a bound class name is a bound name too and `C()` does not
    call the class; then the statically bound function/method/overload; then the receiver's
    type for an instance member; then value flow through the callee's own type. The first
    rung that answers wins — each is a stronger statement about the target than the next.
    """
    node = getattr(callee, "node", None)
    if isinstance(node, TypeInfo):
        return _constructor_candidates(node, targets)
    if isinstance(node, (FuncDef, OverloadedFuncDef, Decorator)):
        found = _named(node, local=True)
        if found:
            return found
    if isinstance(callee, MemberExpr):
        found = _member_candidates(get_proper_type(types.get(callee.expr)), callee.name, targets)
        if found:
            return found
    return _callable_candidates(get_proper_type(types.get(callee)), targets)


def _callee_text(expr: object) -> str:
    """The callee as it reads in source, rebuilt from the AST (design.md §4.3).

    `unresolved_call.extra.callee` is the audit trail for the C-11 gap: it is what tells a
    reader *what* went unresolved, and what task 2.4's import table matches names against.
    It is reconstructed rather than sliced out of the file so that extraction needs no
    second read of source that may already have changed under it (EC-14).
    """
    if isinstance(expr, NameExpr):
        return expr.name
    if isinstance(expr, MemberExpr):
        return f"{_callee_text(expr.expr)}.{expr.name}"
    if isinstance(expr, CallExpr):
        return f"{_callee_text(expr.callee)}(...)"
    if isinstance(expr, IndexExpr):
        return f"{_callee_text(expr.base)}[...]"
    if isinstance(expr, SuperExpr):
        return f"super().{expr.name}"
    if isinstance(expr, LambdaExpr):
        return "<lambda>"
    if isinstance(expr, StrExpr):
        return repr(expr.value)
    return f"<{type(expr).__name__}>"


def resolve_calls(
    source: EngineSource,
    extraction: FileExtraction,
    types: Mapping[Any, Any],
    targets: TargetIndex,
) -> CallResolution:
    """Resolve one file's call sites into `calls` edges, hand-offs and diagnostics.

    `types` is the engine's expression→type map (`BuildOutcome.types`). It is populated only
    for modules the engine actually re-type-checked, so on an incremental run this is called
    for the rechecked set and nothing else (D6 rule 1, task 4.1) — calling it for a
    cache-loaded module would resolve nothing and look like a codebase that lost its edges.
    """
    relpath = source.relpath
    internal: dict[tuple[str, str], _Aggregate] = {}
    external: dict[tuple[str, str], _Aggregate] = {}
    diagnostics: list[Diag] = []

    for site in extraction.call_sites:
        callee = site.expr.callee
        position = (site.line, site.col) if site.line is not None and site.col is not None else None
        found_ids: list[str] = []
        found_external: list[str] = []
        unmapped: list[str] = []
        for candidate in _candidates(callee, types, targets):
            disposition, ids = targets.lookup(candidate.fullname, candidate.lines)
            if disposition == "internal":
                found_ids.extend(ids)
            elif disposition == "external":
                found_external.append(candidate.fullname)
            elif candidate.local and "." not in candidate.fullname:
                # A nested definition: mypy names it bare, so it is looked up in the file
                # the bound name was written in.
                local = targets.local(relpath, candidate.fullname, candidate.lines)
                if local:
                    found_ids.extend(local)
                else:
                    unmapped.append(candidate.fullname)
            else:
                unmapped.append(candidate.fullname)

        found_ids = list(dict.fromkeys(found_ids))
        found_external = list(dict.fromkeys(found_external))
        # AC-40.1/40.2: ambiguity is a property of the *site* — more than one statically
        # possible target — and every edge the site produces carries it.
        ambiguous = len(found_ids) + len(found_external) > 1
        for node_id in found_ids:
            internal.setdefault((site.scope_id, node_id), _Aggregate()).add(position, ambiguous)
        for qualified_name in found_external:
            external.setdefault((site.scope_id, qualified_name), _Aggregate()).add(
                position, ambiguous
            )

        if not found_ids and not found_external:
            # AC-14.2: entirely unresolvable, and on the record with enough to find it again.
            diagnostics.append(
                Diag(
                    kind="unresolved_call",
                    path=relpath,
                    line=site.line,
                    col=site.col,
                    message=(
                        f"no call target could be statically determined for "
                        f"{_callee_text(callee)!r}"
                    ),
                    extra={"callee": _callee_text(callee), "scope": site.scope_id},
                )
            )
        elif unmapped:
            # Partially resolved: some candidate named a definition this index does not
            # carry. The edges that did resolve are written, and the loss is still recorded.
            diagnostics.append(
                Diag(
                    kind="unresolved_call",
                    path=relpath,
                    line=site.line,
                    col=site.col,
                    message=(
                        f"call to {_callee_text(callee)!r} resolved to "
                        f"{', '.join(sorted(unmapped))}, which names no indexed definition"
                    ),
                    extra={
                        "callee": _callee_text(callee),
                        "scope": site.scope_id,
                        "unmapped": sorted(unmapped),
                    },
                )
            )

    return CallResolution(
        edges=tuple(
            EdgeRow(
                src=src,
                dst=dst,
                kind="calls",
                src_file=relpath,
                is_ambiguous=int(aggregate.is_ambiguous),
                attrs=aggregate.attrs(),
            )
            for (src, dst), aggregate in sorted(internal.items())
        ),
        diagnostics=tuple(diagnostics),
        external_calls=tuple(
            ExternalCall(
                src=src,
                qualified_name=dst,
                src_file=relpath,
                call_sites=tuple(sorted(aggregate.sites)),
                is_ambiguous=int(aggregate.is_ambiguous),
            )
            for (src, dst), aggregate in sorted(external.items())
        ),
    )


def find_call_sites(tree: MypyFile) -> list[Any]:
    """Every `CallExpr` under `tree`, in walk order — the walker's coverage surface.

    Kept separate from `extract_file()` so the benchmark measurement (walker coverage vs a
    stdlib-`ast` enumeration) asks about the walk and nothing else.
    """
    found: list[Any] = []
    stack: list[Node] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, CallExpr):
            found.append(node)
        stack.extend(children(node))
    return found


def node_types_covered() -> tuple[frozenset[type], frozenset[type]]:
    """`(composite, leaf)` node types the walker knows — the D1a upgrade check's input."""
    return frozenset(_CHILDREN), _LEAVES


__all__ = [
    "CallResolution",
    "CallSite",
    "ExternalCall",
    "FileExtraction",
    "TargetIndex",
    "children",
    "extract_file",
    "find_call_sites",
    "module_index",
    "node_types_covered",
    "resolve_calls",
]
