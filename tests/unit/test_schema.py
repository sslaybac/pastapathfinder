"""The data model: node-ID grammar, DDL vocabulary, row shapes, fragment validation.

design.md §4.1, §4.2, §4.3, §3.8; requirements FR-21 (AC-21.1), FR-22 (AC-22.1/22.2),
FR-23 (AC-23.2), FR-37 (AC-37.2), FR-19 (AC-19.2's caveat constant).
"""

import re

import pytest

from pastapathfinder.schema import (
    DDL,
    DEADCODE_CAVEAT,
    DIAG_KINDS,
    EDGE_KINDS,
    LANGUAGES,
    NODE_KINDS,
    SCHEMA_VERSION,
    Diag,
    EdgeRow,
    FileRecord,
    FragmentValidationError,
    GraphFragment,
    NodeRow,
    SkipRecord,
    attrs_json,
    is_valid_node_id,
    validate_fragment,
)

HASH = "0" * 64
DDL_TEXT = "\n".join(DDL)


def node(node_id: str, kind: str = "function", **overrides) -> NodeRow:
    fields = {"name": node_id.rpartition(".")[2], "language": "python", **overrides}
    return NodeRow(id=node_id, kind=kind, **fields)


def fragment(*, nodes=(), edges=(), path: str = "pkg/mod.py") -> GraphFragment:
    return GraphFragment(
        file=FileRecord(path=path, content_hash=HASH, status="analyzed"),
        nodes=list(nodes),
        edges=list(edges),
    )


# ---------------------------------------------------------------------------
# §4.1 node-ID grammar (FR-22)
# ---------------------------------------------------------------------------

WELL_FORMED_IDS = [
    "python:file:mod.py",  # file node at the root
    "python:file:pkg/sub/mod.py",  # file node, POSIX relpath
    "python:file:pkg/weird name.py",  # spaces are legal in paths
    "python:pkg.mod.<module>",  # D16 module-body node
    "python:pkg.mod.func",  # function
    "python:pkg.mod.Class.method",  # method
    "python:pkg.mod.func@42",  # @line collision suffix
    "python:pkg.mod.func.<lambda#0>",  # per-scope lambda counter
    "python:pkg.mod.Class.method.<lambda#12>@7",  # both suffix forms at once
    "python:mod",  # single-segment qualname (top-level module)
    "python:os.path.join",  # external leaf node (plain qualified name)
    "python:pkg.mod.ünïcode",  # non-ASCII identifiers are legal Python
    "python:entry:main_block:pkg.mod.<module>@1",
    "python:entry:console_script:pkg.cli.main@10",
    "python:entry:route_flask_fastapi:app.views.index@22",
    "python:entry:route_django:app.views.detail@30",
]

#: §4.1's `module` is built from *path* segments (amended 2026-07-22, D22), so every
#: filename discovery can hand the adapter derives a well-formed ID. The first two are
#: real: 23 files of the pinned Django benchmark are numbered migrations, and dashed
#: directory names are everywhere.
WELL_FORMED_PATH_DERIVED_IDS = [
    "python:django.contrib.auth.migrations.0001_initial.<module>",
    "python:my-app.mod.func@12",
    "python:pkg.locale.is.formats.<module>",  # a package named after a keyword
    "python:pkg.mod v2.func",  # spaces are legal in filenames, so also in module names
]

#: Forms D22's widening made legal that nothing in the pipeline produces. They are listed
#: rather than dropped so the consequence stays visible: `normalize.py` is the only
#: producer of the bracketed segments, and that is where their shape is tested.
LEGAL_BUT_UNPRODUCED_IDS = [
    "python:pkg.mod.<lambda>",  # unnumbered — normalize.py always numbers
    "python:<module>.pkg",  # a module-body segment in leading position
]

MALFORMED_IDS = [
    "pkg.mod.func",  # AC-22.2: no language namespace
    ":pkg.mod.func",  # empty namespace
    "java:pkg.mod.func",  # v1 knows one language
    "python:",  # no local part
    "python:file:",  # empty relpath
    "python:file:/abs/mod.py",  # relpath must be root-relative
    "python:file:../escape.py",  # ... and normalized
    "python:file:pkg\\win.py",  # ... and POSIX-style
    "python:pkg..mod",  # empty segment
    "python:.pkg.mod",  # ... in leading position
    "python:pkg.mod.",  # ... and trailing
    "python:pkg/mod.func",  # a path separator never survives the §4.1 derivation
    "python:pkg.mod.func@",  # line suffix without a line
    "python:pkg.mod.func@x",
    "python:pkg.mod@1.func",  # the line suffix is last, or it is not a suffix
    "python:entry:main_block:pkg.mod.<module>",  # entry IDs carry a line
    "python:entry:no_such_detector:pkg.mod.func@1",
    "PYTHON:pkg.mod.func",  # the namespace token is exact
    "python:pkg.mod.func\n",  # no trailing newline sneaks past fullmatch
]


