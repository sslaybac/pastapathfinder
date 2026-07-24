"""The pinned benchmark codebases: pins, checkout resolution, fetch-by-hash.

specs/tasks.md task 4.4 and its "Benchmark pins" preamble; design.md §8-O5 (the pins are
recorded at design stage and copied verbatim here), D1a (this is the fetch half of the
revalidation procedure); requirements §4.8 (the benchmark designations and the reference
machine), FR-29, FR-30.

Two codebases, hard-designated by the stakeholder (requirements §4.8) and pinned to exact
commits so a measurement taken today can be taken again next year:

* **Django core** — the *performance* reference. FR-29's 600 s bound and FR-30's 30 s bound
  are asserted against it. Analyze the `django/` **package subdirectory**, never the
  repository root: the root is ~4× larger and would misrepresent FR-29.
* **pandas** — the *dynamism and robustness* reference. Run-to-completion and artifact
  correctness are asserted against it; the time bound deliberately is not (AC-29.3).

Nothing here is shipped: this module is dev tooling for `tests/regression/`, like
`compare.py` beside it. It deliberately imports no pytest, so the fetch path is usable as a
plain script:

    python tests/regression/benchmarks.py            # fetch whatever is missing
    python tests/regression/benchmarks.py django     # just the one
    python tests/regression/benchmarks.py --dest /srv/benchmarks --force

A checkout already on the machine is used instead, via `$PASTAPATHFINDER_BENCHMARKS`
(a directory holding `django/` and `pandas/`) or a per-benchmark variable. Either way the
commit is verified against the pin before any measurement runs — an unpinned tree makes
every number in this suite unreproducible, which is the one failure mode a benchmark
regression suite cannot tolerate quietly.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: Where checkouts land when nothing says otherwise. Under `tests/regression/` so a
#: developer finds them next to the suite that uses them; gitignored.
DEFAULT_CHECKOUT_ROOT = Path(__file__).resolve().parent / ".benchmarks"

#: Overrides the directory above; expected to contain one subdirectory per benchmark name.
CHECKOUT_ROOT_ENV = "PASTAPATHFINDER_BENCHMARKS"


class BenchmarkUnavailable(RuntimeError):
    """No usable checkout: absent, not a git repository, or at the wrong commit.

    Carries the message a caller should show — the tests turn it into a skip whose text
    tells the reader exactly which command produces the missing tree.
    """


@dataclass(frozen=True, slots=True)
class Benchmark:
    """One pinned benchmark codebase.

    `package` is the subdirectory that gets analyzed, which is **not** the repository root
    for either benchmark; `file_count` and `code_lines` are the pins' own census figures,
    carried here so a test can assert it is looking at the tree the pin names.
    """

    name: str
    url: str
    commit: str
    package: str
    file_count: int
    code_lines: int
    source: str

    @property
    def env_var(self) -> str:
        """Per-benchmark override, e.g. `PASTAPATHFINDER_DJANGO_BENCHMARK`."""
        return f"PASTAPATHFINDER_{self.name.upper()}_BENCHMARK"


#: specs/tasks.md's "Benchmark pins" block, copied verbatim (design.md §8-O5).
DJANGO = Benchmark(
    name="django",
    url="https://github.com/django/django",
    commit="274df4df0bca7fcfb5c1c1d49567f770df147eeb",
    package="django",
    file_count=908,
    code_lines=131_294,
    source="prototypes/engine-eval/FINDINGS-harness.md §2",
)

PANDAS = Benchmark(
    name="pandas",
    url="https://github.com/pandas-dev/pandas",
    commit="f6df82f9d0bdba793cbe34251f57c5d6e3fe804c",
    package="pandas",
    file_count=1_418,
    code_lines=664_190,
    source="prototypes/engine-eval/FINDINGS-session5.md Part 2",
)

BENCHMARKS: tuple[Benchmark, ...] = (DJANGO, PANDAS)


def by_name(name: str) -> Benchmark:
    for benchmark in BENCHMARKS:
        if benchmark.name == name:
            return benchmark
    known = ", ".join(benchmark.name for benchmark in BENCHMARKS)
    raise KeyError(f"unknown benchmark {name!r} (known: {known})")


# ---------------------------------------------------------------------------
# Locating a checkout
# ---------------------------------------------------------------------------


def checkout_root() -> Path:
    """The directory holding one subdirectory per benchmark."""
    override = os.environ.get(CHECKOUT_ROOT_ENV)
    return Path(override).expanduser().resolve() if override else DEFAULT_CHECKOUT_ROOT


def checkout_path(benchmark: Benchmark) -> Path:
    """Where this benchmark's repository is expected, honoring both overrides."""
    override = os.environ.get(benchmark.env_var)
    if override:
        return Path(override).expanduser().resolve()
    return checkout_root() / benchmark.name


