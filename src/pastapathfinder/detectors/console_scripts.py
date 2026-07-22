"""Packaging-declared CLI entry points, parsed and never executed (design.md §3.7).

design.md §3.7 (`console_scripts` rule), §4.1 (the `entry:` node-ID form), §4.2 (`attrs`);
requirements FR-10 (AC-10.1, AC-10.2, and C-1's packaging-only scope), FR-13 (no execution
of target code).

A console script is a declaration in packaging metadata — `mytool = pkg.mod:func` — that
installs a launcher calling `func`. This detector reads three metadata files **statically**
and emits one `entry_point` node per declaration whose target resolves to an analyzed
function, with a `calls` edge to it:

* `pyproject.toml` — `[project.scripts]` and `[project.entry-points.console_scripts]`
  (`tomllib`);
* `setup.cfg` — `[options.entry_points]`'s `console_scripts` block (`configparser`);
* `setup.py` — a **literal** `entry_points` argument, via a stdlib `ast` walk.

**Never executed (FR-13).** `setup.py` is parsed, not run: only a literal dict/list of
string constants is read, and any *computed* `entry_points` value is recorded as an
`unresolved_entry_declaration` diagnostic rather than evaluated. The TOML and cfg parsers do
not execute code either.

**Resolution, never a guess (AC-10.2).** A declaration's `module:qualname` becomes the
qualname `module.qualname`, which is matched against the index node IDs the run already
built (`ProjectInput.node_ids`) — the same path-derived namespace `normalize.py` writes, so
`pkg.cli:main` resolves to the node `pkg/cli.py`'s `main` was given. A target absent from
the index yields an `unresolved_entry_declaration` diagnostic naming the declaration; it is
never silently dropped and never fabricated into an edge to a node that does not exist.

Bare scripts with neither a `__main__` guard (FR-9) nor a packaging declaration are out of
v1 scope (C-1, backlog B-20); this detector recognizes only declared entry points.
"""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass

from pastapathfinder.adapters.python.normalize import code_node_id
from pastapathfinder.detectors.base import (
    DetectorOutput,
    ProjectDetector,
    ProjectInput,
    entry_point,
)
from pastapathfinder.schema import Diag, EdgeRow, NodeRow

#: §4.2's reserved `attrs` key recording the declaring command name (provenance).
ATTR_COMMAND = "command"

#: The `setup()`-call keyword whose value declares entry points.
_ENTRY_POINTS_KW = "entry_points"

#: The entry-point group console scripts live under, in every format.
_CONSOLE_SCRIPTS = "console_scripts"


@dataclass(frozen=True, slots=True)
class _Declaration:
    """One raw `command = module:qualname` declaration, before resolution."""

    command: str  # the launcher name, e.g. `mytool`
    target: str  # the object reference, e.g. `pkg.mod:func` (extras not yet stripped)
    source: str  # the metadata filename it was read from
    line: int  # 1-based line of the declaration in `source` (its `@line` disambiguator)


# ---------------------------------------------------------------------------
# Source-line lookup
# ---------------------------------------------------------------------------


def _declaration_line(text: str, command: str, target: str) -> int:
    """The 1-based line where `command = target` is written in `text` (1 if not found).

    Every format writes a declaration as `command = target` on one physical line — a TOML
    `mytool = "pkg:func"`, a cfg `mytool = pkg:func`, a `setup.py` list string
    `"mytool = pkg:func"`. `tomllib`/`configparser`/`ast.Constant` give values but no source
    position for the key, so the position is recovered by locating that pair. The line is the
    entry node's `@line` disambiguator and its displayed source location; a miss (an unusual
    layout) falls back to line 1, which is honest — the declaration is in this file — and
    still valid, differing entries keeping distinct IDs through their differing target
    qualnames.
    """
    pattern = re.compile(
        rf"""^\s*["']?{re.escape(command)}["']?\s*=\s*["']?{re.escape(target)}""",
        re.MULTILINE,
    )
    match = pattern.search(text)
    return text.count("\n", 0, match.start()) + 1 if match else 1


# ---------------------------------------------------------------------------
# Per-format extraction (each: text -> declarations + parse-level diagnostics)
# ---------------------------------------------------------------------------


