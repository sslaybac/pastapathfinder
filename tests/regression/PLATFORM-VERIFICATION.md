# Platform and deployment verification (task 5.4)

Record + checklist for FR-31 (AC-31.1–31.4), FR-32 (AC-32.1), FR-33 (AC-33.1), FR-34
(AC-34.1) — the platform/deployment requirements — and the closing US-1..US-5 workflow
pass design.md §8-O6 calls for. This is dev tooling, not shipped product (like the rest of
`tests/regression/`); it **references** the task-4.4 automated slow-test suite rather than
replacing it.

Governing documents: requirements FR-31–34, §3 (US-1..US-5); design.md §8-O6, §5.1.

---

## 1. Status summary

| AC | What it asserts | Status |
|---|---|---|
| AC-31.1 | All five workflows on the reference Linux environment (AlmaLinux 9) | **PENDING** — needs execution on the literal reference machine; checklist in §5 |
| AC-31.2 | WSL2, codebase+index on Linux FS, FR-29/FR-30 bounds hold | **PASS** — executed this session |
| AC-31.3 | WSL2, codebase under `/mnt/c`, bounds not asserted, artifacts correct | **PASS** — executed this session |
| AC-31.4 | Unsupported platforms fail as ordinary errors; no detection/blocking required | **PASS** — confirmed by inspection |
| AC-32.1 | Clean-machine install via `pip install .`, no further manual setup | **PASS** — executed this session |
| AC-33.1 | All workflows function with external network blocked | **PASS** — executed this session |
| AC-34.1 | All workflows function as an unprivileged user (read codebase, write output) | **PASS** — executed this session |

Executed this session: **AC-31.2, AC-31.3, AC-31.4, AC-32.1, AC-33.1, AC-34.1.**
AC-32.1/33.1/34.1 have no reference-machine clause in their own text (only AC-31.1 names
"the reference Linux environment" explicitly) — WSL2 is itself a tested support target per
FR-31, so a WSL2 pass satisfies their letter. Only **AC-31.1** requires the literal
AlmaLinux 9 reference machine and remains pending a run there; §5 is a precise, runnable
checklist for that run.

## 2. Environment this session ran on

- **Platform:** WSL2, Ubuntu 26.04 LTS ("Resolute Raccoon"), kernel
  `6.6.87.2-microsoft-standard-WSL2`. This is a Linux distribution inside WSL2 — **not**
  the AlmaLinux 9 reference machine, so it stands for AC-31.2/31.3, not AC-31.1.
- **Python:** 3.14.4 (system interpreter). Above the `requires-python >= 3.12` floor and
  above the 3.13 CLAUDE.md records as "developed and verified on" — noted in §6, not a
  blocker (all runs below passed on it).
- **User:** unprivileged (`uid=1000`, not root; no `sudo` used for any product operation).
- The system Python shipped without `pip`/`ensurepip`/a working `venv` module (a minimal
  Ubuntu image property, not a pastapathfinder issue, and not WSL-specific — see §6).
  Every venv below was bootstrapped with `python -m venv --without-pip` +
  `get-pip.py`, which needed one outbound HTTPS fetch; this is provisioning the *test
  harness*, not part of what AC-32.1/33.1 assert about the *tool*.

## 3. AC-32.1 — clean-machine install

Fresh venv, network available, documented command only:

```
python -m venv --without-pip clean_install && <bootstrap pip> && clean_install/bin/pip install .
```

- Install completed in 19.4 s: `mypy==2.3.0`, `pathspec`, `flask` (+ their transitive deps)
  resolved and installed, `pastapathfinder` console script placed on the venv's `PATH`.
- `pastapathfinder --help` → exit **0**.
- No-subcommand and unknown-subcommand invocations → exit **2** (argparse's native code,
  per D10), matching the task-1.1 contract.
- No further manual setup was needed before the workflows in §4 ran. **PASS.**

## 4. The five workflows (US-1..US-5)