@pytest.mark.parametrize("node_id", WELL_FORMED_IDS)
def test_grammar_accepts_every_form_of_id(node_id):
    """AC-22.1: every ID the pipeline emits is a §4.1 ID."""
    assert is_valid_node_id(node_id)


@pytest.mark.parametrize("node_id", WELL_FORMED_PATH_DERIVED_IDS + LEGAL_BUT_UNPRODUCED_IDS)
def test_grammar_accepts_path_derived_module_names(node_id):
    """D22: `module` is dotted *path* segments, so no legal filename fails validation."""
    assert is_valid_node_id(node_id)


@pytest.mark.parametrize("node_id", MALFORMED_IDS)
def test_grammar_rejects_malformed_ids(node_id):
    """AC-22.2: a non-namespaced or otherwise malformed ID never validates."""
    assert not is_valid_node_id(node_id)


def test_grammar_rejects_non_strings():
    assert not is_valid_node_id(None)
    assert not is_valid_node_id(17)


# ---------------------------------------------------------------------------
# §4.2 DDL vocabulary (FR-21)
# ---------------------------------------------------------------------------


def test_kind_check_sets_are_the_generic_vocabulary_verbatim():
    """AC-21.1: no Python-specific concept appears as a node or edge *type*."""
    assert "CHECK (kind IN ('file','module','function','class','entry_point'))" in DDL_TEXT
    assert "CHECK (kind IN ('calls','contains','imports'))" in DDL_TEXT

    declared = re.findall(r"kind IN \(([^)]*)\)", DDL_TEXT)
    assert len(declared) == 2
    kinds = {token.strip("'") for group in declared for token in group.split(",")}
    assert kinds == set(NODE_KINDS) | set(EDGE_KINDS)

    # The v1 language's own vocabulary — the concepts D4 says ride in `attrs` — must not
    # have leaked into a kind.
    python_specific = {
        "method",
        "decorator",
        "lambda",
        "module_body",
        "package",
        "coroutine",
        "route",
        "main_block",
        "python",
    }
    assert kinds & python_specific == set()


def test_status_check_set_matches_the_reported_categories():
    """FR-7: `files` rows are analyzed or skipped; excluded paths are not files rows."""
    assert "CHECK (status IN ('analyzed','skipped'))" in DDL_TEXT


def test_attrs_columns_default_to_the_empty_object():
    """AC-21.2: a row with no language-specific detail is still a legal row."""
    assert DDL_TEXT.count("attrs TEXT NOT NULL DEFAULT '{}'") == 2
    assert "is_ambiguous INTEGER NOT NULL DEFAULT 0" in DDL_TEXT


def test_schema_version_is_one():
    assert SCHEMA_VERSION == 1


def test_languages_and_diag_kinds_are_the_documented_sets():
    assert LANGUAGES == ("python",)
    assert "unresolved_call" in DIAG_KINDS
    assert len(set(DIAG_KINDS)) == len(DIAG_KINDS)


# ---------------------------------------------------------------------------
# FR-19's caveat constant
# ---------------------------------------------------------------------------


def test_deadcode_caveat_states_the_approximation():
    """FR-19/AC-19.2: the caveat every rendering must carry says what it must say."""
    assert isinstance(DEADCODE_CAVEAT, str)
    lowered = DEADCODE_CAVEAT.lower()
    assert "approximate" in lowered
    assert "dynamic" in lowered
    assert "getattr" in lowered


# ---------------------------------------------------------------------------
# Canonical JSON (D12)
# ---------------------------------------------------------------------------


def test_attrs_json_sorts_keys_and_omits_incidental_whitespace():
    assert attrs_json({"b": 1, "a": [2, 1]}) == '{"a":[2,1],"b":1}'
    assert attrs_json({"a": 1, "b": 2}) == attrs_json({"b": 2, "a": 1})


# ---------------------------------------------------------------------------
# Row-shape guards
# ---------------------------------------------------------------------------


def test_skip_record_rejects_an_undefined_reason():
    assert SkipRecord("a.py", "parse_error", "line 3").reason == "parse_error"
    with pytest.raises(ValueError, match="unknown skip reason"):
        SkipRecord("a.py", "vibes", "")


def test_diag_rejects_an_undefined_kind():
    assert Diag("unresolved_call", path="a.py", line=1, col=2).kind == "unresolved_call"
    with pytest.raises(ValueError, match="unknown diagnostic kind"):
        Diag("mystery")


# ---------------------------------------------------------------------------
# validate_fragment (AC-22.2, AC-23.2)
# ---------------------------------------------------------------------------


def test_a_conformant_fragment_validates():
    validate_fragment(
        fragment(
            nodes=[
                node("python:file:pkg/mod.py", "file", name="mod.py", file_path="pkg/mod.py"),
                node("python:pkg.mod.func", file_path="pkg/mod.py", start_line=1, end_line=4),
            ],
            edges=[
                EdgeRow(
                    src="python:file:pkg/mod.py",
                    dst="python:pkg.mod.func",
                    kind="contains",
                    src_file="pkg/mod.py",
                )
            ],
        )
    )