def head_commit(path: Path) -> str | None:
    """`git rev-parse HEAD` in `path`, or None when it is not a git working tree."""
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def package_dir(benchmark: Benchmark) -> Path:
    """The directory a run analyzes: the pinned package inside the checkout.

    Accepts either the repository root or the package directory itself as the configured
    location, because both are natural things for a developer to point an env var at.
    """
    location = checkout_path(benchmark)
    package = location / benchmark.package
    return package if package.is_dir() else location


def verify(benchmark: Benchmark) -> Path:
    """Return the package directory to analyze, or raise `BenchmarkUnavailable`.

    The commit check is not a formality. Every number this suite asserts — 908 files,
    ≤ 600 s, the `pandas._libs` edge population — is a statement about one tree at one
    commit; measured against a different one it is neither a pass nor a failure, just
    noise, so a mismatched checkout stops the measurement rather than colouring it.
    """
    location = checkout_path(benchmark)
    hint = (
        f"fetch it with `python tests/regression/{Path(__file__).name} {benchmark.name}` "
        f"(see tests/regression/README.md), or point ${benchmark.env_var} at a checkout"
    )
    if not location.is_dir():
        raise BenchmarkUnavailable(f"no {benchmark.name} checkout at {location} — {hint}")

    package = package_dir(benchmark)
    if not (package / "__init__.py").is_file():
        raise BenchmarkUnavailable(
            f"{package} is not the {benchmark.name} package directory (no __init__.py) — {hint}"
        )

    found = head_commit(location)
    if found is None:
        raise BenchmarkUnavailable(f"{location} is not a git working tree — {hint}")
    if found != benchmark.commit:
        raise BenchmarkUnavailable(
            f"{benchmark.name} checkout at {location} is at {found}, not the pinned "
            f"{benchmark.commit} — the pins are design.md §8-O5's; {hint}"
        )
    return package


# ---------------------------------------------------------------------------
# Fetching by hash
# ---------------------------------------------------------------------------


def _git(*arguments: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *arguments], cwd=str(cwd) if cwd else None, check=True)


def fetch(benchmark: Benchmark, destination: Path | None = None, *, force: bool = False) -> Path:
    """Fetch the pinned commit into `destination`, shallowly, by hash.

    `git fetch <url> <sha>` retrieves exactly the pinned commit — no branch, no tags, no
    history — so the checkout cannot drift with upstream and the download stays small. An
    existing checkout already at the pin is left alone unless `force` is set.
    """
    destination = Path(destination) if destination else checkout_path(benchmark)
    if destination.is_dir() and head_commit(destination) == benchmark.commit and not force:
        print(f"{benchmark.name}: already at {benchmark.commit} in {destination}")
        return destination

    destination.mkdir(parents=True, exist_ok=True)
    print(f"{benchmark.name}: fetching {benchmark.commit} from {benchmark.url} into {destination}")
    if not (destination / ".git").exists():
        _git("init", "--quiet", str(destination))
    _git("fetch", "--depth", "1", "--quiet", benchmark.url, benchmark.commit, cwd=destination)
    _git("checkout", "--quiet", "--force", "FETCH_HEAD", cwd=destination)

    found = head_commit(destination)
    if found != benchmark.commit:
        raise RuntimeError(f"{benchmark.name}: checked out {found}, expected {benchmark.commit}")
    print(f"{benchmark.name}: ready at {destination / benchmark.package}")
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmarks.py",
        description=(
            "Fetch the pinned benchmark codebases (design.md §8-O5) by commit hash for the "
            "tests/regression/ suite."
        ),
    )
    parser.add_argument(
        "names",
        nargs="*",
        choices=[benchmark.name for benchmark in BENCHMARKS],
        help="which benchmarks to fetch (default: all)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help=f"where to put them (default: ${CHECKOUT_ROOT_ENV} or {DEFAULT_CHECKOUT_ROOT})",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-fetch even when the pin is already checked out"
    )
    arguments = parser.parse_args(argv)

    selected = [by_name(name) for name in arguments.names] if arguments.names else BENCHMARKS
    for benchmark in selected:
        destination = Path(arguments.dest) / benchmark.name if arguments.dest else None
        fetch(benchmark, destination, force=arguments.force)
    return 0


if __name__ == "__main__":  # pragma: no cover - the dev entry point
    sys.exit(main())
