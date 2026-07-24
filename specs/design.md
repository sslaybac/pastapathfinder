# pastapathfinder — Technical Design (v1)

**Status:** AMENDED 2026-07-20; amended 2026-07-21 (D16–D21), 2026-07-22 (D22, D23, and §3.5's stdlib-`ast` import table), 2026-07-23 (D19's two clarifications, §3.9's exclusion of external leaves from `dead_code()`, and §8-O7 recorded), 2026-07-24 (D24). Every amendment carries a dated trace at each section it touches; §2's decision log is the index. This is the HOW baseline, consumed by `specs/tasks.md`. It implements `specs/requirements.md` as amended and APPROVED 2026-07-18; it adds, removes, and reinterprets no requirement. Where design work surfaced a requirement-level question, it is flagged in §8, not silently resolved.
**Evidence base:** the engine-evaluation prototype findings (sessions 1–5): `FINDINGS-harness.md`, `FINDINGS-jedi.md`, `FINDINGS-pyright.md`, `FINDINGS-mypy.md`, `FINDINGS-namematch.md`, `FINDINGS-session5.md` (repository location pending the stakeholder's `prototypes/` decision; cited here by filename). All measurements were taken on the reference machine defined in requirements §4.8.
**Audience:** AI coding agents implementing tasks in isolated sessions. Per CLAUDE.md rule 1, re-read the governing FR/AC before each task; this document tells you how, the requirements tell you what. Where this document says "normative," an implementing agent has no discretion.

---

## 1. Overview

pastapathfinder is two programs sharing one artifact:

1. **The analysis pipeline** (`pastapathfinder analyze`) — a batch CLI process that discovers Python source files under a root folder, applies exclusion rules, drives the mypy build API over the survivors, extracts a call/contains/imports graph plus entry points into a SQLite index, computes reachability, and writes the machine-readable reports (coverage, exclusions, re-analysis, change warning, diagnostics, dead code). Runs are incremental by default: unchanged files' results are reused via content hashes and a per-file edge cache layered over mypy's own incremental cache.
2. **The viewer** (`pastapathfinder view`) — a local Flask server bound to 127.0.0.1 that serves a no-build single-page frontend and a small JSON query API. The API reads only the SQLite index; it never touches mypy or any language tooling (FR-25). The frontend lists entry points, opens forward/backward slice views rendered with Cytoscape.js, and displays node source locations.

**Data flow, end to end:** the user runs `analyze <root>` → discovery walks the tree, classifying every enumerated file as analyzed-candidate, excluded (with the rule), or skipped-at-probe → the Python adapter invokes one `mypy.build.build()` over the candidates and walks the resulting typed ASTs, emitting schema-conformant graph fragments (nodes, call/contains/imports edges, per-call-site resolution outcomes) → the import-table fallback attributes calls to unanalyzed imported symbols as external leaf nodes → detectors scan the parsed modules and project metadata, emitting entry-point nodes → the index store validates fragments, canonically sorts, and writes SQLite → reachability BFS marks nodes → report writers emit the JSON artifacts and human-readable summaries → the post-run change check compares recorded hashes against disk. Later, `view` serves slices out of that index; a slice is one recursive SQL query, bounded and visibly truncated before rendering.

The pipeline lands first, the viewer second (requirements §7 sequencing mandate).

## 2. Decision log

Each entry: decision → chosen option → rationale → evidence → requirements served. Rejected options are summarized; full option analysis lives in the Phase A/B record and the FINDINGS files.

**D1 — Analysis engine: mypy, pinned exactly at 2.3.0, driven in-process through `mypy.build.build()`.** Rejected: Jedi and Pyright — per-call-site query interfaces structurally fail FR-30 (125 s and 220 s on the 5-file benchmark vs ≤ 30 s); Jedi additionally has a parso cache bug failing ~30 % of inferences at scale, non-C3 MRO, and content-level nondeterminism; Pyright additionally has a Node dependency and an 881-line concurrency-sensitive adapter. Rejected: PyCG — unmaintained, cannot parse modern Python. mypy measured: Django full build + extraction 11.3 s (FR-29 bound 600 s); core-change incremental 13.2 s and leaf change 2.7 s (FR-30 bound 30 s); pandas 664 k LOC to completion with zero file casualties (AC-29.3); deterministic modulo the FR-44-amended variance class; C3-correct MRO; FR-13 verified by side-effect witness. Its build API is semi-public → policy D1a. Evidence: FINDINGS-jedi/pyright/mypy/session5. Serves FR-12–14, 29, 30, 44, 13. (OQ-1 resolution; requirements §8 updated 2026-07-18.)

**D1a — Engine upgrade policy (normative).** `mypy==2.3.0` exact in `pyproject.toml`. Upgrading mypy is a deliberate maintenance act: bump the pin on a branch, run the revalidation suite (`tests/regression/`) — micro-suite ground-truth scoring, Django timing vs the FR-29/FR-30 bounds, pandas run-to-completion, determinism double-run — and record the result before merging. The internals the adapter touches are enumerated in §3.5 so a failing upgrade localizes fast.

**D2 — Tool implementation language: Python; `requires-python >= 3.12`, developed and verified on 3.13.** mypy's own ecosystem: in-process API, `pip install` (FR-32), no foreign runtime. The ≥ 3.12 floor exists because mypy parses target code with the host interpreter's grammar — session 1 showed Python 3.9 silently dropping `match`/`except*` files. (OQ-7 resolution; requirements §8 updated 2026-07-18.)

**D3 — Index storage: SQLite, single file.** Queryable without loading the whole graph (AC-20.1); transactional in-place incremental updates (FR-24); one-file artifact for FR-44 diff testing; a `meta` table carries FR-39's version. Rejected: JSON files (whole-file rewrite per run; heavy viewer load path), embedded graph DBs (young dependency, no need). Serves FR-20, 24, 39, 44.

**D4 — Schema shape: fixed columns for the generic vocabulary + one JSON `attrs` column.** Generic concepts (`is_external`, `is_ambiguous`, `reachable`, file/span) are columns — they are language-independent, so AC-21.1 is not violated; everything Python-specific rides in `attrs`. Rejected: EAV (miserable queries for no FR-21 gain). Serves FR-21, 22, 36, 37, 40, 18.

**D5 — Slice execution: SQLite recursive CTEs** over `edges` filtered to `kind='calls'`, direction by swapping src/dst; identical code path for CLI `query` and the viewer API. Rejected: in-memory BFS (duplicated logic; revisit only if interaction latency demands it — none expected at slice-bounded sizes), precomputed transitive closure (over-approximation and incrementality both punish it). Serves FR-15–17.

**D6 — Incremental mechanics: content-hash gate → mypy incremental cache → per-file edge cache with evict-and-merge.** Session 5 Part 1 proved the discipline bit-identical to a full rebuild, including eviction. The three hard-won rules are normative in §3.6: (1) the re-extraction set is the **rechecked-modules** report from mypy's build manager, never "which graph states carry a tree"; (2) merge is **replace-not-union** per rechecked file — delete that file's nodes/edges first; (3) edges are keyed by **caller file** (`src_file` column) for eviction. mypy's interface-hash-based recheck set is the C-9-permitted equivalence-preserving narrowing of the transitive-closure floor; the session-5 equivalence proof is the evidence. Serves FR-24, 30, 35. Measured: 13.2 s core / 2.7 s leaf.

**D7 — Pipeline↔viewer boundary: local HTTP server + JSON API expressed purely in index-schema vocabulary.** The API is the verifiable artifact of FR-25/AC-25.1: one server module, zero engine imports. Rejected: sql.js-in-browser (reimplements FR-39 refusal logic in JS; loads the whole index into memory), Tauri/Electron (packaging burden on the highest-mortality component). Serves FR-25–28.

**D7a — Viewer server: Flask, threaded dev-grade server, bound to 127.0.0.1 only, debug off.** Chosen over stdlib `http.server` (more agent-reliable, less hand-rolled routing) and FastAPI/uvicorn (heavier dependency chain for a read-only local API). Localhost bind + vendored assets satisfy FR-33.

**D8 — Graph rendering: Cytoscape.js, vendored into package data; no-build frontend (plain JS/HTML/CSS, no npm toolchain).** Vendoring is mandatory: a CDN reference at runtime violates FR-33. Provisional per OQ-2's own sequencing — confirmed or replaced at the viewer milestone after real slices exist; nothing in the pipeline or API depends on it. Rejected: D3-the-library (bespoke everything under a token budget), Sigma.js (solves whole-graph scale, which FR-28 designs away).

**D9 — Report formats: one JSON document per report, top-level `format_version`, human-readable renderings generated from the parsed JSON at print time** (structured form authoritative by construction). Fixed paths under the output directory, overwritten per run. Serves FR-42, 5, 7, 35, 38, and the C-10 diagnostics convention. Schemas: §5.3.

**D10 — CLI: one executable, three subcommands (`analyze`, `query`, `view`); exit codes 0 = success, 1 = partial success, 2 = failure.** All exceptions are caught at `main()` and mapped to 2 with a message; argparse usage errors (which exit 2 natively) therefore fall in the failure category, and Python's default uncaught-exception exit of 1 can never masquerade as partial success. Serves FR-43, 41.

**D11 — Exclusion matching: `pathspec` (gitwildmatch) for all three rule sources** — defaults, `.gitignore` files, user config — so attribution is uniform `(pattern, source)`. Language-independent defaults (`.git/`) live in a **common convention set** alongside the Python set (the OQ-3 design note's first option: attribution stays clean, future languages reuse it). Serves FR-2–5.

**D12 — Determinism: canonical sort at the write boundary + enumerated volatile fields + variance-class-aware comparison tooling.** Nodes sorted by `id`; edges by `(src, dst, kind)`; JSON with sorted keys and documented list orderings. mypy needs no hash-seed pinning (measured seed-independent); determinism must not depend on the launcher. The prototype-phase conditional on this decision resolved to its content-variance branch: mypy's rare internal variance is irreducible from outside, and the 2026-07-18 FR-44 amendment documents it; the comparison utility (§3.10) classifies diffs as volatile-only / in-variance-class (reported, never silently ignored) / defect. Serves FR-44.

**D13 — Parallelism: none.** Sequential pipeline; the D1 numbers make parallelization moot (11.3 s measured vs a 600 s bound). D12's sort-at-write keeps the door open at zero cost if a future language changes the math.

**D14 — Entry-point detectors: hardcoded in-process registry, one module per detector, per-detector error isolation.** Detectors run on stdlib-`ast` parses (not mypy trees), so AC-8.2's isolation holds even when semantic analysis of a file failed, and detector logic stays engine-independent. No discovery/registration/versioning machinery (§6 item 7 prohibition). Serves FR-8–11.

**D15 — External-call attribution: two-source external nodes inside the Python adapter.** (a) mypy resolves a call to a fullname outside the analyzed set (typeshed/stub or absent-package resolution) → external node with that qualified name. (b) mypy leaves the site unresolved but the callee is a name imported from an unanalyzed module → the adapter's import table supplies the qualified name (the §6-item-10-amended syntactic mechanism; measured 100 % precision on the relevant category, FINDINGS-namematch B-Q1 `external`). Neither source fabricates names: no import edge and no resolution → AC-14.2 diagnostics (AC-36.4). Serves FR-36, and closes the largest honest share of the FR-14/C-11 gap available without the deferred dispatch layer.

**D16 — Module bodies are a first-class `module` node kind; every call site's `src` is its nearest enclosing executed scope (normative).** *Added 2026-07-21; stakeholder-approved in session, resolving the step-4 task-breakdown clarification C-1.* A Python module has two aspects: a namespace of definitions (already the `file` node, with its `contains` edges) and an executable body that CPython compiles to a code object and runs once on import. The second aspect needed a node — §3.5's module-body node — and the four-kind vocabulary had no accurate home for it: `file` is non-sliceable by AC-17.2, and `class` means "a type" (the thing constructor edges resolve through an MRO). `module` is added to §4.2's `kind` set and to §3.9's sliceable and reachability sets. This does not violate AC-21.1: a module/compilation unit is a language-independent concept, not a Python one, so it belongs in the generic vocabulary rather than in `attrs`. Consequence: `dead_code()` reports `function` nodes only, so module bodies — which no `calls` edge can reach except an FR-9 entry point, since `imports` edges are never traversed by reachability — leave the dead-code report by construction rather than by a filter clause; `reachable` is still stored on module nodes, so a future report can use it (how it is populated was subsequently settled by D19). **Attachment rule:** a call site's `src` is the nearest enclosing *executed* scope — the enclosing function, else the enclosing class body (class-level statements and decorators on methods, both executed by the class body), else the module node (top-level statements and decorators on top-level defs). Lexical scope and execution semantics agree on every case, so the rule needs no exceptions and no synthetic edges. Serves FR-12, 15–19, 21, 37.

**D17 — Coverage counts are unit-explicit; a pruned directory is one excluded entry (normative).** *Added 2026-07-21; stakeholder-approved in session, resolving the step-4 task-breakdown clarification C-2.* O1's approved directory pruning means `excluded` mixes units — a pruned `venv/` is one record, a `.gitignore` pattern matching three files is three — so §5.3's coverage counts are renamed to state their units in the data itself: `entries_discovered`, `files_analyzed`, `files_skipped`, `entries_excluded`, with AC-7.1's reconciliation being `entries_discovered = files_analyzed + files_skipped + entries_excluded`, computable from those four fields alone (AC-42.2). Coverage rows carry `is_dir`, mirroring `exclusions.json`. Rejected: keeping the ambiguous names and defining the unit only in prose (relocates the ambiguity into documentation a future agent may not read); enumerating inside excluded directories to make all counts file-scoped (reverses O1's pruning and walks `venv/`/`node_modules/`/`.git/` on every run — the cost O1 exists to avoid, and worst exactly where FR-31 already declines to assert performance). EC-8's audit trail is served by `exclusions.json` naming the directory and its rule, which is the actionable fact; the file count beneath it is not. Serves FR-7, FR-42.

**D18 — Entry points are recomputed wholesale on every proceeding run, never incrementally merged (normative).** *Added 2026-07-21; stakeholder-approved in session, resolving the step-4 task-breakdown clarification C-3.* On every run that proceeds past the change gate (full or incremental), all `entry_point` nodes and their edges are deleted, all four detectors run over **all** analyzed files, and the results are re-inserted before reachability. Detectors derive their import table from the stdlib AST they are handed — it is purely syntactic — so they need no adapter state and no cross-run cache. Rationale: this dissolves three separate incremental hazards at once — the eviction rule for entry edges (whose `src` is a detector, not a caller, so D6 rule 3 never saw them), the cross-file detector dependencies (`include()` recursion, `pkg.mod:func` target resolution) that the rechecked set does not track, and the dangling-target case where a deleted view turns a one-line edit into a validation failure and a full-rebuild fallback. A vanished target now simply yields an AC-11.3/AC-10.2 unresolved diagnostic. **Cost:** one stdlib-`ast` parse pass over the analyzed set per proceeding run — ~2.0 s for Django's 908 files (FINDINGS-harness Q3) — taking the measured leaf-change path from 2.69 s to ~4.7 s against FR-30's 30 s bound. **Change gate (AC-24.1 preserved):** the zero-change fast path skips detectors too, so a run with nothing changed still re-parses nothing and leaves the index untouched. Because packaging metadata is not a Python source and therefore not in the `files` hash table, the gate additionally compares `meta.metadata_hash` — a combined sha256 over the discovered `pyproject.toml` / `setup.cfg` / `setup.py` — so a metadata-only edit is a changed input and proceeds normally. Rejected: fully incremental detectors (needs a `meta_files` table plus cross-file dependency tracking inside the highest-risk component); hybrid with a post-merge dangling sweep (two new mechanisms where this option needs none). Serves FR-8–11, FR-24, FR-30.

**D19 — `reachable` is BFS-computed on functions and *derived* on classes and modules (normative).** *Added 2026-07-21; stakeholder-approved in session, resolving the step-4 task-breakdown clarification C-4.* The BFS over `calls` edges writes `reachable` on `function` nodes — FR-18's literal scope. It cannot answer for the other two kinds: the §3.5 ladder resolves `C()` to `C.__init__` through the MRO (and, when only `object.__init__` is in the MRO, resolves to that typeshed name, which leaves the analyzed set and becomes an external target — amended 2026-07-22, D23), so class nodes receive incoming `calls` edges essentially never, and module nodes are reached only by an FR-9 entry point since reachability does not traverse `imports`. Left to the BFS, both would carry `0` almost universally — an artifact of the edge vocabulary that reads as "dead" in `/api/nodes/{id}` and the viewer's node panel. A second pass therefore **derives** them over existing `contains` edges: a `class` is reachable iff any function it contains is reachable; a `module` is reachable iff it is an entry-point target or contains a reachable function. *Amended 2026-07-23; stakeholder-approved in session, resolving two readings surfaced while implementing task 3.4.* (a) **The module clause ranges over the owning `file` node's `contains` edges.** §3.5 emits `contains` as file→defs and class→methods, so a module-body node has no outgoing `contains` edge of its own and the clause would otherwise be vacuous — leaving every module without an FR-9 guard at `0`, which is the artifact this decision exists to prevent. The functions a module contains are the ones its `file` node holds (D16's two aspects of one file), so the derivation reads: some node that `contains` this module also `contains` a reachable function. The proxy errs only in the safe direction — a reachable function is evidence that its module body ran, while a module imported solely for a side effect still reads `0`. (b) **The derivation is a union with the BFS result, not a replacement**: `reachable = BFS-reached OR contains-derived`, on `class` and `module` alike. That is what the module clause already said in its own words, it keeps the two kinds on one rule rather than two, and it prevents a class an entry point points *directly* at — a Django `X.as_view()` route (§3.7), a `pkg.mod:Class` console-script declaration — from reading `0`. **The derivation is documented as derived, not BFS-computed** — a future consumer (backlog B-6, B-18) must not read it as a graph result. Known imprecision, accepted: a class instantiated only through an inherited `__init__` reads unreachable, which under-claims rather than over-claims, consistent with the FR-19 approximation posture. `dead_code()` remains `function`-only (D16), so no report is affected either way. Serves FR-18, FR-19.

**D20 — The viewer is index-backed without exception; `/api/dead-code` recomputes (normative).** *Added 2026-07-21; stakeholder-approved in session, resolving the step-4 task-breakdown clarification C-5.* §5.2's "the dead-code report JSON verbatim" is amended to: the endpoint calls `queries.dead_code()` and returns the same shape as `deadcode.json` minus the volatile `run*` block (provenance is already at `/api/meta`). Everything in that report except `run*` is recomputable — `caveat` is the `DEADCODE_CAVEAT` constant, `no_entry_points_warning` comes from reachability. Rationale: the server then opens exactly one file, which makes AC-25.1 a testable invariant rather than a claim; the viewer needs only FR-39's index-version refusal, not a second AC-42.4 report-format refusal path and its own missing-report error state; and `query dead-code --json` and this endpoint are literally the same function, so the §5.1 promise that the two surfaces emit identical shapes cannot drift. Serves FR-19, FR-25, FR-42.

**D21 — `--debug` is accepted on every subcommand (normative).** *Added 2026-07-21; stakeholder-approved in session, resolving an ambiguity surfaced while implementing task 1.1.* §3.1 makes the traceback-behind-`--debug` behavior a property of the top-level exception trap, and that trap serves `analyze`, `query`, and `view` alike; §5.1's original synopsis listed the flag only under `analyze`, which left a `query` or `view` failure with no way to obtain a traceback short of editing the source. The flag is therefore attached to all three subcommands and to each `query` sub-subcommand, and §5.1's synopsis is amended to match. It remains a diagnostic switch only — it changes no exit code, no report, no stdout shape, and no index content, so it stays outside §5.4's volatile-field register by construction. Rejected: keeping the literal §5.1 surface (makes the two documents disagree and leaves the query path undiagnosable); a single global `--debug` before the subcommand (argparse's placement rules would force `pastapathfinder --debug query …`, which reads unlike every other flag in §5.1). Serves FR-43, FR-32.

