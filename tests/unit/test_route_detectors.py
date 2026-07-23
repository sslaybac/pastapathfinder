"""Task 3.3 — the Flask/FastAPI and Django URLconf route detectors.

design.md §3.7 (both normative detector rules), §4.1 (the `entry:` ID form), §4.2 (the
`route` `attrs` vocabulary), D14, D18; requirements FR-11 (AC-11.1, AC-11.2, AC-11.3).

The three fixture apps under `tests/fixtures/` reprise the prototype fixture designs of
`FINDINGS-harness.md` §2 — 5 Flask routes, 6 FastAPI routes across two receivers, 2 static
Django routes plus the loop-appended `reports/*` negative control — because those designs
already encode the interesting cases *and* their negative controls. None of them is ever
imported or executed: every test reads them with stdlib `ast` (D14, FR-13).
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

from conftest import write_tree
from pastapathfinder.adapters.python.normalize import code_node_id
from pastapathfinder.detectors import registry
from pastapathfinder.detectors.base import ModuleInput
from pastapathfinder.detectors.django_urlconf import DjangoUrlconfDetector
from pastapathfinder.detectors.flask_fastapi import FlaskFastapiRouteDetector
from pastapathfinder.detectors.registry import run_detectors
from pastapathfinder.schema import (
    FileRecord,
    GraphFragment,
    NodeRow,
    is_valid_node_id,
    validate_fragment,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fixture_source(relpath: str) -> str:
    """One fixture file's text, read — never imported (FR-13)."""
    return (FIXTURES / relpath).read_text(encoding="utf-8")


def module_input(relpath: str, source: str, qualnames: list[str] | None = None) -> ModuleInput:
    """A `ModuleInput` for `source`, with `qualnames` standing in for the run's node IDs.

    The node IDs a real run hands a detector come from the index; here they are stated
    explicitly so each test says which targets exist — which is also what makes the
    "target is gone" case (D18) expressible.
    """
    return ModuleInput.build(
        relpath,
        ast.parse(source),
        frozenset(code_node_id(qualname) for qualname in qualnames or []),
    )


def routes(output) -> list[tuple[str, str, str | None]]:
    """`(name, verb, literal path)` per emitted Flask/FastAPI entry, sorted."""
    return sorted(
        (node.name, node.attrs["route"]["verb"], node.attrs["route"].get("path"))
        for node in output.nodes
    )


def decorator_line(source: str, line: int) -> str:
    """The fixture's own text at a 1-based line — used to prove a span points at a decorator."""
    return source.splitlines()[line - 1]


def messages(output) -> str:
    """Every diagnostic message, joined — for asserting a construct was named."""
    return "\n".join(diag.message for diag in output.diagnostics)


# ---------------------------------------------------------------------------
# Flask — AC-11.1
# ---------------------------------------------------------------------------

FLASK_SOURCE = fixture_source("flask_app/app.py")
FLASK_MODULE = "flask_app.app"
FLASK_FUNCTIONS = [
    f"{FLASK_MODULE}.{name}"
    for name in ("index", "list_items", "item_detail", "search", "create_item", "not_a_route")
]


@pytest.fixture
def flask_output():
    module = module_input("flask_app/app.py", FLASK_SOURCE, FLASK_FUNCTIONS)
    return FlaskFastapiRouteDetector().detect(module)


def test_flask_fixture_yields_one_entry_per_route_decorator(flask_output):
    """AC-11.1: each of the fixture's 5 route decorators yields an entry-point node."""
    assert len(flask_output.nodes) == 5
    assert routes(flask_output) == [
        ("create_item", "post", "/items"),
        ("index", "route", "/"),
        ("item_detail", "route", None),  # DETAIL_RULE — a variable rule string
        ("list_items", "route", "/items"),
        ("search", "route", None),  # PREFIX + "/search" — a variable rule string
    ]


def test_flask_entries_target_the_decorated_function(flask_output):
    """The `calls` edge lands on the decorated function's node (§3.7)."""
    targets = sorted(edge.dst for edge in flask_output.edges)
    assert targets == sorted(
        code_node_id(f"{FLASK_MODULE}.{name}")
        for name in ("create_item", "index", "item_detail", "list_items", "search")
    )
    assert all(edge.kind == "calls" for edge in flask_output.edges)
    assert all(edge.src_file is None for edge in flask_output.edges)


