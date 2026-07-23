"""Hash gate, cache orchestration, evict-and-merge (design.md §3.6, D6, D18).

design.md §3.6 (`incremental`, normative), D6 (the three evict-and-merge rules), D18
(entry points recomputed wholesale, the metadata gate); requirements FR-24 (all ACs, incl.
C-9's transitive-closure floor), FR-30, FR-35, EC-7, EC-13 (pipeline half).

Two entry points, one per phase of an incremental run:

* **`plan_run(index, candidates)`** — the change gate. It hashes every candidate and
  compares against the index's `files` table, and compares a combined hash over the
  packaging metadata (`meta.metadata_hash`, D18). Its `RunPlan` says what changed, what was
  removed, and whether the run may take the AC-24.1 fast path (nothing changed → skip the
  engine *and* the detectors, leave the index untouched).
* **`merge(index, result, plan)`** — the evict-and-merge. It applies D6's three normative
  rules to fold one adapter result into the existing index, then deletes external leaf nodes
  that lost their last caller. It does **not** recompute entry points or reachability: those
  are the runner's, sequenced immediately after this returns (§3.6's merge order), because
  they need the analysis root and the progress channel this function is deliberately kept
  clear of.

**The three D6 rules, each with the silent-corruption bug it prevents** (proven in
`FINDINGS-session5.md` Part 1):

1. The re-extraction set is the build manager's **`rechecked_modules`** report, never "which
   graph states carry a tree" — a warm build keeps trees for cache-loaded modules it never
   re-type-checked, whose types are empty, so re-extracting them drops their cached edges.
   That rule lives in `mypy_driver` / the adapter; here it surfaces as the set of files the
   adapter returned a fragment or a skip for, which is exactly the re-extraction set.
2. Merge **replaces, not unions**, per re-extracted file — this module deletes that file's
   nodes and its `src_file` edges *before* inserting the new fragment, so a call the file no
   longer makes leaves no stale edge behind.
3. Edges are keyed by **caller file** (`src_file`) for eviction; after the merge, external
   nodes with no remaining incoming edge are deleted, because an external leaf whose last
   caller was evicted would otherwise linger with nothing pointing at it.

**Entry points sit outside all three rules (D18).** Their edges carry no `src_file` (their
`src` is a detector, not a caller), so rule 3 never sees them; they are recomputed wholesale
by the runner after this merge, which is why nothing here touches an `entry_point` node.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pastapathfinder.adapters.base import AdapterResult, SourceFile
from pastapathfinder.detectors.base import METADATA_FILENAMES
from pastapathfinder.index import Index
from pastapathfinder.reports import (
    MODE_INCREMENTAL,
    REASON_CONTENT_CHANGED,
    REASON_DEPENDENT,
)
from pastapathfinder.schema import META_METADATA_HASH

#: The `meta` key the store owns for the current root (design.md §4.2). `plan_run` reads it
#: to find the packaging-metadata files, so the gate needs no argument beyond the index.
META_ROOT_PATH = "root_path"

#: The sentinel hash a candidate carries when its bytes could not be read at gate time. It
#: is not a valid sha256, so it never equals a stored hash: an unreadable candidate always
#: counts as changed, which forces the adapter to re-examine it rather than the gate
#: silently treating a file it could not read as unchanged.
_UNREADABLE = "unreadable"


# ---------------------------------------------------------------------------
# The metadata gate (D18)
# ---------------------------------------------------------------------------


def metadata_hash(root: Path) -> str:
    """A combined sha256 over the packaging metadata present at `root` (D18).

    Order-stable and present-only: the files are visited in `METADATA_FILENAMES` order and
    an absent file contributes nothing, so the digest depends on the contents that exist and
    not on filesystem enumeration order. Each present file contributes its name and the
    sha256 of its bytes, so a rename or a content edit both move the digest — which is what
    takes a metadata-only change off the fast path and refreshes the console-script entry
    points (design.md §3.6, §3.7).
    """
    hasher = hashlib.sha256()
    for name in METADATA_FILENAMES:
        path = root / name
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(data).hexdigest().encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# The change gate (design.md §3.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunPlan:
    """What the gate decided: the change delta and whether the run may proceed (§3.6).

    * `changed` — candidates whose content differs from the index (a hash mismatch or a file
      the index never saw). FR-35's `content_changed`.
    * `removed` — files the index holds that are no longer on disk; evicted and listed
      (AC-35.3).
    * `unchanged` — candidates whose hash matches the index; the run reuses their results.
    * `metadata_changed` — the packaging metadata's combined hash moved (D18).
    * `metadata_hash` — the newly computed hash, written to `meta.metadata_hash` on merge so
      the next run's gate compares against it.
    * `candidate_hashes` — every candidate's freshly computed hash, so the caller need not
      hash a second time.
    """

    changed: frozenset[str] = frozenset()
    removed: frozenset[str] = frozenset()
    unchanged: frozenset[str] = frozenset()
    metadata_changed: bool = False
    metadata_hash: str = ""
    candidate_hashes: Mapping[str, str] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        """True when anything at all changed — the run must proceed past the gate."""
        return bool(self.changed or self.removed or self.metadata_changed)

    @property
    def needs_engine(self) -> bool:
        """True when a Python source changed or vanished, so the engine must run.

        A metadata-only change proceeds past the gate but needs no engine pass: no source
        was re-derived, only the console-script declarations, which the wholesale detector
        recompute reads afresh (D18). The graph is left exactly as it was.
        """
        return bool(self.changed or self.removed)


def _hash_candidate(path: Path) -> str:
    """The sha256 of one candidate's bytes, or the unreadable sentinel."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return _UNREADABLE