**D22 — A node ID's `module` is built from path segments, not Python identifiers (normative).** *Added 2026-07-22; stakeholder-approved in session, resolving a conflict surfaced while implementing task 2.2.* §4.1 defines `module` by derivation — "strip `.py`, path separators → `.`" — but gave it no production, and task 1.2 implemented it as dotted identifiers. The two disagree on real input: 23 of the pinned Django benchmark's 908 files are migrations named `0001_initial.py`, whose module-body IDs the identifier reading rejects, and task 2.1 deliberately keeps such files analyzable (mypy 2.3.0 accepts any module-name string; requiring identifiers cost 25 of 908 files, measured). §4.1 therefore states the production the derivation implies: a module segment is any non-empty run of characters other than `.`, `:`, `@`, `/`, `\`, and the control characters — the delimiters the grammar itself uses, plus the path separators the derivation has already consumed. Every filename discovery can hand the adapter now yields a well-formed ID: digit-initial migrations, dashes, dots, spaces, keyword-named packages. FR-22's guarantee is untouched — the language namespace is still mandatory and still checked (AC-22.1/22.2), and empty segments are still rejected. Consequence, recorded so it is not mistaken for an oversight: a bracketed segment is no longer distinguishable by the grammar alone, so `python:pkg.mod.<lambda>` now parses as a module qualname rather than a malformed lambda; the `<lambda#N>` and `<module>` forms remain the only ones `normalize.py` produces, and that is where they are tested. Rejected: restricting a module segment to `\w+` (covers the migrations, still rejects `my-app/mod.py` — a shape common enough in real trees that the run would fail on it); skipping files whose derived module name is not identifiers (costs 2.5 % of the benchmark and contradicts task 2.1). Serves FR-22, FR-1, FR-7.