def test_flask_variable_rule_strings_are_routes_without_a_literal_path(flask_output):
    """A computed rule is detected as a route; the literal path is absent, never invented."""
    variable = [n for n in flask_output.nodes if n.name in {"item_detail", "search"}]
    assert len(variable) == 2
    for node in variable:
        assert "path" not in node.attrs["route"]
        assert node.attrs["route"]["receiver"] == "app"


def test_flask_negative_control_is_not_flagged(flask_output):
    """AC-11.1's control: `not_a_route()` carries no route decorator, so it is not an entry."""
    assert "not_a_route" not in {node.name for node in flask_output.nodes}
    assert code_node_id(f"{FLASK_MODULE}.not_a_route") not in {e.dst for e in flask_output.edges}


def test_flask_entries_are_wellformed_and_located(flask_output):
    """§4.1 IDs, the `@line` disambiguator, and a span that starts at the decorator."""
    for node in flask_output.nodes:
        assert is_valid_node_id(node.id)
        assert node.kind == "entry_point"
        assert node.attrs["detector"] == "route_flask_fastapi"
        assert node.file_path == "flask_app/app.py"
        assert node.id.endswith(f"@{node.start_line}")
        assert decorator_line(FLASK_SOURCE, node.start_line).startswith("@")
        assert node.end_line is not None and node.end_line > node.start_line


def test_flask_fixture_records_no_unresolved_construct(flask_output):
    """Every registration in the Flask fixture is a decorator on a def; nothing is dynamic."""
    assert flask_output.diagnostics == []


# ---------------------------------------------------------------------------
# FastAPI — AC-11.1, AC-11.3
# ---------------------------------------------------------------------------

FASTAPI_SOURCE = fixture_source("fastapi_app/main.py")
FASTAPI_MODULE = "fastapi_app.main"
FASTAPI_FUNCTIONS = [
    f"{FASTAPI_MODULE}.{name}"
    for name in (
        "read_root",
        "read_item",
        "create_item",
        "status",
        "admin_users",
        "admin_delete_user",
        "helper",
        "register_dynamic",
    )
]


@pytest.fixture
def fastapi_output():
    module = module_input("fastapi_app/main.py", FASTAPI_SOURCE, FASTAPI_FUNCTIONS)
    return FlaskFastapiRouteDetector().detect(module)


def test_fastapi_fixture_yields_six_routes(fastapi_output):
    """AC-11.1: 4 routes on `app` and 2 on the `APIRouter`, including a variable rule."""
    assert len(fastapi_output.nodes) == 6
    assert routes(fastapi_output) == [
        ("admin_delete_user", "delete", "/users/{user_id}"),
        ("admin_users", "get", "/users"),
        ("create_item", "post", "/items"),
        ("read_item", "get", "/items/{item_id}"),
        ("read_root", "get", "/"),
        ("status", "get", None),  # VERSION_PREFIX + "/status"
    ]


def test_fastapi_attrs_distinguish_the_app_receiver_from_the_router(fastapi_output):
    """`attrs.route.receiver` separates the `FastAPI()` app from the `APIRouter()` (§4.2)."""
    by_receiver: dict[str, set[str]] = {}
    for node in fastapi_output.nodes:
        by_receiver.setdefault(node.attrs["route"]["receiver"], set()).add(node.name)
    assert by_receiver == {
        "app": {"read_root", "read_item", "create_item", "status"},
        "router": {"admin_users", "admin_delete_user"},
    }


def test_fastapi_router_routes_are_detected_without_include_router(fastapi_output):
    """Routes on a router are entries in their own right; `include_router` is not required."""
    assert {"admin_users", "admin_delete_user"} <= {node.name for node in fastapi_output.nodes}


def test_fastapi_negative_control_is_not_flagged(fastapi_output):
    """`helper()` carries no decorator; `register_dynamic()` registers, but is not itself one."""
    assert {"helper", "register_dynamic"} & {node.name for node in fastapi_output.nodes} == set()


