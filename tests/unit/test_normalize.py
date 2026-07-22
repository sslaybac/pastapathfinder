"""Node-ID construction: the §4.1 grammar's one producer.

design.md §3.5 (`normalize`), §4.1, D16, D22; requirements FR-22 (AC-22.1).
"""

import pytest

from pastapathfinder.adapters.python import normalize
from pastapathfinder.schema import is_valid_node_id, node_id_language

RELPATHS = [
    ("mod.py", "mod"),
    ("pkg/mod.py", "pkg.mod"),
    ("pkg/sub/mod.py", "pkg.sub.mod"),
    ("pkg/__init__.py", "pkg"),
    ("pkg/sub/__init__.py", "pkg.sub"),
    ("__init__.py", "__init__"),  # the package is the analysis root; its name is not in
    ("bin/tool", "bin.tool"),  # an FR-1 rule (b) shebang script has nothing to strip
    ("pkg/migrations/0001_initial.py", "pkg.migrations.0001_initial"),  # D22
    ("my-app/mod.py", "my-app.mod"),  # D22
    ("pkg/locale/is/formats.py", "pkg.locale.is.formats"),  # a keyword-named package
]


@pytest.mark.parametrize(("relpath", "expected"), RELPATHS)
def test_module_names_derive_from_the_relpath(relpath, expected):
    """§4.1: strip `.py`, separators to dots, drop a trailing `.__init__`."""
    assert normalize.module_name(relpath) == expected


@pytest.mark.parametrize(("relpath", "module"), RELPATHS)
def test_every_derived_id_is_a_valid_node_id(relpath, module):
    """AC-22.1: every form this module produces validates, for every legal filename."""
    for node_id in (
        normalize.file_node_id(relpath),
        normalize.code_node_id(normalize.module_body_qualname(module)),
        normalize.code_node_id(normalize.child_qualname(module, "func")),
        normalize.code_node_id(normalize.child_qualname(module, "func"), 42),
        normalize.code_node_id(
            normalize.child_qualname(normalize.child_qualname(module, "func"), "<lambda#3>")
        ),
    ):
        assert is_valid_node_id(node_id), node_id
        assert node_id_language(node_id) == "python"


def test_file_and_code_id_forms():
    assert normalize.file_node_id("pkg/mod.py") == "python:file:pkg/mod.py"
    assert normalize.code_node_id("pkg.mod.f") == "python:pkg.mod.f"
    assert normalize.code_node_id("pkg.mod.f", 7) == "python:pkg.mod.f@7"
    assert normalize.module_body_qualname("pkg.mod") == "pkg.mod.<module>"
    assert normalize.lambda_segment(0) == "<lambda#0>"
    assert normalize.lambda_segment(12) == "<lambda#12>"


def test_a_unique_qualname_takes_no_line_suffix():
    """§4.1: the suffix appears "only on collision"."""
    assert normalize.code_node_ids([("m.a", 1), ("m.b", 4)]) == ["python:m.a", "python:m.b"]


def test_every_member_of_a_colliding_group_takes_the_suffix():
    """Suffixing only the later ones would rename the first the day a second appears."""
    ids = normalize.code_node_ids([("m.dup", 3), ("m.other", 5), ("m.dup", 9)])
    assert ids == ["python:m.dup@3", "python:m.other", "python:m.dup@9"]


def test_a_colliding_member_without_a_span_keeps_the_bare_qualname():
    """AC-37.3 leaves nothing to suffix with; `extract.py` resolves the duplicate."""
    assert normalize.code_node_ids([("m.dup", None), ("m.dup", 9)]) == [
        "python:m.dup",
        "python:m.dup@9",
    ]
