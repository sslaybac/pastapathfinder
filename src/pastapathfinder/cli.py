"""Command-line entry point: argument parsing, exception trapping, exit codes.

design.md §3.1 (responsibility and interface), §5.1 (the CLI surface), §5.2 (the shapes
`--json` emits), D10 (exit codes), D20 (dead code recomputed from the index); requirements
FR-32, FR-43 (AC-43.1-3), FR-20 (AC-20.1/20.2), FR-15-FR-19 (the query surface), FR-39
(AC-39.2).

The subcommand *behavior* lands with the components behind it: `analyze` is wired to
`runner`, the four `query` subcommands to `queries`, and `view` to `viewer.server` in task
5.1. This module owns the parsing, the top-level exception trap, the exit-code
computation, and the human renderings of the query answers.

**A query touches the index and nothing else** (AC-20.1). It does not walk the tree, read a
report, or start an engine: `analyze` wrote everything a query needs, which is what makes
the answers survive a source tree that has since moved or gone. `--json` prints the §5.2
shapes verbatim from `queries`' own serializers, so the CLI and the viewer's API cannot
drift apart; the default rendering is the same data for a human.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pastapathfinder import __version__, queries, reports, runner, viewer
from pastapathfinder.config import load_config
from pastapathfinder.index import INDEX_FILENAME, Index, IndexStoreError, open_index
from pastapathfinder.schema import NodeRow

# D10: three mutually distinct exit codes (FR-43). argparse exits 2 natively on
# usage errors, which places them in the failure category by construction; Python's
# default uncaught-exception exit of 1 can never masquerade as partial success
# because every exception is trapped in main().
EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FAILURE = 2

# design.md §3.11: the viewer binds 127.0.0.1 on this port unless overridden. Read from
# the viewer package, which is its definition site — importing `viewer.server` here would
# pull Flask into every `analyze` and `query` invocation for one integer.
DEFAULT_VIEWER_PORT = viewer.DEFAULT_PORT


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


def exit_code_for(result: runner.RunResult) -> int:
    """Map a run result to its process exit code (design.md §3.1, D10; FR-43).

    0 when the run completed with nothing skipped (AC-43.1), 1 when it completed with at
    least one skipped file (AC-43.2), 2 when it did not complete (AC-43.3). A run that
    did not complete normally raises instead of returning, so the failure branch here is
    the belt to `main()`'s braces.
    """
    if not result.completed:
        return EXIT_FAILURE
    return EXIT_PARTIAL if result.files_skipped else EXIT_SUCCESS


def _handle_analyze(args: argparse.Namespace) -> int:
    result = runner.run_analysis(
        args.root,
        out=args.out,
        config_path=args.config,
        full=args.full,
    )
    return exit_code_for(result)


# ---------------------------------------------------------------------------
# The query subcommands (design.md §5.1; FR-15-FR-20)
# ---------------------------------------------------------------------------


def query_out_dir(requested: Path | str | None) -> Path:
    """Where a query looks for the index: `--out`, else §5.1's derived location.

    `analyze` derives its output directory from the root it was given; a query is given no
    root, so the root is the current working directory — the codebase the user is standing
    in. That makes `pastapathfinder analyze .` and a later `pastapathfinder query …` from
    the same directory agree without either naming a path, and it is the only reading
    available: §5.1's derivation takes a codebase path and a query has exactly one.

    The configuration is consulted for the same reason: a codebase whose
    `.pastapathfinder.toml` redirects `[output] dir` was analyzed *there*, so that is where
    its index is. `--out` skips the lookup entirely — an explicit path is the answer, and a
    config file the user never mentioned should not be able to fail their query.
    """
    if requested is not None:
        return Path(requested).expanduser().resolve()
    root = Path.cwd().resolve()
    return runner.resolve_out_dir(root, None, load_config(root))


def open_query_index(requested: Path | str | None) -> Index:
    """Open the index a query answers from, read-only (AC-20.1, AC-20.2, AC-39.2).

    Read-only because a query is a read: nothing here may modify what `analyze` published.

    `index.open_index` already refuses an absent index and a version this build does not
    support, both by name. The one gap it leaves is a file that exists and cannot be opened
    at all — permissions, a broken mount — which SQLite reports without mentioning the
    index or what to do next; AC-20.2 wants both, so it is restated here.
    """
    path = query_out_dir(requested) / INDEX_FILENAME
    try:
        return open_index(path, read_only=True)
    except sqlite3.Error as exc:
        raise IndexStoreError(
            f"cannot open the index {path}: {exc}; check that it is readable, "
            f"or re-run `pastapathfinder analyze <root>` to rebuild it"
        ) from exc


def _print_json(payload: dict[str, Any]) -> None:
    """Emit one §5.2 document on stdout, formatted as the reports are (sorted, indented)."""
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def _answer(
    args: argparse.Namespace, produce: Callable[[Index], tuple[dict[str, Any], str]]
) -> int:
    """Open the index, run one query, and emit its answer in the requested form.

    Failures are re-raised after the structured body is written, so `main()`'s trap keeps
    its side of D10 — one line on stderr, the traceback only under `--debug`, exit 2 — while
    an agent that asked for `--json` still receives the §5.2 error body on stdout instead
    of having to parse the prose.
    """
    try:
        with open_query_index(args.out) as index:
            payload, text = produce(index)
    except (queries.QueryError, IndexStoreError) as exc:
        if args.json:
            _print_json(queries.error_json(exc))
        raise
    if args.json:
        _print_json(payload)
    else:
        print(text)
    return EXIT_SUCCESS


def _location(file_path: str | None, start_line: int | None, end_line: int | None) -> str:
    """`path:start-end` for a node with a span, or what is known when it has none.

    An external leaf says so rather than showing a blank location: FR-36 stops at the
    boundary deliberately, and AC-37.2 leaves it without a file. A missing span on an
    internal node is AC-37.3's recorded omission, not an error.
    """
    if file_path is None:
        return "external — not analyzed"
    if start_line is None:
        return file_path
    if end_line is None or end_line == start_line:
        return f"{file_path}:{start_line}"
    return f"{file_path}:{start_line}-{end_line}"


def render_entry_points(entries: Sequence[queries.EntryPoint]) -> str:
    """The human form of `query entry-points` (FR-8's listing).

    Zero is a statement, never an empty list: EC-9 makes it the expected outcome for a
    library, and the user needs to know that FR-17 lets them slice from any function
    anyway — the same alternative AC-26.2 requires the viewer to offer.
    """
    if not entries:
        return (
            "Entry points: none detected.\n"
            "  This is expected for a library, whose entry points are its public API.\n"
            "  Slice from any function instead: "
            "pastapathfinder query slice --from NODE_ID --direction forward"
        )
    lines = [f"Entry points: {len(entries)}"]
    for entry in entries:
        lines.append(f"  {entry.id}")
        lines.append(
            f"    {entry.detector} at {_location(entry.file_path, entry.start_line, None)}"
            f" → {entry.target_id or 'no target'}"
        )
    return "\n".join(lines)


def render_node(row: NodeRow) -> str:
    """The human form of `query node` — the node panel's fields (§5.2, AC-27.3)."""
    lines = [
        row.id,
        f"  kind: {row.kind}",
        f"  name: {row.name}",
        f"  location: {_location(row.file_path, row.start_line, row.end_line)}",
    ]
    if row.reachable is not None:
        # NULL means the kind carries no reachability at all (§4.2), which is not the same
        # claim as "not reachable" and must not be printed as one.
        lines.append(f"  reachable from an entry point: {'yes' if row.reachable else 'no'}")
    if row.attrs:
        lines.append(f"  attrs: {json.dumps(row.attrs, sort_keys=True, ensure_ascii=False)}")
    return "\n".join(lines)


def render_slice(origin: str, direction: str, result: queries.SliceResult) -> str:
    """The human form of `query slice` — the flagship answer (FR-15, FR-16, AC-28.2).

    Truncation is stated, not implied: AC-28.2 requires the bound to be visible, so a
    truncated slice names the budget that stopped it and the frontier it stopped at, which
    is also the argument the user re-runs with.
    """
    relation = "calls" if direction == queries.FORWARD else "is called by"
    lines = [
        f"Slice ({direction}) from {origin}: {len(result.nodes)} nodes, {len(result.edges)} edges"
    ]
    for row in result.nodes:
        lines.append(f"  {row.id}  [{row.kind}] {_location(row.file_path, row.start_line, None)}")
    if result.edges:
        lines.append(f"  — {relation} —")
    for edge in result.edges:
        flag = "  (ambiguous)" if edge.is_ambiguous else ""
        lines.append(f"  {edge.src} → {edge.dst}{flag}")
    if len(result.nodes) == 1 and not result.edges:
        # AC-15.2: an origin with no edges in this direction is an empty result, said out
        # loud so it cannot be mistaken for a failed query.
        lines.append(f"  No {'outgoing' if direction == queries.FORWARD else 'incoming'} calls.")
    if result.truncated:
        lines.append(
            f"  Truncated: the node budget was reached, so this slice is partial. "
            f"{len(result.frontier)} nodes are on the frontier; "
            f"re-run with a larger --max-nodes to expand."
        )
        for identifier in result.frontier:
            lines.append(f"    frontier: {identifier}")
    return "\n".join(lines)


def _handle_query_entry_points(args: argparse.Namespace) -> int:
    def produce(index: Index) -> tuple[dict[str, Any], str]:
        entries = queries.entry_points(index)
        return queries.entry_points_json(entries), render_entry_points(entries)

    return _answer(args, produce)


def _handle_query_slice(args: argparse.Namespace) -> int:
    def produce(index: Index) -> tuple[dict[str, Any], str]:
        # §8-O2: the bound has one definition site, in `queries`. `--max-nodes` overrides
        # it; omitting the flag resolves there rather than to a second copy of the number.
        max_nodes = queries.SLICE_MAX_NODES if args.max_nodes is None else args.max_nodes
        result = queries.slice(index, args.from_node, args.direction, max_nodes)
        return queries.slice_json(result), render_slice(args.from_node, args.direction, result)

    return _answer(args, produce)


def _handle_query_node(args: argparse.Namespace) -> int:
    def produce(index: Index) -> tuple[dict[str, Any], str]:
        row = queries.node(index, args.node_id)
        return queries.node_json(row), render_node(row)

    return _answer(args, produce)


def _handle_query_dead_code(args: argparse.Namespace) -> int:
    def produce(index: Index) -> tuple[dict[str, Any], str]:
        # D20: recomputed from the index, never read back from `deadcode.json` — so the
        # answer is correct against an index whose report directory is absent. The
        # rendering is `reports`' own, which is what guarantees AC-19.2's caveat appears
        # here in the same words the run printed.
        document = queries.dead_code_json(queries.dead_code(index))
        return document, reports.render_deadcode(document)

    return _answer(args, produce)


def _handle_view(args: argparse.Namespace) -> int:
    """Serve the local viewer until interrupted (design.md §3.1's wiring; FR-25).

    The index is located exactly as a `query` locates it — the viewer answers the same
    questions from the same file — and `viewer.server` is imported here rather than at
    module scope so that a `query` invocation never pays for Flask.

    `serve()` returns when the server stops (Werkzeug absorbs the interrupt), and a
    stopped server is a completed command: exit 0.
    """
    from pastapathfinder.viewer import server

    server.serve(query_out_dir(args.out), args.port)
    return EXIT_SUCCESS


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
