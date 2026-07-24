/*
 * A stub browser for `src/pastapathfinder/viewer/static/app.js` (specs/tasks.md task 5.2).
 *
 * Task 5.2's verification is behavioral — "selecting an entry point and choosing forward
 * renders that slice", "the node panel shows file_path:start-end" — and behavior means
 * running the file. The dependency set is frozen at mypy/pathspec/flask plus the standard
 * library (design.md §6), so there is no browser-automation package to reach for, and D8
 * forbids an npm toolchain. What is left is this: the smallest environment in which the
 * real app.js runs, driven by tests/unit/test_viewer_frontend_js.py.
 *
 * Three deliberate properties:
 *
 * 1. **The application is not stubbed — its surroundings are.** app.js is evaluated
 *    verbatim in a `vm` context. What is faked is the DOM, `fetch`, and the cytoscape
 *    global; every decision under test is the shipped code's.
 * 2. **The DOM is built from index.html.** One element per declared `id`, with the tag and
 *    the initial `hidden` attribute read from the markup, so a page/script drift shows up
 *    as a failure here rather than in a browser.
 * 3. **Every network-capable API is a tripwire.** `fetch` records its URLs and
 *    XMLHttpRequest / WebSocket / EventSource / Image / sendBeacon / importScripts record
 *    and throw, so FR-33 is a thing the run either did or did not do.
 *
 * Usage: node viewer_dom_harness.js <static-dir> <<< '{"routes": …, "steps": […]}'
 * Output: one JSON document on stdout — `{snapshot, state, log}`.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

// ---------------------------------------------------------------------------
// A very small DOM
// ---------------------------------------------------------------------------

class El {
  constructor(tag, id) {
    this.tagName = tag;
    this.id = id || "";
    this.className = "";
    this.hidden = false;
    this.value = "";
    this.children = [];
    this.listeners = {};
    this._text = "";
  }

  get textContent() {
    if (this.children.length) {
      return this.children.map((child) => child.textContent).join(" ").trim();
    }
    return this._text;
  }

  set textContent(value) {
    this._text = value === null || value === undefined ? "" : String(value);
    this.children = [];
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren() {
    this.children = Array.prototype.slice.call(arguments);
    this._text = "";
  }

  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }

  dispatch(type, event) {
    (this.listeners[type] || []).forEach((handler) => handler(event));
  }
}

/** One element per `id="..."` in index.html, carrying its tag and initial `hidden`. */
function buildDom(html) {
  const registry = new Map();
  const tag = /<([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^<>]*?)?)>/g;
  let match;
  while ((match = tag.exec(html)) !== null) {
    const attributes = match[2] || "";
    const id = /\sid="([^"]+)"/.exec(attributes);
    if (!id) {
      continue;
    }
    const node = new El(match[1], id[1]);
    node.hidden = /\shidden(\s|\/|$)/.test(attributes);
    registry.set(id[1], node);
  }
  return registry;
}

// ---------------------------------------------------------------------------
// The fixture server
// ---------------------------------------------------------------------------

/**
 * `"/nodes/pkg.app:main?x=1"` regardless of how the caller percent-encoded it.
 *
 * Query keys are sorted so a fixture does not have to predict parameter order, and values
 * are decoded so it does not have to reproduce `encodeURIComponent`'s exact escaping.
 */
function canonical(url) {
  const cut = url.indexOf("?");
  const rawPath = cut === -1 ? url : url.slice(0, cut);
  // Routes are keyed by their §5.2 path; every request carries the same `/api` base.
  const decodedPath = decodeURIComponent(
    rawPath.startsWith("/api/") ? rawPath.slice("/api".length) : rawPath
  );
  if (cut === -1) {
    return decodedPath;
  }
  const pairs = url
    .slice(cut + 1)
    .split("&")
    .filter(Boolean)
    .map((pair) => {
      const eq = pair.indexOf("=");
      return [decodeURIComponent(pair.slice(0, eq)), decodeURIComponent(pair.slice(eq + 1))];
    });
  pairs.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return decodedPath + "?" + pairs.map((pair) => pair[0] + "=" + pair[1]).join("&");
}

function makeFetch(routes, log) {
  return function (url) {
    log.requests.push(url);
    const key = canonical(url);
    const route = Object.prototype.hasOwnProperty.call(routes, key) ? routes[key] : null;
    if (!route) {
      log.unrouted.push(key);
      return Promise.resolve({
        ok: false,
        status: 404,
        json: () =>
          Promise.resolve({ error: { code: "not_found", message: "no fixture for " + key } }),
      });
    }
    if (route.throws) {
      return Promise.reject(new TypeError(route.throws));
    }
    return Promise.resolve({
      ok: route.status < 400,
      status: route.status,
      json: () => Promise.resolve(route.body),
    });
  };
}

// ---------------------------------------------------------------------------
// The cytoscape stub
// ---------------------------------------------------------------------------

function makeCytoscape(log) {
  const factory = function (options) {
    const graph = {
      options: options,
      elements: options.elements,
      handlers: [],
      destroyed: false,
      on: function (event, selector, handler) {
        this.handlers.push({ event: event, selector: selector, handler: handler });
      },
      destroy: function () {
        this.destroyed = true;
        log.destroyed += 1;
      },
    };
    log.graphs.push(graph);
    log.current = graph;
    return graph;
  };
  factory.use = function (plugin) {
    log.plugins.push(plugin && plugin.name ? plugin.name : String(plugin));
  };
  return factory;
}

