"""A stub `LanguageAdapter` — a test double, never product code.

design.md §3.4 defines the seam the pipeline drives; the real Python adapter arrives in
milestone 2 (tasks 2.1-2.5). Until then this double supplies schema-conformant fragments
so that the whole `analyze` path — discovery, the index, the six reports, the exit codes
— is exercisable now, which is exactly what task 1.5 owes.

It fabricates a plausible shape rather than analyzing anything: one `file` node, one
`module` body node (D16) and one `function` node per analyzed file, with `contains` edges
between them. Its knobs exist to reach states a real adapter reaches on real code —
skipped files, diagnostics, entry points — plus one (`phantom_files`) that reaches a state
a *correct* adapter never reaches, so the pipeline's own accounting check can be proven
to fire.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pastapathfinder.adapters.base import AdapterResult, SourceFile
from pastapathfinder.progress import ProgressSink
from pastapathfinder.schema import (
    Diag,
    EdgeRow,
    FileRecord,
    GraphFragment,
    NodeRow,
    SkipRecord,
)

ENGINE_NAME = "stub"
ENGINE_VERSION = "0"


def module_name(relpath: str) -> str:
    """The §4.1 module-name derivation: strip `.py`, separators to dots, drop `__init__`."""
    name = relpath.removesuffix(".py").replace("/", ".")
    return name.removesuffix(".__init__") if name.endswith(".__init__") else name


@dataclass
class StubAdapter:
    """A `LanguageAdapter` that emits a fixed shape for every file it is handed.

    * `skip` maps a relpath to the human-readable reason it was skipped (AC-7.2).
    * `skip_reason` is the schema reason class those skips carry.
    * `diagnostics` are emitted verbatim into the run's diagnostics report.
    * `phantom_files` emits fragments for files that were never handed in — a
      deliberately inconsistent adapter, used to prove AC-7.1's reconciliation fires.

    It emits no `entry_point` nodes: those are the detectors' (D18), recomputed wholesale by
    the runner over the analyzed files' stdlib ASTs, so a test that wants one gives a file a
    `__main__` block rather than asking the adapter to fabricate the node.
    """

    skip: Mapping[str, str] = field(default_factory=dict)
    skip_reason: str = "parse_error"
    diagnostics: Sequence[Diag] = ()
    phantom_files: Sequence[str] = ()

    language: str = "python"
    seen: list[SourceFile] = field(default_factory=list)

    def recognizes(self, path: Path, first_line: bytes | None) -> bool:
        if path.suffix == ".py":
            return True
        head = first_line or b""
        return head.startswith(b"#!") and b"python" in head

    def analyze(
        self,
        root: Path,
        files: list[SourceFile],
        cache_dir: Path,
        changed: set[Path] | None,
        progress: ProgressSink,
        prior_nodes=None,
    ) -> AdapterResult:
        self.seen.extend(files)
        fragments: list[GraphFragment] = []
        skipped: list[SkipRecord] = []
        for source in files:
            progress.advance()
            digest = hashlib.sha256(source.path.read_bytes()).hexdigest()
            reason = self.skip.get(source.relpath)
            if reason is not None:
                fragments.append(
                    GraphFragment(
                        file=FileRecord(
                            path=source.relpath,
                            content_hash=digest,
                            status="skipped",
                            skip_reason=self.skip_reason,
                        )
                    )
                )
                skipped.append(
                    SkipRecord(path=source.relpath, reason=self.skip_reason, detail=reason)
                )
                continue
            fragments.append(self._fragment(source.relpath, digest))

        for relpath in self.phantom_files:
            fragments.append(self._fragment(relpath, "0" * 64))

        return AdapterResult(
            fragments=fragments,
            skipped=skipped,
            diagnostics=list(self.diagnostics),
            rechecked=set(),
            engine_meta={"engine": ENGINE_NAME, "engine_version": ENGINE_VERSION},
        )

    def _fragment(self, relpath: str, digest: str) -> GraphFragment:
        module = module_name(relpath)
        file_id = f"python:file:{relpath}"
        module_id = f"python:{module}.<module>"
        function_id = f"python:{module}.main"
        nodes = [
            NodeRow(id=file_id, kind="file", name=relpath, language="python", file_path=relpath),
            NodeRow(
                id=module_id,
                kind="module",
                name=module,
                language="python",
                file_path=relpath,
                start_line=1,
                end_line=1,
                attrs={"python_role": "module_body"},
            ),
            NodeRow(
                id=function_id,
                kind="function",
                name="main",
                language="python",
                file_path=relpath,
                start_line=1,
                end_line=2,
            ),
        ]
        edges = [
            EdgeRow(src=file_id, dst=module_id, kind="contains", src_file=relpath),
            EdgeRow(src=file_id, dst=function_id, kind="contains", src_file=relpath),
        ]
        return GraphFragment(
            file=FileRecord(path=relpath, content_hash=digest, status="analyzed"),
            nodes=nodes,
            edges=edges,
        )
