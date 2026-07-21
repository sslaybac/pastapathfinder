"""Source enumeration, probing, symlink and pruning rules (design.md §3.3; FR-1, FR-2).

design.md §3.3 (`discovery`, normative), §8-O1 (directory pruning, approved), D12, D17;
requirements FR-1 (AC-1.1–1.6), FR-2, EC-10, EC-11.

`discover()` answers one question — *what are this run's inputs, and what did the rules
take away?* — and answers it once. Everything downstream (the adapter, the coverage and
exclusion reports, the change check) consumes its result rather than walking the tree
again, so the tool has exactly one definition of "discovered".

The three rules that shape the walk, all normative in design.md §3.3:

* **Prune at directory matches (§8-O1).** An excluded directory is *one*
  `ExclusionRecord` and its contents are never enumerated — not listed, not hashed, not
  counted (D17's counting unit). This is what keeps `venv/` and `node_modules/` off
  every run's critical path.
* **Recognize, do not guess.** A `.py` suffix is a candidate; an extensionless file is a
  candidate only if the shebang probe says so; anything else is not an analysis input at
  all (AC-1.2) and is silently ignored rather than reported as skipped.
* **Never leave the root.** Directory symlinks are not followed, which makes link cycles
  unreachable by construction (AC-1.6's termination clause); file symlinks are resolved
  and accepted only when their real path is inside the root, where they are analyzed
  once under that real path.

Exclusion is decided *before* recognition, so no byte inside excluded territory is ever
read — a file the rules removed is recorded and dropped without a probe. The
consequence, and it is deliberate: an excluded path is recorded whatever its name
(FR-5's "every excluded path"), while an unexcluded path that is not a recognized source
is not recorded at all (AC-1.2).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pastapathfinder.exclusions import ExclusionRecord, RuleSet
from pastapathfinder.schema import Diag

# ---------------------------------------------------------------------------
# The v1 Python recognition rules (FR-1 (a)/(b); design.md §3.3)
# ---------------------------------------------------------------------------

#: Rule (a).
PYTHON_SUFFIX = ".py"

#: Rule (b), normative bound: the probe reads at most this many bytes of line 1. A file
#: whose first line only says `python` beyond this point is not a recognized source —
#: the bound is what keeps the probe cheap on a tree full of extensionless data files.
PROBE_BYTES = 256

#: Rule (b), normative test: line 1 must start with this…
SHEBANG_PREFIX = b"#!"

#: …and contain this.
PYTHON_MARKER = b"python"


class RootError(Exception):
    """The run's root folder cannot be walked (AC-1.3).

    Run-terminating by design: with no readable root there is nothing to analyze, and a
    run that quietly discovered zero files would report success. `cli.main()` maps this
    to exit 2 with the message on stderr (D10), so the message names the path and the
    reason.
    """


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """What the walk found (design.md §3.3's interface).

    `candidates` are absolute paths — the *real* path of every recognized source file,
    deduplicated and sorted, ready to be opened. `root` is the resolved root they live
    under, so `relpath()` gives the root-relative POSIX form the index, the reports and
    the node-ID grammar all speak.

    `probe_diagnostics` carries the non-fatal anomalies of the walk itself: `probe_failure`
    for something that could not be read while being classified (AC-1.5), `symlink_skip`
    for a link not followed (AC-1.6). An anomaly-free walk returns an empty list, and the
    run's diagnostics report is written either way (the C-10 convention).
    """

    root: Path
    candidates: list[Path]
    excluded: list[ExclusionRecord]
    probe_diagnostics: list[Diag]

    def relpath(self, path: Path) -> str:
        """`path` as a root-relative POSIX string (the form every artifact stores)."""
        return path.relative_to(self.root).as_posix()


def discover(root: Path | str, ruleset: RuleSet) -> DiscoveryResult:
    """Enumerate the analyzable sources under `root`, applying `ruleset` (design.md §3.3).

    Raises `RootError` when the root is missing, is not a directory, or cannot be listed
    (AC-1.3). Every other failure encountered on the way down is a diagnostic and the
    walk continues (FR-6's posture, applied to enumeration): a codebase with one
    unreadable corner still gets analyzed, and the corner is named.
    """
    walk = _Walk(_check_root(root), ruleset)
    walk.run()
    return DiscoveryResult(
        root=walk.root,
        candidates=sorted(walk.candidates),
        excluded=sorted(walk.excluded, key=lambda record: record.path),
        probe_diagnostics=list(walk.diagnostics),
    )


def _check_root(root: Path | str) -> Path:
    """Resolve `root` and prove it is a listable directory, or raise `RootError`."""
    given = Path(root)
    try:
        resolved = given.resolve(strict=True)
    except OSError as exc:
        raise RootError(f"root folder {given}: {_reason(exc)}") from exc
    if not resolved.is_dir():
        raise RootError(f"root folder {given}: not a directory")
    try:
        # Opening the directory is the permission test; scandir raises here, not lazily.
        os.scandir(resolved).close()
    except OSError as exc:
        raise RootError(f"root folder {given}: {_reason(exc)}") from exc
    return resolved


def _reason(exc: OSError) -> str:
    """The human-readable half of an OS error (AC-1.3 wants the reason, not a repr)."""
    return exc.strerror or str(exc)


class _Walk:
    """The traversal state. One instance per `discover()` call; not reusable."""

    __slots__ = ("candidates", "diagnostics", "excluded", "root", "ruleset", "seen")

    def __init__(self, root: Path, ruleset: RuleSet) -> None:
        self.root = root
        self.ruleset = ruleset
        self.candidates: list[Path] = []
        self.excluded: list[ExclusionRecord] = []
        self.diagnostics: list[Diag] = []
        #: Real paths already accepted — AC-1.6's "analyzed once" (dedupe by realpath).
        self.seen: set[Path] = set()

    # -- traversal ---------------------------------------------------------

    def run(self) -> None:
        """Depth-first, alphabetical, no recursion (a deep tree is not a stack risk)."""
        pending = [""]
        while pending:
            subdirectories = self._scan(pending.pop())
            pending.extend(reversed(subdirectories))

    def _scan(self, rel_dir: str) -> list[str]:
        """Classify one directory's entries; return the subdirectories to descend into."""
        directory = self.root / rel_dir if rel_dir else self.root
        try:
            with os.scandir(directory) as entries:
                listing = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            # Not AC-1.3: the *root* was proven listable, so this is one unreadable
            # corner of the tree. Terminating the run over it would hide the whole
            # codebase to report one directory; naming it and continuing does not.
            self._diagnose(
                "probe_failure",
                rel_dir or ".",
                f"{rel_dir or '.'}: directory cannot be listed ({_reason(exc)}); "
                f"its contents are not part of this run",
            )
            return []

        subdirectories: list[str] = []
        for entry in listing:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if self._is_directory(entry):
                record = self.ruleset.match(rel, is_dir=True)
                if record is not None:
                    # §8-O1: one record, and nothing below it is enumerated.
                    self.excluded.append(record)
                elif entry.is_symlink():
                    # AC-1.6: not followed, so a link cycle cannot recur. A link into the
                    # root reaches nothing new — the real directory is walked on its own.
                    self._diagnose(
                        "symlink_skip",
                        rel,
                        f"{rel}: directory symbolic link is not followed",
                    )
                else:
                    subdirectories.append(rel)
                continue

            record = self.ruleset.match(rel, is_dir=False)
            if record is not None:
                self.excluded.append(record)
                continue
            self._consider_file(rel, entry)
        return subdirectories

    def _is_directory(self, entry: os.DirEntry[str]) -> bool:
        try:
            return entry.is_dir()
        except OSError:
            # Undeterminable: treat as a file and let the stat below name the failure.
            return False

    # -- files -------------------------------------------------------------

    def _consider_file(self, rel: str, entry: os.DirEntry[str]) -> None:
        """Apply FR-1's recognition rules to one non-excluded, non-directory entry."""
        suffix = PurePosixPath(entry.name).suffix
        if suffix not in ("", PYTHON_SUFFIX):
            return  # AC-1.2: not recognized by any configured analyzer, not an input.

        target = self._target(rel, entry)
        if target is None:
            return

        try:
            status = os.stat(target)
        except OSError as exc:
            self._diagnose(
                "probe_failure",
                rel,
                f"{rel}: cannot be examined ({_reason(exc)}); not treated as a source file",
            )
            return
        if not stat.S_ISREG(status.st_mode):
            # A fifo, socket or device node is not a source file — and probing a fifo
            # would block the run waiting for a writer.
            return

        if suffix == "" and not self._has_python_shebang(rel, target):
            return  # AC-1.5: binary, or a shebang that names something else.

        if target in self.seen:
            return  # AC-1.6: a file reached twice (via a link) is analyzed once.
        self.seen.add(target)
        self.candidates.append(target)

    def _target(self, rel: str, entry: os.DirEntry[str]) -> Path | None:
        """The real path to analyze, or None when the entry is not usable as an input."""
        path = self.root / rel
        if not entry.is_symlink():
            return path
        try:
            real = Path(os.path.realpath(path, strict=True))
        except OSError as exc:
            self._diagnose(
                "symlink_skip",
                rel,
                f"{rel}: symbolic link cannot be resolved ({_reason(exc)}); not followed",
            )
            return None
        try:
            inside = real.relative_to(self.root)
        except ValueError:
            # AC-1.6: the target is outside the root, so it is not this run's code.
            self._diagnose(
                "symlink_skip",
                rel,
                f"{rel}: symbolic link targets {real}, outside the root folder; not followed",
            )
            return None
        relative = inside.as_posix()
        if self._excludes_target(relative):
            # Following it would smuggle excluded code back in through the side door,
            # and the exclusion the user configured would be silently untrue.
            self._diagnose(
                "symlink_skip",
                rel,
                f"{rel}: symbolic link targets {relative}, which the exclusion rules "
                f"remove; not followed",
            )
            return None
        return real

    def _excludes_target(self, relative: str) -> bool:
        """Do the rules exclude `relative` itself, or prune any directory above it?"""
        parts = relative.split("/")
        return any(
            self.ruleset.match("/".join(parts[:depth]), is_dir=True) is not None
            for depth in range(1, len(parts))
        ) or (self.ruleset.match(relative, is_dir=False) is not None)

    def _has_python_shebang(self, rel: str, path: Path) -> bool:
        """FR-1 rule (b): line 1 starts with `#!` and names a Python interpreter."""
        try:
            with path.open("rb") as stream:
                head = stream.read(PROBE_BYTES)
        except OSError as exc:
            # AC-1.5: an unreadable probe is a diagnostic, never a fatal error and never
            # an assumption — the file is not claimed as a source.
            self._diagnose(
                "probe_failure",
                rel,
                f"{rel}: cannot be read for the shebang probe ({_reason(exc)}); "
                f"not treated as a source file",
            )
            return False
        line = head.split(b"\n", 1)[0].rstrip(b"\r")
        return line.startswith(SHEBANG_PREFIX) and PYTHON_MARKER in line

    # -- diagnostics -------------------------------------------------------

    def _diagnose(self, kind: str, path: str, message: str) -> None:
        self.diagnostics.append(Diag(kind=kind, path=path, message=message))