/** Fire a cytoscape `tap` on the element with `id`, as a real tap on the canvas would. */
function tap(log, kind, id) {
  const graph = log.current;
  if (!graph) {
    throw new Error("no graph is rendered, so nothing can be tapped");
  }
  const element = graph.elements.filter((candidate) => candidate.data.id === id)[0];
  if (!element) {
    throw new Error("no " + kind + " with id " + id + " is on screen");
  }
  const event = { target: { data: (field) => (field ? element.data[field] : element.data) } };
  graph.handlers
    .filter((entry) => entry.event === "tap" && entry.selector === kind)
    .forEach((entry) => entry.handler(event));
}

// ---------------------------------------------------------------------------
// Tripwires — every way a page can talk to the network (FR-33)
// ---------------------------------------------------------------------------

function installTripwires(sandbox, log) {
  ["XMLHttpRequest", "WebSocket", "EventSource", "Image", "importScripts"].forEach((name) => {
    sandbox[name] = function () {
      log.forbidden.push(name);
      throw new Error(name + " is not available to this page");
    };
  });
  sandbox.navigator = {
    sendBeacon: function () {
      log.forbidden.push("sendBeacon");
      return false;
    },
  };
}

// ---------------------------------------------------------------------------
// Snapshotting
// ---------------------------------------------------------------------------

function snapshot(registry) {
  const out = {};
  registry.forEach((node, id) => {
    out[id] = {
      tag: node.tagName,
      text: node.textContent,
      hidden: Boolean(node.hidden),
      className: node.className,
      items: node.children.map((child) => child.textContent),
    };
  });
  return out;
}

// ---------------------------------------------------------------------------
// Driving
// ---------------------------------------------------------------------------

async function applyStep(step, sandbox, registry, log) {
  const app = sandbox.pastapathfinder;
  if (step.call) {
    return app[step.call].apply(null, step.args || []);
  }
  if (step.click) {
    const node = registry.get(step.click);
    if (!node) {
      throw new Error("no element #" + step.click);
    }
    node.dispatch("click", { type: "click" });
    return null;
  }
  if (step.clickItem) {
    // The nth entry of a rendered listing: `{clickItem: ["entry-point-list", 0]}`.
    const [id, position] = step.clickItem;
    const list = registry.get(id);
    const item = list.children[position];
    if (!item) {
      throw new Error("#" + id + " has no item at position " + position);
    }
    // A listing item wraps its button; click the first descendant that has a handler.
    const target = (item.listeners.click ? item : item.children[0]) || item;
    target.dispatch("click", { type: "click" });
    return null;
  }
  if (step.submit) {
    if (step.value !== undefined) {
      registry.get(step.input).value = step.value;
    }
    registry.get(step.submit).dispatch("submit", { preventDefault: () => {} });
    return null;
  }
  if (step.tap) {
    tap(log, step.tap, step.id);
    return null;
  }
  if (step.set) {
    registry.get(step.set).value = step.value;
    return null;
  }
  throw new Error("unknown step: " + JSON.stringify(step));
}

/**
 * Let every promise the step started settle.
 *
 * The click handlers are `async` and nothing awaits them, exactly as in a browser; a few
 * turns of the microtask queue is what a browser's next paint is here.
 */
function settle() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function main() {
  const staticDir = process.argv[2];
  const request = JSON.parse(fs.readFileSync(0, "utf8"));

  const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
  const source = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
  const registry = buildDom(html);

  const log = {
    requests: [],
    unrouted: [],
    forbidden: [],
    plugins: [],
    graphs: [],
    destroyed: 0,
    current: null,
    error: null,
  };

  const sandbox = {
    console: console,
    setTimeout: setTimeout,
    document: {
      getElementById: (id) => registry.get(id) || null,
      createElement: (tag) => new El(tag),
      addEventListener: () => {},
    },
    fetch: makeFetch(request.routes || {}, log),
    cytoscape: makeCytoscape(log),
    cytoscapeDagre: { name: "dagre" },
  };
  sandbox.window = sandbox;
  installTripwires(sandbox, log);
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "app.js" });

  try {
    for (const step of request.steps || []) {
      await applyStep(step, sandbox, registry, log);
      await settle();
    }
  } catch (failure) {
    log.error = failure && failure.stack ? failure.stack : String(failure);
  }

  const state = sandbox.pastapathfinder ? sandbox.pastapathfinder.state : null;
  process.stdout.write(
    JSON.stringify({
      snapshot: snapshot(registry),
      state: state
        ? {
            origin: state.origin,
            direction: state.direction,
            maxNodes: state.maxNodes,
            tab: state.tab,
            selected: state.selected ? state.selected.id : null,
          }
        : null,
      log: {
        requests: log.requests,
        unrouted: log.unrouted,
        forbidden: log.forbidden,
        plugins: log.plugins,
        destroyed: log.destroyed,
        graphs: log.graphs.length,
        elements: log.current ? log.current.elements : [],
        layout: log.current ? log.current.options.layout : null,
        handlers: log.current
          ? log.current.handlers.map((entry) => entry.event + " " + entry.selector)
          : [],
        error: log.error,
      },
    })
  );
}

main();
