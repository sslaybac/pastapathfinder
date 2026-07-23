"""The `query` CLI subcommands (specs/tasks.md task 3.5).

design.md §3.1 (`cli`), §5.1 (the surface), §5.2 (the shapes `--json` emits), D10 (exit
codes), D20 (dead code recomputed from the index); requirements FR-20 (AC-20.1/20.2),
FR-15-FR-19 (the queries behind the four subcommands), FR-39 (AC-39.2), FR-43.

Two kinds of test here. Most run against a hand-built index, because a query's answer is a
claim about a graph and a graph written by hand is one whose expected answer can be
asserted exactly. One module-scoped test drives a real `analyze` and then queries in a
*fresh process with the source tree moved away* — AC-20.1 is a claim about what survives
the pipeline exiting, which nothing short of that can prove.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import write_tree
from pastapathfinder import cli, queries, reports, runner
from pastapathfinder.index import (
    INDEX_FILENAME,
    IndexIncompatibleError,
    IndexMissingError,
    IndexStoreError,
    full_write,
    open_index,
)
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import (
    DEADCODE_CAVEAT,
    SCHEMA_VERSION,
    EdgeRow,
    FileRecord,
    GraphFragment,
    NodeRow,
)

META = {
    "tool_version": "0.1.0",
    "engine": "stub",
    "engine_version": "0",
    "root_path": "/srv/target",
    "created_at": "2026-07-23T09:00:00+00:00",
    "run_id": "88888888-9999-aaaa-bbbb-cccccccccccc",
}

APP = "pkg/app.py"

# design.md §5.2's field sets, transcribed. The tests below assert against these rather
# than against whatever the serializer happens to produce, so a field added or renamed on
# either side of the API contract fails here.
ENTRY_POINT_FIELDS = {"id", "name", "detector", "target_id", "file_path", "start_line"}
NODE_FIELDS = {
    "id",
    "kind",
    "name",
    "file_path",
    "start_line",
    "end_line",
    "is_external",
    "reachable",
    "attrs",
}
SLICE_FIELDS = {"nodes", "edges", "truncated", "frontier"}
SLICE_EDGE_FIELDS = {"src", "dst", "is_ambiguous"}
DEADCODE_FIELDS = {"format_version", "caveat", "no_entry_points_warning", "unreachable"}


# ---------------------------------------------------------------------------
# Fixture construction
# ---------------------------------------------------------------------------


FILE = NodeRow(id=f"python:file:{APP}", kind="file", name=APP, language="python", file_path=APP)
MODULE = NodeRow(
    id="python:pkg.app.<module>",
    kind="module",
    name="pkg.app",
    language="python",
    file_path=APP,
    start_line=1,
    end_line=40,
    attrs={"python_role": "module_body"},
)
MAIN = NodeRow(
    id="python:pkg.app.main",
    kind="function",
    name="main",
    language="python",
    file_path=APP,
    start_line=10,
    end_line=12,
)
HELPER = NodeRow(
    id="python:pkg.app.helper",
    kind="function",
    name="helper",
    language="python",
    file_path=APP,
    start_line=20,
    end_line=22,
)
ORPHAN = NodeRow(
    id="python:pkg.app.orphan",
    kind="function",
    name="orphan",
    language="python",
    file_path=APP,
    start_line=30,
    end_line=32,
)
EXT = NodeRow(
    id="python:os.path.join",
    kind="function",
    name="join",
    language="python",
    is_external=1,
)
ENTRY = NodeRow(
    id="python:entry:main_block:pkg.app@38",
    kind="entry_point",
    name="pkg.app:__main__",
    language="python",
    file_path=APP,
    start_line=38,
    end_line=38,
    attrs={"detector": "main_block"},
)
ENTRY_B = NodeRow(
    id="python:entry:console_script:pkg.app.main@1",
    kind="entry_point",
    name="app",
    language="python",
    file_path="pyproject.toml",
    start_line=1,
    end_line=1,
    attrs={"detector": "console_script"},
)


def _calls(src: NodeRow, dst: NodeRow, *, ambiguous: int = 0) -> EdgeRow:
    return EdgeRow(src=src.id, dst=dst.id, kind="calls", src_file=APP, is_ambiguous=ambiguous)


def _contains(dst: NodeRow) -> EdgeRow:
    return EdgeRow(src=FILE.id, dst=dst.id, kind="contains", src_file=APP)


@pytest.fixture
def indexed(tmp_path: Path) -> Path:
    """An output directory holding an index whose every answer is computable by eye.

    entry(main_block) → <module> → main → helper → os.path.join (external)
    entry(console_script) → main
    orphan: called by nothing, so it is the dead code.
    """
    out = tmp_path / "out"
    out.mkdir()
    nodes = [FILE, MODULE, MAIN, HELPER, ORPHAN, EXT]
    edges = [
        *(_contains(node) for node in (MODULE, MAIN, HELPER, ORPHAN)),
        _calls(MODULE, MAIN),
        _calls(MAIN, HELPER),
        _calls(HELPER, EXT, ambiguous=1),
    ]
    fragment = GraphFragment(
        file=FileRecord(APP, hashlib.sha256(APP.encode()).hexdigest(), "analyzed"),
        nodes=nodes,
        edges=edges,
    )
    with full_write(out / INDEX_FILENAME, META) as store:
        store.write_fragments([fragment])
        store.write_rows([ENTRY, ENTRY_B], [_calls(ENTRY, MODULE), _calls(ENTRY_B, MAIN)])
    with open_index(out / INDEX_FILENAME) as index:
        queries.reachability(index)
    return out


def call(capsys, *argv: str) -> tuple[int, str, str]:
    """Run one CLI invocation in-process; return `(exit code, stdout, stderr)`."""
    try:
        code = cli.main(list(argv))
    except SystemExit as exit_:  # pragma: no cover - usage errors are tested elsewhere
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def failing(capsys, *argv: str) -> tuple[str, str]:
    """Run an invocation expected to fail through `main()`'s trap; return stdout/stderr."""
    code, out, err = call(capsys, *argv)
    assert code == cli.EXIT_FAILURE
    return out, err


