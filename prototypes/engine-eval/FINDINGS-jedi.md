# FINDINGS — Session 2: Jedi as a Call-Graph Engine

Date: 2026-07-17
Scope: `prototypes/engine-eval/`. Throwaway prototype. No product code, `specs/`, or `docs/` touched.
Engine under test: **Jedi 0.20.0 / parso 0.8.7 on CPython 3.13.2** (in-process, per-call-site inference).

**Headline.** Jedi is **high-precision, moderate-recall** on the PyCG micro-benchmark
(**97.2% precision / 77.6% recall**, micro-averaged), and it **never executes analyzed code**
(FR-13 ✅, with a witness-file proof). But it is a poor structural fit for a call-graph product:
it **fails the incremental-re-resolution budget outright** (Q3: 125–255 s vs a 30 s criterion —
Jedi has no incrementality), it is **non-deterministic by default** (Q4: content differs run-to-run
under a random hash seed; only `PYTHONHASHSEED=0` makes it reproducible), and at Django scale a
**parso cache-eviction bug fails ~30% of inferences** (Q2). The full-run *time* budget passes with
room to spare (Q2: 3.84 min vs 10 min), but the produced graph is heavily degraded by that bug.
Making a per-call-site inference engine emit a call graph at all took ~500 lines of adapter
scaffolding (§6) — that cost is itself a finding.

---

## 1. What was tested, and how

The Session-1 harness (`callsites.py`, `resolver.py`, `scorer.py`, `determinism.py`,
`instrument.py`, `run_eval.py`) was reused unchanged in its scoring core. A new
`harness/adapters/jedi_adapter.py` implements the `resolve(project_root, call_sites)` seam.

Jedi has **no whole-program graph** to read off — it answers one positional query at a time. So for
each enumerated call site the adapter points `jedi.Script(...).infer(line, col)` at the *end* of the
callee expression (the last name token) and turns each inferred definition into a
`(caller_qname, callee_qname)` edge. Caller names are computed **syntactically** in the adapter
(see §6), because that is not a question Jedi answers and computing it credits Jedi with nothing;
only the callee side is Jedi's inference.

| Question | Target | Criterion | Harness path |
|---|---|---|---|
| Q1 resolution quality | PyCG micro-benchmark, 119 cases / 264 GT edges | per-category prec/recall | `run_eval.py micro --engine jedi` |
| Q2 full-run wall time (FR-29) | `benchmarks/django/django` (908 files, 37,218 sites) | ≤ 10 min | `run_eval.py scale --engine jedi` |
| Q3 incremental re-resolution (FR-30) | 5 changed Django files + direct importers | ≤ 30 s | `harness/incremental.py` |
| Q4 determinism (FR-44) | micro + `django/forms` | identical under both hash seeds | `harness/q4_determinism.py` |
| Q5 framework routes (FR-11) | 3 fixture apps | detect routes; controls clean | `harness/route_probe.py` |
| FR-13 no-execution | side-effect fixture | witness file never written | `harness/fr13_check.py` |

**Machine (matches the Session-1 reference):** Intel i7-4700HQ @ 2.40 GHz, 4 physical / 8 logical
cores, 23.1 GB RAM, Linux 5.14, CPython 3.13.2. All numbers below are from this machine.

**Constraint check (mandated):** the PyCG micro yardstick still reproduces exactly under the
widened `per_site` key — **97.6% precision / 93.2% recall**, unchanged. Edge-level scoring and the
determinism classifier were not modified; only `per_site` was reshaped (§6.2), leaving `edges`
untouched.

---

## 2. Qualified-name normalization map

Ground truth names *definition sites* and uses sentinel qnames for non-source callees; Jedi reports
real, resolved names. Every rule below maps Jedi → ground-truth convention. None loosens the
scorer's edge-match rule; each is a pure renaming. (Implemented in `_normalize_qname`,
`_constructor_qname`, `_qname_for`.)

