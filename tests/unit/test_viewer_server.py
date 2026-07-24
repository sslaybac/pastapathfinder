"""The viewer's read-only JSON API (specs/tasks.md task 5.1).

design.md §3.11 (`server`), §5.2 (the endpoints and their shapes), D7, D7a, D20;
requirements FR-25 (AC-25.1/25.2), FR-20 (AC-20.1/20.2), FR-26-28 (the data half), FR-33,
FR-39 (AC-39.2), EC-13, EC-15.

The index here is built by hand for the same reason task 3.5's is: an API response is a
claim about a graph, and a graph written out node by node is one whose expected answer can
be asserted exactly rather than approximately.

Three of these tests assert properties that are otherwise only claims:

* `test_the_server_opens_no_file_but_the_index` records every file the process opens while
  each endpoint is answered — D20's invariant, and the thing that makes AC-25.1 testable.
* `test_the_server_binds_only_loopback` runs the real server and reads the kernel's own
  listening table, rather than trusting the argument passed to `app.run`.
* `test_no_request_leaves_the_machine` fails if any endpoint opens a socket (FR-33).
"""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import socket
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pastapathfinder import cli, queries, reports
from pastapathfinder.index import INDEX_FILENAME, full_write, open_index
from pastapathfinder.schema import (
    DEADCODE_CAVEAT,
    SCHEMA_VERSION,
    EdgeRow,
    FileRecord,
    GraphFragment,
    NodeRow,
)
from pastapathfinder.viewer import DEFAULT_PORT, HOST, server

META = {
    "tool_version": "0.1.0",
    "engine": "mypy",
    "engine_version": "2.3.0",
    "root_path": "/srv/target",
    "created_at": "2026-07-24T09:00:00+00:00",
    "run_id": "11111111-2222-3333-4444-555555555555",
}

APP = "pkg/app.py"

#: design.md §5.2's field sets, transcribed — the same transcription task 3.5's tests
#: assert the CLI against, so both surfaces are checked against the document rather than
#: against each other's habits.
META_FIELDS = {"schema_version", "tool_version", "root_path", "created_at", "counts"}
COUNT_FIELDS = {"files", "nodes", "edges", "entry_points"}
ENTRY_POINT_FIELDS = {"id", "name", "detector", "target_id", "file_path", "start_line"}
NODE_FIELDS = {
    "id",
    "kind",
    "name",
    "file_path",
    "start_line",
    "end_line",
    "is_external",
    "reachable",
    "attrs",
}
SLICE_FIELDS = {"nodes", "edges", "truncated", "frontier"}
SLICE_EDGE_FIELDS = {"src", "dst", "is_ambiguous"}
DEADCODE_FIELDS = {"format_version", "caveat", "no_entry_points_warning", "unreachable"}

ENDPOINTS = (
    "/api/meta",
    "/api/entry-points",
    "/api/nodes?search=main",
    "/api/nodes/python:pkg.app.main",
    "/api/slice?from=python:pkg.app.main&direction=forward",
    "/api/dead-code",
)


# ---------------------------------------------------------------------------
# A graph whose every answer is computable by eye
# ---------------------------------------------------------------------------

FILE = NodeRow(id=f"python:file:{APP}", kind="file", name=APP, language="python", file_path=APP)
MODULE = NodeRow(
    id="python:pkg.app.<module>",
    kind="module",
    name="pkg.app",
    language="python",
    file_path=APP,
    start_line=1,
    end_line=40,
    attrs={"python_role": "module_body"},
)
MAIN = NodeRow(
    id="python:pkg.app.main",
    kind="function",
    name="main",
    language="python",
    file_path=APP,
    start_line=10,
    end_line=12,
)
HELPER = NodeRow(
    id="python:pkg.app.helper",
    kind="function",
    name="helper",
    language="python",
    file_path=APP,
    start_line=20,
    end_line=22,
)
ORPHAN = NodeRow(
    id="python:pkg.app.orphan",
    kind="function",
    name="orphan",
    language="python",
    file_path=APP,
    start_line=30,
    end_line=32,
)
EXT = NodeRow(
    id="python:os.path.join", kind="function", name="join", language="python", is_external=1
)
ENTRY = NodeRow(
    id="python:entry:main_block:pkg.app@38",
    kind="entry_point",
    name="pkg.app:__main__",
    language="python",
    file_path=APP,
    start_line=38,
    end_line=38,
    attrs={"detector": "main_block"},
)
ENTRY_B = NodeRow(
    id="python:entry:console_script:pkg.app.main@1",
    kind="entry_point",
    name="app",
    language="python",
    file_path="pyproject.toml",
    start_line=1,
    end_line=1,
    attrs={"detector": "console_script"},
)


