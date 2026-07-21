"""The layered exclusion rule engine.

design.md §3.3 (normative rule layers), §8-O1 (directory pruning), D11; requirements
FR-2 (AC-2.1/2.2), FR-3 (AC-3.1/3.2), FR-4 (AC-4.1/4.2/4.3), FR-5's attribution data.

`prune_walk` below is a test double for the walk that task 1.4 owns: the rule engine's
verification criteria are phrased in terms of candidates and exclusion records, which
only exist once something walks a tree with a `RuleSet` in hand. It is deliberately the
crudest walk that honours §3.3's pruning rule, so what these tests measure is the rules,
not the walker.
"""

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from pastapathfinder.exclusions import (
    COMMON_CONVENTIONS,
    PYTHON_CONVENTIONS,
    SOURCE_COMMON,
    SOURCE_PYTHON,
    SOURCE_USER_EXCLUDE,
    ExclusionRecord,
    InvalidPatternError,
    RuleSet,
    build_ruleset,
)

# design.md §3.3's normative lists, copied verbatim. `test_convention_sets_are_the_
# normative_lists` pins the shipped constants against these, so an edit to the product
# list without a corresponding design amendment fails the suite (CLAUDE.md rule 4).
DESIGN_COMMON_CONVENTIONS = (".git/",)
DESIGN_PYTHON_CONVENTIONS = (
    "venv/",
    ".venv/",
    "env/",
    ".env/",
    "virtualenv/",
    "build/",
    "dist/",
    "__pycache__/",
    ".tox/",
    ".nox/",
    ".eggs/",
    "*.egg-info/",
    ".mypy_cache/",
    ".pytest_cache/",
    "node_modules/",
)


def make_tree(root: Path, files: Mapping[str, str]) -> Path:
    """Write `relpath -> content` under `root`, creating parents."""
    for relpath, content in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def prune_walk(root: Path, ruleset: RuleSet) -> tuple[list[str], list[ExclusionRecord]]:
    """Walk `root`, pruning at directory matches (design.md §3.3, §8-O1).

    Returns `(candidates, exclusions)` as root-relative POSIX paths and records. An
    excluded directory contributes exactly one record and nothing beneath it is
    enumerated — D17's counting unit.
    """
    candidates: list[str] = []
    exclusions: list[ExclusionRecord] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir

        def relpath(name: str, rel_dir: str = rel_dir) -> str:
            return f"{rel_dir}/{name}" if rel_dir else name

        dirnames.sort()
        kept = []
        for name in dirnames:
            record = ruleset.match(relpath(name), is_dir=True)
            if record is None:
                kept.append(name)
            else:
                exclusions.append(record)
        dirnames[:] = kept

        for name in sorted(filenames):
            record = ruleset.match(relpath(name), is_dir=False)
            if record is None:
                candidates.append(relpath(name))
            else:
                exclusions.append(record)
    return candidates, exclusions


def no_warnings(message: str) -> None:
    """A warn sink for runs that must not warn (AC-2.2)."""
    pytest.fail(f"unexpected warning: {message}")


def collecting_warnings() -> tuple[list[str], object]:
    warnings: list[str] = []
    return warnings, warnings.append


# ---------------------------------------------------------------------------
# FR-2 — default exclusion conventions
# ---------------------------------------------------------------------------


def test_convention_sets_are_the_normative_lists():
    assert COMMON_CONVENTIONS == DESIGN_COMMON_CONVENTIONS
    assert PYTHON_CONVENTIONS == DESIGN_PYTHON_CONVENTIONS


def test_venv_is_excluded_once_with_python_attribution(tmp_path):
    """AC-2.1: nothing under `venv/` is a candidate, and one record explains why."""
    make_tree(
        tmp_path,
        {
            "app.py": "",
            "venv/bin/activate": "",
            "venv/lib/site-packages/thing/__init__.py": "",
            "venv/lib/site-packages/thing/core.py": "",
        },
    )
    ruleset = build_ruleset(tmp_path, warn=no_warnings)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert candidates == ["app.py"]
    assert exclusions == [
        ExclusionRecord(path="venv", is_dir=True, pattern="venv/", source=SOURCE_PYTHON)
    ]


