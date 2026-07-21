"""Command-line entry point: argument parsing, exception trapping, exit codes.

design.md §3.1 (responsibility and interface), §5.1 (the CLI surface), D10 (exit codes).
Satisfies FR-43 (AC-43.1-3) and FR-32's console script.

The subcommand *behavior* lands in later tasks: `analyze` in task 1.5, the `query`
subcommands in task 3.5, `view` in task 5.1. This module owns the parsing, the
top-level exception trap, and the exit-code plumbing only.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from pastapathfinder import __version__

# D10: three mutually distinct exit codes (FR-43). argparse exits 2 natively on
# usage errors, which places them in the failure category by construction; Python's
# default uncaught-exception exit of 1 can never masquerade as partial success
# because every exception is trapped in main().
EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FAILURE = 2

# design.md §3.11: the viewer binds 127.0.0.1 on this port unless overridden.
DEFAULT_VIEWER_PORT = 8517


def _add_debug(parser: argparse.ArgumentParser) -> None:
    """Attach `--debug` to a subcommand parser.

    design.md D21 (amended 2026-07-21): the traceback-behind-`--debug` behavior is a
    property of §3.1's top-level trap, which serves every subcommand, so the flag is
    accepted on all three and on each `query` sub-subcommand.
    """
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print a full traceback when the run fails (default: a one-line error)",
    )


def _add_out(parser: argparse.ArgumentParser) -> None:
    """Attach `--out`. The default derivation of design.md §5.1 belongs to task 1.5."""
    parser.add_argument(
        "--out",
        metavar="DIR",
        type=Path,
        help="output directory (default: derived from the analyzed root, per design.md §5.1)",
    )


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the structured shapes of design.md §5.2 on stdout",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the design.md §5.1 command surface."""
    parser = argparse.ArgumentParser(
        prog="pastapathfinder",
        description="Find paths through spaghetti code: sliced call traces for Python codebases.",
    )
    parser.add_argument("--version", action="version", version=f"pastapathfinder {__version__}")
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    analyze = subcommands.add_parser(
        "analyze",
        help="analyze a codebase and write the index and reports",
    )
    analyze.add_argument("root", type=Path, help="root folder of the codebase to analyze")
    _add_out(analyze)
    analyze.add_argument(
        "--config",
        metavar="FILE",
        type=Path,
        help="configuration file (default: <root>/.pastapathfinder.toml if present)",
    )
    analyze.add_argument(
        "--full",
        action="store_true",
        help="force a full analysis instead of the automatic incremental path",
    )
    _add_debug(analyze)
    analyze.set_defaults(handler=_handle_analyze)

    query = subcommands.add_parser("query", help="answer questions from an existing index")
    query_subcommands = query.add_subparsers(dest="query_command", metavar="QUERY", required=True)

    entry_points = query_subcommands.add_parser(
        "entry-points", help="list the detected entry points"
    )
    _add_out(entry_points)
    _add_json(entry_points)
    _add_debug(entry_points)
    entry_points.set_defaults(handler=_handle_query_entry_points)

    slice_ = query_subcommands.add_parser(
        "slice", help="trace forward or backward from a node (the flagship query)"
    )
    slice_.add_argument(
        "--from",
        dest="from_node",
        metavar="NODE_ID",
        required=True,
        help="node to trace from",
    )
    slice_.add_argument(
        "--direction",
        choices=("forward", "backward"),
        required=True,
        help="follow callees (forward) or callers (backward)",
    )
    slice_.add_argument(
        "--max-nodes",
        metavar="N",
        type=int,
        help="node budget for the slice (default: the queries.py bound)",
    )
    _add_out(slice_)
    _add_json(slice_)
    _add_debug(slice_)
    slice_.set_defaults(handler=_handle_query_slice)

    node = query_subcommands.add_parser("node", help="show one node's details")
    node.add_argument("node_id", metavar="NODE_ID", help="node to show")
    _add_out(node)
    _add_json(node)
    _add_debug(node)
    node.set_defaults(handler=_handle_query_node)

    dead_code = query_subcommands.add_parser(
        "dead-code", help="report functions no entry point reaches"
    )
    _add_out(dead_code)
    _add_json(dead_code)
    _add_debug(dead_code)
    dead_code.set_defaults(handler=_handle_query_dead_code)

    view = subcommands.add_parser("view", help="serve the local interactive viewer")
    _add_out(view)
    view.add_argument(
        "--port",
        metavar="PORT",
        type=int,
        default=DEFAULT_VIEWER_PORT,
        help=f"port to bind on 127.0.0.1 (default: {DEFAULT_VIEWER_PORT})",
    )
    _add_debug(view)
    view.set_defaults(handler=_handle_view)

    return parser


def _handle_analyze(args: argparse.Namespace) -> int:
    raise NotImplementedError("analyze is not implemented yet (specs/tasks.md task 1.5)")


def _handle_query_entry_points(args: argparse.Namespace) -> int:
    raise NotImplementedError("query entry-points is not implemented yet (specs/tasks.md task 3.5)")


def _handle_query_slice(args: argparse.Namespace) -> int:
    raise NotImplementedError("query slice is not implemented yet (specs/tasks.md task 3.5)")


def _handle_query_node(args: argparse.Namespace) -> int:
    raise NotImplementedError("query node is not implemented yet (specs/tasks.md task 3.5)")


def _handle_query_dead_code(args: argparse.Namespace) -> int:
    raise NotImplementedError("query dead-code is not implemented yet (specs/tasks.md task 3.5)")


def _handle_view(args: argparse.Namespace) -> int:
    raise NotImplementedError("view is not implemented yet (specs/tasks.md task 5.1)")


def _one_line(exc: BaseException) -> str:
    """Reduce an exception to a single line of user-facing text.

    A multi-line message is cut to its first line and says so, rather than silently
    dropping the rest; `--debug` prints the whole thing.
    """
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    first, separator, _ = text.partition("\n")
    if separator:
        return f"{first} (truncated; re-run with --debug for the full error)"
    return first


def main(argv: Sequence[str] | None = None) -> int:
    """Parse `argv`, dispatch, and map any escaped exception to the failure code.

    Usage errors are argparse's own `SystemExit(2)` and are deliberately not caught
    (D10). Every other exception becomes exit 2 with a one-line message on stderr;
    the full traceback appears only under `--debug`.
    """
    args = build_parser().parse_args(argv)
    debug = bool(getattr(args, "debug", False))
    try:
        return args.handler(args)
    except Exception as exc:  # noqa: BLE001 - the D10 top-level trap is deliberate
        print(f"pastapathfinder: error: {_one_line(exc)}", file=sys.stderr)
        if debug:
            traceback.print_exc()
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