def _from_pyproject(text: str) -> tuple[list[_Declaration], list[Diag]]:
    """`[project.scripts]` and `[project.entry-points.console_scripts]` (PEP 621)."""
    data = tomllib.loads(text)
    project = data.get("project")
    if not isinstance(project, dict):
        return [], []
    entry_points = project.get("entry-points")
    tables = [
        project.get("scripts"),
        entry_points.get(_CONSOLE_SCRIPTS) if isinstance(entry_points, dict) else None,
    ]
    declarations: list[_Declaration] = []
    diagnostics: list[Diag] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        for command, target in table.items():
            if not isinstance(command, str) or not isinstance(target, str):
                diagnostics.append(
                    Diag(
                        kind="unresolved_entry_declaration",
                        path="pyproject.toml",
                        message=f"console-script declaration {command!r} is not a string target",
                    )
                )
                continue
            line = _declaration_line(text, command, target)
            declarations.append(_Declaration(command, target, "pyproject.toml", line))
    return declarations, diagnostics


def _from_setup_cfg(text: str) -> tuple[list[_Declaration], list[Diag]]:
    """`[options.entry_points]`'s `console_scripts` value, one `name = target` per line."""
    parser = configparser.ConfigParser()
    parser.read_string(text)
    if not parser.has_option("options.entry_points", _CONSOLE_SCRIPTS):
        return [], []
    raw = parser.get("options.entry_points", _CONSOLE_SCRIPTS)
    declarations: list[_Declaration] = []
    for entry in raw.splitlines():
        stripped = entry.strip()
        if not stripped or stripped.startswith("#"):
            continue
        command, separator, target = stripped.partition("=")
        if not separator:
            continue  # not a `name = target` line; nothing to resolve
        command, target = command.strip(), target.strip()
        declarations.append(
            _Declaration(command, target, "setup.cfg", _declaration_line(text, command, target))
        )
    return declarations, []


def _from_setup_py(text: str) -> tuple[list[_Declaration], list[Diag]]:
    """A **literal** `entry_points` argument to `setup(...)`, via `ast` — never executed.

    Only a literal dict of literal string lists is read (FR-13). A non-literal
    `entry_points` (a name, a call, a comprehension) or a non-literal list element is
    recorded as `unresolved_entry_declaration` rather than evaluated — the failure mode a
    guessing implementation would hide.
    """
    tree = ast.parse(text)
    declarations: list[_Declaration] = []
    diagnostics: list[Diag] = []
    for call in _setup_calls(tree):
        value = _keyword_value(call, _ENTRY_POINTS_KW)
        if value is None:
            continue
        if not isinstance(value, ast.Dict):
            diagnostics.append(_computed("setup.py", value, "entry_points is not a literal dict"))
            continue
        group = _dict_literal_value(value, _CONSOLE_SCRIPTS)
        if group is None:
            continue  # no console_scripts group, or a non-literal key — nothing literal to read
        if not isinstance(group, ast.List | ast.Tuple):
            diagnostics.append(
                _computed("setup.py", group, "console_scripts is not a literal list")
            )
            continue
        for element in group.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                diagnostics.append(
                    _computed("setup.py", element, "console_scripts entry is not a literal string")
                )
                continue
            command, separator, target = element.value.partition("=")
            if not separator:
                continue
            command, target = command.strip(), target.strip()
            declarations.append(_Declaration(command, target, "setup.py", element.lineno))
    return declarations, diagnostics


def _setup_calls(tree: ast.Module) -> Iterable[ast.Call]:
    """Every `setup(...)` / `<pkg>.setup(...)` call in the module (usually exactly one)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "setup") or (
                isinstance(func, ast.Attribute) and func.attr == "setup"
            ):
                yield node


def _keyword_value(call: ast.Call, name: str) -> ast.expr | None:
    """The AST value of `call`'s `name=` keyword, or None when it is absent."""
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _dict_literal_value(node: ast.Dict, key: str) -> ast.expr | None:
    """The value node paired with the literal string `key` in a dict literal, else None."""
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if isinstance(key_node, ast.Constant) and key_node.value == key:
            return value_node
    return None


def _computed(source: str, node: ast.AST, detail: str) -> Diag:
    """A diagnostic for a value `setup.py` computes rather than states (FR-13)."""
    return Diag(
        kind="unresolved_entry_declaration",
        path=source,
        line=getattr(node, "lineno", None),
        col=getattr(node, "col_offset", None),
        message=f"{detail}; not evaluated (FR-13 forbids executing setup.py)",
    )


