"""Slices, reachability, and dead code (specs/tasks.md task 3.4).

design.md §3.9, §5.3, D5, D16, D19 (as amended 2026-07-23), §8-O2; requirements FR-15
(AC-15.1/15.2), FR-16 (AC-16.1/16.2), FR-17 (AC-17.1/17.2), FR-18 (AC-18.1/18.2), FR-19
(AC-19.1-19.3), FR-28 (AC-28.2), FR-36 (AC-36.3), EC-6, EC-9.

Most assertions here run against hand-built indexes: a slice is a claim about a graph, and
a graph written by hand is one whose expected answer can be computed by eye and asserted as
an exact set (AC-15.1 asks for exactly that). One module-scoped test at the end drives the
real pipeline — engine, detectors, reachability — because AC-18.1 is a claim about what is
in the index *after `analyze`*, which no hand-built fixture can make.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
from pathlib import Path

import pytest

from conftest import write_tree
from pastapathfinder import queries, reports, runner
from pastapathfinder.index import INDEX_FILENAME, full_write, open_index
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import DEADCODE_CAVEAT, EdgeRow, FileRecord, GraphFragment, NodeRow

META = {
    "tool_version": "0.1.0",
    "engine": "stub",
    "engine_version": "0",
    "root_path": "/srv/target",
    "created_at": "2026-07-23T09:00:00+00:00",
    "run_id": "33333333-4444-5555-6666-777777777777",
}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fixture construction
# ---------------------------------------------------------------------------


def function(qualname: str, relpath: str, line: int = 1) -> NodeRow:
    return NodeRow(
        id=f"python:{qualname}",
        kind="function",
        name=qualname.rpartition(".")[2],
        language="python",
        file_path=relpath,
        start_line=line,
        end_line=line + 1,
    )


def klass(qualname: str, relpath: str, line: int = 1) -> NodeRow:
    return NodeRow(
        id=f"python:{qualname}",
        kind="class",
        name=qualname.rpartition(".")[2],
        language="python",
        file_path=relpath,
        start_line=line,
        end_line=line + 5,
    )


def module_body(module: str, relpath: str) -> NodeRow:
    return NodeRow(
        id=f"python:{module}.<module>",
        kind="module",
        name=module,
        language="python",
        file_path=relpath,
        start_line=1,
        end_line=99,
        attrs={"python_role": "module_body"},
    )


def file_node(relpath: str) -> NodeRow:
    return NodeRow(
        id=f"python:file:{relpath}",
        kind="file",
        name=relpath,
        language="python",
        file_path=relpath,
    )


def external(qualified: str) -> NodeRow:
    return NodeRow(
        id=f"python:{qualified}",
        kind="function",
        name=qualified.rpartition(".")[2],
        language="python",
        is_external=1,
    )


def entry(detector: str, qualname: str, line: int, relpath: str) -> NodeRow:
    return NodeRow(
        id=f"python:entry:{detector}:{qualname}@{line}",
        kind="entry_point",
        name=qualname,
        language="python",
        file_path=relpath,
        start_line=line,
        end_line=line,
        attrs={"detector": detector},
    )


def calls(src: NodeRow, dst: NodeRow, relpath: str | None = None) -> EdgeRow:
    return EdgeRow(src=src.id, dst=dst.id, kind="calls", src_file=relpath)


def contains(src: NodeRow, dst: NodeRow, relpath: str) -> EdgeRow:
    return EdgeRow(src=src.id, dst=dst.id, kind="contains", src_file=relpath)


def fragment(relpath: str, nodes: list[NodeRow], edges: list[EdgeRow]) -> GraphFragment:
    return GraphFragment(
        file=FileRecord(relpath, digest(relpath), "analyzed"), nodes=nodes, edges=edges
    )


def build(
    path: Path,
    fragments: list[GraphFragment],
    entry_nodes: list[NodeRow] | None = None,
    entry_edges: list[EdgeRow] | None = None,
):
    """Write an index and open it — the store's own write path, entry rows included."""
    with full_write(path, META) as store:
        store.write_fragments(fragments)
        store.write_rows(entry_nodes or [], entry_edges or [])
    return open_index(path)


# ---------------------------------------------------------------------------
# The slice fixture
# ---------------------------------------------------------------------------

APP = "pkg/app.py"

