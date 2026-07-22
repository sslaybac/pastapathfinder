"""The two detector shapes, the per-module import table, and entry-node emission.

design.md §3.7 (`detectors`), §4.1 (node-ID grammar, `entry:` form), §4.2 (reserved
`attrs` keys), D14 (detectors are engine-independent, on stdlib `ast`), D16 (the
module-body node a `__main__` entry targets), D18 (per-module import table derived from
the handed stdlib AST, no adapter state); requirements FR-8, FR-9.

A detector recognizes exactly one entry-point pattern and emits, per occurrence, one
`entry_point` node plus one `calls` edge to the node the entry drives (FR-8). Two shapes:

* a **per-module** detector reads one file's stdlib AST (`ModuleInput`) — `main_block`,
  and the route detectors of task 3.3;
* a **project-level** detector reads the packaging metadata (`ProjectInput`) —
  `console_scripts` of task 3.2.

Everything Python-specific about how a detector reads code stays inside the detector; the
one thing every detector shares — turning a recognized site into schema-conformant rows —
lives in `entry_point()` here, so adding a detector is one new module plus one registry
entry and never a schema edit (AC-8.1).

**No engine, and no adapter state.** Detectors parse with the standard library, so their
isolation holds even for a file the engine produced no tree for (D14). The one adapter
module they lean on is `normalize`, and only for node-ID construction: it is the single
producer of the ID grammar (design.md §4.1), and an entry edge whose `dst` did not match
the module-body ID `extract.py` wrote would be rejected at validation (AC-23.2). Reusing
that producer is what keeps the two in step; it imports no engine (AC-23.1 is unaffected).
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pastapathfinder.adapters.python.normalize import module_name
from pastapathfinder.schema import DETECTORS, LANGUAGE_PYTHON, Diag, EdgeRow, NodeRow

#: §4.2's reserved `attrs` key naming the detector that emitted an entry node.
ATTR_DETECTOR = "detector"

#: The three packaging files a project-level detector reads (FR-10, C-1's packaging-only
#: scope). Discovered at the analysis root; parsing them is a detector's own job.
METADATA_FILENAMES: tuple[str, ...] = ("pyproject.toml", "setup.cfg", "setup.py")


# ---------------------------------------------------------------------------
# Per-module import table (D18)
# ---------------------------------------------------------------------------


def is_package(relpath: str) -> bool:
    """True when `relpath` is an `__init__.py`, whose relative imports climb differently."""
    return relpath == "__init__.py" or relpath.endswith("/__init__.py")


def _absolute_module(importer: str, in_package: bool, level: int, target: str | None) -> str | None:
    """Resolve a `from`-import's base to an absolute dotted name (None if it climbs out).

    `importer` is the path-derived module name (`normalize.module_name`), so the qualified
    names this table produces are in the same namespace as the node IDs a detector matches
    them against — a call written `views.foo` reconstructs to the very string
    `extract.py` gave `views.py`'s `foo`.
    """
    if not level:
        return target or None
    parts = importer.split(".")
    if not in_package:
        parts = parts[:-1]
    ascend = level - 1
    if ascend:
        if ascend > len(parts):
            return None
        parts = parts[: len(parts) - ascend]
    if target:
        parts.append(target)
    return ".".join(part for part in parts if part) or None


def import_table(tree: ast.Module, module: str, in_package: bool) -> dict[str, str]:
    """`{bound name: qualified name}` for every import statement in one module (D18).

    Built from the stdlib AST the detector is handed and nothing else, so it carries no
    cross-run state and is a pure function of the tree (design.md §3.7). Three rules, each
    matching the adapter's own syntactic import table so the two agree on real code:

    * **Every scope, not only the module body** — deferred imports inside functions are how
      real code (Django especially) breaks cycles, and a routed view is often imported that
      way.
    * **`import a.b.c` binds `a`** — Python's own rule; a callee `a.b.c.thing` is rebuilt
      from the binding plus the attribute path it was written with.
    * **A name bound twice to different things is dropped** — an ambiguous binding would
      make a detector's resolution a guess, which AC-11.3/AC-10.2 forbid.
    """
    bound: dict[str, str | None] = {}

    def bind(name: str, qualified: str) -> None:
        if name in bound and bound[name] != qualified:
            bound[name] = None
        else:
            bound[name] = qualified

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bind(alias.asname, alias.name)
                else:
                    head = alias.name.partition(".")[0]
                    bind(head, head)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(module, in_package, node.level, node.module)
            if base is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    # A star import binds names this table cannot enumerate; claim nothing.
                    continue
                bind(alias.asname or alias.name, f"{base}.{alias.name}")

    return {name: qualified for name, qualified in bound.items() if qualified is not None}


# ---------------------------------------------------------------------------
# Detector inputs (the two shapes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModuleInput:
    """What a per-module detector reads: one analyzed file's stdlib AST (design.md §3.7)."""

    module_path: str  # root-relative POSIX relpath
    tree: ast.Module  # a stdlib `ast` parse — never a mypy tree (D14)
    import_table: Mapping[str, str]  # `{bound name: qualified name}`, from `tree` (D18)

    @classmethod
    def build(cls, module_path: str, tree: ast.Module) -> ModuleInput:
        """Assemble the input, deriving the import table from `tree` alone (D18)."""
        return cls(
            module_path=module_path,
            tree=tree,
            import_table=import_table(tree, module_name(module_path), is_package(module_path)),
        )


