"""Enumeration, probing, symlink and pruning rules.

design.md §3.3 (`discovery`, normative), §8-O1 (directory pruning), D12, D17;
requirements FR-1 (AC-1.1–1.6), FR-2, EC-10, EC-11.

Every test drives the real `discover()` against a real tree: the walk's whole subject
matter is the filesystem, so a mocked one would verify nothing. The two exceptions are
deliberate instrumentation — the `os.scandir` counter that proves pruning never descends
(a property no output can show, since the absence of files is also what a walk that
found nothing produces) and the `os.geteuid` guards on the permission tests, which root
would otherwise pass vacuously.
"""

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from pastapathfinder import discovery
from pastapathfinder.discovery import (
    PROBE_BYTES,
    DiscoveryResult,
    RootError,
    discover,
)
from pastapathfinder.exclusions import SOURCE_PYTHON, SOURCE_USER_EXCLUDE, build_ruleset

AS_ROOT = os.geteuid() == 0


def make_tree(root: Path, files: Mapping[str, str | bytes]) -> Path:
    """Write `relpath -> content` under `root`, creating parents."""
    for relpath, content in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    return root


def no_warnings(message: str) -> None:
    pytest.fail(f"unexpected warning: {message}")


def run(root: Path, **rules: object) -> DiscoveryResult:
    """Discover under `root` with a rule set built from `rules` (see `build_ruleset`)."""
    ruleset = build_ruleset(root, warn=no_warnings, **rules)  # type: ignore[arg-type]
    return discover(root, ruleset)


def found(result: DiscoveryResult) -> list[str]:
    """Candidates as root-relative POSIX paths."""
    return [result.relpath(path) for path in result.candidates]


# ---------------------------------------------------------------------------
# AC-1.1 / AC-1.2 — what is and is not an input
# ---------------------------------------------------------------------------


def test_nested_python_files_at_any_depth_are_candidates(tmp_path):
    """AC-1.1."""
    make_tree(
        tmp_path,
        {
            "app.py": "",
            "pkg/__init__.py": "",
            "pkg/sub/deep/mod.py": "",
            "pkg/sub/deep/deeper/other.py": "",
        },
    )
    result = run(tmp_path)

    assert found(result) == [
        "app.py",
        "pkg/__init__.py",
        "pkg/sub/deep/deeper/other.py",
        "pkg/sub/deep/mod.py",
    ]
    assert result.excluded == []
    assert result.probe_diagnostics == []


def test_unrecognized_files_are_not_inputs(tmp_path):
    """AC-1.2: no status, no record — they were never analysis inputs."""
    make_tree(
        tmp_path,
        {
            "app.py": "",
            "README.md": "",
            "data.json": "{}",
            "notes.txt": "",
            "app.pyc": b"\x00\x01",
            "template.py.j2": "",
        },
    )
    result = run(tmp_path)

    assert found(result) == ["app.py"]
    assert result.excluded == []
    assert result.probe_diagnostics == []


def test_candidates_are_absolute_real_paths_under_the_resolved_root(tmp_path):
    make_tree(tmp_path, {"pkg/mod.py": ""})
    result = run(tmp_path)

    assert result.root == tmp_path.resolve()
    assert result.candidates == [tmp_path.resolve() / "pkg" / "mod.py"]
    assert all(path.is_absolute() for path in result.candidates)


# ---------------------------------------------------------------------------
# AC-1.4 / AC-1.5 — the shebang probe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first_line",
    [
        "#!/usr/bin/env python3",
        "#!/usr/bin/python",
        "#!/usr/bin/env python",
        "#!/usr/local/bin/python3.13 -u",
    ],
)
def test_extensionless_python_shebang_is_discovered(tmp_path, first_line):
    """AC-1.4."""
    make_tree(tmp_path, {"tool": f"{first_line}\nprint('hi')\n"})
    result = run(tmp_path)

    assert found(result) == ["tool"]
    assert result.probe_diagnostics == []


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("elf", b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00"),
        ("image", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"),
        ("shellscript", b"#!/bin/bash\necho hi\n"),
        ("perlscript", b"#!/usr/bin/env perl\n"),
        ("README", b"python is great, but this is prose\n"),
        ("empty", b""),
    ],
)
def test_non_python_extensionless_files_are_not_inputs(tmp_path, name, content):
    """AC-1.5: binary, or a first line that is not a Python shebang."""
    make_tree(tmp_path, {name: content, "app.py": ""})
    result = run(tmp_path)

    assert found(result) == ["app.py"]
    assert result.probe_diagnostics == []


