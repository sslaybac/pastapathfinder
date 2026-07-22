"""The AST walk: nodes, spans, `contains` and `imports` edges.

design.md §3.5 (`extract`, node half), §4.1, §4.2, D16, D22; requirements FR-12 (node
half), FR-21, FR-22 (AC-22.1), FR-37 (AC-37.1, AC-37.2, AC-37.3).

Every test drives the real engine over a real tree: the walker's subject is a mypy 2.3.0
AST, and a hand-built double would be a test of the double's shape rather than of the
engine's. A cold build of a two-file package costs about a second.
"""

import inspect
import io
from pathlib import Path

import mypy.nodes
import mypy.patterns
import pytest
from mypy.nodes import CallExpr, FuncDef, MypyFile, NameExpr, Node

from pastapathfinder.adapters.base import SourceFile
from pastapathfinder.adapters.python import extract
from pastapathfinder.adapters.python.mypy_driver import run_build
from pastapathfinder.index import full_write, open_index
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import (
    NODE_KINDS,
    FileRecord,
    GraphFragment,
    is_valid_node_id,
    validate_fragment,
)

META = {
    "tool_version": "0.1.0",
    "engine": "mypy",
    "engine_version": "2.3.0",
    "root_path": "/srv/target",
    "created_at": "2026-07-22T09:00:00+00:00",
    "run_id": "11111111-2222-3333-4444-555555555555",
}


def build(root: Path, files: dict[str, str]):
    """Write `{relpath: source}` under `root` and run one engine build over it."""
    for relpath, content in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    sources = [SourceFile(path=root / relpath, relpath=relpath) for relpath in sorted(files)]
    return run_build(sources, root.parent / "cache", ProgressSink(stream=io.StringIO()))


def extract_all(outcome) -> dict[str, extract.FileExtraction]:
    """Extract every analyzed file of a build, keyed by relpath."""
    index = extract.module_index(outcome.sources)
    return {
        source.relpath: extract.extract_file(source, outcome.tree(source.relpath), index)
        for source in outcome.sources
    }


def one(tmp_path: Path, source: str, relpath: str = "pkg/m.py") -> extract.FileExtraction:
    """Extract a single module written into a package."""
    outcome = build(tmp_path / "code", {"pkg/__init__.py": "", relpath: source})
    assert outcome.skipped == ()
    return extract_all(outcome)[relpath]


def node_map(extraction: extract.FileExtraction) -> dict[str, object]:
    return {node.id: node for node in extraction.nodes}


def edge_set(extraction: extract.FileExtraction, kind: str) -> set[tuple[str, str]]:
    return {(edge.src, edge.dst) for edge in extraction.edges if edge.kind == kind}


# ---------------------------------------------------------------------------
# The walker: fidelity to `TraverserVisitor`, and the module boundary
# ---------------------------------------------------------------------------

#: Bases that exist to be inherited from, never instantiated as tree nodes.
_ABSTRACT = {
    "Node",
    "SymbolNode",
    "Statement",
    "Expression",
    "FuncBase",
    "FuncItem",
    "Lvalue",
    "ImportBase",
    "RefExpr",
    "TypeVarLikeExpr",
    "Pattern",
}


def concrete_node_types() -> set[type]:
    """Every concrete `Node` subclass mypy 2.3.0 can put in a tree."""
    found: set[type] = set()
    for module in (mypy.nodes, mypy.patterns):
        for name, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, Node)
                and obj.__module__ in ("mypy.nodes", "mypy.patterns")
                and name not in _ABSTRACT
            ):
                found.add(obj)
    return found


def test_the_walker_knows_every_node_type_the_engine_can_produce():
    """The D1a upgrade check: a new mypy node type fails here, not silently downstream.

    `FINDINGS-mypy.md` §2 trap 1 forces a hand-rolled copy of `TraverserVisitor`'s child
    map (its compiled `@trait` cannot be subclassed), and the standing risk of a copy is
    that it goes stale. Total type coverage is what makes staleness loud.
    """
    composite, leaves = extract.node_types_covered()
    known = composite | leaves
    assert concrete_node_types() - known == set()


