# FINDINGS — Session 4 (Probe B): Naive Name-Match Over-Approximator

Date: 2026-07-18
Scope: `prototypes/engine-eval/`. Throwaway prototype. **Not a design; not product code.** This is
**evidence for a pending scope ruling** — how much recall a name-matching resolution layer recovers,
and at what precision and candidate-fanout cost — under the product's FR-14 over-approximation posture
("include an edge to every statically possible target; missing a real edge is the fatal failure mode").

**Headline.** A ~200-line name-matcher over stdlib `ast` alone (no engine, no type inference) is
**deterministic and fast (full Django in 4.4 s)** and it does over-approximate — but the recovery is
**concentrated, not broad.** It recovers recall specifically where a miss is an **import-resolution or
method-name-dispatch** problem (`external` 54.5 % vs the engines' 18.2 %; method calls via the class
index), and it **does NOT** recover the **value-flow-through-data-structures** categories the engines
also drop (`dicts` 26 %, `kwargs` 20 %, `lists` 31 %, `generators` 39 %) — because those calls are
bare-name (`cb()`, `d[k]()`), not `x.method()`, so a syntactic method-name matcher has nothing to
match. The cost of the over-approximation is **severe at scale**: full Django produces **431,513 edges
(23× mypy's 18,318)**, with an ambiguous attribute-call fanout of **median 3 but p99/max 760** — driven
by torture method names (`__init__`, `get`, `save`). **Conclusion: a name-match layer is worth adding
to a precise engine for the method-dispatch + external gap, but it needs receiver-class narrowing
before the fanout is usable, and it is not a substitute for value-flow analysis.**

---

## 1. What was built

**Two indexes**, built from the analyzed files with stdlib `ast` only (`namematch.py`, ~200 lines):

- **Per-module symbol table** (`_ModuleIndex`): for each module, the top-level `def`s and `class`es and
  the `import`/`from … import … as …` aliases, each mapped to the qualified name it resolves to.
  Django's `from django.x import y` has the `django.` prefix stripped to match the enumeration-root
  module naming. This resolves a **bare-name call** `foo(...)` to a single qname through the module's
  own names and its import edges.
- **Class index** (`method_to_classes`): a map from **method name → every analyzed class that defines a
  method of that name**, built by a scoped AST walk (handles nested classes). This is the
  over-approximation engine for attribute calls.

**Resolution policy** (no type inference of any kind):
- **direct call `foo(...)`** → module symbol-table / import lookup → **single candidate**. If `foo`
  names a class → constructor `foo.__init__` **iff** that class defines one, else **no edge** (naive:
  no MRO walk — a deliberate limitation).
- **attribute call `x.m(...)`** → an edge to **every analyzed class's `m`** (all candidates, each
  flagged ambiguous). This is the FR-14-shaped over-approximation and the source of the fanout.
- **chained/subscript callees** (`f().g`, `d[k]`) → not name-matchable → no edge.

**Deliberate naïvetés** (all recorded, none accidental): no MRO (constructor uses the class's own
`__init__` or nothing); no receiver-type narrowing (every `x.m()` fans out to all `m`-defining classes
regardless of what `x` is); no value flow (a callable stored in a dict/list/param is invisible); no
scope resolution beyond the module symbol table (a call to a name bound in an enclosing *function*
scope is not resolved — hence `assignments` 0 %). It is intentionally the crudest FR-14 posture:
over-approximate method dispatch, omit everything else.

---

## 2. B-Q1 — Micro recall/precision (119 cases / 264 GT edges)

**Micro-averaged: precision 84.1 % (111 TP / 21 FP), recall 42.0 % (153 FN).** Deterministic
(identical across two runs). Framed per-category (this probe does not compete on the overall score);
recall columns for the collapsed categories compared against the three engines.