MODULE = module_body("pkg.app", APP)
FILE = file_node(APP)
A = function("pkg.app.a", APP, 10)
B = function("pkg.app.b", APP, 20)
C = function("pkg.app.c", APP, 30)
D = function("pkg.app.d", APP, 40)
E = function("pkg.app.e", APP, 50)
LONE = function("pkg.app.lone", APP, 60)
IMPORT_TIME = function("pkg.app.import_time", APP, 70)
EXT = external("os.path.join")


@pytest.fixture
def graph(tmp_path: Path):
    """A small call graph whose every slice can be computed by eye.

    e → a → b → c → os.path.join(external)
        a → d → c
    <module> → import_time → c
    lone: no call edges at all
    """
    nodes = [FILE, MODULE, A, B, C, D, E, LONE, IMPORT_TIME, EXT]
    edges = [
        contains(FILE, MODULE, APP),
        *(contains(FILE, node, APP) for node in (A, B, C, D, E, LONE, IMPORT_TIME)),
        calls(E, A, APP),
        calls(A, B, APP),
        calls(A, D, APP),
        calls(B, C, APP),
        calls(D, C, APP),
        calls(C, EXT, APP),
        calls(MODULE, IMPORT_TIME, APP),
        calls(IMPORT_TIME, C, APP),
    ]
    with build(tmp_path / INDEX_FILENAME, [fragment(APP, nodes, edges)]) as index:
        yield index


def ids(result: queries.SliceResult) -> set[str]:
    return {node.id for node in result.nodes}


def pairs(result: queries.SliceResult) -> set[tuple[str, str]]:
    return {(edge.src, edge.dst) for edge in result.edges}


# ---------------------------------------------------------------------------
# Forward slices (FR-15)
# ---------------------------------------------------------------------------


def test_a_forward_slice_is_the_reachable_subgraph_and_not_the_whole_graph(graph):
    """AC-15.1: exactly the transitively reachable nodes and the connecting edges."""
    result = queries.slice(graph, A.id, queries.FORWARD)

    assert ids(result) == {A.id, B.id, C.id, D.id, EXT.id}
    assert pairs(result) == {
        (A.id, B.id),
        (A.id, D.id),
        (B.id, C.id),
        (D.id, C.id),
        (C.id, EXT.id),
    }
    # E calls A, and lone/import_time/<module> are elsewhere in the graph: a forward slice
    # is not the program.
    assert {E.id, LONE.id, IMPORT_TIME.id, MODULE.id} & ids(result) == set()
    assert result.truncated is False
    assert result.frontier == []


def test_a_node_with_no_outgoing_calls_returns_an_empty_slice_not_an_error(graph):
    """AC-15.2: an empty result, presented as such."""
    result = queries.slice(graph, LONE.id, queries.FORWARD)
    assert ids(result) == {LONE.id}
    assert result.edges == []
    assert result.truncated is False


def test_a_slice_reaching_an_external_node_includes_it_as_a_terminus(graph):
    """AC-36.3: the external leaf appears in the slice, and nothing continues past it."""
    result = queries.slice(graph, C.id, queries.FORWARD)
    assert ids(result) == {C.id, EXT.id}
    (leaf,) = [node for node in result.nodes if node.is_external]
    assert leaf.id == EXT.id
    assert leaf.file_path is None  # AC-37.2: no span on an external node
    assert [edge for edge in result.edges if edge.src == EXT.id] == []


# ---------------------------------------------------------------------------
# Backward slices (FR-16)
# ---------------------------------------------------------------------------


def test_a_backward_slice_contains_the_transitive_callers(graph):
    """AC-16.1: everything from which the selected node is reachable."""
    result = queries.slice(graph, C.id, queries.BACKWARD)
    assert ids(result) == {C.id, B.id, D.id, A.id, E.id, IMPORT_TIME.id, MODULE.id}
    assert pairs(result) == {
        (B.id, C.id),
        (D.id, C.id),
        (A.id, B.id),
        (A.id, D.id),
        (E.id, A.id),
        (IMPORT_TIME.id, C.id),
        (MODULE.id, IMPORT_TIME.id),
    }
    # The external leaf is downstream of C, so it is not one of C's callers.
    assert EXT.id not in ids(result)


