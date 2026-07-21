# FINDINGS — Session 4 (Probe A): mypy as a Call-Graph Engine

Date: 2026-07-18
Scope: `prototypes/engine-eval/`. Throwaway prototype. No product code, `specs/`, or `docs/` touched.
Engine under test: **mypy 2.3.0** (mypyc-compiled PyPI wheel) driven through its **programmatic build
API** (`mypy.build.build`) in-process on CPython 3.13.2.

**Headline.** mypy is the **first engine in this evaluation to PASS the incremental budget (Q3 /
FR-30).** Re-resolving the 5-file change set + direct importers (279 files / 25,896 sites) is
**13.2 s warm / 17.2 s cold, end-to-end, fresh process** against a ≤ 30 s criterion — versus **Jedi
125 s and Pyright 220–283 s, which both FAIL.** The reason is exactly the structural property the
brief hypothesized: mypy materializes a **whole-program typed AST + expression→type map in ONE
build**, and the call graph is read off it in **bulk with a single AST walk** (extract 0.5 s), so
there is no O(sites) per-site query wall. The same architecture makes the full run (Q2 / FR-29)
**11.3 s for all 908 files / 37,218 sites — ~20× faster than Jedi, ~42× faster than Pyright — with a
complete graph (0 failures).** mypy is **deterministic on every axis** (Q4 ✅, incl. cold-vs-warm
cache), **C3-correct on the diamond (Q6)**, and **never executes analyzed code (FR-13 ✅)**. Its
weakness is **recall: 59.5 %** (Jedi 77.6 %, Pyright 58.7 %) — mypy is *more conservative than Jedi*
on untyped legacy code (it returns `Any`, hence no edge, for unannotated params/returns/dict values).
The adapter is **486 lines** (Jedi ~500, Pyright 881), and the API is **semi-public/unstable** — the
single biggest production risk. **The bulk+incremental hypothesis held.**

---

## 1. What was tested

| item | value |
|---|---|
| mypy version | **2.3.0** (`mypy.version.__version__`); deps `mypy-extensions 1.1.0`, `typing-extensions 4.16.0`, `pathspec`, plus mypyc-compiled `.so`s |
| install | `uv pip install mypy` into `.venv-mypy` (uv venvs ship **no pip** — `uv pip`, not `python -m pip`) |
| API surface touched | `mypy.build.build(sources=[BuildSource(...)], options=Options())` → `BuildResult`; `result.graph[mod].tree` (`MypyFile`), `result.types` (expr→type map). **Semi-public: mypyc consumes it in-repo, NOT stability-guaranteed.** Node internals touched: `CallExpr`, `MemberExpr`, `NameExpr`, `FuncDef`, `OverloadedFuncDef`, `Decorator`, `TypeInfo.get()` (MRO walk), `CallableType.definition`, `Overloaded`, `Instance`, `get_proper_type`. **Version-pin implication recorded in §2/§6.** |
| Harness Python | uv CPython **3.13.2** (never system 3.9), per FINDINGS-harness §5 |
| Machine | Intel **i7-4700HQ** @ 2.40 GHz, 4C/8T, 23 GB RAM, Linux 5.14, CPython 3.13.2 — **matches the session-1/2/3 reference machine**; all timings directly cross-session comparable |
| Django target | `benchmarks/django/django` (908 files / 37,218 sites), build-rooted at the **parent** of `django/` so `import django` resolves |
| Harness changes | none to scoring core / determinism classifier / `per_site` key. Added: `run_eval.py::get_resolver` `mypy` + `namematch` branches; new adapter + probes (§ Reproduction). Inherited session 2's `(file,line,col,callee_expr)` key unchanged. |

**New code** (all under `harness/`): `adapters/mypy_adapter.py`, `mypy_incremental.py` (Q3),
`mypy_q4.py` (Q4), `mypy_route_probe.py` (Q5), `mypy_fr13.py` (FR-13), `q6_diamond.py` mypy branch.

---

## 2. Integration cost report (required)

