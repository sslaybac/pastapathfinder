"""The call-resolution ladder, its ambiguity flags, and its unresolved diagnostics.

design.md §3.5 (`extract` — the ladder, normative), §4.2, §4.3, D1, D15, D16, R3;
requirements FR-12 (AC-12.1, AC-12.2), FR-14 (AC-14.1, AC-14.2), FR-40 (AC-40.1, AC-40.2).

As in `test_extract.py`, every fixture drives the real engine over a real tree: what mypy
2.3.0 does with a union receiver or an inherited `__init__` is the subject under test, and
a hand-built type double would only test the double.
"""

import io
from pathlib import Path

from pastapathfinder.adapters.base import SourceFile
from pastapathfinder.adapters.python import extract
from pastapathfinder.adapters.python.mypy_driver import run_build
from pastapathfinder.index import full_write, open_index
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import (
    FileRecord,
    GraphFragment,
    NodeRow,
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


class Analysis:
    """One built tree, extracted and resolved — the shape every test works from."""

    def __init__(self, outcome, extractions, resolutions):
        self.outcome = outcome
        self.extractions = extractions
        self.resolutions = resolutions

    def edges(self, relpath: str = "pkg/m.py"):
        return self.resolutions[relpath].edges

    def edge_map(self, relpath: str = "pkg/m.py") -> dict[tuple[str, str], object]:
        return {(edge.src, edge.dst): edge for edge in self.edges(relpath)}

    def diagnostics(self, relpath: str = "pkg/m.py"):
        return self.resolutions[relpath].diagnostics

    def externals(self, relpath: str = "pkg/m.py"):
        return self.resolutions[relpath].external_calls


def analyze(root: Path, files: dict[str, str]) -> Analysis:
    """Write `{relpath: source}`, build, extract every file, resolve every call site."""
    for relpath, content in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    sources = [SourceFile(path=root / relpath, relpath=relpath) for relpath in sorted(files)]
    outcome = run_build(sources, root.parent / "cache", ProgressSink(stream=io.StringIO()))

    index = extract.module_index(outcome.sources)
    extractions = {
        source.relpath: extract.extract_file(source, outcome.tree(source.relpath), index)
        for source in outcome.sources
    }
    targets = extract.TargetIndex.build(
        outcome.sources, [node for one in extractions.values() for node in one.nodes]
    )
    resolutions = {
        source.relpath: extract.resolve_calls(
            source, extractions[source.relpath], outcome.types, targets
        )
        for source in outcome.sources
    }
    return Analysis(outcome, extractions, resolutions)


def one(tmp_path: Path, source: str, **extra: str) -> Analysis:
    """Analyze a single module inside a package, plus any extra files it needs."""
    return analyze(tmp_path / "code", {"pkg/__init__.py": "", "pkg/m.py": source, **extra})


# ---------------------------------------------------------------------------
# The direct edge (FR-12)
# ---------------------------------------------------------------------------


def test_an_unambiguous_direct_call_is_exactly_one_edge(tmp_path):
    """AC-12.1, across a file boundary — the shape every trace is built from."""
    analysis = analyze(
        tmp_path / "code",
        {
            "pkg/__init__.py": "",
            "pkg/leaf.py": "def helper():\n    return 1\n",
            "pkg/m.py": "from pkg.leaf import helper\n\n\ndef caller():\n    return helper()\n",
        },
    )
    edges = analysis.edges()
    assert [(edge.src, edge.dst) for edge in edges] == [
        ("python:pkg.m.caller", "python:pkg.leaf.helper")
    ]
    assert edges[0].kind == "calls"
    assert edges[0].src_file == "pkg/m.py"
    assert edges[0].is_ambiguous == 0  # AC-40.2
    assert edges[0].attrs == {"call_sites": [[5, 11]]}


def test_every_written_edge_points_at_a_node_that_exists(tmp_path):
    """The seam's load-bearing invariant: `calls` edges never reference an unminted node.

    A target outside the analyzed set does not become an edge here — it becomes an
    `ExternalCall` for `externals.py` (task 2.4) to mint a node for. Emitting the edge now
    would produce a fragment that `validate_fragment()` rejects (AC-23.2), which is exactly
    how this invariant is checked below.
    """
    analysis = one(
        tmp_path,
        "import os\n"
        "\n"
        "\n"
        "def helper():\n"
        "    return os.getpid()\n"
        "\n"
        "\n"
        "def caller():\n"
        "    return helper(), os.getcwd(), print(1)\n",
    )
    known = {node.id for one_file in analysis.extractions.values() for node in one_file.nodes}
    fragments = [
        GraphFragment(
            file=FileRecord(path=relpath, content_hash="0" * 64, status="analyzed"),
            nodes=list(extraction.nodes),
            edges=[*extraction.edges, *analysis.resolutions[relpath].edges],
        )
        for relpath, extraction in sorted(analysis.extractions.items())
    ]
    for fragment in fragments:
        validate_fragment(fragment, known_ids=known)

    # ... and the external targets are held back, named, and countable.
    assert {call.qualified_name for call in analysis.externals()} == {
        "os.getpid",
        "os.getcwd",
        "builtins.print",
    }

    path = tmp_path / "index.sqlite"
    with full_write(path, META) as index:
        index.write_fragments(fragments)
    with open_index(path, read_only=True) as index:
        stored = index.connection.execute(
            "SELECT count(*) FROM edges WHERE kind = 'calls'"
        ).fetchone()[0]
    assert stored == 1  # caller -> helper; the three external targets wait for task 2.4


def test_a_call_into_a_file_outside_the_analyzed_set_becomes_an_external_hand_off(tmp_path):
    """AC-12.2/AC-36.2: an excluded file's function is named, not silently dropped.

    `pkg/excluded.py` is on disk (so the engine resolves the name) but is not an analysis
    input, which is what an exclusion rule produces upstream. The call therefore leaves the
    analyzed set and is handed to `externals.py` with its qualified name intact.
    """
    root = tmp_path / "code"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "excluded.py").write_text("def hidden():\n    return 1\n", encoding="utf-8")
    analysis = analyze(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "from pkg.excluded import hidden\n\n\ndef caller():\n    return hidden()\n",
        },
    )
    assert analysis.edges() == ()
    assert [(call.src, call.qualified_name) for call in analysis.externals()] == [
        ("python:pkg.m.caller", "pkg.excluded.hidden")
    ]
    assert analysis.diagnostics() == ()