def test_an_unknown_identifier_is_an_error_naming_it(graph):
    """AC-16.2: the query names the identifier it could not find."""
    with pytest.raises(queries.UnknownNodeError) as raised:
        queries.slice(graph, "python:pkg.app.ghost", queries.BACKWARD)
    assert "python:pkg.app.ghost" in str(raised.value)


# ---------------------------------------------------------------------------
# Slice origins (FR-17, D16)
# ---------------------------------------------------------------------------


def test_an_arbitrary_non_entry_point_function_slices_in_both_directions(graph):
    """AC-17.1: any function is a slice origin, not only a detected entry point."""
    forward = queries.slice(graph, B.id, queries.FORWARD)
    backward = queries.slice(graph, B.id, queries.BACKWARD)
    assert ids(forward) == {B.id, C.id, EXT.id}
    assert ids(backward) == {B.id, A.id, E.id}


def test_a_file_node_is_not_sliceable_and_the_error_names_the_kind(graph):
    """AC-17.2: not an empty result — an error stating the node type is not sliceable."""
    with pytest.raises(queries.NotSliceableError) as raised:
        queries.slice(graph, FILE.id, queries.FORWARD)
    message = str(raised.value)
    assert FILE.id in message
    assert "'file'" in message
    assert queries.SLICEABLE_KINDS == ("entry_point", "function", "class", "module")


def test_a_forward_slice_from_a_module_node_is_its_import_time_call_chain(graph):
    """D16: the module body is sliceable, and what it reaches is what import executes."""
    result = queries.slice(graph, MODULE.id, queries.FORWARD)
    assert ids(result) == {MODULE.id, IMPORT_TIME.id, C.id, EXT.id}


# ---------------------------------------------------------------------------
# Bounding (FR-28, AC-28.2, §8-O2)
# ---------------------------------------------------------------------------


def test_a_slice_over_budget_is_truncated_visibly_with_a_frontier(graph):
    """AC-28.2: bounded and *visible* — never silently trimmed."""
    result = queries.slice(graph, A.id, queries.FORWARD, max_nodes=3)
    # BFS order is (depth, id): a, then b and d, then c, then the external leaf.
    assert [node.id for node in result.nodes] == [A.id, B.id, D.id]
    assert result.truncated is True
    assert result.frontier == [C.id]
    # Edges are those among the admitted nodes; the ones crossing the boundary are not
    # presented as if their target were in view.
    assert pairs(result) == {(A.id, B.id), (A.id, D.id)}


def test_the_default_bound_truncates_a_large_graph(tmp_path):
    """The shipped default is exercised, not just the parameter (AC-28.2, §8-O2)."""
    root = function("pkg.wide.root", "pkg/wide.py", 1)
    leaves = [function(f"pkg.wide.leaf{n:03d}", "pkg/wide.py", n + 2) for n in range(250)]
    nodes = [file_node("pkg/wide.py"), root, *leaves]
    edges = [calls(root, leaf, "pkg/wide.py") for leaf in leaves]
    with build(tmp_path / INDEX_FILENAME, [fragment("pkg/wide.py", nodes, edges)]) as index:
        result = queries.slice(index, root.id, queries.FORWARD)
    assert len(result.nodes) == queries.SLICE_MAX_NODES == 200
    assert result.truncated is True
    assert len(result.frontier) == 51  # the 250 leaves, less the 199 admitted


def test_the_slice_bound_has_a_single_definition_site():
    """§8-O2: provisional, so it must be one named constant — one edit when OQ-4 lands."""
    assert inspect.signature(queries.slice).parameters["max_nodes"].default == (
        queries.SLICE_MAX_NODES
    )
    source = Path(queries.__file__).read_text(encoding="utf-8")
    assert source.count("SLICE_MAX_NODES = ") == 1
    assert source.count("200") == 1  # the definition, and nowhere else


def test_a_meaningless_budget_or_direction_is_rejected(graph):
    with pytest.raises(ValueError, match="max_nodes"):
        queries.slice(graph, A.id, queries.FORWARD, max_nodes=0)
    with pytest.raises(ValueError, match="direction"):
        queries.slice(graph, A.id, "sideways")


def test_repeated_slices_return_identical_ordering(graph):
    """Determinism: the same question answered twice is the same answer, in order."""
    first = queries.slice(graph, C.id, queries.BACKWARD)
    second = queries.slice(graph, C.id, queries.BACKWARD)
    assert [node.id for node in first.nodes] == [node.id for node in second.nodes]
    assert [(edge.src, edge.dst) for edge in first.edges] == [
        (edge.src, edge.dst) for edge in second.edges
    ]
    assert first.frontier == second.frontier


