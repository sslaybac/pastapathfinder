# FINDINGS — Session 5 (Closing Probes): pandas Robustness, Incremental Equivalence, Narrowed Name-Match

Date: 2026-07-18
Scope: `prototypes/engine-eval/`. Throwaway prototype. No product code; `specs/`/`docs/` untouched.
Machine: Intel **i7-4700HQ** @ 2.40 GHz, 4C/8T, 23 GB RAM, Linux 5.14, CPython 3.13.2 — **confirmed
identical to the sessions 1–4 reference machine**; all timings directly cross-session comparable.
Engine: mypy **2.3.0** (`.venv-mypy`, pinned, not upgraded); name-match is stdlib `ast` only.

These three probes close the empirical phase. Headline per part:

- **Part 2 (pandas robustness):** mypy **survives the dynamism benchmark under FR-6 semantics.** The
  build ran to completion on a **664,190-LOC / 1,418-file** package (~5× Django) in **53 s / 1.27 GB**,
  **0 parse failures, 0 file casualties, 0 crashes, 0 mitigations**; missing compiled `.so`s surface as
  **typed `.pyi` stubs** (3,747 edges resolve *into* `pandas._libs`), never `Any`-aborts. The one blemish:
  **determinism FAILS by 3 edges** (88,225 vs 88,228 across fresh processes) — small (0.003 %) but real,
  and it **qualifies session 4's "identical on every axis"** (which held on the 618-edge forms tree).