# ---------------------------------------------------------------------------
# `src` attachment (D16, normative)
# ---------------------------------------------------------------------------

SCOPE_FIXTURE = """def compute():
    return 1


def factory(marker):
    def wrap(fn):
        return fn

    return wrap


TOP = compute()


@factory(compute())
def top():
    return compute()


class C:
    attr = compute()

    @factory(compute())
    def method(self):
        return compute()
"""


def test_a_call_sites_src_is_its_nearest_enclosing_executed_scope(tmp_path):
    """D16's attachment rule, in edges, on all five shapes the rule has to answer for."""
    analysis = one(tmp_path, SCOPE_FIXTURE)
    module_id = analysis.extractions["pkg/m.py"].module_id
    compute = "python:pkg.m.compute"

    src_by_line = {
        line: edge.src
        for edge in analysis.edges()
        if edge.dst == compute
        for line, _ in edge.attrs["call_sites"]
    }
    assert src_by_line == {
        12: module_id,  # a top-level statement
        15: module_id,  # a decorator on a top-level def
        17: "python:pkg.m.top",  # a function body
        21: "python:pkg.m.C",  # a class-level statement
        23: "python:pkg.m.C",  # a decorator on a method
        25: "python:pkg.m.C.method",  # a method body
    }


def test_no_synthetic_edge_is_invented_for_a_class_body(tmp_path):
    """D16 needs no module→class edge: a class body's calls attach to the class itself."""
    analysis = one(tmp_path, SCOPE_FIXTURE)
    class_id = "python:pkg.m.C"
    file_id = analysis.extractions["pkg/m.py"].file_id
    assert [edge for edge in analysis.edges() if edge.dst == class_id] == []
    assert [edge for edge in analysis.edges() if edge.src == file_id] == []


# ---------------------------------------------------------------------------
# Over-approximation and the ambiguity flag (FR-14, FR-40)
# ---------------------------------------------------------------------------