def _calls(src: NodeRow, dst: NodeRow, *, ambiguous: int = 0) -> EdgeRow:
    return EdgeRow(src=src.id, dst=dst.id, kind="calls", src_file=APP, is_ambiguous=ambiguous)


def _contains(dst: NodeRow) -> EdgeRow:
    return EdgeRow(src=FILE.id, dst=dst.id, kind="contains", src_file=APP)


@pytest.fixture
def indexed(tmp_path: Path) -> Path:
    """An output directory holding an index and **no reports directory** (D20).

    entry(main_block) → <module> → main → helper → os.path.join (external)
    entry(console_script) → main
    orphan: called by nothing, so it is the dead code.
    """
    out = tmp_path / "out"
    out.mkdir()
    fragment = GraphFragment(
        file=FileRecord(APP, hashlib.sha256(APP.encode()).hexdigest(), "analyzed"),
        nodes=[FILE, MODULE, MAIN, HELPER, ORPHAN, EXT],
        edges=[
            *(_contains(node) for node in (MODULE, MAIN, HELPER, ORPHAN)),
            _calls(MODULE, MAIN),
            _calls(MAIN, HELPER),
            _calls(HELPER, EXT, ambiguous=1),
        ],
    )
    with full_write(out / INDEX_FILENAME, META) as store:
        store.write_fragments([fragment])
        store.write_rows([ENTRY, ENTRY_B], [_calls(ENTRY, MODULE), _calls(ENTRY_B, MAIN)])
    with open_index(out / INDEX_FILENAME) as index:
        queries.reachability(index)
    assert not (out / reports.REPORTS_DIRNAME).exists()
    return out


@pytest.fixture
def client(indexed: Path):
    app = server.create_app(server.index_file(indexed))
    app.testing = True
    return app.test_client()


def get(client, url: str) -> tuple[int, Any]:
    response = client.get(url)
    return response.status_code, response.get_json()


# ---------------------------------------------------------------------------
# The §5.2 endpoints answer from the index (FR-20, FR-25-28)
# ---------------------------------------------------------------------------


def test_meta_reports_the_index_provenance_and_its_size(client):
    status, body = get(client, "/api/meta")
    assert status == 200
    assert set(body) == META_FIELDS
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["tool_version"] == META["tool_version"]
    assert body["root_path"] == META["root_path"]
    assert body["created_at"] == META["created_at"]
    assert set(body["counts"]) == COUNT_FIELDS
    assert body["counts"] == {"files": 1, "nodes": 8, "edges": 9, "entry_points": 2}


def test_entry_points_are_listed_sorted_by_id(client):
    """FR-26's data (AC-26.1), in §5.2's stated order."""
    status, body = get(client, "/api/entry-points")
    assert status == 200
    ids = [entry["id"] for entry in body["entry_points"]]
    assert ids == sorted([ENTRY.id, ENTRY_B.id])
    assert all(set(entry) == ENTRY_POINT_FIELDS for entry in body["entry_points"])
    by_id = {entry["id"]: entry for entry in body["entry_points"]}
    assert by_id[ENTRY.id]["target_id"] == MODULE.id
    assert by_id[ENTRY_B.id]["detector"] == "console_script"


def test_a_node_is_served_with_its_source_location(client):
    """AC-27.3's data: the node panel's fields come from here."""
    status, body = get(client, f"/api/nodes/{MAIN.id}")
    assert status == 200
    assert set(body) == NODE_FIELDS
    assert (body["file_path"], body["start_line"], body["end_line"]) == (APP, 10, 12)
    assert body["reachable"] == 1


def test_an_external_leaf_is_served_without_a_span(client):
    """AC-37.2/FR-36: no file, no span — what the panel renders as "external"."""
    status, body = get(client, f"/api/nodes/{EXT.id}")
    assert status == 200
    assert body["is_external"] == 1
    assert body["file_path"] is None and body["start_line"] is None


