# `tests/regression/` — determinism gate and benchmark regression suite

Development tooling, not shipped product. Two things live here:

| file | what it is | task |
|---|---|---|
| `compare.py` | the FR-44 comparator (volatile fields stripped; in-variance-class diffs reported, never silently passed) | 4.3 |
| `test_compare.py`, `test_determinism.py` | the determinism gate over a fixture tree | 4.3 |
| `benchmarks.py` | the pinned benchmark codebases: pins, checkout resolution, fetch-by-hash | 4.4 |
| `test_benchmarks.py` | FR-29, FR-30, AC-29.3 and FR-44-at-scale on the pinned trees | 4.4 |

Governing documents: requirements §4.8 (reference machine, benchmark designations), FR-29,
FR-30, FR-44; design.md §8-O5 (the pins), D1, D1a (engine-upgrade policy), §3.5 (the
enumerated mypy internals reproduced below).

---

## 1. Benchmark pins (design.md §8-O5)

Copied verbatim from `specs/tasks.md`'s "Benchmark pins" block, which copied them from the
findings. `benchmarks.py` holds the same values in code and
`test_benchmarks.py::test_the_readme_publishes_the_pins_verbatim` asserts the two agree —
so a pin can never be bumped in one place only.

- **Django core** — `github.com/django/django`, commit
  **`274df4df0bca7fcfb5c1c1d49567f770df147eeb`**; analyze the **`django/` package
  subdirectory**, not the repo root (908 `.py` files, 131,294 code lines; the repo root is
  ~4× larger and would misrepresent FR-29). Source:
  `prototypes/engine-eval/FINDINGS-harness.md` §2.
- **pandas** — `github.com/pandas-dev/pandas`, commit
  **`f6df82f9d0bdba793cbe34251f57c5d6e3fe804c`**; analyze the **`pandas/` package
  directory** (1,418 `.py` files, 664,190 LOC). Source:
  `prototypes/engine-eval/FINDINGS-session5.md` Part 2.

Requirements §4.8 designates what each one is for, and the designations are not
interchangeable: **Django core is the performance reference** — FR-29's 600 s bound and
FR-30's 30 s bound are asserted against it. **pandas is the dynamism and robustness
reference** — run-to-completion and artifact correctness are asserted against it and the
time bound deliberately is not (AC-29.3).

**Reference machine** (requirements §4.8, resolving C-3/C-7): a 4-core / 8-thread x86-64
CPU (reference: Intel Core i7-4700HQ), 24 GB RAM, SATA SSD, on an enterprise-Linux-class
distribution (reference: AlmaLinux 9). Stated as a class with a concrete reference:
equivalent-or-better hardware satisfies it, and a bound met here holds a fortiori on newer
hardware. Timings taken anywhere else are informative, not conformance.

## 2. Getting the trees

```sh
python tests/regression/benchmarks.py            # both, into tests/regression/.benchmarks/
python tests/regression/benchmarks.py django     # just one
python tests/regression/benchmarks.py --dest /srv/benchmarks
```

The fetch is **by hash**: `git fetch --depth 1 <url> <sha>` retrieves exactly the pinned
commit — no branch, no tags, no history — so a checkout cannot drift with upstream.

Already have them? Point an environment variable at what you have, and skip the fetch:

| variable | meaning |
|---|---|
| `PASTAPATHFINDER_BENCHMARKS` | a directory containing `django/` and `pandas/` checkouts |
| `PASTAPATHFINDER_DJANGO_BENCHMARK` | that one checkout (repo root or the package inside it) |
| `PASTAPATHFINDER_PANDAS_BENCHMARK` | likewise |

Either way the commit is verified against the pin before anything is measured. A checkout
at a different commit **stops** the measurement rather than colouring it: every number this
suite asserts is a statement about one tree at one commit, and against a different one it
is neither a pass nor a failure, just noise.

## 3. Running the suite

The benchmark tests are marked `slow` and `pyproject.toml` excludes that marker from the
default run, so `pytest` stays a seconds-scale developer loop:

```sh
pytest                                              # the normal suite; benchmarks excluded
pytest tests/regression/test_benchmarks.py -m slow -s   # the benchmarks (~5 min)
pytest tests/regression -m "slow or not slow"           # determinism gate + benchmarks
```

`-s` is worth having: every test prints its measurement next to the prototype reference it
should resemble, whether or not the assertion holds. An outcome inside the bound but far
off the reference is a signal to read, not a pass to wave through.

Without a pinned checkout each test **skips**, with the command that would produce one.

| test | asserts | reference |
|---|---|---|
| `test_django_full_analysis_is_within_the_fr29_bound` | AC-29.1: ≤ 600 s, index + all six reports, 908 files, 0 parse failures | ~11.3 s resolve, 390 MB RSS (`FINDINGS-mypy.md` Q2) |
| `test_django_reanalysis_after_five_changed_files_is_within_the_fr30_bound` | AC-30.1: ≤ 30 s after 5 changed files; `reanalysis.json` attributes exactly those five `content_changed` and the rest `dependent` | 13.2 s core-change + ~2.0 s D18 detector pass; 2.7 s leaf-change (`FINDINGS-mypy.md` Q3, `FINDINGS-session5.md` Part 1) |
| `test_pandas_runs_to_completion` | AC-29.3: completes with every artifact, 1,418 analyzed, 0 skipped; **no time bound** | 53.3 s, 1,267 MB peak, 0 file casualties (`FINDINGS-session5.md` Part 2) |
| `test_pandas_libs_calls_resolve_through_the_shipped_stubs` | missing compiled `.so` files neither abort the build nor collapse `pandas._libs` to `Any` | ~3,747 edges into `pandas._libs.*` |
| `test_two_pandas_runs_are_deterministic` | FR-44: two runs compare `equal` or `in_variance_class`; anything else fails | the 3-of-88,228-edge locus, 0.003 % |