def test_fastapi_add_api_route_is_recorded_unresolved(fastapi_output):
    """AC-11.3: dynamic `add_api_route` registration is reported, not silently missed."""
    dynamic = [d for d in fastapi_output.diagnostics if "add_api_route" in d.message]
    assert len(dynamic) == 1
    (diag,) = dynamic
    assert diag.kind == "unresolved_entry_declaration"
    assert diag.path == "fastapi_app/main.py"
    assert diag.line is not None and "add_api_route" in decorator_line(FASTAPI_SOURCE, diag.line)
    assert "no route is fabricated" in diag.message
    # And it fabricated nothing: the only entries are the six decorated handlers.
    assert len(fastapi_output.nodes) == 6


# ---------------------------------------------------------------------------
# Django — AC-11.2, AC-11.3
# ---------------------------------------------------------------------------

DJANGO_URLS = fixture_source("django_app/urls.py")
DJANGO_VIEWS_MODULE = "django_app.views"
DJANGO_NODES = [
    f"{DJANGO_VIEWS_MODULE}.{name}" for name in ("foo", "legacy_report", "unreferenced", "FooView")
]


@pytest.fixture
def django_output():
    return DjangoUrlconfDetector().detect(
        module_input("django_app/urls.py", DJANGO_URLS, DJANGO_NODES)
    )


def test_django_resolves_a_function_view_and_a_class_based_view(django_output):
    """AC-11.2: `path("x", views.foo)` and `path("y", FooView.as_view())` both resolve."""
    assert len(django_output.nodes) == 2
    paired = zip(django_output.nodes, django_output.edges, strict=True)
    assert sorted((node.name, edge.dst) for node, edge in paired) == [
        ("FooView", code_node_id(f"{DJANGO_VIEWS_MODULE}.FooView")),
        ("foo", code_node_id(f"{DJANGO_VIEWS_MODULE}.foo")),
    ]


def test_django_entries_carry_their_literal_pattern(django_output):
    """§4.2's `route` `attrs` for Django: `{pattern}`, from the literal route string."""
    patterns = sorted(node.attrs["route"]["pattern"] for node in django_output.nodes)
    assert patterns == ["x", "y"]
    for node in django_output.nodes:
        assert node.attrs["detector"] == "route_django"
        assert node.file_path == "django_app/urls.py"
        assert is_valid_node_id(node.id)


def test_django_negative_control_is_not_flagged(django_output):
    """`unreferenced()` appears in no pattern, so no entry targets it."""
    assert code_node_id(f"{DJANGO_VIEWS_MODULE}.unreferenced") not in {
        edge.dst for edge in django_output.edges
    }


def test_django_loop_appended_patterns_are_recorded_unresolved(django_output):
    """AC-11.3: the deliberate control — `urlpatterns.append(...)` in a loop is reported."""
    assert len(django_output.diagnostics) == 1
    (diag,) = django_output.diagnostics
    assert diag.kind == "unresolved_entry_declaration"
    assert diag.path == "django_app/urls.py"
    assert "urlpatterns.append" in diag.message
    assert "no route is fabricated" in diag.message
    # Nothing fabricated: `legacy_report` is reachable only through that loop, so it is not
    # a route, and the two statically stated patterns are the only entries.
    assert code_node_id(f"{DJANGO_VIEWS_MODULE}.legacy_report") not in {
        edge.dst for edge in django_output.edges
    }
    assert len(django_output.nodes) == 2


def test_django_include_is_left_to_the_included_module(django_output):
    """`include("mod")` names no view here; D18's wholesale pass detects that module's own."""
    assert all("include" not in diag.message for diag in django_output.diagnostics)
    assert len(django_output.nodes) == 2


def test_only_mutations_that_add_patterns_are_dynamic_registration():
    """`append`/`extend`/`insert` add routes; reading the list adds none and is not reported."""
    source = (
        "from django.urls import path\nfrom . import views\n"
        "urlpatterns = [path('a', views.foo)]\n"
        "urlpatterns.extend(build())\n"
        "snapshot = urlpatterns.copy()\n"
    )
    out = DjangoUrlconfDetector().detect(module_input("app/urls.py", source, ["app.views.foo"]))
    assert [edge.dst for edge in out.edges] == [code_node_id("app.views.foo")]
    assert len(out.diagnostics) == 1
    assert "urlpatterns.extend" in out.diagnostics[0].message