- **Part 1 (incremental equivalence):** the per-file edge-cache merge is **provably equivalent** — a
  leaf change reloads **exactly 1 module**, the merged graph is **identical to a full rebuild (18,318 =
  18,318, diff 0)**, stale edges **evict correctly**, and the leaf-change path is **2.7 s** (11× under
  the 30 s budget, ~5× faster than session 4's 13.2 s core change). The hard-won lesson: the reloaded
  set is **`manager.rechecked_modules`, NOT tree-presence** (which over-counts and silently drops edges),
  and the merge must **replace, not union**, per-file edges.
- **Part 3 (narrowed name-match):** syntactic narrowing (N2 self/super + N1 duck-typing) cuts full-Django
  edges **6.4× (431,513 → 66,956)** and p99 fanout **760 → 32**, collapsing the documented worst
  offenders — **but the max stays 760** (attribute-chain constructor calls it cannot narrow), so the tail
  survives. On the deployment subset it covers **56.8 %** of mypy-unresolved attribute-call sites; **46.1 %
  of mypy's unresolved sites are bare-name value-flow, permanently uncoverable by any syntactic layer.**
  The hybrid union stays precise (**92.6 / 61.7** vs mypy 94.6 / 59.5) — but **narrowing adds nothing to
  that micro number** (raw-restricted == narrowed-restricted, exactly); its whole value is scale fanout.

---

# Part 2 — pandas run-to-completion (robustness benchmark)

## Target
- Repo: `github.com/pandas-dev/pandas`, **commit `f6df82f9d0bdba793cbe34251f57c5d6e3fe804c`** (2026-07-17,
  shallow clone). Target: the `pandas/` package, build-rooted at its parent `benchmarks/pandas` (same
  rooting rule as Django; `sys_path_root` → root=`benchmarks/pandas`, prefix=`pandas`).
- **`pandas/` package dir: 1,418 `.py` files, 664,190 physical LOC** — ~5× Django's ~131k, as intended.
  Contains **41 `.pyx` Cython sources** and **0 compiled `.so`** (extensions not built), plus **41 `.pyi`
  stubs** shipped alongside `_libs` (only 3 real `.py` in `_libs`).

## Protocol
One full run of the **existing, unchanged** mypy adapter (`run_eval.py scale --engine mypy`),
`check_untyped_defs=True`, cold cache, 30-min timeout.

## Completion verdict — ✅ RAN TO COMPLETION
`mypy.build.build` ran to completion. **outcome = ok.** No `CompileError`, no internal error, no
whole-build crash. **No mitigations were needed** (0 of the 2-mitigation budget spent).

| metric | value |
|---|---|
| `mypy.build.build` completed | **yes** (outcome=ok) |
| build time | **49.2 s** (informational — no bound asserted) |
| edge extraction (one bulk AST walk) | **3.9 s** |
| total resolve wall | **53.3 s**; full process wall **74 s** (`/usr/bin/time`) |
| **peak RSS** | **1,267 MB** (in-process getrusage) |
| discovery+parse | 10.2 s |

53 s for a 5×-larger tree vs Django's 11.3 s is **sub-linear-ish** (pandas is ~6× the call sites); the
whole-program-bulk-read architecture holds at this scale. 1.27 GB peak (vs Django 390 MB) is well within
23 GB.

## Per-file casualties — itemized (FR-6)
The FR-6 criterion is *the run completes and produces a graph for everything else*; individual files may
fail loudly. **Nothing failed:**

| casualty class | count | notes |
|---|---:|---|
| parse failures (ast enumeration) | **0** / 1,418 | |
| modules with no mypy tree (`files_no_tree`) | **0** / 1,418 | mypy produced a tree for **every** file |
| mypy internal errors attributed to a file | **0** | |
| call sites with no paired call node (`sites_no_call_node`) | **362** / 225,341 (**0.16 %**) | chained-call column drift — same benign class as Django's 11; not a failure |
| type-error diagnostics | 6,006 | expected; do **not** stop the build |
| engine errors (`res.errors`) | **0** | |

Sites matched: **224,979 / 225,341 (99.84 %)**. There is nothing to "exclude to get a completing build" —
the build completed on the full package as-is.

## Edges / sites / unresolved
- **Edges produced: 88,228** (fresh runs range 88,225–88,228 — see determinism below).
- **Unresolved sites: 87,600 = 38.9 %** of the 224,979 matched. **Higher than Django's 37 %, as expected**
  (pandas is the dynamism stressor) — but only marginally; the number itself is the finding, not a failure.

## C-extension behavior — richer than expected
The missing compiled `.so` binaries **do not abort the build and do not collapse to `Any`.** pandas ships
**41 `.pyi` stubs** for its `_libs` Cython extensions; with `mypy_path` on the package root and
`ignore_missing_imports=True`, mypy loads the **typed stubs**, so calls into the compiled layer resolve to
the stub-declared signatures. Evidence: **3,747 edges resolve into `pandas._libs.*`**, e.g.
`pandas._libs.lib.is_list_like` (×208), `pandas._libs.interval.Interval.__init__` (×171),
`pandas._libs.tslibs.offsets.BaseOffset.__init__` (×109). Where a stub is *absent*, the import is
missing → `Any` → no edge (the ordinary behavior). Either way the build is unaffected. (For reference,
17,089 edges target `<builtin>.*` and 2,115 target `<**Py…**>` type sentinels.)

## Determinism spot-check — ✗ NOT identical (a real, small caveat)
Two runs, sorted diff:

- **Two fresh processes** (`PYTHONHASHSEED=0` both): **88,225 vs 88,228 edges — CONTENT-DIFFERS by 3 edges.**
  All 3 differing edges target **`pandas.core.computation.ops._in`** (callers `BinOp.__call__`,
  `BinOp.evaluate`, `_eval_single_bin`) — a numexpr-style dynamic-dispatch site mypy resolves via value
  flow only intermittently.
- **Two in-process runs**: also differ (88,228 vs 88,225) — same 3-edge locus. Not hash-seed-driven
  (seed pinned); an order-dependent type-join inside mypy that the small forms tree never exercised.

**Magnitude 3 / 88,228 = 0.003 %.** This does **not** contradict FR-6 (the run completes; the graph is
complete-modulo-3-edges), but it **directly qualifies session 4's Q4 claim** ("mypy deterministic on
every axis, identical everywhere"): that held on `django/forms` (618 edges) and stays true at Django
scale, but **at pandas scale mypy's edge set is not bit-reproducible.** A product asserting FR-44 must
either pin to a tolerance or canonicalize away these value-flow-join races — it cannot assume mypy is
deterministic at arbitrary scale.

## Bonus — name-match on pandas (contextualizes Part 3's Django numbers)
`ast`-only, both policies (raw + narrowed), no scoring:

| tree | files | sites | classes | distinct method names | raw edges | raw fanout p99 / max | narrowed edges | narrowed p99 / max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Django** | 908 | 37,218 | 2,001 | 3,318 | 431,513 | **760 / 760** | 66,956 (**6.4× less**) | 32 / **760** |
| **pandas** | 1,418 | 225,341 | 1,797 | **13,391** | 400,051 | **26 / 292** | 316,884 (**1.26× less**) | 24 / **292** |

**The narrowing payoff is codebase-dependent.** pandas' raw fanout is *already low* (p99 26) because its
**13,391 distinct method names** (vs Django's 3,318) mean far less name-collision per method; narrowing
therefore only trims edges 1.26× and barely moves p99 (26→24). Django's 6.4× win is driven entirely by
its torture method-name collisions (`get`/`save`/`__init__` across hundreds of classes). Both tails
(max 292 / 760) are set by `__init__` over-approximation that narrowing does not reach. pandas also has a
**higher unresolvable-base rate (194/705 = 28 %** vs Django 12 %) — heavy inheritance from external/C
bases the name hierarchy can't resolve — and **46 %** profile-size-1 sites (vs Django 29 %), so N1 fires
even less often. Takeaway: **name-match fanout, and how much narrowing helps, is a property of the target
codebase, not a fixed number.**

## Verdict (Part 2)
**mypy-as-backbone survives the dynamism benchmark under FR-6 semantics.** It analyzes a 664k-LOC,
C-extension-heavy, dynamism-dense codebase to completion with zero file casualties, zero crashes, zero
mitigations, at 53 s / 1.27 GB, and degrades gracefully on missing compiled modules (typed stubs, not
aborts). The 38.9 % unresolved rate is the expected dynamism cost, not a failure. The **one genuine new
caveat** is a **3-edge (0.003 %) determinism failure at scale** that session 4's smaller trees did not
expose — minor, but it means FR-44 cannot be assumed for free at arbitrary size.

### Reproduction (Part 2)
```
PY=.venv-mypy/bin/python
# clone + pin
git clone --depth 1 https://github.com/pandas-dev/pandas benchmarks/pandas   # commit f6df82f9…
# full run -> outcome=ok, 88,228 edges, 53.3 s, 1,267 MB   results/mypy-pandas-scale.json
PYTHONHASHSEED=0 $PY harness/run_eval.py scale --engine mypy --root benchmarks/pandas/pandas \
    --timeout 1800 --out results/mypy-pandas-scale.json
# determinism (two fresh builds + diff) + C-extension edge check: scratchpad pandas_det2.py / pandas_namematch.py
```

---

# Part 1 — leaf-change incremental equivalence

## Leaf file chosen + verification
**`core/management/commands/migrate.py`** (module `core.management.commands.migrate`, 104 call sites),
a genuine **zero-importer leaf**. Verified three ways:
1. **Reverse-import graph** (session-2/4 `incremental.build_import_graph`): 0 direct importers.
2. **grep**: no static `import`/`from … import` of the module anywhere in Django; its package
   `__init__.py` does **not** re-export it (Django management commands are discovered **dynamically** by
   name, not statically imported). *(Note: the reverse-import tool under-resolves package-`__init__`
   relative re-exports — e.g. it wrongly reports `db/models/functions/datetime.py` as zero-importer when
   `functions/__init__.py` does `from .datetime import …`; management commands sidestep that class of
   miss entirely, which is why one was chosen.)*
3. **Empirically** (the decisive check): a warm build after touching the leaf reloads **exactly
   `{migrate}`** (below).

## The reloaded-module set — how lazy is the cache (and the hard lesson)
The **wrong** signal is `state.tree is not None`: a warm build **retains trees for cache-loaded modules
that were never re-type-checked** (430 trees present on one warm build), and `bres.types` is empty for
those, so re-extracting them yields **no edges** and silently **drops their cached edges**. The first
implementation did exactly this and produced a spurious 8,383-edge "equivalence gap."

The **correct** signal is **`bres.manager.rechecked_modules`** — precisely the modules mypy
re-type-checked this run, for which types are exported:

| condition | `rechecked_modules` (django-source) | leaf typed calls |
|---|---:|---|
| **warm build, no change** | **0** | — (confirms FINDINGS-mypy trap 5 exactly: nothing changed → nothing rechecked) |
| **warm build, leaf touched** | **1 = `{migrate}`** | 158 / 161 leaf calls carry types |

So for a zero-importer leaf change the cache is maximally lazy: **1 module reloaded.**

## Equivalence verdict — ✅ IDENTICAL
Protocol: cold build → per-file edge cache (keyed by the file of each call site); touch leaf (append
comment + `_MYPY_LEAF_PROBE = 1`); fresh warm build → reloaded set = `rechecked_modules`; re-extract only
those, **merge with cached edges of every non-reloaded file**; independent fresh cold rebuild of the same
tree; compare.

| set | edges |
|---|---:|
| cold cache (union of per-file, == adapter edges) | 18,318 |
| **merged** (warm leaf re-extract ⊕ cached rest) | **18,318** |
| **cold rebuild** (independent, same modified tree) | **18,318** |
| **diff (merged △ rebuild)** | **0 — IDENTICAL** |

## Eviction-variant verdict — ✅ EVICTS
Cache a leaf variant containing a distinctive self-call (`_leaf_evict_probe → migrate_probe_sentinel`);
rewrite the leaf to **remove** the call; warm-merge:

| check | result |
|---|---|
| cached sentinel edge present (variant A) | **yes** (`…migrate._leaf_evict_probe → …migrate.migrate_probe_sentinel`) |
| sentinel edge in merged graph after removal | **none — evicted** |
| merged == rebuild | **yes, diff 0** |

Eviction works **because the merge replaces a reloaded file's cached edge-set wholesale** with the freshly
extracted one — it does **not** union. A union-style merge would retain the stale edge (the exact bug the
product must not have).

## Timing — leaf ≪ core
| path | fresh-process wall | components | vs 30 s | vs 13.2 s core (s4) |
|---|---:|---|---|---|
| **leaf change (warm)** | **2.69 s** | warm build **0.24 s** + re-extract+merge **0.005 s** + interpreter/mypy startup ~2.4 s | ✅ **11× under** | ✅ **~5× faster** |
| (cold cache warm-up, informational) | 18.98 s | build 11.2 s | | |
| (cold rebuild reference) | 18.41 s | | | |

The leaf-change path is **dominated by process startup**, not analysis: the actual incremental work is
**0.245 s**. This is the direct answer session 4's Q3 masked — a leaf change is ~50× cheaper than the
core-file change (0.24 s build vs 7.3 s), because no cascade fires.

## Verdict (Part 1)
**The per-file edge-cache discipline is sound and provably equivalent to a full rebuild** — for a leaf
change it reloads 1 module, merges in 5 ms, and lands bit-identical to a cold rebuild, with correct
eviction. **What the probe learned the hard way, and the product's cache design MUST encode:** (1) the
reloaded set is `manager.rechecked_modules`, **never** tree-presence — trusting tree-presence drops the
cached edges of every retained-but-unchecked module; (2) the merge must **replace** a reloaded file's
edges, not union them, or stale edges never evict; (3) the per-file cache key is the **caller's file**, so
the union of per-file sets reconstructs the flat graph exactly (asserted: 18,318 == adapter's 18,318).

### Reproduction (Part 1)
```
PY=.venv-mypy/bin/python
# equivalence -> IDENTICAL, reloaded={migrate}, 2.7 s     results/mypy-leaf-incremental.json
PYTHONHASHSEED=0 $PY harness/mypy_leaf_incremental.py --equivalence --out results/mypy-leaf-incremental.json
# eviction    -> cache_had_edge=True, evicted=True         results/mypy-leaf-eviction.json
PYTHONHASHSEED=0 $PY harness/mypy_leaf_incremental.py --eviction    --out results/mypy-leaf-eviction.json
```
Code: `harness/mypy_leaf_incremental.py` (per-file extraction ported from the adapter's pairing loop).

---

# Part 3 — duck-typing-narrowed name-match (extends Probe B)

## What N1/N2 built (extends `harness/namematch.py`; raw policy kept runnable side-by-side)

**N1 — receiver attribute-profile intersection (duck typing).** The class index is extended, in the same
single AST walk, to record each class's **full defined-name set** (`class_attrs`) = methods + class-level
assignments (`X = …`, `X: T = …`) + instance attributes assigned as `self.<name> = …` anywhere in the
class's own methods. For an ambiguous `x.m(...)` whose receiver `x` is a plain local/param **Name**, the
resolver collects `x`'s **attribute profile** in the enclosing function scope (every `x.a`, `x.b(...)`,
`x.c = …`, not crossing nested def/lambda boundaries) and narrows the candidate classes (those defining
method `m`) to those whose `class_attrs` **⊇ the profile**. A profile of size 1 (only `m` itself) narrows
nothing and is recorded as such.

**N2 — self/super special case (the torture-case killer).** A **name-based class hierarchy** is built
from base-class expression tips (`class C(a.b.Base)` → tip `Base`), resolved via a simple-name → class-qname
index; unresolvable tips are **counted, not guessed**. `self.m(...)` → candidates restricted to the
enclosing class **plus its name-resolvable ancestors AND descendants**; `super().m(...)` /
`super().__init__(...)` → the enclosing class's **name-resolvable ancestors only**.

**Deliberate exclusions (recorded, not built):** no MRO ordering within the narrowed set (set membership
only); no type inference; no cross-function profiles; **no narrowing of chained/subscript receivers**
(`f().g`, `d[k]` → no edge, as raw) or of **attribute-chain receivers** (`a.b.m` → the receiver is not a
plain Name/`self`/`super`, so there is no narrowing signal → stays raw over-approximation, recorded as
`other_recv`). This last exclusion is where the residual tail lives.

## 3-M1 — Fanout before/after, full Django (same table as FINDINGS-namematch §3)

| policy | edges | ambiguous attr sites | median | mean | p90 | p99 | **max** | worst offender |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| **raw** (all-ambiguous) | 431,513 | 20,020 | 2 | 22.5 | 22 | 760 | **760** | `super().__init__` @ 760 |
| raw (nonempty only) | | 15,450 | 3 | 29.2 | 31 | 760 | 760 | |
| **narrowed** (all-ambiguous) | **66,956** (**6.4× fewer**) | 20,308 | **1** | **3.83** | **8** | **32** | **760** | `forms.MultiWidget.__init__` @ 760 |
| narrowed (nonempty only) | | 14,426 | 2 | 5.39 | 11 | 45 | 760 | |

**Narrowing kind breakdown (20,308 ambiguous attr sites + 16,709 bare + 201 chained):** `self` 4,870,
`super` 1,289 (N2 narrowed 6,159 total), `n1_narrowed` 3,394 (N1 fired), `n1_noprofile` 5,917 (N1 present
but profile size 1 → narrows nothing), **`other_recv` 4,838** (attribute-chain receivers → un-narrowed).
**Profile-size-1 frequency: 5,917 (29 % of ambiguous attr sites).** **Unresolvable bases: 229 / 1,902 (12 %).**

Reading against the **stated threshold** ("plausible if p99 lands in low tens **and** max is no longer
every class in the project; rejected if the tail survives"): **p99 collapses 760 → 32 (✓ low tens)** and
the documented worst offenders — `super().__init__`, `self.get` — are **gone** (N2 restricts them to the
ancestor/family set). **But the max stays 760 (✗):** the new worst offender `forms.MultiWidget.__init__`
is an **attribute-chain / explicit-ancestor constructor call** (`Widget.__init__(…)`, `forms.X.__init__`)
whose receiver is neither a plain Name nor `self`/`super`, so no narrowing signal exists and it fans out
to all 760 `__init__`-defining classes. **The tail is not eliminated; it is thinned and relocated.**

## 3-M2 — Fanout on the deployment subset (mypy-unresolved sites only)
The hybrid layer would fire **only where mypy is unresolved**. Restricting the narrowed matcher to mypy's
Django unresolved sites (`results/mypy-scale.json` locus, re-derived per-site):

- mypy unresolved sites: **13,786 total**, of which **9,342 are attribute-call** sites.
- **Narrowed fanout on those 9,342 attr sites:** median **1**, mean 4.86, p90 12, **p99 50**, **max 760**
  (nonempty-only: median 3, p90 31, p99 63). The tail is **worse** on this subset than overall — mypy's
  unresolved sites are disproportionately the hard `Any`-receiver dynamic ones.
- **Coverage: the narrowed matcher emits ≥ 1 candidate for 56.8 %** of mypy-unresolved attribute-call
  sites, and **53.9 % of ALL mypy-unresolved sites.**
- **Permanent gap: 46.1 %** of mypy's unresolved sites are **bare-name value-flow** (`cb()`, `d[k]()`,
  `funcs[0]()`) that present no attribute to match — **uncoverable by any syntactic layer, narrowed or not.**

## 3-M3 — Micro suite: narrowed alone + the narrowed union

| config | precision | recall | TP / FP / FN |
|---|---:|---:|---|
| mypy alone | 94.6 % | 59.5 % | 157 / 9 / 107 |
| **mypy ∪ narrowed name-match @ mypy-unresolved sites** | **92.6 %** | **61.7 %** | **163 / 13 / 101** |
| mypy ∪ **raw** name-match @ mypy-unresolved sites | 92.6 % | 61.7 % | **163 / 13 / 101** (identical) |
| narrowed name-match **alone** | 85.6 % | 40.5 % | 107 / 18 / 157 |
| *(session-4 raw union, UN-restricted, for reference)* | *85.3 %* | *61.7 %* | |

Two decision-relevant facts:
1. **The union stays precise: 92.6 % vs mypy's 94.6 %** (−2.0 pts) for **+2.2 pts recall** — a far better
   trade than session 4's un-restricted raw union (85.3 %, −9.3 pts). The precision is saved **not by
   narrowing but by restricting the layer to mypy-unresolved sites** (the honest hybrid model). Moved
   categories: **`external` R 18.2 → 54.5 (P 100 → 100)**, **`classes` R 80.8 → 84.6 (P 97.7 → 89.8)** —
   exactly the import-edge and method-dispatch recall Probe B identified, and the only categories that move.
2. **Narrowing changes the micro union by nothing — `mypy ∪ raw` and `mypy ∪ narrowed` are byte-identical
   (163/13/101).** On the small micro cases there is little fanout to collapse, so raw and narrowed emit
   the same candidates at unresolved sites. **The entire value of narrowing is scale fanout control
   (3-M1/3-M2); it buys zero micro precision.** Narrowed *alone* (85.6 / 40.5) even loses 1.5 pts recall
   vs raw-alone (84.1 / 42.0) — N2's name-based hierarchy occasionally drops a true edge when a base name
   doesn't resolve — while gaining 1.5 pts precision. A wash standalone; the union is where it belongs.

## 3-M4 — Determinism + timing
- **Determinism: two narrowed runs + sort → identical** (pure `ast` + sorted iteration). 66,956 edges both.
- **Timing (full Django):** index build 2.86 s + narrowed resolve 3.26 s = **6.1 s** (+ ~2 s enumerate
  ≈ 8 s end-to-end). Raw resolve alone is 0.22 s; **narrowing adds ~3 s** — the second AST walk that
  computes per-scope receiver profiles and the name hierarchy. Still ~40–60× faster than Jedi/Pyright,
  and ~1.4× slower than raw name-match's 4.4 s.

## Closing assessment (Part 3)
Against the stated threshold, the honest verdict is **viable as a bounded improvement — essentially
N2-driven — not a solution to FR-14, and rejected as a way to *close* the gap:**

- **N2 is the load-bearing mechanism and it works:** it collapses the documented worst offenders
  (`super().__init__`, `self.get`) that made raw unusable, cutting edges 6.4× and p99 760 → 32. **N1 is
  marginal** — it fires on only 3,394 sites and 29 % of N1 sites have a size-1 profile that narrows
  nothing (46 % on pandas). If budget forced a choice, **N2 alone captures most of the win.**
- **But the tail survives (max still 760)** on attribute-chain / explicit-ancestor constructor calls that
  carry no syntactic receiver, so narrowing **thins and relocates** the hairball rather than removing it —
  it does **not** clear the "max no longer every class" bar.
- **At product quality the union buys little:** +2.2 recall / −2.0 precision on micro, and narrowing
  contributes **nothing** to that number (raw-restricted == narrowed-restricted). Narrowing's value is
  purely operational — it keeps the *scale* edge count and fanout distribution tractable so the layer is
  affordable to compute and store — not qualitative.
- **The permanent gap is the decisive number: 46.1 % of mypy's unresolved sites are bare-name value-flow,
  uncoverable by any syntactic layer.** Even a perfect narrowed name-matcher leaves that half of the FR-14
  gap untouched; closing it needs the value/flow analysis mypy soundly declines to do, which is precisely
  what a syntactic over-approximator forgoes. **Narrowing makes the over-approximation layer *cheaper and
  saner*, not *sufficient*.**

### Reproduction (Part 3)
```
PY=.venv-mypy/bin/python
# 3-M1/M2/M4  fanout raw-vs-narrowed + deployment subset + determinism/timing
PYTHONHASHSEED=0 $PY harness/namematch_narrowed_scale.py --out results/namematch-narrowed-scale.json
# 3-M3  micro: narrowed alone + the four union configs
PYTHONHASHSEED=0 $PY harness/namematch_union_micro.py    --out results/namematch-narrowed-union-micro.json
# narrowed alone via the standard micro driver -> 85.6 / 40.5
PYTHONHASHSEED=0 $PY harness/run_eval.py micro --engine namematch-narrowed --out results/namematch-narrowed-micro.json
```
Code: `harness/namematch.py` (`NarrowedNameMatchResolver` + index extensions; raw `NameMatchResolver`
unchanged and runnable), `harness/namematch_narrowed_scale.py`, `harness/namematch_union_micro.py`.
Registered in `run_eval.py::get_resolver` under `namematch-narrowed`.

---

## Items not measured
- **pandas determinism third run** — not measured (two fresh + two in-process runs were sufficient to
  establish content-differs; a variance characterization beyond bimodal 88,225/88,228 was not pursued, per
  the "don't spiral" budget rule).
- **MRO ordering within the narrowed set** — deliberately excluded (set membership only), per the brief.
- Everything asserted above is measured; no result is softened.
