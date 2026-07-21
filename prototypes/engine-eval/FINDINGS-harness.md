# FINDINGS — Session 1: Measurement Harness + PyCG Correctness Yardstick

Date: 2026-07-16
Scope: `prototypes/engine-eval/`. Throwaway prototype. No product code touched.

**Headline:** The harness works end-to-end (Q1 ✅). PyCG scores **97.6% precision / 93.2% recall**
on its own 119-case micro-benchmark under Python 3.9 (Q2 ✅). Both macro-benchmark prerequisites
are met (Q3 ✅). But PyCG is **structurally unable to analyze modern Python** — see the pincer in
§3 Q2b. That is a yardstick caveat, not a blocker for sessions 2–3.

---

## 1. What was built

All under `prototypes/engine-eval/harness/`.

**Call-site enumerator (`callsites.py`).** Walks a root, enumerates `.py` files, `ast.parse`s each
from file *text* only, and emits `CallSite(file, line, col, callee_expr, enclosing)`. FR-13 holds by
construction: the module never imports, executes, or `exec`s analyzed code — `ast.parse` on bytes is
the only thing that touches it. Parse failures are collected as `(relpath, error)` and never fatal.
File and directory iteration is sorted so discovery order is deterministic (FR-44). Noise dirs
(`.git`, `__pycache__`, `.tox`, venvs, `node_modules`) are pruned. `enclosing` is computed by a scope
stack over `FunctionDef`/`AsyncFunctionDef`/`ClassDef`, giving the caller qualified name the scorer
needs. *Cut corner:* `callee_expr` is source text (`ast.unparse`), not a resolved symbol — it is a
comparison key, not semantics.

**Resolver interface (`resolver.py`).** `resolve(project_root, call_sites) -> ResolutionResult`,
carrying `edges: set[(caller_qname, callee_qname)]`, `per_site: {(file,line,col): [candidates]}`
(empty list = unresolved), `errors`, and free-form `extra`. This is the comparison seam sessions 2–3
implement. Whole-program engines build their graph once and answer lookups from it, which is exactly
what the PyCG adapter does.

**Scorer (`scorer.py`).** Loads PyCG-format ground truth, compares edge sets, reports
precision/recall/F1 plus FP/FN examples. Two deliberate decisions: undefined precision/recall are
`None`, never `0` (6 of 119 cases legitimately have zero ground-truth edges — scoring them as 0%
would defame any engine); and totals are **micro-averaged** (pool TP/FP/FN, then divide) so a 1-edge
case cannot outweigh a 20-edge case.

**Timer/instrumentation (`instrument.py`).** `Phases.phase(name)` context manager accumulates
wall-clock per phase (`discovery+parse`, `resolve`, `determinism`); `machine_specs()` captures CPU
model, physical/logical cores, RAM, Python version, platform; `peak_rss_mb()` via
`resource.getrusage`. *Cut corner, and it matters:* `getrusage` gives the high-water mark of the
harness plus the max of any **single** child, not a sum — for subprocess adapters it is a floor, not
a true peak. Do not compare RSS across engines with different process models without saying so.

**Determinism checker (`determinism.py`).** Runs a resolver twice on identical input and classifies
`identical` / `ordering-only` / `content-differs`, listing example edges on content differences.
Adapters may record emission order in `extra["edge_order"]`; without it a set comparison cannot see
ordering, so the checker falls back to canonical sort and can only ever report `identical`. **The
PyCG adapter records order — sessions 2–3 must do the same or the check is vacuous.** The three-way
split earned its keep immediately: see Q2c.

**PyCG adapter (`harness/adapters/pycg_adapter.py`).** Runs PyCG in a **subprocess**, deliberately.
PyCG mutates the host interpreter's `sys.path`, `sys.path_hooks`, and `sys.modules`
(`pycg/machinery/imports.py`); isolating it contains the FR-13 blast radius, gives clean timeouts,
and stops run-1 state from contaminating run-2 in the determinism check. *Known approximation:* PyCG
emits caller-scope→callee edges, not per-call-site targets, so `per_site` is filled by matching the
callee-expression tail against edges out of the enclosing scope. **`edges` (used for all scoring) is
exact; `per_site`/`unresolved_sites` is approximate and is a harness limitation, not an engine
result.** Do not report PyCG unresolved-site counts as an engine property.

