"""Django URLconf fixture, reprising `FINDINGS-harness.md` §2's `django_app/`.

Never imported and never executed. Two statically stated routes — a function view and a
class-based view through `as_view()` — plus an `include()` mount, plus the deliberate
negative control: `reports/*` patterns appended in a loop, which must be reported as
unresolved rather than silently missed (AC-11.3).
"""

from django.urls import include, path

from . import views
from .views import FooView

REPORT_SLUGS = ["daily", "weekly", "monthly"]

urlpatterns = [
    path("x", views.foo),
    path("y", FooView.as_view()),
    path("sub/", include("django_app.more")),
]

for slug in REPORT_SLUGS:
    urlpatterns.append(path(f"reports/{slug}", views.legacy_report))
