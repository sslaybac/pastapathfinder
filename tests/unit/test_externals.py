"""External leaf nodes from D15's two sources.

design.md §3.5 (`externals`), §4.1, §4.2, D15, D23; requirements FR-36 (AC-36.1, AC-36.2,
AC-36.4, AC-36.5), FR-14 (AC-14.2's fallthrough), FR-37 (AC-37.2).

As in `test_extract.py` and `test_extract_calls.py`, the source-(a) fixtures drive the real
engine over a real tree: what mypy 2.3.0 hands back for `os.getcwd()` or for a call into a
file that is on disk but not an analysis input is the subject under test. The source-(b)
fixtures need no engine at all where they exercise the import table itself, which is the
point of that table being stdlib `ast` only (design.md §3.5, amended 2026-07-22).
"""

import ast
import io
from pathlib import Path

import pytest

from pastapathfinder.adapters.base import SourceFile
from pastapathfinder.adapters.python import externals, extract
from pastapathfinder.adapters.python.mypy_driver import EngineSource, run_build
from pastapathfinder.discovery import discover
from pastapathfinder.exclusions import build_ruleset
from pastapathfinder.index import canonical_nodes, full_write, open_index
from pastapathfinder.progress import ProgressSink
from pastapathfinder.reports import RunInfo, exclusions_document
from pastapathfinder.schema import (
    Diag,
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


class Analysis:
    """One built tree, extracted, resolved, and handed to `externals.resolve()`."""

    def __init__(self, outcome, extractions, resolutions, results):
        self.outcome = outcome
        self.extractions = extractions
        self.resolutions = resolutions
        self.results = results

    def nodes(self, relpath: str = "pkg/m.py"):
        return self.results[relpath].nodes

    def edges(self, relpath: str = "pkg/m.py"):
        return self.results[relpath].edges

    def diagnostics(self, relpath: str = "pkg/m.py"):
        return self.results[relpath].diagnostics

    def all_nodes(self):
        return [node for result in self.results.values() for node in result.nodes]

    def all_edges(self):
        return [edge for result in self.results.values() for edge in result.edges]

    def fragments(self) -> list[GraphFragment]:
        """The rows as the runner will assemble them: one fragment per analyzed file."""
        return [
            GraphFragment(
                file=FileRecord(path=relpath, content_hash="0" * 64, status="analyzed"),
                nodes=[*extraction.nodes, *self.results[relpath].nodes],
                edges=[
                    *extraction.edges,
                    *self.resolutions[relpath].edges,
                    *self.results[relpath].edges,
                ],
            )
            for relpath, extraction in sorted(self.extractions.items())
        ]


def analyze(root: Path, files: dict[str, str], on_disk: dict[str, str] | None = None) -> Analysis:
    """Analyze `files`; `on_disk` is written but never handed in as an analysis input.

    That second argument is what an exclusion or a skip leaves behind: the file exists (so
    the engine can read it while following imports) and it is not analyzed, which is
    AC-36.2's and AC-12.2's shape.
    """
    for relpath, content in {**files, **(on_disk or {})}.items():
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
    entries = [(source, resolutions[source.relpath]) for source in outcome.sources]
    results = externals.resolve(entries, [source.module for source in outcome.sources])
    return Analysis(outcome, extractions, resolutions, results)


def one(tmp_path: Path, source: str, **extra: str) -> Analysis:
    """Analyze a single module inside a package, plus any extra files it needs."""
    return analyze(tmp_path / "code", {"pkg/__init__.py": "", "pkg/m.py": source, **extra})


# ---------------------------------------------------------------------------
# AC-36.1 — the leaf node and its edge, from both D15 sources
# ---------------------------------------------------------------------------


def test_a_call_into_an_imported_library_yields_one_leaf_node_and_one_edge(tmp_path):
    """AC-36.1 through D15 source (a): the engine named a target outside the set."""
    analysis = one(
        tmp_path,
        "import os\n\n\ndef caller():\n    return os.getcwd()\n",
    )
    (node,) = analysis.nodes()
    assert node.id == "python:os.getcwd"
    assert node.name == "getcwd"
    assert node.kind == externals.EXTERNAL_KIND
    assert node.language == "python"
    assert node.is_external == 1
    # AC-37.2: no location at all on an external node — FR-36 forbids analyzing it.
    assert (node.file_path, node.start_line, node.end_line) == (None, None, None)
    assert node.attrs == {}

    (edge,) = analysis.edges()
    assert (edge.src, edge.dst, edge.kind) == ("python:pkg.m.caller", node.id, "calls")
    assert edge.src_file == "pkg/m.py"
    assert edge.is_ambiguous == 0
    assert edge.attrs == {"call_sites": [[5, 11]]}
    assert analysis.diagnostics() == ()


def test_a_call_into_an_absent_third_party_package_is_named_by_the_import_table(tmp_path):
    """AC-36.1 through D15 source (b): the engine resolved nothing, the import did.

    `acme` is not installed and never will be — FR-6/AC-13.1 say analysis runs anyway — so
    mypy assigns `Any` and offers no target at all (verified by the diagnostic that reaches
    this module). The `from … import …` statement names it exactly, which is the entire
    mechanism requirements §6 item 10's amendment permits.
    """
    analysis = one(
        tmp_path,
        "from acme.widgets import build\n"
        "import acme.legacy\n"
        "import acme.legacy as legacy\n"
        "\n"
        "\n"
        "def caller():\n"
        "    build()\n"
        "    acme.legacy.run()\n"
        "    legacy.run()\n",
    )
    # Three unresolved sites went in (extract.py resolved none of them) ...
    assert [diag.extra["callee"] for diag in analysis.resolutions["pkg/m.py"].diagnostics] == [
        "build",
        "acme.legacy.run",
        "legacy.run",
    ]
    # ... and came out as two named leaves (two symbols, three spellings), with
    # nothing left unresolved.
    assert [node.id for node in analysis.nodes()] == [
        "python:acme.legacy.run",
        "python:acme.widgets.build",
    ]
    assert analysis.diagnostics() == ()

    edges = {edge.dst: edge for edge in analysis.edges()}
    assert edges["python:acme.widgets.build"].attrs == {"call_sites": [[7, 4]]}
    # `acme.legacy.run` and `legacy.run` are two spellings of one symbol: one node, one
    # edge, both sites on it (§4.2's collapse rule).
    assert edges["python:acme.legacy.run"].attrs == {"call_sites": [[8, 4], [9, 4]]}
    assert all(edge.is_ambiguous == 0 for edge in analysis.edges())


def test_an_unanalyzed_relative_import_is_resolved_against_the_importing_package(tmp_path):
    """Source (b) has to resolve `from . import x` before it can name anything."""
    analysis = analyze(
        tmp_path / "code",
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "from .hidden import work\n\n\ndef caller():\n    return work()\n",
        },
        on_disk={"pkg/hidden.py": "x = 1\n"},
    )
    # `pkg/hidden.py` parses to nothing useful for the engine (`work` is not in it), so the
    # site is unresolved and the import statement is what names the target.
    assert [node.id for node in analysis.nodes()] == ["python:pkg.hidden.work"]
    assert analysis.diagnostics() == ()


