"""Task 3.1 — the detector registry, error isolation, and the `__main__` detector.

design.md §3.7, §4.1, D14, D16, D18; requirements FR-8 (AC-8.1, AC-8.2), FR-9 (AC-9.1,
AC-9.2).

The registry runs detectors over stdlib-`ast` trees and the packaging metadata set, in a
fixed order, each `detect()` call isolated. `main_block` emits an `entry_point` targeting
the module-body node for every `if __name__ == "__main__":` guard.
"""

from __future__ import annotations

import ast

import pytest

from pastapathfinder.adapters.python import normalize
from pastapathfinder.detectors import registry
from pastapathfinder.detectors.base import (
    DetectorOutput,
    ModuleDetector,
    ModuleInput,
    ProjectDetector,
    ProjectInput,
    entry_node_id,
    entry_point,
    import_table,
    is_package,
)
from pastapathfinder.detectors.main_block import MainBlockDetector
from pastapathfinder.detectors.registry import run_detectors
from pastapathfinder.schema import (
    DETECTORS,
    FileRecord,
    GraphFragment,
    NodeRow,
    is_valid_node_id,
    validate_fragment,
)


def module_input(relpath: str, source: str) -> ModuleInput:
    """A `ModuleInput` for `source`, parsed with stdlib `ast` exactly as the registry would."""
    return ModuleInput.build(relpath, ast.parse(source))


def module_body_id(relpath: str) -> str:
    """The module-body node id `extract.py` writes for `relpath` — the `main_block` target."""
    return normalize.code_node_id(normalize.module_body_qualname(normalize.module_name(relpath)))


# ---------------------------------------------------------------------------
# main_block — FR-9
# ---------------------------------------------------------------------------

GUARD = """
def main():
    pass

if __name__ == "__main__":
    main()
"""


def test_a_main_guard_yields_an_entry_targeting_the_module_body():
    """AC-9.1: a `__main__` block yields an entry-point node referencing that module."""
    out = MainBlockDetector().detect(module_input("pkg/cli.py", GUARD))

    assert len(out.nodes) == 1
    (entry,) = out.nodes
    (edge,) = out.edges
    assert entry.kind == "entry_point"
    assert entry.attrs["detector"] == "main_block"
    assert entry.id == "python:entry:main_block:pkg.cli@5"
    assert edge.src == entry.id
    assert edge.kind == "calls"
    # The target is the module-body node id `normalize` produces, not a re-derivation.
    assert edge.dst == module_body_id("pkg/cli.py") == "python:pkg.cli.<module>"


def test_a_main_guard_carries_its_source_span():
    """AC-37.1 for entry nodes: an entry carries relative path and start/end lines."""
    (entry,) = MainBlockDetector().detect(module_input("cli.py", GUARD)).nodes
    assert entry.file_path == "cli.py"
    assert entry.start_line == 5
    assert entry.end_line == 6


def test_both_operand_orders_are_recognized():
    """§3.7: `__name__ == "__main__"` in either order is a guard."""
    swapped = 'if "__main__" == __name__:\n    run()\n'
    (entry,) = MainBlockDetector().detect(module_input("m.py", swapped)).nodes
    assert entry.id == "python:entry:main_block:m@1"


def test_no_guard_yields_no_entry():
    """AC-9.1 negative: a module without a `__main__` block yields none."""
    out = MainBlockDetector().detect(module_input("m.py", "def f():\n    return 1\n"))
    assert out.nodes == []
    assert out.edges == []


@pytest.mark.parametrize(
    "source",
    [
        'if __name__ is "__main__":\n    run()\n',  # `is`, not `==`
        'if __name__ == "__main__" == mode:\n    run()\n',  # chained compare
        'if __name__ == "main":\n    run()\n',  # wrong literal
        'if __module__ == "__main__":\n    run()\n',  # wrong name
        'if name == "__main__":\n    run()\n',  # not the dunder
    ],
)
def test_near_misses_are_not_guards(source):
    """Only `__name__ == "__main__"` is a guard; look-alikes are not (§3.7)."""
    assert MainBlockDetector().detect(module_input("m.py", source)).nodes == []