def test_slice_ordering_does_not_depend_on_insertion_order(tmp_path):
    """FR-44's discipline on the read side: the graph, not the write order, decides."""
    nodes = [file_node(APP), A, B, C, D]
    edges = [calls(A, B, APP), calls(A, D, APP), calls(B, C, APP), calls(D, C, APP)]
    forward = build(tmp_path / "forward.sqlite", [fragment(APP, nodes, edges)])
    reversed_ = build(
        tmp_path / "reversed.sqlite", [fragment(APP, list(reversed(nodes)), list(reversed(edges)))]
    )
    with forward, reversed_:
        one = queries.slice(forward, A.id, queries.FORWARD)
        two = queries.slice(reversed_, A.id, queries.FORWARD)
    assert [node.id for node in one.nodes] == [node.id for node in two.nodes]
    assert [(edge.src, edge.dst) for edge in one.edges] == [
        (edge.src, edge.dst) for edge in two.edges
    ]


# ---------------------------------------------------------------------------
# The reachability fixture (FR-18, D19)
# ---------------------------------------------------------------------------

APP2 = "pkg/app.py"
LIB = "pkg/lib.py"
DEAD = "pkg/dead.py"
VIEWS = "pkg/views.py"
INHERIT = "pkg/inherit.py"

R_FILE = file_node(APP2)
R_MODULE = module_body("pkg.app", APP2)
R_MAIN = function("pkg.app.main", APP2, 10)
R_HELPER = function("pkg.app.helper", APP2, 20)
R_USED = klass("pkg.app.Used", APP2, 30)
R_USED_METHOD = function("pkg.app.Used.method", APP2, 31)
R_UNUSED = klass("pkg.app.Unused", APP2, 40)
R_UNUSED_METHOD = function("pkg.app.Unused.method", APP2, 41)
R_ORPHAN = function("pkg.app.orphan", APP2, 50)
R_ENTRY = entry("main_block", "pkg.app", 60, APP2)

L_FILE = file_node(LIB)
L_MODULE = module_body("pkg.lib", LIB)
L_FN = function("pkg.lib.lib_fn", LIB, 1)

D_FILE = file_node(DEAD)
D_MODULE = module_body("pkg.dead", DEAD)
D_FN = function("pkg.dead.never", DEAD, 1)

V_FILE = file_node(VIEWS)
V_MODULE = module_body("pkg.views", VIEWS)
V_CLASS = klass("pkg.views.FooView", VIEWS, 3)
V_GET = function("pkg.views.FooView.get", VIEWS, 4)
V_ENTRY = entry("route_django", "pkg.views.FooView", 7, "pkg/urls.py")

I_FILE = file_node(INHERIT)
I_MODULE = module_body("pkg.inherit", INHERIT)
I_BASE = klass("pkg.inherit.Base", INHERIT, 1)
I_BASE_INIT = function("pkg.inherit.Base.__init__", INHERIT, 2)
I_CHILD = klass("pkg.inherit.Child", INHERIT, 10)


