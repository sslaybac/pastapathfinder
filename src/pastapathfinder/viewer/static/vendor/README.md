# Vendored frontend libraries (design.md D8; FR-33)

These files are third-party code committed verbatim into the repository and shipped as
package data. Vendoring is mandatory, not a convenience: a CDN reference at runtime would
be external network communication, which FR-33 forbids (design.md D8). There is no npm
toolchain, no lockfile, and no build step — the browser loads these files exactly as they
appear here, from the local viewer's own `/static/vendor/` route.

Nothing in this directory is edited. If a file here needs changing, the change is a version
bump: refetch from the recorded URL, re-record the hash below, and re-run `pytest`.

| File | Package | Version | Source | SHA-256 | License |
|---|---|---|---|---|---|
| `cytoscape.min.js` | `cytoscape` | 3.34.0 | `https://unpkg.com/cytoscape@3.34.0/dist/cytoscape.min.js` | `9c2a3bf2592e0b14a1f7bec07c03a54f16dedf32af9cd0af155c716aa6c87bc3` | MIT |
| `cytoscape-dagre.js` | `cytoscape-dagre` | 4.0.0 | `https://unpkg.com/cytoscape-dagre@4.0.0/dist/cytoscape-dagre.js` | `91f342cc2705aa9cad6a26f468d9ee5faa9e057d9172c3f9e732548fc61c660d` | MIT |

Both licenses are MIT and their texts ride in each file's own header comment, which is why
these files are committed unminified-header and unmodified.

`cytoscape-dagre` 4.0.0 is a self-contained UMD bundle: it carries its copy of the dagre
layout engine, so the "dagre layout plugin" of design.md §3.11 is these two files and not
three. It registers itself as the global `cytoscapeDagre`, and `app.js` installs it with
`cytoscape.use(cytoscapeDagre)`.

The hashes above are the fetched bytes and are checked by `tests/unit/test_viewer_static.py`,
which fails if a file here is edited in place — a vendored library that has been quietly
patched is the failure mode this table exists to prevent.

Note for anyone testing these files under Node rather than a browser: the dagre bundle calls
`structuredClone`, which browsers have had since 2022 but Node gained only in 17. That is a
Node-16 gap, not a defect in the bundle.