def test_a_union_receiver_yields_an_edge_to_every_possible_implementation(tmp_path):
    """AC-14.1/AC-40.1: two implementations, two edges, both flagged.

    This fixture is also the reason the ladder consults the *receiver's* type before the
    callee's own: mypy joins the union at the callee expression and offers `B.m` alone, so
    a resolver reading only that would silently drop `A.m` — precisely what FR-14 forbids.
    """
    analysis = one(
        tmp_path,
        "class A:\n"
        "    def m(self):\n"
        "        return 'a'\n"
        "\n"
        "\n"
        "class B:\n"
        "    def m(self):\n"
        "        return 'b'\n"
        "\n"
        "\n"
        "def dispatch(value: A | B):\n"
        "    return value.m()\n",
    )
    edges = [edge for edge in analysis.edges() if edge.src == "python:pkg.m.dispatch"]
    assert {edge.dst for edge in edges} == {"python:pkg.m.A.m", "python:pkg.m.B.m"}
    assert [edge.is_ambiguous for edge in edges] == [1, 1]
    assert all(edge.attrs["call_sites"] == [[12, 11]] for edge in edges)


def test_an_overload_group_resolves_to_every_member_flagged(tmp_path):
    """The other AC-14.1 shape: one name, several definitions, all statically possible.

    §4.1 gives each member its own `@line` ID, so the group is several nodes; the engine
    names the symbol, not the member. Keeping the implementation and dropping the stubs
    would be a guess, and dropping the implementation would point the trace at `...`.
    """
    analysis = one(
        tmp_path,
        "from typing import overload\n"
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
        "def caller():\n"
        "    return ov(1)\n",
    )
    edges = [edge for edge in analysis.edges() if edge.src == "python:pkg.m.caller"]
    assert {edge.dst for edge in edges} == {
        "python:pkg.m.ov@5",
        "python:pkg.m.ov@7",
        "python:pkg.m.ov@8",
    }
    assert all(edge.is_ambiguous == 1 for edge in edges)


def test_a_single_resolution_is_never_flagged(tmp_path):
    """AC-40.2, stated as its own assertion so the flag cannot become decorative."""
    analysis = one(
        tmp_path,
        "class A:\n"
        "    def m(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def dispatch(value: A):\n"
        "    return value.m()\n",
    )
    assert [(edge.dst, edge.is_ambiguous) for edge in analysis.edges()] == [("python:pkg.m.A.m", 0)]


def test_ambiguity_survives_the_collapse_onto_one_edge(tmp_path):
    """One edge, two sites, one of them ambiguous — the edge says so.

    §4.2 collapses every site for a `(src, dst)` pair onto one edge, so the flag has to
    mean "ambiguous somewhere". Clearing it because a later site was unambiguous would
    lose the FR-40 signal for the site that had it.
    """
    analysis = one(
        tmp_path,
        "class A:\n"
        "    def m(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "class B:\n"
        "    def m(self):\n"
        "        return 2\n"
        "\n"
        "\n"
        "def caller(one: A, both: A | B):\n"
        "    one.m()\n"
        "    return both.m()\n",
    )
    edge = analysis.edge_map()[("python:pkg.m.caller", "python:pkg.m.A.m")]
    assert edge.attrs["call_sites"] == [[12, 4], [13, 11]]
    assert edge.is_ambiguous == 1


# ---------------------------------------------------------------------------
# Unresolvable sites (AC-14.2)
# ---------------------------------------------------------------------------


def test_an_unresolvable_site_is_a_diagnostic_and_not_an_edge(tmp_path):
    """AC-14.2 on both shapes C-11 names: `getattr` dispatch and bare-name value flow."""
    analysis = one(
        tmp_path,
        "def dispatch(obj, name):\n"
        "    return getattr(obj, name)()\n"
        "\n"
        "\n"
        "def apply(callback):\n"
        "    return callback()\n",
    )
    assert analysis.edges() == ()

    diagnostics = {diag.line: diag for diag in analysis.diagnostics()}
    assert set(diagnostics) == {2, 6}
    for diag in diagnostics.values():
        assert diag.kind == "unresolved_call"
        assert diag.path == "pkg/m.py"
        assert diag.col is not None
    assert diagnostics[2].extra["callee"] == "getattr(...)"
    assert diagnostics[2].col == 11
    assert diagnostics[6].extra["callee"] == "callback"
    assert diagnostics[6].extra["scope"] == "python:pkg.m.apply"

    # The `getattr` call itself resolved — it is the *dispatch* that did not.
    assert [call.qualified_name for call in analysis.externals()] == ["builtins.getattr"]


