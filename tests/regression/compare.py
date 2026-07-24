"""The FR-44 comparator: compare two runs' indexes and reports (specs/tasks.md task 4.3).

design.md §3.10 (the comparator, normative), §5.4 (the volatile-field register), §4.2 (the
index content it reads), §5.3 (the report shapes), D12; requirements FR-44 (AC-44.1,
AC-44.2, AC-44.3).

A **dev utility, not a shipped CLI command** — it lives under `tests/` because it verifies
the product rather than being part of it. The determinism tests import it; task 4.4's
benchmark suite runs it over two pandas-scale runs; a developer can run it by hand:

    python tests/regression/compare.py <out-dir-A> <out-dir-B>

Its whole job is one three-way classification (design.md §3.10):

1. strip **exactly** the §5.4 volatile fields — the index's `meta.created_at` and
   `meta.run_id`, and each report's `run` block — and nothing else;
2. no remaining difference → **equal**;
3. remaining differences consisting *solely* of the presence/absence of `calls` edges
   (plus external nodes referenced only by those edges), affecting **≤ 0.01 %** of call
   edges → **in variance class**: the rare engine-internal variance FR-44's 2026-07-18
   amendment documents. It is *reported* — the comparison raises `VarianceWarning` — and
   never silently passed. Threshold basis: 0.003 % measured at pandas scale (3 edges of
   88,228, `FINDINGS-session5.md` Part 2);
4. anything else → **defect**, and a test that sees one fails.

Two consequences of taking "exactly" and "solely" literally, both deliberate:

* the volatile register is imported from the product (`VOLATILE_META_KEYS`,
  `RUN_BLOCK_KEY`) rather than restated here, so §5.4 keeps one definition site and this
  tool cannot drift from what the pipeline writes;
* the variance class is defined over *call edges in the index* and nothing else, so a
  difference in a **report** is a defect even when an edge-presence difference could
  explain it. §5.4 admits exactly one variance class and defines it over call edges; a
  report difference at scale would therefore be a finding to raise against the spec, not
  something this tool should absorb quietly.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pastapathfinder.index import INDEX_FILENAME, open_index
from pastapathfinder.reports import REPORT_FILENAMES, REPORTS_DIRNAME, RUN_BLOCK_KEY
from pastapathfinder.schema import VOLATILE_META_KEYS

# ---------------------------------------------------------------------------
# Verdicts and the variance threshold
# ---------------------------------------------------------------------------

#: No difference survived the volatile strip (AC-44.1/44.2's pass condition).
EQUAL = "equal"

#: Only the documented engine-variance class survived, within threshold. Reported as a
#: warning; never a silent pass (FR-44's 2026-07-18 amendment).
IN_VARIANCE_CLASS = "in_variance_class"

#: Anything else. A test seeing this fails (FR-44).
DEFECT = "defect"

#: design.md §3.10's bound on the variance class: 0.01 % of call edges, inclusive. The
#: measured value it accommodates is 0.003 % (3 of 88,228 edges at pandas scale); the
#: order-of-magnitude headroom is design.md's, not this module's, and this is its single
#: definition site.
VARIANCE_THRESHOLD = 0.0001

# The categories a difference can fall in. Only `nodes` and `edges` differences can ever
# be in the variance class; the rest are defects by construction.
CATEGORY_SCHEMA = "schema"
CATEGORY_META = "meta"
CATEGORY_FILES = "files"
CATEGORY_NODES = "nodes"
CATEGORY_EDGES = "edges"
CATEGORY_REPORT = "report"

CALLS = "calls"


class VarianceWarning(UserWarning):
    """A comparison landed in the FR-44 variance class rather than being equal.

    Raised by every public comparison in this module, so an in-class difference reaches the
    caller even when the caller only looks at `result.ok` — design.md §3.10 requires it to
    be reported, never silently passed.
    """


class _Absent:
    """Sentinel for "this side does not have it", distinct from a stored `None`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<absent>"


ABSENT = _Absent()


