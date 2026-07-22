"""Task 3.2 — `console_scripts`: packaging-declared CLI entry points.

design.md §3.7 (`console_scripts` rule), §4.1 (`entry:` ID form), §4.2; requirements FR-10
(AC-10.1, AC-10.2, C-1's packaging-only scope), FR-13 (no execution of target code).

The detector reads `pyproject.toml`, `setup.cfg`, and `setup.py` **statically** and emits an
`entry_point` node per declaration whose `module:func` target resolves to an analyzed
function; an unresolvable target is a diagnostic, never a drop, and `setup.py` is parsed,
never run.
"""

from __future__ import annotations

from pathlib import Path

from pastapathfinder.detectors import registry
from pastapathfinder.detectors.base import ProjectInput
from pastapathfinder.detectors.console_scripts import ConsoleScriptsDetector
from pastapathfinder.detectors.registry import run_detectors
from pastapathfinder.schema import (
    DETECTORS,
    FileRecord,
    GraphFragment,
    NodeRow,
    is_valid_node_id,
    validate_fragment,
)

# The index already holds these code nodes for the analyzed tree; resolution targets them.
NODE_IDS = frozenset(
    {
        "python:pkg.cli.main",
        "python:pkg.cli.other",
        "python:pkg.web.Server.run",
    }
)


def project(
    tmp_path: Path, files: dict[str, str], node_ids: frozenset[str] = NODE_IDS
) -> ProjectInput:
    """Write the given metadata files under `tmp_path` and discover them for detection."""
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return ProjectInput.discover(tmp_path, node_ids=node_ids)


def detect(tmp_path: Path, files: dict[str, str], node_ids: frozenset[str] = NODE_IDS):
    return ConsoleScriptsDetector().detect(project(tmp_path, files, node_ids))


# ---------------------------------------------------------------------------
# AC-10.1 — a resolvable declaration yields an entry node targeting the function
# ---------------------------------------------------------------------------


def test_pyproject_project_scripts_resolves(tmp_path):
    """AC-10.1: `[project.scripts]` pointing at an analyzed function yields an entry node."""
    out = detect(tmp_path, {"pyproject.toml": '[project.scripts]\nmytool = "pkg.cli:main"\n'})

    assert len(out.nodes) == 1
    (entry,) = out.nodes
    (edge,) = out.edges
    assert entry.kind == "entry_point"
    assert entry.attrs["detector"] == "console_script"
    assert entry.attrs["command"] == "mytool"
    assert entry.name == "mytool"
    assert entry.id == "python:entry:console_script:pkg.cli.main@2"
    assert edge.src == entry.id
    assert edge.kind == "calls"
    assert edge.dst == "python:pkg.cli.main"
    assert edge.src_file is None  # a detector is not a caller file (D18)
    assert out.diagnostics == []


def test_pyproject_entry_points_console_scripts_resolves(tmp_path):
    """AC-10.1: `[project.entry-points.console_scripts]` is read as well as `[project.scripts]`."""
    text = '[project.entry-points.console_scripts]\nmytool = "pkg.cli:main"\n'
    (edge,) = detect(tmp_path, {"pyproject.toml": text}).edges
    assert edge.dst == "python:pkg.cli.main"


def test_setup_cfg_resolves(tmp_path):
    """AC-10.1: `[options.entry_points]`'s `console_scripts` block resolves each line."""
    text = (
        "[options.entry_points]\n"
        "console_scripts =\n"
        "    mytool = pkg.cli:main\n"
        "    aux = pkg.cli:other\n"
    )
    out = detect(tmp_path, {"setup.cfg": text})
    assert sorted(e.dst for e in out.edges) == ["python:pkg.cli.main", "python:pkg.cli.other"]
    assert out.diagnostics == []


def test_setup_py_literal_resolves(tmp_path):
    """AC-10.1: a literal `entry_points` argument to `setup()` resolves."""
    text = (
        "from setuptools import setup\n"
        "setup(\n"
        "    name='pkg',\n"
        "    entry_points={'console_scripts': ['mytool = pkg.cli:main']},\n"
        ")\n"
    )
    (edge,) = detect(tmp_path, {"setup.py": text}).edges
    assert edge.dst == "python:pkg.cli.main"


def test_setup_py_dotted_setuptools_call_resolves(tmp_path):
    """A `setuptools.setup(...)` attribute call is recognized, not only a bare `setup(...)`."""
    text = (
        "import setuptools\n"
        "setuptools.setup(entry_points={'console_scripts': ['mytool = pkg.cli:main']})\n"
    )
    (edge,) = detect(tmp_path, {"setup.py": text}).edges
    assert edge.dst == "python:pkg.cli.main"


def test_a_method_target_resolves_to_the_method_node(tmp_path):
    """`module:Class.method` becomes the dotted qualname `module.Class.method`."""
    text = '[project.scripts]\nserve = "pkg.web:Server.run"\n'
    (edge,) = detect(tmp_path, {"pyproject.toml": text}).edges
    assert edge.dst == "python:pkg.web.Server.run"