def test_the_probe_reads_at_most_the_normative_byte_bound(tmp_path):
    """design.md §3.3: ≤ 256 bytes of line 1 — a marker beyond it is not seen."""
    padding = "#!/usr/bin/env " + "x" * PROBE_BYTES
    make_tree(tmp_path, {"late": f"{padding}python\n", "early": "#!/usr/bin/env python\n"})
    result = run(tmp_path)

    assert found(result) == ["early"]


def test_a_shebang_on_a_later_line_does_not_count(tmp_path):
    make_tree(tmp_path, {"tool": "# a comment\n#!/usr/bin/env python3\n"})
    assert found(run(tmp_path)) == []


@pytest.mark.skipif(AS_ROOT, reason="root reads mode-000 files regardless")
def test_unreadable_probe_is_a_diagnostic_and_the_walk_continues(tmp_path):
    """AC-1.5's failure half."""
    make_tree(tmp_path, {"locked": "#!/usr/bin/env python3\n", "pkg/app.py": "", "later.py": ""})
    (tmp_path / "locked").chmod(0o000)
    try:
        result = run(tmp_path)
    finally:
        (tmp_path / "locked").chmod(0o644)

    assert found(result) == ["later.py", "pkg/app.py"]
    assert [(d.kind, d.path) for d in result.probe_diagnostics] == [("probe_failure", "locked")]
    assert "Permission denied" in result.probe_diagnostics[0].message


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform has no fifos")
def test_a_fifo_is_never_probed(tmp_path):
    """Probing a fifo would block the run until someone wrote to it."""
    os.mkfifo(tmp_path / "pipe")
    make_tree(tmp_path, {"app.py": ""})
    result = run(tmp_path)

    assert found(result) == ["app.py"]
    assert result.probe_diagnostics == []


# ---------------------------------------------------------------------------
# AC-1.6 / EC-11 — symlinks
# ---------------------------------------------------------------------------


def test_file_symlink_outside_the_root_is_skipped_with_a_diagnostic(tmp_path):
    """AC-1.6: not followed, and the skipped target is named."""
    outside = tmp_path / "outside"
    root = tmp_path / "root"
    make_tree(outside, {"secret.py": "x = 1\n"})
    make_tree(root, {"app.py": ""})
    (root / "link.py").symlink_to(outside / "secret.py")

    result = run(root)

    assert found(result) == ["app.py"]
    assert [(d.kind, d.path) for d in result.probe_diagnostics] == [("symlink_skip", "link.py")]
    assert str(outside / "secret.py") in result.probe_diagnostics[0].message


def test_file_symlink_inside_the_root_is_analyzed_once(tmp_path):
    """AC-1.6: dedupe by real path — one file, one candidate, whichever door it came in."""
    make_tree(tmp_path, {"pkg/real.py": "x = 1\n"})
    (tmp_path / "alias.py").symlink_to(tmp_path / "pkg" / "real.py")

    result = run(tmp_path)

    assert found(result) == ["pkg/real.py"]
    assert result.probe_diagnostics == []


def test_a_link_reached_before_its_target_still_yields_the_real_path(tmp_path):
    """The alphabetically-first door is the link; the candidate is still the real file."""
    make_tree(tmp_path, {"zz/real.py": "x = 1\n"})
    (tmp_path / "aa.py").symlink_to(tmp_path / "zz" / "real.py")

    assert found(run(tmp_path)) == ["zz/real.py"]


def test_broken_symlink_is_skipped_with_a_diagnostic(tmp_path):
    make_tree(tmp_path, {"app.py": ""})
    (tmp_path / "dangling.py").symlink_to(tmp_path / "gone.py")

    result = run(tmp_path)

    assert found(result) == ["app.py"]
    assert [(d.kind, d.path) for d in result.probe_diagnostics] == [("symlink_skip", "dangling.py")]


def test_symlink_into_an_excluded_directory_is_not_followed(tmp_path):
    """An exclusion the user configured must not be undone by a link into it."""
    make_tree(tmp_path, {"venv/lib/thing.py": "", "app.py": ""})
    (tmp_path / "shortcut.py").symlink_to(tmp_path / "venv" / "lib" / "thing.py")

    result = run(tmp_path)

    assert found(result) == ["app.py"]
    assert [(d.kind, d.path) for d in result.probe_diagnostics] == [("symlink_skip", "shortcut.py")]
    assert [record.path for record in result.excluded] == ["venv"]


