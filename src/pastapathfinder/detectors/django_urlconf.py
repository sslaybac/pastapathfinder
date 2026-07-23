"""Django URLconf detector (design.md §3.7, D18; FR-11).

design.md §3.7 (`django_urlconf` rule, normative), §4.1 (the `entry:` node-ID form), §4.2
(the `route` `attrs` vocabulary), D14 (stdlib `ast`, no engine), D18 (the import table is
derived from the handed stdlib AST — **not** from `externals.py`, whose tables exist only
for re-extracted modules); requirements FR-11 (AC-11.2, AC-11.3).

Django registers handlers in data rather than in decorators: a URLconf module assigns
`urlpatterns` a list of `path()` / `re_path()` / `url()` calls, each naming a view. This
detector reads that list and emits one `entry_point` node per resolved view, with a `calls`
edge to the view's node — a function node for `views.foo`, the class node for
`FooView.as_view()` — and `attrs.route` carrying the URL pattern when it is a literal.

**The view reference is resolved through the module's import table (§3.7).** `views.foo`
in `app/urls.py` written `from . import views` reconstructs to the qualname `app.views.foo`,
which is the very string `normalize.py` gave that function, so the edge lands on the node
`extract.py` wrote. A name the table does not bind is taken as defined in the URLconf
module itself (URLconfs that define their views inline are ordinary Django). A qualname
that matches no node in the run is unresolved, never fabricated (AC-23.2, and the D18 case
where a routed view has been deleted since the last run).

**`include("mod")` needs no recursion here.** §3.7 describes the included module's patterns
being reached; D18 makes every analyzed file a detector input on every proceeding run, so
`app/more/urls.py` yields its own entries when the pass reaches it — which is the same set
of entry nodes, without this detector reaching for an AST it was not handed. What the
include site owes is the honest report of what it cannot see: a computed include target is
an unresolved diagnostic like any other.

**Anything the list does not state is unresolved, never guessed (AC-11.3).** Patterns
appended in a loop, built by a comprehension, or spliced in from a name are recorded as
`unresolved_entry_declaration` diagnostics with the construct quoted back, and no route is
emitted for them. That is the deliberate negative control of the prototype's Django fixture
(`FINDINGS-harness.md` §2): the dangerous failure is the silent miss, not the miss.
"""

from __future__ import annotations

import ast

from pastapathfinder.adapters.python.normalize import module_name
from pastapathfinder.detectors.base import (
    DetectorOutput,
    ModuleDetector,
    ModuleInput,
    entry_point,
    resolve_target,
)
from pastapathfinder.schema import Diag

#: The name a URLconf module binds its pattern list to (Django's fixed contract).
URLPATTERNS = "urlpatterns"

#: The pattern constructors §3.7 names. `url()` is the pre-2.0 spelling, still widespread.
PATTERN_CALLS: frozenset[str] = frozenset({"path", "re_path", "url"})

#: The call that mounts another URLconf rather than naming a view.
INCLUDE_CALL = "include"

#: The class-based-view adapter: `X.as_view()` routes to the class `X` (§3.7).
AS_VIEW = "as_view"

#: List methods that *add* patterns at runtime — the loop-append idiom. Only these are
#: dynamic registration: `urlpatterns.copy()` adds no route and is not worth a diagnostic.
MUTATORS: frozenset[str] = frozenset({"append", "extend", "insert"})

#: §4.2's reserved node `attrs` key for a route: `{pattern}` here.
ATTR_ROUTE = "route"

#: Longest construct text a diagnostic quotes back, so one diagnostic stays one line.
_QUOTE_LIMIT = 120


def _quote(node: ast.AST) -> str:
    """The source form of `node`, bounded — the audit trail for an unresolved construct."""
    text = ast.unparse(node)
    return text if len(text) <= _QUOTE_LIMIT else f"{text[:_QUOTE_LIMIT]}…"


def _called_name(call: ast.Call) -> str | None:
    """The final name of a call's callee: `path(...)` and `urls.path(...)` both give `path`."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _sort_key(node: ast.AST) -> tuple[int, int]:
    """Source position, so one module's emissions and diagnostics read in file order."""
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