def test_a_file_node_id_containing_slashes_is_addressable(client):
    """§4.1: IDs are URL-encoded in API paths, and a `file:` ID carries path separators."""
    status, body = get(client, f"/api/nodes/{FILE.id}")
    assert status == 200
    assert body["id"] == FILE.id

    quoted = urllib.parse.quote(FILE.id, safe="")
    assert "%2F" in quoted
    status, body = get(client, f"/api/nodes/{quoted}")
    assert status == 200
    assert body["id"] == FILE.id


def test_node_search_finds_by_id_and_by_name(client):
    """AC-26.2's alternative to the entry list: name a node, then slice from it (FR-17)."""
    status, body = get(client, "/api/nodes?search=helper")
    assert status == 200
    assert [node["id"] for node in body["nodes"]] == [HELPER.id]
    assert all(set(node) == NODE_FIELDS for node in body["nodes"])

    _, by_name = get(client, "/api/nodes?search=orphan")
    assert [node["id"] for node in by_name["nodes"]] == [ORPHAN.id]

    # The `file:` node is deliberately absent: its id and name spell the *path*
    # (`pkg/app.py`), not the module (§4.1), so a module-shaped search does not find it.
    _, everything = get(client, "/api/nodes?search=pkg.app")
    assert [node["id"] for node in everything["nodes"]] == sorted(
        [MODULE.id, MAIN.id, HELPER.id, ORPHAN.id, ENTRY.id, ENTRY_B.id]
    )


def test_node_search_honors_limit_and_returns_a_stable_prefix(client):
    _, first = get(client, "/api/nodes?search=pkg.app&limit=2")
    _, again = get(client, "/api/nodes?search=pkg.app&limit=2")
    assert len(first["nodes"]) == 2
    assert first == again
    _, unbounded = get(client, "/api/nodes?search=pkg.app")
    assert [n["id"] for n in first["nodes"]] == [n["id"] for n in unbounded["nodes"]][:2]


def test_node_search_treats_wildcards_as_literal_text(client):
    """`_` and `%` are LIKE's, not the user's: `app%` matches nothing here."""
    _, body = get(client, "/api/nodes?search=app%25")
    assert body["nodes"] == []
    _, underscore = get(client, "/api/nodes?search=pkg_app")
    assert underscore["nodes"] == []


def test_slice_answers_the_flagship_question_in_both_directions(client):
    """FR-15/FR-16 through the API: the subgraph, not the program."""
    status, forward = get(client, f"/api/slice?from={MAIN.id}&direction=forward")
    assert status == 200
    assert set(forward) == SLICE_FIELDS
    assert {node["id"] for node in forward["nodes"]} == {MAIN.id, HELPER.id, EXT.id}
    assert all(set(edge) == SLICE_EDGE_FIELDS for edge in forward["edges"])
    assert any(edge["is_ambiguous"] == 1 for edge in forward["edges"])  # FR-40 survives

    _, backward = get(client, f"/api/slice?from={HELPER.id}&direction=backward")
    assert {node["id"] for node in backward["nodes"]} == {
        HELPER.id,
        MAIN.id,
        MODULE.id,
        ENTRY.id,
        ENTRY_B.id,
    }


def test_slice_direction_defaults_to_forward(client):
    _, defaulted = get(client, f"/api/slice?from={MAIN.id}")
    _, explicit = get(client, f"/api/slice?from={MAIN.id}&direction=forward")
    assert defaulted == explicit


def test_slice_honors_max_nodes_and_says_so(client):
    """AC-28.2/FR-28: bounded visibly — `truncated` set and a non-empty `frontier`."""
    status, body = get(client, f"/api/slice?from={ENTRY.id}&direction=forward&max_nodes=2")
    assert status == 200
    assert len(body["nodes"]) == 2
    assert body["truncated"] is True
    assert body["frontier"]

    _, whole = get(client, f"/api/slice?from={ENTRY.id}&direction=forward")
    assert whole["truncated"] is False
    assert whole["frontier"] == []


def test_slice_max_nodes_defaults_to_the_single_definition_site(client, monkeypatch):
    """design.md §8-O2: the provisional bound is resolved, never re-copied."""
    seen: list[int] = []
    real = queries.slice

    def spy(index, node_id, direction=queries.FORWARD, max_nodes=queries.SLICE_MAX_NODES):
        seen.append(max_nodes)
        return real(index, node_id, direction, max_nodes)

    monkeypatch.setattr(queries, "slice", spy)
    get(client, f"/api/slice?from={MAIN.id}&direction=forward")
    assert seen == [queries.SLICE_MAX_NODES]