# ---------------------------------------------------------------------------
# The four subcommands answer from the index (FR-15-FR-20)
# ---------------------------------------------------------------------------


def test_entry_points_lists_every_entry_sorted_by_id(capsys, indexed):
    code, out, _ = call(capsys, "query", "entry-points", "--out", str(indexed), "--json")
    assert code == cli.EXIT_SUCCESS
    document = json.loads(out)
    assert [entry["id"] for entry in document["entry_points"]] == sorted(
        [ENTRY.id, ENTRY_B.id]
    )  # §5.2: sorted by id
    by_id = {entry["id"]: entry for entry in document["entry_points"]}
    assert by_id[ENTRY.id]["detector"] == "main_block"
    assert by_id[ENTRY.id]["target_id"] == MODULE.id
    assert by_id[ENTRY_B.id]["target_id"] == MAIN.id


def test_entry_points_renders_for_a_human(capsys, indexed):
    code, out, _ = call(capsys, "query", "entry-points", "--out", str(indexed))
    assert code == cli.EXIT_SUCCESS
    assert "Entry points: 2" in out
    assert ENTRY.id in out and MODULE.id in out


def test_zero_entry_points_is_stated_explicitly_not_shown_as_an_empty_list(capsys, tmp_path):
    """EC-9: the library case is normal, and FR-17 is the alternative the user needs told."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    fragment = GraphFragment(
        file=FileRecord(APP, hashlib.sha256(APP.encode()).hexdigest(), "analyzed"),
        nodes=[FILE, MAIN],
        edges=[_contains(MAIN)],
    )
    with full_write(out_dir / INDEX_FILENAME, META) as store:
        store.write_fragments([fragment])

    code, out, _ = call(capsys, "query", "entry-points", "--out", str(out_dir))
    assert code == cli.EXIT_SUCCESS
    assert "none detected" in out
    assert "query slice" in out  # FR-17: slice from any function instead


def test_forward_slice_answers_the_flagship_question(capsys, indexed):
    """FR-15/AC-15.1 through the CLI: the subgraph, not the program."""
    code, out, _ = call(
        capsys,
        "query",
        "slice",
        "--from",
        MAIN.id,
        "--direction",
        "forward",
        "--out",
        str(indexed),
        "--json",
    )
    assert code == cli.EXIT_SUCCESS
    document = json.loads(out)
    assert {node["id"] for node in document["nodes"]} == {MAIN.id, HELPER.id, EXT.id}
    assert ORPHAN.id not in out
    assert document["truncated"] is False
    assert document["frontier"] == []


def test_backward_slice_answers_who_reaches_this(capsys, indexed):
    """FR-16/AC-16.1 through the CLI."""
    code, out, _ = call(
        capsys,
        "query",
        "slice",
        "--from",
        HELPER.id,
        "--direction",
        "backward",
        "--out",
        str(indexed),
        "--json",
    )
    assert code == cli.EXIT_SUCCESS
    document = json.loads(out)
    assert {node["id"] for node in document["nodes"]} == {
        HELPER.id,
        MAIN.id,
        MODULE.id,
        ENTRY.id,
        ENTRY_B.id,
    }


def test_an_empty_slice_is_a_successful_answer(capsys, indexed):
    """AC-15.2: no outgoing calls is a result, presented as such — never an error."""
    code, out, _ = call(
        capsys,
        "query",
        "slice",
        "--from",
        ORPHAN.id,
        "--direction",
        "forward",
        "--out",
        str(indexed),
    )
    assert code == cli.EXIT_SUCCESS
    assert "No outgoing calls." in out


def test_slice_honors_max_nodes_and_shows_the_bound(capsys, indexed):
    """AC-28.2 on the CLI: bounded visibly — the flag, the truncation, and the frontier."""
    code, out, _ = call(
        capsys,
        "query",
        "slice",
        "--from",
        ENTRY.id,
        "--direction",
        "forward",
        "--max-nodes",
        "2",
        "--out",
        str(indexed),
        "--json",
    )
    assert code == cli.EXIT_SUCCESS
    document = json.loads(out)
    assert len(document["nodes"]) == 2
    assert document["truncated"] is True
    assert document["frontier"]

    _, rendered, _ = call(
        capsys,
        "query",
        "slice",
        "--from",
        ENTRY.id,
        "--direction",
        "forward",
        "--max-nodes",
        "2",
        "--out",
        str(indexed),
    )
    assert "Truncated" in rendered
    assert "--max-nodes" in rendered


def test_max_nodes_defaults_to_the_single_definition_site(capsys, indexed, monkeypatch):
    """§8-O2: omitting the flag resolves to `queries.SLICE_MAX_NODES`, not a second copy."""
    seen: list[int] = []
    real = queries.slice

    def spy(index, node_id, direction=queries.FORWARD, max_nodes=queries.SLICE_MAX_NODES):
        seen.append(max_nodes)
        return real(index, node_id, direction, max_nodes)

    monkeypatch.setattr(queries, "slice", spy)
    call(
        capsys, "query", "slice", "--from", MAIN.id, "--direction", "forward", "--out", str(indexed)
    )
    assert seen == [queries.SLICE_MAX_NODES]


def test_node_reports_one_node(capsys, indexed):
    code, out, _ = call(capsys, "query", "node", HELPER.id, "--out", str(indexed), "--json")
    assert code == cli.EXIT_SUCCESS
    document = json.loads(out)
    assert document["id"] == HELPER.id
    assert document["kind"] == "function"
    assert document["file_path"] == APP
    assert document["reachable"] == 1


def test_node_renders_an_external_leaf_as_unanalyzed(capsys, indexed):
    """AC-37.2/FR-36: an external node has no span, and the rendering says why."""
    code, out, _ = call(capsys, "query", "node", EXT.id, "--out", str(indexed))
    assert code == cli.EXIT_SUCCESS
    assert "external — not analyzed" in out


def test_dead_code_reports_the_unreachable_functions_with_the_caveat(capsys, indexed):
    """AC-19.1/19.2: grouped by file, and the caveat verbatim in both forms."""
    code, out, _ = call(capsys, "query", "dead-code", "--out", str(indexed), "--json")
    assert code == cli.EXIT_SUCCESS
    document = json.loads(out)
    assert document["unreachable"] == [
        {
            "file": APP,
            "functions": [{"id": ORPHAN.id, "name": "orphan", "start_line": 30}],
        }
    ]
    assert document["caveat"] == DEADCODE_CAVEAT
    assert document["no_entry_points_warning"] is False

    _, rendered, _ = call(capsys, "query", "dead-code", "--out", str(indexed))
    assert DEADCODE_CAVEAT in rendered


def test_dead_code_is_recomputed_from_the_index_not_read_from_the_report(capsys, indexed):
    """D20: the answer is correct with no `<out>/reports/` directory in existence at all."""
    assert not (indexed / reports.REPORTS_DIRNAME).exists()
    code, out, _ = call(capsys, "query", "dead-code", "--out", str(indexed), "--json")
    assert code == cli.EXIT_SUCCESS
    assert json.loads(out)["unreachable"][0]["functions"][0]["id"] == ORPHAN.id


def test_dead_code_warns_when_no_entry_points_were_detected(capsys, tmp_path):
    """AC-19.3: the finding is qualified, never presented as a dead codebase."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    fragment = GraphFragment(
        file=FileRecord(APP, hashlib.sha256(APP.encode()).hexdigest(), "analyzed"),
        nodes=[FILE, ORPHAN],
        edges=[_contains(ORPHAN)],
    )
    with full_write(out_dir / INDEX_FILENAME, META) as store:
        store.write_fragments([fragment])
    with open_index(out_dir / INDEX_FILENAME) as index:
        queries.reachability(index)

    code, out, _ = call(capsys, "query", "dead-code", "--out", str(out_dir), "--json")
    assert code == cli.EXIT_SUCCESS
    assert json.loads(out)["no_entry_points_warning"] is True

    _, rendered, _ = call(capsys, "query", "dead-code", "--out", str(out_dir))
    assert "no entry points were detected" in rendered
    assert DEADCODE_CAVEAT in rendered


