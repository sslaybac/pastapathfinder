"""The `if __name__ == "__main__":` detector (design.md §3.7; FR-9).

design.md §3.7 (`main_block` rule), §4.1 (the module-body node it targets), D16;
requirements FR-9 (AC-9.1, AC-9.2).

A module's `__main__` guard is the code CPython runs when the file is invoked as a script,
so the entry point it names is the module *body* — the executable aspect D16 gave its own
`module` node — not any single function. The detector emits one `entry_point` node per
guard, each with a `calls` edge to that module-body node.

AC-9.2 needs no code here: a file that failed to parse never becomes a `ModuleInput` (the
registry builds inputs only from files that parse under stdlib `ast`), so it is a skip and
this detector never sees it.
"""

from __future__ import annotations

import ast

from pastapathfinder.adapters.python.normalize import (
    code_node_id,
    module_body_qualname,
    module_name,
)
from pastapathfinder.detectors.base import (
    DetectorOutput,
    ModuleDetector,
    ModuleInput,
    entry_point,
)

#: The string a `__main__` guard tests `__name__` against.
_DUNDER_MAIN = "__main__"


def _is_main_guard(test: ast.expr) -> bool:
    """True when `test` is `__name__ == "__main__"` in either operand order (§3.7).

    Only the `==` comparison is a guard: `is` is not what the idiom is written with, and
    the spec names `==` explicitly. A single comparator keeps a chained `a == b == c` from
    being read as a guard.
    """
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    return _is_dunder_name(left, right) or _is_dunder_name(right, left)


def _is_dunder_name(name_side: ast.expr, literal_side: ast.expr) -> bool:
    """True when `name_side` is the name `__name__` and `literal_side` the string `"__main__"`."""
    return (
        isinstance(name_side, ast.Name)
        and name_side.id == "__name__"
        and isinstance(literal_side, ast.Constant)
        and literal_side.value == _DUNDER_MAIN
    )


class MainBlockDetector(ModuleDetector):
    """Emits an `entry_point` targeting the module body for each `__main__` guard (FR-9)."""

    name = "main_block"

    def detect(self, module: ModuleInput) -> DetectorOutput:
        derived = module_name(module.module_path)
        target = code_node_id(module_body_qualname(derived))
        output = DetectorOutput()
        # `ast.walk` finds guards wherever they sit; the idiom is top-level, but a guard in
        # any scope names the same module body, so there is no reason to restrict the search.
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.If) or not _is_main_guard(node.test):
                continue
            entry, edge = entry_point(
                detector=self.name,
                qualname=derived,
                line=node.lineno,
                name=f"{derived}:{_DUNDER_MAIN}",
                target=target,
                file_path=module.module_path,
                start_line=node.lineno,
                end_line=node.end_lineno,
            )
            output.nodes.append(entry)
            output.edges.append(edge)
        return output