@dataclass(frozen=True, slots=True)
class ProjectInput:
    """What a project-level detector reads: the packaging metadata file set (FR-10).

    `node_ids` is the set of code-node IDs already in the index for this run — the
    resolution target for `console_scripts`, whose declarations (`pkg.mod:func`) name a
    function the analyzed code may or may not contain (design.md §3.7: "resolve `pkg.mod:func`
    against index node IDs"). It rides on the input rather than the detector so a
    project-level detector stays constructible with no arguments (it lives in the static
    registry, design.md §3.7) and so the run stays a pure function of what it is handed
    (D18): the runner (task 3.4) passes the index's IDs, and a test passes its own. The
    default is empty, which resolves nothing — a project with no analyzed graph declares no
    reachable console script, which is the honest answer, not a crash.
    """

    root: Path
    metadata_files: Mapping[str, Path]  # `{filename: path}` for those present at the root
    node_ids: frozenset[str] = frozenset()  # index code-node IDs, the resolution target

    @classmethod
    def discover(cls, root: Path, node_ids: frozenset[str] = frozenset()) -> ProjectInput:
        """The metadata files present at the analysis root, keyed by filename."""
        present = {name: root / name for name in METADATA_FILENAMES if (root / name).is_file()}
        return cls(root=root, metadata_files=present, node_ids=node_ids)


# ---------------------------------------------------------------------------
# Detector shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectorOutput:
    """One `detect()` call's emission: entry nodes, their `calls` edges, and diagnostics.

    Kept separate from `GraphFragment`, which is one *file's* contribution: a project-level
    detector's nodes span many files, and even a per-module entry edge points at a node
    (the module body) that lives in a different fragment.
    """

    nodes: list[NodeRow] = field(default_factory=list)
    edges: list[EdgeRow] = field(default_factory=list)
    diagnostics: list[Diag] = field(default_factory=list)


class Detector:
    """Common base: a detector names itself once, and that name is both the §4.1 `detector`
    production it stamps into IDs and the label AC-8.2's `detector_error` diagnostic names.

    `name` must be one of §4.1's fixed `detector` values (`schema.DETECTORS`); a detector
    naming itself anything else would emit an ID no reader can parse.
    """

    #: The §4.1 `detector` production value; also the diagnostic label.
    name: str = ""


class ModuleDetector(Detector):
    """A detector that reads one analyzed file at a time (design.md §3.7)."""

    def detect(self, module: ModuleInput) -> DetectorOutput:  # pragma: no cover - abstract
        raise NotImplementedError


class ProjectDetector(Detector):
    """A detector that reads the project metadata once (design.md §3.7)."""

    def detect(self, project: ProjectInput) -> DetectorOutput:  # pragma: no cover - abstract
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Entry-node emission (the one thing every detector shares)
# ---------------------------------------------------------------------------


def entry_node_id(detector: str, qualname: str, line: int) -> str:
    """The `entry:` form of §4.1: `python:entry:<detector>:<qualname>@<line>`.

    The single producer of entry-node IDs, mirroring `normalize`'s role for code nodes.
    """
    if detector not in DETECTORS:
        raise ValueError(f"unknown detector {detector!r}; design.md §4.1 defines {list(DETECTORS)}")
    return f"{LANGUAGE_PYTHON}:entry:{detector}:{qualname}@{line}"


def entry_point(
    *,
    detector: str,
    qualname: str,
    line: int,
    name: str,
    target: str,
    file_path: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    is_ambiguous: int = 0,
    node_attrs: Mapping[str, object] | None = None,
) -> tuple[NodeRow, EdgeRow]:
    """Build one `entry_point` node and its single `calls` edge to `target` (FR-8, §3.7).

    Every detector routes its recognized sites through here, so the entry-node shape — the
    `kind`, the `entry:` ID, the `attrs.detector` stamp, the one outgoing `calls` edge — is
    defined once and an emitted entry always validates (§4.2). `target` is the id of the
    node the entry drives: the module-body node for `main_block`, a function or class node
    for the others.

    The edge carries no `src_file`: its `src` is a detector, not a caller, and D18 recomputes
    entry nodes wholesale rather than evicting them by caller file, so the column that keys
    D6's per-file eviction does not apply.
    """
    entry_id = entry_node_id(detector, qualname, line)
    attrs: dict[str, object] = {ATTR_DETECTOR: detector}
    if node_attrs:
        attrs.update(node_attrs)
    node = NodeRow(
        id=entry_id,
        kind="entry_point",
        name=name,
        language=LANGUAGE_PYTHON,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        attrs=attrs,
    )
    edge = EdgeRow(
        src=entry_id,
        dst=target,
        kind="calls",
        src_file=None,
        is_ambiguous=is_ambiguous,
    )
    return node, edge
