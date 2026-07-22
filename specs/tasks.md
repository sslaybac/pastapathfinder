# pastapathfinder — Implementation Tasks (v1)

**Status:** APPROVED, 2026-07-21. Derived from `specs/requirements.md` (approved 2026-07-16, revised 2026-07-18) and `specs/design.md` (approved 2026-07-20). This document adds no requirement and makes no design decision. Five task-level ambiguities were surfaced during derivation rather than guessed at; all five were resolved with the stakeholder in this session and recorded as design.md decisions **D16–D20** (see §5 for the record and §2 for the tasks they touch).

**How to use this document (implementing agents).** You will be given exactly one task. Before starting it, re-read the FR/AC it references in `specs/requirements.md` (including §4's conventions block) and the design.md sections it references — the task summarizes, the specs govern (CLAUDE.md rule 1). A task's *Verification* block is the minimum; it is not a licence to skip an AC the task's requirements list. Divergence between spec and implementation reality is a stop, not a workaround (CLAUDE.md rule 4).

**Sequencing mandate.** Milestones 1–4 (the batch pipeline) complete before Milestone 5 (the viewer), per requirements §7 and design.md §1. Do not reorder across that boundary.

**Benchmark pins (design.md §8-O5, copied verbatim from the findings so no task has to rediscover them):**
- Django core — `github.com/django/django`, commit **`274df4df0bca7fcfb5c1c1d49567f770df147eeb`**; analyze the **`django/` package subdirectory**, not the repo root (908 `.py` files, 131,294 code lines; the repo root is ~4× larger and would misrepresent FR-29). Source: `prototypes/engine-eval/FINDINGS-harness.md` §2.
- pandas — `github.com/pandas-dev/pandas`, commit **`f6df82f9d0bdba793cbe34251f57c5d6e3fe804c`**; analyze the **`pandas/` package directory** (1,418 `.py` files, 664,190 LOC). Source: `prototypes/engine-eval/FINDINGS-session5.md` Part 2.

---

## 1. Milestone overview

| # | Milestone | Done when |
|---|---|---|
| **M1** | **Walking skeleton: discovery, exclusion, store, reports** | `pastapathfinder analyze <root>` walks a real tree, applies default/`.gitignore`/user exclusion rules, creates a versioned SQLite index, writes all six structured reports plus their human renderings, emits progress, and exits 0/1/2 correctly. Graph extraction is still a test double. |
| **M2** | **Real Python call graph** | The same command run against the pinned Django core benchmark drives mypy 2.3.0 in-process, produces file/module/function/class nodes with spans, `calls`/`contains`/`imports` edges, ambiguity flags and external leaf nodes, completes well inside the FR-29 bound, and reconciles coverage. |
| **M3** | **Entry points and queries — the flagship works from the CLI** | `pastapathfinder query entry-points`, `query slice --from … --direction forward\|backward`, `query node`, and `query dead-code` answer from the index alone; reachability and the dead-code report (with caveat) are written on every run. |
| **M4** | **Incremental, fresh, deterministic, benchmarked** | Re-analysis after a small edit re-processes only changed files and their dependents, provably equivalent to a full rebuild; post-run change detection warns on mid-run edits; two identical runs diff clean under the FR-44 comparator; the pinned benchmark suite asserts FR-29/FR-30/AC-29.3 on the reference machine. |
| **M5** | **Viewer, documentation, platform verification** | `pastapathfinder view` serves a localhost SPA that lists entry points, opens bounded forward/backward traces, shows source locations, and refuses an incompatible index; `docs/` is complete; FR-31/33/34 are verified, including one WSL2 pass. |

---

## 2. Task list

### Milestone 1 — Walking skeleton

- [x] **Task 1.1 — Project scaffolding, tooling, and the standing import-discipline test**
  - **Deliverable:** An installable package (`pip install -e .`) exposing the `pastapathfinder` console script with `analyze` / `query` / `view` subcommands parsed per design.md §5.1, top-level exception trapping, `--debug`, and the exit-code mapping; `ruff` and `pytest` configured; the repository tree of design.md §6 created (empty modules where later tasks fill them).
  - **References:** design.md §3.1, §5.1, §6, D2, D10; requirements FR-32, FR-43, FR-23 (AC-23.1), FR-25 (AC-25.1).
  - **Dependencies:** none.
  - **Verification:**
    - `pyproject.toml` pins `mypy==2.3.0` **exactly**, plus `pathspec` and `flask`, with `requires-python >= 3.12`; `pip install -e .` succeeds and `pastapathfinder --help` exits 0.
    - Unit test: an unknown subcommand and a missing required argument exit **2** (argparse's native code, per D10); an exception raised inside a subcommand handler is caught at `main()` and mapped to exit **2** with a one-line message, with the traceback appearing only under `--debug`.
    - Unit test `tests/unit/test_import_discipline.py` (the standing AC-23.1 / AC-25.1 guard): greps every source file under `src/pastapathfinder/` and fails if any module outside `src/pastapathfinder/adapters/python/` imports `mypy.*`, or if anything under `src/pastapathfinder/viewer/` imports `mypy.*` or `pastapathfinder.adapters.*`. It passes vacuously today and must never be weakened.
    - `ruff check` and `ruff format --check` pass; `pytest` runs green.

- [x] **Task 1.2 — `schema.py` + `index.py`: node-ID grammar, DDL, validation, versioning, canonical writes**
  - **Deliverable:** The single source of the data model — the §4.2 DDL, `SCHEMA_VERSION = 1`, the §4.1 node-ID grammar with its validating regex, the row dataclasses (`GraphFragment`, `FileRecord`, `NodeRow`, `EdgeRow`, `SkipRecord`, `Diag`), `DEADCODE_CAVEAT`, `validate_fragment()`, and an index store that creates/opens the SQLite file, enforces the schema version, writes through a canonical-sort layer, and supports atomic full writes (`index.sqlite.tmp` + rename) and transactional merges.
  - **References:** design.md §3.8, §4.1, §4.2, §4.3, D3, D4, D12; requirements FR-20, FR-21, FR-22, FR-23 (AC-23.2), FR-37 (storage), FR-39, FR-40 (storage), FR-44 (write discipline), EC-13.
  - **Dependencies:** 1.1.
  - **Verification:**
    - AC-22.1/22.2: every written node ID matches the grammar; a fragment containing a non-namespaced ID is rejected with an error naming the offending row and nothing is stored.
    - AC-23.2: a fragment with an unknown `kind`, or an edge whose endpoint ID exists neither in the fragment nor in the index, is rejected with a validation error naming the row.
    - AC-39.1/39.2/39.3: an index written by the store carries `meta.schema_version`; opening an index whose `meta.schema_version` is `"2"` fails with an error naming found and supported versions; an index with the key missing, or with an unreadable/corrupt `meta` table, is treated as incompatible — never as current.
    - AC-21.1: a test asserts the `kind` CHECK sets contain only the generic vocabulary (`file|module|function|class|entry_point`, `calls|contains|imports`) — no Python-specific kind.
    - AC-21.2/AC-40.3: queries over nodes/edges succeed when `attrs` is `{}` and when `is_ambiguous` is absent/0.
    - FR-44: writing the same fragment set twice in different insertion orders produces byte-identical database content apart from the §5.4 volatile `meta` keys (`created_at`, `run_id`).

- [x] **Task 1.3 — `config.py` + `exclusions.py`: the layered rule engine**
  - **Deliverable:** TOML config loading (design.md §5.5) and the `pathspec`/gitwildmatch `RuleSet` composing, in precedence order, the common convention set (`.git/`), the normative v1 Python convention set (design.md §3.3), every `.gitignore` in the tree, user `exclude`, and user `reinclude` as highest-precedence negation — with every match attributed `(pattern, source)` where source ∈ `default:common | default:python | gitignore:<relpath> | user:exclude`.
  - **References:** design.md §3.2, §3.3 (`exclusions`), §5.5, D11; requirements FR-2, FR-3, FR-4, FR-5 (data side), OQ-3's settled list.
  - **Dependencies:** 1.1.
  - **Verification:**
    - AC-2.1: a fixture tree with `venv/` yields no candidate under it and one exclusion record attributed to the Python convention set. The full normative list of design.md §3.3 is covered by a table-driven test.
    - AC-2.2: a convention entry matching nothing does not fail or warn the run.
    - AC-3.1: a `.gitignore` pattern matching a directory of `.py` files excludes them, attributed `gitignore:<relpath>`; patterns are interpreted relative to the `.gitignore`'s own directory, verified with a nested `.gitignore` fixture.
    - AC-3.2 (failure): an unreadable `.gitignore` and one containing an unparseable line each produce a warning naming file and line, a `gitignore_problem` diagnostic, and a completed run using the remaining rules.
    - AC-4.1/4.2: `reinclude` restores a default-excluded path to candidacy; a user `exclude` pattern excludes and is attributed `user:exclude`.
    - AC-4.3 (failure): an invalid pattern (rejected by `pathspec`) and malformed TOML each terminate the run with an error naming the pattern/file — never a silent ignore.

- [x] **Task 1.4 — `discovery.py`: enumeration, probing, symlink and pruning rules**
  - **Deliverable:** `discover(root, ruleset) -> DiscoveryResult` implementing design.md §3.3's normative walk: directory-rule pruning (an excluded directory is one exclusion entry; its contents are not enumerated), `.py` candidates, the shebang probe for extensionless files (≤ 256 bytes of line 1, must start `#!` and contain `python`), symlink handling, and root-error termination.
  - **References:** design.md §3.3 (`discovery`), §8-O1; requirements FR-1 (AC-1.1–1.6), FR-2, EC-10, EC-11.
  - **Dependencies:** 1.3.
  - **Verification:**
    - AC-1.1/1.2: nested `.py` files at any depth become candidates; non-source files never do.
    - AC-1.4/1.5: an extensionless file whose first line is `#!/usr/bin/env python3` is discovered; a binary extensionless file and one with a non-Python shebang are not inputs; an unreadable probe emits a `probe_failure` diagnostic and the walk continues.
    - AC-1.6/EC-11: a file symlink whose real path is outside the root is skipped with a `symlink_skip` diagnostic; one inside the root is analyzed once (dedupe by realpath); a directory-symlink cycle terminates (directory symlinks are not followed).
    - AC-1.3 (failure): a missing root and an unreadable root each terminate the run with an error naming the path and reason.
    - EC-10: an empty root, and a root containing zero recognized sources, both complete with zero candidates.
    - Pruning: a fixture with 500 files under an excluded directory yields exactly one exclusion record for that directory and zero enumerated files beneath it.

- [x] **Task 1.5 — `adapters/base.py`, `runner.py`, `reports.py`, `progress.py`: end-to-end `analyze` with a stub adapter**
  - **Deliverable:** The `LanguageAdapter` protocol exactly as given in design.md §3.4; the run orchestrator following design.md §1's order; writers for the six JSON reports of design.md §5.3 (each stamping `format_version: 1`) plus their stdout renderings generated from the parsed JSON; the stderr progress sink; the `--out` directory derivation of design.md §5.1; and `docs/report-formats.md` + `docs/exit-codes.md` documenting what this task ships. A stub adapter under `tests/` (a test double, not product code) supplies fragments so the whole path is exercisable now.
  - **References:** design.md §3.4, §3.10 (`reports`, `progress`, `runner`), §3.1 (exit-code computation), §5.1, §5.3, §5.4, D9, D10; requirements FR-5, FR-7, FR-23 (structure), FR-34, FR-41, FR-42, FR-43.
  - **Dependencies:** 1.2, 1.4.
  - **Verification:**
    - AC-42.1/42.4: every report parses as JSON and carries `format_version`; a consumer given `format_version: 2` refuses rather than misreading.
    - AC-7.1/42.2: `entries_discovered = files_analyzed + files_skipped + entries_excluded` (D17) is asserted from `coverage.json`'s `counts` block alone before the file is written; a deliberately injected mismatch fails the run loudly. A fixture mixing a pruned directory with individually-matched `.gitignore` files asserts the directory contributes exactly 1 to `entries_excluded` and each matched file contributes 1, with `is_dir` set correspondingly on the coverage rows.
    - AC-7.2: a stub-reported skip carries a human-readable reason.
    - AC-7.3/42.3 (failure): an unwritable output directory terminates the run with an error naming the location; no human-readable rendering is substituted for a missing structured report.
    - AC-5.1/5.2/5.3: exclusions appear with their rule; a first run (no prior index) prints the exclusion-report path prominently; an exclusion-free run still writes `exclusions.json` with `none_excluded: true`.
    - AC-43.1/43.2/43.3: exit 0 with zero skips, 1 with ≥ 1 skip, 2 on a terminating error — asserted as three distinct integers.
    - AC-41.1/41.2: progress lines `processed/total` appear on stderr at ≤ 5 s intervals during per-file phases (test with a fake clock); when the total is unknown, an activity indication for the current phase is emitted instead of silence.
    - AC-34.2: an unwritable `--out` produces a permissions error naming the path and no elevation request.
    - `diagnostics.json`, `reanalysis.json`, `change_warning.json`, `deadcode.json` are produced on a clean run with empty lists (the C-10 convention), even where later tasks fill their content.

### Milestone 2 — Real Python call graph

- [x] **Task 2.1 — `adapters/python/mypy_driver.py`: the engine boundary**
  - **Deliverable:** The only module that calls mypy. Builds `Options` with the normative settings of design.md §3.5 (`incremental=True`, `cache_dir=<out>/mypy_cache`, `export_types=True`, `preserve_asts=True`, `check_untyped_defs=True`, `no_site_packages=True`, per-module `ignore_missing_imports=True` for `*`, `follow_imports="normal"`, error display suppressed), calls `mypy.build.build()`, exposes `BuildResult.graph`/`.types`, the build manager's **rechecked-modules** report, and `engine_meta`; converts per-file failures to `SkipRecord`s; converts a whole-build crash to a one-shot full-rebuild fallback (cache wiped) and fails the run with the engine error if the rebuild also crashes; drives the design.md §3.10 heartbeat during the opaque build phase.
  - **References:** design.md §3.5 (`mypy_driver`), D1, D1a, D13, §3.10 (`progress`); requirements FR-6, FR-13, FR-24 (AC-24.3 entry point), FR-41 (AC-41.2).
  - **Dependencies:** 1.5.
  - **Verification:**
    - **Traps that are already paid for — reproduce, don't rediscover** (`FINDINGS-mypy.md` §2): `options.mypy_path` must be the build root or sibling imports silently become `Any`; keep file-root and build-root distinct (build root is the *parent* of the analyzed package); a warm build with nothing changed reloads **zero** trees, so never infer the re-extraction set from tree presence.
    - AC-13.1: analysis of a fixture importing packages absent from the environment completes under FR-6 semantics (`no_site_packages=True` + isolated environment).
    - AC-13.2 (failure): the FR-13 witness fixture — a module whose body, a function `boom()`, and `Detonator.__init__` each write a witness file on execution — is analyzed with **no witness file created** and the analyzed modules **never present in `sys.modules`** (procedure from `FINDINGS-mypy.md` §FR-13).
    - AC-6.1: a tree containing one file with a Python syntax error completes, producing results for the rest and a `SkipRecord(parse_error)` for the offender; EC-12: a non-UTF-8 file yields `encoding_error`.
    - AC-6.2 (failure): a tree in which *every* file fails to parse still completes and the run output states explicitly that no files were analyzed.
    - AC-24.3 path: a corrupted mypy cache directory triggers exactly one wipe-and-rebuild, and the fallback is reported (not silent).
    - AC-41.2: the build phase emits `analyzing (engine build) … {elapsed}s` at ≤ 5 s intervals.
    - A timed cold build over the pinned Django `django/` package completes (reference: build 10.5 s, 390 MB peak RSS — `FINDINGS-mypy.md` Q2).

- [x] **Task 2.2 — `normalize.py` + `extract.py` part 1: nodes, spans, `contains` and `imports` edges**
  - **Deliverable:** Node-ID construction per the §4.1 grammar (module-name derivation from relpath, `<lambda#N>` per-scope counter, `@line` collision suffixing); the AST walk emitting the file node, the module-body node (`kind='module'` per D16, `attrs.python_role="module_body"`), function/method/class nodes with `file_path`/`start_line`/`end_line`, `contains` edges (file→defs, class→methods), and `imports` edges (file→file, restricted to analyzed files).
  - **References:** design.md §3.5 (`normalize`, `extract` — node half), §4.1, §4.2, D16; requirements FR-12 (node half), FR-21, FR-22, FR-37.
  - **Dependencies:** 2.1.
  - **Verification:**
    - The hand-rolled AST walker replicates `mypy.traverser.TraverserVisitor`'s child-visiting map and descends **structural children only** — never `.node`/`.info`/`.type` semantic pointers. mypyc-compiled traits cannot be subclassed (`FINDINGS-mypy.md` §2 trap 1), so subclassing is not an option; a unit test asserts the walker never leaves the module it was given.
    - AC-37.1: every function/class/entry node carries relative path and start/end lines, checked against a fixture with known spans (including decorated defs, nested classes, async defs).
    - AC-37.2/37.3 (failure): an element whose span cannot be determined yields path-only plus a `span_missing` diagnostic — never a fabricated span.
    - AC-22.1: every emitted ID carries the `python:` namespace and validates against the §4.1 regex; collision fixtures (two same-named functions in one module) exercise the `@line` suffix; a lambda fixture exercises `<lambda#N>`.
    - `contains` edge shape verified on a fixture with module-level functions, a class with methods, and nested defs; `imports` edges appear only between analyzed files.
    - Exactly one `module` node per analyzed file, ID ending `.<module>`, `kind='module'` (D16) — a test asserts it validates against the §4.2 CHECK set and that no `file` node carries outgoing `calls` edges.
    - Walker coverage on the pinned Django benchmark is ≥ 99.9 % of enumerated call sites (reference: 37,207 / 37,218 matched, 0.03 % miss — `FINDINGS-mypy.md` §2).

- [x] **Task 2.3 — `extract.py` part 2: the call-resolution ladder and ambiguity flags**  ⚠ *highest-risk*
  - **Deliverable:** `calls` edges per design.md §3.5's normative ladder — bound `NameExpr`/`MemberExpr` `.node` fullname → single target; instance member via the expression-type map → `TypeInfo.get(name)` through the real MRO; constructor `C()` → `__init__` through the MRO; multiple candidates (overloads, union receivers) → one edge each with `is_ambiguous=1`; typeshed/out-of-set fullnames handed to `externals.py` (task 2.4); nothing at all → an `unresolved_call` diagnostic carrying file/line/col and the callee source text. Multiple call sites for one `(src, dst)` collapse into one edge with sorted `attrs.call_sites`.
  - **References:** design.md §3.5, §4.2, §4.3, D1, D16, R3, and requirements C-11's documented gap; requirements FR-12, FR-14, FR-40.
  - **Dependencies:** 2.2.
  - **Verification:**
    - AC-12.1: an unambiguous direct call between two analyzed functions yields exactly one `calls` edge.
    - **`src` attachment (D16, normative):** a fixture containing a top-level call, a decorator on a top-level def, a class-level statement (`x = compute()`), a decorator on a method, and a call inside a method asserts each edge's `src` is the nearest enclosing *executed* scope — module node, module node, class node, class node, method node respectively. No synthetic module→class edge is emitted.
    - AC-14.1/40.1: a fixture whose call site has multiple statically possible targets yields an edge to each, **all** flagged `is_ambiguous=1`; AC-40.2: a single unambiguous resolution is unflagged.
    - AC-14.2 (failure): a fully unresolvable site (`getattr`-dispatch, bare-name value flow) produces an `unresolved_call` diagnostic with file/line/col/callee text, and no edge — never a silent omission.
    - MRO correctness: the diamond fixture `D(B, C)`, `B(A)`, `C(A)` where `A` and `C` define `__init__` — `D()` must resolve to **`C.__init__`** (C3), per `FINDINGS-mypy.md` Q6.
    - Chained-call pairing: `a()()` and `x.f().g()` report the same `(line, col)` for every call in the chain; sort mypy `CallExpr`s by extent and pair index-by-index with enumerated sites, or `direct_calls` collapses onto one node (`FINDINGS-mypy.md` §2 trap 4). A fixture asserts both calls in a chain get distinct edges.
    - `attrs.call_sites` is sorted and stable across runs.
    - Recall is *not* a pass/fail criterion for this task: mypy resolves ~62.9 % of Django call sites and the residual gap is documented and accepted (design.md R3, requirements C-11). Record the measured unresolved rate on the Django benchmark for the record (reference: 13,786 unresolved of 37,218 sites, 37 %).

- [ ] **Task 2.4 — `externals.py`: external leaf nodes (D15's two sources)**
  - **Deliverable:** External-node emission from (a) mypy resolving a call to a fullname outside the analyzed set, and (b) the adapter's per-module import table supplying the qualified name where mypy left the site unresolved but the callee is a name imported from an unanalyzed module; nodes deduplicated by qualified name, `is_external=1`, no span, no outgoing edges (enforced). The import table is a reusable adapter facility (the Django URLconf detector consumes it in task 3.3).
  - **References:** design.md §3.5 (`externals`), D15, §4.2; requirements FR-36, FR-14 (AC-14.2 fallthrough).
  - **Dependencies:** 2.3.
  - **Verification:**
    - AC-36.1: a call into an imported third-party library yields one external leaf node with the qualified name plus a `calls` edge from the caller.
    - AC-36.2: a call into a function in an *excluded* file is represented the same way, and the file's exclusion remains attributed in `exclusions.json`.
    - AC-36.5: the same external symbol called from three sites yields exactly one external node.
    - AC-36.4 (failure): a site with neither a resolution nor an import-table entry produces an `unresolved_call` diagnostic — a guessed external name is never fabricated. A unit test asserts no external node has an outgoing edge and none carries a span (AC-37.2).
    - Evidence anchor: source (b) measured 100 % precision on the relevant category (`FINDINGS-namematch.md` §2, `external` row) — that measurement is why this mechanism is in scope; do not extend it into general name matching, which is backlog B-22 and out of v1 scope.

- [ ] **Task 2.5 — Wire the Python adapter into the run; validate against Django core**
  - **Deliverable:** The stub adapter is replaced by `adapters.python` in the real run path; the runner reconciles adapter output into coverage/skip/diagnostics reporting; `pastapathfinder analyze` produces a populated index for a real codebase.
  - **References:** design.md §1 (data flow), §3.4, §3.10 (`runner`), §7; requirements FR-6, FR-7, FR-12, FR-23, FR-29 (AC-29.1).
  - **Dependencies:** 2.4.
  - **Verification:**
    - End-to-end run over the pinned Django `django/` package: completes, writes the index and all six reports, `discovered = analyzed + skipped + excluded` holds, exit code 0 or 1 per skips.
    - AC-29.1: wall-clock from invocation to all artifacts written is **≤ 10 minutes** on the reference machine (requirements §4.8). Reference measurements to compare against: mypy build 10.5 s + bulk extraction 0.8 s + discovery/parse ~1.9 s = **~11.3 s**, 390 MB peak RSS (`FINDINGS-mypy.md` Q2). An outcome within the bound but an order of magnitude off this reference is a signal worth reporting, not a pass to wave through.
    - AC-23.1: the task 1.1 import-discipline test still passes with the adapter present.
    - The run is repeated once and the index compared informally for gross instability (the formal FR-44 gate arrives in task 4.3).

### Milestone 3 — Entry points and queries

- [ ] **Task 3.1 — `detectors/`: registry, error isolation, and the `__main__` detector**
  - **Deliverable:** `detectors/base.py` + `registry.py` holding the ordered list of design.md §3.7, the two detector shapes (per-module: `(module_path, stdlib_ast_tree, import_table)`; project-level: the metadata file set), per-detector exception wrapping, entry-node emission (`kind='entry_point'`, §4.1 ID form, one `calls` edge to the target), and `main_block.py`.
  - **References:** design.md §3.7, §4.1, D14, D18; requirements FR-8, FR-9.
  - **Dependencies:** 2.5.
  - **Verification:**
    - D18 (normative): detectors run over **all** analyzed files on every proceeding run and take no part in evict-and-merge; the per-module import table is derived from the stdlib AST the detector is handed, not from the adapter, so detectors hold no cross-run state. A test asserts a detector run is a pure function of `(stdlib_ast_tree, metadata files)`.
    - AC-9.1: `if __name__ == "__main__":` (both operand orders, `==`) yields an entry node targeting the **module-body** node; a fixture with no such block yields none.
    - AC-9.2 (failure): a file that failed to parse emits no entry node and already appears in the skip report.
    - AC-8.1: adding a detector touches only one new module plus one registry list entry — asserted by a test that registers a dummy detector without editing any existing detector or `schema.py`.
    - AC-8.2 (failure): a detector raising on one file produces a `detector_error` diagnostic naming detector and file while every other detector still runs over every other file.
    - Detectors parse with **stdlib `ast`**, not mypy trees, so isolation holds even where semantic analysis failed — asserted by a test in which the mypy build produced no tree for a file that still parses under `ast`.

- [ ] **Task 3.2 — `detectors/console_scripts.py`: packaging-declared CLI entry points**
  - **Deliverable:** Static extraction of `[project.scripts]` and `[project.entry-points.console_scripts]` from `pyproject.toml`, `[options.entry_points]` from `setup.cfg`, and a literal `entry_points` argument from `setup.py` via an `ast` walk; resolution of `pkg.mod:func` against index node IDs.
  - **References:** design.md §3.7 (`console_scripts`); requirements FR-10 (and C-1's packaging-only scope), FR-13.
  - **Dependencies:** 3.1.
  - **Verification:**
    - AC-10.1: a declared console script pointing at a function in an analyzed file yields an entry node for that function.
    - AC-10.2 (failure): a declaration naming an unresolvable target yields an `unresolved_entry_declaration` diagnostic — never a silent drop.
    - FR-13: `setup.py` is **parsed, never executed** — a fixture whose `setup.py` writes a witness file on execution leaves no witness; a computed (non-literal) `entry_points` value is recorded unresolved rather than evaluated.
    - Bare scripts with neither a `__main__` guard nor a packaging declaration are **out of v1 scope** (backlog B-20) — a fixture asserts none is detected.

- [ ] **Task 3.3 — `detectors/flask_fastapi.py` + `detectors/django_urlconf.py`: web-framework routes**
  - **Deliverable:** Both route detectors per design.md §3.7's normative rules, including `attrs.route` population and the Django URLconf's import-table-driven view resolution (`Name`/`Attribute` → function node, `X.as_view()` → class node, `include("mod")` → recurse).
  - **References:** design.md §3.7, §4.2 (`attrs` vocabulary), D18; requirements FR-11.
  - **Dependencies:** 3.1. (The import table comes from the detector's own stdlib AST per D18 — **not** from `externals.py`, whose tables exist only for re-extracted modules.)
  - **Verification:** Reprise the prototype fixture designs (`FINDINGS-harness.md` §2), which already encode the interesting cases and their negative controls:
    - Flask fixture — 5 routes via `@app.route`/`@app.post`, including a `DETAIL_RULE` module constant and a `PREFIX + "/search"` concatenation as **variable rule strings** (route detected, literal path absent from `attrs`); `not_a_route()` must **not** be flagged (AC-11.1).
    - FastAPI fixture — 6 routes via `@app.get/post`, including an `APIRouter(prefix="/admin")` whose routes become reachable only via `include_router`, and a `VERSION_PREFIX + "/status"` variable rule; `helper()` must **not** be flagged. `attrs` distinguishes the `app` receiver from the `APIRouter` receiver.
    - Django fixture — `path("x", views.foo)` and `path("y", FooView.as_view())` both resolved (AC-11.2); `unreferenced()` not flagged.
    - AC-11.3 (failure): the Django fixture's **loop-appended `reports/*` patterns** are the deliberate negative control — they must be recorded as `unresolved` diagnostics, **not silently missed**, and no fabricated route is emitted. Same for dynamic FastAPI `add_api_route` registration.

- [ ] **Task 3.4 — `queries.py`: slices, reachability, dead code, and their run integration**
  - **Deliverable:** `slice(index, node_id, direction, max_nodes=200)` as a SQLite recursive CTE over `kind='calls'` edges (forward `src→dst`, backward `dst→src`) in BFS order under a node budget, returning `SliceResult(nodes, edges, truncated, frontier)`; `reachability(index)` writing `reachable` during every analyze run; `dead_code(index)` producing the grouped unreachable set paired with `DEADCODE_CAVEAT`; `deadcode.json` populated by the runner.
  - **References:** design.md §3.9, §5.3, D5, D16, §8-O2; requirements FR-15, FR-16, FR-17, FR-18, FR-19, FR-28 (bound), EC-6, EC-9.
  - **Dependencies:** 3.1–3.3.
  - **Verification:**
    - AC-15.1: on a fixture graph, the forward slice contains exactly the transitively reachable nodes and connecting edges — not the whole graph (asserted by exact set equality against hand-computed expectations).
    - AC-15.2 (failure): a node with no outgoing call edges returns an **empty slice result**, not an error.
    - AC-16.1: the backward slice from X contains its transitive callers; AC-16.2 (failure): an unknown ID raises `UnknownNodeError` naming the identifier.
    - AC-17.1: an arbitrary non-entry-point function slices in both directions; AC-17.2 (failure): a `file` node raises `NotSliceableError` naming the kind (sliceable kinds per D16: `entry_point`, `function`, `class`, `module`). A forward slice from a `module` node returns its import-time call chain.
    - AC-36.3: a slice reaching an external leaf node includes it as a terminus.
    - AC-28.2: a fixture graph exceeding `max_nodes` returns `truncated=True` with a non-empty `frontier` — visibly bounded, never silently trimmed. The 200 default is a **provisional design parameter** (design.md §8-O2), so it must be a named constant with a single definition site.
    - AC-18.1: functions transitively called from an entry point are marked `reachable=1` in the index after `analyze`.
    - **Derived reachability (D19):** after the BFS, a second pass over `contains` edges sets `class` reachable iff any function it contains is reachable, and `module` reachable iff it is an entry-point target or contains a reachable function. A fixture asserts: a class with one reachable method is `reachable=1`; a class whose methods are all unreachable is `0`; a class instantiated only via an inherited `__init__` reads `0` (the accepted D19 imprecision — assert it rather than treat it as a bug); nodes of other kinds stay `NULL`.
    - AC-18.2/19.3 (failure): with zero detected entry points, reachability is still computed and `deadcode.json` carries `no_entry_points_warning: true`, and the run output warns explicitly rather than declaring the codebase dead.
    - AC-19.1/19.2: unreachable functions appear grouped by file, and every rendering carries `DEADCODE_CAVEAT` verbatim (a test asserts the caveat string is present in both the JSON and the stdout rendering). Per D16, `dead_code()` selects `kind='function'` only — a test asserts no `module` node ever appears in `deadcode.json`, while `reachable` is still written on module nodes in the index.
    - Determinism: repeated slice calls return identical node/edge ordering.

- [ ] **Task 3.5 — `query` CLI subcommands**
  - **Deliverable:** `query entry-points`, `query slice --from NODE_ID --direction {forward,backward} [--max-nodes N]`, `query node NODE_ID`, `query dead-code`, each with `[--out DIR] [--json]`, where `--json` emits the same structured shapes as the design.md §5.2 HTTP API.
  - **References:** design.md §3.1, §5.1, §5.2; requirements FR-20, FR-15–19, FR-43.
  - **Dependencies:** 3.4.
  - **Verification:**
    - AC-20.1: after `analyze` exits, all four query subcommands answer from the index alone — asserted by a test that runs `analyze`, deletes nothing, and queries in a fresh process with the source tree **moved away**.
    - AC-20.2 (failure): a missing index and an unreadable index each produce an error identifying the problem and instructing the user to re-run analysis; AC-39.2: a version-incompatible index is refused by name.
    - Query errors map to exit code 2; `--json` output parses and matches the §5.2 shapes field-for-field (shared serializer with the viewer API — one code path, verified by a test that compares CLI JSON with the API response for the same query in task 5.1).

### Milestone 4 — Incremental, fresh, deterministic, benchmarked

- [ ] **Task 4.1 — `incremental.py`: hash gate, evict-and-merge, fallback, re-analysis report**  ⚠ *highest-risk*
  - **Deliverable:** `plan_run(index, candidates) -> RunPlan` and `merge(index, result, plan) -> MergeReport` implementing design.md §3.6, plus `reanalysis.json` population and the `--full` override.
  - **References:** design.md §3.6, D6, D18, §5.3; requirements FR-24 (incl. C-9's transitive-closure floor), FR-30, FR-35, EC-7, EC-13 (pipeline half).
  - **Dependencies:** 2.5, 3.1–3.3 (detector recomputation is part of the merge order), 3.4 (reachability recomputation).
  - **Verification — the three D6 rules are normative and each has a named failure mode proven in `FINDINGS-session5.md` Part 1:**
    1. The re-extraction set is the build manager's **`rechecked_modules`** report, **never** "which graph states carry a tree" — a warm build retains trees for cache-loaded modules that were never re-type-checked (430 observed), whose types are empty, so re-extracting them silently drops their cached edges (the prototype's spurious 8,383-edge gap).
    2. Merge **replaces, not unions**, per rechecked file — delete that file's nodes and its `src_file` edges first.
    3. Edges are keyed by **caller file** (`src_file`) for eviction; after merge, external nodes with zero incoming edges are deleted.
    - **Equivalence test (the decisive one):** cold build → touch a zero-importer leaf → warm build → re-extract only `rechecked_modules` → merge → compare against an independent cold rebuild of the same tree. Result must be **identical, diff 0**. On Django the prototype used `django/core/management/commands/migrate.py` and observed exactly 1 reloaded module and 18,318 == 18,318 edges.
    - **Eviction test:** cache a leaf variant containing a distinctive self-call, rewrite the leaf to remove that call, warm-merge — the stale edge must be **absent** and the merged graph must equal a rebuild.
    - **Entry points are recomputed, not merged (D18):** on every proceeding run, all `entry_point` nodes and edges are deleted and re-emitted by a full detector pass over all analyzed files, ordered *after* the external-node cleanup and *before* reachability. A test deletes a routed view from `views.py`, re-analyzes, and asserts the stale entry node is gone, an AC-11.3 unresolved diagnostic is recorded, and **no cache fallback was triggered**.
    - AC-24.1: zero changed files **and** an unchanged `meta.metadata_hash` → the engine and the detectors are both skipped, nothing is re-parsed, the index is unchanged, and `reanalysis.json` has `mode: skipped_no_changes` with an explicit "no files re-processed" statement (AC-35.2).
    - Metadata gate (D18): editing `pyproject.toml` alone — with no Python source changed — takes the run off the fast path and refreshes console-script entry points. A test asserts the `meta.metadata_hash` combination covers `pyproject.toml`, `setup.cfg`, and `setup.py`, present-only and order-stable.
    - AC-24.2/35.1: exactly one changed file → only that file and its dependents are re-resolved; the report lists the changed file as `content_changed` and each dependent as `dependent`, and **no other files**.
    - AC-35.3: a file present last run and absent now is listed as `removed` and its nodes/edges are evicted.
    - AC-24.3/35.4/30.2 (failure): a corrupt cache or a fragment validation failure on merge wipes caches, runs a full analysis, attributes **every** file `cache_fallback` in the report, and informs the user a longer full run is underway — never silent, never serving corrupt results.
    - AC-30.1: re-analysis after ≤ 5 changed files completes within **30 s** on the reference machine. References: 13.2 s for the 5-file core-module change set (279 files affected) and 2.69 s for a leaf change (`FINDINGS-mypy.md` Q3, `FINDINGS-session5.md` Part 1), **plus ~2.0 s** for D18's full detector parse pass over Django's 908 files (`FINDINGS-harness.md` Q3) — so expect ~4.7 s leaf / ~15 s core against the 30 s bound. Formal benchmark assertion lands in task 4.4.

- [ ] **Task 4.2 — `postrun.py`: post-run change detection**
  - **Deliverable:** The FR-38 check — mtime/size pre-check over enumerated files, hash confirmation of any difference, `change_warning.json` and the stdout warning with the fixed best-effort `note`.
  - **References:** design.md §3.10 (`postrun`), §5.3; requirements FR-38, EC-14.
  - **Dependencies:** 1.5 (report writer), 2.5 (real content hashes).
  - **Verification:**
    - AC-38.1: a file mutated between being read and run completion is named in the warning with a recommendation to re-analyze; the difference is hash-confirmed, not mtime-inferred (a test flips mtime without changing content and asserts **no** warning).
    - AC-38.2: an unchanged run emits empty lists and no warning line.
    - AC-38.3 (failure): a file deleted before completion is listed as `removed`; a file unreadable during the check is reported as a per-file `check_failure`, never treated as unchanged.
    - The `note` field states the best-effort, no-freshness-guarantee wording; a test asserts it is never rendered as a guarantee.

- [ ] **Task 4.3 — `tests/regression/compare.py` and the determinism gate**
  - **Deliverable:** The FR-44 comparator (a dev utility, **not** a shipped CLI command) plus the double-run determinism tests, and the volatile-field register published in `docs/report-formats.md`.
  - **References:** design.md §3.10 (comparator), §5.4, D12; requirements FR-44.
  - **Dependencies:** 2.5, 4.1.
  - **Verification:**
    - The comparator strips exactly the design.md §5.4 volatile fields — index `meta.created_at`, `meta.run_id`; each report's `run*` block — **and nothing else**, then classifies: no remaining diff → equal; diffs consisting *solely* of the presence/absence of `calls` edges (plus external nodes referenced only by those edges) affecting **≤ 0.01 %** of call edges → **in-variance-class, reported as a warning and never silently passed**; anything else → defect / test failure. Threshold basis: 0.003 % measured at pandas scale — 3 edges of 88,228, all targeting `pandas.core.computation.ops._in` (`FINDINGS-session5.md` Part 2).
    - AC-44.1/44.2: two analyze runs over an unchanged fixture tree produce equal indexes and equal reports under the comparator.
    - AC-44.3 (failure): a test that shuffles the candidate-file processing order produces an index that still compares equal — ordering effects outside volatile fields are defects.
    - Determinism must not depend on the launcher: the tests run **without** `PYTHONHASHSEED` pinning (mypy measured seed-independent, `FINDINGS-mypy.md` Q4), and a test asserts equality under both the default seed and `PYTHONHASHSEED=0`.
    - Unit tests for the comparator itself: an injected single-edge difference at 0.001 % is reported as in-variance-class (warning, non-failing); an injected *node* difference, or a call-edge difference above threshold, fails.

- [ ] **Task 4.4 — Benchmark regression suite and the D1a revalidation procedure**  ⚠ *highest-risk*
  - **Deliverable:** `tests/regression/` — a README copying the §"Benchmark pins" hashes **verbatim**, fetch-by-hash scripts, and the three long-running assertions; plus the D1a engine-upgrade revalidation procedure written down as a runnable sequence.
  - **References:** design.md §8-O5, D1, D1a, §3.5 (enumerated mypy internals); requirements FR-29 (AC-29.1, AC-29.3), FR-30 (AC-30.1), FR-44, §4.8 (reference machine and benchmark designations).
  - **Dependencies:** 4.1, 4.3.
  - **Verification (all on the reference machine of requirements §4.8; these are marked slow and excluded from the default `pytest` run):**
    - **Django core, FR-29:** full analyze of `django/` at commit `274df4df0bca7fcfb5c1c1d49567f770df147eeb` completes in **≤ 600 s** (reference: ~11.3 s resolve, 390 MB RSS, 908 files, 0 parse failures). AC-29.2: the bound is a performance assertion, not a timeout — the run is never aborted to satisfy it.
    - **Django core, FR-30:** touch ≤ 5 files, re-analyze, assert **≤ 30 s** (references: 13.2 s core-change / 2.7 s leaf-change).
    - **pandas, AC-29.3:** full analyze of `pandas/` at commit `f6df82f9d0bdba793cbe34251f57c5d6e3fe804c` **runs to completion** producing all artifacts under FR-6/FR-7 semantics; the 10-minute bound is **not asserted**. References: 53.3 s, 1,267 MB peak, 1,418 files, **0 parse failures / 0 file casualties**, 88,225–88,228 edges. Missing compiled `.so` files must not abort the build — pandas ships 41 `.pyi` stubs for `_libs`, and ~3,747 edges are expected to resolve *into* `pandas._libs.*`; an `Any`-collapse there is a regression, not expected behavior.
    - **Determinism at scale:** two pandas runs compared with `compare.py` — expected outcome is *equal* or *in-variance-class* (the 3-edge locus above); anything else fails.
    - **D1a procedure documented:** bump the pin on a branch → micro-suite ground-truth scoring → Django timing vs FR-29/FR-30 → pandas run-to-completion → determinism double-run → record the result before merging. The enumerated mypy internals of design.md §3.5 are reproduced in the README as the upgrade checklist so a failing upgrade localizes fast.

### Milestone 5 — Viewer, documentation, platform verification

- [ ] **Task 5.1 — `viewer/server.py`: the read-only JSON API**
  - **Deliverable:** The Flask app of design.md §3.11 and §5.2 — every endpoint listed there, structured error bodies, `127.0.0.1:<port>` bind (default 8517), debug off, index opened read-only through `index.py`.
  - **References:** design.md §3.11 (`server`), §5.2, D7, D7a, D20; requirements FR-25, FR-20, FR-26–28 (data side), FR-33, FR-39, EC-13, EC-15.
  - **Dependencies:** 3.5.
  - **Verification:**
    - AC-25.1: the task 1.1 import-discipline test covers `viewer/` (no `mypy.*`, no `adapters.*`); every endpoint is answered from the index alone.
    - AC-25.2/20.2/EC-13: with the index missing, unreadable, or schema-incompatible, **every** endpoint returns the structured error (`index_missing` / `index_incompatible`) rather than a partial payload.
    - Error codes `unknown_node` (404) and `not_sliceable` are returned for the corresponding query failures, matching the CLI's errors from task 3.5 (same serializer — a test asserts CLI `--json` and API output agree for the same query).
    - `/api/slice` honors `max_nodes` and returns `truncated` + `frontier`; `/api/entry-points` is sorted by id; `/api/dead-code` calls `queries.dead_code()` and returns `deadcode.json`'s shape minus the `run*` block, caveat included (D20).
    - **D20 invariant:** a test asserts the running server opens **no file other than the index** — the report directory may be absent entirely and every endpoint still answers.
    - FR-33: a test asserts the server binds only 127.0.0.1 and that no request leaves the machine (no outbound sockets during a session).

- [ ] **Task 5.2 — `viewer/static/`: the no-build frontend**  ⚠ *highest-risk*
  - **Deliverable:** `index.html`, `app.js`, `style.css`, and vendored `cytoscape.min.js` + its dagre layout plugin shipped as package data — entry-point list, trace view with forward/backward toggle, node panel, truncation banner with frontier-expand, and full-screen error states.
  - **References:** design.md §3.11 (`static`), D8, §8-O2, §8-O3, R2; requirements FR-25–28, FR-33, EC-15, EC-6.
  - **Dependencies:** 5.1.
  - **Verification:**
    - AC-26.1: all entry nodes are listed and selectable; AC-26.2: with zero entry points the view says so explicitly and offers slice-by-any-node via the `/api/nodes?search=` box.
    - AC-27.1: selecting an entry point and choosing forward renders that slice, and each displayed call edge can be followed to its target node.
    - AC-27.2 (failure): a query error is surfaced verbatim in-view — never a blank view.
    - AC-27.3: the node panel shows `file_path:start–end` for non-external nodes and "external — not analyzed" for external ones.
    - AC-28.1: the standard workflow renders a **slice**, never the whole graph; AC-28.2: when `truncated` is set, the truncation banner and frontier-expand action are visible.
    - EC-15: selecting a node that no longer exists after re-analysis surfaces the unknown-ID error and routes the user back to the entry list — no crash, no stale slice presented as current.
    - FR-33: **no CDN references, no npm toolchain** — a test greps the shipped assets for external URLs and fails on any; the page renders with all external network access blocked.
    - Note for the implementing agent: D8 (Cytoscape.js) is **provisional** per OQ-2's own sequencing and design.md §8-O3. If real slices make it a poor fit, that is a finding to report for the OQ-2/O3 two-sided update — not a licence to change the §5.2 API, which is stable (design.md R2).

- [ ] **Task 5.3 — `docs/`: the owed user-facing set**
  - **Deliverable:** `docs/install.md`, `docs/configuration.md`, `docs/wsl.md`, and a completeness pass over `docs/report-formats.md` and `docs/exit-codes.md` (created in task 1.5, extended by 4.3's volatile register).
  - **References:** design.md §6, §5.1, §5.3, §5.4, §5.5; requirements FR-31, FR-32, FR-42 (AC-42.1/42.4), FR-43, FR-4.
  - **Dependencies:** 5.2 (so the documented workflows are the shipped ones).
  - **Verification:**
    - `report-formats.md` publishes all six §5.3 schemas verbatim including `format_version` semantics, plus the §5.4 volatile-field register; a test parses each report produced by a fixture run against the documented schema.
    - `exit-codes.md` documents 0/1/2 with their FR-43 meanings.
    - `wsl.md` states the FR-31 condition explicitly: the FR-29/FR-30 bounds apply **only** when the target codebase and index reside on the Linux filesystem; codebases under `/mnt/c/...` are analyzable with all functional requirements applying and the performance bounds not asserted.
    - `configuration.md` documents `.pastapathfinder.toml` per §5.5 and the `--out` default derivation; `install.md` documents the single `pip install` command (AC-32.1) and the offline/no-admin posture.

- [ ] **Task 5.4 — Platform and deployment verification (incl. the WSL2 pass)**
  - **Deliverable:** A recorded verification pass of the US-1..US-5 workflows on both supported platforms, plus the offline and unprivileged assertions.
  - **References:** design.md §8-O6, §5.1; requirements FR-31 (AC-31.1–31.4), FR-32, FR-33, FR-34.
  - **Dependencies:** 5.3.
  - **Verification:**
    - AC-31.1: all five workflows function on the reference Linux environment (enterprise-Linux-class; reference AlmaLinux 9).
    - AC-31.2: on WSL2 with codebase and index on the **Linux** filesystem, all workflows function and the FR-29/FR-30 bounds hold (re-run the task 4.4 Django assertions there). Working assumption from design.md §8-O6 is that **no WSL-specific code is required**; if that assumption fails, stop and report it as a design divergence (CLAUDE.md rule 4).
    - AC-31.3: with the codebase under `/mnt/c/...`, analysis completes and produces correct artifacts; bounds are not asserted; path-case and symlink behavior follow the mounted filesystem's semantics.
    - AC-33.1: all workflows function with external network access blocked; AC-34.1: all workflows function as an unprivileged user with read access to the codebase and write access to the output location.
    - AC-32.1: a clean-machine install via the documented command yields a runnable tool with no manual setup beyond documented configuration.

---

## 3. Coverage check

### 3.1 Requirements → tasks

| FR | Task(s) | | FR | Task(s) |
|---|---|---|---|---|
| FR-1 | 1.4 | | FR-23 | 1.1 (guard test), 1.5 (protocol), 2.5 |
| FR-2 | 1.3, 1.4 | | FR-24 | 4.1 |
| FR-3 | 1.3 | | FR-25 | 5.1 |
| FR-4 | 1.3 | | FR-26 | 5.2 |
| FR-5 | 1.3 (data), 1.5 (report) | | FR-27 | 5.2 |
| FR-6 | 2.1, 2.5 | | FR-28 | 3.4 (bound), 5.2 |
| FR-7 | 1.5, 2.5 | | FR-29 | 2.5, 4.4 |
| FR-8 | 3.1 | | FR-30 | 4.1, 4.4 |
| FR-9 | 3.1 | | FR-31 | 5.3, 5.4 |
| FR-10 | 3.2 | | FR-32 | 1.1, 5.3, 5.4 |
| FR-11 | 3.3 | | FR-33 | 5.1, 5.2, 5.4 |
| FR-12 | 2.2, 2.3 | | FR-34 | 1.5, 5.4 |
| FR-13 | 2.1, 3.2 | | FR-35 | 4.1 |
| FR-14 | 2.3, 2.4 | | FR-36 | 2.4 |
| FR-15 | 3.4, 3.5 | | FR-37 | 2.2 |
| FR-16 | 3.4, 3.5 | | FR-38 | 4.2 |
| FR-17 | 3.4, 3.5 | | FR-39 | 1.2, 3.5, 5.1 |
| FR-18 | 3.4 | | FR-40 | 2.3 |
| FR-19 | 3.4, 3.5, 5.2 | | FR-41 | 1.5, 2.1 |
| FR-20 | 1.2, 3.5 | | FR-42 | 1.5, 5.3 |
| FR-21 | 1.2 | | FR-43 | 1.1, 1.5 |
| FR-22 | 1.2, 2.2 | | FR-44 | 1.2 (write side), 4.3, 4.4 |

No FR is unmapped.

### 3.2 Design components → tasks

| design.md component | Task(s) |
|---|---|
| 3.1 `cli` | 1.1, 1.5, 3.5 |
| 3.2 `config` | 1.3 |
| 3.3 `discovery` + `exclusions` | 1.3, 1.4 |
| 3.4 `adapters.base` | 1.5 |
| 3.5 `adapters.python` (`mypy_driver`, `extract`, `externals`, `normalize`) | 2.1, 2.2, 2.3, 2.4 |
| 3.6 `incremental` | 4.1 |
| 3.7 `detectors` | 3.1, 3.2, 3.3 |
| 3.8 `schema` + `index` | 1.2 |
| 3.9 `queries` | 3.4 |
| 3.10 `reports` / `postrun` / `progress` / `runner` | 1.5 (reports, progress, runner), 4.2 (postrun) |
| 3.10 `tests/regression/compare.py` | 4.3 |
| 3.11 `viewer.server` / `viewer.static` | 5.1, 5.2 |
| §6 `docs/` owed set | 1.5 (report-formats, exit-codes), 5.3 |
| §8-O5 benchmark pins, §8-O6 WSL pass | 4.4, 5.4 |

No component is unmapped.

---

## 4. Suggested session boundaries

Review points where the next task should not start until a human has looked:

1. **After 1.2 (schema + index).** Every downstream task is keyed to the ID grammar and DDL. A mistake here is cheap now and expensive after M2.
2. **After 2.3 (call-resolution ladder).** This is where measured resolution quality first meets the spec's FR-14 posture. Expect the unresolved-rate number (~37 % of Django sites) to be uncomfortable; it is documented and accepted (C-11, design.md R3), but it deserves a human look before three more milestones build on it.
3. **After 2.5 (Django full run).** The first FR-29 datapoint from real product code rather than a prototype. Compare against the ~11.3 s reference before proceeding.
4. **After 4.1 (incremental merge).** Silent edge loss or stale-edge retention is the failure mode that would corrupt every later query without any test noticing. The equivalence and eviction tests are the gate; read them, not just their green ticks.
5. **After 4.4 (benchmarks).** This is the conformance checkpoint for FR-29/FR-30/FR-44 on the reference machine — the last point before viewer work where a spec-level trade-off could still be raised cheaply.
6. **Before 5.2 (frontend).** design.md R2 names the viewer the project's highest-mortality component and D8 is explicitly provisional; a scoping conversation before the session is cheaper than a rescue after it.

---

## 5. Design clarifications — all resolved 2026-07-21

Task-level ambiguities found while deriving this breakdown: details the design did not state and that an implementing agent would otherwise have had to guess. None was a design contradiction. All five were worked through with the stakeholder in the step-4 session and resolved as design.md decisions **D16–D20**, each carrying a dated trace at every design.md section it touched. The entries are retained with their IDs (numbering is append-only) so the reasoning stays findable.

**No task in §2 is blocked.**

- **C-1 — What `kind` does the module-body node carry?** ***Resolved 2026-07-21*** — stakeholder-approved amendment recorded as design.md **D16**, with inline traces in §3.5, §3.9, and §4.2. A generic `module` kind is added to the §4.2 CHECK set and to §3.9's sliceable and reachability sets; `dead_code()` reports `function` nodes only, so module bodies leave the dead-code report by construction. The same decision settled where non-function call sites attach: a call site's `src` is the **nearest enclosing executed scope** — enclosing function, else enclosing class body (class-level statements and method decorators), else the module node (top-level statements and decorators on top-level defs). Tasks 1.2, 2.2, 2.3, and 3.4 updated accordingly.
- **C-2 — What unit does `coverage.json`'s `excluded` count?** ***Resolved 2026-07-21*** — stakeholder-approved amendment recorded as design.md **D17**, with edits to §5.3 (schema), §3.10 (the pre-write assertion), and a trace on §8-O1. Coverage counts are renamed to state their units in the data: `entries_discovered`, `files_analyzed`, `files_skipped`, `entries_excluded`, reconciling as `entries_discovered = files_analyzed + files_skipped + entries_excluded`; coverage rows carry `is_dir`. A pruned directory is one excluded entry. Tasks 1.4 and 1.5 updated accordingly.
- **C-3 — Detector scope and entry-node eviction on incremental runs.** ***Resolved 2026-07-21*** — stakeholder-approved amendment recorded as design.md **D18**, with edits to §3.7 (recompute rule, import-table source), §3.6 (change gate and merge order), and §4.2 (`meta.metadata_hash`). Entry points are recomputed wholesale on every proceeding run and take no part in evict-and-merge, which dissolves all three sub-questions; the zero-change fast path skips detectors too, preserving AC-24.1; and the gate additionally compares a combined hash over `pyproject.toml` / `setup.cfg` / `setup.py` so metadata-only edits are not invisible. Tasks 3.1, 3.3, and 4.1 updated accordingly.
- **C-4 — Is `reachable` computed for `class` nodes?** ***Resolved 2026-07-21*** — stakeholder-approved amendment recorded as design.md **D19**, with edits to §3.9 and §4.2. `reachable` is BFS-computed on `function` nodes and **derived** on `class` and `module` nodes from `contains` edges (class reachable iff any contained function is; module reachable iff it is an entry target or contains a reachable function), documented as derived rather than BFS-computed. The one accepted imprecision — a class instantiated only via an inherited `__init__` reads unreachable — under-claims rather than over-claims. Task 3.4 updated accordingly.
- **C-5 — Where does `/api/dead-code` read from?** ***Resolved 2026-07-21*** — stakeholder-approved amendment recorded as design.md **D20**, with edits to §5.2 and §3.11. The endpoint recomputes via `queries.dead_code()` and returns `deadcode.json`'s shape minus the volatile `run*` block; the server opens the index and no other file, making AC-25.1 a testable invariant and keeping the CLI and API on one code path. Task 5.1 updated accordingly.

---

## 6. Highest-risk tasks (recommended personal review)

1. **Task 4.1 — incremental evict-and-merge.** The prototype hit two silent-corruption bugs here (tree-presence over-counting, union-instead-of-replace) and only found them because it diffed against a full rebuild. This is the single task where a passing test suite is most likely to coexist with a wrong index.
2. **Task 2.3 — the call-resolution ladder.** The heart of FR-12/FR-14 and the place where design.md R3's accepted recall ceiling becomes visible in real output. Most likely task to surface a requirements-level conversation (rather than a bug).
3. **Task 4.4 — benchmark regression suite.** Where FR-29, FR-30, AC-29.3, and FR-44 stop being prototype measurements and become product assertions on the reference machine. If any prototype number fails to reproduce in product code, this is where it shows.
4. **Task 2.1 — the mypy driver.** design.md R1's "highest risk" — the whole pipeline rests on a semi-public API, and every trap in `FINDINGS-mypy.md` §2 is a silent-degradation trap (wrong `mypy_path` costs recall without any error).
5. **Task 5.2 — the viewer frontend.** design.md R2's named highest-mortality component, on a provisional stack (D8), against two deliberately open questions (OQ-2, OQ-4).
