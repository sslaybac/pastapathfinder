"""The read-only JSON API over the index (design.md §3.11, §5.2, D7, D7a, D20).

design.md §3.11 (`server`), §5.2 (the endpoints and their shapes, normative), §4.1 (node
IDs are URL-encoded where they appear in API paths), D7 (the pipeline↔viewer boundary is a
local HTTP API in index-schema vocabulary), D7a (Flask, threaded, 127.0.0.1, debug off),
D20 (the server opens the index and no other file); requirements FR-25 (AC-25.1/25.2),
FR-20 (AC-20.1/20.2), FR-26-28 (the data half; the views are task 5.2), FR-33, FR-39
(AC-39.2/39.3), EC-13, EC-15.

**Every answer comes from the index, and the index is the only file of the *analysis* this
process opens** (D20). There is no report directory in any code path here:
`/api/dead-code` recomputes through `queries.dead_code()` rather than reading
`deadcode.json`, which is what makes AC-25.1 a property a test can assert rather than a
claim. The engine is unreachable by construction — this package may import neither
`mypy.*` nor `pastapathfinder.adapters.*` (the standing task-1.1 import test), so the whole
API is `index.py` plus `queries.py`.

The frontend's own files are the one other thing this process reads, and they are not an
exception to D20: §3.11 ships `static/` as package data in the same paragraph that states
the rule, so the rule is about where analysis *data* comes from. The distinction is
enforced rather than asserted — `tests/unit/test_viewer_server.py` still holds every
`/api/*` endpoint to the index alone, and `tests/unit/test_viewer_static.py` holds the
asset routes to the package's own `static/` directory.

**One serializer with the CLI.** Every payload below is produced by a `queries.*_json()`
function, the same one `pastapathfinder query … --json` prints (design.md §5.1). Two
renderings of one shape would drift the first time a field moved, so there is only one, and
a test compares this server's responses with the CLI's output for the same query.

**The index is opened per request**, read-only, and closed again. AC-25.2 requires *every*
endpoint to answer with a structured error when the index is missing, unreadable, or
schema-incompatible — so the app must start without one, which rules out holding an open
store. Per-request opening also keeps each of the threaded server's workers on its own
SQLite connection, which is what `sqlite3` requires.

**Error bodies.** §5.2's four codes (`unknown_node`, `not_sliceable`, `index_missing`,
`index_incompatible`) are the vocabulary of *query* failures, and `queries.error_code()`
assigns them; they are never used for anything else. §5.2 does not name a code for a
malformed request or an unrouted URL — it names none because the frontend never sends one
— so those carry the HTTP status's own slug (`bad_request`, `not_found`,
`method_not_allowed`, `server_error`) in the same `{error: {code, message}}` envelope.
That choice is this module's, recorded here because it is an addition at a point where the
spec is silent rather than an interpretation of something it says.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from flask import Flask, request, send_from_directory

from pastapathfinder import queries
from pastapathfinder.index import INDEX_FILENAME, Index, IndexStoreError, open_index
from pastapathfinder.schema import META_SCHEMA_VERSION
from pastapathfinder.viewer import DEFAULT_PORT, HOST

# ---------------------------------------------------------------------------
# Defaults (design.md §3.11, D7a; FR-33)
# ---------------------------------------------------------------------------

#: `/api/nodes?search=`'s default and maximum result count (§5.2 publishes the default).
#: The ceiling exists because `limit` is caller-supplied and the response is built in
#: memory; it bounds the answer rather than rejecting the request.
SEARCH_LIMIT = 50
SEARCH_LIMIT_MAX = 500

#: The no-build frontend, shipped as package data (design.md §3.11 `static`, D8; FR-33).
#: Resolved from this module's own location so that the assets travel with the installed
#: package — there is no CDN and no build directory to fall back to.
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: The URL prefix the page's own assets are served under. `index.html` addresses every
#: asset absolutely (`/static/...`), so the page works identically at `/` and anywhere else.
STATIC_URL_PATH = "/static"

#: The page itself.
INDEX_HTML = "index.html"

# ---------------------------------------------------------------------------
# HTTP status mapping
# ---------------------------------------------------------------------------

#: §5.2's query-failure codes, each with the status that says what kind of failure it is:
#: a node that is not there is a 404; a node whose kind has no slice is a client asking for
#: something undefined (400); an index that is missing or unreadable is the *server* unable
#: to answer at all (503), which is the state AC-25.2 has the frontend render full-screen.
STATUS_FOR_ERROR: dict[str, int] = {
    queries.ERROR_UNKNOWN_NODE: 404,
    queries.ERROR_NOT_SLICEABLE: 400,
    queries.ERROR_INDEX_MISSING: 503,
    queries.ERROR_INDEX_INCOMPATIBLE: 503,
}

#: Codes for failures that are not query failures — see the module docstring.
ERROR_BAD_REQUEST = "bad_request"
ERROR_SERVER = "server_error"
_HTTP_ERROR_CODES: dict[int, str] = {
    400: ERROR_BAD_REQUEST,
    404: "not_found",
    405: "method_not_allowed",
}


class BadRequest(Exception):
    """A request this API cannot interpret: a missing or unusable query parameter.

    Distinct from every `queries.QueryError`, which is a well-formed request the index
    cannot answer. The message names the parameter and what was wrong with it.
    """


# ---------------------------------------------------------------------------
# Index access (AC-25.2, EC-13; D20)
# ---------------------------------------------------------------------------


def index_file(out_dir: Path | str) -> Path:
    """The index inside an output directory — the one file this server opens (D20)."""
    return Path(out_dir) / INDEX_FILENAME


@contextmanager
def _opened(path: Path) -> Iterator[Index]:
    """Open the index read-only for the length of one request.

    Read-only because the viewer is a reader: nothing served here may modify what
    `analyze` published. `open_index` already refuses an absent index (AC-20.2) and a
    schema version this build does not support (AC-39.2/39.3, FR-39); the one case it
    leaves is a file that exists and cannot be opened at all — permissions, a truncated
    file, a broken mount — which SQLite reports without naming the index or the remedy, so
    that is restated here (AC-20.2, EC-13).
    """
    try:
        index = open_index(path, read_only=True)
    except sqlite3.Error as exc:
        raise IndexStoreError(
            f"cannot open the index {path}: {exc}; check that it is readable, "
            f"or re-run `pastapathfinder analyze <root>` to rebuild it"
        ) from exc
    try:
        yield index
    finally:
        index.close()


# ---------------------------------------------------------------------------
# Parameter parsing
# ---------------------------------------------------------------------------


def _required(name: str) -> str:
    value = request.args.get(name, "").strip()
    if not value:
        raise BadRequest(f"the {name!r} query parameter is required")
    return value


def _direction() -> str:
    """`direction`, defaulting to forward — `queries.slice()`'s own default.

    A value that is neither is refused rather than coerced: answering the backward question
    for a request that said `backwards` would be a wrong answer presented as a right one.
    """
    value = request.args.get("direction")
    if value is None or value == "":
        return queries.FORWARD
    if value not in queries.DIRECTIONS:
        raise BadRequest(f"unknown direction {value!r}; expected one of {list(queries.DIRECTIONS)}")
    return value


def _positive_int(name: str, default: int, maximum: int | None = None) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(f"{name!r} must be an integer, not {raw!r}") from None
    if value < 1:
        raise BadRequest(f"{name!r} must be at least 1, not {value}")
    return value if maximum is None else min(value, maximum)


# ---------------------------------------------------------------------------
# `/api/meta` (§5.2)
# ---------------------------------------------------------------------------

_COUNTS = {
    "files": "SELECT COUNT(*) FROM files",
    "nodes": "SELECT COUNT(*) FROM nodes",
    "edges": "SELECT COUNT(*) FROM edges",
    "entry_points": "SELECT COUNT(*) FROM nodes WHERE kind = 'entry_point'",
}


def meta_json(index: Index) -> dict[str, Any]:
    """`/api/meta`'s shape (§5.2): the index's provenance and its size.

    `schema_version` is read from the index rather than from this build's constant: the
    two are provably equal here — `open_index` refused everything else (FR-39) — and
    reporting the value that is actually stored is what makes the field an answer rather
    than an assertion.
    """
    meta = index.meta()
    return {
        "schema_version": int(meta[META_SCHEMA_VERSION]),
        "tool_version": meta.get("tool_version"),
        "root_path": meta.get("root_path"),
        "created_at": meta.get("created_at"),
        "counts": {
            name: int(index.connection.execute(sql).fetchone()[0]) for name, sql in _COUNTS.items()
        },
    }


# ---------------------------------------------------------------------------
# The app (design.md §3.11)
# ---------------------------------------------------------------------------


def create_app(index_path: Path | str) -> Flask:
    """Build the read-only API over the index at `index_path`.

    The app is constructed whether or not that index exists: AC-25.2 requires every
    endpoint to answer with a structured error in that case, which it cannot do if
    construction fails first.

    The frontend is served from the package's own `static/` directory (design.md §3.11,
    D8): `/` is the page and `/static/...` its assets, all of them local files that ship
    with the install. Nothing here reaches the network, and nothing here is generated —
    there is no build step to run before the viewer works (FR-33).
    """
    path = Path(index_path)
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path=STATIC_URL_PATH)
    # Deterministic bodies, for the same reason the store sorts at the write boundary
    # (D12): two identical requests must produce identical bytes.
    app.json.sort_keys = True

    def answer(produce: Callable[[Index], dict[str, Any]]) -> tuple[dict[str, Any], int]:
        with _opened(path) as index:
            return produce(index), 200

    @app.get("/")
    def page():
        """The viewer page (design.md §3.11 `static`).

        Served whatever the state of the index: the page is what *renders* AC-25.2's
        unreadable-index message, so refusing to serve it when the index is missing would
        leave the user with nothing to read the error in.
        """
        return send_from_directory(STATIC_DIR, INDEX_HTML)

    @app.get("/api/meta")
    def meta() -> tuple[dict[str, Any], int]:
        return answer(meta_json)

    @app.get("/api/entry-points")
    def entry_points() -> tuple[dict[str, Any], int]:
        """FR-26's data: every entry node, sorted by id (§5.2)."""
        return answer(lambda index: queries.entry_points_json(queries.entry_points(index)))

    @app.get("/api/nodes")
    def search_nodes() -> tuple[dict[str, Any], int]:
        """AC-26.2's alternative: find a node by substring of its id or name.

        This is what the frontend offers when a codebase has zero entry points (EC-9's
        normal outcome for a library) — FR-17 slicing needs an origin, and this is how the
        user names one.
        """
        term = _required("search")
        limit = _positive_int("limit", SEARCH_LIMIT, SEARCH_LIMIT_MAX)
        return answer(lambda index: queries.nodes_json(queries.search_nodes(index, term, limit)))

    @app.get("/api/nodes/<path:node_id>")
    def node(node_id: str) -> tuple[dict[str, Any], int]:
        """One node (§5.2), or `unknown_node` at 404 (AC-16.2, EC-15).

        `<path:…>` because a node ID contains `/` in its `file:` form (§4.1) and `:` in
        every form; Werkzeug has already percent-decoded whatever the client encoded.
        """
        return answer(lambda index: queries.node_json(queries.node(index, node_id)))

    @app.get("/api/slice")
    def slice_() -> tuple[dict[str, Any], int]:
        """The flagship query (FR-15-FR-17), bounded visibly (FR-28/AC-28.2).

        `max_nodes` defaults to `queries.SLICE_MAX_NODES` — design.md §8-O2's provisional
        bound, resolved at its single definition site rather than copied here — and the
        response carries `truncated` and `frontier` so the frontend can show the bound and
        offer to expand it.
        """
        node_id = _required("from")
        direction = _direction()
        max_nodes = _positive_int("max_nodes", queries.SLICE_MAX_NODES)
        return answer(
            lambda index: queries.slice_json(queries.slice(index, node_id, direction, max_nodes))
        )

    @app.get("/api/dead-code")
    def dead_code() -> tuple[dict[str, Any], int]:
        """`deadcode.json`'s shape minus the volatile `run*` block, recomputed (D20).

        Recomputed, never read back from the report file: the server opens the index and
        nothing else, so this answers correctly against an output directory whose
        `reports/` has been deleted or never written.
        """
        return answer(lambda index: queries.dead_code_json(queries.dead_code(index)))

    @app.errorhandler(Exception)
    def on_error(exc: Exception) -> tuple[dict[str, Any], int]:
        """Every failure leaves as `{error: {code, message}}` — never HTML, never partial.

        AC-25.2 and AC-27.2 are the same requirement seen from two sides: the frontend must
        be able to show the user what went wrong, which it can only do if the failure
        arrives in the shape it parses. A query failure carries its §5.2 code; anything
        else carries its HTTP status slug (see the module docstring).
        """
        if isinstance(exc, queries.QueryError | IndexStoreError):
            body = queries.error_json(exc)
            return body, STATUS_FOR_ERROR[body["error"]["code"]]
        if isinstance(exc, BadRequest):
            return {"error": {"code": ERROR_BAD_REQUEST, "message": str(exc)}}, 400
        status = int(getattr(exc, "code", 500) or 500)
        message = str(getattr(exc, "description", None) or exc) or type(exc).__name__
        return {
            "error": {"code": _HTTP_ERROR_CODES.get(status, ERROR_SERVER), "message": message}
        }, status

    return app