def test_extras_suffix_is_stripped_before_resolution(tmp_path):
    """A `module:func [extras]` reference resolves on the object reference alone (PEP 508)."""
    text = '[project.scripts]\nmytool = "pkg.cli:main [color]"\n'
    (edge,) = detect(tmp_path, {"pyproject.toml": text}).edges
    assert edge.dst == "python:pkg.cli.main"


def test_multiple_scripts_yield_distinct_entries(tmp_path):
    """FR-10 emits one entry per declaration, with distinct IDs from distinct targets."""
    text = '[project.scripts]\none = "pkg.cli:main"\ntwo = "pkg.cli:other"\n'
    out = detect(tmp_path, {"pyproject.toml": text})
    assert sorted(n.id for n in out.nodes) == [
        "python:entry:console_script:pkg.cli.main@2",
        "python:entry:console_script:pkg.cli.other@3",
    ]


# ---------------------------------------------------------------------------
# AC-10.2 — an unresolvable declaration is a diagnostic, never a silent drop
# ---------------------------------------------------------------------------


def test_unresolvable_target_is_diagnosed_not_dropped(tmp_path):
    """AC-10.2: a target absent from the index yields `unresolved_entry_declaration`."""
    out = detect(tmp_path, {"pyproject.toml": '[project.scripts]\nghost = "pkg.cli:missing"\n'})
    assert out.nodes == []
    assert out.edges == []
    (diag,) = out.diagnostics
    assert diag.kind == "unresolved_entry_declaration"
    assert diag.path == "pyproject.toml"
    assert "ghost" in diag.message
    assert "pkg.cli:missing" in diag.message


def test_a_malformed_reference_is_diagnosed(tmp_path):
    """A reference with no `module:object` colon cannot be shaped into a target."""
    out = detect(tmp_path, {"pyproject.toml": '[project.scripts]\nbad = "just_a_module"\n'})
    assert out.nodes == []
    (diag,) = out.diagnostics
    assert diag.kind == "unresolved_entry_declaration"


def test_resolvable_and_unresolvable_coexist(tmp_path):
    """One good and one bad declaration: the good resolves, the bad is diagnosed."""
    text = '[project.scripts]\ngood = "pkg.cli:main"\nbad = "pkg.cli:missing"\n'
    out = detect(tmp_path, {"pyproject.toml": text})
    assert [e.dst for e in out.edges] == ["python:pkg.cli.main"]
    assert [d.kind for d in out.diagnostics] == ["unresolved_entry_declaration"]


# ---------------------------------------------------------------------------
# FR-13 — setup.py is parsed, never executed
# ---------------------------------------------------------------------------


def test_setup_py_is_parsed_never_executed(tmp_path):
    """FR-13: a `setup.py` that writes a witness on execution leaves no witness."""
    witness = tmp_path / "witness.txt"
    text = (
        "from setuptools import setup\n"
        f"open({str(witness)!r}, 'w').write('ran')\n"
        "setup(entry_points={'console_scripts': ['mytool = pkg.cli:main']})\n"
    )
    out = detect(tmp_path, {"setup.py": text})
    assert not witness.exists()  # the module body never ran
    assert [e.dst for e in out.edges] == ["python:pkg.cli.main"]  # yet the literal was read


def test_computed_entry_points_is_recorded_unresolved_not_evaluated(tmp_path):
    """FR-13: a non-literal `entry_points` value is diagnosed, not evaluated."""
    text = (
        "from setuptools import setup\n"
        "eps = {'console_scripts': ['mytool = pkg.cli:main']}\n"
        "setup(entry_points=eps)\n"
    )
    out = detect(tmp_path, {"setup.py": text})
    assert out.nodes == []
    (diag,) = out.diagnostics
    assert diag.kind == "unresolved_entry_declaration"
    assert "entry_points is not a literal dict" in diag.message
    assert "FR-13" in diag.message


def test_computed_console_scripts_list_is_recorded_unresolved(tmp_path):
    """A computed `console_scripts` value inside a literal dict is still not evaluated."""
    text = "from setuptools import setup\nsetup(entry_points={'console_scripts': make_scripts()})\n"
    out = detect(tmp_path, {"setup.py": text})
    assert out.nodes == []
    assert [d.kind for d in out.diagnostics] == ["unresolved_entry_declaration"]
    assert "console_scripts is not a literal list" in out.diagnostics[0].message


def test_computed_list_element_is_recorded_unresolved(tmp_path):
    """A non-literal element of an otherwise-literal list is diagnosed, others still read."""
    text = (
        "from setuptools import setup\n"
        "setup(entry_points={'console_scripts': ['good = pkg.cli:main', NAME]})\n"
    )
    out = detect(tmp_path, {"setup.py": text})
    assert [e.dst for e in out.edges] == ["python:pkg.cli.main"]
    assert [d.kind for d in out.diagnostics] == ["unresolved_entry_declaration"]
    assert "not a literal string" in out.diagnostics[0].message


# ---------------------------------------------------------------------------
# B-20 / C-1 — only packaging-declared entry points; bare scripts are not detected
# ---------------------------------------------------------------------------