# ---------------------------------------------------------------------------
# The `urlpatterns` value: what is stated, and what is not
# ---------------------------------------------------------------------------


def _flatten(value: ast.expr) -> tuple[list[ast.expr], list[ast.expr]]:
    """`(stated elements, unresolvable expressions)` for a `urlpatterns` value.

    A list/tuple literal states its elements; `+` concatenates two such values (§3.7's
    "list/`+`-concatenation"); a starred element, a comprehension, a name, or a call splices
    in patterns this detector cannot enumerate, and is returned for a diagnostic.
    """
    if isinstance(value, ast.List | ast.Tuple):
        stated: list[ast.expr] = []
        dynamic: list[ast.expr] = []
        for element in value.elts:
            if isinstance(element, ast.Starred):
                dynamic.append(element)
            else:
                stated.append(element)
        return stated, dynamic
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        left_stated, left_dynamic = _flatten(value.left)
        right_stated, right_dynamic = _flatten(value.right)
        return left_stated + right_stated, left_dynamic + right_dynamic
    return [], [value]


def _urlpatterns_values(tree: ast.Module) -> tuple[list[ast.expr], list[ast.expr]]:
    """Every value assigned to `urlpatterns`, and every mutation of it (`append`, `extend`).

    An assignment states patterns; a method call on the list adds patterns at runtime — the
    loop-append idiom — and is returned as a dynamic construct.
    """
    assigned: list[ast.expr] = []
    mutations: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == URLPATTERNS for t in node.targets):
                assigned.append(node.value)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == URLPATTERNS and node.value is not None:
                assigned.append(node.value)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in MUTATORS:
                receiver = func.value
                if isinstance(receiver, ast.Name) and receiver.id == URLPATTERNS:
                    mutations.append(node)
    return assigned, mutations


# ---------------------------------------------------------------------------
# View references
# ---------------------------------------------------------------------------


def _dotted(expr: ast.expr) -> list[str] | None:
    """The dotted name `expr` spells (`views.foo` → `['views', 'foo']`), else None."""
    parts: list[str] = []
    node: ast.expr = expr
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    parts.reverse()
    return parts


def _qualify(expr: ast.expr, module: ModuleInput, this_module: str) -> str | None:
    """The qualname a view reference names, via the import table (§3.7), else None.

    An unbound head name is a definition of this module — a URLconf that defines its views
    inline — so it qualifies under this module's own name. That is a *derivation*, not a
    guess: either the qualname exists as a node or the caller reports it unresolved.
    """
    parts = _dotted(expr)
    if parts is None:
        return None
    base = module.import_table.get(parts[0])
    if base is None:
        return ".".join([this_module, *parts])
    return ".".join([base, *parts[1:]])


def _include_is_static(call: ast.Call) -> bool:
    """True when `include(...)` names its URLconf with a literal (`"mod"` or `("mod", "app")`)."""
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return True
    if isinstance(first, ast.Tuple | ast.List) and first.elts:
        head = first.elts[0]
        return isinstance(head, ast.Constant) and isinstance(head.value, str)
    return False


