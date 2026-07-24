"""`benchmarks.py`'s pin and checkout resolution (specs/tasks.md task 4.4).

design.md §8-O5 (the pins), D1a; requirements §4.8.

Fast and unmarked, so it runs in the default suite: everything `test_benchmarks.py`
asserts is a statement about one tree at one commit, and this is the machinery that decides
whether the tree in front of it is that one. If it silently accepted the wrong checkout,
every benchmark number would still be *green* and none of them would mean anything.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import benchmarks
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README = Path(__file__).resolve().parent / "README.md"


def make_repo(root: Path, *, package: str, commit_message: str = "pin") -> Path:
    """A git working tree holding `<root>/<package>/__init__.py`, committed."""
    (root / package).mkdir(parents=True)
    (root / package / "__init__.py").write_text("", encoding="utf-8")
    for command in (
        ["init", "--quiet", "--initial-branch=main"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "--quiet", "-m", commit_message],
    ):
        subprocess.run(["git", "-C", str(root), *command], check=True, capture_output=True)
    return root


def test_the_pins_are_the_ones_the_specs_record():
    """The literal values from `specs/tasks.md`'s benchmark-pins block."""
    assert benchmarks.DJANGO.commit == "274df4df0bca7fcfb5c1c1d49567f770df147eeb"
    assert benchmarks.DJANGO.package == "django"
    assert benchmarks.DJANGO.file_count == 908
    assert benchmarks.PANDAS.commit == "f6df82f9d0bdba793cbe34251f57c5d6e3fe804c"
    assert benchmarks.PANDAS.package == "pandas"
    assert benchmarks.PANDAS.file_count == 1418
    # Both are analyzed at a package subdirectory, never at the repository root.
    assert all(benchmark.package for benchmark in benchmarks.BENCHMARKS)


def test_the_readme_reproduces_design_35s_enumerated_mypy_internals():
    """D1a's checklist is design.md §3.5's list, not a paraphrase of it.

    The list exists so a failing engine upgrade localizes fast; a README that had drifted
    from it would send the reader looking in the wrong place at exactly the wrong moment.
    Read out of the spec rather than restated here, so drift in either direction fails.
    """
    design = (REPOSITORY_ROOT / "specs" / "design.md").read_text(encoding="utf-8")
    anchor = design.index("Enumerated mypy internals touched")
    sentence = design[anchor : design.index("\n", anchor)]
    names = {
        fragment for token in re.findall(r"`([^`]+)`", sentence) for fragment in token.split("/")
    }
    assert len(names) >= 15, sorted(names)  # the list is 11 entries, 8 of them a slash-run

    readme = README.read_text(encoding="utf-8")
    missing = sorted(name for name in names if name not in readme)
    assert not missing, f"design.md §3.5 lists internals the README omits: {missing}"
    # And the rechecked-modules report, which the sentence names in prose rather than code.
    assert "rechecked-modules" in readme


def test_a_checkout_at_the_wrong_commit_is_refused_by_name(tmp_path: Path, monkeypatch):
    """The failure this module exists for: a plausible tree that is not the pinned one."""
    root = make_repo(tmp_path / "django", package="django")
    monkeypatch.setenv(benchmarks.DJANGO.env_var, str(root))

    with pytest.raises(benchmarks.BenchmarkUnavailable) as refusal:
        benchmarks.verify(benchmarks.DJANGO)

    message = str(refusal.value)
    assert benchmarks.DJANGO.commit in message
    assert str(benchmarks.head_commit(root)) in message


def test_a_missing_checkout_names_the_command_that_produces_one(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(benchmarks.DJANGO.env_var, str(tmp_path / "absent"))

    with pytest.raises(benchmarks.BenchmarkUnavailable) as refusal:
        benchmarks.verify(benchmarks.DJANGO)

    assert "tests/regression/benchmarks.py django" in str(refusal.value)


def test_a_tree_that_is_not_a_git_checkout_is_refused(tmp_path: Path, monkeypatch):
    """No commit, no provenance — an unpacked tarball cannot be verified against the pin."""
    root = tmp_path / "django"
    (root / "django").mkdir(parents=True)
    (root / "django" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setenv(benchmarks.DJANGO.env_var, str(root))

    with pytest.raises(benchmarks.BenchmarkUnavailable, match="not a git working tree"):
        benchmarks.verify(benchmarks.DJANGO)


def test_the_package_subdirectory_is_what_gets_analyzed(tmp_path: Path, monkeypatch):
    """Pointing at the repository root still analyzes `django/` — the pin says so, and the
    root is ~4× larger, which would misrepresent FR-29."""
    root = make_repo(tmp_path / "checkout", package="django")
    monkeypatch.setenv(benchmarks.DJANGO.env_var, str(root))

    assert benchmarks.package_dir(benchmarks.DJANGO) == root / "django"


def test_pointing_directly_at_the_package_also_works(tmp_path: Path, monkeypatch):
    """The other natural thing to point an environment variable at."""
    root = make_repo(tmp_path / "checkout", package="django")
    monkeypatch.setenv(benchmarks.DJANGO.env_var, str(root / "django"))

    # No nested `django/django`, so the configured directory is itself the package.
    assert benchmarks.package_dir(benchmarks.DJANGO) == root / "django"


def test_the_shared_root_variable_holds_one_directory_per_benchmark(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(benchmarks.DJANGO.env_var, raising=False)
    monkeypatch.setenv(benchmarks.CHECKOUT_ROOT_ENV, str(tmp_path))

    assert benchmarks.checkout_path(benchmarks.DJANGO) == tmp_path / "django"
    assert benchmarks.checkout_path(benchmarks.PANDAS) == tmp_path / "pandas"


def test_the_per_benchmark_variable_wins_over_the_shared_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(benchmarks.CHECKOUT_ROOT_ENV, str(tmp_path / "shared"))
    monkeypatch.setenv(benchmarks.PANDAS.env_var, str(tmp_path / "elsewhere"))

    assert benchmarks.checkout_path(benchmarks.PANDAS) == tmp_path / "elsewhere"
    assert benchmarks.checkout_path(benchmarks.DJANGO) == tmp_path / "shared" / "django"
