"""CLI parsing, the top-level exception trap, and the exit-code mapping.

design.md §3.1, §5.1, D10; requirements FR-32, FR-43 (AC-43.1-3).

Task 1.1 owns the plumbing, not the behavior behind it: every subcommand handler is
a stub that raises until its implementing task lands, which is exactly the escaped
exception the D10 trap must map to exit 2.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from pastapathfinder import cli

TRACEBACK_MARKER = "Traceback (most recent call last)"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the tool in a real subprocess, so the assertion is on a process exit code."""
    return subprocess.run(
        [sys.executable, "-m", "pastapathfinder", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_exit_codes_are_three_distinct_integers():
    """FR-43: success, partial success, and failure are mutually distinct (D10: 0/1/2)."""
    codes = (cli.EXIT_SUCCESS, cli.EXIT_PARTIAL, cli.EXIT_FAILURE)
    assert codes == (0, 1, 2)
    assert len(set(codes)) == 3


def test_help_exits_zero():
    result = run_cli("--help")
    assert result.returncode == 0
    assert "pastapathfinder" in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="no-subcommand"),
        pytest.param(["nonesuch"], id="unknown-subcommand"),
        pytest.param(["query", "nonesuch"], id="unknown-query-subcommand"),
        pytest.param(["analyze"], id="missing-required-positional"),
        pytest.param(["query", "node"], id="missing-required-node-id"),
        pytest.param(["query", "slice", "--from", "python:pkg.mod.f"], id="missing-direction"),
        pytest.param(["query", "slice", "--direction", "forward"], id="missing-from"),
        pytest.param(
            ["query", "slice", "--from", "python:pkg.mod.f", "--direction", "sideways"],
            id="invalid-direction-choice",
        ),
    ],
)
def test_usage_errors_exit_failure(argv):
    """AC-43.3: argparse's native exit 2 is the failure code (D10)."""
    result = run_cli(*argv)
    assert result.returncode == cli.EXIT_FAILURE


def test_usage_errors_raise_systemexit_from_main():
    """The trap must not swallow argparse's SystemExit and turn it into something else."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["nonesuch"])
    assert excinfo.value.code == cli.EXIT_FAILURE


def test_handler_exception_maps_to_failure_with_one_line():
    """AC-43.3: an exception escaping a handler exits 2 with a one-line message.

    `query entry-points` is the live example: its handler still raises until task 3.5
    fills it in, which is exactly the escaped exception the D10 trap must map.
    """
    result = run_cli("query", "entry-points")
    assert result.returncode == cli.EXIT_FAILURE
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("pastapathfinder: error: ")
    assert TRACEBACK_MARKER not in result.stderr


def test_handler_exception_prints_traceback_only_under_debug():
    result = run_cli("query", "entry-points", "--debug")
    assert result.returncode == cli.EXIT_FAILURE
    assert result.stderr.startswith("pastapathfinder: error: ")
    assert TRACEBACK_MARKER in result.stderr
    assert "NotImplementedError" in result.stderr


def test_arbitrary_exception_is_trapped(monkeypatch, capsys):
    """The mapping is generic, not tied to the stubs' NotImplementedError."""

    def explode(args):
        raise ValueError("boom")

    monkeypatch.setattr(cli, "_handle_analyze", explode)
    assert cli.main(["analyze", "."]) == cli.EXIT_FAILURE
    assert capsys.readouterr().err == "pastapathfinder: error: boom\n"


def test_multiline_exception_message_is_reduced_to_one_line(monkeypatch, capsys):
    def explode(args):
        raise RuntimeError("first line\nsecond line")

    monkeypatch.setattr(cli, "_handle_analyze", explode)
    assert cli.main(["analyze", "."]) == cli.EXIT_FAILURE
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_handler_return_value_is_the_exit_code(monkeypatch):
    monkeypatch.setattr(cli, "_handle_analyze", lambda args: cli.EXIT_PARTIAL)
    assert cli.main(["analyze", "."]) == cli.EXIT_PARTIAL


# --- the design.md §5.1 surface ------------------------------------------------


def _parse(*argv: str):
    return cli.build_parser().parse_args(argv)


def test_analyze_accepts_its_documented_flags(tmp_path):
    args = _parse("analyze", str(tmp_path), "--out", "/o", "--config", "/c.toml", "--full")
    assert (args.root, args.out, args.config) == (tmp_path, Path("/o"), Path("/c.toml"))
    assert args.full is True


def test_query_subcommands_accept_their_documented_flags():
    assert _parse("query", "entry-points", "--json").json is True
    slice_args = _parse(
        "query",
        "slice",
        "--from",
        "python:pkg.mod.f",
        "--direction",
        "backward",
        "--max-nodes",
        "5",
    )
    assert (slice_args.from_node, slice_args.direction, slice_args.max_nodes) == (
        "python:pkg.mod.f",
        "backward",
        5,
    )
    assert _parse("query", "node", "python:pkg.mod.f").node_id == "python:pkg.mod.f"
    assert _parse("query", "dead-code", "--out", "/o").out is not None


def test_max_nodes_defaults_to_none_so_queries_owns_the_bound():
    """design.md §8-O2: the 200-node bound has a single definition site, in queries.py."""
    assert _parse("query", "slice", "--from", "x", "--direction", "forward").max_nodes is None


def test_view_binds_the_default_port():
    assert _parse("view").port == cli.DEFAULT_VIEWER_PORT
    assert _parse("view", "--port", "9000").port == 9000


@pytest.mark.parametrize(
    "argv",
    [
        ["analyze", "."],
        ["query", "entry-points"],
        ["query", "slice", "--from", "x", "--direction", "forward"],
        ["query", "node", "x"],
        ["query", "dead-code"],
        ["view"],
    ],
)
def test_debug_is_accepted_on_every_subcommand(argv):
    """design.md D21: §3.1's top-level trap serves every subcommand, so the flag does too."""
    assert _parse(*argv).debug is False
    assert _parse(*argv, "--debug").debug is True