# ---------------------------------------------------------------------------
# `--json` is the §5.2 surface, field for field
# ---------------------------------------------------------------------------


def test_json_documents_carry_exactly_the_5_2_fields(capsys, indexed):
    """The §5.2 shapes are the contract the viewer's API also serves (task 5.1)."""
    _, entry_points, _ = call(capsys, "query", "entry-points", "--out", str(indexed), "--json")
    document = json.loads(entry_points)
    assert set(document) == {"entry_points"}
    assert all(set(entry) == ENTRY_POINT_FIELDS for entry in document["entry_points"])

    _, node, _ = call(capsys, "query", "node", MAIN.id, "--out", str(indexed), "--json")
    assert set(json.loads(node)) == NODE_FIELDS

    _, sliced, _ = call(
        capsys,
        "query",
        "slice",
        "--from",
        MAIN.id,
        "--direction",
        "forward",
        "--out",
        str(indexed),
        "--json",
    )
    document = json.loads(sliced)
    assert set(document) == SLICE_FIELDS
    assert all(set(row) == NODE_FIELDS for row in document["nodes"])
    assert all(set(edge) == SLICE_EDGE_FIELDS for edge in document["edges"])
    assert any(edge["is_ambiguous"] == 1 for edge in document["edges"])  # FR-40 survives

    _, dead, _ = call(capsys, "query", "dead-code", "--out", str(indexed), "--json")
    assert set(json.loads(dead)) == DEADCODE_FIELDS