@pytest.fixture
def marked(tmp_path: Path):
    """An index with reachability already computed over five files.

    Entry points: a `__main__` guard on `pkg/app.py`, and a Django route pointing straight
    at `pkg.views.FooView` (the D18/§3.7 `X.as_view()` shape).
    """
    app = fragment(
        APP2,
        [
            R_FILE,
            R_MODULE,
            R_MAIN,
            R_HELPER,
            R_USED,
            R_USED_METHOD,
            R_UNUSED,
            R_UNUSED_METHOD,
            R_ORPHAN,
        ],
        [
            contains(R_FILE, R_MODULE, APP2),
            contains(R_FILE, R_MAIN, APP2),
            contains(R_FILE, R_HELPER, APP2),
            contains(R_FILE, R_USED, APP2),
            contains(R_USED, R_USED_METHOD, APP2),
            contains(R_FILE, R_UNUSED, APP2),
            contains(R_UNUSED, R_UNUSED_METHOD, APP2),
            contains(R_FILE, R_ORPHAN, APP2),
            calls(R_MODULE, R_MAIN, APP2),
            calls(R_MAIN, R_HELPER, APP2),
            calls(R_HELPER, R_USED_METHOD, APP2),
        ],
    )
    lib = fragment(
        LIB,
        [L_FILE, L_MODULE, L_FN],
        [contains(L_FILE, L_MODULE, LIB), contains(L_FILE, L_FN, LIB)],
    )
    dead = fragment(
        DEAD,
        [D_FILE, D_MODULE, D_FN],
        [contains(D_FILE, D_MODULE, DEAD), contains(D_FILE, D_FN, DEAD)],
    )
    views = fragment(
        VIEWS,
        [V_FILE, V_MODULE, V_CLASS, V_GET],
        [
            contains(V_FILE, V_MODULE, VIEWS),
            contains(V_FILE, V_CLASS, VIEWS),
            contains(V_CLASS, V_GET, VIEWS),
        ],
    )
    inherit = fragment(
        INHERIT,
        [I_FILE, I_MODULE, I_BASE, I_BASE_INIT, I_CHILD],
        [
            contains(I_FILE, I_MODULE, INHERIT),
            contains(I_FILE, I_BASE, INHERIT),
            contains(I_BASE, I_BASE_INIT, INHERIT),
            contains(I_FILE, I_CHILD, INHERIT),
        ],
    )
    # `pkg.app.main` calls into the library and constructs a Child, whose `__init__` the
    # §3.5 ladder resolves through the MRO onto `Base.__init__`.
    cross = [calls(R_MAIN, L_FN, APP2), calls(R_MAIN, I_BASE_INIT, APP2)]
    app = GraphFragment(file=app.file, nodes=app.nodes, edges=[*app.edges, *cross])

    with build(
        tmp_path / INDEX_FILENAME,
        [app, lib, dead, views, inherit],
        entry_nodes=[R_ENTRY, V_ENTRY],
        entry_edges=[
            EdgeRow(src=R_ENTRY.id, dst=R_MODULE.id, kind="calls"),
            EdgeRow(src=V_ENTRY.id, dst=V_CLASS.id, kind="calls"),
        ],
    ) as index:
        result = queries.reachability(index)
        yield index, result


def reachable(index, node_id: str):
    return queries.node(index, node_id).reachable


def test_functions_called_from_an_entry_point_are_marked_reachable(marked):
    """AC-18.1, on the BFS half: transitively called functions carry 1, others 0."""
    index, result = marked
    assert reachable(index, R_MAIN.id) == 1
    assert reachable(index, R_HELPER.id) == 1
    assert reachable(index, R_USED_METHOD.id) == 1
    assert reachable(index, L_FN.id) == 1
    assert reachable(index, R_ORPHAN.id) == 0
    assert reachable(index, R_UNUSED_METHOD.id) == 0
    assert reachable(index, D_FN.id) == 0
    assert result.entry_points == 2
    assert result.no_entry_points is False


def test_class_reachability_is_derived_from_the_functions_it_contains(marked):
    """D19: a class with a reachable method is 1; a class with none is 0."""
    index, _ = marked
    assert reachable(index, R_USED.id) == 1
    assert reachable(index, R_UNUSED.id) == 0


def test_a_class_reached_only_through_an_inherited_init_reads_unreachable(marked):
    """D19's accepted imprecision, asserted rather than treated as a bug.

    `Child()` resolves through the MRO onto `Base.__init__`, so `Base` derives reachable
    and `Child` — which contains nothing reachable — does not. It under-claims, which is
    the FR-19 posture.
    """
    index, _ = marked
    assert reachable(index, I_BASE.id) == 1
    assert reachable(index, I_CHILD.id) == 0


def test_a_class_an_entry_point_targets_directly_stays_reachable(marked):
    """D19 as amended 2026-07-23: the derivation is a union with the BFS, not a replacement.

    A Django `X.as_view()` route puts a `calls` edge straight onto the class node. Its
    methods are dispatched by the framework and stay unreachable — the ordinary FR-19
    approximation — but the class an entry point points at must not read as dead.
    """
    index, _ = marked
    assert reachable(index, V_CLASS.id) == 1
    assert reachable(index, V_GET.id) == 0


