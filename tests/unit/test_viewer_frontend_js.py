"""The frontend's behavior, asserted by running it (specs/tasks.md task 5.2).

design.md §3.11 (`static`), §5.2, D8, R2; requirements FR-26 (AC-26.1/26.2), FR-27
(AC-27.1/27.2/27.3), FR-28 (AC-28.1/28.2), FR-19 (AC-19.2/19.3), FR-25 (AC-25.2), FR-33,
FR-15 (AC-15.2), EC-9, EC-13, EC-15.

Task 5.2's verification bullets are claims about what the page *does*, so they are checked
by doing it: `viewer_dom_harness.js` evaluates the shipped `app.js` verbatim under a stub
DOM, a fixture `fetch`, and a recording cytoscape, and each test here drives a workflow and
asserts on the resulting DOM. What is faked is only the surroundings — every decision under
test is the shipped code's.

The fixture responses below are written in §5.2's shapes by hand rather than taken from a
live server, for the reason task 3.5's tests give: a payload written out field by field is
one whose expected rendering can be asserted exactly. `test_viewer_server.py` is what ties
those shapes back to a real index.

**Node.** Running JavaScript needs a JavaScript engine, and the dependency set is frozen at
`mypy`/`pathspec`/`flask` plus the standard library (design.md §6) — so this file uses the
system `node` if there is one and skips if there is not, rather than adding a dependency or
an npm toolchain (D8). Everything that can be checked without an engine is in
`test_viewer_static.py`, which never skips.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from pastapathfinder.viewer import server

HARNESS = Path(__file__).resolve().parent / "viewer_dom_harness.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None, reason="no `node` on PATH: the frontend behavior tests need a JS engine"
)

# ---------------------------------------------------------------------------
# A graph small enough to check by eye — the same one test_viewer_server.py uses
# ---------------------------------------------------------------------------

APP = "pkg/app.py"
ENTRY = "python:entry:main_block:pkg.app@38"
ENTRY_B = "python:entry:console_script:pkg.app.main@1"
MODULE = "python:pkg.app.<module>"
MAIN = "python:pkg.app.main"
HELPER = "python:pkg.app.helper"
ORPHAN = "python:pkg.app.orphan"
EXTERNAL = "python:os.path.join"

CAVEAT = (
    "Dead-code findings are approximate: Python's dynamism (reflection, dynamic imports, "
    "framework registration) produces false positives."
)


def node_doc(
    node_id: str,
    kind: str,
    name: str,
    *,
    file_path: str | None = APP,
    start: int | None = None,
    end: int | None = None,
    external: int = 0,
    reachable: int | None = 1,
) -> dict[str, Any]:
    """One `/api/nodes/{id}` document (§5.2)."""
    return {
        "id": node_id,
        "kind": kind,
        "name": name,
        "file_path": None if external else file_path,
        "start_line": None if external else start,
        "end_line": None if external else end,
        "is_external": external,
        "reachable": reachable,
        "attrs": {},
    }


NODES = {
    ENTRY: node_doc(ENTRY, "entry_point", "pkg.app:__main__", start=38, end=38),
    ENTRY_B: node_doc(ENTRY_B, "entry_point", "app", file_path="pyproject.toml", start=1, end=1),
    MODULE: node_doc(MODULE, "module", "pkg.app", start=1, end=40),
    MAIN: node_doc(MAIN, "function", "main", start=10, end=12),
    HELPER: node_doc(HELPER, "function", "helper", start=20, end=22),
    ORPHAN: node_doc(ORPHAN, "function", "orphan", start=30, end=32, reachable=0),
    EXTERNAL: node_doc(EXTERNAL, "function", "join", external=1, reachable=0),
}

META = {
    "schema_version": 1,
    "tool_version": "0.1.0",
    "root_path": "/srv/target",
    "created_at": "2026-07-24T09:00:00+00:00",
    "counts": {"files": 1, "nodes": 8, "edges": 9, "entry_points": 2},
}

ENTRY_POINTS = {
    "entry_points": [
        {
            "id": ENTRY_B,
            "name": "app",
            "detector": "console_script",
            "target_id": MAIN,
            "file_path": "pyproject.toml",
            "start_line": 1,
        },
        {
            "id": ENTRY,
            "name": "pkg.app:__main__",
            "detector": "main_block",
            "target_id": MODULE,
            "file_path": APP,
            "start_line": 38,
        },
    ]
}

FORWARD_SLICE = {
    "nodes": [NODES[ENTRY], NODES[MODULE], NODES[MAIN], NODES[HELPER], NODES[EXTERNAL]],
    "edges": [
        {"src": ENTRY, "dst": MODULE, "is_ambiguous": 0},
        {"src": MODULE, "dst": MAIN, "is_ambiguous": 0},
        {"src": MAIN, "dst": HELPER, "is_ambiguous": 0},
        {"src": HELPER, "dst": EXTERNAL, "is_ambiguous": 1},
    ],
    "truncated": False,
    "frontier": [],
}

DEAD_CODE = {
    "format_version": 1,
    "caveat": CAVEAT,
    "no_entry_points_warning": False,
    "unreachable": [
        {"file": APP, "functions": [{"id": ORPHAN, "name": "orphan", "start_line": 30}]}
    ],
}


# ---------------------------------------------------------------------------
# Driving the harness
# ---------------------------------------------------------------------------


def route(path: str, **params: Any) -> str:
    """The harness's canonical route key: decoded values, query keys sorted."""
    if not params:
        return path
    query = "&".join(f"{name}={value}" for name, value in sorted(params.items()))
    return f"{path}?{query}"