# ---------------------------------------------------------------------------
# AC-36.2 — an excluded file's function, still attributed as excluded
# ---------------------------------------------------------------------------


def test_a_call_into_an_excluded_file_is_an_external_leaf_and_stays_attributed(tmp_path):
    """AC-36.2, end to end: the exclusion rule and the external node are both visible.

    The exclusion is produced by the real rule engine rather than asserted by hand, because
    the requirement is about the *pair* — the call is represented, and the reason its target
    was never analyzed remains findable in the exclusion report.
    """
    root = tmp_path / "code"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "generated.py").write_text("def hidden():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "m.py").write_text(
        "from pkg.generated import hidden\n\n\ndef caller():\n    return hidden()\n",
        encoding="utf-8",
    )

    ruleset = build_ruleset(root, exclude=["pkg/generated.py"])
    discovery = discover(root, ruleset)
    candidates = {discovery.relpath(path) for path in discovery.candidates}
    assert candidates == {"pkg/__init__.py", "pkg/m.py"}
    analysis = analyze(root, {relpath: (root / relpath).read_text() for relpath in candidates})

    (node,) = analysis.nodes()
    assert node.id == "python:pkg.generated.hidden"
    assert node.is_external == 1
    assert [(edge.src, edge.dst) for edge in analysis.edges()] == [
        ("python:pkg.m.caller", "python:pkg.generated.hidden")
    ]

    # ... and the excluded file is still attributed by its rule (FR-5), which is the half
    # of AC-36.2 that makes the leaf node interpretable rather than mysterious.
    run = RunInfo.start(run_id="r").finish(0.0)
    document = exclusions_document(run, discovery.excluded)
    assert document["exclusions"] == [
        {
            "path": "pkg/generated.py",
            "is_dir": False,
            "pattern": "pkg/generated.py",
            "source": "user:exclude",
        }
    ]