class DjangoUrlconfDetector(ModuleDetector):
    """Emits an `entry_point` per URLconf-referenced view (FR-11, AC-11.2)."""

    name = "route_django"

    def detect(self, module: ModuleInput) -> DetectorOutput:
        output = DetectorOutput()
        assigned, mutations = _urlpatterns_values(module.tree)
        if not assigned and not mutations:
            return output  # not a URLconf module

        this_module = module_name(module.module_path)
        stated: list[ast.expr] = []
        dynamic: list[ast.expr] = list(mutations)
        for value in assigned:
            value_stated, value_dynamic = _flatten(value)
            stated.extend(value_stated)
            dynamic.extend(value_dynamic)

        for element in sorted(stated, key=_sort_key):
            self._pattern(element, module, this_module, output)
        for construct in sorted(dynamic, key=_sort_key):
            output.diagnostics.append(
                self._unresolved(
                    module,
                    construct,
                    f"{_quote(construct)!r} adds URL patterns that are not statically "
                    "enumerable; no route is fabricated",
                )
            )
        return output

    # -- one `path()` element ------------------------------------------------

    def _pattern(
        self, element: ast.expr, module: ModuleInput, this_module: str, output: DetectorOutput
    ) -> None:
        if not (isinstance(element, ast.Call) and _called_name(element) in PATTERN_CALLS):
            output.diagnostics.append(
                self._unresolved(
                    module,
                    element,
                    f"urlpatterns entry {_quote(element)!r} is not a "
                    "path()/re_path()/url() call; no route is fabricated",
                )
            )
            return

        view = self._view_argument(element)
        if view is None:
            output.diagnostics.append(
                self._unresolved(
                    module, element, f"{_quote(element)!r} names no view argument to resolve"
                )
            )
            return

        if isinstance(view, ast.Call) and _called_name(view) == INCLUDE_CALL:
            # The included URLconf is its own detector input on this same run (D18); the only
            # thing owed here is the report when its target is computed.
            if not _include_is_static(view):
                output.diagnostics.append(
                    self._unresolved(
                        module,
                        view,
                        f"{_quote(view)!r} includes a URLconf named at runtime; "
                        "its patterns are not statically reachable from here",
                    )
                )
            return

        reference = view
        if isinstance(view, ast.Call):
            func = view.func
            if isinstance(func, ast.Attribute) and func.attr == AS_VIEW:
                reference = func.value  # `X.as_view()` routes to the class `X` (§3.7)
            else:
                output.diagnostics.append(
                    self._unresolved(
                        module,
                        view,
                        f"view expression {_quote(view)!r} is computed; no route is fabricated",
                    )
                )
                return

        qualname = _qualify(reference, module, this_module)
        if qualname is None:
            output.diagnostics.append(
                self._unresolved(
                    module,
                    view,
                    f"view expression {_quote(view)!r} is not a name this module binds; "
                    "no route is fabricated",
                )
            )
            return

        target, ambiguous = resolve_target(qualname, module.node_ids)
        if target is None:
            output.diagnostics.append(
                self._unresolved(
                    module,
                    view,
                    f"view {_quote(view)!r} resolves to {qualname!r}, which is not an "
                    "analyzed node",
                )
            )
            return

        attrs: dict[str, object] = {}
        pattern = self._literal_pattern(element)
        if pattern is not None:
            attrs[ATTR_ROUTE] = {"pattern": pattern}
        entry, edge = entry_point(
            detector=self.name,
            qualname=qualname,
            line=element.lineno,
            name=qualname.rpartition(".")[2],
            target=target,
            file_path=module.module_path,
            start_line=element.lineno,
            end_line=element.end_lineno,
            is_ambiguous=1 if ambiguous else 0,
            node_attrs=attrs,
        )
        output.nodes.append(entry)
        output.edges.append(edge)

    @staticmethod
    def _view_argument(call: ast.Call) -> ast.expr | None:
        """`path(route, view, …)`'s second positional argument, or its `view=` keyword."""
        if len(call.args) > 1:
            return call.args[1]
        for keyword in call.keywords:
            if keyword.arg == "view":
                return keyword.value
        return None

    @staticmethod
    def _literal_pattern(call: ast.Call) -> str | None:
        """The URL pattern when it is a literal; None for a computed rule (an f-string, …)."""
        if call.args:
            first = call.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
            return None
        for keyword in call.keywords:
            if keyword.arg in ("route", "regex") and isinstance(keyword.value, ast.Constant):
                value = keyword.value.value
                if isinstance(value, str):
                    return value
        return None

    @staticmethod
    def _unresolved(module: ModuleInput, node: ast.AST, message: str) -> Diag:
        """An AC-11.3 diagnostic locating the construct that could not be resolved."""
        return Diag(
            kind="unresolved_entry_declaration",
            path=module.module_path,
            line=getattr(node, "lineno", None),
            col=getattr(node, "col_offset", None),
            message=message,
        )