| Jedi produces | Normalized to | Rule |
|---|---|---|
| `builtins.str.join`, `builtins.dict.get`, … | `<**PyStr**>.join`, `<**PyDict**>.get` | builtin *types* use the `<**PyXxx**>` sentinel (9 types mapped: str/dict/list/int/float/bool/set/tuple/bytes) |
| `builtins.len`, `builtins.super`, `builtins.range` | `<builtin>.len`, `<builtin>.super`, `<builtin>.range` | builtin *functions/classes* use `<builtin>.`; **no `.__init__` appended** (GT writes `<builtin>.range`, not `<builtin>.range.__init__`) |
| a class `C` inferred at a call site `C()` | qname of the `__init__` actually reached **through the MRO** | GT names the *definition site*: `B()` inheriting `A.__init__` → `main.A.__init__`. A class with no source `__init__` anywhere in its MRO → **no edge** (matches `classes/call`, `classes/imported_call_without_init`) |
| `object.__init__` / `type.__init__` (typeshed) | — (dropped) | a stub is not a definition site; GT emits no edge |
| a lambda (`full_name=None`, `name='<lambda>'`) | `<scope>.<lambdaN>` via syntactic index | 1-based per-scope counter in source order, mirroring PyCG's `inc_lambda_counter`. Cross-module lambdas are dropped, not guessed |
| any intra-project function/method | Jedi's `full_name` verbatim (with package prefix, §6.3) | — |

The MRO resolution (`_constructor_qname`) is worth calling out: Jedi exposes **no** "resolve this
attribute through the MRO" API, so the adapter appends `<ClassPath>.__init__` to the class's own
module source and infers *there*. That is Jedi answering the MRO question through the only door its
API opens — not the adapter reimplementing C3.

---

## 3. Results

### Q1 — Resolution quality (PyCG micro, 119 cases)

**Micro-averaged total: precision 97.2% (205 TP / 6 FP), recall 77.6% (59 FN).**

| category | cases | TP | FP | FN | precision | recall |
|---|---:|---:|---:|---:|---:|---:|
| args | 6 | 12 | 0 | 2 | 100.0% | 85.7% |
| assignments | 4 | 13 | 0 | 2 | 100.0% | 86.7% |
| builtins | 3 | 6 | 0 | 4 | 100.0% | 60.0% |
| classes | 22 | 48 | 0 | 4 | 100.0% | 92.3% |
| **decorators** | 7 | 7 | 0 | 15 | 100.0% | **31.8%** ⚠ |
| dicts | 12 | 16 | 3 | 3 | 84.2% | 84.2% |
| **dynamic** | 1 | 0 | 1 | 2 | 0.0% | **0.0%** ⚠ |
| **exceptions** | 3 | 0 | 0 | 3 | n/a | **0.0%** ⚠ |
| **external** | 6 | 2 | 0 | 9 | 100.0% | **18.2%** ⚠ |
| functions | 4 | 4 | 0 | 0 | 100.0% | 100.0% |
| generators | 6 | 10 | 0 | 8 | 100.0% | 55.6% |
| imports | 14 | 14 | 0 | 0 | 100.0% | 100.0% |
| kwargs | 3 | 8 | 0 | 2 | 100.0% | 80.0% |
| lambdas | 5 | 14 | 0 | 0 | 100.0% | 100.0% |
| lists | 8 | 15 | 1 | 1 | 93.8% | 93.8% |
| mro | 7 | 15 | 1 | 3 | 93.8% | 83.3% |
| returns | 4 | 12 | 0 | 0 | 100.0% | 100.0% |
| **TOTAL** | **119** | **205** | **6** | **59** | **97.2%** | **77.6%** |

**Shape of the result.** Jedi almost never emits a *wrong* edge (6 FP total, precision ≥ 93.8% in
every category that has any): it is conservative by design — when it can't infer, it returns
nothing. The recall gap is concentrated in four categories, all confirmed as **genuine inference
limits, not naming mismatches** (normalization was verified against each before flagging):

- **decorators — 31.8% (15 FN).** Jedi resolves the decorated *name*, but does not model the
  wrapper's call *into* the wrapped function, nor `functools.wraps` round-trips. Most GT edges here
  are the wrapping/dispatch edges Jedi never forms.
- **dynamic — 0% (and the only FP category besides dicts/lists/mro).** `getattr(obj, name)()`
  dispatch is unresolvable statically; the lone FP is Jedi guessing one concrete attribute.
- **exceptions — 0% (3 cases, 0 edges produced).** Jedi emits nothing for these raise/handler call
  patterns; precision is `n/a` (correctly **not** coerced to 0%).
- **external — 18.2% (9 FN).** Calls into modules with no source in the snippet. Jedi resolves to a
  typeshed stub, which is not the definition site GT names.

- **mro — 83.3%, and it did *not* mis-linearize here** despite Jedi's known depth-first (non-C3)
  diamond bug: the 7 micro cases don't include a diamond whose C3 order flips the `__init__` target,
  so the bug is latent, not triggered. The 1 mro FP / 3 FN are ordinary inference gaps. **This is a
  live risk at scale** (§5) — the micro suite simply doesn't exercise it.