def test_every_child_accessor_reads_attributes_the_engine_actually_has():
    """The other half of the staleness check: an accessor naming a departed attribute.

    Total type coverage catches a node type mypy *adds*; this catches a child attribute
    mypy *renames or removes*, which is the quieter of the two failures — the walk would
    still run, and simply stop descending wherever the name went missing.
    """
    composite, _ = extract.node_types_covered()
    globals_of_extract = set(vars(extract))
    missing = [
        (node_type.__name__, name)
        for node_type in composite
        for name in extract._CHILDREN[node_type].__code__.co_names
        if name not in globals_of_extract and not hasattr(node_type, name)
    ]
    assert missing == []


def test_the_walk_never_leaves_the_module_it_was_given(tmp_path):
    """Structural children only: a `.node` pointer is not an edge the walk may follow.

    The fixture is the exact shape that would betray a walker reading semantic pointers:
    `caller.py` calls a function defined in `other.py`, so the `NameExpr` in the first
    module holds a `FuncDef` belonging to the second. Following it would attribute
    `other.py`'s code to `caller.py`.
    """
    outcome = build(
        tmp_path / "code",
        {
            "pkg/__init__.py": "",
            "pkg/other.py": "def helper() -> int:\n    return 1\n",
            "pkg/caller.py": "from pkg.other import helper\n\n\ndef top() -> int:\n"
            "    return helper()\n",
        },
    )

    def reachable(tree: MypyFile) -> dict[int, Node]:
        seen: dict[int, Node] = {}
        stack = [tree]
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen[id(node)] = node
            stack.extend(extract.children(node))
        return seen

    caller = reachable(outcome.tree("pkg/caller.py"))
    other = reachable(outcome.tree("pkg/other.py"))

    # The temptation exists: the caller really does hold a pointer into the other module.
    crossings = [
        node
        for node in caller.values()
        if isinstance(node, NameExpr) and isinstance(node.node, FuncDef) and id(node.node) in other
    ]
    assert crossings, "fixture no longer exercises a cross-module semantic pointer"
    # ... and the walk does not take it.
    assert set(caller) & set(other) == set()


def test_call_sites_attach_to_the_nearest_enclosing_executed_scope(tmp_path):
    """D16's attachment rule, as far as this task produces it (task 2.3 asserts the edges).

    The walk is what decides a call site's scope, so the shapes that make the rule
    non-obvious — an argument default and a decorator, both executed by the *enclosing*
    scope rather than by the function they belong to — are pinned here.
    """
    extraction = one(
        tmp_path,
        "import os\n"
        "\n"
        "TOP = os.getpid()\n"
        "\n"
        "\n"
        "@os.wraps(os.getcwd())\n"
        "def top(arg=os.getpid()):\n"
        "    return os.getpid()\n"
        "\n"
        "\n"
        "class C:\n"
        "    attr = os.getpid()\n"
        "\n"
        "    @os.wraps(os.getcwd())\n"
        "    def meth(self):\n"
        "        return os.getpid()\n",
    )
    scopes = {site.line: site.scope_id for site in extraction.call_sites}
    module_id = extraction.module_id
    assert scopes[3] == module_id  # a top-level statement
    assert scopes[6] == module_id  # a decorator on a top-level def
    assert scopes[7] == module_id  # an argument default of a top-level def
    assert scopes[8] == "python:pkg.m.top"  # the body
    assert scopes[12] == "python:pkg.m.C"  # a class-level statement
    assert scopes[14] == "python:pkg.m.C"  # a decorator on a method
    assert scopes[16] == "python:pkg.m.C.meth"  # the method body


def test_find_call_sites_sees_every_call_the_stdlib_ast_sees(tmp_path):
    """The walker's coverage surface, on shapes that hide calls from a careless walk."""
    import ast

    source = (
        "import os\n"
        "\n"
        "\n"
        "def top(arg=os.getpid()):\n"
        "    values = [os.getcwd() for _ in range(int(os.getpid()))]\n"
        "    mapping = {os.getcwd(): os.getpid()}\n"
        "    with open(os.getcwd()) as handle:\n"
        "        try:\n"
        "            print(handle.read())\n"
        "        except OSError(os.getcwd()):\n"
        "            pass\n"
        "    match os.getpid():\n"
        "        case int(x) if bool(os.getpid()):\n"
        "            pass\n"
        "    return values, mapping, (lambda: os.getpid())()\n"
    )
    outcome = build(tmp_path / "code", {"pkg/__init__.py": "", "pkg/m.py": source})
    found = extract.find_call_sites(outcome.tree("pkg/m.py"))
    expected = sum(isinstance(node, ast.Call) for node in ast.walk(ast.parse(source)))
    assert len(found) == expected
    assert all(isinstance(call, CallExpr) for call in found)


