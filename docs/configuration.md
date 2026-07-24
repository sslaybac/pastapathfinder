# Configuration

pastapathfinder runs on sensible defaults and needs no configuration. Everything below is
optional: it exists so you can exclude paths the defaults miss, re-include paths the
defaults exclude, and choose where the tool writes its output.

## The configuration file: `.pastapathfinder.toml`

By default the tool looks for a file named `.pastapathfinder.toml` at the root of the
codebase you analyze. If it is not there, the run proceeds on defaults and `.gitignore`
rules alone — a missing file is not an error. You can point at a different file explicitly:

```
pastapathfinder analyze <root> --config /path/to/config.toml
```

A `--config` file that you name but that does not exist **is** an error: naming a file the
run cannot find would otherwise analyze the wrong thing silently.

The file has exactly two tables, and every key in them is validated. An unknown table, an
unknown key, a value of the wrong type, or an exclusion pattern the matcher cannot compile
each stops the run with an error that names the problem (AC-4.3). A typo is never ignored —
an ignored typo is an exclusion rule you think you have and do not.

```toml
[exclude]
add       = ["generated/", "*.pb2.py"]   # extra patterns to exclude
reinclude = ["vendor/keep_this/"]        # negate any default or .gitignore match

[output]
dir = "/absolute/path/for/index/and/reports"   # optional; overrides the default location
```

### `[exclude] add`

A list of [gitwildmatch](https://git-scm.com/docs/gitignore) patterns — the same syntax as
`.gitignore`. Any path a pattern matches is excluded from analysis and recorded in
`exclusions.json` with the source `user:exclude`, so every exclusion stays auditable.

### `[exclude] reinclude`

A list of gitwildmatch patterns that **restore** paths the defaults or a `.gitignore` would
otherwise exclude. Re-inclusion is the highest-precedence rule: a path matched by both a
default exclusion and a `reinclude` pattern is analyzed. Use it when a convention default is
too broad for your tree — for example, to analyze a vendored subpackage that lives under a
directory the defaults skip.

### `[output] dir`

An absolute path where the index and reports are written, overriding the default location
described below. Relative paths and `~` are expanded and resolved.

## Where output goes

Every run writes one SQLite index (`index.sqlite`) and a `reports/` directory. The tool
never writes into the codebase it analyzes — the output location is deliberately outside the
target tree, so the tool cannot discover or re-analyze its own output, and so read-only
codebases are analyzable.

The location is chosen by this precedence, highest first:

1. **`--out DIR`** on the command line.
2. **`[output] dir`** in the configuration file.
3. **The derived default** (used when neither of the above is set):

   ```
   $XDG_DATA_HOME/pastapathfinder/<basename>-<sha256(abspath-of-root)[:12]>/
   ```

   `$XDG_DATA_HOME` falls back to `~/.local/share` when it is unset (or not an absolute
   path). `<basename>` is the analyzed root's directory name, kept so the directory is
   recognizable when you browse it; the twelve-character digest of the root's absolute path
   keeps two same-named roots from colliding.

`query` and `view` derive the same location from the same rules, so after `analyze` you can
run them with no arguments and they find the index that `analyze` just wrote. Pass the same
`--out` (or set the same `[output] dir`) if you moved the default.

If the chosen output location cannot be created or is not writable, the run stops with an
error that names the path and suggests choosing a writable one — it never asks for elevated
privileges (AC-34.2).

## What is excluded by default

You do not need to configure any of this; it is listed so you know what the defaults already
handle before you reach for `[exclude] add`.

- **Common conventions:** `.git/`.
- **Python conventions:** virtualenv directories (`venv/`, `.venv/`, `env/`, `.env/`,
  `virtualenv/`), build output (`build/`, `dist/`, `*.egg-info/`, `.eggs/`), caches
  (`__pycache__/`, `.mypy_cache/`, `.pytest_cache/`, `.tox/`, `.nox/`), and `node_modules/`.
- **Every `.gitignore` in the tree,** interpreted with gitwildmatch semantics relative to
  each `.gitignore`'s own directory.

Every exclusion, from whatever layer, is attributed in `exclusions.json` with the rule and
source that produced it (see `report-formats.md`). On the first analysis of a codebase the
run points at that report by name, so an over-aggressive default is visible rather than
silent.