def test_a_file_that_fails_to_parse_never_reaches_a_detector(tmp_path):
    """AC-9.2: a file that failed to parse emits no entry node.

    Detectors receive stdlib-`ast` trees, and a file that fails stdlib parse produces no
    tree — it becomes a `parse_error` skip in the adapter (design.md §3.5) and is not among
    the `ModuleInput`s the registry runs over. The boundary is `ModuleInput.build` itself:
    it cannot be constructed for an unparsable file, so no entry can be emitted for one.
    """
    with pytest.raises(SyntaxError):
        module_input("broken.py", "def broken(:\n    pass\n")

    # The good file's entry is emitted; the broken file, absent from the input, contributes
    # nothing — there is no path by which an unparsed file appears in the output.
    out = run_detectors(modules(("good.py", GUARD)), empty_project(tmp_path))
    assert [n.id for n in out.nodes] == ["python:entry:main_block:good@5"]


def test_multiple_guards_yield_distinct_entries():
    """FR-9 emits one entry per guard; the `@line` suffix keeps their ids distinct."""
    source = 'if __name__ == "__main__":\n    a()\nx = 1\nif __name__ == "__main__":\n    b()\n'
    out = MainBlockDetector().detect(module_input("m.py", source))
    ids = sorted(node.id for node in out.nodes)
    assert ids == ["python:entry:main_block:m@1", "python:entry:main_block:m@4"]


def test_the_detector_reads_stdlib_ast_not_a_mypy_tree():
    """D14: a file the engine produced no tree for still parses under `ast` and detects.

    The detector is handed a stdlib parse and only a stdlib parse — there is no mypy object
    anywhere in its input — so its result cannot depend on whether semantic analysis
    succeeded for the file.
    """
    tree = ast.parse(GUARD)  # a plain stdlib tree, as on a warm build with no engine tree
    (entry,) = MainBlockDetector().detect(ModuleInput.build("svc/run.py", tree)).nodes
    assert entry.id == "python:entry:main_block:svc.run@5"


def test_a_digit_initial_module_still_yields_a_wellformed_id():
    """D22: a migration-style filename derives a legal module and a legal entry id."""
    (entry,) = MainBlockDetector().detect(module_input("app/0001_initial.py", GUARD)).nodes
    assert is_valid_node_id(entry.id)
    assert entry.id == "python:entry:main_block:app.0001_initial@5"


# ---------------------------------------------------------------------------
# Emitted rows validate against the schema (§4.2)
# ---------------------------------------------------------------------------


def test_emitted_entry_rows_validate():
    """The entry node and its `calls` edge conform to §4.2 (AC-22.1, AC-23.2).

    The module-body node lives in the same file's fragment, so a fragment carrying both the
    body node `extract.py` would emit and the entry `main_block` emits validates as a whole.
    """
    relpath = "pkg/cli.py"
    out = MainBlockDetector().detect(module_input(relpath, GUARD))
    body = NodeRow(
        id=module_body_id(relpath),
        kind="module",
        name="cli",
        language="python",
        file_path=relpath,
        start_line=1,
        end_line=6,
        attrs={"python_role": "module_body"},
    )
    fragment = GraphFragment(
        file=FileRecord(path=relpath, content_hash="0" * 64, status="analyzed"),
        nodes=[body, *out.nodes],
        edges=out.edges,
    )
    validate_fragment(fragment)  # raises FragmentValidationError on any nonconformance


def test_entry_node_id_rejects_an_unknown_detector():
    """§4.1's `detector` production is closed; a stray name is a programming error."""
    with pytest.raises(ValueError, match="unknown detector"):
        entry_node_id("route_typo", "pkg.mod", 1)


def test_every_registered_detector_names_a_known_detector_production():
    """Each detector's `name` must be a §4.1 `detector` value, so its ids are parseable."""
    for detector in registry.DETECTORS:
        assert detector.name in DETECTORS


# ---------------------------------------------------------------------------
# The per-module import table — D18
# ---------------------------------------------------------------------------


def test_is_package_recognizes_init_files():
    assert is_package("pkg/__init__.py")
    assert is_package("__init__.py")
    assert not is_package("pkg/mod.py")