# ---------------------------------------------------------------------------
# Nodes and spans (FR-37)
# ---------------------------------------------------------------------------

SPAN_FIXTURE = """import functools


@functools.cache
def decorated(value):
    return value


class Outer:
    @property
    def method(self):
        def nested():
            return 1

        return nested

    class Inner:
        async def coroutine(self):
            return 2


async def top_level_coroutine():
    return 3


handler = lambda value: (
    value + 1
)
"""


def test_every_node_carries_its_path_and_span(tmp_path):
    """AC-37.1, against hand-counted lines: decorated defs, nested classes, async defs.

    A decorated definition's span starts at its `def`/`class` line, not at its first
    decorator — the convention stdlib `ast` uses, and the one mypy's own `line` reports.
    """
    extraction = one(tmp_path, SPAN_FIXTURE)
    spans = {node.id: (node.file_path, node.start_line, node.end_line) for node in extraction.nodes}
    assert spans["python:pkg.m.decorated"] == ("pkg/m.py", 5, 6)
    assert spans["python:pkg.m.Outer"] == ("pkg/m.py", 9, 19)
    assert spans["python:pkg.m.Outer.method"] == ("pkg/m.py", 11, 15)
    assert spans["python:pkg.m.Outer.method.nested"] == ("pkg/m.py", 12, 13)
    assert spans["python:pkg.m.Outer.Inner"] == ("pkg/m.py", 17, 19)
    assert spans["python:pkg.m.Outer.Inner.coroutine"] == ("pkg/m.py", 18, 19)
    assert spans["python:pkg.m.top_level_coroutine"] == ("pkg/m.py", 22, 23)
    # mypy leaves `end_line` unset on a lambda; the extent comes from its body.
    assert spans["python:pkg.m.<lambda#0>"] == ("pkg/m.py", 26, 27)
    assert extraction.diagnostics == ()


def test_the_module_body_node_spans_the_executed_body(tmp_path):
    """D16's module node covers line 1 through the last line its statements reach."""
    extraction = one(tmp_path, "x = 1\n\n\ndef f():\n    return x\n\n\n# a trailing comment\n")
    module = node_map(extraction)[extraction.module_id]
    assert (module.start_line, module.end_line) == (1, 5)


def test_an_empty_module_is_still_a_module_node(tmp_path):
    extraction = one(tmp_path, "")
    module = node_map(extraction)[extraction.module_id]
    assert module.kind == "module"
    assert (module.start_line, module.end_line) == (1, 1)


def test_a_node_without_a_determinable_span_keeps_its_path_and_is_reported(tmp_path):
    """AC-37.2/37.3: path only, a `span_missing` diagnostic, and never a fabricated span."""
    outcome = build(tmp_path / "code", {"pkg/__init__.py": "", "pkg/m.py": "def f():\n    pass\n"})
    tree = outcome.tree("pkg/m.py")
    tree.defs[0].line = -1  # the state mypy reports when it has no position
    extraction = extract.extract_file(
        outcome.source_for("pkg/m.py"), tree, extract.module_index(outcome.sources)
    )

    function = node_map(extraction)["python:pkg.m.f"]
    assert function.file_path == "pkg/m.py"
    assert function.start_line is None and function.end_line is None
    assert [(d.kind, d.path) for d in extraction.diagnostics] == [("span_missing", "pkg/m.py")]
    assert "pkg.m.f" in extraction.diagnostics[0].message