def test_an_augmented_assignment_of_a_literal_list_still_states_its_patterns():
    """`urlpatterns += [path(...)]` is §3.7's concatenation shape written the other way."""
    source = (
        "from django.urls import path\nfrom . import views\n"
        "urlpatterns = [path('a', views.foo)]\n"
        "urlpatterns += [path('b', views.bar)]\n"
    )
    out = DjangoUrlconfDetector().detect(
        module_input("app/urls.py", source, ["app.views.foo", "app.views.bar"])
    )
    assert sorted(edge.dst for edge in out.edges) == sorted(
        code_node_id(q) for q in ("app.views.foo", "app.views.bar")
    )
    assert out.diagnostics == []


def test_a_module_without_urlpatterns_is_not_a_urlconf():
    """The detector emits nothing for every other analyzed file (it runs over all of them)."""
    out = DjangoUrlconfDetector().detect(
        module_input("app/views.py", fixture_source("django_app/views.py"))
    )
    assert out.nodes == [] and out.diagnostics == []


# ---------------------------------------------------------------------------
# AC-11.3 — the shapes that must be reported rather than guessed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "quoted"),
    [
        # A comprehension: the patterns exist only at runtime.
        (
            "from django.urls import path\nfrom . import views\n"
            "urlpatterns = [path(p, views.foo) for p in RULES]\n",
            "for p in RULES",
        ),
        # A name spliced in from elsewhere.
        (
            "from . import views\nurlpatterns = extra_patterns\n",
            "extra_patterns",
        ),
        # A `+` concatenation with one computed operand: the literal half still resolves.
        (
            "from django.urls import path\nfrom . import views\n"
            "urlpatterns = [path('a', views.foo)] + legacy\n",
            "legacy",
        ),
        # A starred splice inside an otherwise literal list.
        (
            "from django.urls import path\nfrom . import views\n"
            "urlpatterns = [path('a', views.foo), *legacy]\n",
            "*legacy",
        ),
        # A view expression built by a call that is not `as_view()`.
        (
            "from django.urls import path\nfrom . import views\n"
            "urlpatterns = [path('a', make_view('foo'))]\n",
            "make_view",
        ),
        # A view named by a string, the pre-1.10 Django spelling this detector cannot chase.
        (
            "from django.urls import path\nurlpatterns = [path('a', 'app.views.foo')]\n",
            "'app.views.foo'",
        ),
        # An include whose URLconf is computed.
        (
            "from django.urls import include, path\n"
            "urlpatterns = [path('a', include(module_name))]\n",
            "include(module_name)",
        ),
        # An entry that is not a pattern call at all.
        (
            "urlpatterns = [handler]\n",
            "handler",
        ),
    ],
)
def test_dynamic_urlconf_constructs_are_reported_and_never_fabricated(source, quoted):
    """AC-11.3: each shape yields a diagnostic quoting the construct, and no bogus route."""
    out = DjangoUrlconfDetector().detect(
        module_input("app/urls.py", source, [f"app.views.{n}" for n in ("foo",)])
    )
    assert [d.kind for d in out.diagnostics] == ["unresolved_entry_declaration"] * len(
        out.diagnostics
    )
    assert quoted in messages(out)
    # No fabricated route: the only entries are ones whose view was stated literally.
    assert all(edge.dst == code_node_id("app.views.foo") for edge in out.edges)


def test_a_partly_dynamic_urlconf_still_yields_its_static_routes():
    """FR-6's posture at detector scale: the literal half of a `+` is still resolved."""
    source = (
        "from django.urls import path\nfrom . import views\n"
        "urlpatterns = [path('a', views.foo)] + legacy\n"
    )
    out = DjangoUrlconfDetector().detect(module_input("app/urls.py", source, ["app.views.foo"]))
    assert [edge.dst for edge in out.edges] == [code_node_id("app.views.foo")]
    assert len(out.diagnostics) == 1