def test_a_partially_resolved_site_still_records_what_was_lost(tmp_path):
    """Never silently drop: an edge that resolved does not excuse a candidate that did not.

    The fixture forces the shape directly — one of two union members names a definition the
    index does not carry — because it is the one case where a site produces an edge *and* a
    loss, and the loss would otherwise be invisible.
    """
    analysis = one(
        tmp_path,
        "class A:\n"
        "    def m(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "class B:\n"
        "    def m(self):\n"
        "        return 2\n"
        "\n"
        "\n"
        "def dispatch(value: A | B):\n"
        "    return value.m()\n",
    )
    extraction = analysis.extractions["pkg/m.py"]
    source = analysis.outcome.source_for("pkg/m.py")
    # An index that has forgotten `B.m` — what a merge (task 4.1) would look like if a
    # target file's nodes went missing.
    targets = extract.TargetIndex.build(
        analysis.outcome.sources,
        [node for node in extraction.nodes if node.id != "python:pkg.m.B.m"],
    )
    resolution = extract.resolve_calls(source, extraction, analysis.outcome.types, targets)

    assert [edge.dst for edge in resolution.edges] == ["python:pkg.m.A.m"]
    (diag,) = resolution.diagnostics
    assert diag.kind == "unresolved_call"
    assert diag.extra["unmapped"] == ["pkg.m.B.m"]
    assert "names no indexed definition" in diag.message


# ---------------------------------------------------------------------------
# Constructors and the MRO
# ---------------------------------------------------------------------------


def test_a_constructor_resolves_through_the_real_c3_mro(tmp_path):
    """The diamond: `D(B, C)`, `B(A)`, `C(A)`, `A` and `C` define `__init__`.

    C3 linearizes `D` as `[D, B, C, A]`, so `D()` runs **`C.__init__`** — not `A.__init__`,
    which a depth-first walk would reach (`FINDINGS-mypy.md` Q6, where this distinguished
    mypy from Jedi). Getting this wrong points every constructor trace at the wrong base.
    """
    analysis = one(
        tmp_path,
        "class A:\n"
        "    def __init__(self):\n"
        "        self.a = 1\n"
        "\n"
        "\n"
        "class B(A):\n"
        "    pass\n"
        "\n"
        "\n"
        "class C(A):\n"
        "    def __init__(self):\n"
        "        self.c = 1\n"
        "\n"
        "\n"
        "class D(B, C):\n"
        "    pass\n"
        "\n"
        "\n"
        "def make():\n"
        "    return D()\n",
    )
    assert [(edge.src, edge.dst) for edge in analysis.edges()] == [
        ("python:pkg.m.make", "python:pkg.m.C.__init__")
    ]


def test_an_inherited_constructor_resolves_to_the_base_that_defines_it(tmp_path):
    """`Sub()` reaches `Base.__init__`; the two call sites collapse onto one edge."""
    analysis = one(
        tmp_path,
        "class Base:\n"
        "    def __init__(self, x):\n"
        "        self.x = x\n"
        "\n"
        "\n"
        "class Sub(Base):\n"
        "    pass\n"
        "\n"
        "\n"
        "def make():\n"
        "    Base(1)\n"
        "    return Sub(2)\n",
    )
    edges = analysis.edges()
    assert [(edge.src, edge.dst) for edge in edges] == [
        ("python:pkg.m.make", "python:pkg.m.Base.__init__")
    ]
    assert edges[0].attrs["call_sites"] == [[11, 4], [12, 11]]


def test_an_external_class_is_named_by_the_class_not_by_its_stubs_init(tmp_path):
    """AC-36.1 wants a leaf node for *the imported symbol*.

    `collections.OrderedDict()` resolves through typeshed to `builtins.dict.__init__`; that
    name is an implementation detail of the stub, not the symbol the caller wrote, so the
    ladder stops at the class as soon as the class leaves the analyzed set.
    """
    analysis = one(
        tmp_path,
        "import collections\n\n\ndef make():\n    return collections.OrderedDict()\n",
    )
    assert [call.qualified_name for call in analysis.externals()] == ["collections.OrderedDict"]