# ---------------------------------------------------------------------------
# AC-36.5 — one node per symbol, however many callers
# ---------------------------------------------------------------------------


def test_the_same_external_symbol_from_three_sites_is_exactly_one_node(tmp_path):
    """AC-36.5, within a file and across files, and then in the store."""
    analysis = analyze(
        tmp_path / "code",
        {
            "pkg/__init__.py": "",
            "pkg/m.py": (
                "import os\n"
                "\n"
                "\n"
                "def first():\n"
                "    return os.getcwd()\n"
                "\n"
                "\n"
                "def second():\n"
                "    os.getcwd()\n"
                "    return os.getcwd()\n"
            ),
            "pkg/other.py": "import os\n\n\ndef third():\n    return os.getcwd()\n",
        },
    )
    assert [node.id for node in analysis.nodes()] == ["python:os.getcwd"]
    assert [node.id for node in analysis.nodes("pkg/other.py")] == ["python:os.getcwd"]

    # Three sites, two callers, two files → three edges (one per caller scope), each
    # carrying its own sites, and exactly one node once the store has collapsed the
    # identical rows every calling fragment carries.
    assert sorted(
        (edge.src, tuple(map(tuple, edge.attrs["call_sites"]))) for edge in analysis.all_edges()
    ) == [
        ("python:pkg.m.first", ((5, 11),)),
        ("python:pkg.m.second", ((9, 4), (10, 11))),
        ("python:pkg.other.third", ((5, 11),)),
    ]
    assert len(canonical_nodes(analysis.all_nodes())) == 1

    path = tmp_path / "index.sqlite"
    with full_write(path, META) as index:
        index.write_fragments(analysis.fragments())
    with open_index(path, read_only=True) as index:
        rows = index.connection.execute(
            "SELECT id, kind, file_path, start_line, end_line FROM nodes WHERE is_external = 1"
        ).fetchall()
    assert rows == [("python:os.getcwd", "function", None, None, None)]


# ---------------------------------------------------------------------------
# AC-36.4 — never a guessed name
# ---------------------------------------------------------------------------


def test_a_site_with_no_resolution_and_no_import_stays_a_diagnostic(tmp_path):
    """AC-36.4: no candidate and no import statement means no node — and no silence.

    The three shapes are C-11's own: `getattr` dispatch, a bare name that flowed in as a
    parameter, and an attribute call on an untyped receiver. The last is the boundary this
    module deliberately does not cross: a class in this very file defines `save`, and
    matching on that name is backlog B-22's un-narrowed mechanism (`FINDINGS-namematch.md`
    §3 measured its fanout at up to 760 candidates), not this one's.
    """
    analysis = one(
        tmp_path,
        "class Model:\n"
        "    def save(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def dispatch(obj, name):\n"
        "    return getattr(obj, name)()\n"
        "\n"
        "\n"
        "def apply(callback):\n"
        "    return callback()\n"
        "\n"
        "\n"
        "def store(record):\n"
        "    return record.save()\n",
    )
    # The only external is `getattr` itself, which the engine did name.
    assert [node.id for node in analysis.nodes()] == ["python:builtins.getattr"]

    diagnostics = {diag.line: diag for diag in analysis.diagnostics()}
    assert sorted(diagnostics) == [7, 11, 15]
    assert [diag.kind for diag in diagnostics.values()] == ["unresolved_call"] * 3
    assert [diagnostics[line].extra["callee"] for line in (7, 11, 15)] == [
        "getattr(...)",
        "callback",
        "record.save",
    ]
    assert all(diag.path == "pkg/m.py" and diag.col is not None for diag in diagnostics.values())


