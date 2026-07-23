"""Post-run change detection (design.md §3.10, §5.3; FR-38, EC-14).

FR-38: at the completion of every analysis run — initial or incremental — the recorded
content of the enumerated files is compared against their current on-disk state, and a
warning names any file that differs or has been removed, recommending re-analysis. The
check is best-effort (EC-14): it narrows the window in which an edit made *while the run is
in progress* goes unnoticed; it cannot close it, and its `note` (fixed in `reports`, the
`reports.CHANGE_WARNING_NOTE` wording) never reads as a guarantee of freshness.

The mechanism is the two-stage compare design.md §3.10 prescribes — a stat pre-check that
avoids hashing the common (unchanged) case, over a content hash that is the authority for
what actually counts as a change:

1. **A stat baseline** (`snapshot`) is taken early in the run — after discovery, before the
   engine reads a byte — recording each enumerated file's `(size, mtime_ns)`. This is the
   cheap pre-check FR-38 permits, and it is transient: it describes *this* run, because
   FR-38 detects changes within a single run, not across runs.
2. **The completion check** (`check`) re-stats each recorded file. A file whose
   `(size, mtime_ns)` is unchanged is passed *without hashing*; only a file whose stat moved
   is hash-confirmed against the content the run actually recorded — the index's
   `content_hash`, which is FR-24's authority for the bytes as read. A stat that moved but a
   hash that did not is **not** a change: a merely re-touched file with identical bytes must
   not warn (AC-38.1). mtime alone never decides a change, so a run whose files were only
   re-touched emits no warning at all (AC-38.2).

A file gone from disk at check time is `removed`; one that cannot be stat-ed or read during
the check is a per-file `check_failure`, named and carried rather than silently treated as
unchanged (AC-38.3).

This module opens nothing but the files it is asked about: the recorded hashes are supplied
by the caller (the runner reads them from the index's `content_hashes()`), so `postrun`
imports no engine, no index, and no adapter (AC-23.1 holds trivially here).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileState:
    """The cheap pre-check fingerprint of one file: its size and nanosecond mtime.

    Deliberately *not* a content hash — hashing every enumerated file at snapshot time
    would defeat the pre-check's purpose (EC-3's cost concern). The hash comparison happens
    only for files whose `(size, mtime_ns)` moved, and only at check time.
    """

    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ChangeCheck:
    """The outcome of the FR-38 check, in the vocabulary `change_warning.json` publishes.

    `changed` and `removed` are sorted relpaths; `check_failures` is `[{path, error}]` for
    files the check could not stat or read. `clean` is the AC-38.2 fast question: nothing
    moved, so no warning line is emitted.
    """

    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    check_failures: list[dict[str, str]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.changed or self.removed or self.check_failures)


def snapshot(root: Path, relpaths: Iterable[str]) -> dict[str, FileState]:
    """Record `(size, mtime_ns)` for each enumerated file — the FR-38 pre-check baseline.

    Taken early in the run (after discovery, before the engine reads anything) so the
    baseline reflects the files as they stood when the run began reading them. Best-effort:
    a file that cannot be stat-ed here contributes no baseline entry and is simply hashed at
    check time — the pre-check is an optimization, never a source of truth.
    """
    states: dict[str, FileState] = {}
    for relpath in relpaths:
        try:
            info = os.stat(root / relpath)
        except OSError:
            continue
        states[relpath] = FileState(size=info.st_size, mtime_ns=info.st_mtime_ns)
    return states


def _reason(exc: OSError) -> str:
    """A human-readable one-liner for a per-file check failure (AC-38.3)."""
    return exc.strerror or str(exc)


def check(
    root: Path,
    baseline: Mapping[str, FileState],
    recorded_hashes: Mapping[str, str],
) -> ChangeCheck:
    """Compare each recorded file against disk, hash-confirming any stat difference (FR-38).

    Iterates the files the run recorded content for (`recorded_hashes`, keyed by relpath —
    the index's `content_hashes()`), which is exactly "the recorded content of discovered
    files" FR-38 speaks of: a file with no recorded content has nothing to compare and is
    not this check's concern.

    Per file: a stat that matches the `baseline` fingerprint passes without hashing (the
    pre-check); a stat that moved is hash-confirmed against the recorded content, and only a
    *hash* difference is a change (AC-38.1 — a re-touched file with identical bytes does not
    warn). A missing file is `removed`; a file that cannot be stat-ed or read is a named
    `check_failure` (AC-38.3).
    """
    changed: list[str] = []
    removed: list[str] = []
    failures: list[dict[str, str]] = []

    for relpath in sorted(recorded_hashes):
        path = root / relpath
        try:
            info = os.stat(path)
        except FileNotFoundError:
            removed.append(relpath)
            continue
        except OSError as exc:
            failures.append({"path": relpath, "error": _reason(exc)})
            continue

        prior = baseline.get(relpath)
        if prior is not None and info.st_size == prior.size and info.st_mtime_ns == prior.mtime_ns:
            # Pre-check pass: the stat is unmoved, so the bytes are unchanged. Do not hash.
            continue

        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            failures.append({"path": relpath, "error": _reason(exc)})
            continue

        # The stat moved; only a moved hash is a real change. mtime alone never decides.
        if digest != recorded_hashes[relpath]:
            changed.append(relpath)

    return ChangeCheck(changed=changed, removed=removed, check_failures=failures)