def ok(body: Any) -> dict[str, Any]:
    return {"status": 200, "body": body}


def fails(code: str, message: str, status: int = 400) -> dict[str, Any]:
    """A §5.2 error body, exactly as `server.py` returns it."""
    return {"status": status, "body": {"error": {"code": code, "message": message}}}


def base_routes() -> dict[str, dict[str, Any]]:
    """Everything the standard workflow asks for, answered."""
    routes = {
        "/meta": ok(META),
        "/entry-points": ok(ENTRY_POINTS),
        "/dead-code": ok(DEAD_CODE),
        route("/slice", **{"from": ENTRY, "direction": "forward"}): ok(FORWARD_SLICE),
    }
    for node_id, document in NODES.items():
        routes[f"/nodes/{node_id}"] = ok(document)
    return routes


def drive(routes: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the shipped app.js through `steps` and return `{snapshot, state, log}`."""
    assert NODE is not None
    completed = subprocess.run(
        [NODE, str(HARNESS), str(server.STATIC_DIR)],
        input=json.dumps({"routes": routes, "steps": steps}),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(completed.stdout)
    # A thrown step is a broken workflow, not a quiet zero-assertion pass.
    assert result["log"]["error"] is None, result["log"]["error"]
    return result


def start(routes: dict[str, Any] | None = None, *, steps: list[dict[str, Any]] | None = None):
    return drive(base_routes() if routes is None else routes, [{"call": "start"}, *(steps or [])])


def text(result: dict[str, Any], element_id: str) -> str:
    return result["snapshot"][element_id]["text"]


def hidden(result: dict[str, Any], element_id: str) -> bool:
    return result["snapshot"][element_id]["hidden"]


def items(result: dict[str, Any], element_id: str) -> list[str]:
    return result["snapshot"][element_id]["items"]


# ---------------------------------------------------------------------------
# FR-26 — the entry-point list
# ---------------------------------------------------------------------------


def test_every_entry_point_is_listed_and_selectable():
    """AC-26.1: all of them, each one a control that opens its trace."""
    result = start()
    listed = items(result, "entry-point-list")
    assert len(listed) == len(ENTRY_POINTS["entry_points"])
    assert "app · console_script · pyproject.toml:1" in listed
    assert f"pkg.app:__main__ · main_block · {APP}:38" in listed
    assert hidden(result, "entry-points-empty")

    opened = start(steps=[{"clickItem": ["entry-point-list", 1]}])
    assert opened["state"]["origin"] == ENTRY
    assert opened["state"]["direction"] == "forward"


def test_zero_entry_points_says_so_and_offers_slicing_from_any_node():
    """AC-26.2 / EC-9: the empty case is stated, with FR-17's alternative attached."""
    routes = base_routes()
    routes["/entry-points"] = ok({"entry_points": []})
    routes[route("/nodes", search="help")] = ok({"nodes": [NODES[HELPER]]})
    routes[route("/slice", **{"from": HELPER, "direction": "forward"})] = ok(
        {"nodes": [NODES[HELPER]], "edges": [], "truncated": False, "frontier": []}
    )

    result = drive(
        routes,
        [
            {"call": "start"},
            {"submit": "search-form", "input": "search-input", "value": "help"},
        ],
    )
    assert items(result, "entry-point-list") == []
    assert not hidden(result, "entry-points-empty")
    message = text(result, "entry-points-empty")
    assert "No entry points were detected" in message
    assert "slice from it instead" in message

    assert items(result, "search-results") == [f"helper · function · {APP}:20–22"]

    followed = drive(
        routes,
        [
            {"call": "start"},
            {"submit": "search-form", "input": "search-input", "value": "help"},
            {"clickItem": ["search-results", 0]},
        ],
    )
    assert followed["state"]["origin"] == HELPER


# ---------------------------------------------------------------------------
# FR-27 — opening and navigating a trace
# ---------------------------------------------------------------------------


def test_selecting_an_entry_point_and_choosing_forward_renders_that_slice():
    """AC-27.1, first half: the rendered graph is the slice the API returned."""
    result = start(steps=[{"call": "openSlice", "args": [ENTRY, "forward"]}])

    elements = result["log"]["elements"]
    nodes = [item["data"]["id"] for item in elements if item["group"] == "nodes"]
    edges = [
        (item["data"]["source"], item["data"]["target"])
        for item in elements
        if item["group"] == "edges"
    ]
    assert nodes == [node["id"] for node in FORWARD_SLICE["nodes"]]
    assert edges == [(edge["src"], edge["dst"]) for edge in FORWARD_SLICE["edges"]]
    assert result["log"]["layout"]["name"] == "dagre"
    assert "forward slice" in text(result, "trace-origin")
    assert hidden(result, "trace-error")


def test_a_displayed_call_edge_can_be_followed_to_its_target():
    """AC-27.1, second half: tapping the edge selects the node it points at."""
    result = start(
        steps=[
            {"call": "openSlice", "args": [ENTRY, "forward"]},
            {"tap": "edge", "id": f"{MAIN}→{HELPER}"},
        ]
    )
    assert "tap edge" in result["log"]["handlers"]
    assert result["state"]["selected"] == HELPER
    assert text(result, "node-panel-name") == "helper"
    assert text(result, "node-panel-location") == f"{APP}:20–22"


def test_a_failed_query_is_surfaced_in_view_rather_than_a_blank_graph():
    """AC-27.2: the server's own message, in view, and no stale graph beside it."""
    routes = base_routes()
    message = "node python:file:pkg/app.py has kind 'file', which is not sliceable"
    routes[route("/slice", **{"from": "python:file:pkg/app.py", "direction": "forward"})] = fails(
        "not_sliceable", message
    )
    routes[f"/nodes/python:file:{APP}"] = ok(node_doc(f"python:file:{APP}", "file", APP))

    result = start(
        routes,
        steps=[{"call": "openSlice", "args": [f"python:file:{APP}", "forward"]}],
    )
    assert not hidden(result, "trace-error")
    assert text(result, "trace-error") == message
    assert result["log"]["graphs"] == 0


def test_the_node_panel_shows_the_source_span():
    """AC-27.3: `file_path:start–end` for a node the analysis looked inside."""
    result = start(steps=[{"call": "selectNode", "args": [MAIN]}])
    assert text(result, "node-panel-name") == "main"
    assert text(result, "node-panel-kind") == "function"
    assert text(result, "node-panel-location") == f"{APP}:10–12"
    assert not hidden(result, "node-panel-body")
    assert hidden(result, "node-panel-empty")


def test_the_node_panel_states_that_an_external_target_was_not_analyzed():
    """AC-27.3's other half: FR-36's leaves have no span, and the panel says why."""
    result = start(steps=[{"call": "selectNode", "args": [EXTERNAL]}])
    assert text(result, "node-panel-location") == "external — not analyzed"
    assert "not computed for external" in text(result, "node-panel-reachable")


def test_an_empty_slice_is_presented_as_an_empty_slice():
    """AC-15.2 at the viewer: no outgoing edges is an answer, not an error."""
    routes = base_routes()
    routes[route("/slice", **{"from": HELPER, "direction": "forward"})] = ok(
        {"nodes": [], "edges": [], "truncated": False, "frontier": []}
    )
    result = start(routes, steps=[{"call": "openSlice", "args": [HELPER, "forward"]}])
    assert not hidden(result, "trace-empty")
    assert "the slice is empty" in text(result, "trace-empty")
    assert hidden(result, "trace-error")


def test_the_direction_toggle_reopens_the_slice_the_other_way():
    """FR-27: forward/backward is one control over one origin."""
    routes = base_routes()
    routes[route("/slice", **{"from": ENTRY, "direction": "backward"})] = ok(
        {"nodes": [NODES[ENTRY]], "edges": [], "truncated": False, "frontier": []}
    )
    result = start(
        routes,
        steps=[{"call": "openSlice", "args": [ENTRY, "forward"]}, {"click": "direction-backward"}],
    )
    assert result["state"]["direction"] == "backward"
    assert result["snapshot"]["direction-backward"]["className"] == "toggle toggle-active"
    assert result["snapshot"]["direction-forward"]["className"] == "toggle"


# ---------------------------------------------------------------------------
# FR-28 — slice-first, and the bound is visible
# ---------------------------------------------------------------------------


def test_the_standard_workflow_renders_a_slice_and_never_the_whole_graph():
    """AC-28.1: every graph this page draws came from `/api/slice`, bounded to a selection."""
    result = start(steps=[{"clickItem": ["entry-point-list", 1]}])
    slice_requests = [url for url in result["log"]["requests"] if url.startswith("/api/slice")]
    assert len(slice_requests) == 1
    assert "from=" in slice_requests[0]
    assert result["log"]["unrouted"] == []

    drawn = len([item for item in result["log"]["elements"] if item["group"] == "nodes"])
    assert drawn == len(FORWARD_SLICE["nodes"]) < META["counts"]["nodes"]


def test_a_truncated_slice_shows_the_bound_and_offers_to_expand_it():
    """AC-28.2: the truncation banner, the frontier, and an action that widens the bound."""
    bounded = {
        "nodes": [NODES[ENTRY], NODES[MODULE]],
        "edges": [{"src": ENTRY, "dst": MODULE, "is_ambiguous": 0}],
        "truncated": True,
        "frontier": [MAIN],
    }
    expanded = dict(FORWARD_SLICE)
    routes = base_routes()
    routes[route("/slice", **{"from": ENTRY, "direction": "forward"})] = ok(bounded)
    routes[route("/slice", **{"from": ENTRY, "direction": "forward", "max_nodes": "4"})] = ok(
        expanded
    )

    result = start(routes, steps=[{"call": "openSlice", "args": [ENTRY, "forward"]}])
    assert not hidden(result, "truncation-banner")
    banner = text(result, "truncation-message")
    assert "2 nodes are shown" in banner
    assert "1 frontier node was not expanded" in banner
    assert items(result, "frontier-list") == [MAIN]
    assert text(result, "frontier-expand") == "Expand to 4 nodes"

    grown = start(
        routes,
        steps=[{"call": "openSlice", "args": [ENTRY, "forward"]}, {"click": "frontier-expand"}],
    )
    assert grown["state"]["maxNodes"] == 4
    assert any("max_nodes=4" in url for url in grown["log"]["requests"])
    assert hidden(grown, "truncation-banner")
    assert len([item for item in grown["log"]["elements"] if item["group"] == "nodes"]) == 5


def test_expanding_one_slice_does_not_bound_the_next_one():
    """An expansion is a property of one request, not a setting the session keeps.

    The failure it prevents is the surprising direction: expanding a *small* truncated
    slice to four nodes and then opening an unrelated entry point would ask for four nodes
    there too — a tighter bound than the server's own, applied invisibly.
    """
    bounded = {
        "nodes": [NODES[ENTRY], NODES[MODULE]],
        "edges": [{"src": ENTRY, "dst": MODULE, "is_ambiguous": 0}],
        "truncated": True,
        "frontier": [MAIN],
    }
    routes = base_routes()
    routes[route("/slice", **{"from": ENTRY, "direction": "forward"})] = ok(bounded)
    routes[route("/slice", **{"from": ENTRY, "direction": "forward", "max_nodes": "4"})] = ok(
        FORWARD_SLICE
    )
    routes[route("/slice", **{"from": ENTRY_B, "direction": "forward"})] = ok(FORWARD_SLICE)

    result = start(
        routes,
        steps=[
            {"call": "openSlice", "args": [ENTRY, "forward"]},
            {"click": "frontier-expand"},
            {"clickItem": ["entry-point-list", 0]},
        ],
    )
    assert result["state"]["origin"] == ENTRY_B
    assert result["state"]["maxNodes"] is None
    assert result["log"]["unrouted"] == []
    assert [unquote(url) for url in result["log"]["requests"][-2:]] == [
        f"/api/slice?from={ENTRY_B}&direction=forward",
        f"/api/nodes/{ENTRY_B}",
    ]


def test_an_edge_into_a_node_beyond_the_bound_is_not_drawn():
    """A dangling edge would claim something the truncated slice does not."""
    bounded = {
        "nodes": [NODES[ENTRY], NODES[MODULE]],
        "edges": [
            {"src": ENTRY, "dst": MODULE, "is_ambiguous": 0},
            {"src": MODULE, "dst": MAIN, "is_ambiguous": 0},
        ],
        "truncated": True,
        "frontier": [MAIN],
    }
    routes = base_routes()
    routes[route("/slice", **{"from": ENTRY, "direction": "forward"})] = ok(bounded)
    result = start(routes, steps=[{"call": "openSlice", "args": [ENTRY, "forward"]}])
    edges = [item for item in result["log"]["elements"] if item["group"] == "edges"]
    assert [(edge["data"]["source"], edge["data"]["target"]) for edge in edges] == [(ENTRY, MODULE)]


# ---------------------------------------------------------------------------
# EC-15 — the index changed underneath the page
# ---------------------------------------------------------------------------


def test_a_vanished_node_routes_the_user_back_to_the_entry_list():
    """EC-15: the unknown-ID error is stated, the stale trace goes, the user lands safe."""
    routes = base_routes()
    message = f"unknown node identifier: {ORPHAN}"
    routes[route("/slice", **{"from": ORPHAN, "direction": "forward"})] = fails(
        "unknown_node", message, status=404
    )

    result = start(
        routes,
        steps=[
            {"call": "openSlice", "args": [ENTRY, "forward"]},
            {"call": "openSlice", "args": [ORPHAN, "forward"]},
        ],
    )
    assert message in text(result, "trace-error")
    assert "the index has changed" in text(result, "trace-error")
    assert result["state"]["origin"] is None
    assert result["state"]["tab"] == "entry-points"
    assert not hidden(result, "entry-point-list")
    assert len(items(result, "entry-point-list")) == 2
    # The stale graph is torn down rather than left on screen looking current.
    assert result["log"]["destroyed"] == 1
    assert hidden(result, "fatal")


# ---------------------------------------------------------------------------
# AC-25.2 / EC-13 — the index itself cannot be read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "title"),
    [
        ("index_missing", "The index cannot be read"),
        ("index_incompatible", "This index was written by a different schema version"),
    ],
)
def test_an_unreadable_index_covers_the_page_with_the_reason(code: str, title: str):
    """AC-25.2 / AC-20.2 / EC-13: an error identifying the problem, not a blank UI."""
    message = "no index at /srv/out/index.sqlite3; run `pastapathfinder analyze <root>` first"
    result = start({"/meta": fails(code, message, status=503)})

    assert not hidden(result, "fatal")
    assert text(result, "fatal-title") == title
    assert text(result, "fatal-message") == message
    assert "analyze" in text(result, "fatal-hint")
    # Nothing below the failure was attempted: there is no index to answer from.
    assert result["log"]["requests"] == ["/api/meta"]


