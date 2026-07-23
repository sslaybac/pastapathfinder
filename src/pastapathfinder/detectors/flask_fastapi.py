"""Flask/FastAPI route decorator detector (design.md §3.7; FR-11).

design.md §3.7 (`flask_fastapi` rule, normative), §4.1 (the `entry:` node-ID form), §4.2
(the `route` `attrs` vocabulary), D14 (stdlib `ast`, no engine), D18 (wholesale recompute,
no cross-run state); requirements FR-11 (AC-11.1, AC-11.3).

Both frameworks register a request handler the same way: a decorator on the handler's
`def`, written `<name>.<verb>(...)` or `<name>.route(...)`. That shape is what this
detector recognizes — one `entry_point` node per route decorator, with a `calls` edge to
the decorated function — and `attrs.route` records the receiver name, the verb, and the
first literal path argument when there is one.

**Syntax, not semantics (D14).** The rule is deliberately syntactic: the detector never
asks what `app` is bound to, because it reads a stdlib parse and a file whose semantic
analysis failed must still yield its routes. Recognition is therefore by decorator shape
alone, which is what makes the negative controls work — a plain helper carries no route
decorator, so nothing flags it.

**Variable rule strings are routes with no literal path.** `@app.route(DETAIL_RULE)` and
`@app.get(PREFIX + "/search")` register real routes whose URL is computed; the route is
detected and `attrs.route` simply carries no `path`. Reconstructing the string would be a
fabrication, and an omitted key is a readable "not statically known".

**Registration that is not a decorator on a def is unresolved, never guessed (AC-11.3).**
Two shapes are recognizable as route registration without being a decorator: a call to a
framework's programmatic registrar (`add_api_route`, `add_url_rule`, and their websocket
siblings) and a route decorator applied as a plain call, `app.route(rule)(handler)` — the
loop-registration idiom. Each yields an `unresolved_entry_declaration` diagnostic and no
node. Nothing else is flagged: a bare `<name>.<verb>(...)` call is `dict.get`,
`requests.post`, or `session.delete` far more often than it is a route, and diagnosing
every one of those would bury the real ones.
"""

from __future__ import annotations

import ast

from pastapathfinder.adapters.python.normalize import child_qualname, module_name
from pastapathfinder.detectors.base import (
    DetectorOutput,
    ModuleDetector,
    ModuleInput,
    entry_point,
    resolve_target,
)
from pastapathfinder.schema import Diag

#: §3.7's verb set. `route` (Flask's generic decorator) joins them in `ROUTE_DECORATORS`.
ROUTE_VERBS: frozenset[str] = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options", "websocket"}
)

#: Every decorator attribute name that registers a route.
ROUTE_DECORATORS: frozenset[str] = ROUTE_VERBS | {"route"}

#: The programmatic registrars: Flask's `add_url_rule` and FastAPI/Starlette's `add_*_route`.
#: A call to one registers a route whose handler this detector will not chase (AC-11.3).
DYNAMIC_REGISTRARS: frozenset[str] = frozenset(
    {"add_url_rule", "add_api_route", "add_api_websocket_route", "add_websocket_route"}
)

#: §4.2's reserved node `attrs` key for a route: `{receiver, verb, path}` here.
ATTR_ROUTE = "route"

#: Keywords the two frameworks accept for the rule when it is not passed positionally.
_PATH_KEYWORDS: tuple[str, ...] = ("rule", "path")

#: Longest construct text a diagnostic quotes back, so one diagnostic stays one line.
_QUOTE_LIMIT = 120


def _quote(node: ast.AST) -> str:
    """The source form of `node`, bounded — the audit trail for an unresolved construct."""
    text = ast.unparse(node)
    return text if len(text) <= _QUOTE_LIMIT else f"{text[:_QUOTE_LIMIT]}…"


def _route_decorator(expr: ast.expr) -> tuple[str, str] | None:
    """`(receiver, verb)` when `expr` is a `<name>.<verb>(...)` route decorator, else None.

    The receiver must be a plain name: `<name>.<verb>` is the shape §3.7 states, and it is
    the shape both frameworks' documentation and every fixture use. `@app.route` without a
    call is not it either — the decorators these frameworks define are always called.
    """
    if not isinstance(expr, ast.Call):
        return None
    func = expr.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None
    if func.attr not in ROUTE_DECORATORS:
        return None
    return func.value.id, func.attr


def _literal_path(call: ast.Call) -> str | None:
    """The first literal path argument, or None when the rule is computed (a variable rule)."""
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None
    for keyword in call.keywords:
        if keyword.arg in _PATH_KEYWORDS:
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    return None