def test_an_imported_name_from_an_analyzed_module_is_never_named_external(tmp_path):
    """AC-36.4's other half: the import table may not rename analyzed code.

    Driven directly rather than through the engine, because mypy resolves this shape — the
    fixture forces the state the guard exists for (an unresolved site whose callee *is*
    imported, from a module the run analyzed) and asserts the site stays unresolved.
    """
    root = tmp_path / "code"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "m.py").write_text(
        "from pkg.other import helper\nfrom vendor.lib import tool\n", encoding="utf-8"
    )
    source = _source(root / "pkg" / "m.py", "pkg/m.py", "pkg.m")
    resolution = extract.CallResolution(
        diagnostics=(
            _unresolved("helper", "python:pkg.m.caller"),
            _unresolved("tool", "python:pkg.m.caller"),
        )
    )

    result = externals.resolve_file(source, resolution, {"pkg", "pkg.m", "pkg.other"})
    assert [node.id for node in result.nodes] == ["python:vendor.lib.tool"]
    assert [diag.extra["callee"] for diag in result.diagnostics] == ["helper"]


def test_a_partially_resolved_site_is_left_alone(tmp_path):
    """A site that already produced an edge is not source (b)'s business (D15)."""
    source = _source(tmp_path / "m.py", "m.py", "m")
    partial = Diag(
        kind="unresolved_call",
        path="m.py",
        line=3,
        col=4,
        message="resolved to a definition the index does not carry",
        extra={"callee": "tool", "scope": "python:m.caller", "unmapped": ["pkg.other.tool"]},
    )
    result = externals.resolve_file(source, extract.CallResolution(diagnostics=(partial,)), set())
    assert result.nodes == ()
    assert result.diagnostics == (partial,)


def test_an_unreadable_file_leaves_every_site_unresolved(tmp_path):
    """The import table is unavailable, so nothing is named — EC-14's window, honestly.

    A file that vanished between the engine's read and this pass cannot be attributed from
    its imports without attributing from contents the analysis never used.
    """
    source = _source(tmp_path / "gone.py", "gone.py", "gone")
    resolution = extract.CallResolution(diagnostics=(_unresolved("tool", "python:gone.<module>"),))
    result = externals.resolve_file(source, resolution, set())
    assert (result.nodes, result.edges) == ((), ())
    assert [diag.extra["callee"] for diag in result.diagnostics] == ["tool"]


# ---------------------------------------------------------------------------
# The leaf invariants (FR-36, AC-37.2)
# ---------------------------------------------------------------------------


EXTERNAL_MIX = (
    "import os\n"
    "from acme.widgets import build\n"
    "from collections import OrderedDict\n"
    "\n"
    "\n"
    "def caller():\n"
    "    os.getcwd()\n"
    "    build()\n"
    "    print(OrderedDict())\n"
)


def test_no_external_node_has_an_outgoing_edge_or_a_span(tmp_path):
    """FR-36's leaf guarantee and AC-37.2, over a fixture mixing both D15 sources."""
    analysis = one(tmp_path, EXTERNAL_MIX)
    external_ids = {node.id for node in analysis.all_nodes()}
    assert external_ids >= {"python:os.getcwd", "python:acme.widgets.build"}

    for node in analysis.all_nodes():
        assert node.is_external == 1
        assert (node.file_path, node.start_line, node.end_line) == (None, None, None)
        assert is_valid_node_id(node.id)  # AC-22.1

    # Nothing anywhere in the run's rows leaves an external node.
    for fragment in analysis.fragments():
        assert not any(edge.src in external_ids for edge in fragment.edges)


def test_the_rows_validate_and_store_as_one_graph(tmp_path):
    """AC-23.2's endpoint rule: the edges this module writes point at nodes that exist."""
    analysis = one(tmp_path, EXTERNAL_MIX)
    fragments = analysis.fragments()
    known = {node.id for fragment in fragments for node in fragment.nodes}
    for fragment in fragments:
        validate_fragment(fragment, known_ids=known)

    path = tmp_path / "index.sqlite"
    with full_write(path, META) as index:
        index.write_fragments(fragments)
    with open_index(path, read_only=True) as index:
        dangling = index.connection.execute(
            "SELECT count(*) FROM edges WHERE dst NOT IN (SELECT id FROM nodes)"
        ).fetchone()[0]
        outgoing = index.connection.execute(
            "SELECT count(*) FROM edges WHERE src IN (SELECT id FROM nodes WHERE is_external = 1)"
        ).fetchone()[0]
    assert (dangling, outgoing) == (0, 0)