def test_directory_symlinks_are_not_followed(tmp_path):
    """The real directory is walked on its own; the link adds nothing but a cycle risk."""
    make_tree(tmp_path, {"pkg/mod.py": ""})
    (tmp_path / "alias").symlink_to(tmp_path / "pkg", target_is_directory=True)

    result = run(tmp_path)

    assert found(result) == ["pkg/mod.py"]
    assert [(d.kind, d.path) for d in result.probe_diagnostics] == [("symlink_skip", "alias")]


def test_a_directory_symlink_cycle_terminates(tmp_path):
    """EC-11: the walk must terminate — not following directory links makes it trivial."""
    make_tree(tmp_path, {"pkg/mod.py": "", "app.py": ""})
    (tmp_path / "pkg" / "self").symlink_to(tmp_path / "pkg", target_is_directory=True)
    (tmp_path / "pkg" / "up").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)

    result = run(tmp_path)

    assert found(result) == ["app.py", "pkg/mod.py"]
    assert {d.path for d in result.probe_diagnostics} == {"loop", "pkg/self", "pkg/up"}
    assert {d.kind for d in result.probe_diagnostics} == {"symlink_skip"}


def test_an_excluded_directory_symlink_is_an_exclusion_not_a_symlink_diagnostic(tmp_path):
    """Attribution first: the rule that removed it is the more useful fact."""
    make_tree(tmp_path, {"pkg/mod.py": ""})
    (tmp_path / "venv").symlink_to(tmp_path / "pkg", target_is_directory=True)

    result = run(tmp_path)

    assert [(r.path, r.is_dir, r.source) for r in result.excluded] == [
        ("venv", True, SOURCE_PYTHON)
    ]
    assert result.probe_diagnostics == []


# ---------------------------------------------------------------------------
# AC-1.3 — root failures terminate the run
# ---------------------------------------------------------------------------


def test_missing_root_terminates_the_run(tmp_path):
    """AC-1.3: the error names the path and the reason."""
    missing = tmp_path / "nope"
    with pytest.raises(RootError) as excinfo:
        discover(missing, build_ruleset(tmp_path, warn=no_warnings))

    assert str(missing) in str(excinfo.value)
    assert "No such file or directory" in str(excinfo.value)


@pytest.mark.skipif(AS_ROOT, reason="root lists mode-000 directories regardless")
def test_unreadable_root_terminates_the_run(tmp_path):
    """AC-1.3, the permission half."""
    root = tmp_path / "root"
    make_tree(root, {"app.py": ""})
    ruleset = build_ruleset(root, warn=no_warnings)
    root.chmod(0o000)
    try:
        with pytest.raises(RootError) as excinfo:
            discover(root, ruleset)
    finally:
        root.chmod(0o755)

    assert str(root) in str(excinfo.value)
    assert "Permission denied" in str(excinfo.value)


def test_a_file_as_root_terminates_the_run(tmp_path):
    make_tree(tmp_path, {"app.py": ""})
    with pytest.raises(RootError, match="not a directory"):
        discover(tmp_path / "app.py", build_ruleset(tmp_path, warn=no_warnings))


@pytest.mark.skipif(AS_ROOT, reason="root lists mode-000 directories regardless")
def test_an_unreadable_subdirectory_is_a_diagnostic_not_a_termination(tmp_path):
    """One unreadable corner must not hide the rest of the codebase (FR-6's posture)."""
    make_tree(tmp_path, {"app.py": "", "locked/hidden.py": "", "pkg/mod.py": ""})
    (tmp_path / "locked").chmod(0o000)
    try:
        result = run(tmp_path)
    finally:
        (tmp_path / "locked").chmod(0o755)

    assert found(result) == ["app.py", "pkg/mod.py"]
    assert [(d.kind, d.path) for d in result.probe_diagnostics] == [("probe_failure", "locked")]


# ---------------------------------------------------------------------------
# EC-10 — nothing to analyze is a complete run
# ---------------------------------------------------------------------------


def test_an_empty_root_completes_with_zero_candidates(tmp_path):
    result = run(tmp_path)

    assert result == DiscoveryResult(
        root=tmp_path.resolve(), candidates=[], excluded=[], probe_diagnostics=[]
    )