def test_a_class_whose_mro_offers_only_objects_init_leaves_the_analyzed_set(tmp_path):
    """An analyzed class with no `__init__` anywhere in its MRO resolves to typeshed's.

    design.md §3.5's ladder ends "fullname outside the analyzed set → hand to externals",
    and `builtins.object.__init__` is such a fullname. The alternative — dropping the site —
    would make a constructor call the one call shape with no trace at all, and D19's
    conclusion (class nodes take essentially no incoming `calls` edges) holds either way.
    """
    analysis = one(tmp_path, "class Plain:\n    pass\n\n\ndef make():\n    return Plain()\n")
    assert analysis.edges() == ()
    assert [(call.src, call.qualified_name) for call in analysis.externals()] == [
        ("python:pkg.m.make", "builtins.object.__init__")
    ]
    assert analysis.diagnostics() == ()


# ---------------------------------------------------------------------------
# Chained calls (`FINDINGS-mypy.md` §2 trap 4)
# ---------------------------------------------------------------------------


def test_every_call_in_a_chain_gets_its_own_edge(tmp_path):
    """`x.f().g()` reports one `(line, col)` for both calls — and is still two edges.

    The prototype keyed sites on `(line, col)` and collapsed a chain onto one node, zeroing
    its `direct_calls` score. Resolving each `CallExpr` against its own callee is what makes
    the collision harmless here, so the assertion is that the shared position costs nothing.
    """
    analysis = one(
        tmp_path,
        "class Middle:\n"
        "    def g(self) -> int:\n"
        "        return 1\n"
        "\n"
        "\n"
        "class Head:\n"
        "    def f(self) -> Middle:\n"
        "        return Middle()\n"
        "\n"
        "\n"
        "def chained(head: Head) -> int:\n"
        "    return head.f().g()\n",
    )
    chained = [edge for edge in analysis.edges() if edge.src == "python:pkg.m.chained"]
    assert {edge.dst for edge in chained} == {"python:pkg.m.Head.f", "python:pkg.m.Middle.g"}
    assert {tuple(site) for edge in chained for site in edge.attrs["call_sites"]} == {(12, 11)}


def test_a_double_call_chain_resolves_both_ends(tmp_path):
    """`a()()` — the same collision with no member expression to tell the two apart."""
    analysis = one(
        tmp_path,
        "from collections.abc import Callable\n"
        "\n"
        "\n"
        "def inner() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def outer() -> Callable[[], int]:\n"
        "    return inner\n"
        "\n"
        "\n"
        "def caller():\n"
        "    return outer()()\n",
    )
    caller = [edge for edge in analysis.edges() if edge.src == "python:pkg.m.caller"]
    assert [edge.dst for edge in caller] == ["python:pkg.m.outer"]
    # The outer link of the chain is a `Callable[[], int]` with no definition behind it —
    # unresolvable, and therefore on the record rather than absent.
    assert [diag.extra["callee"] for diag in analysis.diagnostics()] == ["outer(...)"]


# ---------------------------------------------------------------------------
# Nested definitions, value flow, and the target index
# ---------------------------------------------------------------------------


def test_a_call_to_a_nested_definition_resolves_inside_its_own_file(tmp_path):
    """mypy names a nested function bare (`helper`), so the lookup is file-local."""
    analysis = one(
        tmp_path,
        "def outer():\n    def helper():\n        return 1\n\n    return helper()\n",
    )
    assert [(edge.src, edge.dst) for edge in analysis.edges()] == [
        ("python:pkg.m.outer", "python:pkg.m.outer.helper")
    ]


def test_value_flow_through_a_variable_still_resolves(tmp_path):
    """`CallableType.definition` is the rung that follows a callable through an assignment."""
    analysis = one(
        tmp_path,
        "def target():\n    return 1\n\n\ndef caller():\n    fn = target\n    return fn()\n",
    )
    assert {(edge.src, edge.dst) for edge in analysis.edges()} == {
        ("python:pkg.m.caller", "python:pkg.m.target")
    }