# ---------------------------------------------------------------------------
# Differences
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Difference:
    """One surviving difference between two runs.

    `identity` carries the row's structured key (a node ID, an `(src, dst, kind)` triple)
    where the classification needs it; `key` is its printable form, and the pair is what
    makes a difference report say *which* row moved rather than only that something did.
    """

    category: str
    key: str
    left: Any
    right: Any
    identity: Any = None

    @property
    def present_only_in(self) -> str | None:
        """`"left"`/`"right"` when this is a presence/absence difference, else None."""
        if isinstance(self.right, _Absent) and not isinstance(self.left, _Absent):
            return "left"
        if isinstance(self.left, _Absent) and not isinstance(self.right, _Absent):
            return "right"
        return None

    def describe(self, labels: tuple[str, str] = ("left", "right")) -> str:
        side = self.present_only_in
        if side is not None:
            label = labels[0] if side == "left" else labels[1]
            return f"{self.category}: {self.key} — present only in {label}"
        return f"{self.category}: {self.key} — {self.left!r} vs {self.right!r}"


def _sort_key(difference: Difference) -> tuple[str, str]:
    return (difference.category, difference.key)


def _count(total: int, noun: str) -> str:
    return f"{total} {noun}" if total == 1 else f"{total} {noun}s"


# ---------------------------------------------------------------------------
# Index content (design.md §4.2, minus §5.4's volatile meta)
# ---------------------------------------------------------------------------

_NODE_COLUMNS = (
    "id",
    "kind",
    "name",
    "language",
    "file_path",
    "start_line",
    "end_line",
    "is_external",
    "reachable",
    "attrs",
)
_EDGE_COLUMNS = ("src", "dst", "kind", "src_file", "is_ambiguous", "attrs")
_FILE_COLUMNS = ("path", "content_hash", "status", "skip_reason")

EdgeKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class IndexContent:
    """Everything in an index that FR-44 requires two runs to agree on.

    The volatile pair is already gone: `meta` excludes `created_at` and `run_id`
    (§5.4), and nothing else is dropped — `tool_version`, `engine`, `root_path` and
    `metadata_hash` are all compared, because a run that changed one of them did not
    analyze the same thing.
    """

    schema: dict[str, str]
    meta: dict[str, str]
    files: dict[str, dict[str, Any]]
    nodes: dict[str, dict[str, Any]]
    edges: dict[EdgeKey, dict[str, Any]]

    @property
    def call_edges(self) -> int:
        """How many `calls` edges the index holds — the variance fraction's denominator."""
        return sum(1 for key in self.edges if key[2] == CALLS)

    def incidence(self) -> dict[str, set[EdgeKey]]:
        """`node id -> the edges touching it` (the "referenced only by" test's input)."""
        table: dict[str, set[EdgeKey]] = {}
        for key in self.edges:
            table.setdefault(key[0], set()).add(key)
            table.setdefault(key[1], set()).add(key)
        return table


def read_index(path: Path | str) -> IndexContent:
    """Read an index's comparable content, refusing an incompatible one (FR-39).

    Opened read-only through the product's own store, so a comparison can never be run
    against an index this build could not have written.
    """
    with open_index(Path(path), read_only=True) as store:
        connection = store.connection
        schema = {
            str(name): str(sql or "")
            for name, sql in connection.execute("SELECT name, sql FROM sqlite_master")
        }
        meta = {key: value for key, value in store.meta().items() if key not in VOLATILE_META_KEYS}
        files = {
            str(row[0]): dict(zip(_FILE_COLUMNS, row, strict=True))
            for row in connection.execute(f"SELECT {', '.join(_FILE_COLUMNS)} FROM files")
        }
        nodes = {
            str(row[0]): dict(zip(_NODE_COLUMNS, row, strict=True))
            for row in connection.execute(f"SELECT {', '.join(_NODE_COLUMNS)} FROM nodes")
        }
        edges = {
            (str(row[0]), str(row[1]), str(row[2])): dict(zip(_EDGE_COLUMNS, row, strict=True))
            for row in connection.execute(f"SELECT {', '.join(_EDGE_COLUMNS)} FROM edges")
        }
    return IndexContent(schema=schema, meta=meta, files=files, nodes=nodes, edges=edges)


