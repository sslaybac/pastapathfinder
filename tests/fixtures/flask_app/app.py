"""Flask route fixture — 5 routes, reprising `FINDINGS-harness.md` §2's `flask_app/`.

Never imported and never executed: the detectors read it with stdlib `ast` (D14, FR-13).
Two of the five rules are *variable rule strings* — a module constant and a concatenation —
so the route is detected while `attrs.route` carries no literal path. `not_a_route()` is the
negative control (AC-11.1).
"""

from flask import Flask

app = Flask(__name__)

PREFIX = "/api"
DETAIL_RULE = "/items/<int:item_id>"


@app.route("/")
def index():
    return "index"


@app.route("/items", methods=["GET"])
def list_items():
    return "items"


@app.route(DETAIL_RULE)
def item_detail(item_id):
    return f"item {item_id}"


@app.route(PREFIX + "/search")
def search():
    return "results"


@app.post("/items")
def create_item():
    return "created"


def not_a_route():
    """A plain helper: no decorator, so nothing to detect."""
    return "helper"