def _is_applied_decorator(call: ast.Call) -> bool:
    """True for `app.route(rule)(handler)` — a route decorator applied as a plain call."""
    return isinstance(call.func, ast.Call) and _route_decorator(call.func) is not None


def _is_dynamic_registrar(call: ast.Call) -> bool:
    """True for `<name>.add_api_route(...)` and its siblings (§3.7's dynamic registration)."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.attr in DYNAMIC_REGISTRARS
    )


class FlaskFastapiRouteDetector(ModuleDetector):
    """Emits an `entry_point` per Flask/FastAPI route decorator (FR-11, AC-11.1)."""

    name = "route_flask_fastapi"

    def detect(self, module: ModuleInput) -> DetectorOutput:
        output = DetectorOutput()
        self._walk(module.tree, module_name(module.module_path), module, output)
        return output

    def _walk(
        self, node: ast.AST, prefix: str, module: ModuleInput, output: DetectorOutput
    ) -> None:
        """Traverse in source order, carrying the qualname prefix of the enclosing scope.

        The prefix is what makes a route on a nested def (Flask's application-factory
        idiom) or on a method resolve to the right node: the qualname `extract.py` gave the
        handler is its scope chain, and this walk reproduces exactly that chain.
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                self._function(child, prefix, module, output)
                self._walk(child, child_qualname(prefix, child.name), module, output)
            elif isinstance(child, ast.ClassDef):
                self._class(child, module, output)
                self._walk(child, child_qualname(prefix, child.name), module, output)
            else:
                if isinstance(child, ast.Call):
                    self._call(child, module, output)
                self._walk(child, prefix, module, output)

    def _function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        prefix: str,
        module: ModuleInput,
        output: DetectorOutput,
    ) -> None:
        """One route entry per route decorator on this def (stacked decorators stack routes)."""
        qualname = child_qualname(prefix, node.name)
        for decorator in node.decorator_list:
            recognized = _route_decorator(decorator)
            if recognized is None:
                continue
            receiver, verb = recognized
            assert isinstance(decorator, ast.Call)  # _route_decorator matched a call
            target, ambiguous = resolve_target(qualname, module.node_ids)
            if target is None:
                output.diagnostics.append(
                    Diag(
                        kind="unresolved_entry_declaration",
                        path=module.module_path,
                        line=decorator.lineno,
                        col=decorator.col_offset,
                        message=(
                            f"route decorator {_quote(decorator)!r} on {qualname!r} does not "
                            "resolve to an analyzed node"
                        ),
                    )
                )
                continue
            route: dict[str, object] = {"receiver": receiver, "verb": verb}
            path = _literal_path(decorator)
            if path is not None:
                route["path"] = path
            entry, edge = entry_point(
                detector=self.name,
                qualname=qualname,
                line=decorator.lineno,
                name=node.name,
                target=target,
                file_path=module.module_path,
                start_line=decorator.lineno,
                end_line=node.end_lineno,
                is_ambiguous=1 if ambiguous else 0,
                node_attrs={ATTR_ROUTE: route},
            )
            output.nodes.append(entry)
            output.edges.append(edge)

    def _class(self, node: ast.ClassDef, module: ModuleInput, output: DetectorOutput) -> None:
        """A route decorator on a class registers something this detector cannot name."""
        for decorator in node.decorator_list:
            if _route_decorator(decorator) is None:
                continue
            output.diagnostics.append(
                Diag(
                    kind="unresolved_entry_declaration",
                    path=module.module_path,
                    line=decorator.lineno,
                    col=decorator.col_offset,
                    message=(
                        f"route decorator {_quote(decorator)!r} decorates class {node.name!r}, "
                        "not a function; no handler to point at"
                    ),
                )
            )

    def _call(self, node: ast.Call, module: ModuleInput, output: DetectorOutput) -> None:
        """Registration that is not a decorator on a def: recorded, never resolved (AC-11.3)."""
        if _is_dynamic_registrar(node):
            detail = "registers a route programmatically"
        elif _is_applied_decorator(node):
            detail = "applies a route decorator as a call"
        else:
            return
        output.diagnostics.append(
            Diag(
                kind="unresolved_entry_declaration",
                path=module.module_path,
                line=node.lineno,
                col=node.col_offset,
                message=(
                    f"{_quote(node)!r} {detail}; the handler is not statically resolvable "
                    "and no route is fabricated"
                ),
            )
        )