def read_reports(directory: Path | str) -> dict[str, Any]:
    """Every report in `directory`, parsed, with §5.4's volatile `run` block stripped.

    A report that is absent on one side, or unparseable, is itself a difference — recorded
    as such rather than raising, so the comparison reports *all* of what moved.
    """
    directory = Path(directory)
    names = sorted({*REPORT_FILENAMES, *(path.name for path in directory.glob("*.json"))})
    documents: dict[str, Any] = {}
    for name in names:
        path = directory / name
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # unreadable or malformed: a difference, not a crash
            documents[name] = {"__unreadable__": str(exc)}
            continue
        if isinstance(document, dict):
            document = {key: value for key, value in document.items() if key != RUN_BLOCK_KEY}
        documents[name] = document
    return documents


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _diff_mapping(
    category: str,
    left: Mapping[Any, Any],
    right: Mapping[Any, Any],
    *,
    render: Any = str,
) -> Iterator[Difference]:
    """Presence and value differences over two keyed row sets.

    Ordering is left to `_result`, which sorts every difference by `(category, key)` — so a
    comparison of two large indexes pays for rendering and sorting only the rows that
    actually moved.
    """
    for key in set(left) | set(right):
        left_value = left.get(key, ABSENT)
        right_value = right.get(key, ABSENT)
        if left_value != right_value:
            yield Difference(category, render(key), left_value, right_value, identity=key)


def _render_edge(key: EdgeKey) -> str:
    src, dst, kind = key
    return f"{src} -{kind}-> {dst}"


def _json_differences(name: str, left: Any, right: Any) -> Iterator[Difference]:
    """Deep, path-addressed differences between two parsed reports."""

    def walk(left_value: Any, right_value: Any, path: str) -> Iterator[Difference]:
        if isinstance(left_value, Mapping) and isinstance(right_value, Mapping):
            for key in sorted(set(left_value) | set(right_value)):
                child = f"{path}.{key}" if path else str(key)
                yield from walk(left_value.get(key, ABSENT), right_value.get(key, ABSENT), child)
            return
        if isinstance(left_value, list) and isinstance(right_value, list):
            for position in range(max(len(left_value), len(right_value))):
                yield from walk(
                    left_value[position] if position < len(left_value) else ABSENT,
                    right_value[position] if position < len(right_value) else ABSENT,
                    f"{path}[{position}]",
                )
            return
        if left_value != right_value:
            yield Difference(
                CATEGORY_REPORT, f"{name}:{path}" if path else name, left_value, right_value
            )

    yield from walk(left, right, "")


# ---------------------------------------------------------------------------
# The variance class (design.md §3.10, normative)
# ---------------------------------------------------------------------------


def _variance_subset(
    differences: Sequence[Difference], left: IndexContent, right: IndexContent
) -> list[Difference]:
    """The differences that are the documented engine-variance class, shape-wise.

    Admissible: a `calls` edge present on exactly one side; and an **external** node
    present on exactly one side whose every edge on that side is one of those call-edge
    differences — that is design.md §3.10's "plus external nodes referenced only by those
    edges", which is how an edge that failed to resolve takes its leaf node with it.

    Everything else — a node with a span, a `contains` or `imports` edge, an edge present
    on both sides whose `attrs` moved — is outside the class by construction, whatever its
    size. Only the *shape* is decided here; the threshold is applied by the caller.
    """
    edge_differences = [
        difference
        for difference in differences
        if difference.category == CATEGORY_EDGES
        and difference.identity[2] == CALLS
        and difference.present_only_in is not None
    ]
    admissible_edges = {difference.identity for difference in edge_differences}

    candidates = [
        difference
        for difference in differences
        if difference.category == CATEGORY_NODES and difference.present_only_in is not None
    ]
    incidence = {"left": left.incidence(), "right": right.incidence()} if candidates else {}
    node_differences = []
    for difference in candidates:
        side = difference.present_only_in
        row = difference.left if side == "left" else difference.right
        if not isinstance(row, Mapping) or not row.get("is_external"):
            continue
        touching = incidence[str(side)].get(str(difference.identity), set())
        # Non-empty *and* contained: a leaf that came in with a varying edge is in the
        # class, an external node referenced by nothing at all is unexplained and is not.
        if touching and touching <= admissible_edges:
            node_differences.append(difference)

    return [*edge_differences, *node_differences]