| category | cases | TP | FP | FN | prec | **name-match R** | Jedi R | Pyright R | mypy R |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **dynamic** | 1 | 0 | 0 | 2 | n/a | **0.0%** | 0.0 | 0.0 | 0.0 |
| **exceptions** | 3 | 0 | 0 | 3 | n/a | **0.0%** | 0.0 | 0.0 | 0.0 |
| **decorators** | 7 | 7 | 2 | 15 | 77.8% | **31.8%** | 31.8 | 36.4 | 31.8 |
| **dicts** | 12 | 5 | 0 | 14 | 100% | **26.3%** | 84.2 | 26.3 | 42.1 |
| **kwargs** | 3 | 2 | 0 | 8 | 100% | **20.0%** | 80.0 | 30.0 | 30.0 |
| **direct_calls** | 4 | 3 | 0 | 7 | 100% | **30.0%** | — | 40.0 | 40.0 |
| **generators** | 6 | 7 | 0 | 11 | 100% | **38.9%** | 55.6 | 44.4 | 44.4 |
| **lists** | 8 | 5 | 0 | 11 | 100% | **31.2%** | 93.8 | 50.0 | 62.5 |
| **external** | 6 | 6 | 0 | 5 | 100% | **54.5%** ⬆ | 18.2 | 18.2 | 18.2 |
| classes | 22 | 36 | 10 | 16 | 78.3% | 69.2% | 92.3 | 80.8 | 80.8 |
| mro | 7 | 11 | 7 | 7 | 61.1% | 61.1% | 83.3 | 88.9 | 83.3 |
| args | 6 | 6 | 0 | 8 | 100% | 42.9% | 85.7 | 50.0 | 50.0 |
| assignments | 4 | 0 | 0 | 15 | n/a | 0.0% | 86.7 | 86.7 | 93.3 |
| builtins | 3 | 1 | 0 | 9 | 100% | 10.0% | 60.0 | 60.0 | 60.0 |
| functions | 4 | 1 | 0 | 3 | 100% | 25.0% | 100 | 100 | 100 |
| imports | 14 | 8 | 2 | 6 | 80.0% | 57.1% | 100 | 100 | 100 |
| lambdas | 5 | 5 | 0 | 9 | 100% | 35.7% | 100 | 50.0 | 35.7 |
| returns | 4 | 8 | 0 | 4 | 100% | 66.7% | 100 | 66.7 | 66.7 |
| **TOTAL** | **119** | **111** | **21** | **153** | **84.1%** | **42.0%** | 77.6 | 58.7 | 59.5 |

**Reading the collapsed categories (the point of this probe).** The hypothesis — that symbol-table +
class-hierarchy name matching recovers "much of the recall the inference engines drop on
dynamic/value-flow categories" — is **only partially borne out, and the split is the finding**:

- **Where it recovers (⬆): `external` (54.5 % vs 18.2 %).** These are calls into modules the engines
  drop as typeshed stubs; the name-match **import table** resolves `ext.function` directly from the
  `from ext import …` edge, no stub involved. This is genuine, precision-free recall the engines
  cannot reach. It also holds recall on **method-dispatch** (`classes` 69 %, `mro` 61 %) — but at a
  **precision cost the engines don't pay** (10 FP on `classes`, 7 on `mro`: the over-approximation
  names the wrong class's method).