def test_the_target_index_is_built_from_rows_not_from_trees(tmp_path):
    """Task 4.1's precondition: a target can be named from index rows alone.

    On an incremental run only the rechecked modules have trees, so the file holding a
    call's *target* may not have been re-read at all. The index is therefore built from
    `NodeRow`s — which come equally from this run's extraction or from the stored index.
    """
    analysis = one(tmp_path, "def helper():\n    return 1\n")
    rows = [
        NodeRow(
            id="python:pkg.m.helper",
            kind="function",
            name="helper",
            language="python",
            file_path="pkg/m.py",
            start_line=1,
            end_line=2,
        )
    ]
    targets = extract.TargetIndex.build(analysis.outcome.sources, rows)
    assert targets.lookup("pkg.m.helper") == ("internal", ("python:pkg.m.helper",))
    assert targets.lookup("pkg.m.helper", lines=(1,)) == ("internal", ("python:pkg.m.helper",))
    assert targets.lookup("pkg.m.absent") == ("unknown", ())
    assert targets.lookup("requests.get") == ("external", ("requests.get",))
    assert targets.module_of("pkg.m.helper") == ("pkg/m.py", "helper")


def test_engine_module_names_and_path_derived_ids_are_reconciled(tmp_path):
    """The benchmark's own shape: the analysis root is itself a package.

    Analyzing `django/` makes `db/models/query.py` the file's name everywhere in this tool
    and `django.db.models.query` the engine's name for it (design.md §3.5 trap 2). Every
    fullname the ladder resolves therefore carries a package prefix that no node ID has, and
    reconciling the two is the whole reason `TargetIndex` exists rather than a dict lookup.
    """
    root = tmp_path / "code" / "pkg"
    analysis = analyze(
        root,
        {
            "__init__.py": "",
            "sub/__init__.py": "",
            "sub/leaf.py": "def helper():\n    return 1\n",
            "caller.py": "from pkg.sub.leaf import helper\n\n\ndef top():\n    return helper()\n",
        },
    )
    # The engine names the target `pkg.sub.leaf.helper`; the index calls it `sub.leaf.helper`.
    assert analysis.outcome.source_for("sub/leaf.py").module == "pkg.sub.leaf"
    assert [(edge.src, edge.dst) for edge in analysis.edges("caller.py")] == [
        ("python:caller.top", "python:sub.leaf.helper")
    ]


def test_a_path_derived_module_name_is_a_usable_caller(tmp_path):
    """D22: a migration file (`0001_initial.py`) is an ordinary source of `calls` edges.

    Its module name is not a dotted identifier, so nothing can import it by name — but it
    calls, and those edges have to carry its path-derived ID as their `src`.
    """
    analysis = analyze(
        tmp_path / "code",
        {
            "pkg/__init__.py": "",
            "pkg/leaf.py": "def helper():\n    return 1\n",
            "pkg/migrations/__init__.py": "",
            "pkg/migrations/0001_initial.py": (
                "from pkg.leaf import helper\n\n\ndef run():\n    return helper()\n"
            ),
        },
    )
    assert [(edge.src, edge.dst) for edge in analysis.edges("pkg/migrations/0001_initial.py")] == [
        ("python:pkg.migrations.0001_initial.run", "python:pkg.leaf.helper")
    ]


# ---------------------------------------------------------------------------
# Determinism (FR-44, write side)
# ---------------------------------------------------------------------------


def test_call_sites_are_sorted_and_stable_across_runs(tmp_path):
    """§4.2's collapsed sites are a sorted list, and the same tree yields the same list."""
    analysis = one(
        tmp_path,
        "def target():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def caller():\n"
        "    a = target()\n"
        "    b = [target(), target()]\n"
        "    return a, b, target()\n",
    )
    (edge,) = analysis.edges()
    assert edge.attrs["call_sites"] == [[6, 8], [7, 9], [7, 19], [8, 17]]
    assert edge.attrs["call_sites"] == sorted(edge.attrs["call_sites"])

    source = analysis.outcome.source_for("pkg/m.py")
    extraction = analysis.extractions["pkg/m.py"]
    targets = extract.TargetIndex.build(analysis.outcome.sources, extraction.nodes)
    first = extract.resolve_calls(source, extraction, analysis.outcome.types, targets)
    second = extract.resolve_calls(source, extraction, analysis.outcome.types, targets)
    assert first == second
    assert [(edge.src, edge.dst) for edge in first.edges] == sorted(
        (edge.src, edge.dst) for edge in first.edges
    )


def test_every_emitted_endpoint_is_a_grammar_id(tmp_path):
    """FR-22 again, on the half of the graph this task writes."""
    analysis = one(tmp_path, SCOPE_FIXTURE)
    for edge in analysis.edges():
        assert is_valid_node_id(edge.src) and is_valid_node_id(edge.dst)
    for call in analysis.externals():
        assert is_valid_node_id(call.src)
