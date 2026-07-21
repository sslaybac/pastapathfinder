# FINDINGS — Session 3: Pyright as a Call-Graph Engine

Date: 2026-07-17
Scope: `prototypes/engine-eval/`. Throwaway prototype. No product code, `specs/`, or `docs/` touched.
Engine under test: **Pyright 1.1.411** (npm package bundled by the `pyright` pip shim), driven over
LSP (`pyright-langserver --stdio`) from CPython 3.13.2, running on **system Node v16.20.2**.

**Headline.** Pyright's native incrementality — the property expected to be decisive for this whole
evaluation — **does NOT rescue the incremental budget (Q3/FR-30).** Re-resolving the 5-file change
set + direct importers (279 files / 25,896 sites) takes **283 s warm** (328 s cold; **220 s warm +
pipelined**, an optimistic lower bound) against a **≤ 30 s** criterion — *slower* than Jedi (125 s
warm) — because a call graph over LSP needs **one type-evaluating `textDocument/definition` per call
site** (~8.5 ms each, even warm), and the server's fast incremental re-*analysis* does nothing for
the client's need to re-*query* every affected site. **Both engines fail FR-30** (requirements
revision trigger). Where Pyright is strong: it is **deterministic on every axis including the
server-state probe** (Q4 ✅, strictly better than Jedi's default-seed `content-differs`), never
executes analyzed code (FR-13 ✅), and is **high-precision** (94.5 %). But its **recall is 58.7 %**
(below Jedi's 77.6 %) because LSP `definition` resolves *where a name is declared*, not *what an
expression evaluates to* — so value-flow calls (dict/list/kwarg/return-of-return) go unresolved. The
adapter is **881 lines** vs Jedi's ~500.

---

## 1. What was tested

| item | value |
|---|---|
| Pyright version | **1.1.411** (`pyright --version`) |
| Install / node provenance | `pip install pyright` → **pip shim** `pyright==1.1.411` (pure-Python), deps **`nodeenv`, `typing-extensions`**. The shim carries the **bundled npm package** at `.venv-pyright/.../site-packages/pyright/dist/dist/pyright-langserver.js`; it runs it with **system node** by default (`PYRIGHT_PYTHON_GLOBAL_NODE=True`). No separate node was downloaded. **System node is v16.20.2** (old; current Pyright targets Node ≥18) — the langserver nonetheless initialized and answered `textDocument/definition` under it (handshake 0.43 s). Recorded because the product's install story inherits this Python-shim-over-node dependency chain. |
| Harness Python | uv CPython **3.13.2** (never system 3.9), per FINDINGS-harness §5 |
| Integration path | **LSP** (not CLI). `pyright-langserver --stdio`, hand-rolled JSON-RPC client |
| Machine | Intel **i7-4700HQ** @ 2.40 GHz, 4C/8T, 23 GB RAM, Linux 5.14, CPython 3.13.2 — **matches the session-1/2 reference machine**, so all timings are directly cross-session comparable |
| Django target | `benchmarks/django/django` (the `django/` subdir), 908 files / 37,218 sites on 3.13 |
| Harness changes | none to scoring core / determinism classifier / `per_site` key. Added: `run_eval.py::get_resolver` `pyright` branch; new adapter + probes (below). Inherited session 2's `(file,line,col,callee_expr)` `per_site` key unchanged. |

**New code** (all under `harness/`): `adapters/pyright_adapter.py`, `adapters/lsp_client.py`,
`pyright_incremental.py` (Q3), `pyright_q4.py` (Q4), `pyright_route_probe.py` (Q5),
`pyright_fr13.py` (FR-13), `q6_diamond.py` + `benchmarks/diamond-mro/` (Q6).

**Positions.** Pyright does **not** honor a `positionEncoding: utf-8` request — it echoes back no
`positionEncoding` in `initialize`, i.e. LSP-default **UTF-16 code units**. `ast` emits UTF-8 **byte**
offsets. The adapter converts byte→UTF-16 (`_utf16_col`) on the query side and char→UTF-16 on the
definition-index side; ASCII is the fast path where byte==char==utf16.

---

## 2. Integration cost report (required)

**Plumbing rounds.** Handshake→first `definition` worked on **round 1** (well under the 6-round
budget): one smoke test brought up the langserver under node 16, negotiated capabilities, and
resolved a definition. Two follow-ups de-risked the hard cases before the adapter was written
(cross-file, method, constructor-via-synthetic-append, builtin/typeshed). **One real bug cost a
round after first light:** Pyright issues a server→client `workspace/configuration` **request**
(with an id) and **stalls its background analysis until answered**; the first adapter run hung
because the client filed it as a notification and never replied. Fix: answer server→client requests
(config → per-section settings, everything else → `null`).

**What broke / traps found (all Pyright-specific, none shared with Jedi):**

1. **`definition` returns a LOCATION, not a name.** This is the whole adapter. Pyright answers
   `textDocument/definition` with a file+range; turning that into a ground-truth qualified name needs
   a **location→qname map** (`_DefIndex`): parse the target file (source OR `.pyi`), record each
   def/class/lambda/assign **identifier position → qname**, look up the returned `range.start`. Jedi
   handed back `.full_name` for free; this is ~250 lines that Jedi simply did not need.
2. **`definition` does NOT follow local aliases.** `a = func; a()` → definition of `a` stops at the
   assignment target `a`, not `to_import.func` (all of `definition`/`typeDefinition`/`declaration`
   do the same). The adapter chases the RHS by re-querying definition at the assignment's value
   (depth-capped) — Pyright doing the work, iterated. Jedi's `.infer()` followed this in one call.
3. **`definition` on `C()` returns the CLASS, not `__init__`.** Recovered MRO-correctly by a
   **synthetic `C.__init__` append** (didChange overlay on C's own module + a definition query);
   Pyright resolves the attribute through the MRO to the definition site (verified: a `Derived()`
   with an inherited `__init__` lands on `Base.__init__`). This is the Pyright analog of Jedi's
   `_constructor_qname`, and — unlike Jedi — it is **C3-correct** because Pyright owns the MRO.
4. **Builtin/typeshed classes must skip the constructor path** and normalize to their sentinel
   directly (`super()` → `<builtin>.super`, no `.__init__`), or the object.__init__ stub-drop
   silently eats the edge.
5. **Server→client request handling** (trap above) — a class of bug Jedi (in-process) can't have.
6. **UTF-16 positions** — Pyright is worse than Jedi here: Jedi wanted char offsets; Pyright wants
   UTF-16 and won't negotiate utf-8.

**Ported from `jedi_adapter.py` verbatim/near-verbatim** (harness/ground-truth concerns, credited to
neither engine): `_ScopeIndex` (syntactic caller qnames + lambda counter), the normalization map
(`_normalize_qname`, builtin sentinels), `sys_path_root`/`_self_imports` (workspace rooting at the
parent of `django/`), byte-column conversion.

**Size.** `pyright_adapter.py` **704 lines + `lsp_client.py` 177 = 881 lines**, vs Jedi's **~500**
and PyCG's ~120. ~75% larger than Jedi's. Of the delta, ~180 lines are the raw LSP transport and
~250 are location→qname + alias-chasing + constructor-overlay + typeshed-module-naming — i.e. the
cost is almost entirely "the engine speaks locations, the scorer speaks names." A production adapter
would be **larger still**: it must own LSP lifecycle/crash-recovery, config negotiation, and
document-sync bookkeeping that a throwaway can skip.

---

## 3. Normalization map — rules applied, deltas from session 2

Started from FINDINGS-jedi §2 and kept every rule; each is a pure renaming, none loosens the scorer.

| rule | status vs session 2 |
|---|---|
| builtin *types* → `<**PyXxx**>` sentinel (9 types) | **same** (`_normalize_qname`, ported) |
| builtin funcs/classes → `<builtin>.x`, no `.__init__` | **same**; needed an added guard so typeshed *classes* (e.g. `super`) take this path instead of the constructor path |
| constructor `C()` → `__init__` through the MRO to the def site | **same intent**, different mechanism: Pyright synthetic-`.__init__`-append overlay instead of Jedi's inferred attribute access. **C3-correct** (Pyright owns MRO) vs Jedi's depth-first (§Q6) |
| typeshed / `.pyi` stub target → dropped (not a def site) | **same**, and it fires MUCH more often — Pyright is typeshed-driven, so every stdlib/builtin call lands in a `.pyi` |
| lambda → `<scope>.<lambdaN>` per-scope counter | **same** (ported `_ScopeIndex`) |
| definition-site-not-alias | **same intent**; NEW mechanism: alias RHS-chasing, because Pyright's `definition` stops at the local alias (§2.2) where Jedi's `.infer()` did not |

**Delta summary:** no new sentinel conventions; two new *mechanisms* forced by Pyright answering with
locations instead of inferred values (alias-chasing, constructor-overlay), and one new *guard*
(typeshed classes → sentinel, not constructor).

---

## 4. Results

Q3 and Q4 are the headline (the decisive properties for this evaluation), then Q2/Q1/Q5/Q6 and the
FR-13 safety check. Each result is stated against its criterion and side-by-side with Jedi where a
session-2 number exists. `None` precision/recall is never rendered as 0; category rows carry case
counts; the node subprocess's memory is labeled as such (not comparable to Jedi's in-process figure).

### Q3 — Incremental re-resolution (FR-30 proxy) — DECISIVE — ❌ FAIL (and incrementality does NOT rescue it)

Change set identical to session 2: 5 Django-core files + their direct importers =
**279 files / 25,896 call sites** (`core.exceptions` alone has 162 direct importers). "Direct
importers" under-counts true dependents, so this is a **lower bound** on real re-analysis cost.
Criterion: ≤ 30 s.

| condition | wall time | what it measures | vs 30 s |
|---|---:|---|---|
| **cold-server** | **328.8 s** | fresh process: index 29.9 s + **query 298.4 s** | ❌ 11.0× |
| **warm-server (sequential)** | **283.6 s** | server pre-indexed (warmup 310.5 s), 5 files really changed (`didChange`+`didSave`), then re-query the 279-file set with `reopen=False`; query 282.6 s | ❌ 9.5× |
| **warm-server (PIPELINED, 256 in-flight)** | **219.8 s** | same, but definition requests pipelined instead of blocking — an **optimistic lower bound** (excludes qname-mapping + constructor overlays) | ❌ 7.3× |

Jedi's numbers to beat: **255 s cold / 125 s warm.** Pyright is **slower than Jedi on this axis**,
and — the decisive point — **its native incrementality does not save it.** All three variants
produce the same 16,141 edges with **0 infer errors** (no failure-masking, unlike Jedi's parso bug).

**Why incrementality doesn't help.** The cost is NOT indexing (a warm/incremental re-check of 5
changed files is fast — index is only ~30–40 s cold and near-free warm). The cost is the **query
phase**: producing a call graph over LSP requires **one `textDocument/definition` per call site**,
and each definition triggers **on-demand type evaluation** — measured at **~8.5 ms/query even warm
and pipelined**. 25,896 sites × ~8.5 ms ≈ 220 s. Pyright does not expose a prebuilt whole-program
symbol/reference graph you can read cheaply; every edge is a fresh type-eval. Incrementality makes
the server re-*analyze* the change fast, but the client must still re-*query* every site in the
affected set, and that is the wall. **Pipelining (the fair, product-realistic optimization) cut only
22 %** (283→220 s), so the failure is not a client-latency artifact.

**Memory (obligation 5):** the node server's RSS was observed at **~0.9–1.1 GB** during the warm
279-file run (via `ps`, sampled). The `/proc/<pid>/status` VmHWM read in the harness returned a
bogus 23 MB (read raced the server's shutdown); use the observed RSS, labeled as node-subprocess RSS
(not comparable to Jedi's in-process getrusage number).

### Q4 — Determinism (FR-44) + server-state probe — ✅ PASS (identical under both seeds)

Target: `django/forms` (9 files, 1,350 sites, **958 edges**). Criterion: `identical`/`ordering-only`.

| condition | classification | detail |
|---|---|---|
| two runs, **default (random) hash seed** | **identical** | 958 = 958 edges |
| two runs, **`PYTHONHASHSEED=0`** | **identical** | 958 = 958 |
| **server-state probe** — fresh server vs a server that already processed the project | **identical** | 958 = 958, both seeds |
| server-state probe — warmed server 1st query vs 2nd query | **identical** | — |
| micro suite (weak check, 60 cases) | **identical** | — |

**Pyright is deterministic on every axis measured, including the server-state probe** — the axis
the brief flagged as the live concern for a long-lived server, and the one that mattered because
Pyright's nondeterminism sources are internal to Node (the harness hash seed is irrelevant to it, as
the default-seed = `PYTHONHASHSEED=0` result confirms). **This is strictly better than Jedi**, which
was `content-differs` under a default seed and needed `PYTHONHASHSEED=0` pinned to be reproducible.

**Render canary.** The `widgets.Media.render` ambiguity that flipped `render_js`↔`render_css` for
Jedi does **not** arise for Pyright: `widgets.py:171 self.render` resolves to a single stable
candidate `widgets.Media.render`, byte-identical across fresh and fully-warmed servers. (Pyright
resolves `self.render` to the *method being named*, not to what it dispatches to — a different, and
for determinism cleaner, resolution than Jedi's.) `render_canary.stable = true` under both seeds.

### Q2 — Full-run wall time (FR-29 proxy) — ✅ PASS (time), and a COMPLETE graph

All 37,218 sites in `benchmarks/django/django` (908 files, 0 parse failures on 3.13).

| metric | Pyright | Jedi (session 2) |
|---|---|---|
| **resolve wall time** | **477.9 s (7.96 min)** — criterion ≤ 10 min ✅ | 230.7 s (3.84 min) ✅ |
| index / open+settle | 48.5 s | — |
| query | 428.8 s | — |
| discovery+parse | 2.2 s | 2.0 s |
| edges produced | **22,889** | 15,553 |
| unresolved sites | 8,115 (21.8 %) | 17,500 |
| **infer / query failures** | **0** ✅ | **11,129 (29.9 %)** ⚠ (parso cache bug) |
| node RSS (observed) | ~1.0 GB | 332 MB (in-proc floor) |

Pyright is **~2× slower** than Jedi here but produces a **complete, un-degraded graph**: **zero query
failures**, 22,889 edges vs Jedi's 15,553 (Jedi lost ~30 % of Django inferences to the parso
cache-eviction bug). So the honest both-axes read: Jedi is faster on paper but ships a graph with a
third of its inferences missing at scale; Pyright is slower but the graph it produces is whole. The
8,115 unresolved sites are the value-flow/dynamic/external categories from Q1, not failures — the
adapter recorded them as empty candidate lists (FR-14 evidence), with 0 errors.

### FR-13 — Pyright never imports or executes analyzed code — ✅ PASS

`benchmarks/fr13-probe` (module body + `boom()` + `Detonator.__init__` each write a witness file on
execution). The adapter resolved all 12 call sites (12 edges, incl.
`main → sideeffect.Detonator.__init__`, `sideeffect.boom → <builtin>.open`); the **witness file was
never created**, and the analyzed modules never entered this process's `sys.modules`. Pyright runs
**out-of-process in a node subprocess** and analyzes from its own parser — an even cleaner FR-13
story than Jedi's in-process (but non-importing) model. One confirming run, as specified.

### Q1 — Resolution quality (PyCG micro, 119 cases / 264 GT edges)

**Micro-averaged: precision 94.5% (155 TP / 9 FP), recall 58.7% (109 FN).**
Comparison: **Jedi 97.2% / 77.6%**; PyCG (self-benchmark ceiling) 97.6% / 93.2%.

| category | cases | TP | FP | FN | precision | recall |
|---|---:|---:|---:|---:|---:|---:|
| args | 6 | 7 | 0 | 7 | 100.0% | 50.0% |
| assignments | 4 | 13 | 0 | 2 | 100.0% | 86.7% |
| builtins | 3 | 6 | 0 | 4 | 100.0% | 60.0% |
| classes | 22 | 42 | 2 | 10 | 95.5% | 80.8% |
| **decorators** | 7 | 8 | 1 | 14 | 88.9% | **36.4%** ⚠ |
| **dicts** | 12 | 5 | 1 | 14 | 83.3% | **26.3%** ⚠ |
| **direct_calls** | 4 | 4 | 0 | 6 | 100.0% | **40.0%** ⚠ |
| **dynamic** | 1 | 0 | 1 | 2 | 0.0% | **0.0%** ⚠ |
| **exceptions** | 3 | 0 | 0 | 3 | n/a | **0.0%** ⚠ |
| **external** | 6 | 2 | 0 | 9 | 100.0% | **18.2%** ⚠ |
| functions | 4 | 4 | 0 | 0 | 100.0% | 100.0% |
| **generators** | 6 | 8 | 0 | 10 | 100.0% | **44.4%** ⚠ |
| imports | 14 | 14 | 0 | 0 | 100.0% | 100.0% |
| **kwargs** | 3 | 3 | 0 | 7 | 100.0% | **30.0%** ⚠ |
| lambdas | 5 | 7 | 1 | 7 | 87.5% | 50.0% |
| **lists** | 8 | 8 | 0 | 8 | 100.0% | **50.0%** ⚠ |
| mro | 7 | 16 | 0 | 2 | 100.0% | 88.9% |
| returns | 4 | 8 | 3 | 4 | 72.7% | 66.7% |
| **TOTAL** | **119** | **155** | **9** | **109** | **94.5%** | **58.7%** |

**Shape.** Like Jedi, Pyright is **high-precision / recall-limited**: 9 FP total, precision ≥ 83% in
every category that emits anything. But recall (58.7%) is **below Jedi's 77.6%**, and the gap is
structural: **Pyright's LSP `definition` is a "go to where this name is declared" primitive, not a
"what does this expression evaluate to" inference primitive.** Every low-recall category above is a
value-flow case `definition` cannot follow:

- **dicts (26%), lists (50%), kwargs (30%), generators (44%), direct_calls (40%)** — calling a
  callable that was *stored in / yielded from a collection*, *passed as a keyword arg*, or is the
  *return value of a call* (`func()()`). `definition` resolves the name being called, not the value
  it will hold. **Jedi's `.infer()` did follow these** (Jedi: dicts 84%, generators 56%, returns
  100%), so this is a category where Jedi's inference model is genuinely more capable.
- **decorators (36%)** — same as Jedi (31.8%): the wrapper→wrapped call edges are never formed.
- **external (18%)** — same hazard as Jedi and as warned: Pyright resolves into a typeshed `.pyi`
  stub, which is not the definition site GT names; the adapter drops stubs, so no edge. **Confirmed:
  typeshed-centricity hurts `external`, exactly the risk flagged.**
- **exceptions (0%), dynamic (0%)** — same inherent static limits as Jedi.

Categories where Pyright **matches or beats** Jedi: `classes` (80.8% vs 92.3% — slightly worse),
**`mro` 88.9%** (Jedi 83.3%) and it resolves `super()` cooperative chains correctly (a category PyCG
*missed*), `assignments` 86.7%, `imports`/`functions` 100%. All FP/FN were checked to be genuine
resolution behavior, not naming mismatches (two normalization rounds; §3).

### Q5 — Framework routes (FR-11) — routes detected across all three; ≈ Jedi, with two differences

Fixtures venv `.venv-pyright-fixtures` (Flask 3.1.3, FastAPI 0.139.2, Django 6.0.7, same as session
2), passed to pyright as `python.pythonPath`. AST supplies decorator/arg shape + literal values;
Pyright supplies resolution (route-decorator identity, view target). Only the latter is credited.

| fixture | routes detected | Pyright identified as route | rules recovered | negative control |
|---|---|---|---|---|
| flask_app | 5 / 5 | 5 / 5 (`…Scaffold.route`/`.post`) | 3 / 5 literal | `not_a_route` **not** flagged ✅ |
| fastapi_app | 6 / 6 | 6 / 6 (`FastAPI.get/post` **vs** `APIRouter.get/delete`) | 3 / 6 literal | `helper` **not** flagged ✅ |
| django_app | 2 / 2 static | — (URLconf, not decorators) | 2 / 2 literal | `unreferenced` **not** flagged ✅ |

Matches Jedi's profile: all decorator routes identified; FastAPI `app` distinguished from
`APIRouter`; variable rule strings (`DETAIL_RULE`, `PREFIX+…`, `VERSION_PREFIX+…`, router-prefix
composition) not recovered (correctly — AST's job); zero controls mis-flagged. Two deltas vs Jedi at
the session-2 calibration points:

- **(b) `path`/`re_path` — Pyright is BETTER.** These are `functools.partial` bindings; Jedi's
  `infer()` returned nothing and needed `goto()`. Pyright's `textDocument/definition` resolves `path`
  straight to **`django.urls.conf`** (with `typeDefinition` → `functools`), no fallback query needed.
- **(a) `FooView.as_view()` — Pyright is slightly WORSE / same limit.** Querying the `as_view()` call
  returned **nothing** (Jedi got `typing.Callable`). The concrete view class is not recovered by
  either engine — a typeshed artifact (`as_view()` → `Callable`); the route is still detected and its
  rule matches.
- **(c) variable rule strings** → reported non-literal (`rule=False`), same as Jedi.

**MODE of the expected static miss** (`django_app`, loop-appended `reports/*`): **over-approximated,
not silent** — the `path()` at `urls.py:17` is surfaced, its view **resolves correctly to
`views.legacy_report`**, and only the `'reports/%s' % _kind` rule is (correctly) left unresolved.
This matches Jedi and is the desirable FR-14 behavior. As with Jedi, the "this is dynamic" signal is
an **AST** observation (`rule_literal is None`, `inside_for_loop=True`), not a Pyright one.

### Q6 — Diamond-MRO (optional) — Pyright C3-CORRECT, Jedi depth-first WRONG

Fixture `benchmarks/diamond-mro/`: `A` and `C` define `__init__`, `B`/`D` do not; `D(B, C)`,
`B(A)`, `C(A)`. C3 MRO of `D` is `[D, B, C, A]` → `D()` reaches **`C.__init__`**; a naive depth-first
walk `[D, B, A, C]` reaches `A.__init__`. Ground truth = the C3 target. Run through both adapters:

| engine | `make() → D()` resolves to | verdict |
|---|---|---|
| **Pyright 1.1.411** | `main.C.__init__` | ✅ **C3-correct** |
| **Jedi 0.20.0** | `main.A.__init__` | ❌ depth-first (FP + FN) |

This **confirms session 2's latent-bug hypothesis (FINDINGS-jedi §5.4)**: the micro suite never
triggers it, but a diamond whose C3 order flips the target does — and Jedi mis-linearizes while
Pyright, which owns a real C3 MRO, resolves correctly. Concrete evidence that Pyright's
class-resolution is sounder at scale (Django's deep form/model/admin hierarchies).

## 5. Failure modes

Naming/plumbing failures (fixable, and fixed) are separated from genuine resolution limits.

1. **`definition` ≠ inference (the big recall one).** Value-flow calls (collection-stored,
   kwarg-passed, return-of-return) are unresolved because `textDocument/definition` names symbols,
   not expression values. Structural to the LSP primitive; not tunable in the adapter without
   reimplementing inference. Counts: the entire recall gap vs Jedi (§Q1) is this.
2. **Typeshed stub-drop on `external`** — resolves to `.pyi`, dropped as non-definition-site (18%
   recall). Naming-vs-resolution boundary is clean here: it's a *definition-site* mismatch, correctly
   not counted as a naming failure.
3. **Alias/tuple/lambda-assignment needed explicit adapter chasing** (§2.2, fixed) — without it,
   recall on `assignments`/`lambdas` collapsed and self-loop FPs appeared. A **naming/plumbing**
   failure that WAS fixed, separated here from the genuine (1)/(2) resolution limits. (Before the
   fix: 79.7 %/51.9 %; after: 94.5 %/58.7 %.)
4. **Per-site query cost at scale (the FR-30 killer, §Q3).** Not a bug — a structural property: one
   type-evaluating `definition` per call site, ~8.5–11.5 ms each, unchanged by warmth. 25,896 sites →
   ~220–300 s. This is the dominant scale mode and it is why FR-30 fails.
5. **Adapter plumbing bug found the hard way: stdin write race.** Answering pyright's server→client
   `workspace/configuration` request from the reader thread raced the main thread's request writes on
   an unlocked stdin, corrupting the JSON-RPC framing and **deadlocking** a warm run (~1h44m hang, CPU
   idle, before I killed it). Fixed with a write lock. A production LSP adapter MUST get concurrency
   right — this class of bug does not exist for an in-process engine like Jedi. Recorded as an
   integration-cost trap, not an engine property.
6. **`positionEncoding` not negotiable** — pyright ignores a utf-8 request and uses UTF-16; wrong
   column math silently resolves the wrong token. Fixed by byte→UTF-16 conversion.

## 6. Recommendation

**Pyright is NOT viable as the sole engine for a whole-program call-graph product if FR-30 (≤ 30 s
incremental re-resolution) is a hard requirement — and its native incrementality, the property this
evaluation was built to test, does not change that.** It is viable-with-caveats for everything else,
and it is the better engine on safety, determinism, completeness, and class-resolution correctness.

**Per-criterion, head-to-head with Jedi:**

| criterion | Jedi | Pyright | favors |
|---|---|---|---|
| **FR-30** incremental ≤ 30 s | 125 s warm ❌ | **283 s warm / 220 s warm+pipelined ❌** | **neither — BOTH FAIL** |
| **FR-44** determinism | `content-differs` by default; needs `PYTHONHASHSEED=0` | **identical on all axes incl. server-state probe** | **Pyright** |
| **FR-29** full run ≤ 10 min | 3.84 min but **29.9 % infer failures** | 7.96 min, **0 failures**, +47 % more edges | **Pyright** (complete graph) |
| **FR-13** no execution | pass (in-process) | pass (**out-of-process** node) | ~tie (Pyright cleaner) |
| **FR-12/14** resolution quality | 97.2 % / **77.6 %** | 94.5 % / **58.7 %** | **Jedi** (recall; value-flow) |
| **FR-11** routes | detected; controls clean | detected; controls clean; better on `partial` | ~tie |
| MRO correctness (Q6) | depth-first **WRONG** | **C3-correct** | **Pyright** |
| adapter complexity | ~500 lines | **881 lines** + LSP concurrency traps | **Jedi** |

**The decisive finding, stated plainly: both Jedi and Pyright fail FR-30's ≤ 30 s criterion**
(Jedi 125 s, Pyright 283 s, warm). Per the brief, this **fires the recorded requirements-revision
trigger** and must not be softened. The two engines fail for *different* reasons, and the difference
is the important part:

- **Jedi fails because it has no incrementality** — it re-infers everything from scratch.
- **Pyright fails despite HAVING incrementality** — because the incrementality is in the *server's
  re-analysis*, while the cost is in the *client's re-query*: producing a call graph over the LSP
  `definition` interface is O(call sites) type-evaluating round-trips, and there is no cheaper bulk
  path exposed. Pyright's binder *does* build an internal symbol/reference graph, but LSP does not
  surface it; you can only ask one position at a time.

**This strengthens session 2's closing suggestion** (whole-program or hybrid instead of either engine
alone) — and sharpens it. The evidence says the bottleneck is not "which engine infers better" but
**the per-call-site query interface itself**. Any design that re-derives the graph one site at a time
(Jedi `infer`, Pyright `definition`) will lose to a 30 s budget on a 25 k-site change set. A viable
path is a **whole-program engine that materializes and *incrementally updates* a reference/call graph
you can read in bulk** — which is closer to Pyright's *internal* binder than to anything on its LSP
surface. Concretely: (a) drive Pyright's type engine through its internal/programmatic API rather
than LSP `definition`, so a change invalidates and recomputes only affected edges in the server's own
graph; or (b) a fast syntactic call-graph for the bulk, with a precise engine consulted only for the
hard minority of sites. **A pure per-position LSP adapter over Pyright is not the answer for FR-30.**

If FR-30 is **relaxed** (e.g., editor-scale single-file queries, or a batch/CI call graph without a
tight incremental budget), Pyright becomes attractive: deterministic, safe, complete at scale, and
MRO-correct, at the cost of a larger, concurrency-sensitive adapter and lower recall on value-flow
calls than Jedi. For a batch whole-program graph (FR-29 only), Pyright's completeness (0 failures vs
Jedi's 30 %) likely makes it the better choice despite being ~2× slower.

---

### Reproduction

```
PY=.venv-pyright/bin/python
# Q1  micro  -> 94.5% / 58.7%   (results/pyright-micro.json)   [fast scorer, no determinism phase]
$PY harness/pyright_micro_score.py --out results/pyright-micro.json
#   full run incl. determinism weak-check: PYTHONHASHSEED=0 $PY harness/run_eval.py micro --engine pyright
# Q2  scale  -> 7.96 min, 0 failures, 22,889 edges   (results/pyright-scale.json)
PYTHONHASHSEED=0 $PY harness/run_eval.py scale --engine pyright --root benchmarks/django/django
# Q3  incremental -> cold 329s / warm 284s   (results/pyright-incremental.json)
PYTHONHASHSEED=0 $PY harness/pyright_incremental.py --measure both --out results/pyright-incremental.json
#   pipelined warm lower bound -> 220s   (results/pyright-incremental-pipelined.json)
PYTHONHASHSEED=0 $PY harness/pyright_q3_pipeline.py --window 256
# Q4  determinism -> identical (both seeds)
PYTHONHASHSEED=0 $PY harness/pyright_q4.py --out results/pyright-determinism.json            # seed=0
$PY harness/pyright_q4.py --out results/pyright-determinism-defaultseed.json                 # default seed
# Q5  routes (needs .venv-pyright-fixtures)     $PY harness/pyright_route_probe.py
# Q6  diamond MRO   $PY harness/q6_diamond.py --engine pyright ; .venv-jedi/bin/python harness/q6_diamond.py --engine jedi
# FR-13             $PY harness/pyright_fr13.py
```

Adapter/probes: `harness/adapters/{pyright_adapter,lsp_client}.py`,
`harness/pyright_{incremental,q4,q3_pipeline,route_probe,fr13,micro_score}.py`,
`harness/q6_diamond.py`, `benchmarks/diamond-mro/`. Node stderr is quiet by default
(`PyrightServer(log_stderr=True)` to see it). Env: `.venv-pyright` (pyright 1.1.411),
`.venv-pyright-fixtures` (+ flask/fastapi/django) for Q5 only.