- **Where it does NOT recover: `dicts` 26 %, `kwargs` 20 %, `lists` 31 %, `generators` 39 %,
  `dynamic`/`exceptions` 0 %.** These are the value-flow categories, and name-match is **at or below**
  the engines here — because the calls are **bare-name through a data structure or parameter**
  (`cb()`, `d["k"]()`, `funcs[0]()`), which present no attribute to match and no import to follow. A
  syntactic method-name matcher structurally cannot see a callable that flowed through a dict. These
  need real value-flow analysis (where mypy's `CallableType.definition` helps and name-match cannot).

So name-match's over-approximation fires **exactly on `x.method()` shapes and import edges**, not on
value flow. It is a **complement to** a precise engine on method dispatch + external, **not a
replacement** for flow analysis.

**Union with mypy (the decision-relevant number).** Scoring `mypy ∪ name-match` against GT — i.e. what
an over-approximation *layer on top of* the precise engine actually buys — the micro totals go from
**mypy alone 94.6 % / 59.5 %** to **union 85.3 % / 61.7 %**: **+2.2 pts recall for −9.3 pts precision.**
The recall gain is almost entirely `external` (18.2 → 54.5) and `classes` (80.8 → 84.6); the value-flow
categories (`dicts`, `kwargs`, `lambdas`, `generators`, `decorators`, `dynamic`, `exceptions`) are
**unchanged** — the naive layer adds nothing where mypy is weakest — while the FP count triples
(9 → 28). **A raw, un-narrowed name-match layer is a poor recall trade on top of a precise engine:** it
recovers a narrow slice (imports/external + some method dispatch) at a steep precision cost, and misses
the value-flow edges entirely. This is the sharpest single argument that the layer needs
receiver-narrowing (§3, §5) — or that the FR-14 gap is better closed by improving the engine's value-flow
resolution than by bolting on syntactic over-approximation.

---

## 3. B-Q2 — Candidate fanout at scale (the hairball metric)

`ast`-only, so fast; run over `django/forms` and the full `django/` tree. Distribution of candidate-set
sizes for **ambiguous attribute-call sites** (`x.m(...)` that matched ≥ 1 class).

| tree | files | sites | edges | classes indexed | distinct method names | ambiguous attr sites | fanout median | mean | p90 | p99 | **max** | worst offender |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| django/forms | 9 | 1,350 | **3,629** | 97 | 158 | 678 | 3 | 10.1 | 51 | 51 | **51** | `super().__init__` (`__init__`, fields.py:177) |
| django (full) | 908 | 37,218 | **431,513** | 2,001 | 3,318 | 20,020 | 3 | 29.2 | 31 | 760 | **760** | `super().__init__` (`__init__`, admin/filters.py:97) |

(Fanout stats above are over non-empty ambiguous sites; including attribute sites that matched **zero**
classes, the full-Django median is 2 and mean 22.5.)

**The honest cost of the posture.** For the same full Django, mypy produced **18,318** edges; name-match
produces **431,513 — a 23× blow-up.** The distribution is bimodal: a **median attribute-call fanout of
3** is tolerable, but the tail is not — **p99 = 760** and the max, `super().__init__` / any `__init__`
call, fans out to **760 classes** (every analyzed class with an `__init__`). The torture cases are the
predictable common method names — `__init__`, and (spot-checked in the index) `get`, `save`, `render`,
`__str__` — dozens-to-hundreds of definitions each. **A median slice is usable; a slice through
`self.get(...)` or `super().__init__(...)` is a hairball of hundreds of spurious targets.** This is the
number that decides the scope ruling: **the posture is not viable at product quality without
receiver-class narrowing** (restrict `x.m()` to classes compatible with the static/inferred type of
`x`) — which is precisely the type information a name-only matcher throws away and an engine like mypy
already has.

---

## 4. B-Q3 — Wall time + incremental reasoning

**Full-Django wall time: 4.4 s** (index build 2.24 s + enumerate ~1.9 s + resolve 0.20 s), vs
**3.84 min (Jedi) / 7.96 min (Pyright) / 0.19 min (mypy)** for the same tree. The `ast`-only matcher is
**~50× faster than Jedi and ~100× faster than Pyright** — as expected, it does no analysis. django/forms
runs in 0.16 s. **Determinism: two runs + sort → identical** (pure `ast` + sorted iteration; recorded).

**Incremental reasoning (no implementation, per the brief).** A changed file invalidates the two
indexes only where it contributes symbols, and both are **per-file additive**, so rebuilding them is
cheap: (1) the **module symbol table** for exactly that one module is recomputed; (2) the **class
index** loses/re-adds only the method-name entries for classes *defined in that file*. No cross-file
recomputation is needed to rebuild the indexes. **But** the expensive ripple is in the *edges*, not the
indexes: every ambiguous `x.m()` edge **anywhere in the project** whose target method-name's
defining-class *set* changed must be re-materialized. For the 5-file change set this is a small delta —
**unless** one of the changed files adds or removes a method with a very common name (`get`/`save`/
`__init__`), in which case a large fraction of the project's attribute-call edges shift. So the
incremental cost of the name-match layer is **bounded by the churn in common method names**, not by the
number of changed files — another reason receiver-narrowing (which localizes each `x.m()` to few
classes) matters for incrementality as well as slice quality.

---

## 5. Assessment

**Does name matching recover enough recall to justify an over-approximation layer?** Partially, and
selectively. It **does** recover a class of edges a precise engine soundly drops — **`external`
(imports into un-analyzed modules)** and **method-dispatch fan-out** — which is real FR-14 value: on the
categories where mypy emits nothing, name-match emits *something*. But it **does not** recover the
**value-flow** edges (callables through dicts/kwargs/lists/returns), because those are invisible to a
syntactic matcher; recovering those needs the very type/flow analysis name-match forgoes. So the layer
is a **complement for method-dispatch + external gaps**, not a general recall recovery.

**Is the fanout manageable, or does it need receiver-narrowing?** **It needs receiver-narrowing to be
usable.** Median fanout 3 is fine, but the 23× edge blow-up and the p99/max of 760 (common method
names) make un-narrowed slices unusable for the torture cases that dominate real code (`self.get`,
`super().__init__`). The obvious narrowing — restrict `x.m()` to classes compatible with `x`'s
static type — is exactly what a typed engine (mypy) supplies for free, which points at the hybrid: use
mypy's precise resolution where it fires, and fall back to **name matching narrowed by mypy's inferred
receiver type** where mypy soundly declines.

**What this probe CANNOT answer (out of scope — future work if the direction is approved):**
- It is **not a design.** Precision at *product* quality (the 21 micro FP / 10 `classes` FP are the
  naive-matching noise floor; a real layer would narrow) is unmeasured.
- **MRO handling** is stubbed (constructor = own `__init__` or nothing); a real layer needs C3.
- **Receiver-narrowing strategies** (static-type-compatible classes, subclass-closure, duck-typing
  bounds) are unimplemented — this probe only quantifies the *un-narrowed* cost that motivates them.
- **Union precision** with a precise engine (does mypy ∪ name-match keep precision acceptable, or does
  the fanout drown it?) is measured only on the micro suite (§2), not at Django scale, and only for the
  raw un-narrowed policy.

---

### Reproduction

```
PY=.venv-mypy/bin/python   # ast-only; any 3.13 works, .venv-mypy is convenient
# B-Q1  micro -> 84.1 / 42.0 (identical)      results/namematch-micro.json
PYTHONHASHSEED=0 $PY harness/run_eval.py micro --engine namematch --out results/namematch-micro.json
# B-Q2/B-Q3  fanout + timing + determinism    results/namematch-scale.json
$PY harness/namematch_scale.py --out results/namematch-scale.json
```

Code: `harness/namematch.py` (indexes + resolver, ~200 lines), `harness/namematch_scale.py` (fanout +
timing). Registered in `run_eval.py::get_resolver` under `namematch`.