def test_an_empty_slice_is_a_successful_answer(client):
    """AC-15.2: no outgoing calls is a result — 200 with one node, not an error."""
    status, body = get(client, f"/api/slice?from={ORPHAN.id}&direction=forward")
    assert status == 200
    assert [node["id"] for node in body["nodes"]] == [ORPHAN.id]
    assert body["edges"] == []


def test_dead_code_is_recomputed_with_its_caveat_and_without_the_run_block(client):
    """D20: `queries.dead_code()`, `deadcode.json`'s shape minus the volatile `run*`."""
    status, body = get(client, "/api/dead-code")
    assert status == 200
    assert set(body) == DEADCODE_FIELDS
    assert reports.RUN_BLOCK_KEY not in body
    assert body["caveat"] == DEADCODE_CAVEAT
    assert body["no_entry_points_warning"] is False
    assert body["unreachable"] == [
        {"file": APP, "functions": [{"id": ORPHAN.id, "name": "orphan", "start_line": 30}]}
    ]


def test_responses_are_deterministic(client):
    """D12/FR-44's posture at the API surface: same request, same bytes."""
    for url in ENDPOINTS:
        assert client.get(url).get_data() == client.get(url).get_data()


# ---------------------------------------------------------------------------
# One code path with the CLI (§5.1's "the same structured shapes")
# ---------------------------------------------------------------------------


def test_the_api_and_the_cli_json_agree_for_the_same_query(client, indexed, capsys):
    """Task 3.5's other half: `--json` and the API are the same serializer, checked.

    Task 3.5 asserted the CLI emits exactly what `queries.*_json()` produces; this asserts
    the API does too, which makes the two surfaces equal by transitivity — and would fail
    the moment either one started decorating the payload on its way out.
    """
    pairs = [
        ("/api/entry-points", ("query", "entry-points")),
        (f"/api/nodes/{MAIN.id}", ("query", "node", MAIN.id)),
        ("/api/dead-code", ("query", "dead-code")),
        (
            f"/api/slice?from={MAIN.id}&direction=forward",
            ("query", "slice", "--from", MAIN.id, "--direction", "forward"),
        ),
    ]
    for url, argv in pairs:
        status, body = get(client, url)
        assert status == 200
        assert cli.main([*argv, "--out", str(indexed), "--json"]) == cli.EXIT_SUCCESS
        assert json.loads(capsys.readouterr().out) == body


@pytest.mark.parametrize(
    ("url", "argv", "code", "status"),
    [
        pytest.param(
            "/api/nodes/python:pkg.app.nonesuch",
            ("query", "node", "python:pkg.app.nonesuch"),
            queries.ERROR_UNKNOWN_NODE,
            404,
            id="unknown_node",
        ),
        pytest.param(
            f"/api/slice?from={FILE.id}&direction=forward",
            ("query", "slice", "--from", FILE.id, "--direction", "forward"),
            queries.ERROR_NOT_SLICEABLE,
            400,
            id="not_sliceable",
        ),
    ],
)
def test_query_failures_carry_the_same_error_body_as_the_cli(
    client, indexed, capsys, url, argv, code, status
):
    """§5.2's codes, and EC-15's route: an ID that is gone is a named 404, not a crash."""
    got, body = get(client, url)
    assert got == status
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code

    assert cli.main([*argv, "--out", str(indexed), "--json"]) == cli.EXIT_FAILURE
    assert json.loads(capsys.readouterr().out) == body


# ---------------------------------------------------------------------------
# AC-25.2 / AC-20.2 / EC-13 — every endpoint, every broken-index state
# ---------------------------------------------------------------------------


def _break_index(out: Path, how: str) -> str:
    path = out / INDEX_FILENAME
    if how == "missing":
        path.unlink()
        return queries.ERROR_INDEX_MISSING
    if how == "unreadable":
        path.write_bytes(b"this is not a database at all")
        return queries.ERROR_INDEX_INCOMPATIBLE
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    return queries.ERROR_INDEX_INCOMPATIBLE