**Driver (`run_eval.py`).** Subcommands `micro`, `scale`, `enumerate`; emits results JSON per
`harness/RESULTS_FORMAT.md`.

---

## 2. Benchmarks

### Django core (macro)
- Repo: https://github.com/django/django, shallow clone (`--depth 50`) at
  `benchmarks/django/`.
- **Commit: `274df4df0bca7fcfb5c1c1d49567f770df147eeb`** (2026-07-16, "Fixed #37093 — Clarified pull
  request instructions and adjusted error messages.")
- Line counts, counted by script (no cloc), for the **`django/` core package** — the intended
  ~100k-line target:

  | | files | total lines | blank | comment | code |
  |---|---|---|---|---|---|
  | `django/` (core package) | 908 | 165,065 | 22,475 | 11,296 | **131,294** |
  | whole repo (incl. tests/docs) | 2,927 | 523,363 | 69,771 | 24,740 | 428,852 |

  131k code lines is ~1.3× the ~100k FR-29 target — the right order of magnitude, and if anything a
  slightly conservative (harder) target. Use the `django/` subdirectory, not the repo root; the repo
  root is 4× larger and would silently make FR-29 look worse than specified.

### PyCG benchmark suite (micro)
- Repo: https://github.com/vitsalis/PyCG at `benchmarks/pycg-repo/`, commit
  **`8d5dc40837803beef1d8d379fbf2cdad6cd94641`** — whose message is literally *"Archival message"*
  (2023-11-26). **PyCG is archived and explicitly unmaintained.**
- **Artifact used: `micro-benchmark/snippets/`** — 119 cases across 18 categories, each a directory
  with `main.py` (+ helper modules), a `README.md`, and per-case ground truth `callgraph.json`. This
  is the artifact the prompt named as the fallback, and it is the one with per-case ground truth.
- **Not used:** `micro-benchmark-key-errs/` (targets PyCG's dict key-error operation, not call
  graphs — out of scope). The paper's macro-benchmark set (real PyPI packages) is **not in the repo**;
  it is not part of these artifacts. Only the micro suite was scored.
- Suite totals: **264 ground-truth edges**; **6 cases have zero edges** (all in `imports`) — see §4.

### Fixtures (`benchmarks/fixtures/`) — built for sessions 2–3 (FR-11)
All three parse clean and each ships `ground_truth.json`. None are ever executed.

| fixture | file(s) | routes | the interesting part |
|---|---|---|---|
| `flask_app/` | `app.py` | 5 | `@app.route`/`@app.post`; `DETAIL_RULE` module constant and `PREFIX + "/search"` concatenation are **variable rule strings**; `not_a_route()` is a negative control |
| `fastapi_app/` | `main.py` | 6 | `@app.get/post`; an `APIRouter(prefix="/admin")` whose 2 routes only become reachable via `app.include_router(router)`; `VERSION_PREFIX + "/status"` variable rule; `helper()` negative control |
| `django_app/` | `urls.py`, `views.py` | 2 static + 3 dynamic | `path("x", views.foo)`, `path("y", FooView.as_view())`, plus **loop-appended `reports/*` patterns that static analysis is expected to miss**; `unreferenced()` negative control |

The Django fixture's dynamic patterns are the deliberate negative control. Ground truth records them
under `expected_static_misses`, not `routes` — an engine is not penalized for missing them. What
sessions 2–3 should record is **whether the engine misses them silently or flags the dynamic append
as unresolved**; a silent miss is the dangerous failure mode, and under FR-14 the desirable behavior
is to over-approximate `views.legacy_report` as a possible route target without a concrete rule.

---

## 3. Q1 / Q2 / Q3 results

### Q1 — Is a shared, engine-agnostic measurement harness feasible? ✅ YES

Criterion was: runs end-to-end with ≥1 resolver plugged in and emits the results JSON. Met.
`python3 run_eval.py micro` drives PyCG across all 119 cases and writes
`results/pycg-micro.json` conforming to `RESULTS_FORMAT.md`; `run_eval.py scale` does the same for
Django. Full micro run: **28.6s wall** (14.1s resolve + 14.3s determinism re-runs + 0.05s
discovery/parse), peak RSS 22.4 MB.

The seam held under real strain, which is the actual evidence: the PyCG adapter is subprocess-based,
whole-program, and needs explicit entry points, yet none of that leaked into the scorer, the
determinism checker, or the results schema. An in-process, per-call-site engine (Jedi) should plug
into the same interface without schema changes. One caveat recorded honestly: the `per_site` half of
the interface fits PyCG only approximately (§1), so the *edge-level* seam is proven and the
*site-level* seam is not yet.

### Q2 — What does the suite establish, and what does PyCG score? ✅ ANSWERED (with a large caveat)

Ground truth: 119 cases / 18 categories / 264 edges, adjacency-list format (§4).

**PyCG 0.0.8, Python 3.9.25, micro-benchmark, micro-averaged:**

| category | cases | TP | FP | FN | precision | recall |
|---|---|---|---|---|---|---|
| args | 6 | 14 | 0 | 0 | 100.0% | 100.0% |
| assignments | 4 | 13 | 0 | 2 | 100.0% | 86.7% |
| builtins | 3 | 3 | 0 | 7 | 100.0% | 30.0% |
| classes | 22 | 52 | 0 | 0 | 100.0% | 100.0% |
| decorators | 7 | 20 | 2 | 2 | 90.9% | 90.9% |
| dicts | 12 | 18 | 3 | 1 | 85.7% | 94.7% |
| direct_calls | 4 | 10 | 0 | 0 | 100.0% | 100.0% |
| dynamic | 1 | 0 | 1 | 2 | 0.0% | 0.0% |
| exceptions | 3 | 3 | 0 | 0 | 100.0% | 100.0% |
| external | 6 | 11 | 0 | 0 | 100.0% | 100.0% |
| functions | 4 | 4 | 0 | 0 | 100.0% | 100.0% |
| generators | 6 | 18 | 0 | 0 | 100.0% | 100.0% |
| imports | 14 | 14 | 0 | 0 | 100.0% | 100.0% |
| kwargs | 3 | 10 | 0 | 0 | 100.0% | 100.0% |
| lambdas | 5 | 14 | 0 | 0 | 100.0% | 100.0% |
| lists | 8 | 15 | 0 | 1 | 100.0% | 93.8% |
| mro | 7 | 15 | 0 | 3 | 100.0% | 83.3% |
| returns | 4 | 12 | 0 | 0 | 100.0% | 100.0% |
| **TOTAL** | **119** | **246** | **6** | **18** | **97.6%** | **93.2%** (F1 0.954) |

Read this as a **ceiling, not a neutral baseline**: it is PyCG scored on the benchmark PyCG's own
authors wrote. Treat it as "what a call-graph-specialized engine achieves on cases chosen to
showcase it," and expect any engine to do worse on real code.

Where it loses points (all 6 FPs and 18 FNs, complete):
- `builtins` recall 30% — misses `map`-dispatched calls (`main -> main.func{,2,3}`) and builtin
  method calls on inferred types (`<**PyStr**>.join/split`, `<**PyDict**>.items`).
- `dynamic/eval` 0/0 — reports `main -> <builtin>.eval` but not the call *through* eval. Expected and
  correct-by-design: no static engine resolves `eval`. This single case drags the `dynamic` row to
  0%; it is 1 case and 2 edges.
- `mro/super_call` — misses `main.B.__init__ -> main.A.__init__` and `main.C.__init__ ->
  main.B.__init__`, i.e. `super()` chains in cooperative multiple inheritance. Relevant to Django,
  which leans on `super()` heavily.
- `dicts` FPs — 3 spurious `main -> main.func1` edges from dict-stored callables (over-approximation,
  the FR-14-desirable direction).
- `decorators` — 2 FPs / 2 FNs around decorator-returned functions.

### Q2b — PyCG cannot analyze modern Python (the pincer) ⚠️

This is the most decision-relevant thing found, and it is a *recorded result*, not a problem to fix
(no patching attempted, per the hard rule):

- **On Python 3.9, PyCG cannot parse the target code.** The Django scale probe **crashed in 3.1s**
  with an unhandled `SyntaxError` on `except*` (`django/core/handlers/asgi.py:208`, PEP 654, 3.11+).
  PyCG calls `ast.parse` on the *host* interpreter, so its syntax support is bounded by the Python it
  runs on.
- **On Python 3.13, PyCG cannot run at all.** Installed cleanly (needed `setuptools<81` for
  `pkg_resources`, removed in 81+), then failed on the *simplest* micro case — one that scores
  perfectly on 3.9 — with `ImportManagerError: Can't add edge to a non existing node`. Its import-hook
  machinery does not survive CPython 3.11+ import internals.

So PyCG needs an interpreter **at least as new as** the analyzed code's syntax, but **breaks on**
interpreters that new. Django main requires Python 3.12+. The two constraints do not intersect:
**PyCG cannot analyze Django core at any Python version available here.** Stopped at the budget.

A second, separate finding from the same crash: **PyCG has no per-file error tolerance.** One
unparseable file out of 908 aborted the entire analysis. The harness enumerator counts and continues.
For a tool that must survive real codebases, that difference is the point.

**FR-13 note (PyCG):** PyCG does *not* execute analyzed code — it installs a custom loader whose
`get_data()` returns `""`, so module bodies compile to nothing while the real import system resolves
paths. But it drives `importlib` for real (incl. an `importlib.import_module` fallback at
`imports.py:162`) and mutates `sys.path`/`sys.path_hooks`/`sys.modules`. It satisfies FR-13's letter
via a fragile trick rather than by construction. The harness enumerator satisfies it by construction.
**If an engine ever runs in-process, this distinction is a review item.**

### Q2c — Determinism (FR-44) ✅ ordering-only, and controllable

60 cases re-run twice (`classes`, `imports`, `dicts`, `mro`, `lambdas`): **45 `identical`, 15
`ordering-only`, 0 `content-differs`.** Overall classification: **`ordering-only`**.

Root cause confirmed, not guessed: the 15 cases cluster in `classes`/`dicts`/`mro`, where PyCG
iterates sets/dicts. Running one such case 4× with the default random hash seed gave 4 different
orderings; 4× with `PYTHONHASHSEED=0` gave **byte-identical** output. Same fact set every time.

Actionable: PyCG's nondeterminism is **emission-order only and fully controllable** — set
`PYTHONHASHSEED=0` and/or canonically sort at the write boundary. No real engine nondeterminism
observed. This also validates the three-way classifier: a boolean check would have reported "PyCG is
nondeterministic," which is true but badly misleading.

### Q3 — Are the two macro-benchmarks usable? ✅ YES

- **Cloned + pinned:** commit `274df4df0bca7fcfb5c1c1d49567f770df147eeb` (§2).
- **Line count confirmed:** 908 `.py` files, 165,065 total / **131,294 code lines** in `django/`.
- **Enumerable + parseable, with a version dependency worth knowing:**

  | harness Python | files | call sites | parse failures |
  |---|---|---|---|
  | 3.9.25 (system) | 908 | 36,682 | **5** |
  | 3.13.2 (via uv) | 908 | 37,218 | **0** |

  The 5 failures on 3.9 are **not Django defects** — they are `match` (3.10+) in `utils/json.py`,
  `utils/choices.py`, `template/defaulttags.py`, `test/selenium.py` and `except*` (3.11+) in
  `core/handlers/asgi.py`. On 3.13: **0 parse failures across all 908 files**, and the 536 extra call
  sites are exactly what those 5 files contain.

  **Constraint for the whole evaluation: the harness must run on a Python ≥ the analyzed code's
  syntax level.** Run the harness on 3.13 (`uv python install 3.13`), not the system 3.9.

  Free FR-29 datapoint: discovery+parse of all 908 files is **~2.0s wall, ~37 MB RSS**. Against a
  10-minute budget, parsing is ~0.3% — the FR-29 risk lives entirely in the resolve phase, so that is
  where sessions 2–3 should spend their measurement attention.

---

## 4. Ground-truth caveats (read before scoring anything)

1. **Format.** `callgraph.json` is an adjacency list `{caller_qname: [callee_qname, ...]}`, *not* an
   edge list. Keys with `[]` are graph **nodes with no outgoing edges — not edges**. Counting keys,
   or treating `[]` as an edge, inflates everything. `scorer.load_ground_truth()` handles this.
2. **6 of 119 cases have zero ground-truth edges** (all in `imports`, e.g. `imports/simple_import`
   only does `import to_import` and defines an uncalled function). Recall is **undefined** there, not
   0. The scorer returns `None`; micro-averaging skips them naturally. Do not let a reporting layer
   coerce `None` to `0`.
3. **Normalization applied** (`scorer.normalize()`, deliberately minimal):
   - Module `__init__` is **stripped**: `nested/__init__.py` → module `nested`. Confirmed against
     `imports/init_func_import`, where ground truth says `nested.func`, not `nested.__init__.func`.
     PyCG normalizes the same way (`pycg.py:_get_mod_name`), so this rule is compatible, not a thumb
     on the scale.
   - A trailing `.__init__` that is a **constructor** (`main.MyClass.__init__`) is **kept** — ground
     truth and PyCG agree on those. Only the *module* form collapses. Conflating the two would
     silently delete real constructor edges.
   - Repeated/leading/trailing dots collapse. Nothing else is touched — no case folding, no path
     rewriting.
4. **Ground truth names definition sites, not import aliases.** `from nested import func2` where
   `nested/__init__.py` re-exports from `.mod` yields `nested.mod.func2`. An engine reporting the
   alias will score as FN+FP. Sessions 2–3: if Jedi/Pyright report aliases, normalize **in the
   adapter** and say so here — do not loosen the scorer's edge-match rule.
5. **Non-source callees use sentinel qnames**, not real modules: `<builtin>.eval`, `<**PyStr**>.join`,
   `<**PyDict**>.items`, and `ext.*` for external packages (`external/` category). An engine that
   reports real stdlib qnames (`builtins.eval`, `str.join`) will score 0 on those categories for a
   naming reason, not a correctness one. **This is the most likely way to accidentally defame Jedi or
   Pyright.** Map to sentinels in the adapter, or exclude `builtins`/`external` and say so.
6. **The suite is PyCG's own.** It is small (264 edges), synthetic, and category-imbalanced —
   `classes` is 22 cases, `dynamic` is 1. A single case swings a category's percentage completely
   (`dynamic` = 0% on 2 edges). Quote category rows with case counts attached, and prefer the
   micro-averaged total.
7. **`micro-benchmark-key-errs/` is not a call-graph benchmark** — different operation, skipped.
8. The paper's **macro-benchmark set is not in the repo**; only the micro suite exists here.

---

## 5. Handoff notes for sessions 2–3

**Use Python 3.13 for the harness** (`uv python install 3.13`; path
`~/.local/share/uv/python/cpython-3.13.2-linux-x86_64-gnu/bin/python3.13`). The system 3.9 silently
drops 5 Django files. The harness itself is stdlib-only and version-agnostic; PyCG's venvs are
separate and irrelevant to you.

**Where things live**
- Resolver interface: `harness/resolver.py` — implement `resolve(project_root, call_sites) -> ResolutionResult`.
- Reference adapter: `harness/adapters/pycg_adapter.py`.
- Results schema: `harness/RESULTS_FORMAT.md`. Emit to `results/<engine>-<benchmark>.json`.
- Fixtures + route ground truth: `benchmarks/fixtures/{flask_app,fastapi_app,django_app}/ground_truth.json`.
- Call-graph ground truth: `benchmarks/pycg-repo/micro-benchmark/snippets/<category>/<case>/callgraph.json`.
- Django: `benchmarks/django/django` (the **`django/` subdir**, not the repo root).

**Adding an adapter**
1. Write `harness/adapters/<engine>_adapter.py` exposing `name`, `version`, and `resolve(...)`.
2. Populate `res.edges` with `(caller_qname, callee_qname)` — this is what gets scored.
3. Set `res.extra["edge_order"] = [...]` in emission order, or **the determinism check is vacuous**
   and can only ever return `identical`.
4. Set `res.extra["outcome"]` to `ok`/`crash`/`timeout`/`error`/`bad-output`.
5. Leave `per_site[...] = []` for genuinely unresolved sites (FR-14 evidence).
6. Register it in `run_eval.py::get_resolver` (one `if` branch; it raises with a pointer if unknown).

**Commands**
```bash
PY=~/.local/share/uv/python/cpython-3.13.2-linux-x86_64-gnu/bin/python3.13
cd prototypes/engine-eval/harness

# score an engine on the micro suite -> results/<engine>-micro.json
$PY run_eval.py micro --engine <engine> --out ../results/<engine>-micro.json

# scale probe against Django core (FR-29), 15-min cap
$PY run_eval.py scale --engine <engine> --root ../benchmarks/django/django \
    --timeout 900 --out ../results/<engine>-scale.json

# enumeration only, no engine (sanity-check a codebase)
$PY run_eval.py enumerate --root ../benchmarks/django/django

# reproduce the PyCG yardstick (needs the 3.9 venv; will NOT work on 3.13 — see Q2b)
python3 run_eval.py micro --out ../results/pycg-micro.json
```

**Gotchas already paid for** — you do not need to rediscover these:
- `pip install pycg` from **PyPI is broken on Linux**: the 0.0.8 wheel ships the package as `PyCG/`
  but imports `from pycg import ...`, which only works on case-insensitive filesystems (macOS).
  Install from the cloned repo instead (`pip install ./benchmarks/pycg-repo`), which builds correctly.
  The 3.9 venv is at `.venv-pycg/` and works.
- PyCG opens `-o` with mode `w+`, so **`-o /dev/stdout` fails** under a subprocess pipe ("not
  seekable"). Write to a real temp file.
- Pass **absolute** paths for `project_root` and entry points. The adapter runs with
  `cwd=project_root`, so relative paths double (`benchmarks/django/benchmarks/django/...`).
- Set `PYTHONHASHSEED=0` when comparing engine output across runs (Q2c).
- FR-13 is the harness's by construction (`ast.parse` on text only). If you plug in an engine that
  runs **in-process**, re-examine it — Jedi and Pyright have their own import/execution semantics, and
  this is the requirement most easily violated by accident.

---

## 6. Recommendation / risks to the evaluation plan

No engine recommendation — this session is infrastructure. Four flags, in priority order:

1. **The PyCG yardstick is weaker than the plan assumes, and cannot be strengthened.** PyCG gives a
   clean *correctness* reference on the micro suite (97.6%/93.2%) but **cannot run against Django
   core at any available Python** (Q2b). So there will be **no PyCG scale/FR-29 reference point** to
   compare Jedi and Pyright against — only a synthetic-correctness one. Any plan step that assumed
   "PyCG's Django timing" as a baseline needs to be dropped or re-scoped now. Per the hard rule, no
   patching was attempted; this is the recorded result.
2. **PyCG's numbers are a self-benchmark ceiling, not a neutral bar.** 97.6%/93.2% is PyCG scored on
   PyCG's own 264-edge synthetic suite. Do **not** set an acceptance threshold for Jedi/Pyright by
   reference to it. If a real bar is needed, it should come from the fixtures and Django, not here.
3. **Naming mismatches are the top scoring hazard.** Sentinel qnames (`<builtin>.eval`,
   `<**PyStr**>.join`, `ext.*`) and definition-site-vs-alias naming (§4.4–4.5) will make a *correct*
   engine look broken. Budget adapter time for qname normalization and record every mapping. When an
   engine scores surprisingly badly on `builtins`/`external`/`imports`, suspect naming first.
4. **The site-level seam is unproven.** `edges` is exact and fully exercised; `per_site` /
   `unresolved_sites` fits PyCG only approximately (§1), so FR-14 (over-approximation of ambiguous
   calls) has **not** actually been measured yet. Jedi/Pyright are per-call-site engines and should
   fit it *better* than PyCG did — but session 2 is where that interface first gets real load. If it
   doesn't fit, change it then, and expect to re-run PyCG's `edges`-only numbers unchanged.