def _classify(
    differences: Sequence[Difference], variance: Sequence[Difference], call_edges: int
) -> tuple[str, float]:
    """Verdict and variance fraction, from the differences and the class they fall in."""
    if not differences:
        return EQUAL, 0.0
    edge_variance = sum(1 for difference in variance if difference.category == CATEGORY_EDGES)
    fraction = edge_variance / call_edges if call_edges else float("inf")
    if len(variance) == len(differences) and fraction <= VARIANCE_THRESHOLD:
        return IN_VARIANCE_CLASS, fraction
    return DEFECT, fraction


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """The outcome of one comparison: a verdict plus everything that led to it."""

    verdict: str
    differences: tuple[Difference, ...] = ()
    variance: tuple[Difference, ...] = ()
    call_edges: int = 0
    variance_fraction: float = 0.0
    labels: tuple[str, str] = ("left", "right")

    @property
    def equal(self) -> bool:
        return self.verdict == EQUAL

    @property
    def ok(self) -> bool:
        """True unless the comparison found a defect (an in-class difference still warns)."""
        return self.verdict != DEFECT

    @property
    def defects(self) -> tuple[Difference, ...]:
        """The differences that are not in the variance class — the failing ones."""
        in_class = {id(difference) for difference in self.variance}
        return tuple(
            difference for difference in self.differences if id(difference) not in in_class
        )

    def merge(
        self, other: ComparisonResult, labels: tuple[str, str] | None = None
    ) -> ComparisonResult:
        """Combine two comparisons (index and reports) into one verdict."""
        differences = tuple(sorted([*self.differences, *other.differences], key=_sort_key))
        variance = tuple(sorted([*self.variance, *other.variance], key=_sort_key))
        call_edges = max(self.call_edges, other.call_edges)
        verdict, fraction = _classify(differences, variance, call_edges)
        return ComparisonResult(
            verdict=verdict,
            differences=differences,
            variance=variance,
            call_edges=call_edges,
            variance_fraction=fraction,
            labels=labels or self.labels,
        )

    def summary(self, limit: int = 20) -> str:
        """A human-readable account: the verdict, then the differences behind it."""
        left, right = self.labels
        if self.verdict == EQUAL:
            return f"equal: {left} and {right} differ only in the §5.4 volatile fields"
        headline = {
            IN_VARIANCE_CLASS: (
                f"in variance class: {_count(len(self.variance), 'call-edge difference')} "
                f"({self.variance_fraction:.6%} of {self.call_edges} call edges, threshold "
                f"{VARIANCE_THRESHOLD:.2%}) — reported, not ignored (FR-44, amended 2026-07-18)"
            ),
            DEFECT: (
                f"DEFECT: {_count(len(self.differences), 'difference')} outside the volatile "
                f"register"
                + (
                    f", including {_count(len(self.variance), 'call-edge presence difference')} "
                    f"({self.variance_fraction:.6%} of {self.call_edges} call edges, over the "
                    f"{VARIANCE_THRESHOLD:.2%} threshold)"
                    if self.variance
                    else ""
                )
            ),
        }[self.verdict]
        # Every difference when they are all in class (whether the verdict turned on the
        # threshold or not); otherwise the ones that are not, which are the failing rows.
        shown = self.defects or self.differences
        lines = [headline]
        lines += [f"  {difference.describe(self.labels)}" for difference in shown[:limit]]
        if len(shown) > limit:
            lines.append(f"  … and {len(shown) - limit} more")
        return "\n".join(lines)


def _result(
    differences: Iterable[Difference],
    variance: Iterable[Difference],
    call_edges: int,
    labels: tuple[str, str],
) -> ComparisonResult:
    ordered = tuple(sorted(differences, key=_sort_key))
    in_class = tuple(sorted(variance, key=_sort_key))
    verdict, fraction = _classify(ordered, in_class, call_edges)
    return ComparisonResult(
        verdict=verdict,
        differences=ordered,
        variance=in_class,
        call_edges=call_edges,
        variance_fraction=fraction,
        labels=labels,
    )


def _announce(result: ComparisonResult) -> ComparisonResult:
    """Warn on an in-class difference so no caller can pass one silently (§3.10)."""
    if result.verdict == IN_VARIANCE_CLASS:
        warnings.warn(result.summary(), VarianceWarning, stacklevel=3)
    return result