- **`super()` chains** resolved correctly where present (folded into `classes`/`mro`).

**Determinism sub-check (default seed): `identical` across 60 re-run cases.** This turned out to be
*falsely reassuring* — see Q4.

### Q2 — Full-run wall time on Django (FR-29): time ✅, graph degraded ⚠

| metric | value |
|---|---|
| files / call sites | 908 / 37,218 |
| **resolve wall time** | **230.7 s (3.84 min)** — criterion ≤ 10 min ✅ |
| discovery+parse | 2.0 s |
| peak RSS (harness + Jedi env subprocess floor) | 332 MB |
| edges produced | 15,553 |
| unresolved sites | 17,500 |
| **infer failures (`KeyError`)** | **11,129 (29.9% of sites)** ⚠ |

The time budget passes comfortably. **But the graph is badly degraded**: 11,129 of 37,218 call
sites (29.9%) raised `KeyError` inside Jedi and produced no edge. Root cause in §5 (a parso
cache-eviction bug). This is reported as **real, out-of-the-box engine behavior**. A one-line
monkeypatch (`JEDI_PARSO_CACHE_TRIGGER`) suppresses it and was *deliberately not* used for the
headline number — masking a 30% failure rate would misrepresent the product. The escape hatch exists
in the adapter, documented, for anyone who wants the counterfactual clean run.

### Q3 — Incremental re-resolution (FR-30): ❌ FAIL, badly

Changed set: 5 Django-core files
(`utils/translation/__init__.py`, `db/models/query.py`, `core/exceptions.py`,
`utils/functional.py`, `db/models/fields/__init__.py`) **+ their direct importers = 279 files /
25,896 call sites**. (The blow-up is real: `core.exceptions` has 162 direct importers,
`utils.functional` 118. "Direct importers" is a crude stand-in for dependents — the transitive
closure is larger still, so this is a *lower* bound on the true re-analysis cost.)

| condition | wall time | criterion |
|---|---|---|
| cold-process (fresh interpreter) | **255.2 s** | ≤ 30 s ❌ (8.5×) |
| warm-project (same `jedi.Project`, 2nd resolve; warmup 238.8 s) | **124.8 s** | ≤ 30 s ❌ (4.2×) |

**Fails under both conditions.** The root cause is architectural, not tunable: **Jedi has no
incrementality.** Each `resolve` builds fresh `jedi.Script` objects and re-infers every call site
from scratch. A warm `jedi.Project` only keeps parso's *syntax trees* around (halving the time); it
does not cache *inference results* across resolves. There is no "these 5 files changed, re-analyze
only what depends on them" entry point — the notion of a change set is foreign to the API. Two
approaches were tried (cold vs warm-project); a third (persisted inference) has no Jedi API to build
on.

### Q4 — Determinism (FR-44): ❌ non-deterministic by default, ✅ controllable

Target: `django/forms` (9 files, 1,350 call sites, 782 edges) — a mid-sized subpackage that actually
exercises Jedi's ambiguity resolution.

| condition | classification | detail |
|---|---|---|
| **default (random) hash seed** | **`content-differs`** | run A: `widgets.Media.render → widgets.Media.render_js`; run B: `…→ render_css`. Same 782-edge count, symmetric difference of 2 |
| **`PYTHONHASHSEED=0`** | **`identical`** | reproducible |
| micro (default seed, 60 cases) | `identical` | did not exercise the ambiguous path |

**Jedi's per-call-site result depends on hash ordering.** Where inference yields multiple candidate
definitions, which one survives edge-dedup depends on set/dict iteration order, which the random hash
seed perturbs — so the *content* of the graph changes run to run. This is a genuine FR-44 violation.
It **is controllable**: pinning `PYTHONHASHSEED=0` restores identity. Note the trap the harness
warned about (`FINDINGS-harness.md` §1) sprang here — the micro suite reported `identical` and would
have hidden this; only the larger subpackage surfaced it.

### Q5 — Framework route resolution (FR-11): routes detected, rules partially

Run with `.venv-jedi-fixtures` (Flask 3.1.3, FastAPI 0.139.2, Django 6.0.7 installed). Division of
labor stated up front: **AST supplies the shape** (which functions carry decorators, which args
`path()` got) and literal string values; **Jedi supplies the resolution** (that `app.route` is
`flask…Scaffold.route` and not some unrelated `.route`; that a URLconf view reference resolves to a
function). Only the second is credited to Jedi.