def _example_directory(pattern: str) -> str:
    """A directory name the convention `pattern` must match."""
    stem = pattern.rstrip("/")
    return "mypkg.egg-info" if stem == "*.egg-info" else stem


@pytest.mark.parametrize(
    ("pattern", "source"),
    [(pattern, SOURCE_COMMON) for pattern in COMMON_CONVENTIONS]
    + [(pattern, SOURCE_PYTHON) for pattern in PYTHON_CONVENTIONS],
)
def test_every_convention_prunes_its_directory(tmp_path, pattern, source):
    """The full normative list of design.md §3.3, one directory at a time."""
    name = _example_directory(pattern)
    make_tree(tmp_path, {"keep.py": "", f"{name}/mod.py": "", f"{name}/deep/other.py": ""})
    ruleset = build_ruleset(tmp_path, warn=no_warnings)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert candidates == ["keep.py"]
    assert exclusions == [ExclusionRecord(path=name, is_dir=True, pattern=pattern, source=source)]


def test_conventions_matching_nothing_are_not_errors(tmp_path):
    """AC-2.2: unmatched default rules neither fail nor warn."""
    make_tree(tmp_path, {"pkg/__init__.py": "", "pkg/core.py": ""})
    ruleset = build_ruleset(tmp_path, warn=no_warnings)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert candidates == ["pkg/__init__.py", "pkg/core.py"]
    assert exclusions == []
    assert ruleset.diagnostics == ()


def test_a_file_named_like_a_directory_convention_is_kept(tmp_path):
    """Directory patterns match directories; `.env` the file is not `.env/` the tree."""
    make_tree(tmp_path, {".env": "SECRET=1", "app.py": ""})
    ruleset = build_ruleset(tmp_path, warn=no_warnings)
    _, exclusions = prune_walk(tmp_path, ruleset)

    assert exclusions == []


def test_the_root_itself_is_never_excluded(tmp_path):
    ruleset = build_ruleset(tmp_path, warn=no_warnings)
    assert ruleset.match("", is_dir=True) is None
    assert ruleset.match(".", is_dir=True) is None


# ---------------------------------------------------------------------------
# FR-3 — .gitignore incorporation
# ---------------------------------------------------------------------------


def test_gitignore_directory_pattern_excludes_with_file_attribution(tmp_path):
    """AC-3.1: the excluding rule is named down to the `.gitignore` that carried it."""
    make_tree(
        tmp_path,
        {
            ".gitignore": "generated/\n",
            "app.py": "",
            "generated/pb2.py": "",
            "generated/more/pb2.py": "",
        },
    )
    ruleset = build_ruleset(tmp_path, warn=no_warnings)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert candidates == [".gitignore", "app.py"]
    assert exclusions == [
        ExclusionRecord(
            path="generated", is_dir=True, pattern="generated/", source="gitignore:.gitignore"
        )
    ]


def test_gitignore_file_pattern_excludes_per_file(tmp_path):
    make_tree(
        tmp_path,
        {".gitignore": "*.gen.py\n", "a.gen.py": "", "b.py": "", "pkg/c.gen.py": ""},
    )
    ruleset = build_ruleset(tmp_path, warn=no_warnings)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert candidates == [".gitignore", "b.py"]
    assert [(record.path, record.is_dir, record.source) for record in exclusions] == [
        ("a.gen.py", False, "gitignore:.gitignore"),
        ("pkg/c.gen.py", False, "gitignore:.gitignore"),
    ]


