"""The frontend as shipped: what is in the package and what the browser can reach.

specs/tasks.md task 5.2; design.md §3.11 (`static`), D8, D20; requirements FR-33 (AC-33.1),
FR-25 (AC-25.2), EC-13.

This file asserts the properties that hold whether or not JavaScript ever runs — the ones
about *bytes*: which files ship, that the vendored libraries are the ones that were fetched
and have not been edited in place, and that every resource the page pulls comes from this
machine. The behavioral acceptance criteria (AC-26/27/28, EC-15) are asserted in
`test_viewer_frontend_js.py`, which executes `app.js`.

**On FR-33's "no external URL" check.** Task 5.2 words it as a grep over the shipped assets
that fails on any external URL. Taken to the letter that is unsatisfiable alongside D8's
mandatory vendoring: the MIT license notices inside `cytoscape.min.js`, and the ECMAScript
citations inside the dagre bundle's comments, are URL text in files that must ship
byte-identical to what was fetched. So the check is split at the line where it means
something:

* **authored assets** (`index.html`, `app.js`, `style.css`) — no external URL at all, of any
  kind, anywhere in the file. That is the strict grep, and it is the one that could
  actually regress;
* **all assets, vendored included** — nothing that a browser *fetches* may point off this
  machine: every subresource the page declares, every `@import`/`url()`, and every source
  map must be a local relative path, and no CDN host may be named at all.

The proof that no request leaves the machine at runtime lives in two other places:
`test_viewer_server.py::test_no_request_leaves_the_machine` (the server side) and
`test_viewer_frontend_js.py::test_no_workflow_touches_a_network_api` (the page side).
"""

from __future__ import annotations

import builtins
import os
import re
import tomllib
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from pastapathfinder.viewer import server

STATIC = server.STATIC_DIR
VENDOR = STATIC / "vendor"

#: The files the browser loads. `vendor/README.md` ships too — it is the provenance record
#: for the two libraries — but no browser ever requests it, so it is not a page asset.
AUTHORED = ("index.html", "app.js", "style.css")
VENDORED = ("vendor/cytoscape.min.js", "vendor/cytoscape-dagre.js")
PAGE_ASSETS = AUTHORED + VENDORED

#: Hosts that would mean the no-build frontend had quietly acquired a build (D8: no CDN).
CDN_HOSTS = (
    "unpkg.com",
    "jsdelivr.net",
    "cdnjs.cloudflare.com",
    "ajax.googleapis.com",
    "googleapis.com",
    "cdn.skypack.dev",
    "esm.sh",
    "cytoscape.org/download",
)

ABSOLUTE_URL = re.compile(r"(?:https?:)?//[A-Za-z0-9.\-]+", re.IGNORECASE)
SOURCE_MAP = re.compile(r"sourceMappingURL=(\S+)")


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# What ships
# ---------------------------------------------------------------------------


def test_every_shipped_asset_is_present():
    """design.md §3.11: `index.html`, `app.js`, `style.css`, and the vendored library."""
    missing = [name for name in (*PAGE_ASSETS, "vendor/README.md") if not (STATIC / name).is_file()]
    assert missing == []
    assert not (STATIC / ".gitkeep").exists(), "the scaffolding placeholder outlived its purpose"


def test_the_static_directory_lives_inside_the_installed_package():
    """The assets travel with the install — there is no build directory to find them in."""
    package_root = Path(server.__file__).resolve().parent.parent
    assert STATIC.resolve().is_relative_to(package_root)


def test_package_data_ships_every_asset():
    """A file the wheel does not carry is a viewer that works only from a source tree."""
    config = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_bytes().decode()
    )
    patterns = config["tool"]["setuptools"]["package-data"]["pastapathfinder"]
    unshipped = [
        str(path.relative_to(STATIC.parent.parent))
        for path in sorted(STATIC.rglob("*"))
        if path.is_file()
        and not any(path.relative_to(STATIC.parent.parent).match(pattern) for pattern in patterns)
    ]
    assert unshipped == []


# ---------------------------------------------------------------------------
# The vendored libraries are the ones that were fetched (D8)
# ---------------------------------------------------------------------------


def recorded_vendor_hashes() -> dict[str, str]:
    """The `(file, sha256)` pairs from `vendor/README.md`'s provenance table."""
    rows = {}
    for line in (VENDOR / "README.md").read_text(encoding="utf-8").splitlines():
        cells = [cell.strip().strip("`") for cell in line.split("|")]
        if len(cells) >= 8 and cells[1].endswith(".js"):
            rows[cells[1]] = cells[5]
    return rows


def test_the_vendored_libraries_match_their_recorded_hashes():
    """A vendored library that has been patched in place is what this table prevents.

    The point is not integrity against a hostile network — the files are in git — but
    against a well-meant local edit: there is no build step to regenerate them from, so a
    patched bundle would be permanent and invisible.
    """
    import hashlib

    recorded = recorded_vendor_hashes()
    assert set(recorded) == {"cytoscape.min.js", "cytoscape-dagre.js"}
    actual = {name: hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() for name in recorded}
    assert actual == recorded


# ---------------------------------------------------------------------------
# FR-33 — nothing the page loads comes from off this machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", AUTHORED)
def test_authored_assets_contain_no_external_url(name: str):
    """The strict grep: our own three files name no host, ever (FR-33, D8)."""
    assert ABSOLUTE_URL.findall(read(name)) == []


@pytest.mark.parametrize("name", PAGE_ASSETS)
def test_no_asset_names_a_cdn(name: str):
    """D8's "no CDN" as a property of the bytes, vendored files included."""
    text = read(name).lower()
    assert [host for host in CDN_HOSTS if host in text] == []


