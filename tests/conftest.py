"""Shared test support.

Its presence also puts `tests/` on `sys.path`, so any test module can
`from stub_adapter import StubAdapter` (the task 1.5 test double for design.md §3.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest


def write_tree(root: Path, files: Mapping[str, str]) -> Path:
    """Materialize `{relpath: content}` under `root`, creating parents as needed."""
    for relpath, content in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def tree(tmp_path: Path):
    """Build a fixture codebase under a fresh temporary root."""

    def build(files: Mapping[str, str], name: str = "codebase") -> Path:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        return write_tree(root, files)

    return build


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """A run's output directory, outside the analyzed tree (design.md §5.1)."""
    path = tmp_path / "out"
    path.mkdir()
    return path