def test_two_indistinguishable_definitions_do_not_silently_collapse(tmp_path):
    """A duplicate ID that no span can break is dropped *and* recorded, never dropped."""
    outcome = build(
        tmp_path / "code",
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "if True:\n    def dup():\n        pass\nelse:\n    def dup():\n"
            "        pass\n",
        },
    )
    tree = outcome.tree("pkg/m.py")
    for statement in (tree.defs[0].body[0].body[0], tree.defs[0].else_body.body[0]):
        statement.line = -1
    extraction = extract.extract_file(
        outcome.source_for("pkg/m.py"), tree, extract.module_index(outcome.sources)
    )

    assert [node.id for node in extraction.nodes].count("python:pkg.m.dup") == 1
    messages = [d.message for d in extraction.diagnostics]
    assert any("could not be distinguished" in message for message in messages)


# ---------------------------------------------------------------------------
# IDs (FR-22, AC-22.1)
# ---------------------------------------------------------------------------


def test_every_emitted_id_is_a_namespaced_grammar_id(tmp_path):
    extraction = one(tmp_path, SPAN_FIXTURE)
    for node in extraction.nodes:
        assert node.id.startswith("python:")
        assert is_valid_node_id(node.id), node.id
        assert node.language == "python"
    for edge in extraction.edges:
        assert is_valid_node_id(edge.src) and is_valid_node_id(edge.dst)


def test_same_named_definitions_take_the_line_suffix(tmp_path):
    """§4.1's collision rule, on the three shapes that produce one in real code."""
    extraction = one(
        tmp_path,
        "from typing import overload\n"
        "\n"
        "if True:\n"
        "    def dup():\n"
        "        pass\n"
        "else:\n"
        "    def dup():\n"
        "        pass\n"
        "\n"
        "\n"
        "@overload\n"
        "def ov(x: int) -> int: ...\n"
        "@overload\n"
        "def ov(x: str) -> str: ...\n"
        "def ov(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "class C:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return 1\n"
        "\n"
        "    @value.setter\n"
        "    def value(self, new):\n"
        "        pass\n",
    )
    ids = {node.id for node in extraction.nodes}
    assert {"python:pkg.m.dup@4", "python:pkg.m.dup@7"} <= ids
    assert {"python:pkg.m.ov@12", "python:pkg.m.ov@14", "python:pkg.m.ov@15"} <= ids
    assert {"python:pkg.m.C.value@21", "python:pkg.m.C.value@25"} <= ids
    assert "python:pkg.m.dup" not in ids  # every member of the group is suffixed


def test_lambdas_are_numbered_per_scope_in_source_order(tmp_path):
    extraction = one(
        tmp_path,
        "first = lambda: 1\n"
        "second = lambda: 2\n"
        "\n"
        "\n"
        "def outer():\n"
        "    inner_first = lambda: 3\n"
        "    inner_second = lambda: 4\n"
        "    return inner_first, inner_second\n"
        "\n"
        "\n"
        "class C:\n"
        "    attr = lambda self: 5\n",
    )
    ids = {node.id for node in extraction.nodes}
    assert {"python:pkg.m.<lambda#0>", "python:pkg.m.<lambda#1>"} <= ids
    assert {"python:pkg.m.outer.<lambda#0>", "python:pkg.m.outer.<lambda#1>"} <= ids
    assert "python:pkg.m.C.<lambda#0>" in ids
    numbered = {node.id: node.start_line for node in extraction.nodes}
    assert numbered["python:pkg.m.<lambda#0>"] < numbered["python:pkg.m.<lambda#1>"]


def test_lambda_numbering_follows_position_not_visiting_order(tmp_path):
    """An assignment is visited rvalue-first; the IDs must not inherit that order.

    Numbering by walk order would give the *later* lambda `#0` here. An ID that depends on
    traversal order is one a mypy upgrade can renumber under an unchanged file, which is a
    determinism problem (FR-44) rather than a cosmetic one.
    """
    extraction = one(tmp_path, "store = {}\nstore[\n    lambda: 1\n] = lambda: 2\n")
    by_id = node_map(extraction)
    assert by_id["python:pkg.m.<lambda#0>"].start_line == 3
    assert by_id["python:pkg.m.<lambda#1>"].start_line == 4