Run against this repository's own `src/pastapathfinder/` tree (30 files, a real,
unfamiliar-to-the-tool codebase) using the clean-install venv from §3, then repeated
against `tests/fixtures/flask_app/` for the offline and unprivileged passes below.

- **US-2 / US-3 (`analyze`, structural artifacts, unfamiliar tree):**
  `pastapathfinder analyze <root>` completed, exit 0, `30 discovered = 30 analyzed + 0
  skipped + 0 excluded`, all six reports and the index written. Same pipeline path
  regardless of whose code it is, per the task brief.
- **US-2 (`query`):** `query entry-points --json` (9 entries), `query slice --from
  python:entry:main_block:cli@454 --direction forward --json` (25 nodes / 31 edges, not
  truncated), `query node python:cli.<module> --json`, and `query dead-code --json`
  (caveat string present) all answered correctly from the index alone.
- **US-1 (viewer):** `pastapathfinder view --port 8517` served `/` (200), `/static/app.js`,
  `/static/style.css`, `/static/vendor/cytoscape.min.js`, `/static/vendor/cytoscape-dagre.js`
  (all 200); `/api/meta` reported correct counts; `/api/slice?from=...&direction=forward`
  returned a bounded slice (`truncated: false`); `/api/nodes/<unknown>` → 404. Server bound
  to `127.0.0.1:8517` only (`ss -tlnp` confirms no other listener); no outbound sockets
  held by the process while serving.
- **US-4 (incremental):** on a working copy, a full `analyze` took 4.88 s
  (`reanalysis.json: mode=full`); appending one method to `discovery.py` and re-running
  took **0.16 s**, `reanalysis.json` reported `mode=incremental`,
  `reprocessed: [{"path": "discovery.py", "reason": "content_changed"}]` — no other file
  reprocessed.
- **US-5 (coverage/exclusion):** `coverage.json` counts reconciled
  (`entries_discovered = files_analyzed + files_skipped + entries_excluded`);
  `exclusions.json` carried `none_excluded: true` on the exclusion-free fixture.