def test_nested_gitignore_patterns_are_relative_to_their_own_directory(tmp_path):
    """AC-3.1: an anchored pattern anchors at its own `.gitignore`, not at the root."""
    make_tree(
        tmp_path,
        {
            ".gitignore": "/top/\n",
            "sub/.gitignore": "/local/\n",
            "top/a.py": "",
            "local/b.py": "",
            "sub/local/c.py": "",
            "sub/top/d.py": "",
        },
    )
    ruleset = build_ruleset(tmp_path, warn=no_warnings)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert set(exclusions) == {
        ExclusionRecord(path="top", is_dir=True, pattern="/top/", source="gitignore:.gitignore"),
        ExclusionRecord(
            path="sub/local", is_dir=True, pattern="/local/", source="gitignore:sub/.gitignore"
        ),
    }
    assert "local/b.py" in candidates
    assert "sub/top/d.py" in candidates


def test_a_deeper_gitignore_outranks_a_shallower_one(tmp_path):
    make_tree(
        tmp_path,
        {
            ".gitignore": "*.tmp.py\n",
            "sub/.gitignore": "!keep.tmp.py\n",
            "a.tmp.py": "",
            "sub/keep.tmp.py": "",
            "sub/other.tmp.py": "",
        },
    )
    ruleset = build_ruleset(tmp_path, warn=no_warnings)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert "sub/keep.tmp.py" in candidates
    assert {record.path for record in exclusions} == {"a.tmp.py", "sub/other.tmp.py"}


def test_gitignore_inside_a_pruned_directory_is_never_read(tmp_path):
    """§8-O1: pruning means the rules inside an excluded tree are not even parsed."""
    make_tree(tmp_path, {"venv/.gitignore": "!\n", "app.py": ""})
    warnings, warn = collecting_warnings()
    ruleset = build_ruleset(tmp_path, warn=warn)

    assert ruleset.diagnostics == ()
    assert warnings == []


def test_unparseable_gitignore_line_warns_and_the_run_continues(tmp_path):
    """AC-3.2: warning names file and line, a diagnostic is recorded, rules survive."""
    make_tree(
        tmp_path,
        {
            ".gitignore": "generated/\n!\nlogs/\n",
            "app.py": "",
            "generated/x.py": "",
            "logs/y.py": "",
        },
    )
    warnings, warn = collecting_warnings()
    ruleset = build_ruleset(tmp_path, warn=warn)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert len(ruleset.diagnostics) == 1
    diagnostic = ruleset.diagnostics[0]
    assert diagnostic.kind == "gitignore_problem"
    assert diagnostic.path == ".gitignore"
    assert diagnostic.line == 2
    assert warnings == [diagnostic.message]
    assert ".gitignore:2" in diagnostic.message

    # The remaining rules in the same file are still in force.
    assert {record.path for record in exclusions} == {"generated", "logs"}
    assert candidates == [".gitignore", "app.py"]


def test_unreadable_gitignore_warns_and_the_run_continues(tmp_path):
    """AC-3.2, the whole-file half: undecodable bytes are as unreadable as no access."""
    make_tree(tmp_path, {"app.py": "", "venv/x.py": ""})
    (tmp_path / ".gitignore").write_bytes(b"generated/\n\xff\xfe not utf-8\n")
    warnings, warn = collecting_warnings()
    ruleset = build_ruleset(tmp_path, warn=warn)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert [diagnostic.kind for diagnostic in ruleset.diagnostics] == ["gitignore_problem"]
    diagnostic = ruleset.diagnostics[0]
    assert diagnostic.path == ".gitignore"
    assert diagnostic.line is None
    assert warnings == [diagnostic.message]

    # The file's own rules are gone, but the run completes on the remaining rules.
    assert candidates == [".gitignore", "app.py"]
    assert [record.source for record in exclusions] == [SOURCE_PYTHON]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files regardless")
def test_permission_denied_gitignore_warns_and_the_run_continues(tmp_path):
    make_tree(tmp_path, {"app.py": "", ".gitignore": "generated/\n", "generated/x.py": ""})
    (tmp_path / ".gitignore").chmod(0o000)
    warnings, warn = collecting_warnings()
    try:
        ruleset = build_ruleset(tmp_path, warn=warn)
        candidates, exclusions = prune_walk(tmp_path, ruleset)
    finally:
        (tmp_path / ".gitignore").chmod(0o644)

    assert [diagnostic.kind for diagnostic in ruleset.diagnostics] == ["gitignore_problem"]
    assert ".gitignore" in ruleset.diagnostics[0].message
    assert warnings == [ruleset.diagnostics[0].message]
    assert exclusions == []
    assert "generated/x.py" in candidates


