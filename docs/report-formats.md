# Report formats

Every analysis run writes six JSON reports to `<out>/reports/`, overwriting the previous
run's. They are the machine-readable record of what the run did (FR-42); the summaries
printed to stdout are rendered *from* these files, so where a summary and a report
disagree, the report is right.

## Format versioning

Every report carries a top-level `format_version`, an integer. This build writes and
reads **`1`**.

A consumer must check it and refuse a version it does not know, rather than reading the
fields it recognizes and ignoring the rest — a later version may change what an existing
field means. The tool applies this rule to itself: its own renderings go through the same
refusal path, so a report from a future version produces an explicit error naming the
found and supported versions, never a partial reading.

## Volatile fields

Two runs over an unchanged codebase produce identical reports except for one block:

```json
"run": {
  "run_id": "…",             // unique per run
  "started_at": "…",         // ISO-8601 UTC, second precision
  "finished_at": "…",
  "duration_seconds": 0.0
}
```

Every report carries it, and nothing else in a report is permitted to vary between two
runs over identical input (FR-44). The index has its own volatile pair, `meta.created_at`
and `meta.run_id`. The complete register, and the comparison tool that enforces it,
arrive with the determinism gate.

Lists inside reports are ordered deterministically — paths and diagnostics sort, they do
not appear in whatever order the filesystem or the engine produced them.

## `coverage.json`

Every discovered entry with exactly one status, and the counts that reconcile (FR-7).

```json
{
  "format_version": 1,
  "run": { … },
  "counts": {
    "entries_discovered": 12,
    "files_analyzed": 9,
    "files_skipped": 1,
    "entries_excluded": 2
  },
  "files": [
    {"path": "pkg/app.py", "status": "analyzed", "is_dir": false},
    {"path": "pkg/broken.py", "status": "skipped", "is_dir": false,
     "reason": "line 3: invalid syntax"},
    {"path": "venv", "status": "excluded", "is_dir": true,
     "rule": {"pattern": "venv/", "source": "default:python"}}
  ]
}
```

- `entries_discovered = files_analyzed + files_skipped + entries_excluded`, computable
  from the `counts` block alone. The run asserts this before writing the file; a run
  whose accounting does not reconcile fails rather than publishing it.
- The count names state their units. `files_*` count files. `entries_*` count *entries*,
  which is not the same thing: an excluded **directory** is one entry, and its contents
  are never enumerated — that is what keeps `venv/` and `node_modules/` off every run's
  critical path. `is_dir` on a row tells you which kind of entry you are looking at.
- `status` is one of `analyzed`, `skipped`, `excluded`. `reason` is present on skipped
  rows only and is human-readable. `rule` is present on excluded rows only.

## `exclusions.json`

Every excluded path with the rule that excluded it (FR-5) — the audit trail against
over-aggressive defaults.

```json
{
  "format_version": 1,
  "run": { … },
  "exclusions": [
    {"path": "venv", "is_dir": true, "pattern": "venv/", "source": "default:python"},
    {"path": "gen/pb2.py", "is_dir": false, "pattern": "*.pb2.py", "source": "user:exclude"}
  ],
  "none_excluded": false
}
```

`source` is one of:

| Source | Meaning |
|---|---|
| `default:common` | The language-independent convention set (`.git/`). |
| `default:python` | The Python convention set (virtualenvs, `build/`, `dist/`, caches, …). |
| `gitignore:<relpath>` | A pattern from that `.gitignore`, named relative to the analysis root. |
| `user:exclude` | A pattern from your configuration's `[exclude] add`. |

A run that excluded nothing still writes this report, with `none_excluded: true`. On the
first analysis of a codebase the run output points at this file by name.

## `reanalysis.json`

Which files a run re-processed and why (FR-35).

```json
{
  "format_version": 1,
  "run": { … },
  "mode": "full",
  "reprocessed": [{"path": "pkg/app.py", "reason": "content_changed"}],
  "removed": ["pkg/gone.py"]
}
```

- `mode` is `full`, `incremental`, `skipped_no_changes`, or `fallback`.
- `reason` is exactly one of `content_changed` (the file's own content changed),
  `dependent` (it imports, transitively, something that changed), or `cache_fallback`
  (cached results were unusable and the file was re-analyzed from scratch).
- `removed` lists files present in the previous run and absent now.

Incremental re-analysis is not built yet: every run currently reports `mode: full` with
empty lists.

## `change_warning.json`

Whether the codebase changed while the run was in progress (FR-38).

```json
{
  "format_version": 1,
  "run": { … },
  "note": "Best effort, not a guarantee. …",
  "changed": [],
  "removed": [],
  "check_failures": []
}
```

`note` carries fixed wording, and it means what it says: the check compares the contents
the run read against the files as they are now, which narrows the window in which an edit
made during a run goes unnoticed but cannot close it. An empty warning is not a proof of
freshness. `check_failures` lists files that could not be re-read, each with its error —
they are never counted as unchanged.

The post-run check is not built yet: every run currently writes empty lists.

## `diagnostics.json`

Non-fatal anomalies from the run (FR-42; produced on every run, empty when clean).

```json
{
  "format_version": 1,
  "run": { … },
  "diagnostics": [
    {"kind": "symlink_skip", "path": "link.py", "line": null, "col": null,
     "message": "link.py: symbolic link targets /elsewhere, outside the root folder; not followed",
     "extra": {}}
  ]
}
```

`kind` is one of: `unresolved_call`, `detector_error`, `probe_failure`, `symlink_skip`,
`span_missing`, `gitignore_problem`, `change_check_failure`,
`unresolved_entry_declaration`. `line`, `col` and `path` are present when they are known
and `null` otherwise; `extra` carries kind-specific detail.

## `deadcode.json`

Code unreachable from any detected entry point (FR-19).

```json
{
  "format_version": 1,
  "run": { … },
  "caveat": "Approximate result. …",
  "no_entry_points_warning": true,
  "unreachable": [
    {"file": "pkg/util.py", "functions": [{"id": "python:pkg.util.helper", "name": "helper", "start_line": 12}]}
  ]
}
```

- `caveat` is fixed text and **must be reproduced in every presentation of this report**.
  Reachability is static and Python is dynamic: anything reached through `getattr`, a
  registry, reflection, framework dispatch, or an entry point this tool does not detect
  is invisible to it. Entries here are candidates for review, never proof that code is
  unused.
- `no_entry_points_warning` is `true` when the run detected no entry points at all. That
  is the expected outcome for a library, whose entry points are its public API — in that
  case the list says nothing about dead code and must not be read as if it did.

Reachability analysis is not built yet: every run currently writes an empty `unreachable`
list.