def test_the_cli_emits_exactly_what_the_shared_serializer_produces(capsys, indexed):
    """One code path with the viewer's API (task 5.1): the CLI adds no field and drops none.

    The other half of that cross-check — the running server's response — belongs to task
    5.1; this asserts the CLI side is the serializer's own output and nothing else, so the
    comparison there is against a fixed point.
    """
    with open_index(indexed / INDEX_FILENAME, read_only=True) as index:
        expected = {
            ("query", "entry-points"): queries.entry_points_json(queries.entry_points(index)),
            ("query", "node", MAIN.id): queries.node_json(queries.node(index, MAIN.id)),
            ("query", "dead-code"): queries.dead_code_json(queries.dead_code(index)),
        }
    for argv, payload in expected.items():
        _, out, _ = call(capsys, *argv, "--out", str(indexed), "--json")
        assert json.loads(out) == payload


def test_json_output_is_deterministic(capsys, indexed):
    """D12/FR-44's posture at the query surface: same question, same bytes."""
    argv = ("query", "slice", "--from", ENTRY.id, "--direction", "forward", "--out", str(indexed))
    _, first, _ = call(capsys, *argv, "--json")
    _, second, _ = call(capsys, *argv, "--json")
    assert first == second


# ---------------------------------------------------------------------------
# Failure paths (AC-20.2, AC-39.2, AC-16.2, AC-17.2; D10)
# ---------------------------------------------------------------------------