| fixture | routes detected | Jedi identified as route | view resolved | rules recovered | negative control |
|---|---|---|---|---|---|
| flask_app | 5 / 5 | 5 / 5 (`Scaffold.route`/`.post`) | n/a (decorator on fn) | 3 / 5 literal | `not_a_route` **not** flagged ✅ |
| fastapi_app | 6 / 6 | 6 / 6 (`FastAPI.get/post`, `APIRouter.get/delete`) | n/a | 3 / 6 literal | `helper` **not** flagged ✅ |
| django_app | 2 / 2 static | — | `views.foo` ✅; `FooView.as_view()` → only `typing.Callable` ⚠ | 2 / 2 literal | `unreferenced` **not** flagged ✅ |

Strengths: all decorator-based routes are correctly *identified as routes* (Jedi distinguishes a
Flask `app` from a FastAPI `APIRouter` — the two `APIRouter` routes resolve to
`fastapi.routing.APIRouter`, not `FastAPI`). Zero negative controls were mis-reported as routes
across all three apps.

Limits, all documented:
- **Variable rule strings are not recovered** (this is AST's job, and correctly so): Flask's
  `DETAIL_RULE` constant and `PREFIX + "/search"`, FastAPI's `VERSION_PREFIX + "/status"` and the
  `include_router(prefix=…)` composition — rule shows non-literal, so those rows read `rule=False`.
  Jedi does no string folding.
- **Class-based views lose their class**: `path("y", views.FooView.as_view())` resolves the view
  only to `typing.Callable` (`.as_view()` returns a Callable per typeshed). The route is detected and
  its rule matches, but the concrete view class is not named.
- **`path`/`re_path` themselves need `goto`, not `infer`**: Django binds `path = partial(_path, …)`,
  and `Script.infer()` returns nothing for a `functools.partial` name; `goto(follow_imports=True)`
  does find `django.urls.conf.path`. (A general Jedi limitation worth flagging for §7.)

**MODE of the expected miss** (`django_app`, the loop-appended `reports/*` patterns — GT records
these under `expected_static_misses`, so no penalty): **over-approximated, not silent.** The
`path()` call at `urls.py:17` *is* surfaced; its view **resolves correctly to `views.legacy_report`**;
only the rule `'reports/%s' % _kind` is non-literal, so the 3 concrete rules
(`reports/{daily,weekly,monthly}`) are not enumerated. This is exactly the desirable FR-14 behavior
`FINDINGS-harness.md` §2 asked for: the view target over-approximates as a possible route, the
concrete rule is (correctly) left unresolved. The "this is dynamic / inside a for-loop" flag,
however, is an **AST observation** (`rule_literal is None`, `inside_for_loop=True`), **not** a Jedi
signal — Jedi's contribution is resolving the view, not noticing the dynamism.

---

## 4. FR-13 — Jedi never imports or executes analyzed code: ✅ PASS

Proven, not asserted. `benchmarks/fr13-probe/sideeffect.py` writes a **witness file** at import time
*and* inside `boom()` and `Detonator.__init__`. `harness/fr13_check.py` runs the adapter over it and
checks three things:

1. **Witness file never created** — `RESULT: PASS`. The module body and both call targets would
   write it on execution; none ran. Jedi resolved all 12 call sites (12 edges, incl.
   `main → sideeffect.Detonator.__init__`, `sideeffect.boom → <builtin>.open`) from parso syntax
   trees alone.
2. **Subprocesses spawned** — exactly one, Jedi's environment prober
   (`jedi/inference/compiled/subprocess/__main__.py`), invoked with the site-packages path and
   `"3.13.2"` as argv. It introspects the *environment's* installed packages; it does **not** run
   the analyzed fixture. (The adapter pins `environment_path=sys.executable` so this subprocess is a
   known quantity rather than whatever Jedi's probing would pick.)
3. **`sys.modules` pollution** — none; the analyzed modules were never imported.

The adapter reads file *bytes* and `ast.parse`s them (same as the enumerator) and hands Jedi source
*strings*; nothing touches the code as code. FR-13 holds by construction and by test.

---

## 5. Failure modes (for Session 3)

1. **parso cache eviction → 30% infer failures at scale (the big one).**
   `parso.cache._set_cache_item` garbage-collects the parser cache once it holds ≥ 600 modules,
   evicting every entry whose `last_used` is older than 10 minutes. `last_used` is **pickled**
   (`parso/cache.py:180` sets it from *file mtime*, not "now"), so any module reloaded from parso's
   on-disk cache (`~/.cache/jedi`) returns carrying a timestamp from a previous run and is evicted
   immediately — while Jedi still holds a reference to it. `jedi/parser_utils.py:287` then does an
   unguarded `parser_cache[grammar._hashed][path]` and raises `KeyError`. Django (908 files, plus
   transitive stdlib imports) always crosses the 600 trigger — measured 11,129 failures on Django
   (Q2) and 3,943–4,757 even on the 279-file Q3 set. **Not** a stale-pickle artifact: a cold-disk run
   failed too. Escape hatch `JEDI_PARSO_CACHE_TRIGGER` (raise the threshold above project module
   count) is in the adapter but off by default. This is a Jedi/parso robustness bug that would bite
   any real large-codebase deployment.

2. **No incrementality (Q3).** Architectural, not a bug: re-analysis re-infers from scratch. A 5-file
   change costs 125–255 s. This is disqualifying for FR-30 as written.

3. **Non-determinism under a random hash seed (Q4).** Content-level, controllable only by pinning
   `PYTHONHASHSEED`. A product built on Jedi would have to set this in its own launcher.

4. **MRO diamond bug is latent.** Jedi linearizes depth-first, not C3, for diamond inheritance. The
   micro suite doesn't trigger it, but Django's deep class hierarchies (forms, models, admin) plausibly
   do — expect occasional wrong-`__init__` edges at scale that the micro score does not predict.

5. **`functools.partial` defeats `.infer()`.** `path`/`re_path` and any partial-bound callable return
   nothing from `infer()`; `goto()` is the workaround but yields a *location*, not an inferred value.

6. **Recall ceiling on decorators / dynamic / exceptions / external** (§3) — inherent to static
   per-call-site inference.

---

## 6. Adapter-complexity note (this is itself a finding)

`jedi_adapter.py` is **~500 lines**, and most of it exists to bridge the gap between "per-call-site
inference engine" and "call-graph producer." A whole-program engine (PyCG) needed a ~120-line
adapter. The delta is the cost of the impedance mismatch:

1. **Syntactic caller + lambda naming** (`_ScopeIndex`, ~75 lines). Jedi does not tell you *which
   scope a call is in* — that's the caller side of every edge, and it's a parsing question, not an
   inference one. The adapter walks the AST assigning caller qnames and reproducing PyCG's exact
   per-scope lambda counter. Decorators/defaults/annotations must be visited in the enclosing scope
   and the body in the function scope, each **exactly once**, or the lambda counter double-increments.

2. **`per_site` key widening** (`resolver.py`, `pycg_adapter.py`). Keyed `(file, line, col,
   callee_expr)` instead of `(file, line, col)` because 5.76% of Django call sites share a
   `(file,line,col)` — chained calls (`a().b().c()`) all start at the same column. `edges` (all
   scoring) is untouched; only `per_site` was reshaped, and the PyCG yardstick was re-verified
   unchanged (§1).

3. **sys.path root walk** (`_sys_path_root` + `_self_imports`, ~60 lines). The enumeration root and
   Jedi's import root differ: Django must be rooted at the *parent* of `django/` for `import django`
   to resolve (rooting at the package dir left 43% of `core/` unresolved). The walk is gated on the
   package importing itself absolutely, so three PyCG micro cases with a package-root `__init__.py`
   (relative imports only) are correctly *not* walked. Caller qnames get the package prefix so
   intra-Django edges don't look cross-module.

4. **MRO constructor probe** (`_constructor_qname`, ~50 lines). Jedi has no MRO-attribute API; the
   adapter synthesizes an attribute-access expression and infers it (§2).

5. **Byte→char column conversion** (`_char_col`). `ast` gives UTF-8 *byte* offsets; parso/Jedi want
   *character* offsets. They diverge on 50/37,218 Django sites (non-ASCII lines) — left unconverted,
   Jedi raises `ValueError` or, worse, silently resolves the wrong token.

6. **parso cache escape hatch** (§5).

None of these credits Jedi with anything it didn't do — they're all plumbing to make its answer
*comparable*. But the sheer amount of plumbing is the point: Jedi is an IDE/autocomplete engine, and
using it as a call-graph backend means the product owns a large, fiddly adapter with several
correctness traps (column offsets, lambda counters, root selection) that are easy to get subtly
wrong.

---

## 7. Recommendation

**Jedi is a weak fit as the call-graph engine, despite an attractive precision number.**

For:
- **Precision is excellent** (97.2%, ≤ 6 FP on 119 cases): it rarely fabricates edges.
- **FR-13 is airtight** — never executes analyzed code, minimal well-understood subprocess. Best-in-
  class on the safety requirement.
- **Route *identification* works** across Flask/FastAPI/Django (FR-11), with clean negative controls.

Against:
- **FR-30 fails outright** (125–255 s for a 5-file change vs 30 s). No incrementality exists in the
  API to build on — this is not tunable.
- **Non-deterministic by default** (FR-44); needs `PYTHONHASHSEED` pinned in the product's launcher.
- **~30% of inferences fail at Django scale** today, via a parso cache bug the product would have to
  patch around (the escape hatch works, but shipping a monkeypatch of a dependency's cache internals
  is a liability).
- **Recall is moderate (77.6%)** and structurally capped on decorators/dynamic/external — plus a
  latent MRO-diamond bug the micro score hides.
- **~500-line adapter** with several correctness traps, because the engine's model is per-position
  inference, not call graphs.

If the product's priorities are *safety + precision on a single-file / editor-scale query*, Jedi is
strong. If they are *whole-program call graphs with an incremental re-analysis budget and
run-to-run reproducibility* (FR-29/30/44 as written), Jedi does not clear the bar without owning
significant scaffolding and a dependency patch. **Recommend Session 3 weigh a whole-program engine
(or a hybrid: fast syntactic call-graph + Jedi only for hard single-site queries) rather than Jedi
alone.**

---

## 8. Handoff to Session 3

**Reproduce:**
```
# Q1  (writes results/jedi-micro.json)  -> 97.2% / 77.6%
.venv-jedi/bin/python harness/run_eval.py micro --engine jedi --out results/jedi-micro.json
# Q2  (writes results/jedi-scale.json)  -> 3.84 min, 11,129 infer errors
.venv-jedi/bin/python harness/run_eval.py scale --engine jedi \
    --root benchmarks/django/django --out results/jedi-scale.json
#   clean counterfactual (masks the parso bug):  JEDI_PARSO_CACHE_TRIGGER=100000 …
# Q3  -> cold 255s / warm 125s   (results/jedi-incremental.json)
.venv-jedi/bin/python harness/incremental.py --measure cold
.venv-jedi/bin/python harness/incremental.py --measure warm-project
# Q4  -> content-differs / identical   (results/jedi-determinism.json)
.venv-jedi/bin/python harness/q4_determinism.py --root benchmarks/django/django/forms
PYTHONHASHSEED=0 .venv-jedi/bin/python harness/q4_determinism.py --root benchmarks/django/django/forms
# Q5  (needs the fixtures venv with flask/fastapi/django)
.venv-jedi-fixtures/bin/python harness/route_probe.py
# FR-13
.venv-jedi/bin/python harness/fr13_check.py
```

**Environments:** `.venv-jedi` (Jedi 0.20.0, parso 0.8.7, CPython 3.13.2) for everything except Q5;
`.venv-jedi-fixtures` (same + Flask/FastAPI/Django installed) for the route probe only.

**Results JSON:** `results/jedi-{micro,scale,incremental,determinism}.json`. The Q5/FR-13 probes
print to stdout (not persisted as JSON — small enough to re-run).

**Open items / traps for next session:**
- The Q2 `jedi-scale.json` on disk is the **unmitigated** run (11,129 errors) — the honest headline.
  Re-run with `JEDI_PARSO_CACHE_TRIGGER=100000` if you want the clean-cache counterfactual, but label
  it as such.
- The MRO-diamond bug (§5.4) is **latent** in the micro score. If Session 3 wants a true read on
  Jedi's class-resolution at scale, build a diamond-inheritance fixture whose C3 order flips the
  `__init__` target — the micro suite won't tell you.
- Q3's "direct importers" under-counts dependents (no transitive closure). The true incremental cost
  is *worse* than 125–255 s, not better.
- If Session 3 pursues Jedi despite §7, the two must-dos before any real use: pin `PYTHONHASHSEED`,
  and decide how to handle the parso cache bug (patch parso, cap project size, or raise the trigger).
- `.infer()` vs `.goto()`: partial-bound callables (`path`, `re_path`, anything `functools.partial`)
  need `goto`. Any Session-3 route/framework work must account for this per-symbol.