def test_an_unreachable_server_is_the_same_full_screen_state():
    """The viewer stopped, or was never up: still an error identifying the problem."""
    result = start({"/meta": {"throws": "Failed to fetch"}})
    assert not hidden(result, "fatal")
    assert "cannot reach the viewer server" in text(result, "fatal-message")


def test_the_retry_button_reloads_once_the_index_is_readable():
    """The remedy the hint names is `analyze`; retrying is how the page picks it up."""
    result = start({"/meta": fails("index_missing", "no index", status=503)})
    assert not hidden(result, "fatal")

    recovered = drive(
        base_routes(),
        [{"call": "start"}, {"click": "fatal-retry"}],
    )
    assert hidden(recovered, "fatal")
    assert len(items(recovered, "entry-point-list")) == 2


# ---------------------------------------------------------------------------
# FR-19 — dead code is never rendered without its caveat
# ---------------------------------------------------------------------------


def test_the_dead_code_view_carries_its_caveat_and_its_findings():
    """AC-19.2: the caveat is in the rendering, and it is the report's own text."""
    result = start(steps=[{"click": "tab-dead-code"}])
    assert text(result, "dead-code-caveat") == CAVEAT
    assert items(result, "dead-code-list") == [f"{APP} orphan:30"]
    assert "1 unreachable functions" in text(result, "dead-code-status")
    assert hidden(result, "dead-code-warning")
    assert hidden(result, "entry-points-pane")