def test_module_reachability_derives_from_the_owning_files_contains_edges(marked):
    """D19 as amended 2026-07-23: clause (a).

    `pkg/lib.py` has no entry point and nothing calls its module body — only `imports`
    edges lead to it, and reachability does not traverse those. It is reachable because a
    function its file holds is: for `lib_fn` to run, the module was imported.
    """
    index, _ = marked
    assert reachable(index, R_MODULE.id) == 1  # the entry-point target (BFS)
    assert reachable(index, L_MODULE.id) == 1  # derived from `pkg.lib.lib_fn`
    assert reachable(index, D_MODULE.id) == 0  # nothing in the file is reachable
    # The accepted under-claim of the same clause: `pkg/views.py` holds a reachable
    # *class*, not a reachable function, so its module body stays 0.
    assert reachable(index, V_MODULE.id) == 0


def test_other_kinds_keep_a_null_reachability(marked):
    """§4.2: `reachable` is 0/1 on function, class and module only — else NULL."""
    index, _ = marked
    assert reachable(index, R_FILE.id) is None
    assert reachable(index, L_FILE.id) is None
    assert reachable(index, R_ENTRY.id) is None
    assert queries.REACHABLE_KINDS == ("function", "class", "module")


def test_recomputing_reachability_clears_what_the_previous_graph_said(marked):
    """A second pass is a fresh answer, not an accumulation over a graph that is gone."""
    index, first = marked
    index.connection.execute(
        "DELETE FROM edges WHERE kind = 'calls' AND src = ? AND dst = ?",
        (
            R_MAIN.id,
            R_HELPER.id,
        ),
    )
    second = queries.reachability(index)
    assert reachable(index, R_HELPER.id) == 0
    assert reachable(index, R_USED.id) == 0  # its only reachable method went with it
    assert second.reachable_functions < first.reachable_functions


# ---------------------------------------------------------------------------
# Dead code (FR-19)
# ---------------------------------------------------------------------------


def test_unreachable_functions_are_grouped_by_file_with_the_caveat(marked):
    """AC-19.1/19.2: grouped by file, and never presented without the caveat."""
    index, _ = marked
    result = queries.dead_code(index)

    assert [group.file for group in result.unreachable] == [APP2, DEAD, VIEWS]
    by_file = {
        group.file: [function.name for function in group.functions] for group in result.unreachable
    }
    assert by_file[APP2] == ["method", "orphan"]  # (start_line, id) order within the file
    assert by_file[DEAD] == ["never"]
    assert by_file[VIEWS] == ["get"]
    assert result.caveat == DEADCODE_CAVEAT
    assert result.no_entry_points_warning is False


def test_module_nodes_never_appear_in_the_dead_code_report(marked):
    """D16: `dead_code()` is `function`-only, while `reachable` is still stored on modules."""
    index, _ = marked
    result = queries.dead_code(index)
    reported = {function.id for group in result.unreachable for function in group.functions}
    assert D_MODULE.id not in reported
    assert not any(identifier.endswith(".<module>") for identifier in reported)
    # The unreachable module body is absent from the report but present in the index.
    assert reachable(index, D_MODULE.id) == 0


def test_external_leaves_are_not_reported_as_dead_code(tmp_path):
    """FR-36: their internals are deliberately unanalyzed, and they have no file to group under."""
    caller = function("pkg.app.caller", APP, 1)
    nodes = [file_node(APP), caller, EXT]
    edges = [contains(file_node(APP), caller, APP), calls(caller, EXT, APP)]
    with build(tmp_path / INDEX_FILENAME, [fragment(APP, nodes, edges)]) as index:
        queries.reachability(index)
        result = queries.dead_code(index)
    reported = {function.id for group in result.unreachable for function in group.functions}
    assert reported == {caller.id}
    assert EXT.id not in reported


def test_with_no_entry_points_reachability_is_still_computed_and_flagged(tmp_path):
    """AC-18.2/19.3, EC-9: the library case — computed, and explicitly qualified."""
    lone = function("pkg.lib.thing", LIB, 1)
    nodes = [file_node(LIB), module_body("pkg.lib", LIB), lone]
    edges = [contains(file_node(LIB), lone, LIB)]
    with build(tmp_path / INDEX_FILENAME, [fragment(LIB, nodes, edges)]) as index:
        result = queries.reachability(index)
        dead = queries.dead_code(index)
        assert result.no_entry_points is True
        assert result.entry_points == 0
        # Computed, not skipped: the answer is a written 0, not an absent value.
        assert reachable(index, lone.id) == 0
    assert dead.no_entry_points_warning is True
    assert [group.file for group in dead.unreachable] == [LIB]