_EXTRACTORS = {
    "pyproject.toml": _from_pyproject,
    "setup.cfg": _from_setup_cfg,
    "setup.py": _from_setup_py,
}


# ---------------------------------------------------------------------------
# Resolution against index node IDs
# ---------------------------------------------------------------------------


def _resolve(target: str, node_ids: frozenset[str]) -> tuple[str | None, str | None, bool]:
    """`(target_qualname, target_node_id, ambiguous)` for a `module:qualname` reference.

    Returns `(None, None, False)` for a reference this detector cannot even shape into a
    qualname (no `module:object` colon). Otherwise `target_qualname` is always the derived
    qualname (used to name the entry even when unresolved), `target_node_id` is the matching
    index node or None when none matches, and `ambiguous` marks a target that matches only
    through `@line`-suffixed collision variants of which there is more than one.
    """
    spec = target.split("[", 1)[0].strip()  # drop any `[extras]` suffix
    module, separator, attribute = spec.partition(":")
    module, attribute = module.strip(), attribute.strip()
    if not separator or not module or not attribute:
        return None, None, False
    qualname = f"{module}.{attribute}"
    exact = code_node_id(qualname)  # `python:module.attribute`, the collision-free form
    if exact in node_ids:
        return qualname, exact, False
    prefix = f"{exact}@"  # `@line`-suffixed collision variants (normalize.code_node_ids)
    suffixes = (node_id[len(prefix) :] for node_id in node_ids if node_id.startswith(prefix))
    lines = sorted(int(suffix) for suffix in suffixes if suffix.isdigit())  # by line: "@7" < "@12"
    variants = [f"{prefix}{line}" for line in lines]
    if not variants:
        return qualname, None, False
    return qualname, variants[0], len(variants) > 1


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


class ConsoleScriptsDetector(ProjectDetector):
    """Emits an `entry_point` for each packaging-declared console script (FR-10)."""

    name = "console_script"

    def detect(self, project: ProjectInput) -> DetectorOutput:
        nodes: list[NodeRow] = []
        edges: list[EdgeRow] = []
        diagnostics: list[Diag] = []

        declarations: list[_Declaration] = []
        for filename, extractor in _EXTRACTORS.items():
            path = project.metadata_files.get(filename)
            if path is None:
                continue
            text = path.read_text(encoding="utf-8")
            try:
                found, parse_diagnostics = extractor(text)
            except (tomllib.TOMLDecodeError, configparser.Error, SyntaxError, ValueError) as exc:
                # A metadata file we cannot parse yields no declarations, but the failure is
                # named rather than swallowed: the run should not appear to have found no
                # console scripts when it in fact could not read them.
                diagnostics.append(
                    Diag(
                        kind="unresolved_entry_declaration",
                        path=filename,
                        message=f"could not parse {filename}: {exc}",
                    )
                )
                continue
            declarations.extend(found)
            diagnostics.extend(parse_diagnostics)

        # Deduplicate declarations that would build the same entry (e.g. the same command in
        # both pyproject tables), keeping deterministic order.
        seen: set[tuple[str, str, str]] = set()
        for declaration in declarations:
            key = (declaration.command, declaration.target, declaration.source)
            if key in seen:
                continue
            seen.add(key)

            qualname, target_id, ambiguous = _resolve(declaration.target, project.node_ids)
            if target_id is None:
                diagnostics.append(
                    Diag(
                        kind="unresolved_entry_declaration",
                        path=declaration.source,
                        line=declaration.line,
                        message=(
                            f"console script {declaration.command!r} → {declaration.target!r} "
                            "does not resolve to an analyzed function"
                        ),
                    )
                )
                continue

            assert qualname is not None  # a resolved target always has a qualname
            node, edge = entry_point(
                detector=self.name,
                qualname=qualname,
                line=declaration.line,
                name=declaration.command,
                target=target_id,
                file_path=declaration.source,
                start_line=declaration.line,
                end_line=declaration.line,
                is_ambiguous=1 if ambiguous else 0,
                node_attrs={ATTR_COMMAND: declaration.command},
            )
            nodes.append(node)
            edges.append(edge)

        return DetectorOutput(nodes=nodes, edges=edges, diagnostics=diagnostics)