@pytest.mark.parametrize("how", ["missing", "unreadable", "incompatible"])
@pytest.mark.parametrize("url", ENDPOINTS)
def test_every_endpoint_refuses_a_broken_index_with_a_structured_error(indexed, how, url):
    """AC-25.2/AC-20.2/EC-13: an error body from *every* endpoint — never a partial payload.

    The three states are FR-39's whole surface: absent (AC-20.2), present but not a
    database this build can read, and a schema version it does not support (AC-39.2/39.3).
    """
    expected = _break_index(indexed, how)
    app = server.create_app(server.index_file(indexed))
    app.testing = True

    status, body = get(app.test_client(), url)
    assert status == 503
    assert set(body) == {"error"}
    assert body["error"]["code"] == expected
    assert str(indexed / INDEX_FILENAME) in body["error"]["message"]


def test_the_version_refusal_names_found_and_supported(indexed):
    """AC-39.2: the message the frontend shows full-screen has to say which version."""
    _break_index(indexed, "incompatible")
    app = server.create_app(server.index_file(indexed))
    app.testing = True
    _, body = get(app.test_client(), "/api/meta")
    assert "'99'" in body["error"]["message"]
    assert f"'{SCHEMA_VERSION}'" in body["error"]["message"]


def test_an_app_is_constructible_without_an_index_at_all(tmp_path):
    """AC-25.2 presupposes a server that started: construction may not require the index."""
    app = server.create_app(tmp_path / "never-written.sqlite")
    app.testing = True
    assert get(app.test_client(), "/api/meta")[0] == 503


# ---------------------------------------------------------------------------
# Malformed requests (the module's own code vocabulary — see its docstring)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/api/slice",
        "/api/slice?from=",
        f"/api/slice?from={MAIN.id}&direction=sideways",
        f"/api/slice?from={MAIN.id}&max_nodes=0",
        f"/api/slice?from={MAIN.id}&max_nodes=lots",
        "/api/nodes",
        "/api/nodes?search=",
        "/api/nodes?search=main&limit=0",
    ],
)
def test_a_malformed_request_is_a_structured_400(client, url):
    status, body = get(client, url)
    assert status == 400
    assert body["error"]["code"] == server.ERROR_BAD_REQUEST
    assert body["error"]["message"]


def test_an_unrouted_url_is_json_too_never_html(client):
    """A JSON API answers in JSON even when the answer is "no such endpoint"."""
    status, body = get(client, "/api/nonesuch")
    assert status == 404
    assert body["error"]["code"] == "not_found"
    assert client.post("/api/meta").get_json()["error"]["code"] == "method_not_allowed"


def test_an_unexpected_failure_is_still_a_structured_body(client, monkeypatch):
    """AC-27.2: the frontend must be able to show *any* failure, so none escapes as HTML."""

    def explode(index):
        raise RuntimeError("something unforeseen")

    monkeypatch.setattr(queries, "dead_code", explode)
    status, body = get(client, "/api/dead-code")
    assert status == 500
    assert body["error"]["code"] == server.ERROR_SERVER
    assert "something unforeseen" in body["error"]["message"]


# ---------------------------------------------------------------------------
# D20 — the index is the only file the server opens
# ---------------------------------------------------------------------------