def test_a_missing_index_names_the_problem_and_the_remedy(capsys, tmp_path):
    """AC-20.2, first half."""
    _, err = failing(capsys, "query", "entry-points", "--out", str(tmp_path))
    assert str(tmp_path / INDEX_FILENAME) in err
    assert "analyze" in err


def test_an_unreadable_index_names_the_problem_and_the_remedy(capsys, tmp_path):
    """AC-20.2, second half: a file that is not a database this build can read."""
    (tmp_path / INDEX_FILENAME).write_bytes(b"this is not a database at all")
    _, err = failing(capsys, "query", "entry-points", "--out", str(tmp_path))
    assert str(tmp_path / INDEX_FILENAME) in err
    assert "analyze" in err


def test_an_incompatible_index_is_refused_by_version(capsys, indexed):
    """AC-39.2: the refusal names the version found and the version supported."""
    with sqlite3.connect(indexed / INDEX_FILENAME) as connection:
        connection.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    _, err = failing(capsys, "query", "entry-points", "--out", str(indexed))
    assert "'99'" in err
    assert f"'{SCHEMA_VERSION}'" in err


def test_an_unknown_node_id_is_named(capsys, indexed):
    """AC-16.2 at the CLI: the identifier appears in the error."""
    _, err = failing(capsys, "query", "node", "python:pkg.app.nonesuch", "--out", str(indexed))
    assert "python:pkg.app.nonesuch" in err


def test_a_file_node_is_refused_as_not_sliceable(capsys, indexed):
    """AC-17.2: the error names the kind, so it cannot read as an empty result."""
    _, err = failing(
        capsys,
        "query",
        "slice",
        "--from",
        FILE.id,
        "--direction",
        "forward",
        "--out",
        str(indexed),
    )
    assert "file" in err
    assert "not sliceable" in err


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        pytest.param(
            ("query", "node", "python:pkg.app.nonesuch"),
            queries.ERROR_UNKNOWN_NODE,
            id="unknown_node",
        ),
        pytest.param(
            ("query", "slice", "--from", "python:file:pkg/app.py", "--direction", "forward"),
            queries.ERROR_NOT_SLICEABLE,
            id="not_sliceable",
        ),
    ],
)
def test_json_failures_carry_the_5_2_error_body(capsys, indexed, argv, code):
    """§5.2: `{error: {code, message}}` on stdout, and D10's one line still on stderr."""
    out, err = failing(capsys, *argv, "--out", str(indexed), "--json")
    body = json.loads(out)
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert err.startswith("pastapathfinder: error: ")