def plan_run(index: Index, candidates: Sequence[SourceFile]) -> RunPlan:
    """Hash every candidate and diff it against the index — the AC-24.1 change gate (§3.6).

    The comparison is by content hash, never mtime: a file whose modification time moved but
    whose bytes did not is unchanged, and the fast path must recognize that (AC-24.1). A
    candidate the index never recorded, or one whose hash moved, is `changed`; a recorded
    file no longer on disk is `removed`. The metadata gate runs alongside, so a `pyproject`
    edit with no source change still takes the run off the fast path (D18).

    The analysis root comes from the index's own `meta.root_path` (the packaging metadata
    lives there), which is why the gate needs only the index and the candidates: an index and
    the root it was built from are one artifact, since the output directory is derived from
    the root.
    """
    prior = index.content_hashes()
    current = {source.relpath: _hash_candidate(source.path) for source in candidates}

    changed = frozenset(
        relpath for relpath, digest in current.items() if prior.get(relpath) != digest
    )
    unchanged = frozenset(relpath for relpath in current if relpath not in changed)
    removed = frozenset(prior) - frozenset(current)

    root = Path(index.get_meta(META_ROOT_PATH) or ".")
    new_hash = metadata_hash(root)
    prior_hash = index.get_meta(META_METADATA_HASH)

    return RunPlan(
        changed=changed,
        removed=removed,
        unchanged=unchanged,
        # A first index written before D18 has no `metadata_hash` at all; a missing prior is
        # treated as a change so the key is populated and the gate is honest from then on.
        metadata_changed=prior_hash != new_hash,
        metadata_hash=new_hash,
        candidate_hashes=current,
    )


# ---------------------------------------------------------------------------
# Evict-and-merge (design.md §3.6; D6 rules 1-3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MergeReport:
    """What one merge did, in the vocabulary `reanalysis.json` publishes (FR-35).

    * `mode` — always `incremental` here; the fallback and full modes are the runner's, built
      from a full write rather than a merge.
    * `reprocessed` — `(relpath, reason)` for every re-extracted file, FR-35-attributed:
      `content_changed` for a file whose own bytes moved, `dependent` for one re-resolved
      only because a dependency did.
    * `removed` — files evicted because they left the disk (AC-35.3).
    * `evicted` — every relpath whose nodes and edges this merge deleted (re-extracted or
      removed); the record the equivalence proof checks against.
    """

    mode: str = MODE_INCREMENTAL
    reprocessed: tuple[tuple[str, str], ...] = ()
    removed: tuple[str, ...] = ()
    evicted: frozenset[str] = frozenset()