# ---------------------------------------------------------------------------
# The comparisons
# ---------------------------------------------------------------------------


def _labels(left: Path, right: Path) -> tuple[str, str]:
    return (str(left), str(right))


def _compare_indexes(left: Path, right: Path) -> ComparisonResult:
    left_content = read_index(left)
    right_content = read_index(right)
    differences = [
        *_diff_mapping(CATEGORY_SCHEMA, left_content.schema, right_content.schema),
        *_diff_mapping(CATEGORY_META, left_content.meta, right_content.meta),
        *_diff_mapping(CATEGORY_FILES, left_content.files, right_content.files),
        *_diff_mapping(CATEGORY_NODES, left_content.nodes, right_content.nodes),
        *_diff_mapping(
            CATEGORY_EDGES, left_content.edges, right_content.edges, render=_render_edge
        ),
    ]
    variance = _variance_subset(differences, left_content, right_content)
    call_edges = max(left_content.call_edges, right_content.call_edges)
    return _result(differences, variance, call_edges, _labels(left, right))


def compare_indexes(left: Path | str, right: Path | str) -> ComparisonResult:
    """Compare two index files (AC-44.1). Volatile `meta` stripped; nothing else."""
    return _announce(_compare_indexes(Path(left), Path(right)))


def _compare_reports(left: Path, right: Path) -> ComparisonResult:
    left_documents = read_reports(left)
    right_documents = read_reports(right)
    differences: list[Difference] = []
    for name in sorted(set(left_documents) | set(right_documents)):
        differences.extend(
            _json_differences(
                name, left_documents.get(name, ABSENT), right_documents.get(name, ABSENT)
            )
        )
    # Report differences are never in the variance class (see the module docstring).
    return _result(differences, [], 0, _labels(left, right))


def compare_reports(left: Path | str, right: Path | str) -> ComparisonResult:
    """Compare two report directories (AC-44.2). Each `run` block stripped; nothing else."""
    return _announce(_compare_reports(Path(left), Path(right)))


def compare_runs(left: Path | str, right: Path | str) -> ComparisonResult:
    """Compare two complete `--out` directories: index and reports together.

    The shape a determinism test wants: `analyze` twice into two output directories, then
    one call that judges everything the two runs published.
    """
    left, right = Path(left), Path(right)
    result = _compare_indexes(left / INDEX_FILENAME, right / INDEX_FILENAME).merge(
        _compare_reports(left / REPORTS_DIRNAME, right / REPORTS_DIRNAME),
        labels=_labels(left, right),
    )
    return _announce(result)


def require_equivalent(result: ComparisonResult) -> ComparisonResult:
    """Raise on a defect; pass an in-class difference through (it has already warned).

    The assertion form for callers that only care whether FR-44 held — task 4.4's
    determinism-at-scale check is one.
    """
    if not result.ok:
        raise AssertionError(result.summary())
    return result


# ---------------------------------------------------------------------------
# Command line (dev utility)
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """`compare.py A B` — exit 0 when equal or in variance class, 1 on a defect."""
    parser = argparse.ArgumentParser(
        prog="compare.py",
        description=(
            "Compare two pastapathfinder runs for FR-44 determinism: strips the design.md "
            "§5.4 volatile fields, then classifies what is left as equal, in variance "
            "class, or a defect."
        ),
    )
    parser.add_argument("left", type=Path, help="an --out directory from one run")
    parser.add_argument("right", type=Path, help="an --out directory from the other run")
    parser.add_argument(
        "--part",
        choices=("all", "index", "reports"),
        default="all",
        help="compare only the index, only the reports, or both (default: both)",
    )
    args = parser.parse_args(argv)

    if args.part == "index":
        result = compare_indexes(args.left / INDEX_FILENAME, args.right / INDEX_FILENAME)
    elif args.part == "reports":
        result = compare_reports(args.left / REPORTS_DIRNAME, args.right / REPORTS_DIRNAME)
    else:
        result = compare_runs(args.left, args.right)

    print(result.summary())
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - the dev entry point
    sys.exit(main())