def test_a_root_without_recognized_sources_completes_with_zero_candidates(tmp_path):
    make_tree(tmp_path, {"docs/guide.md": "", "Makefile": "all:\n", "data/rows.csv": "a,b\n"})
    result = run(tmp_path)

    assert result.candidates == []
    assert result.excluded == []
    assert result.probe_diagnostics == []


# ---------------------------------------------------------------------------
# FR-2 / §8-O1 — pruning
# ---------------------------------------------------------------------------


def test_a_pruned_directory_costs_one_record_and_no_enumeration(tmp_path, monkeypatch):
    """§8-O1 + D17: one entry for the directory, and nothing beneath it is even listed."""
    tree = {f"venv/lib/site-packages/pkg{index // 50}/mod{index}.py": "" for index in range(500)}
    tree["app.py"] = ""
    make_tree(tmp_path, tree)

    scanned: list[str] = []
    real_scandir = os.scandir

    def recording_scandir(path):
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(discovery.os, "scandir", recording_scandir)
    result = run(tmp_path)

    assert found(result) == ["app.py"]
    assert [(r.path, r.is_dir, r.pattern, r.source) for r in result.excluded] == [
        ("venv", True, "venv/", SOURCE_PYTHON)
    ]
    # The decisive assertion: the pruned subtree was never listed, so its 500 files cost
    # nothing at all — not one stat, not one exclusion record. (The root and `pkg` were
    # listed, which is what proves the instrumentation is in force.)
    assert str(tmp_path.resolve()) in scanned
    assert not any("venv" in path for path in scanned)


def test_file_level_exclusions_are_recorded_per_file(tmp_path):
    """A file pattern is not a prune: each matched file is its own entry (D17)."""
    make_tree(tmp_path, {"a_pb2.py": "", "b_pb2.py": "", "keep.py": "", "pkg/c_pb2.py": ""})
    result = run(tmp_path, exclude=["*_pb2.py"])

    assert found(result) == ["keep.py"]
    assert [(r.path, r.is_dir, r.source) for r in result.excluded] == [
        ("a_pb2.py", False, SOURCE_USER_EXCLUDE),
        ("b_pb2.py", False, SOURCE_USER_EXCLUDE),
        ("pkg/c_pb2.py", False, SOURCE_USER_EXCLUDE),
    ]


def test_nothing_inside_an_excluded_directory_is_probed(tmp_path):
    """A probe inside pruned territory would both cost I/O and warn about non-code."""
    make_tree(tmp_path, {"venv/bin/activate": "#!/usr/bin/env python3\n", "app.py": ""})
    result = run(tmp_path)

    assert found(result) == ["app.py"]
    assert result.probe_diagnostics == []


def test_reinclude_restores_discovery_under_a_default_excluded_directory(tmp_path):
    """AC-4.1's data side, seen from the walk: the tree is descended again."""
    make_tree(tmp_path, {"build/generated/mod.py": "", "venv/x.py": "", "app.py": ""})
    result = run(tmp_path, reinclude=["build/"])

    assert found(result) == ["app.py", "build/generated/mod.py"]
    assert [record.path for record in result.excluded] == ["venv"]


def test_gitignored_python_files_are_excluded_and_attributed(tmp_path):
    """AC-3.1 seen end-to-end: the walk honours the .gitignore layer too."""
    make_tree(
        tmp_path,
        {
            ".gitignore": "generated/\n",
            "app.py": "",
            "generated/pb2.py": "",
            "generated/x/y.py": "",
        },
    )
    result = run(tmp_path)

    assert found(result) == ["app.py"]
    assert [(r.path, r.source) for r in result.excluded] == [("generated", "gitignore:.gitignore")]


# ---------------------------------------------------------------------------
# D12 — determinism
# ---------------------------------------------------------------------------


def test_two_walks_over_one_tree_agree(tmp_path):
    make_tree(
        tmp_path,
        {
            "z.py": "",
            "a.py": "",
            "pkg/m.py": "",
            "pkg/deep/n.py": "",
            "venv/x.py": "",
            "gen/a.py": "",
            "tool": "#!/usr/bin/env python3\n",
            ".gitignore": "gen/\n",
        },
    )
    (tmp_path / "outside.py").symlink_to(tmp_path.parent / "elsewhere.py")

    first = run(tmp_path)
    second = run(tmp_path)

    assert first == second
    assert found(first) == sorted(found(first))
    assert [record.path for record in first.excluded] == sorted(
        record.path for record in first.excluded
    )