@pytest.mark.parametrize("name", PAGE_ASSETS)
def test_every_source_map_reference_is_local(name: str):
    """A source map is a fetch a browser may make; it must stay on this server."""
    for target in SOURCE_MAP.findall(read(name)):
        assert not ABSOLUTE_URL.match(target), f"{name} points its source map at {target}"


def test_the_stylesheet_fetches_nothing():
    """No `@import`, no `url(...)`: a stylesheet's two ways of pulling in a second file.

    Comments are stripped first — the file's own header says it uses neither, and a rule
    that its documentation trips is a rule nobody will keep.
    """
    css = re.sub(r"/\*.*?\*/", "", read("style.css"), flags=re.DOTALL)
    assert "@import" not in css
    assert "url(" not in css


def test_the_graph_style_loads_no_images():
    """Cytoscape fetches an image only for a `background-image` style; there is none.

    That is the single network-capable code path in the vendored library (`new Image` in
    its style handling), so naming no image URL closes it by construction.
    """
    app_js = read("app.js")
    assert "background-image" not in app_js
    assert "url(" not in app_js


# ---------------------------------------------------------------------------
# The page and the routes that serve it
# ---------------------------------------------------------------------------


class Subresources(HTMLParser):
    """Every file `index.html` tells the browser to load."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for attribute in ("src", "href"):
            url = values.get(attribute)
            if url:
                self.urls.append(url)
                if tag == "script":
                    self.scripts.append(url)


def subresources() -> Subresources:
    parser = Subresources()
    parser.feed(read("index.html"))
    return parser


@pytest.fixture
def client(tmp_path: Path):
    """A client over an output directory with **no index at all**.

    The page and its assets must be served regardless: the page is what renders AC-25.2's
    unreadable-index message, so an index-dependent asset route would leave the user with
    a blank browser and nothing to read the error in.
    """
    app = server.create_app(server.index_file(tmp_path))
    app.testing = True
    return app.test_client()


def test_the_page_is_served_at_the_root_even_with_no_index(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == read("index.html")


def test_every_subresource_the_page_declares_is_served_from_this_server(client):
    """The complete resource graph of the page, resolved (FR-33/AC-33.1).

    Each URL is checked twice: that it is a local absolute path rather than a host, and
    that requesting it returns the bytes on disk. Together those say the page renders with
    all external network access blocked — there is nothing off-machine left to fetch.
    """
    urls = subresources().urls
    assert urls, "index.html declares no assets at all — the parser or the page is wrong"

    for url in urls:
        assert url.startswith("/static/"), f"{url} is not served by this server"
        response = client.get(url)
        assert response.status_code == 200, f"{url} is declared but not served"
        assert response.get_data() == (STATIC / url[len("/static/") :]).read_bytes()


def test_the_page_loads_the_graph_library_before_the_application():
    """`app.js` registers the dagre layout against the cytoscape global as it evaluates."""
    scripts = subresources().scripts
    assert scripts == [
        "/static/vendor/cytoscape.min.js",
        "/static/vendor/cytoscape-dagre.js",
        "/static/app.js",
    ]


def test_an_unknown_asset_is_a_structured_error_not_html(client):
    """The §5.2 envelope covers the asset routes too — one error shape for the page."""
    response = client.get("/static/nothing-here.js")
    assert response.status_code == 404
    assert response.get_json() == {
        "error": {"code": "not_found", "message": response.get_json()["error"]["message"]}
    }


# ---------------------------------------------------------------------------
# D20 — serving the page reads the package, and nothing else
# ---------------------------------------------------------------------------


@pytest.fixture
def opened_paths(monkeypatch) -> Iterator[list[str]]:
    """Record every path the process opens through Python's file APIs."""
    recorded: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        recorded.append(str(file))
        return real_open(file, *args, **kwargs)

    def spy_os_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        recorded.append(str(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    yield recorded


def test_serving_the_page_reads_only_the_packages_own_static_directory(client, opened_paths):
    """D20 is about analysis data; the page's own files are the package's, not the run's.

    So the rule is stated where it bites: serving `/` and `/static/...` touches nothing
    outside `viewer/static/` — no index, no report, no file of the analyzed codebase.
    """
    del opened_paths[:]
    for url in ("/", *subresources().urls):
        assert client.get(url).status_code == 200

    static = STATIC.resolve()
    outside = []
    for path in opened_paths:
        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError, TypeError):  # pragma: no cover - fd ints
            continue
        if not resolved.is_relative_to(static):
            outside.append(path)
    assert outside == []


# ---------------------------------------------------------------------------
# index.html and app.js are one contract
# ---------------------------------------------------------------------------


def test_app_js_binds_only_element_ids_that_index_html_declares():
    """The two authored files drift apart silently; this is the check that they have not.

    `app.js` resolves its whole element table at startup and throws on a missing id, so a
    drift is a page that renders nothing. Catching it here names the id instead.
    """
    declared = set(re.findall(r'id="([^"]+)"', read("index.html")))
    listed = re.search(r"const IDS = \[(.*?)\];", read("app.js"), re.DOTALL)
    assert listed, "app.js no longer declares its element table as `const IDS = [...]`"
    bound = set(re.findall(r'"([^"]+)"', listed.group(1)))
    assert bound - declared == set(), "app.js binds ids index.html does not declare"

    # The reverse direction, allowing for the ids that exist only as layout hooks: every
    # declared id is either driven by the application or styled by the stylesheet, and one
    # that is neither is dead markup.
    styled = set(re.findall(r"#([A-Za-z0-9_-]+)", read("style.css")))
    assert declared - bound - styled == set(), "index.html declares ids nothing uses"
