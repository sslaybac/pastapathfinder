"""Node-ID construction per the §4.1 grammar (design.md §3.5, §4.1; FR-22).

design.md §3.5 (`normalize`), §4.1 (node-ID grammar, normative), §4.2 (reserved `attrs`
keys), D16, D22; requirements FR-21, FR-22 (AC-22.1/22.2), FR-37.

Every ID the Python adapter writes is built here, so the grammar has one producer and
`schema.is_valid_node_id()` is its one checker. Nothing in this module touches mypy: an ID
is a function of the file's root-relative path and the lexical shape of what is in it.

**Module names are path-derived, not engine-derived.** §4.1 says `module` comes from the
relpath — strip `.py`, separators to dots, drop a trailing `.__init__` — and that is a
different string from the module name the engine imports a file as. Analyzing Django's
`django/` package, `db/models/query.py` is `db.models.query` here and
`django.db.models.query` to mypy (design.md §3.5's trap 2, `FINDINGS-mypy.md` §2). The
two are deliberately kept apart: IDs stay stable under where the analysis root is placed,
which is what makes them root-relative in every artifact.

Since a module segment is a path segment (D22), it needs no relation to Python's
identifier rules: `0001_initial.py`, `my-app/`, and a package named `is` all derive legal
module names, and 23 files of the pinned Django benchmark are the first shape.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pastapathfinder.schema import LANGUAGE_PYTHON

#: §4.1's module-body segment. D16 makes the node it names a first-class `module` node.
MODULE_BODY_SEGMENT = "<module>"

#: §4.2's reserved `attrs.python_role` value for that node.
PYTHON_ROLE_MODULE_BODY = "module_body"


def module_name(relpath: str) -> str:
    """§4.1's derivation: strip `.py`, separators to dots, drop a trailing `.__init__`.

    A root-level `__init__.py` keeps the name `__init__`: the package it initializes is
    the analysis root itself, whose name is not part of any relpath and so is not part of
    any ID.
    """
    name = relpath.removesuffix(".py").replace("/", ".")
    return name.removesuffix(".__init__") if name.endswith(".__init__") else name


def file_node_id(relpath: str) -> str:
    """The `file:` form: `python:file:<root-relative POSIX path>`."""
    return f"{LANGUAGE_PYTHON}:file:{relpath}"


def module_body_qualname(module: str) -> str:
    """The module-body node's qualname, `<module>.<module>` in §4.1's notation (D16)."""
    return f"{module}.{MODULE_BODY_SEGMENT}"


def lambda_segment(index: int) -> str:
    """`<lambda#N>` — N counts lambdas within one enclosing scope, in source order."""
    return f"<lambda#{index}>"


def child_qualname(prefix: str, segment: str) -> str:
    """`prefix.segment` — the qualname of something defined inside `prefix`."""
    return f"{prefix}.{segment}"


def code_node_id(qualname: str, line: int | None = None) -> str:
    """The code-node form: `python:<qualname>`, with `@line` only when disambiguating."""
    return (
        f"{LANGUAGE_PYTHON}:{qualname}" if line is None else f"{LANGUAGE_PYTHON}:{qualname}@{line}"
    )


def code_node_ids(entries: Sequence[tuple[str, int | None]]) -> list[str]:
    """IDs for one file's code nodes: `(qualname, start_line)` in, IDs out.

    §4.1 suffixes a code node's ID with `@start_line` "only on collision". Collision is a
    property of the *group*, not of who was walked first: when a qualname occurs more than
    once in a file — conditional definitions, `@overload` items, a property and its setter
    — **every** member of the group takes the suffix. Suffixing only the later ones would
    make an ID depend on walk order and would silently rename the first definition the day
    a second one appears above it.

    A member whose start line is unknown (AC-37.3) cannot be suffixed; it keeps the bare
    qualname and the caller resolves the resulting duplicate (`extract.py` keeps the first
    and records the loss in diagnostics). Two same-named definitions with no spans at all
    is not a shape mypy produces; it is handled rather than assumed away.
    """
    occurrences = Counter(qualname for qualname, _ in entries)
    return [
        code_node_id(qualname, line if occurrences[qualname] > 1 else None)
        for qualname, line in entries
    ]