# ---------------------------------------------------------------------------
# `contains` edges
# ---------------------------------------------------------------------------


def test_contains_edges_mirror_the_definition_nesting(tmp_path):
    """design.md §3.5: file→defs, class→methods — and, by the same rule, def→nested def."""
    extraction = one(
        tmp_path,
        "def top():\n"
        "    def nested():\n"
        "        pass\n"
        "    return nested\n"
        "\n"
        "\n"
        "class C:\n"
        "    def method(self):\n"
        "        pass\n"
        "\n"
        "    class Inner:\n"
        "        pass\n",
    )
    contains = edge_set(extraction, "contains")
    file_id = extraction.file_id
    assert contains == {
        (file_id, "python:pkg.m.<module>"),
        (file_id, "python:pkg.m.top"),
        (file_id, "python:pkg.m.C"),
        ("python:pkg.m.top", "python:pkg.m.top.nested"),
        ("python:pkg.m.C", "python:pkg.m.C.method"),
        ("python:pkg.m.C", "python:pkg.m.C.Inner"),
    }
    assert all(edge.src_file == "pkg/m.py" for edge in extraction.edges)


# ---------------------------------------------------------------------------
# `imports` edges
# ---------------------------------------------------------------------------


def test_imports_edges_appear_only_between_analyzed_files(tmp_path):
    """design.md §3.5: file→file, restricted to the analyzed set — nothing else."""
    outcome = build(
        tmp_path / "code",
        {
            "pkg/__init__.py": "",
            "pkg/leaf.py": "def helper() -> int:\n    return 1\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/deep.py": "VALUE = 2\n",
            "pkg/caller.py": (
                "import os\n"
                "import json.decoder\n"
                "from pkg.leaf import helper\n"
                "from pkg.sub import deep\n"
                "from . import leaf as also_leaf\n"
                "from pkg import caller\n"
                "\n"
                "\n"
                "def use():\n"
                "    return helper(), deep.VALUE, os.name, json.decoder, also_leaf, caller\n"
            ),
        },
    )
    extraction = extract_all(outcome)["pkg/caller.py"]
    imports = edge_set(extraction, "imports")
    assert imports == {
        ("python:file:pkg/caller.py", "python:file:pkg/__init__.py"),
        ("python:file:pkg/caller.py", "python:file:pkg/leaf.py"),
        ("python:file:pkg/caller.py", "python:file:pkg/sub/__init__.py"),
        ("python:file:pkg/caller.py", "python:file:pkg/sub/deep.py"),
    }
    # Everything else the file imports — `os`, `json.decoder`, and itself — is either
    # outside the analyzed set or not a dependency of itself.
    analyzed = {f"python:file:{relpath}" for relpath in extract_all(outcome)}
    assert {dst for _, dst in imports} <= analyzed - {"python:file:pkg/caller.py"}


def test_a_relative_import_from_a_deeper_package_resolves_upwards(tmp_path):
    outcome = build(
        tmp_path / "code",
        {
            "pkg/__init__.py": "",
            "pkg/shared.py": "SHARED = 1\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/mod.py": "from ..shared import SHARED\n\n\ndef use():\n    return SHARED\n",
        },
    )
    extraction = extract_all(outcome)["pkg/sub/mod.py"]
    assert ("python:file:pkg/sub/mod.py", "python:file:pkg/shared.py") in edge_set(
        extraction, "imports"
    )


def test_a_single_file_analysis_has_no_imports_edges(tmp_path):
    extraction = one(tmp_path, "import os\n\n\ndef f():\n    return os.name\n")
    assert edge_set(extraction, "imports") == set()


# ---------------------------------------------------------------------------
# The module node, the file node, and the store (D16, §4.2)
# ---------------------------------------------------------------------------


