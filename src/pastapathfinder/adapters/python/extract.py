"""The AST walk: nodes, spans, `contains` and `imports` edges (design.md §3.5, D16).

design.md §3.5 (`extract`, node half), §4.1 (node-ID grammar), §4.2 (DDL, reserved
`attrs` keys), D16, D22; requirements FR-12 (node half), FR-21, FR-22, FR-37 (AC-37.1,
AC-37.2, AC-37.3).

One module's typed AST in, one file's graph rows out: the `file` node, the module-body
node (`kind='module'`, D16), a node per function/method/class/lambda with its span, the
`contains` edges that hold them together, and `imports` edges to the other analyzed files
this one imports. `calls` edges are task 2.3's; this module hands that task the call sites
it found, already attached to the scope D16 makes their `src`.

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
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

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
    "CallSite",
    "FileExtraction",
    "children",
    "extract_file",
    "find_call_sites",
    "module_index",
    "node_types_covered",
]