AC-29.2 is why every bound is asserted *after* the run rather than imposed as a timeout:
the bound is a performance requirement, and a run is never aborted to satisfy it.

The FR-30 test works on a **copy** of the pinned package — it edits five files, and the pin
must stay pristine.

## 4. D1a — the engine-upgrade revalidation procedure

design.md **D1a** (normative): `mypy==2.3.0` is pinned exactly, and upgrading it is a
deliberate maintenance act. The build API is semi-public — mypyc consumes it in-repo, and
nothing about it is stability-guaranteed (design.md R1) — so an upgrade is revalidated
against measurement, never against a green unit suite alone.

Run this sequence on a branch, in order. Each step is one command.

```sh
# 0. Branch, and bump the pin in pyproject.toml (exactly one place, still `==`).
git switch -c bump-mypy-<version>
$EDITOR pyproject.toml && pip install -e '.[dev]'

# 1. Micro-suite ground-truth scoring. The call-resolution fixtures are the ground truth:
#    each asserts a named resolution outcome (MRO diamond, ambiguity flags, chained calls,
#    constructor/external boundary, spans, the walker's coverage of the child map).
pytest tests/unit/test_extract.py tests/unit/test_extract_calls.py \
       tests/unit/test_externals.py tests/unit/test_normalize.py \
       tests/unit/test_mypy_driver.py tests/unit/test_python_adapter.py -q

# 2. The rest of the functional suite, including the AC-23.1 import-discipline guard.
pytest -q

# 3. Django timing against FR-29 and FR-30.
pytest tests/regression/test_benchmarks.py -m slow -s -k django

# 4. pandas run-to-completion (AC-29.3) and the `_libs` stub resolution.
pytest tests/regression/test_benchmarks.py -m slow -s -k "pandas and not deterministic"

# 5. Determinism: the fixture-scale gate and the pandas-scale double run (FR-44).
pytest tests/regression/test_determinism.py -q
pytest tests/regression/test_benchmarks.py -m slow -s -k deterministic

# 6. Record the result before merging (§5 below). An upgrade whose numbers were never
#    written down is an upgrade nobody can compare the next one against.
```

**Reading a failure.** Steps 1–2 localize a *semantic* break (the resolution ladder now
answers differently); step 3 localizes a *performance* break; step 5 localizes a
*determinism* break. A step-1 failure is the cheap one — the fixtures name the mechanism.
A pass at step 1 with a large recall shift visible in step 3's printed diagnostic counts is
the expensive one, and it means the upgrade changed what mypy resolves, not what we ask.

### The mypy internals this codebase touches (design.md §3.5, the upgrade checklist)

Reproduced verbatim so a failing upgrade localizes fast. All of it lives behind
`src/pastapathfinder/adapters/python/`, which is the only place allowed to import `mypy.*`
(AC-23.1, enforced by `tests/unit/test_import_discipline.py`).

- `mypy.build.build`
- `BuildSource`
- `Options`
- `BuildResult.graph` / `.types`
- `State.tree`
- the build manager's **rechecked-modules** report
- node types `MypyFile` / `CallExpr` / `NameExpr` / `MemberExpr` / `FuncDef` / `ClassDef` /
  `Decorator` / `LambdaExpr`
- `SymbolNode.fullname`
- `TypeInfo.get` / `.mro`
- `CallableType.definition`

Two of these are load-bearing in ways an upgrade can break silently, both already paid for
in `FINDINGS-mypy.md` §2 — reproduce them, don't rediscover them:

1. **`options.mypy_path` must be the build root** — the *parent* of the analyzed package —
   or sibling imports resolve to `Any` and recall drops with no error anywhere.
2. **The re-extraction set is the rechecked-modules report, never "which graph states carry
   a tree."** A warm build retains trees for cache-loaded modules that were never
   re-type-checked (430 observed) whose types are empty; re-extracting them silently drops
   their cached edges (D6 rule 1).

## 5. Recording a revalidation

Append the outcome to the upgrade's PR description or commit body — this README is not a
log, and `prototypes/` is append-only historical record for the engine evaluation, not a
home for maintenance runs. Record, at minimum:

- the mypy version moved from and to, and the machine the numbers were taken on;
- step 3's Django wall-clock (full and incremental) against 600 s / 30 s;
- step 4's pandas outcome: analyzed / skipped counts and the `pandas._libs` edge count;
- step 5's comparator verdict, including any in-variance-class warning and its fraction;
- anything that moved in the printed diagnostic counts, which is where a recall change
  shows up before it shows up anywhere else.