# ---------------------------------------------------------------------------
# Run integration (the deliverable's second half)
# ---------------------------------------------------------------------------

RUN_TREE = {
    "pkg/__init__.py": "",
    "pkg/util.py": ("def helper():\n    return 1\n\n\ndef never_called():\n    return 2\n"),
    "pkg/app.py": (
        "from pkg.util import helper\n"
        "\n"
        "\n"
        "def main():\n"
        "    return helper()\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    ),
}


@pytest.fixture(scope="module")
def analyzed_run(tmp_path_factory):
    """One real `analyze` — engine, detectors, reachability — shared by the tests below.

    Module-scoped because it drives a cold engine build; the assertions after it only read
    what that run produced.
    """
    base = tmp_path_factory.mktemp("run")
    root = write_tree(base / "codebase", RUN_TREE)
    out = base / "out"
    out.mkdir()
    stdout = io.StringIO()
    result = runner.run_analysis(
        root,
        out=out,
        progress=ProgressSink(io.StringIO(), interval=0.0),
        stdout=stdout,
    )
    return result, stdout.getvalue()


def test_reachability_is_written_to_the_index_by_analyze(analyzed_run):
    """AC-18.1: after `analyze`, functions transitively called from an entry point are 1."""
    result, _ = analyzed_run
    with open_index(result.index_path) as index:
        entries = index.connection.execute(
            "SELECT id FROM nodes WHERE kind = 'entry_point'"
        ).fetchall()
        assert [row[0] for row in entries] == ["python:entry:main_block:pkg.app@8"]

        assert reachable(index, "python:pkg.app.main") == 1
        assert reachable(index, "python:pkg.util.helper") == 1
        assert reachable(index, "python:pkg.util.never_called") == 0
        # D19 clause (a) end to end: `pkg/util.py`'s body is reachable because a function
        # its file holds is, though no `calls` edge leads to it.
        assert reachable(index, "python:pkg.util.<module>") == 1
        assert reachable(index, "python:pkg.app.<module>") == 1


def test_the_flagship_slice_answers_from_the_written_index(analyzed_run):
    """FR-15 over real extraction: the entry point traces to the far end of the chain."""
    result, _ = analyzed_run
    with open_index(result.index_path) as index:
        traced = queries.slice(index, "python:entry:main_block:pkg.app@8", queries.FORWARD)
    assert {"python:pkg.app.<module>", "python:pkg.app.main", "python:pkg.util.helper"} <= ids(
        traced
    )
    assert "python:pkg.util.never_called" not in ids(traced)


def test_the_run_writes_the_dead_code_report_with_its_caveat(analyzed_run):
    """AC-19.1/19.2: `deadcode.json` is populated by the runner, caveat verbatim in both forms."""
    result, stdout = analyzed_run
    document = json.loads(result.report_paths[reports.DEADCODE_REPORT].read_text(encoding="utf-8"))
    assert document["no_entry_points_warning"] is False
    assert document["unreachable"] == [
        {
            "file": "pkg/util.py",
            "functions": [
                {"id": "python:pkg.util.never_called", "name": "never_called", "start_line": 5}
            ],
        }
    ]
    # AC-19.2: verbatim in the structured artifact *and* in the rendering built from it.
    assert document["caveat"] == DEADCODE_CAVEAT
    assert DEADCODE_CAVEAT in stdout


def test_the_run_warns_explicitly_when_no_entry_points_were_detected(tree, out_dir):
    """AC-18.2/19.3: the run says reachability is uninformative — not that the code is dead."""
    root = tree({"pkg/lib.py": "def thing():\n    return 1\n"})
    stdout = io.StringIO()
    result = runner.run_analysis(
        root,
        out=out_dir,
        progress=ProgressSink(io.StringIO(), interval=0.0),
        stdout=stdout,
    )
    document = json.loads(result.report_paths[reports.DEADCODE_REPORT].read_text(encoding="utf-8"))
    assert document["no_entry_points_warning"] is True
    assert "no entry points were detected" in stdout.getvalue()
    assert DEADCODE_CAVEAT in stdout.getvalue()