@pytest.mark.parametrize(
    ("source", "quoted"),
    [
        # A route decorator applied as a plain call — the loop-registration idiom.
        ("for rule in rules:\n    app.route(rule)(handler)\n", "app.route(rule)(handler)"),
        # Flask's programmatic registrar.
        ("app.add_url_rule('/x', 'x', handler)\n", "add_url_rule"),
        # FastAPI's websocket registrar.
        ("app.add_websocket_route('/ws', handler)\n", "add_websocket_route"),
        # A route decorator on a class: there is no handler def to point at.
        ("@app.route('/x')\nclass Resource:\n    pass\n", "decorates class 'Resource'"),
    ],
)
def test_dynamic_route_registration_is_reported_and_never_fabricated(source, quoted):
    """AC-11.3 for Flask/FastAPI: registration off the decorator path is named, not guessed."""
    out = FlaskFastapiRouteDetector().detect(module_input("app/main.py", source))
    assert out.nodes == [] and out.edges == []
    assert [d.kind for d in out.diagnostics] == ["unresolved_entry_declaration"]
    assert quoted in out.diagnostics[0].message


def test_an_ordinary_get_call_is_not_mistaken_for_route_registration():
    """The bound is deliberate: `<name>.<verb>(...)` calls are dict/HTTP-client calls far
    more often than routes, and diagnosing every one would bury the real ones."""
    source = (
        "import requests\n"
        "def work(mapping, session):\n"
        "    mapping.get('k')\n"
        "    requests.post('http://x')\n"
        "    session.delete(1)\n"
    )
    out = FlaskFastapiRouteDetector().detect(module_input("app/work.py", source))
    assert out.nodes == [] and out.diagnostics == []


# ---------------------------------------------------------------------------
# Resolution: the target must exist (AC-23.2, and D18's vanished view)
# ---------------------------------------------------------------------------


def test_a_route_whose_target_is_not_in_the_run_is_unresolved_not_fabricated():
    """D18: a deleted view yields an AC-11.3 diagnostic, never an edge to a missing node."""
    source = (
        "from django.urls import path\nfrom . import views\nurlpatterns = [path('a', views.gone)]\n"
    )
    out = DjangoUrlconfDetector().detect(module_input("app/urls.py", source, ["app.views.other"]))
    assert out.nodes == [] and out.edges == []
    assert out.diagnostics[0].kind == "unresolved_entry_declaration"
    assert "app.views.gone" in out.diagnostics[0].message


def test_a_flask_route_whose_function_has_no_node_is_unresolved():
    """The same rule on the decorator side: no node, no edge, one diagnostic."""
    source = "@app.get('/x')\ndef handler():\n    return 1\n"
    out = FlaskFastapiRouteDetector().detect(module_input("app/main.py", source))
    assert out.nodes == []
    assert [d.kind for d in out.diagnostics] == ["unresolved_entry_declaration"]
    assert "app.main.handler" in out.diagnostics[0].message


def test_a_colliding_qualname_resolves_to_a_line_variant_and_flags_ambiguity():
    """§4.1's `@line` collision form: the lowest-line variant, with `is_ambiguous=1` (FR-40)."""
    source = "@app.get('/x')\ndef handler():\n    return 1\n"
    module = ModuleInput.build(
        "app/main.py",
        ast.parse(source),
        frozenset({"python:app.main.handler@2", "python:app.main.handler@9"}),
    )
    out = FlaskFastapiRouteDetector().detect(module)
    (edge,) = out.edges
    assert edge.dst == "python:app.main.handler@2"
    assert edge.is_ambiguous == 1


# ---------------------------------------------------------------------------
# Scopes: routes are not only on top-level defs
# ---------------------------------------------------------------------------


def test_a_route_on_a_nested_def_carries_its_scope_chain():
    """The application-factory idiom: the entry targets `mod.create_app.index`."""
    source = (
        "def create_app():\n"
        "    app = Flask(__name__)\n"
        "\n"
        "    @app.route('/')\n"
        "    def index():\n"
        "        return 'i'\n"
        "\n"
        "    return app\n"
    )
    out = FlaskFastapiRouteDetector().detect(
        module_input("pkg/factory.py", source, ["pkg.factory.create_app.index"])
    )
    (edge,) = out.edges
    assert edge.dst == code_node_id("pkg.factory.create_app.index")


def test_a_route_on_a_method_carries_its_class():
    source = "class Api:\n    @app.get('/x')\n    def read(self):\n        return 1\n"
    out = FlaskFastapiRouteDetector().detect(
        module_input("pkg/api.py", source, ["pkg.api.Api.read"])
    )
    (edge,) = out.edges
    assert edge.dst == code_node_id("pkg.api.Api.read")