def test_a_non_namespaced_node_id_is_rejected_by_name():
    """AC-22.2: the error names the offending row."""
    offender = node("pkg.mod.func")
    with pytest.raises(FragmentValidationError) as raised:
        validate_fragment(fragment(nodes=[offender]))
    message = str(raised.value)
    assert "§4.1 grammar" in message
    assert "pkg.mod.func" in message
    assert "pkg/mod.py" in message  # the fragment it came from


def test_an_unknown_node_kind_is_rejected_by_name():
    """AC-23.2: the fragment is rejected rather than written as malformed data."""
    offender = node("python:pkg.mod.func", kind="decorator")
    with pytest.raises(FragmentValidationError) as raised:
        validate_fragment(fragment(nodes=[offender]))
    assert "unknown node kind" in str(raised.value)
    assert "decorator" in str(raised.value)


def test_an_unknown_edge_kind_is_rejected():
    with pytest.raises(FragmentValidationError, match="unknown edge kind"):
        validate_fragment(
            fragment(
                nodes=[node("python:pkg.mod.func")],
                edges=[EdgeRow("python:pkg.mod.func", "python:pkg.mod.func", "inherits")],
            )
        )


def test_an_edge_endpoint_absent_from_fragment_and_index_is_rejected_by_name():
    """AC-23.2: dangling endpoints never reach the store."""
    edge = EdgeRow("python:pkg.mod.func", "python:pkg.other.gone", "calls")
    with pytest.raises(FragmentValidationError) as raised:
        validate_fragment(fragment(nodes=[node("python:pkg.mod.func")], edges=[edge]))
    message = str(raised.value)
    assert "python:pkg.other.gone" in message
    assert "is not a known node" in message


def test_an_edge_endpoint_known_to_the_index_is_accepted():
    edge = EdgeRow("python:pkg.mod.func", "python:pkg.other.helper", "calls")
    validate_fragment(
        fragment(nodes=[node("python:pkg.mod.func")], edges=[edge]),
        known_ids={"python:pkg.other.helper"},
    )


def test_an_external_node_may_not_carry_a_source_location():
    """AC-37.2: external nodes have no span, so none may be fabricated for them."""
    with pytest.raises(FragmentValidationError, match="AC-37.2"):
        validate_fragment(
            fragment(
                nodes=[
                    node(
                        "python:requests.get",
                        is_external=1,
                        file_path="pkg/mod.py",
                        start_line=3,
                        end_line=3,
                    )
                ]
            )
        )


def test_span_without_a_line_number_is_allowed_but_an_inverted_span_is_not():
    """AC-37.3: a missing span is legal; an impossible one is not."""
    validate_fragment(fragment(nodes=[node("python:pkg.mod.func", file_path="pkg/mod.py")]))
    with pytest.raises(FragmentValidationError, match="end_line precedes start_line"):
        validate_fragment(
            fragment(
                nodes=[
                    node("python:pkg.mod.func", file_path="pkg/mod.py", start_line=9, end_line=2)
                ]
            )
        )


def test_file_record_rules():
    with pytest.raises(FragmentValidationError, match="content_hash"):
        validate_fragment(GraphFragment(file=FileRecord("pkg/mod.py", "not-a-digest", "analyzed")))
    with pytest.raises(FragmentValidationError, match="unknown file status"):
        validate_fragment(GraphFragment(file=FileRecord("pkg/mod.py", HASH, "excluded")))
    with pytest.raises(FragmentValidationError, match="skip_reason must be one of"):
        validate_fragment(GraphFragment(file=FileRecord("pkg/mod.py", HASH, "skipped")))
    with pytest.raises(FragmentValidationError, match="root-relative POSIX path"):
        validate_fragment(GraphFragment(file=FileRecord("/abs/mod.py", HASH, "analyzed")))
    validate_fragment(GraphFragment(file=FileRecord("pkg/mod.py", HASH, "skipped", "parse_error")))


def test_attrs_must_be_a_json_serializable_mapping():
    with pytest.raises(FragmentValidationError, match="not JSON-serializable"):
        validate_fragment(fragment(nodes=[node("python:pkg.mod.func", attrs={"seen": {1, 2}})]))


def test_flags_are_zero_or_one():
    with pytest.raises(FragmentValidationError, match="is_external must be 0 or 1"):
        validate_fragment(fragment(nodes=[node("python:pkg.mod.func", is_external=2)]))
    with pytest.raises(FragmentValidationError, match="is_ambiguous must be 0 or 1"):
        validate_fragment(
            fragment(
                nodes=[node("python:pkg.mod.func")],
                edges=[
                    EdgeRow("python:pkg.mod.func", "python:pkg.mod.func", "calls", is_ambiguous=7)
                ],
            )
        )


def test_language_must_agree_with_the_id_namespace():
    with pytest.raises(FragmentValidationError, match="unknown language"):
        validate_fragment(
            fragment(nodes=[NodeRow("python:pkg.mod.func", "function", "func", "java")])
        )
