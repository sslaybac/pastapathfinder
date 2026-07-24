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

## Volatile fields — the complete register

Analysis is deterministic: two runs over the same codebase, with the same configuration
and the same tool version, produce the same index and the same reports (FR-44). Exactly
two things are allowed to differ, and this is the whole list.

| Artifact | Field | What it is |
|---|---|---|
| Every report | the `run` block | `run_id`, `started_at`, `finished_at`, `duration_seconds` |
| `index.sqlite` | `meta.created_at` | when the index was written |
| `index.sqlite` | `meta.run_id` | the run that wrote it |

```json
"run": {
  "run_id": "…",             // unique per run
  "started_at": "…",         // ISO-8601 UTC, second precision
  "finished_at": "…",
  "duration_seconds": 0.0
}
```

Nothing else may vary. Every other `meta` key — `schema_version`, `tool_version`,
`engine`, `engine_version`, `root_path`, `metadata_hash` — is compared, as is every node,
edge and file row, and every field of every report outside its `run` block. Lists inside
reports are ordered deterministically: paths and diagnostics sort, they do not appear in
whatever order the filesystem or the engine produced them. Diffing two runs is therefore a
supported way to check that a change to your codebase — or to this tool — did what you
expected.

### One documented exception: engine variance

The analysis engine exhibits rare internal variance at very large scale. Measured: **3
call edges out of 88,228** (0.003 %) differed between two runs of a 664,000-line codebase;
at ~131,000 lines the difference was zero. The variance appears only as a `calls` edge
being present in one run and absent in the other, together with any external node that had
no other reference.

This is documented rather than hidden: a difference of that shape, affecting at most
**0.01 % of call edges**, is a known engine characteristic. Anything else — a missing
function, a changed span, a different `contains` edge, any report difference — is a defect
in this tool, not variance.

The repository's comparison utility (`tests/regression/compare.py`, a development tool and
not part of the installed command) implements exactly that classification: it strips the
fields in the register above, then reports *equal*, *in variance class* (as a warning — it
is never passed over silently), or a *defect*.

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

`analyze` is incremental automatically when a compatible index already exists; `--full`
forces a full rebuild. The first analysis of a codebase — and any `--full` run — reports
`mode: full`. A run in which no file and no packaging-metadata file changed reports
`mode: skipped_no_changes` and re-processes nothing. A run that had to discard an unusable
cache and rebuild from scratch reports `mode: fallback` with every file attributed
`cache_fallback`.

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

The check runs on every analysis. A run during which nothing changed writes empty lists and
prints no warning.

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

Reachability is computed on every analysis: functions reachable from a detected entry point
are marked in the index, and `unreachable` lists the functions that are not, grouped by
file. Per the caveat above, that is a conservative approximation, not proof of unused code.