@pytest.fixture
def opened_paths(monkeypatch) -> Iterator[list[str]]:
    """Record every path the process opens through Python's file APIs."""
    recorded: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def spy_open(file, *args, **kwargs):
        recorded.append(str(file))
        return real_open(file, *args, **kwargs)

    def spy_os_open(path, *args, **kwargs):
        recorded.append(str(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    yield recorded


def _under(path: str, directory: Path) -> bool:
    """True when `path` names something inside `directory` (best effort, never raises)."""
    try:
        return Path(path).resolve().is_relative_to(directory)
    except (OSError, ValueError, TypeError):  # pragma: no cover - fd ints and odd inputs
        return False


def test_the_server_opens_no_file_but_the_index(indexed, opened_paths):
    """D20's invariant, asserted rather than claimed (AC-25.1).

    Every endpoint is answered with no `reports/` directory in existence, and nothing
    inside the output directory is opened except the index itself — so the viewer cannot be
    reading a report file, and `/api/dead-code` is provably a recomputation.
    """
    app = server.create_app(server.index_file(indexed))
    app.testing = True
    client = app.test_client()

    del opened_paths[:]  # ignore whatever importing and app construction touched
    answered = [get(client, url)[0] for url in ENDPOINTS]
    assert answered == [200] * len(ENDPOINTS)

    assert [path for path in opened_paths if _under(path, indexed.resolve())] == []
    assert not (indexed / reports.REPORTS_DIRNAME).exists()


def test_every_endpoint_answers_with_the_report_directory_deleted(indexed):
    """The same invariant from the user's side: reports are the pipeline's, not the API's."""
    (indexed / reports.REPORTS_DIRNAME).mkdir()
    (indexed / reports.REPORTS_DIRNAME / "deadcode.json").write_text("{}", encoding="utf-8")
    app = server.create_app(server.index_file(indexed))
    app.testing = True
    before = {url: get(app.test_client(), url) for url in ENDPOINTS}

    for child in (indexed / reports.REPORTS_DIRNAME).iterdir():
        child.unlink()
    (indexed / reports.REPORTS_DIRNAME).rmdir()
    after = {url: get(app.test_client(), url) for url in ENDPOINTS}
    assert after == before


# ---------------------------------------------------------------------------
# FR-33 — loopback only, and nothing leaves the machine
# ---------------------------------------------------------------------------


def test_no_request_leaves_the_machine(client, monkeypatch):
    """FR-33/AC-33.1: answering any endpoint opens no socket at all."""
    attempts: list[Any] = []

    def refuse(self, address):  # pragma: no cover - the assertion is that this never runs
        attempts.append(address)
        raise AssertionError(f"the viewer attempted an outbound connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    for url in ENDPOINTS:
        assert get(client, url)[0] == 200
    assert attempts == []


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def _listening_local_addresses(port: int) -> set[str]:
    """The kernel's own answer: every listening socket's local address for `port`.

    `/proc/net/tcp{,6}` rather than a connection probe, because "refuses connections from
    elsewhere" and "is not bound elsewhere" are different claims and FR-33 wants the
    second. Addresses are the kernel's hex form; loopback is `0100007F` (v4) and
    `…00000001` (v6).
    """
    found: set[str] = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if not table.exists():
            continue
        for line in table.read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            local, state = fields[1], fields[3]
            address, _, hex_port = local.partition(":")
            if state == "0A" and int(hex_port, 16) == port:
                found.add(address)
    return found


@pytest.mark.skipif(not Path("/proc/net/tcp").exists(), reason="needs Linux /proc")
def test_the_server_binds_only_loopback(indexed):
    """FR-33/D7a, against the real server: 127.0.0.1 and nothing else, debug off."""
    port = _free_port()
    thread = threading.Thread(
        target=server.serve,
        args=(indexed, port),
        kwargs={"stdout": io.StringIO()},
        daemon=True,
    )
    thread.start()

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}/api/meta", timeout=1.0) as answer:
                body = json.loads(answer.read())
            break
        except OSError:  # not up yet
            time.sleep(0.05)
    else:  # pragma: no cover - a server that never came up is a failure, not a flake
        raise AssertionError(f"the viewer never started on {HOST}:{port}")

    assert body["schema_version"] == SCHEMA_VERSION
    assert _listening_local_addresses(port) == {"0100007F"}


def test_serve_runs_the_app_with_debug_off_on_loopback(indexed, monkeypatch, capsys):
    """D7a as an argument-level assertion: the flags that must never drift."""
    captured: dict[str, Any] = {}

    def fake_run(self, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("flask.Flask.run", fake_run)
    server.serve(indexed)
    assert captured == {
        "host": HOST,
        "port": DEFAULT_PORT,
        "debug": False,
        "use_reloader": False,
        "threaded": True,
    }
    assert HOST in capsys.readouterr().out


def test_the_view_subcommand_serves_the_index_the_queries_answer_from(indexed, monkeypatch):
    """design.md §3.1's wiring: `view` locates the index exactly as `query` does.

    The viewer answers the same questions from the same file, so the two must never
    disagree about which file that is — `_handle_view` resolves through `query_out_dir`,
    the one derivation §5.1 defines.
    """
    called: dict[str, Any] = {}

    def fake_serve(out_dir, port, **kwargs):
        called["out_dir"] = out_dir
        called["port"] = port

    monkeypatch.setattr(server, "serve", fake_serve)
    assert cli.main(["view", "--out", str(indexed), "--port", "9123"]) == cli.EXIT_SUCCESS
    assert called == {"out_dir": indexed.resolve(), "port": 9123}
    assert server.index_file(called["out_dir"]) == indexed / INDEX_FILENAME


def test_the_default_port_has_one_definition_site():
    """design.md §3.11's 8517, resolved by the CLI rather than copied into it."""
    assert DEFAULT_PORT == 8517
    assert cli.DEFAULT_VIEWER_PORT is DEFAULT_PORT