def test_dead_code_with_no_entry_points_carries_the_warning_too():
    """AC-19.3 / AC-18.2: unreachable means nothing when nothing is an entry point."""
    routes = base_routes()
    routes["/dead-code"] = ok({**DEAD_CODE, "no_entry_points_warning": True})
    result = start(routes, steps=[{"click": "tab-dead-code"}])
    assert text(result, "dead-code-caveat") == CAVEAT
    assert not hidden(result, "dead-code-warning")
    assert "No entry points were detected" in text(result, "dead-code-warning")


# ---------------------------------------------------------------------------
# FR-33 — the page talks to this server and to nothing else
# ---------------------------------------------------------------------------


def test_no_workflow_touches_a_network_api_or_a_foreign_host():
    """FR-33/AC-33.1: every request is a same-origin `/api/…` call, and nothing else.

    The harness makes `XMLHttpRequest`, `WebSocket`, `EventSource`, `Image`,
    `importScripts` and `navigator.sendBeacon` record-and-throw, so a page that reached for
    any of them would show up here rather than in a packet capture.
    """
    result = start(
        steps=[
            {"clickItem": ["entry-point-list", 1]},
            {"tap": "node", "id": HELPER},
            {"tap": "edge", "id": f"{HELPER}→{EXTERNAL}"},
            {"click": "tab-dead-code"},
            {"click": "tab-entry-points"},
            {"submit": "search-form", "input": "search-input", "value": "main"},
        ],
    )
    assert result["log"]["forbidden"] == []
    assert result["log"]["requests"], "the workflow made no requests at all"
    off_api = [url for url in result["log"]["requests"] if not url.startswith("/api/")]
    assert off_api == []


def test_the_dagre_layout_plugin_is_registered_from_the_vendored_bundle():
    """D8: the layout comes from the file in `vendor/`, installed at load time."""
    result = start()
    assert result["log"]["plugins"] == ["dagre"]