**Plumbing rounds to first light: 3** (budget 5). Round 1: `build()` a toy, read a bound `CallExpr`
callee `.fullname` — worked immediately, and revealed the **first trap**. Round 2: the resolution
mechanisms (below). Round 3: constructor/builtin/external edge cases + column alignment.

**What broke / traps found (the next-session tax):**

1. **mypyc-compiled visitors cannot be subclassed (the big one).** The installed mypy wheel is
   **mypyc-compiled**, so `mypy.traverser.TraverserVisitor` is a compiled **`@trait`** and raises
   `TypeError: interpreted classes cannot inherit from compiled traits` when subclassed from
   interpreted Python — *even though* it is decorated `@mypyc_attr(allow_interpreted_subclasses=True)`
   (the attr does not lift the `@trait` restriction). The idiomatic way to walk a mypy AST is
   therefore **unavailable**. The adapter replicates `TraverserVisitor`'s exact child-visiting map
   (read from `traverser.py`) as a plain recursive walker (`_find_calls`/`_children`, ~90 lines). It
   descends **only structural children** (never `.node`/`.info`/`.type` semantic pointers), so it
   cannot wander into other modules' definitions. Coverage was validated empirically: on Django it
   matched **37,207 / 37,218** call sites (11 misses = 0.03 %). *A product on a compiled mypy owns
   this walker; a product on a pure-Python mypy build (`MYPY_USE_MYPYC=0`) could subclass directly
   but pays ~4× the analysis time — the compiled speed is the whole point, so keep the walker.*
2. **`mypy_path` must be set to the build root or sibling imports silently become `Any`.** With only
   source files passed as `BuildSource`, `from sibling import f` was treated as a *missing import*
   and `f` became a `Var: Any` with no `.node` — **zeroing recall on `imports` (14 %) and
   `direct_calls` (0 %)** until `options.mypy_path = [build_root]` was set. mypy does **not** infer
   the search path from a BuildSource's location the way this needs. (One round after first light.)
3. **File paths vs the build root.** Files are enumerated relative to the *package* dir
   (`benchmarks/django/django`) but mypy roots at its *parent*; joining relpaths against the build
   root dropped the inner `django/` and produced `CompileError: Cannot read file …`. The adapter
   keeps the two roots distinct (`file_root` for paths, `build_root` for `mypy_path` + module names).
4. **Chained calls share a start position.** `a()()` and `x.f().g()` report the *same* `(line,col)`
   for every call in the chain; keying mypy `CallExpr`s on `(line,col)` alone collapsed them onto one
   node and **zeroed `direct_calls`**. Fix: within each `(line,col)` bucket, calls are always
   *nested* (outer callee contains inner), so sort mypy calls by extent and the harness sites by
   callee-text length and pair index-by-index. (This is the same collision session 2 solved with the
   widened `per_site` key; here it recurs on the engine side.)
5. **Lazy cache = no trees when nothing changed.** A warm-cache build with **no** file change reloads
   **zero** trees (there is nothing to re-check), so a bulk re-extraction sees nothing. Only *changed*
   modules (+ cascade) get trees. This is correct mypy behavior but it means a product must keep a
   **per-file edge cache** and re-extract only reloaded trees (§5). It also shaped the Q4 cache probe
   (§4).

**Ported from prior adapters verbatim/near-verbatim** (harness/ground-truth concerns, credited to
neither engine): the `<**PyXxx**>`/`<builtin>` normalization + sentinels, `sys_path_root`/
`_self_imports` (build-root = parent of the self-importing package). The syntactic caller name +
lambda counter (`_ScopeIndex`) was **not** needed — the harness call site already carries `enclosing`,
and mypy's own `.column`/`.end_column` let me pair sites to nodes without recomputing scope.