@pytest.mark.parametrize(
    ("prepare", "code"),
    [
        pytest.param(lambda path: None, queries.ERROR_INDEX_MISSING, id="index_missing"),
        pytest.param(
            lambda path: path.write_bytes(b"nope"),
            queries.ERROR_INDEX_INCOMPATIBLE,
            id="index_incompatible",
        ),
    ],
)
def test_json_store_failures_carry_the_5_2_error_body(capsys, tmp_path, prepare, code):
    """EC-13's two store failures, classified for a machine reader as §5.2 requires."""
    prepare(tmp_path / INDEX_FILENAME)
    out, _ = failing(capsys, "query", "dead-code", "--out", str(tmp_path), "--json")
    assert json.loads(out)["error"]["code"] == code


def test_every_5_2_error_code_is_produced_by_some_failure():
    """The vocabulary is exhaustive and the mapping total (no unclassifiable query error)."""
    produced = {
        queries.error_code(queries.UnknownNodeError("python:x")),
        queries.error_code(queries.NotSliceableError("python:file:x.py", "file")),
        queries.error_code(IndexMissingError("no index")),
        queries.error_code(IndexIncompatibleError(Path("/x"), "99")),
        queries.error_code(IndexStoreError("unopenable")),
    }
    assert produced == set(queries.ERROR_CODES)
    with pytest.raises(ValueError, match="not a §5.2 query failure"):
        queries.error_code(RuntimeError("something else"))


