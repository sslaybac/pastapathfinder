/*
 * pastapathfinder viewer — the no-build frontend (design.md §3.11 `static`, D8).
 *
 * Requirements: FR-25 (AC-25.2), FR-26 (AC-26.1/26.2), FR-27 (AC-27.1/27.2/27.3),
 * FR-28 (AC-28.1/28.2), FR-19 (AC-19.2/19.3), FR-33, EC-6, EC-13, EC-15.
 *
 * Three properties this file is written to keep:
 *
 * 1. **Every byte is local.** No CDN, no npm, no build step (FR-33, D8). The only network
 *    calls are same-origin `/api/...` requests to the viewer's own server, and the only
 *    libraries are the two files under `vendor/`.
 * 2. **The index is reached only through the §5.2 API.** This page knows nothing about
 *    SQLite, the schema, or the engine; it knows six endpoints and their shapes, which
 *    design.md R2 calls stable while this frontend iterates.
 * 3. **No failure is silent.** Every code path that can fail ends in visible text: a
 *    full-screen state when the index itself cannot be read (AC-25.2), an in-view banner
 *    when a query fails (AC-27.2). A blank view is the one outcome this file forbids.
 *
 * The DOM vocabulary used here is deliberately small — `getElementById`, `createElement`,
 * `textContent`, `className`, `hidden`, `value`, `appendChild`, `replaceChildren`,
 * `addEventListener` — because `tests/unit/test_viewer_frontend_js.py` drives this file
 * under a stub DOM to assert the acceptance criteria above on real rendered output.
 */