def test_exactly_one_module_node_per_analyzed_file(tmp_path):
    outcome = build(
        tmp_path / "code",
        {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n", "pkg/b.py": "def f():\n    pass\n"},
    )
    for relpath, extraction in extract_all(outcome).items():
        modules = [node for node in extraction.nodes if node.kind == "module"]
        assert len(modules) == 1, relpath
        assert modules[0].id == extraction.module_id
        assert modules[0].id.endswith(".<module>")
        assert modules[0].attrs == {"python_role": "module_body"}
        assert modules[0].kind in NODE_KINDS
        files = [node for node in extraction.nodes if node.kind == "file"]
        assert [node.id for node in files] == [extraction.file_id]


def test_no_file_node_carries_an_outgoing_calls_edge(tmp_path):
    """A file is a namespace, not an executed scope: its edges are `contains`/`imports`."""
    outcome = build(
        tmp_path / "code",
        {"pkg/__init__.py": "", "pkg/m.py": "import os\n\n\nprint(os.name)\n"},
    )
    extraction = extract_all(outcome)["pkg/m.py"]
    file_ids = {node.id for node in extraction.nodes if node.kind == "file"}
    assert not [edge for edge in extraction.edges if edge.kind == "calls"]
    assert not [
        edge
        for edge in extraction.edges
        if edge.src in file_ids and edge.kind not in ("contains", "imports")
    ]
    # The module-level call site is attached to the module node, not to the file (D16).
    assert {site.scope_id for site in extraction.call_sites} == {extraction.module_id}


def test_extracted_rows_pass_validation_and_reach_the_index(tmp_path):
    """AC-23.2 / §4.2: what the walk emits is what the store accepts, CHECK sets included."""
    outcome = build(
        tmp_path / "code",
        {
            "pkg/__init__.py": "",
            "pkg/leaf.py": "def helper():\n    return 1\n",
            "pkg/m.py": "from pkg.leaf import helper\n\n\nclass C:\n    def method(self):\n"
            "        return helper()\n",
        },
    )
    extractions = extract_all(outcome)
    fragments = [
        GraphFragment(
            file=FileRecord(path=relpath, content_hash="0" * 64, status="analyzed"),
            nodes=list(extraction.nodes),
            edges=list(extraction.edges),
        )
        for relpath, extraction in sorted(extractions.items())
    ]
    known = {node.id for extraction in extractions.values() for node in extraction.nodes}
    for fragment in fragments:
        validate_fragment(fragment, known_ids=known)

    path = tmp_path / "index.sqlite"
    with full_write(path, META) as index:
        index.write_fragments(fragments)

    with open_index(path, read_only=True) as index:
        rows = index.connection.execute("SELECT kind, count(*) FROM nodes GROUP BY kind").fetchall()
        kinds = {kind for kind, _ in rows}
        assert kinds <= set(NODE_KINDS)
        assert dict(rows)["module"] == 3
        edge_kinds = {
            row[0] for row in index.connection.execute("SELECT DISTINCT kind FROM edges").fetchall()
        }
        assert edge_kinds == {"contains", "imports"}


def test_extraction_is_stable_across_repeated_walks(tmp_path):
    """FR-44's write-side promise starts here: the same tree yields the same rows."""
    outcome = build(tmp_path / "code", {"pkg/__init__.py": "", "pkg/m.py": SPAN_FIXTURE})
    index = extract.module_index(outcome.sources)
    source = outcome.source_for("pkg/m.py")
    tree = outcome.tree("pkg/m.py")
    first = extract.extract_file(source, tree, index)
    second = extract.extract_file(source, tree, index)
    assert first.nodes == second.nodes
    assert first.edges == second.edges
    assert first.diagnostics == second.diagnostics


@pytest.mark.parametrize(
    ("relpath", "module_id"),
    [
        ("pkg/migrations/0001_initial.py", "python:pkg.migrations.0001_initial.<module>"),
        ("pkg/locale/is/formats.py", "python:pkg.locale.is.formats.<module>"),
    ],
)
def test_path_derived_module_names_survive_extraction(tmp_path, relpath, module_id):
    """D22, on the two shapes the pinned Django benchmark actually contains."""
    package_inits = {
        f"{parent}/__init__.py": ""
        for parent in ("pkg", *[str(Path(relpath).parent)])
        if parent != "."
    }
    outcome = build(tmp_path / "code", {**package_inits, relpath: "def f():\n    return 1\n"})
    assert outcome.skipped == ()
    extraction = extract_all(outcome)[relpath]
    assert extraction.module_id == module_id
    assert is_valid_node_id(extraction.module_id)
