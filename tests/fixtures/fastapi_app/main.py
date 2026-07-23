"""FastAPI route fixture — 6 routes, reprising `FINDINGS-harness.md` §2's `fastapi_app/`.

Never imported and never executed. Four routes are registered on the `app` receiver and two
on an `APIRouter(prefix="/admin")` whose routes become reachable only via `include_router`,
so `attrs.route.receiver` is what distinguishes them. `VERSION_PREFIX + "/status"` is a
variable rule string; `helper()` is the negative control; `register_dynamic()` is the
AC-11.3 control — an `add_api_route` loop no static reading can enumerate.
"""

from fastapi import APIRouter, FastAPI

app = FastAPI()
router = APIRouter(prefix="/admin")

VERSION_PREFIX = "/v1"


@app.get("/")
def read_root():
    return {"ok": True}


@app.get("/items/{item_id}")
def read_item(item_id: int):
    return {"item": item_id}


@app.post("/items")
def create_item():
    return {"created": True}


@app.get(VERSION_PREFIX + "/status")
def status():
    return {"status": "up"}


@router.get("/users")
def admin_users():
    return []


@router.delete("/users/{user_id}")
def admin_delete_user(user_id: int):
    return {"deleted": user_id}


app.include_router(router)


def helper():
    """A plain helper: no decorator, so nothing to detect."""
    return None


def register_dynamic(rules):
    """Programmatic registration: the handler is not statically resolvable (AC-11.3)."""
    for rule in rules:
        app.add_api_route(rule, helper)