def test_stacked_route_decorators_yield_one_entry_each():
    """Two routes on one handler are two entries, distinguished by the decorator line."""
    source = "@app.route('/a')\n@app.route('/b')\ndef handler():\n    return 1\n"
    out = FlaskFastapiRouteDetector().detect(module_input("m.py", source, ["m.handler"]))
    assert sorted(node.id for node in out.nodes) == [
        "python:entry:route_flask_fastapi:m.handler@1",
        "python:entry:route_flask_fastapi:m.handler@2",
    ]
    assert sorted(node.attrs["route"]["path"] for node in out.nodes) == ["/a", "/b"]


def test_an_async_handler_is_detected():
    """FastAPI handlers are usually `async def`; the decorator rule does not care."""
    source = "@app.get('/x')\nasync def handler():\n    return 1\n"
    out = FlaskFastapiRouteDetector().detect(module_input("m.py", source, ["m.handler"]))
    assert [edge.dst for edge in out.edges] == [code_node_id("m.handler")]


def test_a_urlconf_view_defined_in_the_urlconf_itself_resolves_locally():
    """A name the import table does not bind is this module's own definition."""
    source = (
        "from django.urls import path\n"
        "def home(request):\n    return 1\n"
        "urlpatterns = [path('', home)]\n"
    )
    out = DjangoUrlconfDetector().detect(module_input("app/urls.py", source, ["app.urls.home"]))
    assert [edge.dst for edge in out.edges] == [code_node_id("app.urls.home")]


def test_a_keyword_view_argument_resolves():
    """`path(route, view=…)` names the same view as the positional form."""
    source = (
        "from django.urls import path\nfrom . import views\n"
        "urlpatterns = [path('a', view=views.foo)]\n"
    )
    out = DjangoUrlconfDetector().detect(module_input("app/urls.py", source, ["app.views.foo"]))
    assert [edge.dst for edge in out.edges] == [code_node_id("app.views.foo")]


def test_re_path_and_url_are_pattern_calls_too():
    """§3.7 names all three spellings; `re_path`/`url` are the ones legacy code uses."""
    source = (
        "from django.conf.urls import url\nfrom django.urls import re_path\nfrom . import views\n"
        "urlpatterns = [re_path(r'^a$', views.foo), url(r'^b$', views.bar)]\n"
    )
    out = DjangoUrlconfDetector().detect(
        module_input("app/urls.py", source, ["app.views.foo", "app.views.bar"])
    )
    assert sorted(edge.dst for edge in out.edges) == sorted(
        code_node_id(q) for q in ("app.views.foo", "app.views.bar")
    )


def test_a_computed_django_pattern_string_still_routes_without_a_literal():
    """The Django half of the variable-rule case: the view resolves, the pattern is absent."""
    source = (
        "from django.urls import path\nfrom . import views\n"
        "urlpatterns = [path(PREFIX + 'a', views.foo)]\n"
    )
    out = DjangoUrlconfDetector().detect(module_input("app/urls.py", source, ["app.views.foo"]))
    (node,) = out.nodes
    assert "route" not in node.attrs
    assert [edge.dst for edge in out.edges] == [code_node_id("app.views.foo")]


# ---------------------------------------------------------------------------
# Schema conformance, registry wiring, and determinism
# ---------------------------------------------------------------------------


def test_emitted_route_rows_validate(flask_output):
    """The entry rows and their `calls` edges conform to §4.2 (AC-22.1, AC-23.2)."""
    relpath = "flask_app/app.py"
    targets = [
        NodeRow(
            id=code_node_id(qualname),
            kind="function",
            name=qualname.rpartition(".")[2],
            language="python",
            file_path=relpath,
            start_line=1,
            end_line=2,
        )
        for qualname in FLASK_FUNCTIONS
    ]
    fragment = GraphFragment(
        file=FileRecord(path=relpath, content_hash="0" * 64, status="analyzed"),
        nodes=[*targets, *flask_output.nodes],
        edges=flask_output.edges,
    )
    validate_fragment(fragment)  # raises FragmentValidationError on any nonconformance


def test_both_route_detectors_are_registered_in_design_order():
    """§3.7's ordered list, now complete (AC-8.1: one new module + one registry entry)."""
    assert [detector.name for detector in registry.DETECTORS] == [
        "main_block",
        "console_script",
        "route_flask_fastapi",
        "route_django",
    ]


