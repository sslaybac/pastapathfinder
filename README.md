# pastapathfinder

**Static analysis for legacy and AI-generated Python codebases — finding paths through spaghetti code.**

pastapathfinder analyzes a Python codebase folder and produces a queryable index plus a
set of structural reports, then lets you follow **sliced call traces** — forward or
backward from a chosen entry point — through a local interactive viewer. It exists for the
maintainer who has inherited code they did not write (or that an AI agent wrote) and needs
to understand its structure, dependencies, and change impact without reading the whole
thing.

The flagship capability is the slice: pick an entry point you know — a `main`, a CLI
command, an API route — and trace the flow of calls forward to locate a suspect function,
or backward to see everything that reaches a given node.

## What it does

- **Recursive discovery** of Python sources (`.py` files and shebang-named extensionless
  scripts) under a single root, with layered exclusions: a built-in Python convention set
  (`venv/`, `.git/`, `build/`, `dist/`, …), your `.gitignore`, and project configuration.
- **Call-graph extraction** built on a pinned [mypy](https://mypy-lang.org/) engine, with a
  call-resolution ladder and explicit ambiguity flags where a call site cannot be resolved
  to a single target.
- **Entry-point detection** across several sources: `__main__` blocks, packaging-declared
  console scripts, and web-framework routes (Flask / FastAPI, Django URLconf).
- **Queries** over the index: forward/backward slices, reachability, and dead-code
  (functions no entry point reaches).
- **An interactive viewer** — a local, read-only, no-build web frontend for exploring
  slices visually.
- **Incremental re-analysis**: after the first run, only what changed is re-processed, so a
  trace you are following stays current without a full re-run.
- **Honest coverage**: every discovered file is reported as analyzed, skipped, or excluded,
  with the reason — the analysis never silently hides code.
- **Deterministic output**: two runs over the same codebase, same config, and same tool
  version produce byte-identical reports apart from a small, documented set of volatile
  fields (timestamps and run IDs).

Everything runs entirely on the local machine — no network access, no elevated privileges,
no post-install setup.

## Requirements

- **Python 3.12 or newer.** The floor is load-bearing: the engine parses your target code
  with the running interpreter's grammar, and an older interpreter silently drops files
  that use newer syntax. Run the tool on 3.12+ even when the code you analyze targets
  something older.
- **A supported platform: Linux, or Windows via WSL2** (see [`docs/wsl.md`](docs/wsl.md)).
  macOS and native, non-WSL Windows are out of scope.

## Installation

pastapathfinder is not published to PyPI; install it from a checkout of the source tree:

```
pip install .
```

That pulls in the three runtime dependencies — `mypy` (pinned exactly), `pathspec`, and
`flask` — and puts a `pastapathfinder` console script on your `PATH`. Confirm it:

```
pastapathfinder --help
```

For development, install editable and pull in the dev toolchain (`pytest`, `ruff`):

```
pip install -e '.[dev]'
```

See [`docs/install.md`](docs/install.md) for virtual-environment and offline notes.

## Usage

### Analyze a codebase

```
pastapathfinder analyze /path/to/codebase
```

This writes an `index.sqlite` and six JSON reports to an output directory derived from the
analyzed root. Re-running is automatically incremental; force a full re-analysis with
`--full`. A project may carry a `.pastapathfinder.toml` (auto-detected at the root, or
passed with `--config`); see [`docs/configuration.md`](docs/configuration.md).

### Query the index

```
pastapathfinder query entry-points                       # list detected entry points
pastapathfinder query slice <NODE_ID> --direction forward # the flagship trace
pastapathfinder query slice <NODE_ID> --direction backward
pastapathfinder query node <NODE_ID>                     # one node's details
pastapathfinder query dead-code                          # functions no entry point reaches
```

`--direction forward` follows callees; `backward` follows callers. `--budget` bounds the
number of nodes a slice returns.

### Explore in the viewer

```
pastapathfinder view
```

Serves the local, read-only interactive viewer on `127.0.0.1` (default port configurable
with `--port`). The viewer reads the existing index only — it never re-analyzes.

Every subcommand accepts `--debug` for a full traceback on failure and `--out` to point at
a specific output directory. Exit codes are `0` (success), `1` (analysis/query error), and
`2` (usage error) — see [`docs/exit-codes.md`](docs/exit-codes.md).

## Reports

Each run writes six JSON reports under `<out>/reports/`, overwriting the previous run's.
They are the machine-readable record of what the run did; the summaries printed to stdout
are rendered from them. Every report carries an integer `format_version` and is refused if
the version is unknown. Full field-by-field documentation is in
[`docs/report-formats.md`](docs/report-formats.md).

## Documentation

| Document | Contents |
|---|---|
| [`docs/install.md`](docs/install.md) | Installation, virtual environments, offline/unprivileged operation |
| [`docs/configuration.md`](docs/configuration.md) | `.pastapathfinder.toml` options and exclusion layering |
| [`docs/report-formats.md`](docs/report-formats.md) | The six JSON reports, format versioning, volatile fields |
| [`docs/exit-codes.md`](docs/exit-codes.md) | Exit-code contract (0/1/2) |
| [`docs/wsl.md`](docs/wsl.md) | The Windows/WSL2 Linux-filesystem condition |

## Project layout

- **`src/pastapathfinder/`** — the tool. The analysis pipeline (`runner.py`, `discovery.py`,
  `queries.py`, …), the pinned-engine Python adapter (`adapters/python/`), entry-point
  `detectors/`, and the local `viewer/`.
- **`specs/`** — the documents that govern building the product: `requirements.md`,
  `design.md`, `tasks.md`, `backlog.md`. Source of truth for scope, architecture, and build
  sequencing.
- **`docs/`** — the user-facing documentation set above.
- **`prototypes/`** — empirical findings from prototype sessions (append-only historical
  record), including the engine evaluation behind the mypy choice.
- **`tests/`** — the test suite. Determinism and benchmark regression checks live under
  `tests/regression/`.

## Development

The engine is pinned (`mypy==2.3.0`, exact) and only code under
`adapters/python/` may import `mypy.*`. Style is enforced with `ruff` (lint and format);
tests run with `pytest`:

```
pytest                # default suite
pytest -m slow        # long-running benchmark regression over the pinned codebases
ruff check .          # lint
ruff format .         # format
```

## Status

**v1.0.** All 23 implementation tasks across the five milestones are complete: the analysis
pipeline, the query layer, incremental re-analysis, and the interactive viewer.
