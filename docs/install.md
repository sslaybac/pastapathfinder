# Installing pastapathfinder

pastapathfinder is a Python package. Installing it is a single command, and the tool it
installs runs entirely on the local machine — no network access, no elevated privileges,
no post-install setup steps (FR-32, FR-33, FR-34).

## Requirements

- **Python 3.12 or newer.** The floor is load-bearing, not conservative: the analysis
  engine parses your target code with the running interpreter's grammar, and an older
  interpreter silently drops files that use newer syntax. Develop and run the tool on a
  3.12+ interpreter even if the code you are analyzing targets something older.
- A supported platform: **Linux**, or **Windows via WSL2** (see `wsl.md`). macOS and
  native, non-WSL Windows are out of scope.

## The install command

pastapathfinder is not published to PyPI. You install it from a checkout of the source
tree — a single `pip install` command from the repository root:

```
pip install .
```

That is the whole installation (AC-32.1). It pulls in the three runtime dependencies —
`mypy` (pinned to an exact version), `pathspec`, and `flask` — and puts a
`pastapathfinder` console script on your `PATH`. Confirm it:

```
pastapathfinder --help
```

For development, install editable so source edits take effect without reinstalling:

```
pip install -e .
```

Use a virtual environment if you do not want the tool and its dependencies in your global
site-packages:

```
python -m venv .venv && . .venv/bin/activate
pip install .                 # from the repository root
```

## Offline and unprivileged by construction

- **No admin rights.** Nothing in the tool asks for or needs elevated privileges. It reads
  the codebase you point it at and writes its index and reports to a location under your
  home directory (see `configuration.md` for the exact path). If the output location is not
  writable, the run stops with an error that names the path — it never escalates.
- **No network at runtime.** Once installed, analysis, queries, and the viewer run with all
  external network access blocked. The viewer binds to `127.0.0.1` only and serves its
  frontend from vendored assets; no request ever leaves the machine (FR-33).
- **Install-time network only.** `pip install` itself needs to reach a package index to
  download the wheels, like any Python install. If that download fails, it fails as pip's
  ordinary error — there is no partial-install repair step to run afterward (AC-32.2).

## What you get

Three subcommands, documented in the rest of `docs/`:

- `pastapathfinder analyze <root>` — walk a codebase, build the index, write the reports.
- `pastapathfinder query …` — answer entry-point, slice, node, and dead-code questions
  from the index alone.
- `pastapathfinder view` — serve the local, read-only viewer.

Exit codes are in `exit-codes.md`; the report formats in `report-formats.md`; configuration
and the output-directory layout in `configuration.md`.