**Size: `mypy_adapter.py` = 486 lines**, vs **Jedi ~500** and **Pyright 881**. mypy is the *smallest*
adapter of the three despite being the most capable, because the engine hands back a `.fullname`
directly (like Jedi, unlike Pyright's location→qname problem) **and** answers the whole program in one
call (unlike Jedi's per-site querying + MRO-probe synthesis). The ~90-line hand-rolled walker (trap 1)
is the only structural tax. **A production adapter would grow** mainly to pin/adapt to mypy-internal
API churn and to own the per-file edge cache (§5), not to add resolution logic.

---

## 3. Normalization map — deltas from the inherited rules

Started from FINDINGS-jedi §2 + FINDINGS-pyright §3; kept every rule; each is a pure renaming.

| rule | status vs prior sessions |
|---|---|
| builtin *types* → `<**PyXxx**>` sentinel (9 types) | **same** (`_normalize_builtin`) |
| builtin funcs/classes → `<builtin>.x`, no `.__init__` | **same**; needed for builtin **class constructors** too — `range()`/`map()` resolve to a `TypeInfo` in `builtins`, and GT names them `<builtin>.range` (class sentinel, NO `.__init__`). A constructor short-circuit emits the sentinel instead of chasing `__init__` (which would hit `object.__init__` and drop). |
| constructor `C()` → `__init__` through the **real C3 MRO** to the def site | **same intent**; mechanism is **`TypeInfo.get('__init__')`** (walks the actual MRO) — cleaner than Jedi's synthetic attribute-infer and Pyright's synthetic-`.__init__`-overlay. **C3-correct** (§Q6). No source `__init__` in the MRO → `object.__init__` → dropped, as before. |
| typeshed/`.pyi`/site-packages target → dropped | **same**; implemented as "keep only if the target's module is a **source module** (its file is under the enumeration root), else drop" — a cleaner test than path-sniffing, since mypy gives fullnames not paths. |
| lambda → `<scope>.<lambdaN>` | **NOT recovered** — see §5.2. mypy's `CallableType` for a lambda has `definition = None`, so a lambda-dispatch target cannot be named in GT's positional convention. A naming-convention gap, not an inference gap. |

**Delta summary:** no new sentinel conventions; one new *mechanism* (source-module membership as the
drop test), one *short-circuit* (builtin-class constructors → sentinel), and one *unrecovered*
convention (lambdas).

---

## 4. Results

Q3 and Q2 first (the decisive/scale axes), then Q4/Q1/Q5/Q6 and FR-13. `None` precision/recall is
never rendered 0; category rows carry case counts. All numbers are in-process getrusage / perf_counter
on the reference machine unless labeled fresh-process.

### Q3 — Incremental re-resolution (FR-30 proxy) — DECISIVE — ✅ **PASS** (first engine to)

Change set identical to sessions 2/3: the 5 pinned files + direct importers = **279 files / 25,896
sites** (`core.exceptions` alone has 162 direct importers). "Direct importers" under-counts true
dependents → **lower bound**. Protocol: (1) full build populates a persistent incremental cache
(timed separately); (2) a **real content change** to all 5 files (append a comment + a trivial
statement so the hash can't shortcut); (3) time a **fresh interpreter** doing `build()` against the
warm cache **+ the bulk re-read + edge re-extraction** — change-to-updated-edges, end to end.
Criterion ≤ 30 s.

| condition | fresh-process wall | components | edges | vs 30 s |
|---|---:|---|---:|---|
| **warm cache** | **13.2 s** | build **7.3 s** + extract **0.5 s** + interpreter/mypy startup ~5 s | 13,073 | ✅ **2.3× under** |
| **cold (no cache)** | **17.2 s** | build **10.0 s** + extract 0.5 s + startup ~5 s | 13,073 | ✅ 1.7× under |

**Numbers it beats: Jedi 125 s warm, Pyright 220 s pipelined / 283 s warm — both FAIL.** mypy is
**~9–17× faster** on this axis and **passes with headroom.**

**Why it passes — and the honest nuance.** The bulk read is the win: the whole affected set's edges
come from **one AST walk of the already-typed trees (0.5 s)**, not 25,896 individual queries (Pyright's
220 s wall) or 25,896 re-inferences (Jedi's 125 s). The **incremental cache is secondary**: it cuts
the *build* component from 10 s cold to 7.3 s warm (it saves re-analyzing the deep typeshed/Django
dependency closure), but even **cold, mypy re-analyzes the 279-file set in 17.2 s** — mypy is simply
*fast* at whole-program analysis. So the FR-30 pass is driven **primarily by whole-program speed +
bulk edge read, with incrementality as a bonus**, not by a clever change-set-diff. The re-extracted
graph is **complete and identical to cold** (13,073 = 13,073 edges), so the speed costs nothing in
completeness. (Changing heavily-imported *core* modules cascades a recheck across most of the 279 set,
which is why the warm build is still 7.3 s rather than near-zero — a smaller/leaf change would be
faster still.)

**FineGrainedBuildManager / dmypy variant: not measured — intentionally dropped.** The batch
`build()` path already passes ≤ 30 s decisively, so the long-lived-process number would only widen an
already-clear margin; spent the budget on Q1 naming instead (per the priority rule). A product wanting
sub-second edits should still evaluate `dmypy`'s fine-grained daemon — flagged for a follow-up.

### Q2 — Full run (FR-29 proxy) — ✅ **PASS** (time) with a COMPLETE graph

All 37,218 sites in `benchmarks/django/django` (908 files, 0 parse failures), `check_untyped_defs=True`,
cold cache.

| metric | mypy 2.3.0 | Pyright (s3) | Jedi (s2) |
|---|---|---|---|
| **resolve wall time** | **11.3 s (0.19 min)** ✅ | 477.9 s (7.96 min) ✅ | 230.7 s (3.84 min) ✅ |
| build / analysis | 10.5 s | 48.5 s (index) | — |
| **edge extraction** | **0.8 s** (one bulk AST walk) | 428.8 s (per-site query) | — |
| discovery+parse | 1.9 s | 2.2 s | 2.0 s |
| edges produced | **18,318** | 22,889 | 15,553 |
| sites matched | 37,207 / 37,218 (99.97 %) | — | — |
| unresolved sites | 13,786 (37 %) | 8,115 (22 %) | 17,500 |
| **infer / query failures** | **0** ✅ | **0** ✅ | 11,129 (29.9 %) ⚠ |
| type-error diagnostics | 2,553 (expected; do not stop the build) | — | — |
| peak RSS | **390 MB** (in-process) | ~1.0 GB (node) | 332 MB (in-proc floor) |

mypy is **~20× faster than Jedi and ~42× faster than Pyright** on the full run, with **0 failures**
and a complete graph, at **390 MB**. The `check_untyped_defs=True` measurement is the headline (Django
is largely unannotated; without it mypy skips unannotated *bodies* and resolves almost nothing). The
13,786 unresolved sites are the value-flow/dynamic/external categories from Q1 (empty candidate lists =
FR-14 evidence), not failures.

### Q4 — Determinism (FR-44) + cache-state probe — ✅ **PASS** (identical everywhere)

Target `django/forms` (9 files, 1,350 sites, **618 edges**). Criterion `identical`/`ordering-only`.

| condition | classification | detail |
|---|---|---|
| two runs, **default (random) hash seed** | **identical** | 618 = 618 |
| two runs, **`PYTHONHASHSEED=0`** | **identical** | 618 = 618 |
| **cold vs warm-cache** (build from a populated incremental cache, files re-touched to force tree reload) | **identical** | 618 = 618 — a from-cache build yields the same edge set as cold |
| micro suite (60 cases, weak check) | **identical** | — |

**mypy is deterministic on every axis, including the cache-state probe** — the mypy analog of session
3's server-state probe, and it comes out clean. Unlike Jedi (default-seed `content-differs`, needs
`PYTHONHASHSEED=0`), mypy needs no seed pinning: the default seed and seed 0 give identical graphs.
The cache probe needed care — a warm build with *nothing* changed reloads no trees (§2 trap 5), which
is not a determinism failure but *no work*; forcing tree reload (a real re-touch) shows cold==warm.

**Render canary.** `widgets.Media.render` resolves to a stable single set (`<**PyStr**>.join`,
`<builtin>.getattr`) across runs and seeds — no `render_js`/`render_css` flip (that was Jedi's; Pyright
was also stable). `render_canary.stable = true` under both seeds.

### Q1 — Resolution quality (PyCG micro, 119 cases / 264 GT edges)

**Micro-averaged: precision 94.6 % (157 TP / 9 FP), recall 59.5 % (107 FN).** Determinism: identical.
Comparison: **Jedi 97.2 / 77.6, Pyright 94.5 / 58.7, PyCG ceiling 97.6 / 93.2.**

| category | cases | TP | FP | FN | precision | recall | vs Jedi / Pyright recall |
|---|---:|---:|---:|---:|---:|---:|---|
| args | 6 | 7 | 0 | 7 | 100.0% | 50.0% | 85.7 / 50.0 |
| assignments | 4 | 14 | 0 | 1 | 100.0% | **93.3%** | 86.7 / 86.7 |
| builtins | 3 | 6 | 0 | 4 | 100.0% | 60.0% | 60.0 / 60.0 |
| classes | 22 | 42 | 1 | 10 | 97.7% | 80.8% | 92.3 / 80.8 |
| **decorators** | 7 | 7 | 2 | 15 | 77.8% | **31.8%** ⚠ | 31.8 / 36.4 |
| **dicts** | 12 | 8 | 4 | 11 | 66.7% | **42.1%** ⚠ | **84.2** / 26.3 |
| direct_calls | 4 | 4 | 0 | 6 | 100.0% | 40.0% | — / 40.0 |
| **dynamic** | 1 | 0 | 1 | 2 | 0.0% | **0.0%** ⚠ | 0.0 / 0.0 |
| **exceptions** | 3 | 0 | 0 | 3 | n/a | **0.0%** ⚠ | 0.0 / 0.0 |
| **external** | 6 | 2 | 0 | 9 | 100.0% | **18.2%** ⚠ | 18.2 / 18.2 |
| functions | 4 | 4 | 0 | 0 | 100.0% | 100.0% | 100 / 100 |
| generators | 6 | 8 | 0 | 10 | 100.0% | 44.4% | 55.6 / 44.4 |
| imports | 14 | 14 | 0 | 0 | 100.0% | 100.0% | 100 / 100 |
| **kwargs** | 3 | 3 | 0 | 7 | 100.0% | **30.0%** ⚠ | **80.0** / 30.0 |
| **lambdas** | 5 | 5 | 0 | 9 | 100.0% | **35.7%** ⚠ | **100** / 50.0 |
| lists | 8 | 10 | 1 | 6 | 90.9% | 62.5% | 93.8 / 50.0 |
| mro | 7 | 15 | 0 | 3 | 100.0% | 83.3% | 83.3 / 88.9 |
| returns | 4 | 8 | 0 | 4 | 100.0% | 66.7% | 100 / 66.7 |
| **TOTAL** | **119** | **157** | **9** | **107** | **94.6%** | **59.5%** | 77.6 / 58.7 |

**Shape.** High-precision / recall-limited, like both prior engines (9 FP, precision ≥ 90 % in every
category that emits ≥ 5 edges). Recall (59.5 %) sits ~1 pt above Pyright and **well below Jedi**. The
surprising finding is **where** mypy loses to Jedi: the **value-flow categories the brief expected
mypy to recover** are exactly where it is *weaker* than Jedi — **dicts 42 % (Jedi 84 %), kwargs 30 %
(Jedi 80 %), lambdas 36 % (Jedi 100 %).** Root cause is genuine, not naming (verified after two naming
rounds that fixed `imports` 14→100 %, `direct_calls` 0→40 %, `functions` 75→100 %): **mypy is a
soundness-oriented checker, so on unannotated legacy code it assigns `Any` and emits no edge**, where
Jedi's IDE-style inference speculatively follows the value. Specifically:
- **kwargs (30 %)** — an unannotated parameter `def f(cb=g)` has type `Any` (mypy does not narrow a
  param to its default), so `cb()` is unresolved. Jedi follows the default.
- **dicts (42 %)** — `CallableType.definition` recovers a dict/list value's callable **when mypy
  infers a precise element type** (`{"k": g}` → `dict[str, Callable]`), but a reassigned/updated/
  parametrized dict widens to `object`/`Any` and the edge is lost, and it picks a **single** value
  where GT expects the union (the 4 dict FPs are this: right category, wrong element).
- **lambdas (36 %)** — mypy types a lambda-dispatch call as `CallableType` but with `definition=None`;
  the lambda has no fullname, so it cannot be named `<lambdaN>` (§5.2). Jedi names it.
- **decorators (32 %), external (18 %), dynamic (0 %), exceptions (0 %)** — the same inherent static
  limits as both prior engines (wrapper→wrapped edges never formed; typeshed stub-drop on `external`;
  `getattr`/`eval` dispatch unresolvable; raise/handler patterns emit nothing).

Categories where mypy **wins or ties**: `assignments` 93 % (best of the three), `imports`/`functions`
100 %, `classes` 81 %, `mro` 83 %, and it is **C3-correct** where Jedi is not (§Q6).

### Q5 — Framework routes (FR-11) — routes detected across all three; controls clean

`.venv-mypy-fixtures` (mypy 2.3.0 + Flask 3.1.3 / FastAPI 0.139.2 / Django 6.0.7, same versions as
sessions 2/3), so mypy analyzes the frameworks' source. Frameworks are visible via `mypy_path` +
running under that venv (recorded: mypy reads the venv's site-packages automatically). AST supplies
decorator/arg shape + literal strings; mypy supplies resolution. Raw framework fullnames are kept (not
dropped) so route identity is visible.

| fixture | routes detected | mypy identified as route | rules recovered | negative control |
|---|---|---|---|---|
| flask_app | 5 / 5 | 5 / 5 (`flask.sansio.scaffold.Scaffold.route`/`.post`) | 3 / 5 literal | `not_a_route` **not** flagged ✅ |
| fastapi_app | 6 / 6 | 6 / 6 (`fastapi.applications.FastAPI.get/post` **vs** `fastapi.routing.APIRouter.get/delete`) | 3 / 6 literal | `helper` **not** flagged ✅ |
| django_app | 2 / 2 static | — (URLconf) | 2 / 2 literal | `unreferenced` **not** flagged ✅ |

Matches the profile of both prior engines: all decorator routes identified; FastAPI `app`
distinguished from `APIRouter`; variable rule strings (`DETAIL_RULE`, `PREFIX+…`, `VERSION_PREFIX+…`,
router-prefix composition) not recovered (correctly — AST's job); zero controls mis-flagged. Calibration
points:
- **`path`/`re_path` (functools.partial):** mypy resolved the **view** (`views.foo` →
  `django_app.views.foo`) but returned **nothing for `path` itself** — the `partial` binding defeats
  its callee resolution (like Jedi's `infer`, unlike Pyright which reached `django.urls.conf`). Not
  material: FR-11 wants the *view* target, which mypy got.
- **`FooView.as_view()`:** the concrete view class is **not** recovered (empty) — the typeshed
  `as_view()→Callable` artifact, same limit as both prior engines.
- **MODE of the expected static miss** (loop-appended `reports/*`): **over-approximated, not silent** —
  the `path()` at `urls.py:17` is surfaced, its view **resolves to `django_app.views.legacy_report`**,
  and only the `'reports/%s' % _kind` rule is (correctly) left unresolved. The "dynamic/in-loop" flag
  is an **AST** observation (`rule_literal is None`, `inside_for_loop=True`), not a mypy signal. This
  is the desirable FR-14 behavior and matches sessions 2/3.

### Q6 — Diamond MRO — mypy **C3-CORRECT** (like Pyright, unlike Jedi)

`benchmarks/diamond-mro`: `D(B, C)`, `B(A)`, `C(A)`; `A` and `C` define `__init__`. C3 MRO of `D` is
`[D, B, C, A]` → `D()` reaches **`C.__init__`**. mypy: `make() → main.C.__init__` — **C3-correct** (TP
1 / FP 0 / FN 0), via `TypeInfo.get('__init__')` walking the real MRO. Confirms the class-resolution
soundness Jedi lacked (Jedi: depth-first → `A.__init__`).

### FR-13 — mypy never imports or executes analyzed code — ✅ PASS

`benchmarks/fr13-probe` (module body + `boom()` + `Detonator.__init__` each write a witness on
execution). mypy resolved all 12 call sites (7 edges, incl. `main → sideeffect.Detonator.__init__`,
`sideeffect.boom → <builtin>.open`); the **witness file was never created**, and the analyzed modules
**never entered this process's `sys.modules`**. mypy runs **in-process** (like Jedi) but parses from
its own tokenizer/parser and type-checks statically — it does not import target code. One confirming
run.

---

## 5. Failure modes (naming/plumbing vs genuine limits)

Naming/plumbing failures (found and fixed) are separated from genuine resolution limits.

1. **Plumbing, FIXED (recall-critical):** `mypy_path` not set → sibling imports become `Any`
   (`imports` 14 %→100 %, `direct_calls` 0 %→40 %); file-root/build-root confusion → `CompileError`;
   chained-call `(line,col)` collision → `direct_calls` 0 %→40 %. All three were **naming/matching**,
   not inference (§2). Before fixes the total was 93.5 / 48.9; after, 94.6 / 59.5.
2. **Genuine limit — lambdas unnameable (naming-convention gap).** mypy types a lambda-dispatch call as
   `CallableType` **but `definition = None`** and a lambda has no fullname, so it cannot be mapped to
   GT's positional `<lambdaN>`. Recoverable only with a separate lambda-position index + value-flow
   tracking — more than a throwaway warrants. `lambdas` 35.7 % is this, not an inference failure (mypy
   *knows* the call target is a callable; it just can't name it in GT's convention).
3. **Genuine limit — conservative on untyped code (the recall story).** Unannotated params/returns/
   widened collections → `Any` → no edge. This is *by design* (mypy is a soundness checker) and is why
   mypy trails Jedi on value-flow (dicts/kwargs). It is **not tunable** without abandoning soundness;
   `check_untyped_defs=True` is already the maximally-permissive setting (measured: without it, Django
   resolves almost nothing).
4. **Genuine limit — typeshed stub-drop on `external` (18 %)** and inherent static gaps on
   `dynamic`/`exceptions`/`decorators` — identical to both prior engines.
5. **Integration risk — lazy cache needs a per-file edge cache.** A warm build with no change reloads
   no trees (§2 trap 5). A product must maintain an **edge cache keyed by file** and re-extract only
   the modules mypy actually reloaded, merging with retained edges — otherwise a warm re-extraction
   under-reports. (In Q3 this was masked because the core-file change cascaded a reload across most of
   the set; a leaf change would expose it.) This is real work a production adapter owns.
6. **Integration risk — API instability (the top production risk).** `mypy.build.build`,
   `BuildResult.graph/.types`, and every node/type internal touched are **semi-public, not
   stability-guaranteed** (mypyc consumes them in-repo, but they change between mypy releases). The
   compiled-`@trait` walker (§2 trap 1) is a standing tax tied to whatever mypy version ships. **A
   product MUST pin mypy exactly and re-validate the adapter on every bump.** 486 lines today; the
   pin-and-revalidate cost is the hidden ongoing tax.

---

## 6. Recommendation

**mypy is VIABLE as the sole engine, with caveats — and it is the only engine of the three that clears
FR-30.** The bulk+incremental hypothesis **held**: the whole-program typed-AST + bulk edge read is
exactly the structural fit the per-site engines lacked.

**Per-criterion, head-to-head:**

| criterion | Jedi | Pyright | **mypy** | favors |
|---|---|---|---|---|
| **FR-30** incremental ≤ 30 s | 125 s ❌ | 220–283 s ❌ | **13.2 s warm / 17.2 s cold ✅** | **mypy (only pass)** |
| **FR-29** full run ≤ 10 min | 3.84 min, **30 % failures** | 7.96 min, 0 failures | **0.19 min, 0 failures** ✅ | **mypy (fastest + complete)** |
| **FR-44** determinism | content-differs by default | identical | **identical (all axes)** | **mypy ≈ Pyright** |
| **FR-13** no execution | pass (in-proc) | pass (out-of-proc) | **pass (in-proc, no import)** | ~tie |
| **FR-12/14** resolution quality | 97.2 / **77.6** | 94.5 / 58.7 | 94.6 / **59.5** | **Jedi (recall)** |
| **FR-11** routes | detected | detected | **detected, controls clean** | ~tie |
| MRO correctness (Q6) | depth-first WRONG | C3-correct | **C3-correct** | mypy ≈ Pyright |
| adapter size | ~500 | 881 | **486** | **mypy** |
| API stability | stable public | LSP (stable protocol) | **semi-public, unstable** ⚠ | Jedi/Pyright |

**Did the bulk+incremental hypothesis hold? YES.** Q3 warm **13.2 s < 30 s**, the first and only
engine to pass FR-30 — by a **2.3× margin**, with a complete graph. **FR-30 does NOT fail for mypy**;
the requirements-amendment trigger that fired for Jedi and Pyright **does not fire here**. (If anything,
the relevant number for a *pending amendment* is the opposite: mypy shows a 30 s incremental budget is
**achievable** with a whole-program bulk-read engine, so the amendment discussion should be about
whether to *keep* the budget now that an engine meets it, not relax it.)

**The one thing that would stop me shipping mypy as-is** is the **recall (59.5 %)** against the product's
FR-14 over-approximation posture ("missing a real edge is the fatal failure mode"). mypy's soundness
makes it *drop* edges it can't prove — the opposite of over-approximation. On value-flow (dicts/kwargs/
lambdas) it is even more conservative than Jedi. So the strongest recommendation is a **hybrid**:
**mypy as the fast, complete, incremental, deterministic backbone** (it wins FR-29/30/44/Q6 outright),
**paired with an over-approximation layer** for the categories it soundly declines — which is exactly
what Probe B (FINDINGS-namematch.md) measures. mypy alone gives a precise, incrementally-cheap graph
that **under-approximates**; the product's FR-14 posture needs that union'd with a name-match/heuristic
layer for the dynamic/value-flow edges. The API-instability risk (§5.6) is a real but manageable
engineering cost (pin + revalidate), not a disqualifier.

**Verdict: viable with caveats — recommended as the graph backbone of a hybrid, not as a standalone
over-approximator.** It is the strongest engine tested on every axis except recall, and the only one
that meets the incremental budget.

---

### Reproduction

```
PY=.venv-mypy/bin/python
# Q1  micro -> 94.6 / 59.5 (identical)          results/mypy-micro.json
PYTHONHASHSEED=0 $PY harness/run_eval.py micro --engine mypy --out results/mypy-micro.json
# Q2  scale -> 11.3 s, 18,318 edges, 0 failures  results/mypy-scale.json
PYTHONHASHSEED=0 $PY harness/run_eval.py scale --engine mypy --root benchmarks/django/django --out results/mypy-scale.json
# Q3  incremental -> warm 13.2 s / cold 17.2 s (PASS)   results/mypy-incremental{,-cold}.json
PYTHONHASHSEED=0 $PY harness/mypy_incremental.py --measure warm --out results/mypy-incremental.json
PYTHONHASHSEED=0 $PY harness/mypy_incremental.py --measure cold --out results/mypy-incremental-cold.json
# Q4  determinism -> identical (both seeds + cold==warm cache)   results/mypy-determinism{,-defaultseed}.json
PYTHONHASHSEED=0 $PY harness/mypy_q4.py --out results/mypy-determinism.json
$PY harness/mypy_q4.py --out results/mypy-determinism-defaultseed.json
# Q5  routes (needs .venv-mypy-fixtures)   results/mypy-routes.json
.venv-mypy-fixtures/bin/python harness/mypy_route_probe.py
# Q6  diamond MRO -> C3-correct
$PY harness/q6_diamond.py --engine mypy
# FR-13 -> PASS
$PY harness/mypy_fr13.py
```

Adapter/probes: `harness/adapters/mypy_adapter.py`, `harness/mypy_{incremental,q4,route_probe,fr13}.py`,
`harness/q6_diamond.py` (mypy branch). Env: `.venv-mypy` (mypy 2.3.0), `.venv-mypy-fixtures`
(+flask/fastapi/django) for Q5. mypy prints a benign `Nothing to do?!` on some micro cases — filtered
in reproduction, harmless. Q3 uses a persistent cache at `.mypy-cache-q3/` (wiped per run).
