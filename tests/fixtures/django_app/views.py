"""The views the URLconf fixture references, plus its negative control.

`unreferenced()` appears in no `urlpatterns` entry and must never be flagged as a route.
"""

from django.views import View


def foo(request):
    return "foo"


def legacy_report(request):
    """Referenced only by the loop-appended patterns, so statically unroutable."""
    return "report"


def unreferenced(request):
    """The negative control: defined, never routed."""
    return "unreferenced"


class FooView(View):
    def get(self, request):
        return "FooView"