def test_the_registry_runs_both_route_detectors_over_every_module(tmp_path):
    """The route entries appear from a plain `run_detectors()` call, not only in isolation."""
    from pastapathfinder.detectors.base import ProjectInput

    node_ids = frozenset(
        code_node_id(q) for q in (*FLASK_FUNCTIONS, *DJANGO_NODES, f"{DJANGO_VIEWS_MODULE}.foo")
    )
    modules = [
        ModuleInput.build("flask_app/app.py", ast.parse(FLASK_SOURCE), node_ids),
        ModuleInput.build("django_app/urls.py", ast.parse(DJANGO_URLS), node_ids),
    ]
    out = run_detectors(modules, ProjectInput.discover(tmp_path))
    detectors = sorted({node.attrs["detector"] for node in out.nodes})
    assert detectors == ["route_django", "route_flask_fastapi"]
    assert len(out.nodes) == 7  # 5 Flask routes + 2 Django views


def test_detection_is_a_pure_function_of_the_tree(tmp_path):
    """D18: no cross-run state — the same tree detects identically, twice."""
    module = module_input("flask_app/app.py", FLASK_SOURCE, FLASK_FUNCTIONS)
    first = FlaskFastapiRouteDetector().detect(module)
    second = FlaskFastapiRouteDetector().detect(module)
    assert [n.id for n in first.nodes] == [n.id for n in second.nodes]
    assert [(e.src, e.dst) for e in first.edges] == [(e.src, e.dst) for e in second.edges]
    assert first.diagnostics == second.diagnostics


def test_django_output_is_ordered_by_source_position(django_output):
    """Deterministic emission order: entries and diagnostics read in file order."""
    lines = [node.start_line for node in django_output.nodes]
    assert lines == sorted(lines)


# ---------------------------------------------------------------------------
# The IDs agree with the ones the adapter actually writes
# ---------------------------------------------------------------------------


#: Routes in the three scopes whose qualnames are constructed differently — module level, a
#: nested def inside an application factory, and a method. The receiver is defined in the
#: file so the run needs no third-party package: what is under test is ID agreement, not
#: whether mypy can find Flask.
ID_AGREEMENT_SOURCE = (
    "class Router:\n"
    "    def route(self, rule):\n"
    "        return rule\n"
    "\n"
    "    def get(self, rule):\n"
    "        return rule\n"
    "\n"
    "\n"
    "app = Router()\n"
    "\n"
    "\n"
    '@app.route("/")\n'
    "def index():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def create_app():\n"
    '    @app.get("/nested")\n'
    "    def nested():\n"
    "        return 2\n"
    "\n"
    "    return nested\n"
    "\n"
    "\n"
    "class Api:\n"
    '    @app.get("/method")\n'
    "    def read(self):\n"
    "        return 3\n"
)


def test_route_targets_are_ids_a_real_analyze_run_produced(tmp_path):
    """The claim the whole detector rests on, checked against the real thing.

    A detector resolves a route to a node ID; that ID must be the one `extract.py` wrote,
    or the entry edge would dangle (AC-23.2). Here an actual `analyze` run supplies the node
    IDs, and every route resolves against them — so the qualnames this detector builds from
    a stdlib parse and the ones the adapter builds from the engine's trees are the same
    strings, in all three scope shapes.
    """
    from pastapathfinder.index import open_index
    from pastapathfinder.progress import ProgressSink
    from pastapathfinder.runner import run_analysis

    root = write_tree(tmp_path / "code", {"svc/routes.py": ID_AGREEMENT_SOURCE})
    out = tmp_path / "out"
    with io.StringIO() as sink:
        run_analysis(root, out=out, progress=ProgressSink(sink, interval=0.0), stdout=sink)

    with open_index(out / "index.sqlite") as index:
        node_ids = frozenset(index.node_ids())

    module = ModuleInput.build("svc/routes.py", ast.parse(ID_AGREEMENT_SOURCE), node_ids)
    result = FlaskFastapiRouteDetector().detect(module)
    assert result.diagnostics == []
    assert sorted(edge.dst for edge in result.edges) == [
        "python:svc.routes.Api.read",
        "python:svc.routes.create_app.nested",
        "python:svc.routes.index",
    ]
    assert all(edge.dst in node_ids for edge in result.edges)