(function (global) {
  "use strict";

  // The dagre layout is a plugin registered against the cytoscape global; index.html
  // loads both vendored files before this one, so both are present by now.
  global.cytoscape.use(global.cytoscapeDagre);

  // -------------------------------------------------------------------------
  // The API client (§5.2)
  // -------------------------------------------------------------------------

  /** A structured `{error: {code, message}}` from the server, or the failure to reach it. */
  class ApiError extends Error {
    constructor(code, message) {
      super(message);
      this.code = code;
    }
  }

  /** Codes that mean the index itself is unusable — the full-screen states (AC-25.2). */
  const FATAL_CODES = ["index_missing", "index_incompatible", "unreachable"];

  /**
   * `GET /api/<path>`, resolving to the parsed body or throwing `ApiError`.
   *
   * Every §5.2 failure arrives as `{error: {code, message}}` with an HTTP status, so the
   * error path reads the body rather than inventing a message from the status: the server
   * already wrote one that names the problem and its remedy, and AC-27.2 asks for that
   * text, not a paraphrase of it.
   */
  async function api(path) {
    let response;
    try {
      response = await fetch("/api" + path);
    } catch (failure) {
      throw new ApiError(
        "unreachable",
        "cannot reach the viewer server at this address: " +
          (failure && failure.message ? failure.message : String(failure))
      );
    }
    let body = null;
    try {
      body = await response.json();
    } catch (ignored) {
      body = null;
    }
    if (body && body.error) {
      throw new ApiError(body.error.code, body.error.message);
    }
    if (!response.ok) {
      throw new ApiError("http_" + response.status, "the server answered HTTP " + response.status);
    }
    return body;
  }

  const encodeId = (id) => encodeURIComponent(id);

  // -------------------------------------------------------------------------
  // Elements
  // -------------------------------------------------------------------------

  const el = {};
  const IDS = [
    "fatal",
    "fatal-title",
    "fatal-message",
    "fatal-hint",
    "fatal-retry",
    "meta-root",
    "meta-counts",
    "tab-entry-points",
    "tab-dead-code",
    "entry-points-pane",
    "entry-point-list",
    "entry-points-empty",
    "search-form",
    "search-input",
    "search-submit",
    "search-status",
    "search-results",
    "dead-code-pane",
    "dead-code-caveat",
    "dead-code-warning",
    "dead-code-status",
    "dead-code-list",
    "trace-origin",
    "direction-forward",
    "direction-backward",
    "back-to-entry-points",
    "trace-error",
    "truncation-banner",
    "truncation-message",
    "frontier-expand",
    "frontier-list",
    "trace-empty",
    "trace-placeholder",
    "graph",
    "node-panel-empty",
    "node-panel-body",
    "node-panel-name",
    "node-panel-kind",
    "node-panel-location",
    "node-panel-reachable",
    "node-panel-id",
    "node-panel-slice-forward",
    "node-panel-slice-backward",
  ];

  /** Bind every id in `IDS`, failing loudly if index.html and this file have drifted. */
  function bind() {
    IDS.forEach(function (id) {
      const node = document.getElementById(id);
      if (!node) {
        throw new Error("index.html is missing the element #" + id);
      }
      el[id] = node;
    });
  }

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  const state = {
    /** The slice origin currently displayed, or null before one is chosen. */
    origin: null,
    originLabel: "",
    direction: "forward",
    /**
     * The node budget for the next slice request, or null to let the server apply its own
     * default. The default lives in `queries.SLICE_MAX_NODES` (design.md §8-O2's
     * provisional bound) and is deliberately *not* copied here: this page learns the bound
     * from the truncated response it gets back, so tuning O2 changes one constant.
     */
    maxNodes: null,
    slice: null,
    selected: null,
    tab: "entry-points",
  };

  let cy = null;

  // -------------------------------------------------------------------------
  // Small rendering helpers
  // -------------------------------------------------------------------------

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function button(className, text, onClick) {
    const node = element("button", className, text);
    node.addEventListener("click", onClick);
    return node;
  }

  function show(node, text) {
    node.textContent = text;
    node.hidden = false;
  }

  function hide(node) {
    node.hidden = true;
  }

  // -------------------------------------------------------------------------
  // Error surfaces
  // -------------------------------------------------------------------------

  /**
   * The full-screen state for an unreadable index (AC-25.2, AC-20.2, EC-13).
   *
   * The server's own message is shown verbatim — it names the file and says to re-run
   * `analyze` — and the hint repeats the remedy in the viewer's own words, because a user
   * who reached this page may not know what produced the index.
   */
  function showFatal(error) {
    el["fatal-title"].textContent =
      error.code === "index_incompatible"
        ? "This index was written by a different schema version"
        : "The index cannot be read";
    el["fatal-message"].textContent = error.message;
    el["fatal-hint"].textContent =
      "Re-run `pastapathfinder analyze <root>` to build or rebuild the index, then reload " +
      "this page. Nothing is shown until the index can be read.";
    el.fatal.hidden = false;
  }

  function clearFatal() {
    el.fatal.hidden = true;
  }

  /** A query failure, surfaced in the trace view rather than swallowed (AC-27.2). */
  function showQueryError(error) {
    show(el["trace-error"], error.message);
  }

  /**
   * EC-15: the selected node is gone — the index was rebuilt underneath this page.
   *
   * The error is stated, the stale trace is torn down rather than left on screen looking
   * current, and the user lands back on a view that is certainly valid: the entry-point
   * list, reloaded from the index that exists now.
   */
  async function handleVanishedNode(error) {
    clearTrace();
    show(
      el["trace-error"],
      error.message +
        " — the index has changed since this view was opened; showing the current entry points."
    );
    state.origin = null;
    state.slice = null;
    clearNodePanel();
    showTab("entry-points");
    await loadEntryPoints();
  }

  /** Route an `ApiError` to the surface that matches what it means. */
  async function handleError(error) {
    if (!(error instanceof ApiError)) {
      throw error;
    }
    if (FATAL_CODES.indexOf(error.code) !== -1) {
      showFatal(error);
      return;
    }
    if (error.code === "unknown_node") {
      await handleVanishedNode(error);
      return;
    }
    showQueryError(error);
  }

  // -------------------------------------------------------------------------
  // Masthead
  // -------------------------------------------------------------------------

  function renderMeta(meta) {
    el["meta-root"].textContent = meta.root_path || "";
    const counts = meta.counts || {};
    el["meta-counts"].textContent =
      counts.files +
      " files · " +
      counts.nodes +
      " nodes · " +
      counts.edges +
      " edges · " +
      counts.entry_points +
      " entry points";
  }

  // -------------------------------------------------------------------------
  // Entry points (FR-26)
  // -------------------------------------------------------------------------

  /** One line of the entry-point list: what it is, what it starts, and where it lives. */
  function entryPointLabel(entry) {
    const where = entry.file_path
      ? entry.file_path + (entry.start_line ? ":" + entry.start_line : "")
      : "no source location";
    return entry.name + " · " + entry.detector + " · " + where;
  }

  function renderEntryPoints(entries) {
    const items = entries.map(function (entry) {
      const item = element("li", "listing-item");
      item.appendChild(
        button("link", entryPointLabel(entry), function () {
          openSlice(entry.id, "forward", entry.name);
        })
      );
      return item;
    });
    el["entry-point-list"].replaceChildren.apply(el["entry-point-list"], items);

    if (entries.length === 0) {
      // AC-26.2 / EC-9: a library codebase legitimately has none. Say so, and point at
      // the alternative (FR-17) rather than presenting an empty list as a result.
      show(
        el["entry-points-empty"],
        "No entry points were detected in this codebase. That is the expected result for " +
          "a library, whose entry points are its public API. Search for any function, " +
          "method, or module below and slice from it instead."
      );
    } else {
      hide(el["entry-points-empty"]);
    }
  }

  async function loadEntryPoints() {
    try {
      renderEntryPoints((await api("/entry-points")).entry_points);
    } catch (error) {
      await handleError(error);
    }
  }

  // -------------------------------------------------------------------------
  // Node search — FR-17's origin picker, AC-26.2's alternative
  // -------------------------------------------------------------------------

  function renderSearchResults(nodes, term) {
    const items = nodes.map(function (node) {
      const item = element("li", "listing-item");
      item.appendChild(
        button("link", nodeLabel(node), function () {
          openSlice(node.id, state.direction, node.name);
        })
      );
      return item;
    });
    el["search-results"].replaceChildren.apply(el["search-results"], items);
    if (nodes.length === 0) {
      show(el["search-status"], "No node matches " + JSON.stringify(term) + ".");
    } else {
      show(el["search-status"], nodes.length + " matching nodes.");
    }
  }

  function nodeLabel(node) {
    return node.name + " · " + node.kind + " · " + locationText(node);
  }

  async function search(term) {
    const trimmed = (term || "").trim();
    if (!trimmed) {
      show(el["search-status"], "Type part of a name to search.");
      el["search-results"].replaceChildren();
      return;
    }
    try {
      const body = await api("/nodes?search=" + encodeURIComponent(trimmed));
      renderSearchResults(body.nodes, trimmed);
    } catch (error) {
      await handleError(error);
    }
  }

  // -------------------------------------------------------------------------
  // The trace view (FR-27, FR-28)
  // -------------------------------------------------------------------------

  /** Cytoscape elements for one slice — pure, so the mapping can be asserted directly. */
  function graphElements(slice, originId) {
    const nodes = slice.nodes.map(function (node) {
      return {
        group: "nodes",
        data: {
          id: node.id,
          label: node.name,
          kind: node.kind,
          external: node.is_external ? "yes" : "no",
          origin: node.id === originId ? "yes" : "no",
        },
      };
    });
    const present = new Set(slice.nodes.map((node) => node.id));
    const edges = slice.edges
      // An edge is drawn only when both ends are on screen: a truncated slice can carry an
      // edge into a node beyond the bound, and a dangling edge would be a claim the slice
      // does not make. The frontier list is where those nodes are named instead.
      .filter((edge) => present.has(edge.src) && present.has(edge.dst))
      .map(function (edge) {
        return {
          group: "edges",
          data: {
            id: edge.src + "→" + edge.dst,
            source: edge.src,
            target: edge.dst,
            ambiguous: edge.is_ambiguous ? "yes" : "no",
          },
        };
      });
    return nodes.concat(edges);
  }

  const GRAPH_STYLE = [
    {
      selector: "node",
      style: {
        "background-color": "#4c6ef5",
        label: "data(label)",
        "font-size": "10px",
        "text-valign": "center",
        "text-halign": "right",
        "text-margin-x": 4,
        color: "#1b1b1f",
        width: 14,
        height: 14,
      },
    },
    { selector: 'node[external = "yes"]', style: { "background-color": "#adb5bd", shape: "diamond" } },
    {
      selector: 'node[origin = "yes"]',
      style: { "background-color": "#f76707", width: 20, height: 20 },
    },
    { selector: 'node[kind = "entry_point"]', style: { shape: "round-rectangle" } },
    {
      selector: "edge",
      style: {
        width: 1.4,
        "line-color": "#868e96",
        "target-arrow-color": "#868e96",
        "target-arrow-shape": "triangle",
        "curve-style": "bezier",
      },
    },
    {
      selector: 'edge[ambiguous = "yes"]',
      style: { "line-style": "dashed", "line-color": "#e8590c", "target-arrow-color": "#e8590c" },
    },
    { selector: ".selected", style: { "border-width": 3, "border-color": "#1b1b1f" } },
  ];

  function renderGraph(slice, originId) {
    if (cy) {
      cy.destroy();
      cy = null;
    }
    cy = global.cytoscape({
      container: el.graph,
      elements: graphElements(slice, originId),
      style: GRAPH_STYLE,
      layout: { name: "dagre", rankDir: "LR", nodeSep: 18, rankSep: 90 },
      wheelSensitivity: 0.2,
    });
    // AC-27.1: a displayed edge is followable — tapping it selects the node it points at.
    cy.on("tap", "edge", function (event) {
      selectNode(event.target.data("target"));
    });
    cy.on("tap", "node", function (event) {
      selectNode(event.target.data("id"));
    });
    return cy;
  }

  function clearTrace() {
    if (cy) {
      cy.destroy();
      cy = null;
    }
    hide(el["truncation-banner"]);
    hide(el["trace-empty"]);
    el["trace-origin"].textContent = "";
    el["frontier-list"].replaceChildren();
  }

  /** AC-28.2's visible bound, stated in the terms the response arrived in. */
  function truncationText(slice) {
    return (
      "This slice is bounded: " +
      slice.nodes.length +
      " nodes are shown and " +
      slice.frontier.length +
      (slice.frontier.length === 1 ? " frontier node was" : " frontier nodes were") +
      " not expanded. What is displayed is a partial slice."
    );
  }

  function renderTruncation(slice) {
    if (!slice.truncated) {
      hide(el["truncation-banner"]);
      el["frontier-list"].replaceChildren();
      return;
    }
    el["truncation-message"].textContent = truncationText(slice);
    // Expanding doubles the bound the server just applied, so the frontend never carries
    // its own copy of §8-O2's provisional default.
    const expanded = slice.nodes.length * 2;
    el["frontier-expand"].textContent = "Expand to " + expanded + " nodes";
    const items = slice.frontier.map(function (id) {
      const item = element("li", "listing-item");
      item.appendChild(
        button("link", id, function () {
          openSlice(id, state.direction, id);
        })
      );
      return item;
    });
    el["frontier-list"].replaceChildren.apply(el["frontier-list"], items);
    el["truncation-banner"].hidden = false;
  }

  function renderDirection() {
    el["direction-forward"].className =
      state.direction === "forward" ? "toggle toggle-active" : "toggle";
    el["direction-backward"].className =
      state.direction === "backward" ? "toggle toggle-active" : "toggle";
  }

  /**
   * Open (or re-open) the slice from `nodeId` — the flagship workflow (FR-15–FR-17).
   *
   * AC-28.1: this is the only thing that ever draws a graph, and it draws exactly one
   * slice. There is no whole-graph path in this file.
   */
  async function openSlice(nodeId, direction, label, maxNodes) {
    state.origin = nodeId;
    state.direction = direction || state.direction;
    if (label !== undefined) {
      state.originLabel = label;
    }
    // The budget belongs to one request, not to the session: only `expandFrontier` passes
    // one, so opening any other slice goes back to the server's own bound. Carrying the
    // last expansion forward would silently *shrink* the next slice whenever the expanded
    // one was small.
    state.maxNodes = maxNodes || null;
    renderDirection();
    hide(el["trace-error"]);
    hide(el["trace-placeholder"]);

    let query = "/slice?from=" + encodeId(nodeId) + "&direction=" + state.direction;
    if (state.maxNodes) {
      query += "&max_nodes=" + state.maxNodes;
    }

    let slice;
    try {
      slice = await api(query);
    } catch (error) {
      // A failed slice must not leave the previous one on screen looking like the answer
      // to the question just asked.
      clearTrace();
      await handleError(error);
      return null;
    }

    state.slice = slice;
    el["trace-origin"].textContent =
      (state.originLabel || nodeId) + " — " + state.direction + " slice";
    if (slice.nodes.length === 0) {
      // AC-15.2: an empty slice is a valid answer, and is presented as one.
      clearTrace();
      el["trace-origin"].textContent =
        (state.originLabel || nodeId) + " — " + state.direction + " slice";
      show(
        el["trace-empty"],
        "This node has no " + state.direction + " call edges: the slice is empty."
      );
      return slice;
    }
    hide(el["trace-empty"]);
    renderGraph(slice, nodeId);
    renderTruncation(slice);
    await selectNode(nodeId);
    return slice;
  }

  /** AC-28.2's expand action: ask again with a larger budget, same origin and direction. */
  async function expandFrontier() {
    if (!state.slice || !state.slice.truncated) {
      return null;
    }
    return openSlice(state.origin, state.direction, undefined, state.slice.nodes.length * 2);
  }

  // -------------------------------------------------------------------------
  // The node panel (FR-27, AC-27.3)
  // -------------------------------------------------------------------------

  /**
   * AC-27.3: `file_path:start–end` for a node with a source location; the external
   * statement for one without, since FR-36 leaves external targets unanalyzed and
   * AC-37.2 leaves them without a span to show.
   */
  function locationText(node) {
    if (node.is_external) {
      return "external — not analyzed";
    }
    if (!node.file_path) {
      return "no source location recorded";
    }
    if (node.start_line === null || node.start_line === undefined) {
      return node.file_path;
    }
    const end = node.end_line === null || node.end_line === undefined ? node.start_line : node.end_line;
    return node.file_path + ":" + node.start_line + "–" + end;
  }

  function reachableText(node) {
    if (node.is_external) {
      return "reachability is not computed for external targets";
    }
    if (node.reachable === 1) {
      return "reachable from a detected entry point";
    }
    if (node.reachable === 0) {
      return "not reachable from any detected entry point (approximate — see Dead code)";
    }
    return "reachability not recorded";
  }

  function renderNodePanel(node) {
    el["node-panel-name"].textContent = node.name;
    el["node-panel-kind"].textContent = node.kind;
    el["node-panel-location"].textContent = locationText(node);
    el["node-panel-reachable"].textContent = reachableText(node);
    el["node-panel-id"].textContent = node.id;
    el["node-panel-body"].hidden = false;
    hide(el["node-panel-empty"]);
  }

  function clearNodePanel() {
    state.selected = null;
    el["node-panel-body"].hidden = true;
    el["node-panel-empty"].hidden = false;
  }

  async function selectNode(nodeId) {
    let node;
    try {
      node = await api("/nodes/" + encodeId(nodeId));
    } catch (error) {
      await handleError(error);
      return null;
    }
    state.selected = node;
    renderNodePanel(node);
    return node;
  }

  // -------------------------------------------------------------------------
  // Dead code (FR-19)
  // -------------------------------------------------------------------------

  function renderDeadCode(report) {
    // AC-19.2: the caveat is written before the findings, on every rendering, and it comes
    // from the report itself rather than being restated here.
    el["dead-code-caveat"].textContent = report.caveat;
    if (report.no_entry_points_warning) {
      // AC-19.3 / AC-18.2: with no entry points, "unreachable" means nothing yet.
      show(
        el["dead-code-warning"],
        "No entry points were detected, so nothing is reachable by construction. These " +
          "findings are uninformative until an entry point exists."
      );
    } else {
      hide(el["dead-code-warning"]);
    }

    const items = [];
    report.unreachable.forEach(function (group) {
      const item = element("li", "listing-item");
      item.appendChild(element("span", "file", group.file));
      const functions = element("ul", "listing");
      group.functions.forEach(function (fn) {
        const line = element("li", "listing-item");
        line.appendChild(
          button("link", fn.name + (fn.start_line ? ":" + fn.start_line : ""), function () {
            openSlice(fn.id, "backward", fn.name);
          })
        );
        functions.appendChild(line);
      });
      item.appendChild(functions);
      items.push(item);
    });
    el["dead-code-list"].replaceChildren.apply(el["dead-code-list"], items);

    const total = report.unreachable.reduce(function (sum, group) {
      return sum + group.functions.length;
    }, 0);
    show(
      el["dead-code-status"],
      total === 0
        ? "No unreachable functions were found."
        : total + " unreachable functions in " + report.unreachable.length + " files."
    );
  }

  async function showDeadCode() {
    showTab("dead-code");
    try {
      renderDeadCode(await api("/dead-code"));
    } catch (error) {
      await handleError(error);
    }
  }

  // -------------------------------------------------------------------------
  // Tabs
  // -------------------------------------------------------------------------

  function showTab(name) {
    state.tab = name;
    el["entry-points-pane"].hidden = name !== "entry-points";
    el["dead-code-pane"].hidden = name !== "dead-code";
    el["tab-entry-points"].className = name === "entry-points" ? "tab tab-active" : "tab";
    el["tab-dead-code"].className = name === "dead-code" ? "tab tab-active" : "tab";
  }

  // -------------------------------------------------------------------------
  // Wiring and startup
  // -------------------------------------------------------------------------

  function wire() {
    el["search-form"].addEventListener("submit", function (event) {
      if (event && event.preventDefault) {
        event.preventDefault();
      }
      search(el["search-input"].value);
    });
    el["tab-entry-points"].addEventListener("click", function () {
      showTab("entry-points");
    });
    el["tab-dead-code"].addEventListener("click", function () {
      showDeadCode();
    });
    el["direction-forward"].addEventListener("click", function () {
      if (state.origin) {
        openSlice(state.origin, "forward");
      }
    });
    el["direction-backward"].addEventListener("click", function () {
      if (state.origin) {
        openSlice(state.origin, "backward");
      }
    });
    el["back-to-entry-points"].addEventListener("click", function () {
      showTab("entry-points");
      loadEntryPoints();
    });
    el["frontier-expand"].addEventListener("click", function () {
      expandFrontier();
    });
    el["fatal-retry"].addEventListener("click", function () {
      start();
    });
    el["node-panel-slice-forward"].addEventListener("click", function () {
      if (state.selected) {
        openSlice(state.selected.id, "forward", state.selected.name);
      }
    });
    el["node-panel-slice-backward"].addEventListener("click", function () {
      if (state.selected) {
        openSlice(state.selected.id, "backward", state.selected.name);
      }
    });
  }

  let wired = false;

  /**
   * Load the page: provenance first, because `/api/meta` failing is exactly the
   * unreadable-index case AC-25.2 requires the full-screen state for, and nothing below it
   * can succeed if it fails.
   */
  async function start() {
    if (!wired) {
      bind();
      wire();
      wired = true;
    }
    clearFatal();
    renderDirection();
    try {
      renderMeta(await api("/meta"));
    } catch (error) {
      await handleError(error);
      return;
    }
    showTab("entry-points");
    await loadEntryPoints();
  }

  global.pastapathfinder = {
    ApiError: ApiError,
    api: api,
    el: el,
    entryPointLabel: entryPointLabel,
    expandFrontier: expandFrontier,
    graphElements: graphElements,
    locationText: locationText,
    openSlice: openSlice,
    reachableText: reachableText,
    search: search,
    selectNode: selectNode,
    showDeadCode: showDeadCode,
    showTab: showTab,
    start: start,
    state: state,
    truncationText: truncationText,
    cy: function () {
      return cy;
    },
  };

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("DOMContentLoaded", function () {
      start();
    });
  }
})(typeof window !== "undefined" ? window : globalThis);