def test_an_edge_leaving_an_external_node_fails_the_run_rather_than_being_written():
    """The "enforced" in design.md §3.5's parenthesis, exercised.

    The shape cannot arise from the ladder — an edge's `src` is a caller scope in an
    analyzed file — so it is forced here. A leaf that acquired an outgoing edge would let a
    slice walk into code FR-36 says was never analyzed, and silence would be the worst way
    to find that out.
    """
    source = _source(Path("/nonexistent/m.py"), "m.py", "m")
    resolution = extract.CallResolution(
        external_calls=(
            extract.ExternalCall("python:m.caller", "acme.tool", "m.py", ((1, 0),)),
            extract.ExternalCall("python:acme.tool", "other.thing", "m.py", ((2, 0),)),
        )
    )
    with pytest.raises(ValueError, match="must have no outgoing edges"):
        externals.resolve_file(source, resolution, set())


def test_an_unnameable_target_becomes_a_diagnostic_rather_than_a_dropped_site():
    """Nothing leaves without a trace: a name no §4.1 ID can hold is still recorded."""
    source = _source(Path("/nonexistent/m.py"), "m.py", "m")
    resolution = extract.CallResolution(
        external_calls=(
            extract.ExternalCall(
                src="python:m.caller",
                qualified_name="weird:name",  # a colon is the grammar's own delimiter
                src_file="m.py",
                call_sites=((4, 8),),
            ),
        )
    )
    result = externals.resolve_file(source, resolution, set())
    assert (result.nodes, result.edges) == ((), ())
    (diag,) = result.diagnostics
    assert diag.kind == "unresolved_call"
    assert (diag.path, diag.line, diag.col) == ("m.py", 4, 8)
    assert diag.extra["callee"] == "weird:name"
    assert "cannot be written as a node identifier" in diag.message


# ---------------------------------------------------------------------------
# The import table itself (stdlib `ast` only)
# ---------------------------------------------------------------------------


def table(source: str, module: str = "pkg.sub.m", is_package: bool = False):
    """`{bound name: (qualified name, module depth)}` — the table, flattened for reading."""
    parsed = externals.import_table(ast.parse(source), module, is_package)
    return {name: (binding.qualified, binding.module_depth) for name, binding in parsed.items()}


def test_the_import_table_maps_every_binding_form_to_its_qualified_name():
    """Each form also records how much of the name the *statement* proved is a module."""
    assert table(
        "import os\n"
        "import os.path\n"
        "import numpy as np\n"
        "import acme.legacy as legacy\n"
        "from acme.widgets import build, Widget as W\n"
    ) == {
        # `import os.path` binds `os` and proves only `os` is a module — the rest of a
        # callee written `os.path.join` is attribute syntax as far as this table knows.
        "os": ("os", 1),
        "np": ("numpy", 1),
        "legacy": ("acme.legacy", 2),
        "build": ("acme.widgets.build", 2),
        "W": ("acme.widgets.Widget", 2),
    }


def test_the_import_table_resolves_relative_imports():
    """`from . import x` and `from ..pkg import y`, against the importer's own module."""
    assert table("from . import sibling\nfrom .leaf import work\n") == {
        "sibling": ("pkg.sub.sibling", 2),
        "work": ("pkg.sub.leaf.work", 3),
    }
    assert table("from .. import top\n") == {"top": ("pkg.top", 1)}
    assert table("from . import inner\n", module="pkg.sub", is_package=True) == {
        "inner": ("pkg.sub.inner", 2)
    }
    # A statement that climbs above the root names nothing rather than guessing.
    assert table("from ..... import nowhere\n") == {}


def test_the_import_table_covers_deferred_imports_inside_functions():
    """Real code breaks import cycles this way, and those calls are the unresolved ones."""
    assert table("def caller():\n    from acme.widgets import build\n    return build()\n") == {
        "build": ("acme.widgets.build", 2)
    }


def test_a_name_bound_twice_to_different_targets_is_dropped():
    """Two statements disagreeing about a name make the lookup a guess (AC-36.4)."""
    assert table("from acme import tool\nfrom other import tool\n") == {}
    assert table("from acme import tool\nfrom acme import tool\n") == {"tool": ("acme.tool", 1)}


def test_a_star_import_binds_nothing():
    """`from acme import *` names symbols this table cannot enumerate; it claims none."""
    assert table("from acme import *\n") == {}