def test_query_errors_exit_two_in_a_real_process(indexed):
    """AC-43.3/D10 as a process exit code, not an in-process return value."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pastapathfinder",
            "query",
            "node",
            "python:nope",
            "--out",
            str(indexed),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == cli.EXIT_FAILURE
    assert len(result.stderr.strip().splitlines()) == 1


# ---------------------------------------------------------------------------
# AC-20.1 — answering from the index alone, after the pipeline is gone
# ---------------------------------------------------------------------------

RUN_TREE = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper():\n    return 1\n\n\ndef never_called():\n    return 2\n",
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

MAIN_ENTRY = "python:entry:main_block:pkg.app@8"


@pytest.fixture(scope="module")
def analyzed(tmp_path_factory):
    """One real `analyze`, then the source tree moved away and the process gone.

    Module-scoped because it drives a cold engine build. Moving the tree rather than
    deleting it keeps the failure mode honest: a query that secretly re-read a source file
    would fail on the path, not quietly succeed against the original bytes.
    """
    base = tmp_path_factory.mktemp("query-cli")
    root = write_tree(base / "codebase", RUN_TREE)
    out = base / "out"
    out.mkdir()
    result = runner.run_analysis(
        root,
        out=out,
        progress=ProgressSink(io.StringIO(), interval=0.0),
        stdout=io.StringIO(),
    )
    shutil.move(root, base / "moved-away")
    assert not root.exists()
    return result


def query(out_dir: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run a query in a fresh process — the only way to assert "after the pipeline exits"."""
    return subprocess.run(
        [sys.executable, "-m", "pastapathfinder", "query", *argv, "--out", str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_query_answers_after_analyze_with_the_source_tree_gone(analyzed):
    """AC-20.1: all four subcommands, a fresh process, and no codebase to fall back on."""
    out_dir = analyzed.out_dir

    entry_points = query(out_dir, "entry-points", "--json")
    assert entry_points.returncode == cli.EXIT_SUCCESS, entry_points.stderr
    entries = json.loads(entry_points.stdout)["entry_points"]
    assert [entry["id"] for entry in entries] == [MAIN_ENTRY]
    assert entries[0]["target_id"] == "python:pkg.app.<module>"

    sliced = query(out_dir, "slice", "--from", MAIN_ENTRY, "--direction", "forward", "--json")
    assert sliced.returncode == cli.EXIT_SUCCESS, sliced.stderr
    reached = {node["id"] for node in json.loads(sliced.stdout)["nodes"]}
    assert {"python:pkg.app.<module>", "python:pkg.app.main", "python:pkg.util.helper"} <= reached
    assert "python:pkg.util.never_called" not in reached

    backward = query(
        out_dir, "slice", "--from", "python:pkg.util.helper", "--direction", "backward", "--json"
    )
    assert backward.returncode == cli.EXIT_SUCCESS, backward.stderr
    assert MAIN_ENTRY in {node["id"] for node in json.loads(backward.stdout)["nodes"]}

    node = query(out_dir, "node", "python:pkg.util.never_called", "--json")
    assert node.returncode == cli.EXIT_SUCCESS, node.stderr
    assert json.loads(node.stdout)["reachable"] == 0

    dead = query(out_dir, "dead-code", "--json")
    assert dead.returncode == cli.EXIT_SUCCESS, dead.stderr
    assert json.loads(dead.stdout)["unreachable"] == [
        {
            "file": "pkg/util.py",
            "functions": [
                {"id": "python:pkg.util.never_called", "name": "never_called", "start_line": 5}
            ],
        }
    ]


def test_dead_code_json_is_the_report_shape_minus_the_volatile_block(analyzed):
    """D20/§5.4: `deadcode.json` without `run*`, recomputed rather than re-read."""
    written = json.loads(analyzed.report_paths[reports.DEADCODE_REPORT].read_text(encoding="utf-8"))
    answered = json.loads(query(analyzed.out_dir, "dead-code", "--json").stdout)
    assert set(written) - set(answered) == {reports.RUN_BLOCK_KEY}
    assert {
        key: value for key, value in written.items() if key != reports.RUN_BLOCK_KEY
    } == answered


def test_the_out_default_is_derived_from_the_working_directory(tmp_path, monkeypatch):
    """§5.1: a query run inside the analyzed codebase finds its index without `--out`.

    The derivation takes a codebase path and a query is given none, so the working
    directory is the root — which is what makes `analyze .` and a later bare `query` in the
    same directory agree. `XDG_DATA_HOME` is redirected so the assertion is against a
    temporary tree rather than the developer's own data directory.
    """
    root = write_tree(tmp_path / "codebase", {"pkg/lib.py": "def thing():\n    return 1\n"})
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    analyze = subprocess.run(
        [sys.executable, "-m", "pastapathfinder", "analyze", "."],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
        env=_env(),
    )
    assert analyze.returncode == cli.EXIT_SUCCESS, analyze.stderr
    derived = runner.derive_out_dir(root.resolve())
    assert derived.is_relative_to(tmp_path / "xdg")
    assert (derived / INDEX_FILENAME).is_file()

    answered = subprocess.run(
        [sys.executable, "-m", "pastapathfinder", "query", "entry-points", "--json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
        env=_env(),
    )
    assert answered.returncode == cli.EXIT_SUCCESS, answered.stderr
    assert json.loads(answered.stdout) == {"entry_points": []}


def test_the_out_default_follows_the_configured_output_directory(tmp_path, monkeypatch):
    """§5.5: a codebase that redirects `[output] dir` is queried where it was analyzed."""
    root = write_tree(
        tmp_path / "codebase",
        {".pastapathfinder.toml": f'[output]\ndir = "{tmp_path / "elsewhere"}"\n'},
    )
    monkeypatch.chdir(root)
    assert cli.query_out_dir(None) == (tmp_path / "elsewhere").resolve()
    assert cli.query_out_dir(str(tmp_path / "explicit")) == (tmp_path / "explicit").resolve()


def _env() -> dict[str, str]:
    """The parent environment, so a subprocess keeps `PATH` and the virtualenv."""
    return dict(os.environ)