def _evict_file(index: Index, relpath: str) -> None:
    """Delete one file's nodes and its `src_file` edges (D6 rule 2, keyed by rule 3).

    Edges first, then the nodes, then the `files` row: the order is not required — no SQLite
    foreign key is enforced (design.md §3.8) — but it keeps the deletion readable as "unhook,
    then remove". External leaf nodes are *not* touched here: they carry no `file_path`, so
    the node delete never matches one, and orphans among them are swept once, after the whole
    merge, by `_delete_orphan_externals`.
    """
    connection = index.connection
    connection.execute("DELETE FROM edges WHERE src_file = ?", (relpath,))
    connection.execute("DELETE FROM nodes WHERE file_path = ?", (relpath,))
    connection.execute("DELETE FROM files WHERE path = ?", (relpath,))


def _delete_orphan_externals(index: Index) -> None:
    """Delete external leaf nodes with no remaining incoming edge (D6 rule 3's tail).

    An external node only ever appears as an edge `dst` (it has no outgoing edges, enforced
    at validation), so "no incoming edge" is "not a `dst` of any edge". Session 5's eviction
    variant proved this necessary: without it, an external whose last caller was evicted
    lingers, and the merged index stops matching a rebuild.
    """
    index.connection.execute(
        "DELETE FROM nodes WHERE is_external = 1 AND id NOT IN (SELECT dst FROM edges)"
    )


def merge(index: Index, result: AdapterResult, plan: RunPlan) -> MergeReport:
    """Fold `result` into `index` per D6's three rules, then sweep orphaned externals (§3.6).

    The re-extraction set is what the adapter returned data for — one fragment per file it
    re-derived, plus a `SkipRecord` for one it could not — which per D6 rule 1 is the build
    manager's `rechecked_modules` and never inferred from tree presence. Every file in that
    set, plus every removed file, is evicted (rule 2) before the new fragments are inserted;
    then externals that lost their last caller are deleted (rule 3).

    Everything runs in one transaction, which nests inside the caller's when there is one, so
    the graph merge, the metadata-hash refresh and — outside this function — the entry-point
    and reachability recompute either all land or all roll back (EC-13: the index a run
    publishes is never half-merged).

    `entry_point` nodes are deliberately untouched: D18 recomputes them wholesale after this
    returns, so evicting them by caller file (which they have none of) is neither done nor
    needed.
    """
    reprocessed_paths = {fragment.file.path for fragment in result.fragments}
    reprocessed_paths.update(record.path for record in result.skipped)
    evict = reprocessed_paths | set(plan.removed)

    with index.transaction():
        for relpath in sorted(evict):
            _evict_file(index, relpath)
        # A single validated batch: an edge into a preserved file resolves against the index's
        # surviving nodes, an edge into another re-extracted file against the batch itself, and
        # a bad row rejects the whole merge (AC-23.2) — which the runner turns into the
        # cache-fallback full rebuild (AC-24.3).
        index.write_fragments(result.fragments)
        _delete_orphan_externals(index)
        index.set_meta({META_METADATA_HASH: plan.metadata_hash})

    changed = set(plan.changed)
    reprocessed = tuple(
        (relpath, REASON_CONTENT_CHANGED if relpath in changed else REASON_DEPENDENT)
        for relpath in sorted(reprocessed_paths)
    )
    return MergeReport(
        mode=MODE_INCREMENTAL,
        reprocessed=reprocessed,
        removed=tuple(sorted(plan.removed)),
        evicted=frozenset(evict),
    )


__all__ = [
    "MergeReport",
    "RunPlan",
    "merge",
    "metadata_hash",
    "plan_run",
]