def test_import_table_maps_every_binding_form():
    """`import a.b.c` binds `a`; `as` binds the alias; `from x import y` binds `y`."""
    source = (
        "import os\n"
        "import a.b.c\n"
        "import d.e as de\n"
        "from pkg import thing\n"
        "from pkg import other as o\n"
    )
    table = import_table(ast.parse(source), "app.mod", in_package=False)
    assert table == {
        "os": "os",
        "a": "a",
        "de": "d.e",
        "thing": "pkg.thing",
        "o": "pkg.other",
    }


def test_import_table_resolves_relative_imports_in_the_path_namespace():
    """A relative import resolves against the path-derived module (D18, matching node ids)."""
    source = "from . import views\nfrom .models import Thing\n"
    table = import_table(ast.parse(source), "app.urls", in_package=False)
    assert table == {"views": "app.views", "Thing": "app.models.Thing"}


def test_import_table_covers_deferred_imports_inside_functions():
    """Every scope, not only the module body — deferred imports break real cycles (D18)."""
    source = "def handler():\n    from pkg import view\n    return view\n"
    table = import_table(ast.parse(source), "app.mod", in_package=False)
    assert table == {"view": "pkg.view"}


def test_import_table_drops_a_name_bound_two_ways():
    """An ambiguous binding is dropped rather than guessed (AC-11.3/AC-10.2)."""
    source = "from a import thing\nfrom b import thing\n"
    assert import_table(ast.parse(source), "m", in_package=False) == {}


def test_import_table_ignores_star_imports():
    """A star import binds names the table cannot enumerate, so it claims none."""
    assert import_table(ast.parse("from pkg import *\n"), "m", in_package=False) == {}


def test_module_input_build_populates_the_import_table():
    """The shape's import table is derived from its own tree (D18)."""
    mod = module_input("app/urls.py", "from . import views\n")
    assert mod.import_table == {"views": "app.views"}


# ---------------------------------------------------------------------------
# ProjectInput.discover
# ---------------------------------------------------------------------------


