# Exit codes

`pastapathfinder` exits with one of three codes. They are mutually distinct so that
scripts and agents can tell the outcomes apart without parsing any output (FR-43).

| Code | Meaning | When |
|---|---|---|
| `0` | **Success** | The run completed and every discovered, non-excluded file was analyzed. |
| `1` | **Partial success** | The run completed, but at least one file was skipped. The skipped files and their reasons are in `reports/coverage.json`. |
| `2` | **Failure** | The run did not complete. Nothing downstream should treat the output directory as current. |

## What produces each code

**0 — success.** `analyze` walked the tree, analyzed everything it discovered, wrote the
index and all six reports. Excluded paths do not count against success: an exclusion is a
deliberate rule, not a failure, and every one of them is attributed in
`reports/exclusions.json`.

**1 — partial success.** At least one file was discovered and not excluded, but could not
be analyzed — a syntax error, an unreadable encoding, an engine failure. The run still
produced every artifact, covering the files it could analyze. This is the tool's normal
posture on legacy code: analyze what you can, report loudly what you skipped. Read
`reports/coverage.json` for the list and each file's reason.

**2 — failure.** The run stopped before it finished. Causes include:

- the root folder is missing, is not a directory, or cannot be read;
- the output location cannot be created or written (the error names the path — the tool
  never asks for elevated privileges, and never needs them);
- the configuration file is invalid, or contains an exclusion pattern that cannot be
  compiled;
- a report could not be written (the run never substitutes a printed summary for a
  missing report file);
- a command-line usage error, such as an unknown subcommand or a missing argument;
- any unexpected internal error.

Every failure prints a one-line message to stderr. Re-run with `--debug` — accepted on
every subcommand — to get the full traceback instead.

`query` and `view` use the same codes: `0` when the command answered, `2` when it could
not (a missing, unreadable, or version-incompatible index, or an unknown node). They do
not produce `1`, which describes an analysis run's coverage.