def test_no_declarations_yields_nothing(tmp_path):
    """A `pyproject.toml` without a scripts table declares no console script (B-20 scope)."""
    out = detect(tmp_path, {"pyproject.toml": '[project]\nname = "pkg"\n'})
    assert out.nodes == []
    assert out.edges == []
    assert out.diagnostics == []


def test_a_bare_root_yields_nothing(tmp_path):
    """With no metadata files present, the detector emits nothing (C-1: packaging-only)."""
    out = ConsoleScriptsDetector().detect(ProjectInput.discover(tmp_path, node_ids=NODE_IDS))
    assert out.nodes == []
    assert out.diagnostics == []


# ---------------------------------------------------------------------------
# Robustness — a metadata file that cannot be parsed is named, not swallowed
# ---------------------------------------------------------------------------


def test_malformed_pyproject_is_diagnosed_not_crashing(tmp_path):
    """A malformed metadata file yields a diagnostic naming it, and other files still read."""
    files = {
        "pyproject.toml": "[project.scripts\nmytool = broken\n",  # unterminated table header
        "setup.cfg": "[options.entry_points]\nconsole_scripts =\n    aux = pkg.cli:other\n",
    }
    out = detect(tmp_path, files)
    # The good file's declaration still resolves...
    assert [e.dst for e in out.edges] == ["python:pkg.cli.other"]
    # ...and the broken one is named in a diagnostic rather than silently ignored.
    parse_diags = [d for d in out.diagnostics if "could not parse pyproject.toml" in d.message]
    assert len(parse_diags) == 1


# ---------------------------------------------------------------------------
# Resolution details — @line collision variants and index-driven resolution
# ---------------------------------------------------------------------------


def test_resolution_follows_a_collision_suffixed_node(tmp_path):
    """A target present only as an `@line`-suffixed collision variant still resolves."""
    node_ids = frozenset({"python:pkg.cli.main@7"})
    out = detect(tmp_path, {"pyproject.toml": '[project.scripts]\nt = "pkg.cli:main"\n'}, node_ids)
    (edge,) = out.edges
    assert edge.dst == "python:pkg.cli.main@7"
    assert edge.is_ambiguous == 0


def test_multiple_collision_variants_flag_ambiguous(tmp_path):
    """Several `@line` variants for one qualname over-approximate: the edge is ambiguous."""
    node_ids = frozenset({"python:pkg.cli.main@7", "python:pkg.cli.main@12"})
    files = {"pyproject.toml": '[project.scripts]\nt = "pkg.cli:main"\n'}
    (edge,) = detect(tmp_path, files, node_ids).edges
    assert edge.dst == "python:pkg.cli.main@7"  # deterministic: the lowest line
    assert edge.is_ambiguous == 1


def test_detection_is_a_pure_function_of_its_inputs(tmp_path):
    """D18: a run over the same metadata and node IDs is identical — no cross-run state."""
    files = {"pyproject.toml": '[project.scripts]\na = "pkg.cli:main"\nb = "pkg.cli:other"\n'}
    proj = project(tmp_path, files)
    first = ConsoleScriptsDetector().detect(proj)
    second = ConsoleScriptsDetector().detect(proj)
    assert [n.id for n in first.nodes] == [n.id for n in second.nodes]
    assert [(e.src, e.dst) for e in first.edges] == [(e.src, e.dst) for e in second.edges]


# ---------------------------------------------------------------------------
# Emitted rows validate against the schema (§4.2)
# ---------------------------------------------------------------------------


def test_emitted_entry_rows_validate(tmp_path):
    """The entry node and its `calls` edge conform to §4.2 (AC-22.1, AC-23.2)."""
    out = detect(tmp_path, {"pyproject.toml": '[project.scripts]\nmytool = "pkg.cli:main"\n'})
    (entry,) = out.nodes
    assert is_valid_node_id(entry.id)
    assert entry.file_path == "pyproject.toml"
    target = NodeRow(id="python:pkg.cli.main", kind="function", name="main", language="python")
    fragment = GraphFragment(
        file=FileRecord(path="pkg/cli.py", content_hash="0" * 64, status="analyzed"),
        nodes=[target, *out.nodes],
        edges=out.edges,
    )
    validate_fragment(fragment)  # raises on any nonconformance


# ---------------------------------------------------------------------------
# Registry integration (AC-8.1) — one appended entry, driven by run_detectors
# ---------------------------------------------------------------------------


def test_the_detector_is_registered(tmp_path):
    """design.md §3.7: `ConsoleScriptsDetector` is in the ordered registry list."""
    assert any(isinstance(d, ConsoleScriptsDetector) for d in registry.DETECTORS)
    assert ConsoleScriptsDetector.name in DETECTORS  # a valid §4.1 `detector` production


def test_run_detectors_drives_the_console_script_detector(tmp_path):
    """The registry hands the project (with its node IDs) to the project-level detector."""
    (tmp_path / "pyproject.toml").write_text('[project.scripts]\nmytool = "pkg.cli:main"\n')
    proj = ProjectInput.discover(tmp_path, node_ids=NODE_IDS)
    out = run_detectors([], proj)
    assert [n.id for n in out.nodes] == ["python:entry:console_script:pkg.cli.main@2"]