def test_project_input_discovers_present_metadata_files(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    project = ProjectInput.discover(tmp_path)
    assert set(project.metadata_files) == {"pyproject.toml", "setup.py"}
    assert project.metadata_files["pyproject.toml"] == tmp_path / "pyproject.toml"


def test_project_input_discover_on_a_bare_root_finds_nothing(tmp_path):
    assert ProjectInput.discover(tmp_path).metadata_files == {}


# ---------------------------------------------------------------------------
# run_detectors — FR-8, D18
# ---------------------------------------------------------------------------


def modules(*sources: tuple[str, str]) -> list[ModuleInput]:
    return [module_input(relpath, src) for relpath, src in sources]


def empty_project(tmp_path) -> ProjectInput:
    return ProjectInput.discover(tmp_path)


def test_run_detectors_emits_entries_over_all_modules(tmp_path):
    """The default registry runs `main_block` over every analyzed module (FR-8)."""
    mods = modules(("a.py", GUARD), ("b.py", "x = 1\n"), ("c.py", GUARD))
    out = run_detectors(mods, empty_project(tmp_path))
    ids = sorted(node.id for node in out.nodes)
    assert ids == ["python:entry:main_block:a@5", "python:entry:main_block:c@5"]


def test_run_detectors_is_a_pure_function_of_its_inputs(tmp_path):
    """D18: a run over the same trees and metadata is identical — no cross-run state."""
    mods = modules(("a.py", GUARD), ("b.py", GUARD))
    project = empty_project(tmp_path)
    first = run_detectors(mods, project)
    second = run_detectors(mods, project)
    assert [n.id for n in first.nodes] == [n.id for n in second.nodes]
    assert [(e.src, e.dst) for e in first.edges] == [(e.src, e.dst) for e in second.edges]
    assert first.diagnostics == second.diagnostics


# --- AC-8.1: adding a detector is one new module + one registry entry ---


class _DummyModuleDetector(ModuleDetector):
    """A stand-in for a future detector, defined entirely outside the registry."""

    name = "console_script"  # any valid §4.1 production; the point is it is *new* here

    def __init__(self) -> None:
        self.seen: list[str] = []

    def detect(self, module: ModuleInput) -> DetectorOutput:
        self.seen.append(module.module_path)
        return DetectorOutput()


def test_registering_a_detector_touches_no_existing_detector_or_schema(tmp_path):
    """AC-8.1: a new detector plugs in through the `detectors=` list, editing nothing else."""
    dummy = _DummyModuleDetector()
    mods = modules(("a.py", GUARD), ("b.py", "x = 1\n"))
    out = run_detectors(mods, empty_project(tmp_path), detectors=[*registry.DETECTORS, dummy])
    # The dummy ran over every module...
    assert dummy.seen == ["a.py", "b.py"]
    # ...and the built-in main_block still produced its entry alongside it.
    assert [n.id for n in out.nodes] == ["python:entry:main_block:a@5"]


# --- AC-8.2: one detector failing on one file isolates to that (detector, file) ---


class _ExplodingModuleDetector(ModuleDetector):
    name = "route_flask_fastapi"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def detect(self, module: ModuleInput) -> DetectorOutput:
        self.seen.append(module.module_path)
        if module.module_path == "bad.py":
            raise RuntimeError("boom")
        return DetectorOutput()


def test_a_detector_error_is_isolated_and_recorded(tmp_path):
    """AC-8.2: the failure is a diagnostic naming detector and file; everything else runs."""
    exploding = _ExplodingModuleDetector()
    mods = modules(("good.py", GUARD), ("bad.py", GUARD), ("also.py", GUARD))
    out = run_detectors(mods, empty_project(tmp_path), detectors=[exploding, MainBlockDetector()])

    # The exploding detector still ran over every file, not just up to the failure.
    assert exploding.seen == ["good.py", "bad.py", "also.py"]

    # Exactly one detector_error, naming the detector and the offending file.
    errors = [d for d in out.diagnostics if d.kind == "detector_error"]
    assert len(errors) == 1
    assert errors[0].path == "bad.py"
    assert "route_flask_fastapi" in errors[0].message
    assert "bad.py" in errors[0].message

    # The other detector produced entries for every file, including the one that broke the
    # first detector — the failure did not remove `bad.py` from the run.
    ids = sorted(n.id for n in out.nodes)
    assert ids == [
        "python:entry:main_block:also@5",
        "python:entry:main_block:bad@5",
        "python:entry:main_block:good@5",
    ]


class _ExplodingProjectDetector(ProjectDetector):
    name = "console_script"

    def detect(self, project: ProjectInput) -> DetectorOutput:
        raise RuntimeError("metadata boom")


def test_a_project_detector_error_is_isolated_and_recorded(tmp_path):
    """AC-8.2 for the project shape: the failure names the detector, others still run."""
    mods = modules(("a.py", GUARD))
    out = run_detectors(
        mods,
        empty_project(tmp_path),
        detectors=[_ExplodingProjectDetector(), MainBlockDetector()],
    )
    errors = [d for d in out.diagnostics if d.kind == "detector_error"]
    assert len(errors) == 1
    assert errors[0].path is None
    assert "console_script" in errors[0].message
    assert [n.id for n in out.nodes] == ["python:entry:main_block:a@5"]


def test_a_project_detector_receives_the_metadata_set(tmp_path):
    """A project-level detector is handed the discovered metadata files (FR-10 shape)."""
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    seen: list[str] = []

    class _Recorder(ProjectDetector):
        name = "console_script"

        def detect(self, project: ProjectInput) -> DetectorOutput:
            seen.extend(sorted(project.metadata_files))
            return DetectorOutput()

    run_detectors([], ProjectInput.discover(tmp_path), detectors=[_Recorder()])
    assert seen == ["pyproject.toml"]


def test_entry_point_helper_target_and_attrs():
    """`entry_point()` builds one node and one `calls` edge to `target`, stamping detector."""
    node, edge = entry_point(
        detector="main_block",
        qualname="pkg.mod",
        line=9,
        name="pkg.mod:__main__",
        target="python:pkg.mod.<module>",
        file_path="pkg/mod.py",
        start_line=9,
        end_line=10,
    )
    assert node.id == "python:entry:main_block:pkg.mod@9"
    assert node.attrs == {"detector": "main_block"}
    assert edge.src == node.id
    assert edge.dst == "python:pkg.mod.<module>"
    assert edge.kind == "calls"
    assert edge.src_file is None  # a detector is not a caller file (D18)