def test_qualify_only_answers_for_a_plain_dotted_name():
    bindings = {
        "acme": externals.ImportBinding("acme", 1),
        "build": externals.ImportBinding("acme.widgets.build", 2),
    }
    assert externals.qualify("build", bindings) == externals.ImportBinding("acme.widgets.build", 2)
    assert externals.qualify("acme.legacy.run", bindings) == externals.ImportBinding(
        "acme.legacy.run", 1
    )
    assert externals.qualify("unbound", bindings) is None
    assert externals.qualify("factory(...).run", bindings) is None
    assert externals.qualify("handlers[...]", bindings) is None
    assert externals.qualify("<lambda>", bindings) is None


def test_inside_analyzed_set_tests_only_prefixes_the_statement_proved_are_modules():
    """The AC-36.2 subtlety: an analyzed package must not swallow its excluded submodule."""
    analyzed = {"pkg", "pkg.other"}
    assert externals.inside_analyzed_set(externals.ImportBinding("pkg.other.helper", 2), analyzed)
    # `import pkg` then `pkg.other.helper()`: the statement proved only `pkg`, and the
    # walk up finds `pkg.other` anyway.
    assert externals.inside_analyzed_set(externals.ImportBinding("pkg.other.helper", 1), analyzed)
    # `from pkg.hidden import work`, where `pkg/hidden.py` was excluded: `pkg` is analyzed
    # and `pkg.hidden` is not, so the name does leave the set (AC-36.2).
    assert not externals.inside_analyzed_set(
        externals.ImportBinding("pkg.hidden.work", 2), analyzed
    )
    assert not externals.inside_analyzed_set(externals.ImportBinding("pkgx.helper", 1), analyzed)
    assert not externals.inside_analyzed_set(externals.ImportBinding("os.path.join", 1), analyzed)


def test_the_import_table_is_built_with_the_standard_library_parser(tmp_path):
    """requirements §6 item 10's amendment is conditional on *which* parser this is.

    The exemption reads "bounded syntactic mechanisms built on the standard library's
    parser", and design.md §3.5 pins it further: never from the engine's trees, which do
    not exist for a file the engine did not recheck. Both halves are asserted — the module
    imports `ast` and nothing from `mypy`, and it tables a file no build ever saw.
    """
    module = Path(externals.__file__)
    imported: set[str] = set()
    for node in ast.walk(ast.parse(module.read_bytes())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    assert "ast" in imported
    assert not any(name == "mypy" or name.startswith("mypy.") for name in imported)

    unseen = tmp_path / "never_built.py"
    unseen.write_text("from acme.widgets import build\n", encoding="utf-8")
    assert externals.read_import_table(unseen, "never_built", False) == {
        "build": externals.ImportBinding("acme.widgets.build", 2)
    }
    assert externals.read_import_table(tmp_path / "absent.py", "absent", False) is None
    (tmp_path / "broken.py").write_text("def (\n", encoding="utf-8")
    assert externals.read_import_table(tmp_path / "broken.py", "broken", False) is None


# ---------------------------------------------------------------------------
# Determinism (FR-44)
# ---------------------------------------------------------------------------


def test_two_runs_over_one_tree_produce_identical_rows(tmp_path):
    """Nodes sorted by id, edges by (src, dst), call sites sorted (D12)."""
    first = one(tmp_path / "a", EXTERNAL_MIX)
    second = one(tmp_path / "b", EXTERNAL_MIX)
    assert first.nodes() == second.nodes()
    assert first.edges() == second.edges()
    assert [node.id for node in first.nodes()] == sorted(node.id for node in first.nodes())
    assert [(edge.src, edge.dst) for edge in first.edges()] == sorted(
        (edge.src, edge.dst) for edge in first.edges()
    )


# ---------------------------------------------------------------------------
# Test support
# ---------------------------------------------------------------------------


def _source(path: Path, relpath: str, module: str):
    """An `EngineSource` for the fixtures that drive `resolve_file()` directly."""
    return EngineSource(
        path=path, relpath=relpath, module=module, source_root=path.parent, content_hash="0" * 64
    )


def _unresolved(callee: str, scope: str, line: int = 3, col: int = 4) -> Diag:
    """The `unresolved_call` shape `extract.py` records for a fully unresolved site."""
    return Diag(
        kind="unresolved_call",
        path="pkg/m.py",
        line=line,
        col=col,
        message=f"no call target could be statically determined for {callee!r}",
        extra={"callee": callee, "scope": scope},
    )