**D23 — A constructor call is named on whichever side of the analyzed-set boundary its target falls (normative).** *Added 2026-07-22; stakeholder-approved in session, resolving an ambiguity surfaced while implementing task 2.3.* §3.5's ladder resolves `C()` to the `__init__` the real MRO reaches and hands any out-of-set fullname to `externals.py`; the composition of those two clauses was ambiguous in two shapes, both settled by one principle: **the external node names the first symbol that leaves the analyzed set.** (a) Where the constructed class is *analyzed* but its MRO offers only `object.__init__`, the site resolves to that typeshed name and becomes an external target — it is not dropped. D19's parenthetical ("drops it entirely") described the prototype's behavior, not this ladder's; D19's conclusion is unaffected, since class nodes take no incoming `calls` edges either way. Dropping would make the constructor the one call shape with no trace at all, and would defeat the per-site accounting by which FR-14's never-silently-drop posture is checked. Measured cost: 155 of the pinned Django benchmark's 38,875 call sites. (b) Where the constructed class is *itself* outside the analyzed set, the ladder stops at the class rather than chasing `__init__` into its stubs: AC-36.1 asks for a leaf node for the imported symbol, and the chase collapses distinct library types onto their stubs' constructors — every builtin exception construction becoming one `builtins.BaseException.__init__`, where the benchmark distinguishes `builtins.ValueError` (472 sites) from `builtins.TypeError` (244). Since task 2.4 dedupes external nodes by qualified name, this rule is that dedup key (AC-36.5). Rejected: dropping the (a) sites (silent, untestable, and C-11's audit trail is per-site); recording (a) as an `unresolved_call` diagnostic (inaccurate — the site resolved — and it would inflate the FR-14 gap with sites that are not a recall gap). Serves FR-12, FR-14, FR-36.

**D24 — The §3.4 adapter protocol carries `prior_nodes` in and `cache_fallback` out (normative).** *Added 2026-07-24; stakeholder-approved in session, recording a spec-text drift surfaced while implementing task 4.1 and left open until now.* Task 4.1 extended the §3.4 protocol in code without an approved amendment; §3.4 kept describing the five-parameter form. The spec text is corrected to what the seam actually is, because the two additions are each load-bearing and neither is expressible any other way. **(a) `prior_nodes: Sequence[NodeRow] | None = None`.** On a warm build, mypy 2.3.0 returns cache-loaded (non-rechecked) modules with their `defs` stripped — measured, and true even under `preserve_asts=True` — so an unchanged file's structure is no longer walkable and cannot be re-derived. §3.5's `TargetIndex` already anticipated this in its own words ("this run's extractions on a full run, or the index's own rows where a file was not re-extracted"); the index rows are the only surviving source, and `analyze()` is the only channel that reaches the adapter. Without them an incremental run silently drops every `calls` edge from a changed file into an unchanged one (reproduced: `leaf_fn → base_fn` lost) — precisely the D6-rule-1 silent-edge-loss class, caught only because the equivalence test diffs against a full rebuild. The parameter is optional and `None` on a full run, where every target is freshly extracted, so an adapter with no incremental path ignores it and still conforms. **(b) `cache_fallback: bool`.** §3.5 already required the driver to convert a whole-build crash into a one-shot wipe-and-rebuild, and §3.6 already required the runner to publish that run as a fallback and attribute every file `cache_fallback` (AC-24.3, AC-35.4, AC-30.2); the flag is how the runner learns it happened. Left implicit, the two sections agreed on the behavior and named no carrier, which is how AC-30.2's "inform the user" silently becomes unreachable. FR-23 is unaffected: both additions are language-neutral — an incremental engine's stripped-tree problem and a corrupt-cache recovery are properties of caching engines, not of Python — and neither mentions mypy, so the seam still admits a second language without change. Rejected: passing the prior rows through a module-level or constructor-held handle on the adapter (hides a per-run input in adapter state, and the FR-44 determinism gate exists precisely to catch runs that depend on state the protocol does not name); re-reading unchanged files from disk inside the adapter to rebuild their structure (re-parses the whole analyzed set on every incremental run — the cost FR-30 is budgeted against, to recover rows the index already holds); inferring the fallback from `rechecked` covering every file (indistinguishable from a legitimate full run, and it would make AC-35.4's attribution a guess). Serves FR-23, FR-24, FR-30, FR-35; evidence: task 4.1's D6 equivalence test, `FINDINGS-session5.md` Part 1.

**Closed triggers and deferred alternatives (for the record).** FR-30's revision trigger fired mid-evaluation for the per-site engines and closed without amendment via engine selection (trace on FR-30, 2026-07-18). The narrowed method-dispatch over-approximation layer was evaluated (FINDINGS-namematch; FINDINGS-session5 Part 3) and deferred to the backlog under the C-6 discipline (C-11); nothing in this design builds toward it, and nothing blocks it — it would arrive as new adapter output plus an FR-39 schema-version bump at most.

## 3. Component design

Components are listed in pipeline order. "Interface" means the Python surface other components may import; anything else is private. Every component names the requirements it satisfies; §7 gives the inverse map.

### 3.1 `cli` — entry point, exit codes, progress plumbing
**Responsibility:** argument parsing for `analyze` / `query` / `view`; wiring subcommands to `runner`, `queries`, `viewer.server`; catching every exception at the top and converting to exit 2 with a one-line error (stack trace behind `--debug`, which every subcommand accepts — amended 2026-07-21, D21); computing the final exit code from the run result (0 if the run completed with `skipped == 0`; 1 if completed with skips; 2 otherwise).
**Interface:** `main(argv) -> int`. Console script `pastapathfinder = pastapathfinder.cli:main`.
**Satisfies:** FR-43 (AC-43.1–3), FR-32 (console script), part of FR-41 (owns the stderr progress channel handed to `runner`).

### 3.2 `config` — user configuration
**Responsibility:** load TOML config (stdlib `tomllib`) from `--config PATH` or, by default, `<root>/.pastapathfinder.toml` if present; validate; expose `exclude: list[str]`, `reinclude: list[str]`, `out_dir: str | None`. Invalid TOML or an invalid pattern (rejected by `pathspec`) is a run-terminating error naming the pattern (AC-4.3) — never silently ignored.
**Interface:** `load_config(root: Path, explicit: Path | None) -> Config` (frozen dataclass).
**Satisfies:** FR-4.

### 3.3 `discovery` + `exclusions` — enumeration and rule engine
**Responsibility (`exclusions`):** build the layered `RuleSet`: the common convention set (normative: `.git/`), the Python convention set (normative v1 list, settling OQ-3 per its "settled during design" assumption: `venv/`, `.venv/`, `env/`, `.env/`, `virtualenv/`, `build/`, `dist/`, `__pycache__/`, `.tox/`, `.nox/`, `.eggs/`, `*.egg-info/`, `.mypy_cache/`, `.pytest_cache/`, `node_modules/`), then every `.gitignore` in the tree (gitwildmatch semantics, patterns relative to the `.gitignore`'s directory; an unreadable or unparseable file/line → warning naming file and line + diagnostics entry, continue, AC-3.2), then user `exclude`, with user `reinclude` as the highest-precedence negation. Every match records `(pattern, source)` where source ∈ `default:common | default:python | gitignore:<relpath> | user:exclude`. Unmatched rules are not errors (AC-2.2).
**Responsibility (`discovery`):** walk from root without following directory symlinks. **Directory-rule pruning (normative; flagged §8-O1):** a directory matched by a directory-pattern rule is not descended into; it is recorded as one exclusion-report entry, and its contents are neither enumerated nor counted. File-level matches are recorded per file. Enumerated files are classified: `.py` → candidate; extensionless → shebang probe (read ≤ 256 bytes of line 1; must start `#!` and contain `python`) (AC-1.4); binary/non-matching → not an input; unreadable probe → diagnostics entry, continue (AC-1.5). File symlinks: resolve; real path outside root → skip + diagnostics (AC-1.6); inside root → analyze the real path once (dedupe by realpath). Not following directory symlinks makes link cycles unreachable (AC-1.6 termination). Root missing/unreadable → run-terminating error naming path and reason (AC-1.3).
**Interface:** `discover(root, ruleset) -> DiscoveryResult` with `candidates: list[Path]`, `excluded: list[ExclusionRecord]`, `probe_diagnostics: list[Diag]`.
**Satisfies:** FR-1 (all ACs), FR-2, FR-3, FR-5's data, EC-10, EC-11.

### 3.4 `adapters.base` — the language-adapter boundary
**Responsibility:** the language-neutral seam (FR-23). Normative protocol:

```python
class LanguageAdapter(Protocol):
    language: str                       # namespace token, e.g. "python"
    def recognizes(self, path: Path, first_line: bytes | None) -> bool: ...
    def analyze(self, root: Path, files: list[SourceFile],
                cache_dir: Path, changed: set[Path] | None,
                progress: ProgressSink,
                prior_nodes: Sequence[NodeRow] | None = None,   # amended 2026-07-24, D24
                ) -> AdapterResult: ...
```

`AdapterResult` = `fragments: list[GraphFragment]` (per source file: its nodes, its outgoing edges, its file record with content hash), `skipped: list[SkipRecord]`, `diagnostics: list[Diag]`, `rechecked: set[Path]` (the re-extraction set on incremental runs), `engine_meta: dict`, `cache_fallback: bool` (the adapter recovered from an unusable engine cache by wiping and rebuilding cold — amended 2026-07-24, D24). `GraphFragment` fields are exactly the §4 row shapes. No component outside `adapters.python` may import anything from `mypy.*` (AC-23.1; enforced by a unit test that greps imports).
**Satisfies:** FR-23 structure; FR-21/22 by construction of `GraphFragment`.

### 3.5 `adapters.python` — the mypy driver and extractor
**Responsibility:** the only component touching mypy. Submodules:
- `mypy_driver.py` — builds `Options` (normative settings: `incremental=True`, `cache_dir=<out>/mypy_cache`, `export_types=True`, `preserve_asts=True`, `check_untyped_defs=True`, `no_site_packages=True`, per-module `ignore_missing_imports=True` for `*`, `follow_imports="normal"`, error display suppressed) and calls `mypy.build.build(sources, options)`. `no_site_packages` plus the tool's own isolated environment guarantee target-environment independence (AC-13.1) and cross-machine determinism; type errors in target code are expected and never stop the build. Enumerated mypy internals touched (the D1a upgrade checklist): `mypy.build.build`, `BuildSource`, `Options`, `BuildResult.graph`/`.types`, `State.tree`, the build manager's rechecked-modules report, node types `MypyFile/CallExpr/NameExpr/MemberExpr/FuncDef/ClassDef/Decorator/LambdaExpr`, `SymbolNode.fullname`, `TypeInfo.get`/`.mro`, `CallableType.definition`. A whole-build crash (as opposed to a per-file failure) is caught and converted to the AC-24.3 full-rebuild fallback once (cache dir wiped); if the full rebuild also crashes, the run fails with the engine error surfaced.
- `extract.py` — walks each analyzed module's typed AST and emits: the file node; the module-body node (`kind='module'` — amended 2026-07-21, D16 — id suffix `.<module>`, `attrs.python_role="module_body"`; module-level call sites attach here, so module flow is sliceable and FR-9 entry points have a target); function/class/method nodes with spans (span underivable → path only + diagnostics entry, AC-37.3); `contains` edges (file→defs, class→methods); `imports` edges (file→file, from mypy's dependency info, restricted to analyzed files — these drive FR-35 attribution); and `calls` edges — whose `src` is normatively the **nearest enclosing executed scope** of the call site (enclosing function, else enclosing class body for class-level statements and method decorators, else the module node for top-level statements and decorators on top-level defs; amended 2026-07-21, D16) — per the resolution ladder: bound `NameExpr/MemberExpr .node` fullname → single target; instance member via the expression-type map → `TypeInfo.get(name)` through the real MRO; constructor `C()` → `__init__` through MRO, save where the class itself lies outside the analyzed set, which is named by the class (amended 2026-07-22, D23); multiple candidates (overloads, union receivers) → one edge each, all `is_ambiguous=1` (AC-40.1), single unambiguous resolutions unflagged (AC-40.2); typeshed-stub target or fullname outside the analyzed set → hand to `externals.py`; nothing at all → per-site unresolved diagnostic with file/line/col/callee text (AC-14.2). Per-file parse failure (syntax error, encoding error) → `SkipRecord` with reason class (`parse_error | encoding_error | engine_error`), continue (FR-6, EC-1/2/12); all-files-fail still produces the index and reports with zero analyzed files and an explicit statement (AC-6.2).
- `externals.py` — D15's two sources; emits external leaf nodes (`is_external=1`, no span, no outgoing edges — enforced) deduplicated by qualified name (AC-36.5); maintains the per-module import table, built with **stdlib `ast`** over the re-extracted set and never from the engine's trees (amended 2026-07-22, stakeholder-approved in session: requirements §6 item 10 exempts this mechanism from the orchestration-only exclusion *as a bounded syntactic mechanism built on the standard library's parser*, which is also the artifact whose precision D15 cites — `FINDINGS-namematch.md` §1 built its indexes with stdlib `ast` only. Cost: one parse pass, ~2.0 s over the pinned benchmark's 908 files against FR-29's 600 s bound, and only files carrying unresolved sites need one). This table has a single consumer, `externals.py` itself: the Django URLconf detector derives its own from the stdlib AST it is handed (amended 2026-07-21, D18).
- `normalize.py` — node-ID construction (§4.1 grammar), module-name derivation from relpath, lambda naming (`<lambda#N>` per-scope counter), `@line` collision suffixing.
**Satisfies:** FR-12–14 (as clarified by C-11), FR-36, FR-37, FR-40, FR-6, FR-13, FR-21/22 output discipline; measured against FR-29/30.

### 3.6 `incremental` — hash gate, cache orchestration, evict-and-merge
**Responsibility:** before analysis, hash (sha256) every candidate and compare to the index's `files` table, and compare `meta.metadata_hash` against the discovered packaging-metadata files (D18). Zero changes on both → skip the engine **and the detectors** entirely (AC-24.1: nothing re-parsed, index untouched), write reports (re-analysis report: "no files re-processed"; AC-35.2, AC-24.1), finish. Changes → pass `changed` to the adapter; on return, apply the three normative D6 rules: eviction set = `rechecked ∪ removed`; for each file in the set, delete its nodes and its `src_file` edges, then insert the new fragments (replace-not-union); after the merge, delete external nodes with zero incoming edges (prevents stale-external leakage — proven necessary by the session-5 eviction variant); then recompute entry points wholesale (D18 — `entry_point` nodes are outside this discipline); then recompute reachability. Removed files (in index, absent on disk) → evicted and listed (AC-35.3). Cache corruption (mypy cache unreadable, index/files-table inconsistency, fragment validation failure on merge) → wipe caches, run full analysis, attribute every file `cache_fallback` in the re-analysis report (AC-35.4, AC-24.3), and inform the user a longer full run is underway (AC-30.2). FR-35 attribution: hash-changed → `content_changed`; otherwise rechecked → `dependent`; fallback path → `cache_fallback`.
**Interface:** `plan_run(index, candidates) -> RunPlan`; `merge(index, result, plan) -> MergeReport`.
**Satisfies:** FR-24 (all ACs; equivalence evidence FINDINGS-session5 Part 1), FR-30, FR-35, EC-7, EC-13 pipeline half.

### 3.7 `detectors` — entry-point detection
**Responsibility:** `registry.py` holds the ordered list `[MainBlockDetector, ConsoleScriptsDetector, FlaskFastapiRouteDetector, DjangoUrlconfDetector]`; adding a detector = one new module + one list entry (AC-8.1). Two shapes: per-module detectors receive `(module_path, stdlib_ast_tree, import_table)` — the import table derived from that same stdlib AST, not from the adapter (D18) — ; project-level detectors receive the metadata file set. **Detectors are recomputed wholesale on every proceeding run over all analyzed files (D18); they take no part in §3.6's evict-and-merge.** Every `detect()` call is wrapped: an exception becomes a diagnostics entry naming the detector and file, and iteration continues (AC-8.2). Normative per-detector rules:
- **main_block (FR-9):** an `If` whose test compares `__name__` with the literal `"__main__"` (either operand order, `==`) → entry node targeting the module-body node. Files that failed stdlib parse emit nothing (AC-9.2; the file is already a skip).
- **console_scripts (FR-10):** parse `pyproject.toml` (`[project.scripts]`, `[project.entry-points.console_scripts]`), `setup.cfg` (`[options.entry_points]`), and `setup.py` **statically only** — an `ast` walk extracting a literal `entry_points` argument; anything computed is recorded unresolved in diagnostics (FR-13 forbids executing `setup.py`). Resolve `pkg.mod:func` against index node IDs; unresolvable → diagnostics unresolved (AC-10.2), never dropped.
- **flask_fastapi (FR-11):** a decorator of shape `<name>.<verb>(...)` or `<name>.route(...)` where verb ∈ {get, post, put, delete, patch, head, options, websocket} → entry node targeting the decorated function; `attrs` records the receiver name, verb, and first literal path argument when present. Registration not expressible as a decorator on a def (dynamic `add_api_route`, loops) → diagnostics unresolved (AC-11.3); no fabrication.
- **django_urlconf (FR-11):** any module assigning `urlpatterns` to a list/`+`-concatenation of `path()/re_path()/url()` calls; the view argument resolved through the module import table: `Name/Attribute` → function node; `X.as_view()` → the class node; `include("mod")` → recurse into the referenced module's patterns; comprehensions/loops/computed patterns → diagnostics unresolved (AC-11.3).
Emitted entry nodes: `kind='entry_point'`, id per §4.1, one `calls` edge to the target node — which makes FR-18 reachability a single-edge-kind BFS.
**Satisfies:** FR-8–11, EC-9 data side.

### 3.8 `schema` + `index` — the store
**Responsibility (`schema.py`):** the dataclasses mirroring §4, `SCHEMA_VERSION = 1`, the node-ID grammar with its validating regex, the DDL strings, the `DEADCODE_CAVEAT` constant, and `validate_fragment()` — rejects non-namespaced IDs (AC-22.2), unknown kinds, and edges referencing IDs absent from fragment ∪ index (AC-23.2), with errors naming the offending row.
**Responsibility (`index.py`):** open/create the SQLite file. On open, read `meta.schema_version` and refuse any value ≠ `SCHEMA_VERSION`, naming found and supported versions (AC-39.2); a missing or unreadable version is treated as incompatible (AC-39.3). Full runs write to `index.sqlite.tmp` and atomically rename; incremental merges run inside one transaction. All writers pass through the canonical-sort layer (D12). Convenience: `content_hashes() -> dict[path, sha256]` (consumed by `incremental` and `postrun`).
**Satisfies:** FR-20–22 (storage side), FR-39, FR-44 write discipline, EC-13.

### 3.9 `queries` — slices, reachability, dead code
**Responsibility:** `slice(index, node_id, direction, max_nodes=200)` — recursive CTE over `calls` edges; forward follows `src→dst`, backward `dst→src`; BFS order under a node budget; returns `SliceResult(nodes, edges, truncated: bool, frontier: list[node_id])`. Unknown ID → `UnknownNodeError` naming it (AC-16.2). Non-sliceable kind (`file`) → `NotSliceableError` naming the kind (AC-17.2); `entry_point`, `function`, `class`, `module` are sliceable (D16). An empty slice is a valid result, presented as such (AC-15.2). **The 200-node default is a provisional design parameter, not requirements-derived** — flagged §8-O2; it exists to satisfy AC-28.2's bound-somehow-visibly mandate and is tuned when OQ-4 resolves. `reachability(index)` — BFS from all `entry_point` nodes over `calls` edges; writes `reachable` 0/1 on `function` nodes, then derives it on `class` and `module` nodes over `contains` edges (D19: a class is reachable iff any function it contains is reachable; a module iff it is an entry target or contains a reachable function — amended 2026-07-23, D19: the derivation is a *union* with the BFS result, and a module's clause reads the `contains` edges of the `file` node that holds it); zero entry points → still computed, with a warning flag returned for the run output (AC-18.2). `dead_code(index)` — `kind='function'` nodes with `reachable=0` (module nodes are excluded by construction, D16), grouped by file, paired with the `DEADCODE_CAVEAT` constant every renderer must include (AC-19.2, AC-19.3). External leaf nodes are excluded as well (recorded 2026-07-23, task 3.4): they carry the `function` kind, so the BFS writes `reachable` on them and `/api/nodes/{id}` reports it, but FR-36 leaves their internals deliberately unanalyzed — "unreachable" claims nothing about a symbol this tool never looked inside — and AC-37.2 leaves them without the `file_path` this report groups by.
**Satisfies:** FR-15–19, EC-6 query side.

### 3.10 `reports`, `postrun`, `progress`, `runner`, and the FR-44 comparator
- **`reports.py`:** writers for the six JSON artifacts (§5.3) plus their stdout renderings; every writer stamps `format_version`; a write failure terminates the run with the failing path (AC-7.3, AC-42.3). First-run detection (no prior index) → the exclusion-report path is printed prominently (AC-5.2); an exclusion-free run still writes the report stating so (AC-5.3). The coverage reconciliation `entries_discovered = files_analyzed + files_skipped + entries_excluded` (D17) is asserted before writing; a mismatch is a pipeline bug and fails the run loudly (AC-7.1).
- **`postrun.py`:** FR-38 — mtime/size pre-check over the enumerated files, hash-confirm any difference, emit the change-warning report naming changed/removed files and recommending re-analysis (AC-38.1); deleted files listed as removed and unreadable-during-check files reported as per-file check failures (AC-38.3); no differences → empty lists and no warning line (AC-38.2). The report's fixed `note` field carries the best-effort/no-guarantee wording.
- **`progress.py`:** stderr sink; per-file phases emit `processed/total` at ≤ 5 s intervals (AC-41.1); the single-call mypy build phase gets a heartbeat thread emitting `analyzing (engine build) … {elapsed}s` at ≤ 5 s (AC-41.2's activity indication — normative, because the build is opaque).
- **`runner.py`:** orchestrates a run end-to-end in the §1 order and assembles the run summary the CLI turns into an exit code.
- **`tests/regression/compare.py`** (dev utility, not a shipped CLI command): the index/report comparator implementing the FR-44 amendment — strip the §5.4 volatile fields, then classify remaining diffs: none → equal; differences consisting solely of the presence/absence of `calls` edges (plus external nodes referenced only by them) affecting ≤ 0.01 % of call edges → **in-variance-class**, reported as a warning, never silently passed; anything else → defect, test failure. Threshold basis: 0.003 % measured at pandas scale (FINDINGS-session5 Part 2).
**Satisfies:** FR-5, 7, 35, 38, 41, 42, 44 (verification side), EC-8, EC-14.

### 3.11 `viewer` — server and frontend
- **`server.py`:** Flask app; opens the index read-only via `index.py` — and opens no other file (D20) — inheriting FR-39 refusal; endpoints per §5.2; index missing/unreadable/incompatible → every endpoint returns the structured error, and the frontend shows it full-screen with the re-run instruction (AC-25.2, AC-20.2, EC-13 viewer half). Binds `127.0.0.1:<port>` (default 8517), debug off, no external requests (FR-33). Imports nothing from `adapters` or `mypy` (AC-25.1; same import-grep test as 3.4).
- **`static/`:** `index.html`, `app.js`, `style.css`, `vendor/cytoscape.min.js` (plus its dagre layout plugin), all shipped as package data — no CDN, no npm. Views: **entry-point list** (all entry nodes, selectable, AC-26.1; zero → explicit empty-state text offering slice-by-any-node with a node search box backed by `/api/nodes?search=`, AC-26.2); **trace view** (forward/backward toggle; Cytoscape rendering of the slice; selecting a displayed edge follows it to its target, AC-27.1; truncation banner with a frontier-expand action whenever `truncated`, AC-28.1/28.2); **node panel** (name, kind, `file_path:start–end` for non-external nodes, "external — not analyzed" for external ones, AC-27.3); query errors surfaced verbatim in-view (AC-27.2); a selected node vanishing after re-analysis → the API's unknown-ID error routes the user back to the entry list (EC-15).
**Satisfies:** FR-25–28, EC-15.

## 4. Data models

### 4.1 Node-ID grammar (normative)

```
node_id   := language ":" local
language  := "python"                     (v1; FR-22)
local     := "file:" relpath              (file nodes; POSIX-style, root-relative)
           | qualname [ "@" start_line ]  (code nodes; suffix only on collision)
           | "entry:" detector ":" qualname "@" line
qualname  := module { "." segment }
module    := mod_seg { "." mod_seg }       (amended 2026-07-22, D22)
mod_seg   := any non-empty run of characters other than "." ":" "@" "/" "\"
             and the ASCII control characters
segment   := identifier | "<module>" | "<lambda#" N ">"
detector  := "main_block" | "console_script" | "route_flask_fastapi" | "route_django"
```

`module` derives from the relpath: strip `.py`, path separators → `.`, a trailing `.__init__` dropped. Its segments are therefore path segments, not Python identifiers — `0001_initial.py`, `my-app/`, and a directory named after a keyword all derive legal module names (amended 2026-07-22, D22). External nodes use the plain `python:<qualified_name>` form with `is_external=1` (collision with analyzed nodes is impossible by definition; the qualified name is the AC-36.5 dedup key). IDs are URL-encoded where they appear in API paths.

### 4.2 SQLite DDL (normative; `schema.py` is the single source)

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- required keys: schema_version ("1"), tool_version, engine ("mypy"),
-- engine_version ("2.3.0"), root_path, created_at*, run_id*   (* = volatile)
-- metadata_hash: combined sha256 over discovered pyproject.toml /
--   setup.cfg / setup.py (added 2026-07-21, D18; part of the change gate)

CREATE TABLE files (
  path TEXT PRIMARY KEY,            -- root-relative POSIX path
  content_hash TEXT NOT NULL,       -- sha256 hex of file bytes as read
  status TEXT NOT NULL CHECK (status IN ('analyzed','skipped')),
  skip_reason TEXT                  -- NULL unless skipped: parse_error |
);                                  --   encoding_error | engine_error

CREATE TABLE nodes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('file','module','function','class','entry_point')),
                                    -- 'module' added 2026-07-21 (D16)
  name TEXT NOT NULL,
  language TEXT NOT NULL,
  file_path TEXT,                   -- NULL for external (AC-37.2)
  start_line INTEGER, end_line INTEGER,   -- NULL when span unknown (AC-37.3)
  is_external INTEGER NOT NULL DEFAULT 0,
  reachable INTEGER,                -- 0/1 on function (BFS) and on class|module
                                    --   (derived from contains, D19); else NULL
  attrs TEXT NOT NULL DEFAULT '{}'  -- JSON; all language-specific detail
);

CREATE TABLE edges (
  src TEXT NOT NULL REFERENCES nodes(id),
  dst TEXT NOT NULL REFERENCES nodes(id),
  kind TEXT NOT NULL CHECK (kind IN ('calls','contains','imports')),
  src_file TEXT,                    -- caller file, for eviction (D6 rule 3)
  is_ambiguous INTEGER NOT NULL DEFAULT 0,
  attrs TEXT NOT NULL DEFAULT '{}', -- JSON; e.g. call_sites: [[line,col],...]
  PRIMARY KEY (src, dst, kind)
);
CREATE INDEX ix_edges_dst ON edges(kind, dst);
CREATE INDEX ix_edges_srcfile ON edges(src_file);
```

Multiple call sites for the same `(src, dst)` collapse into one edge; the sites live sorted in `attrs.call_sites`. Reserved `attrs` keys in v1 — nodes: `python_role` (`"module_body"`), `decorators` (list of dotted names), `detector`, `route` (`{receiver, verb, path}` or `{pattern}`), `script_name`; edges: `call_sites`. Anything a future language needs rides the same column (FR-21); Python-specific concepts never become kinds (AC-21.1), and generic queries never depend on `attrs` contents (AC-21.2, AC-40.3).

### 4.3 Adapter fragment (in-memory)

`GraphFragment(file: FileRecord, nodes: list[NodeRow], edges: list[EdgeRow])` — row shapes mirror the DDL columns exactly. `SkipRecord(path, reason, detail)`. `Diag(kind, path, line, col, message, extra)` with `kind` ∈ `unresolved_call | detector_error | probe_failure | symlink_skip | span_missing | gitignore_problem | change_check_failure | unresolved_entry_declaration` (the C-10 diagnostic classes; `unresolved_call.extra.callee` carries the callee source text — the audit trail for the C-11-documented gap).

## 5. Interface contracts

### 5.1 CLI

```
pastapathfinder analyze <root> [--out DIR] [--config FILE] [--full] [--debug]
pastapathfinder query entry-points [--out DIR] [--json] [--debug]
pastapathfinder query slice --from NODE_ID --direction {forward,backward}
                    [--max-nodes N] [--out DIR] [--json] [--debug]
pastapathfinder query node NODE_ID [--out DIR] [--json] [--debug]
pastapathfinder query dead-code [--out DIR] [--json] [--debug]
pastapathfinder view [--out DIR] [--port PORT] [--debug]
```

`--out` default: `$XDG_DATA_HOME/pastapathfinder/<basename>-<sha256(abspath)[:12]>/` (fallback `~/.local/share/…`) — deliberately outside the target tree, so the tool never writes into the codebase (§6 item 17's spirit) and never discovers its own output; also unprivileged by construction (FR-34, AC-34.2's permissions error names the path on failure). `analyze` is incremental automatically when a compatible index exists; `--full` forces the full path. `--json` emits the same structured shapes as the HTTP API on stdout — the agent-facing mechanical surface. `--debug` is accepted on **every** subcommand (amended 2026-07-21, D21): it selects the traceback form of §3.1's top-level exception trap, which serves all three. Exit codes 0/1/2 per D10, documented in `docs/exit-codes.md`.

### 5.2 Viewer HTTP API (localhost only)

```
GET /api/meta         → {schema_version, tool_version, root_path, created_at,
                         counts: {files, nodes, edges, entry_points}}
GET /api/entry-points → {entry_points: [{id, name, detector, target_id,
                         file_path, start_line}]}                (sorted by id)
GET /api/nodes/{id}   → {id, kind, name, file_path, start_line, end_line,
                         is_external, reachable, attrs}          (404 unknown)
GET /api/nodes?search=SUBSTR&limit=50 → {nodes: [...]}           (id/name match)
GET /api/slice?from=ID&direction=forward|backward&max_nodes=N
                      → {nodes: [...], edges: [{src, dst, is_ambiguous}],
                         truncated, frontier: [id]}
GET /api/dead-code    → deadcode.json's shape minus run*, recomputed from the
                         index via queries.dead_code() (incl. caveat) — D20
Errors                → HTTP 4xx/5xx + {error: {code, message}}
  codes: unknown_node | not_sliceable | index_missing | index_incompatible
```

### 5.3 Report files (all under `<out>/reports/`, overwritten each run; every document carries `format_version: 1`)

- `coverage.json` — `{format_version, run*, counts: {entries_discovered, files_analyzed, files_skipped, entries_excluded}, files: [{path, status, is_dir, reason?, rule?}]}` (field names and `is_dir` amended 2026-07-21, D17); `entries_discovered = files_analyzed + files_skipped + entries_excluded` is AC-7.1's reconciliation and satisfies AC-42.2 from those four fields alone.
- `exclusions.json` — `{format_version, run*, exclusions: [{path, is_dir, pattern, source}], none_excluded: bool}` (AC-5.3's explicit empty statement).
- `reanalysis.json` — `{format_version, run*, mode: full|incremental|skipped_no_changes|fallback, reprocessed: [{path, reason: content_changed|dependent|cache_fallback}], removed: [path]}`.
- `change_warning.json` — `{format_version, run*, note: "<best-effort wording>", changed: [path], removed: [path], check_failures: [{path, error}]}`.
- `diagnostics.json` — `{format_version, run*, diagnostics: [Diag…]}` — present and empty-listed on clean runs (C-10 convention).
- `deadcode.json` — `{format_version, run*, caveat: DEADCODE_CAVEAT, no_entry_points_warning: bool, unreachable: [{file, functions: [{id, name, start_line}]}]}`.

`run*` = `{run_id, started_at, finished_at, duration_seconds}` — volatile. Schemas are published verbatim to `docs/report-formats.md` (the FR-42/AC-42.4 documentation debt CLAUDE.md records).

### 5.4 Volatile-field register (FR-44; single authoritative list, mirrored in `docs/report-formats.md`)

Index: `meta.created_at`, `meta.run_id`. Reports: the `run*` block. Nothing else. Any other cross-run difference is either the FR-44-amendment variance class (call-edge presence within the comparator's 0.01 % threshold — reported, never ignored) or a defect.

### 5.5 Config file (`.pastapathfinder.toml`)

```toml
[exclude]
add = ["generated/", "*.pb2.py"]       # gitwildmatch patterns
reinclude = ["vendor/keep_this/"]      # negates any default/gitignore match
[output]
dir = "/absolute/path"                 # optional; overrides the XDG default
```

## 6. Repository structure

Per CLAUDE.md's boundary rule (specs/ govern building; docs/ ship with the product). The `prototypes/` placement is the stakeholder's open call (§8-O4).

```
pyproject.toml            # deps: mypy==2.3.0, pathspec, flask; requires-python >=3.12
CLAUDE.md
specs/    requirements.md  backlog.md  design.md  tasks.md (arrives at step 4)
docs/     report-formats.md  exit-codes.md  wsl.md  configuration.md  install.md
          # wsl.md carries FR-31's Linux-filesystem performance condition
src/pastapathfinder/
    __init__.py  __main__.py  cli.py  config.py  discovery.py  exclusions.py
    progress.py  runner.py  incremental.py  queries.py  reports.py  postrun.py
    schema.py  index.py
    adapters/  base.py
               python/  __init__.py  mypy_driver.py  extract.py  externals.py  normalize.py
    detectors/ base.py  registry.py  main_block.py  console_scripts.py
               flask_fastapi.py  django_urlconf.py
    viewer/    server.py
               static/  index.html  app.js  style.css  vendor/cytoscape.min.js
tests/
    unit/            # per-component; includes the AC-23.1/AC-25.1 import-grep tests
    fixtures/        # micro codebases per AC (syntax-error file, symlink layouts,
                     #   route fixtures reprising the prototype designs, …)
    regression/      # compare.py (FR-44 comparator), benchmark pins + README
prototypes/engine-eval/…   # the FINDINGS files (home pending §8-O4)
```

Coding conventions proposed to fill CLAUDE.md's TBD block on approval: Python ≥ 3.12 (developed on 3.13); `ruff` for lint + format; `pytest`, invoked as `pytest`; build/run via `pip install -e .` then `pastapathfinder …`; commit subjects in imperative mood with FR/AC references in the body.

## 7. Traceability check

Every FR maps to at least one component; every §3 component appears in the map. No orphans in either direction.

| FR | Component(s) | | FR | Component(s) |
|---|---|---|---|---|
| FR-1 | discovery (3.3) | | FR-23 | adapters.base (3.4), index validation (3.8) |
| FR-2 | exclusions (3.3) | | FR-24 | incremental (3.6), index (3.8) |
| FR-3 | exclusions (3.3) | | FR-25 | viewer.server (3.11) |
| FR-4 | config (3.2), exclusions (3.3) | | FR-26 | viewer frontend (3.11), queries (3.9) |
| FR-5 | reports (3.10), exclusions data (3.3) | | FR-27 | viewer frontend (3.11), queries (3.9) |
| FR-6 | adapters.python (3.5), runner (3.10) | | FR-28 | queries bound (3.9), viewer (3.11) |
| FR-7 | reports (3.10), discovery + adapter statuses | | FR-29 | whole pipeline; evidence FINDINGS-mypy Q2 |
| FR-8 | detectors.registry (3.7) | | FR-30 | incremental (3.6); evidence FINDINGS-mypy Q3, session5 P1 |
| FR-9 | detectors.main_block (3.7) | | FR-31 | packaging + docs/wsl.md (§6) |
| FR-10 | detectors.console_scripts (3.7) | | FR-32 | pyproject + cli (3.1) |
| FR-11 | detectors.flask_fastapi, django_urlconf (3.7) | | FR-33 | viewer bind + vendored assets (3.11, D8) |
| FR-12 | adapters.python.extract (3.5) | | FR-34 | out-dir under $HOME (5.1); no elevation anywhere |
| FR-13 | mypy_driver options (3.5), detector static parsing (3.7) | | FR-35 | incremental (3.6), reports (3.10) |
| FR-14 | extract resolution ladder + C-11 diagnostics (3.5) | | FR-36 | externals (3.5, D15) |
| FR-15 | queries.slice (3.9) | | FR-37 | extract spans (3.5), schema (4.2) |
| FR-16 | queries.slice (3.9) | | FR-38 | postrun (3.10) |
| FR-17 | queries.slice kinds (3.9) | | FR-39 | index versioning (3.8) |
| FR-18 | queries.reachability (3.9) | | FR-40 | extract ambiguity flag (3.5), schema (4.2) |
| FR-19 | queries.dead_code (3.9), reports (3.10), viewer (3.11) | | FR-41 | progress (3.10), cli (3.1) |
| FR-20 | index (3.8) | | FR-42 | reports (3.10), schemas (5.3) |
| FR-21 | schema (3.8, 4.2) | | FR-43 | cli (3.1) |
| FR-22 | schema grammar (4.1), validation (3.8) | | FR-44 | index canonical writes (3.8), compare.py (3.10), register (5.4) |

Component inverse check: 3.1–3.11 each appear above; `tests/regression/compare.py` serves FR-44; `tests/fixtures` serve the acceptance criteria generally.

## 8. Risks and open items

- **R1 — mypy's semi-public API (highest risk).** The pipeline's core rests on interfaces with no stability guarantee. Mitigation: exact pin; D1a revalidation procedure; §3.5's enumerated-internals checklist localizing breakage. Working assumption: mypy 2.3.0 remains installable and sufficient for v1's lifetime; upgrades are elective, never forced.
- **R2 — Viewer mortality** (the project's named highest-mortality component). Mitigations: pipeline-first sequencing; no-build frontend; Flask-minimal server; slice-bounded rendering; D8's provisional status keeping the graph library swappable. Working assumption: the §5.2 API is stable even while the frontend iterates.
- **R3 — Recall ceiling.** mypy resolves ~62.9 % of Django call sites; the C-11-documented gap (bare-name value-flow; un-narrowed attribute dispatch) means flagship traces can go cold exactly where legacy bugs live. Mitigations: D15 closes the external share of the gap; per-site unresolved diagnostics make the remainder auditable; the §6-item-1 manual workaround is documented; the deferred dispatch layer is preserved in the backlog with all its evidence. Working assumption: accepted per C-11's portfolio-tiebreaker resolution.
- **O1 — Directory-prune reading of discovery (stakeholder confirmation requested).** §3.3 prunes traversal at directory-rule matches: an excluded directory is one exclusion-report entry; its contents are not enumerated, hashed, or counted in AC-7.1's arithmetic, and FR-38's change check covers enumerated files only. This is the performance-consistent reading of FR-1/FR-2/AC-2.1 (hashing an entire `venv/` tree for the change check would defeat EC-3's purpose), but FR-7's "every discovered file" admits a per-file reading. Approving this design approves the pruning reading; rejecting it changes §3.3 and the coverage-report shape. *Counting units settled 2026-07-21 (D17): a pruned directory is one excluded entry, and §5.3's coverage fields name their units explicitly.*
- **O2 — Slice-bound default (200 nodes) is provisional and not requirements-derived.** It exists to implement AC-28.2's bound-somehow-visibly mandate; it is tuned when OQ-4 resolves against real-graph experience at the viewer milestone. OQ-4 stays open in requirements §8 until then (two-sided update at that point).
- **O3 — OQ-2 stays formally open.** D7/D7a/D8 record the provisional stack (Flask + no-build SPA + Cytoscape.js); confirmation and the §8 two-sided update happen at the viewer milestone, per OQ-2's own sequencing.
- **O4 — Repository home for the FINDINGS files** (and this document's citations of them): stakeholder decision pending; the standing proposal is `prototypes/` plus a one-line addition to CLAUDE.md's boundary rule.
- **O5 — Benchmark pins.** Requirements §4.8 requires exact versions/commits pinned at design stage: Django core and pandas are pinned at the commits recorded in FINDINGS-harness.md (session 1) and FINDINGS-session5.md (Part 2) respectively; `tests/regression/README` copies the hashes verbatim at implementation, and the regression suite fetches by hash.
- **O6 — FR-31 WSL verification.** All measurements to date are native-Linux; the WSL2 tested-support claim needs one verification pass of the US-1..US-5 workflows before release. Working assumption: no WSL-specific code is required (filesystem-semantics-dependent behavior follows the mounted filesystem, per FR-31's own text); this is a step-4 test-plan item, not a design item.
- **O7 — §4.1's `entry:` ID is not unique per registration site** *(recorded 2026-07-23, reproduced while implementing task 3.4)*. An entry node's ID is `(detector, qualname, line)`, which collides when one source line declares two entries for the same target. `urlpatterns = [path("a", views.foo), path("b", views.foo)]` yields two `python:entry:route_django:app.views.foo@3` nodes differing only in `attrs.route`; the store rejects them as conflicting rows for one primary key (D12's canonical layer), so the run fails with exit 2 on legal Django input. `console_scripts` carries a narrower variant: `_declaration_line()` falls back to line 1 when its regex misses, and two commands sharing one target then collide the same way. Task 3.4's scope was `queries.py`, so no workaround was built and none should be improvised — the run fails loudly rather than storing a fabricated or arbitrarily-chosen entry. Candidate resolutions, all design-level: add a per-site ordinal to the ID; add the call site's column beside its line; or collapse same-target registrations into one entry node carrying several routes, which changes what `attrs.route` means (§4.2 reserves it as a single `{pattern}` / `{receiver, verb, path}`). Whichever is chosen touches §4.1's grammar and §3.7's detectors, not the query layer.