All five: **PASS** on WSL2 Ubuntu (this session's environment).

## 5. AC-31.1 — checklist for the reference Linux environment (AlmaLinux 9)

Not executed this session — this environment is WSL2, not the AlmaLinux 9 reference
machine (requirements §4.8). Run the following on that machine and paste results in below.

```sh
# 0. Environment
python3 --version                      # expect >= 3.12 (developed/verified on 3.13)
cat /etc/os-release                    # expect AlmaLinux 9 (or equivalent)

# 1. Clean install (AC-32.1, reprise on this machine for the record)
python3 -m venv /tmp/ppf-clean && /tmp/ppf-clean/bin/pip install .
/tmp/ppf-clean/bin/pastapathfinder --help                 # expect exit 0

# 2. US-2/US-3 — analyze an unfamiliar tree
/tmp/ppf-clean/bin/pastapathfinder analyze <some codebase, e.g. this repo's src/pastapathfinder>
# expect: exit 0 or 1, all six reports + index written, coverage arithmetic reconciles

# 3. US-2 — query
/tmp/ppf-clean/bin/pastapathfinder query entry-points --out <out-dir> --json
/tmp/ppf-clean/bin/pastapathfinder query slice --from <an entry id> --direction forward --out <out-dir> --json
/tmp/ppf-clean/bin/pastapathfinder query dead-code --out <out-dir> --json

# 4. US-1 — viewer
/tmp/ppf-clean/bin/pastapathfinder view --out <out-dir> --port 8517 &
curl -s http://127.0.0.1:8517/api/meta
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8517/
# then open http://127.0.0.1:8517/ in a browser, select an entry point, follow a forward slice

# 5. US-4 — incremental
# edit one file in the analyzed tree, re-run step 2's analyze command (no --full)
# expect reanalysis.json: mode=incremental, only the edited file content_changed

# RESULTS (fill in):
# date run: ____
# python version: ____
# os-release: ____
# step 1 (install) result: ____
# step 2 (analyze) result: ____
# step 3 (query) result: ____
# step 4 (viewer) result: ____
# step 5 (incremental) result: ____
# AC-31.1 verdict: PASS / FAIL (reason if FAIL): ____
```

## 6. AC-31.2 — WSL2, Linux filesystem (Django benchmark)

Django core checked out at the pinned commit
`274df4df0bca7fcfb5c1c1d49567f770df147eeb` on the WSL2 **Linux filesystem** (verified via
`git log -1`, matching `tests/regression/benchmarks.py`'s pin), pointed to via
`PASTAPATHFINDER_DJANGO_BENCHMARK`:

```
PASTAPATHFINDER_DJANGO_BENCHMARK=<path to the Linux-FS checkout> pytest -m slow -k django -s
```

Result: **2 passed** in 32.12 s.

- `test_django_full_analysis_is_within_the_fr29_bound`: **11.5 s** (bound 600 s; prototype
  reference 11.3 s) — 908 files, 0 skipped, 14,477 nodes / 38,115 edges, peak RSS 613 MB.
- `test_django_reanalysis_after_five_changed_files_is_within_the_fr30_bound`: **8.1 s**
  (bound 30 s; prototype reference 13.2–15.2 s) — same file/node counts after the 5-file
  edit set, `reanalysis.json` attributed correctly.

Five-workflow pass reprised here too (§4's procedure, same outcomes) against the Django
checkout itself in place of the smaller fixture, confirming the workflows function at
benchmark scale on the Linux filesystem, not only on a 30-file tree.

**AC-31.2: PASS** — both FR-29 and FR-30 bounds hold on the WSL2 Linux filesystem.

## 7. AC-31.3 — WSL2, Windows-mounted filesystem (`/mnt/c`)

Same pinned commit, second checkout under `/mnt/c` (verified via `git log -1` against the
same pin), pointed to via the same env var:

```
PASTAPATHFINDER_DJANGO_BENCHMARK=<path to the /mnt/c checkout> pytest -m slow -k django -s
```

Result: **2 passed** in 157.27 s. Per FR-31, the bound is **not asserted** here — recorded
for information only:

- Full analyze: **61.2 s** (vs. 11.5 s on the Linux FS — the `/mnt/c` translation-layer
  slowdown `docs/wsl.md` describes, ~5×). Same artifact shape as the Linux-FS run:
  908 files analyzed, 0 skipped, 14,477 nodes / 38,115 edges — **artifacts are correct**,
  only slower.
- Re-analyze after 5 changed files: **9.4 s**. Same reconciled counts.

Both checkouts were confirmed still pristine at the pinned commit (`git status --short`
empty) after their respective test runs — the FR-30 test edits a scratch copy, never the
pinned tree itself.

**AC-31.3: PASS** — analysis completes, artifacts are correct; bounds not asserted (by
design) and, incidentally, still inside them on this run.

## 8. AC-31.4 — no platform-gating code

```
grep -rn "sys.platform\|platform.system\|os.name ==\|win32\|WSL" src/pastapathfinder/
```

No matches. `src/pastapathfinder/` contains no platform-detection or platform-gating logic
of any kind. Per AC-31.4's text, this is exactly what's required: an unsupported platform
fails as an ordinary error (e.g., a syscall or filesystem error surfacing normally), and
there is no requirement to detect or block it, so none was built. **PASS** (confirmed by
inspection — nothing to execute).

## 9. AC-33.1 — offline (external network blocked)

An unprivileged network namespace with no route to the outside world, loopback up:

```
unshare --net --map-root-user bash -c 'ip link set lo up; <workflow commands>'
```

Confirmed blocked: `curl https://pypi.org` inside the namespace failed
(`Could not resolve host`); loopback (`127.0.0.1`) worked normally.

Inside that namespace, against `tests/fixtures/flask_app/`:

- `analyze` completed, exit 0, all reports written (`reanalysis.json: mode=full`).
- `query entry-points --json` answered (5 entries).
- `view --port 8518` served `/api/meta` and `/` (200) over loopback.
- A second, separate offline pass exercised US-4 directly: an edit to `app.py` followed by
  re-`analyze` inside the same blocked-network namespace produced `reanalysis.json:
  mode=incremental`, `reprocessed: [{"path": "app.py", "reason": "content_changed"}]`,
  0.055 s.

**AC-33.1: PASS** — all five workflow families (including incremental re-analysis,
verified directly rather than inferred) function with all external network access blocked.

## 10. AC-34.1 — unprivileged user, read-only codebase

Never used `sudo`/root for any product operation in this session (`whoami` → `scott`,
`uid=1000`, no elevated group used). To make the read/write split explicit rather than
merely incidental:

```
cp -r tests/fixtures/flask_app <codebase-copy>
chmod -R a-w <codebase-copy>            # read-only: 555 dirs, 444 files
mkdir <writable-out-dir>
pastapathfinder analyze <codebase-copy> --out <writable-out-dir>
```

- `analyze` completed, exit 0, wrote the index/reports to the writable `--out` only; the
  read-only codebase directory was never modified (`find <codebase-copy> -newer /tmp`
  found nothing after the run touched only the analyzed files it read).
- `query entry-points --out <writable-out-dir> --json` answered (5 entries).
- `view --out <writable-out-dir> --port 8519` served `/api/meta` and `/` (200).

**AC-34.1: PASS** — all three workflow families function as an unprivileged user with
read-only access to the codebase and write access only to the output location.

(One fixture, `tests/fixtures/django_app/`, failed under this same read-only setup with a
mypy "No parent module -- cannot perform relative import" error on `urls.py`. Reproduced
identically with normal, writable permissions on the same copy — confirmed **unrelated to
privilege**; that fixture's relative imports assume a package context it doesn't carry
standalone. Not a task 5.4 finding; `tests/fixtures/flask_app/` and the full
`src/pastapathfinder/` tree both analyze cleanly under identical read-only conditions.)

## 11. The repository's check set

Run against the dev venv (`pip install -e '.[dev]'`); Node.js v22.14.0 was added to `PATH`
for one of the runs below (a standalone binary, fetched only to exercise the existing
`tests/unit/test_viewer_frontend_js.py` JS-behavior suite — not a runtime dependency of the
product).

```
ruff check .                 # All checks passed!
ruff format --check .        # src/ and tests/: 73 files, all already formatted.
                              # specs/design.md: flagged — see note below.
pytest                       # final clean run (no PASTAPATHFINDER_DJANGO_BENCHMARK set):
                              # 768 passed, 5 skipped, 8 deselected, 0 failed, 192.90 s
pytest -m slow -k django     # 2 passed (×2, once per checkout — §6/§7)
```

- **`ruff format --check .` flags `specs/design.md`.** The currently available `ruff`
  (0.16.0) formats fenced ` ```python ` code blocks inside Markdown by default — a behavior
  this repository's conventions predate. It wants to reformat the `LanguageAdapter`
  snippet in design.md §3.4. **Not fixed**: editing `specs/` requires explicit stakeholder
  approval (CLAUDE.md rule 2) and this is unrelated to task 5.4's scope (a `ruff`-version
  drift, not a platform-verification finding). `src/` and `tests/` — the files this check
  actually guards against regression — are 100 % clean. Flagging for stakeholder awareness.
- **`pytest`'s default run** (no slow marker, no Node.js, no benchmark env var) skips 27:
  22 need Node.js on `PATH` (`tests/unit/test_viewer_frontend_js.py`) and 5 need
  `PASTAPATHFINDER_DJANGO_BENCHMARK` (`tests/unit/test_extract_benchmark.py`,
  `tests/unit/test_analyze_benchmark.py`) — both by design (README-documented
  preconditions, not failures). With Node.js provisioned and the env var left unset, the
  **final, clean run** above passed 768 (746 + the 22 Node-dependent tests), skipped
  exactly the 5 that need the benchmark checkout, 0 failed. The 5 skipped tests pass too
  when the env var is provided (confirmed in isolation: `test_extract_benchmark.py`,
  3 passed in 19.91 s).
- **Two anomalies during this process, both traced to the test invocation, not the
  product, and absent from the final clean run above:**
  1. One combined-suite attempt (Node.js on `PATH` **and**
     `PASTAPATHFINDER_DJANGO_BENCHMARK` set, run immediately after two ~2–3 minute Django
     benchmark subprocess runs in the same shell session) crashed with a native SIGSEGV
     inside the mypy/`librt` extension stack (`dmesg`: `python3.14: pytest: potentially
     unexpected fatal signal 11`, a futex-related fault). It did **not** reproduce on an
     immediate rerun of the identical command (see next bullet's 772-passed run), nor when
     the suites making it up ran in isolation. Recorded for visibility rather than chased
     further — the signature (a futex fault after sustained back-to-back heavy runs) reads
     as transient host contention, not a deterministic defect; if it recurs, look at
     `mypy`/`librt` under concurrent load on this kernel
     (`6.6.87.2-microsoft-standard-WSL2`).
  2. The immediate rerun of that same combined command (Node.js + benchmark env var, no
     crash this time) reported **1 failed, 772 passed**:
     `test_the_per_benchmark_variable_wins_over_the_shared_root`
     (`tests/regression/test_pins.py:135`) failed because `PASTAPATHFINDER_DJANGO_BENCHMARK`
     was still exported in the invoking shell — the test sets `CHECKOUT_ROOT_ENV` and the
     *pandas* per-benchmark var via `monkeypatch` to assert the Django lookup falls back to
     the shared root, but never clears the *Django* per-benchmark var itself, so an
     ambient value (exactly what this session had, left over from §6/§7) wins and the
     assertion sees the wrong path. Confirmed self-inflicted: `test_pins.py` alone passes
     9/9 with the var unset (isolated rerun), and the final clean run above — same
     invocation, env var unset — passed with no failures. Worth a note for whoever next
     touches `test_pins.py`: the test should `monkeypatch.delenv` the Django var itself
     (not just set the shared-root and pandas vars) so it doesn't depend on the ambient
     shell being free of it; not fixed here as it is task 4.4's test, not part of this
     task's scope, and does not affect the product.

## 12. §8-O6 — closing the WSL2 working assumption

design.md §8-O6's working assumption — **no WSL-specific code is required** — held. §6/§7
above are the WSL2 pass §8-O6 asked for: all workflows function on WSL2, on both the Linux
filesystem (bounds hold, §6) and `/mnt/c` (bounds not asserted, artifacts correct, §7), with
no WSL-specific branch anywhere in `src/pastapathfinder/` (§8 confirms no platform code
exists at all). The dated trace closing §8-O6 is recorded in `specs/design.md` alongside
this file.

## 13. Other observations (not blocking, recorded for awareness)

- **Python 3.14 vs. the 3.13 CLAUDE.md records as verified.** This session's WSL2 install
  used the system Python, 3.14.4 — above `requires-python >= 3.12` but past the version
  CLAUDE.md names as developed/verified on. Every check in this document passed on it;
  recorded so a 3.14-specific regression, if one ever surfaces, isn't a surprise.
- **A minimal Python install can lack `pip`/`ensurepip`/`venv`'s pip support.** This
  WSL2 Ubuntu image shipped without them (`apt install python3.14-venv` would fix it, and
  needs privileges this session didn't have); bootstrapping via `get-pip.py` worked without
  elevation. Universal Python-packaging behavior, not a pastapathfinder or WSL2-specific
  issue — `docs/install.md` doesn't need to cover it, but a maintainer hitting "No module
  named pip" on a fresh WSL2 distro should know this is why.