def test_gitignore_diagnostics_are_ordered_deterministically(tmp_path):
    """D12: two builds over one tree agree, walk order included."""
    make_tree(
        tmp_path,
        {".gitignore": "!\n", "b/.gitignore": "!\n", "a/.gitignore": "!\n", "app.py": ""},
    )
    first = build_ruleset(tmp_path, warn=lambda message: None).diagnostics
    second = build_ruleset(tmp_path, warn=lambda message: None).diagnostics

    assert first == second
    assert [diagnostic.path for diagnostic in first] == [
        ".gitignore",
        "a/.gitignore",
        "b/.gitignore",
    ]


# ---------------------------------------------------------------------------
# FR-4 — user overrides
# ---------------------------------------------------------------------------


def test_user_exclude_is_attributed_to_the_user(tmp_path):
    """AC-4.2."""
    make_tree(tmp_path, {"app.py": "", "gen/x.py": "", "thing_pb2.py": ""})
    ruleset = build_ruleset(tmp_path, exclude=["gen/", "*_pb2.py"], warn=no_warnings)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert candidates == ["app.py"]
    assert set(exclusions) == {
        ExclusionRecord(path="gen", is_dir=True, pattern="gen/", source=SOURCE_USER_EXCLUDE),
        ExclusionRecord(
            path="thing_pb2.py", is_dir=False, pattern="*_pb2.py", source=SOURCE_USER_EXCLUDE
        ),
    }


def test_reinclude_restores_a_default_excluded_path(tmp_path):
    """AC-4.1: the user's re-inclusion outranks the convention that excluded it."""
    make_tree(tmp_path, {"build/keep.py": "", "venv/x.py": "", "app.py": ""})
    ruleset = build_ruleset(tmp_path, reinclude=["build/"], warn=no_warnings)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert candidates == ["app.py", "build/keep.py"]
    assert exclusions == [
        ExclusionRecord(path="venv", is_dir=True, pattern="venv/", source=SOURCE_PYTHON)
    ]


def test_reinclude_outranks_a_gitignore(tmp_path):
    make_tree(tmp_path, {".gitignore": "vendor/\n", "vendor/keep_this/a.py": ""})
    ruleset = build_ruleset(tmp_path, reinclude=["vendor/"], warn=no_warnings)
    candidates, exclusions = prune_walk(tmp_path, ruleset)

    assert exclusions == []
    assert "vendor/keep_this/a.py" in candidates


def test_user_exclude_outranks_a_gitignore_negation(tmp_path):
    make_tree(tmp_path, {".gitignore": "!vendor/\n", "vendor/a.py": ""})
    ruleset = build_ruleset(tmp_path, exclude=["vendor/"], warn=no_warnings)
    _, exclusions = prune_walk(tmp_path, ruleset)

    assert [record.source for record in exclusions] == [SOURCE_USER_EXCLUDE]


def test_invalid_user_pattern_terminates_the_run(tmp_path):
    """AC-4.3: the error names the pattern; nothing is silently ignored."""
    with pytest.raises(InvalidPatternError, match=r"'!'"):
        build_ruleset(tmp_path, exclude=["ok/", "!"], warn=no_warnings)

    with pytest.raises(InvalidPatternError, match=r"'!'"):
        build_ruleset(tmp_path, reinclude=["!"], warn=no_warnings)


def test_inert_user_pattern_is_rejected_rather_than_ignored(tmp_path):
    """A comment or blank line in user configuration would match nothing forever."""
    with pytest.raises(InvalidPatternError, match="matches nothing"):
        build_ruleset(tmp_path, exclude=["# generated"], warn=no_warnings)

    with pytest.raises(InvalidPatternError, match="matches nothing"):
        build_ruleset(tmp_path, exclude=[""], warn=no_warnings)
