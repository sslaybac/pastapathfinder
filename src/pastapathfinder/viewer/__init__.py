"""The local viewer: server and no-build frontend (design.md §3.11; FR-25-FR-28).

Nothing in this package may import `mypy.*` or `pastapathfinder.adapters.*` (AC-25.1).

The bind constants live here rather than in `server` so that `cli` can read them while
building its parser without importing Flask on every `analyze` or `query` invocation;
`server` imports them from here, so there is still one definition site each.
"""

#: The loopback address, and the only address the viewer ever binds (design.md D7a).
#: FR-33 is a property of the bind: a viewer reachable from another machine would be
#: external network communication whether or not anyone used it.
HOST = "127.0.0.1"

#: design.md §3.11's default port.
DEFAULT_PORT = 8517

__all__ = ["DEFAULT_PORT", "HOST"]