# ---------------------------------------------------------------------------
# Running it (design.md §3.11, D7a; FR-33)
# ---------------------------------------------------------------------------


def serve(
    out_dir: Path | str,
    port: int = DEFAULT_PORT,
    *,
    host: str = HOST,
    stdout: TextIO | None = None,
) -> None:
    """Serve the API on `host:port` until interrupted (design.md §3.11, D7a).

    `debug=False` and no reloader, both stated rather than inherited: Flask's debugger is a
    remote-code-execution console, and D7a puts the server on loopback with it off.
    `threaded=True` keeps one slow query from blocking the page — each request opens its
    own read-only connection, so there is no shared state to protect.

    Werkzeug's server absorbs the interrupt and returns, so a Ctrl-C exits through the
    normal path rather than as an escaped `KeyboardInterrupt`.
    """
    path = index_file(out_dir)
    stream = sys.stdout if stdout is None else stdout
    print(f"pastapathfinder viewer: http://{host}:{port}/", file=stream)
    print(f"  index: {path}", file=stream)
    print("  press Ctrl-C to stop", file=stream)
    create_app(path).run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


__all__ = [
    "DEFAULT_PORT",
    "ERROR_BAD_REQUEST",
    "ERROR_SERVER",
    "HOST",
    "INDEX_HTML",
    "SEARCH_LIMIT",
    "SEARCH_LIMIT_MAX",
    "STATIC_DIR",
    "STATIC_URL_PATH",
    "STATUS_FOR_ERROR",
    "BadRequest",
    "create_app",
    "index_file",
    "meta_json",
    "serve",
]
