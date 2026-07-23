"""The ordered detector list and per-detector error isolation (design.md §3.7; FR-8, D18).

design.md §3.7 (the registry and its error wrapping), D14 (hardcoded registry, no
discovery/registration machinery), D18 (detectors run wholesale over all analyzed files
on every proceeding run and hold no cross-run state); requirements FR-8 (AC-8.1, AC-8.2),
FR-9.

`DETECTORS` is the ordered list of design.md §3.7, now complete: `MainBlockDetector`,
`ConsoleScriptsDetector`, `FlaskFastapiRouteDetector`, `DjangoUrlconfDetector`. That is the
whole registration mechanism — appending one entry — so adding a detector touches one new
module plus this one line and nothing else (AC-8.1); the core schema is never edited to add
a detector.

`run_detectors()` is a pure function of the module trees and the metadata file set it is
handed (D18): it derives nothing from adapter state and reads no cross-run cache, so a run
over the same inputs is byte-for-byte the same. Every `detect()` call is wrapped — an
exception becomes a `detector_error` diagnostic naming the detector and the file, and
iteration continues, so one detector failing on one file stops neither that detector on
other files nor the other detectors (AC-8.2).

Wiring this into the analyze run — parsing the analyzed files with stdlib `ast`, running
the detectors before reachability, evicting the previous run's entry nodes (D18) — belongs
to later tasks (3.4's run integration, 4.1's merge order). This module produces the entry
nodes; it does not decide when the pipeline calls it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pastapathfinder.detectors.base import (
    Detector,
    DetectorOutput,
    ModuleDetector,
    ModuleInput,
    ProjectDetector,
    ProjectInput,
)
from pastapathfinder.detectors.console_scripts import ConsoleScriptsDetector
from pastapathfinder.detectors.django_urlconf import DjangoUrlconfDetector
from pastapathfinder.detectors.flask_fastapi import FlaskFastapiRouteDetector
from pastapathfinder.detectors.main_block import MainBlockDetector
from pastapathfinder.schema import Diag, EdgeRow, NodeRow

#: The ordered detector list of design.md §3.7. Extended by one entry per detector task.
DETECTORS: tuple[Detector, ...] = (
    MainBlockDetector(),
    ConsoleScriptsDetector(),
    FlaskFastapiRouteDetector(),
    DjangoUrlconfDetector(),
)


def _run_module(detector: ModuleDetector, module: ModuleInput) -> DetectorOutput:
    """Run one per-module `detect()`, isolating any failure to a diagnostic (AC-8.2)."""
    try:
        return detector.detect(module)
    except Exception as exc:  # noqa: BLE001 - isolation is the whole point (AC-8.2)
        return _detector_error(detector, module.module_path, exc)


def _run_project(detector: ProjectDetector, project: ProjectInput) -> DetectorOutput:
    """Run the project-level `detect()`, isolating any failure to a diagnostic (AC-8.2)."""
    try:
        return detector.detect(project)
    except Exception as exc:  # noqa: BLE001 - isolation is the whole point (AC-8.2)
        return _detector_error(detector, None, exc)


def _detector_error(detector: Detector, path: str | None, exc: Exception) -> DetectorOutput:
    """A `detector_error` diagnostic naming the detector and the input, and no rows.

    A detector that raises produces nothing for that input, which is the honest outcome: a
    partial emission from a crashed detector could be worse than none.
    """
    message = f"detector {detector.name!r} raised on {path or '<project metadata>'}: {exc}"
    return DetectorOutput(diagnostics=[Diag(kind="detector_error", path=path, message=message)])


def run_detectors(
    modules: Sequence[ModuleInput],
    project: ProjectInput,
    *,
    detectors: Iterable[Detector] = DETECTORS,
) -> DetectorOutput:
    """Run every detector and aggregate the entry nodes, edges, and diagnostics (FR-8, D18).

    `modules` is one `ModuleInput` per analyzed file that parsed under stdlib `ast`;
    `project` is the packaging metadata set. Per-module detectors run over every module,
    project-level detectors once — each detector in `detectors` order, each `detect()` call
    isolated (AC-8.2). `detectors` defaults to the registry but is a parameter so a test can
    register a dummy detector without editing this list (AC-8.1).

    Output order is stable — detector order, then module order — but is not the index's
    final order: the store's canonical-sort layer (D12) reorders nodes by id and edges by
    `(src, dst, kind)` at the write boundary, so this order is for reproducibility, not for
    the artifact.
    """
    nodes: list[NodeRow] = []
    edges: list[EdgeRow] = []
    diagnostics: list[Diag] = []

    def collect(output: DetectorOutput) -> None:
        nodes.extend(output.nodes)
        edges.extend(output.edges)
        diagnostics.extend(output.diagnostics)

    for detector in detectors:
        if isinstance(detector, ModuleDetector):
            for module in modules:
                collect(_run_module(detector, module))
        elif isinstance(detector, ProjectDetector):
            collect(_run_project(detector, project))
        else:  # pragma: no cover - a detector must be one of the two shapes
            raise TypeError(
                f"detector {detector.name!r} is neither a ModuleDetector nor a ProjectDetector"
            )

    return DetectorOutput(nodes=nodes, edges=edges, diagnostics=diagnostics)
