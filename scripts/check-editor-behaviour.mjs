#!/usr/bin/env node
/* Behavioural check of the three browser assets, outside pytest.
 *
 * `tests/test_web_assets.py` reads editor.html, viewer.html and renderer.js as
 * *text* — injection slots, balanced tags, the offline guarantee. That boundary
 * held until fixtures, and then let three real editor.html defects through, one
 * of them a document-corruption path: a malformed `affects` array threw
 * mid-resize, after snapshot() and after earlier planes had already been
 * rewritten, and the Download button writes that half-resized document to disk
 * without the server ever seeing it. Each defect was found by extracting the
 * handler and running it under node; each of those harnesses was a throwaway.
 * This is the same technique, kept.
 *
 * It loads the **shipped** assets — the ones under
 * souroldgeezer-fivee-sim/engine/src/fivee_sim/web/static, never a copy — runs
 * renderer.js in a `node:vm` context, and runs each page's own inline script in
 * that context against a stub DOM. What is asserted is therefore the text a
 * user gets, driven through the page's real wiring: a document is dropped on
 * the page, buttons are clicked, and the assertions read what the fake canvas
 * was painted and what the Download button would have written.
 *
 * **What it does not cover.** There is no browser here. No layout, no CSS, no
 * real canvas, no event ordering, no `file://`, no network. The DOM is a stub
 * this file defines, so an assertion about *what the page decided* is
 * meaningful and an assertion about *what the page looks like* is not
 * available. Text-level contracts stay in tests/test_web_assets.py.
 *
 * Node builtins only, by design: no package.json, no npm install, no browser
 * toolchain in a Python repository. Node 20 or newer.
 *
 * Usage: node scripts/check-editor-behaviour.mjs [static-dir]
 *
 * The optional directory exists for this script's own mutation check: copy the
 * static directory somewhere scratch, delete a guard, and confirm the case
 * that names it fails. Every other run must read the shipped path.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..");
const SHIPPED = path.join(
  REPO_ROOT, "souroldgeezer-fivee-sim", "engine", "src", "fivee_sim", "web", "static"
);
const STATIC = process.argv[2] ? path.resolve(process.argv[2]) : SHIPPED;

/* --- reporting, in the shape the repo's other check scripts use ---------- */

let passed = 0;
let failed = 0;

function report(ok, label, detail) {
  console.log("  " + (ok ? "PASS" : "FAIL") + "  " + label);
  if (ok) { passed += 1; return; }
  failed += 1;
  if (detail !== undefined && detail !== null && String(detail) !== "") {
    console.log(String(detail).split("\n").map((line) => "          | " + line).join("\n"));
  }
}

const check = (label, ok, detail) => report(!!ok, label, ok ? "" : detail);
const show = (value) => {
  try { return JSON.stringify(value); } catch (error) { return String(value); }
};

/* A suite that dies takes its own remaining cases with it, but not the run:
 * the other pages are still worth checking, and a harness that stops at the
 * first stub it is missing tells you about one of them at a time. Cases within
 * a suite share a page, so a real failure often takes the rest of the suite
 * with it — read the FIRST failure in a section and ignore the ones after. */
async function suite(title, stubSite, body) {
  console.log("\n=== " + title + " ===");
  try {
    await body();
  } catch (error) {
    report(false, title + " — the harness stopped, so the rest of this suite did not run",
      explain(error, stubSite));
  }
}

/* Let a page's promise chain finish before asserting on it. The stub `fetch`
 * resolves immediately, but each `.then` still costs a microtask turn, and a
 * served page spends several of them (list, then bundle, then draw). A fixed
 * number of turns is enough because nothing here is really asynchronous —
 * there is no timer and no socket to wait on. */
async function flush(turns = 24) {
  for (let i = 0; i < turns; i += 1) { await Promise.resolve(); }
}

/* Every id the shipped markup ships `hidden`, read off the page rather than
 * listed by hand. The stub DOM does not parse HTML — it invents an element the
 * first time the page asks for one — so without this every element starts
 * visible and "the page left it hidden" would be unfalsifiable. Derived from
 * the source so it cannot drift from the markup it is modelling. */
function hiddenElementIds(html) {
  const found = new Set();
  const tags = html.match(/<[a-z][^>]*>/gi) || [];
  tags.forEach((tag) => {
    if (!/[\s"']hidden[\s>/]/.test(tag)) { return; }
    const id = /\sid="([^"]+)"/.exec(tag);
    if (id) { found.add(id[1]); }
  });
  return found;
}

/* The failure this script is most likely to have, and the one the throwaways
 * kept having: the page grew a call the stubs do not answer. An opaque
 * `ReferenceError: foo is not defined` reads as a page defect. It is not — it
 * is this file being out of date, and it should say so. */
function explain(error, stubSite) {
  const message = String((error && error.message) || error);
  const lines = [message];
  let missing = /^(\w+) is not defined$/.exec(message);
  if (missing) {
    lines.push(
      "This harness needs a stub for `" + missing[1] + "`: the page reads a global this",
      "file does not define. Add it to " + stubSite + ", in",
      "scripts/check-editor-behaviour.mjs."
    );
  } else if (/ is not a function$/.test(message)) {
    lines.push(
      "This harness needs a stub for the call above: the page reaches for a DOM or host",
      "method the stub DOM does not provide. Add it to " + stubSite + " or to the El",
      "class, in scripts/check-editor-behaviour.mjs."
    );
  } else if (/Cannot read propert(y|ies) of (undefined|null)/.test(message)) {
    lines.push(
      "Something read through a value that is not there. Three causes, in the order worth",
      "checking: a case above this one already failed and this one is reading its wreckage;",
      "the page changed shape; or " + stubSite + " needs extending, in",
      "scripts/check-editor-behaviour.mjs."
    );
  }
  if (error && error.stack) {
    lines.push("");
    lines.push(String(error.stack).split("\n").slice(1, 4).join("\n"));
  }
  return lines.join("\n");
}

/* --- reading the shipped assets ------------------------------------------ */

function read(name) {
  try {
    return readFileSync(path.join(STATIC, name), "utf8");
  } catch (error) {
    throw new Error("cannot read " + path.join(STATIC, name) + ": " + error.message);
  }
}

/* The page's own script, chosen by a function it defines rather than by
 * position. `lastIndexOf("<script>")` was how the throwaways found it, and a
 * new trailing block would have handed them the wrong one silently. */
function inlineScript(html, source, marker) {
  const blocks = [];
  let at = 0;
  for (;;) {
    const open = html.indexOf("<script>", at);
    if (open < 0) { break; }
    const start = open + "<script>".length;
    const end = html.indexOf("</script>", start);
    if (end < 0) { throw new Error(source + " has an unterminated <script> block"); }
    blocks.push(html.slice(start, end));
    at = end;
  }
  const found = blocks.filter((block) => block.indexOf(marker) !== -1);
  if (found.length !== 1) {
    throw new Error(
      source + " has " + found.length + " inline <script> blocks containing `" + marker
      + "`, expected exactly 1. This harness identifies the page's own script by that\n"
      + "marker; if the page was reorganised, pick a new one."
    );
  }
  return found[0];
}

const rendererSrc = read("renderer.js");
const editorHtml = read("editor.html");
const viewerHtml = read("viewer.html");
const homeHtml = read("home.html");
const replayInvalidCorpus = JSON.parse(readFileSync(path.join(
  REPO_ROOT, "souroldgeezer-fivee-sim", "engine", "tests", "fixtures",
  "replay-invalid.json"
), "utf8"));
/* The families viewer.html animates, declared once. `test_web_assets.py` holds
 * this list against the viewer's own dispatch and `test_replay_sample.py`
 * requires the showcase to demonstrate every entry; this file is the half that
 * proves the page actually does something for each one, so the declaration
 * cannot list a family that animates nothing. */
const animatedFamilies = JSON.parse(readFileSync(path.join(
  REPO_ROOT, "souroldgeezer-fivee-sim", "engine", "tests", "fixtures",
  "animated-event-families.json"
), "utf8"));

/* --- preflight ------------------------------------------------------------
 * Everything below drives the pages through named element ids and named
 * renderer exports. A rename should fail here, naming what moved, rather than
 * twenty cases down as a null dereference. */

const EDITOR_IDS = [
  "map", "status", "fixture-list", "btn-preview", "feature-info", "level-select",
  "btn-download", "btn-save", "map-name", "elevation-default", "btn-heights",
  "btn-delete-feature", "ambient-light", "feature-config", "feature-sight-levels",
  "feature-light-bright", "feature-light-dim", "feature-light-color", "btn-undo",
  "provenance", "dlg-resize", "dlg-resize-go", "rs-width", "rs-height", "rs-anchor",
  "rs-fill", "door-config", "door-orientation", "door-hinge", "door-swing",
  "door-linked", "facing-config", "feature-facing", "map-compass",
];
const VIEWER_IDS = [
  "stage", "scrub", "ticker", "readout", "title", "seed", "empty-note",
  "btn-play", "btn-back", "btn-forward", "speed", "embedded-data", "level-select",
  "follow-level", "sight-cones", "combatant-state", "adventure-chapters",
  "chapter-select",
];
const HOME_IDS = [
  "operations", "ops-status", "ops-count", "link-openapi", "engine-version",
];
const RENDERER_EXPORTS = [
  "render", "fitView", "resizeCanvas", "visibleBounds", "terrainOverridesFor",
];

console.log("=== preflight: the assets this harness drives ===");
console.log("          | " + STATIC + (STATIC === SHIPPED ? "" : "  (NOT the shipped path)"));

function checkIds(source, html, ids) {
  const absent = ids.filter((id) => html.indexOf('id="' + id + '"') === -1);
  check(
    source + " still carries the " + ids.length + " element ids this harness drives",
    absent.length === 0,
    "missing: " + absent.join(", ") + "\nThis harness clicks and reads them by id; "
      + "update scripts/check-editor-behaviour.mjs to match the rename."
  );
}
checkIds("editor.html", editorHtml, EDITOR_IDS);
checkIds("viewer.html", viewerHtml, VIEWER_IDS);
checkIds("home.html", homeHtml, HOME_IDS);

/* --- the stub DOM ---------------------------------------------------------
 * Shared by both pages. Deliberately dumb: it records what the page did to it
 * and answers the handful of methods the pages call. It is not a DOM, and the
 * assertions never pretend otherwise — nothing here lays anything out. */

class ClassList {
  constructor() { this.set = new Set(); }
  add(name) { this.set.add(name); }
  remove(name) { this.set.delete(name); }
  contains(name) { return this.set.has(name); }
  toggle(name, on) {
    const want = on === undefined ? !this.set.has(name) : !!on;
    if (want) { this.set.add(name); } else { this.set.delete(name); }
    return want;
  }
}

function makeElementClass(contextOf) {
  return class El {
    constructor(tag, id) {
      this.tagName = String(tag || "div").toUpperCase();
      this.id = id || "";
      this.children = [];
      this.handlers = Object.create(null);
      this.classList = new ClassList();
      this.style = {};
      this.dataset = {};
      this.className = "";
      this.title = "";
      this.type = "";
      this.value = "";
      this.max = "";
      this.href = "";
      this.download = "";
      this.checked = false;
      this.disabled = false;
      this.hidden = false;
      this.open = false;
      this.width = 800;
      this.height = 600;
      this.files = [];
      this._text = "";
    }
    get textContent() {
      return this._text + this.children.map((child) => child.textContent).join("");
    }
    set textContent(value) { this._text = String(value); this.children = []; }
    get innerHTML() { return this.textContent; }
    set innerHTML(value) { this._text = ""; this.children = []; }
    appendChild(child) { this.children.push(child); return child; }
    removeChild(child) {
      this.children = this.children.filter((each) => each !== child);
      return child;
    }
    remove() {}
    addEventListener(type, fn) {
      (this.handlers[type] = this.handlers[type] || []).push(fn);
    }
    removeEventListener(type, fn) {
      this.handlers[type] = (this.handlers[type] || []).filter((each) => each !== fn);
    }
    dispatch(type, event) {
      const detail = event || {};
      if (detail.target === undefined) { detail.target = this; }
      if (detail.preventDefault === undefined) { detail.preventDefault = () => {}; }
      if (detail.stopPropagation === undefined) { detail.stopPropagation = () => {}; }
      (this.handlers[type] || []).slice().forEach((fn) => fn.call(this, detail));
    }
    click() { this.dispatch("click", {}); }
    focus() {}
    blur() {}
    scrollIntoView() {}
    getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 600 }; }
    getContext() { return contextOf(); }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    setPointerCapture() {}
    releasePointerCapture() {}
    showModal() { this.open = true; }
    close() { this.open = false; }
  };
}

/* The fake 2D context. Two observation channels, and the split matters.
 *
 * `fills` records fillRect: the colour a square was painted, and the alpha it
 * was painted at. Alpha is recorded because the context outlives the frame. A
 * glyph that sets globalAlpha inside a save()/restore() pair and loses the
 * restore leaves every later fill — this frame and every frame after it, since
 * render() never resets alpha — painted at the value it borrowed. Reading
 * fillStyle alone cannot see that: the colour string is unchanged and the whole
 * map just goes faint.
 *
 * `paths` records the other way this renderer draws: build a path with
 * beginPath/moveTo/lineTo/arc, then commit it with fill() or stroke(). Until
 * this existed, every such glyph was invisible here — the door's swing arc was
 * the documented example, and a case asserting it would have passed with the
 * arc deleted. A committed path carries the ops that built it and the style at
 * the moment of commit, because a path stroked in the wrong ink or at a
 * borrowed alpha is as wrong as one never drawn.
 *
 * What this still does not see: geometry. Nothing here rasterises, so a chevron
 * pointing the wrong way is a path whose *coordinates* a case must judge for
 * itself. The recorder hands over the numbers; it does not know what they mean. */
function makeContext(fills, paths) {
  const state = {
    fillStyle: "", strokeStyle: "", lineWidth: 1, font: "",
    textAlign: "", textBaseline: "", globalAlpha: 1, lineCap: "", lineJoin: "",
    canvas: null,
  };
  const saved = [];
  let building = [];
  const commit = (kind) => {
    /* A commit with no path behind it is a no-op, not a record: fill() after a
     * bare rect() is legal canvas and says nothing about a glyph. */
    if (!building.length) { return; }
    paths.push({
      kind,
      ops: building.slice(),
      ink: kind === "fill" ? state.fillStyle : state.strokeStyle,
      alpha: state.globalAlpha,
      lineWidth: state.lineWidth,
    });
  };
  return new Proxy(state, {
    get(target, prop) {
      if (prop === "fillRect") {
        return (x, y, w, h) => fills.push([x, y, w, h, target.fillStyle, target.globalAlpha]);
      }
      /* Path construction: recorded verbatim, in call order, so a case can read
       * the shape a glyph actually described. */
      if (prop === "beginPath") { return () => { building = []; }; }
      if (prop === "moveTo") { return (x, y) => building.push(["moveTo", x, y]); }
      if (prop === "lineTo") { return (x, y) => building.push(["lineTo", x, y]); }
      if (prop === "closePath") { return () => building.push(["closePath"]); }
      if (prop === "arc") {
        return (x, y, r, from, to, ccw) =>
          building.push(["arc", x, y, r, from, to, Boolean(ccw)]);
      }
      if (prop === "fill") { return () => commit("fill"); }
      if (prop === "stroke") { return () => commit("stroke"); }
      /* save/restore are the two no-ops with state behind them: a glyph that
       * borrows alpha or a line cap has to give it back. */
      if (prop === "save") {
        return () => { saved.push({ ...target }); };
      }
      if (prop === "restore") {
        return () => {
          const previous = saved.pop();
          if (previous) { Object.assign(target, previous); }
        };
      }
      if (prop === "measureText") { return () => ({ width: 4 }); }
      if (prop in target) { return target[prop]; }
      /* Every other context method is drawing this harness does not read.
       * A no-op here is a deliberate blind spot, not an oversight: see the
       * "what it does not cover" note at the top. */
      return () => {};
    },
    set(target, prop, value) { target[prop] = value; return true; },
  });
}

/* A page in a box: the sandbox, its element table, the fills the last render
 * painted, and the blobs the page tried to hand the user. */
function makePage(options) {
  const fills = [];
  const paths = [];
  const context = makeContext(fills, paths);
  const El = makeElementClass(() => context);
  const elements = new Map();
  const canvasIds = new Set(options.canvasIds || []);
  const element = (id) => {
    if (!elements.has(id)) {
      const el = new El(canvasIds.has(id) ? "canvas" : "div", id);
      if (options.seed && Object.prototype.hasOwnProperty.call(options.seed, id)) {
        el.textContent = options.seed[id];
      }
      if (options.hiddenIds && options.hiddenIds.has(id)) { el.hidden = true; }
      elements.set(id, el);
    }
    return elements.get(id);
  };
  /* A page need not draw. `makeContext` starts `canvas` null and the landing
   * page never reads it, so a canvas-less page omits `canvasIds` entirely
   * rather than passing an empty array and getting an element keyed on
   * `undefined` — which worked only for as long as nothing looked at it. */
  if (options.canvasIds && options.canvasIds.length) {
    context.canvas = element(options.canvasIds[0]);
  }

  const documentStub = new El("document");
  documentStub.getElementById = element;
  documentStub.createElement = (tag) => new El(tag);
  documentStub.createTextNode = (text) => {
    const node = new El("#text");
    node.textContent = text;
    return node;
  };
  documentStub.querySelectorAll = (selector) => (options.selectAll || (() => []))(selector);
  documentStub.querySelector = (selector) => {
    const found = documentStub.querySelectorAll(selector);
    return found.length ? found[0] : null;
  };
  documentStub.visibilityState = "visible";
  documentStub.body = new El("body");

  const store = new Map();
  const requests = [];
  const blobs = [];
  const alerts = [];
  const animationFrames = new Map();
  let nextAnimationFrame = 1;
  let immediateFrameTime = 0;
  const page = {
    fills, paths, context, element, elements, documentStub, requests, blobs, alerts,
    renders: [],
    reply: () => ({ status: 200, body: {} }),
  };

  const sandbox = {
    console,
    document: documentStub,
    devicePixelRatio: 1,
    matchMedia: (query) => ({
      matches: !!options.reducedMotion && query.indexOf("prefers-reduced-motion") !== -1,
      addEventListener() {},
    }),
    requestAnimationFrame: options.manualAnimationFrames
      ? (cb) => {
        const id = nextAnimationFrame++;
        animationFrames.set(id, cb);
        return id;
      }
      : (cb) => { immediateFrameTime += 1000; cb(immediateFrameTime); return 1; },
    cancelAnimationFrame: options.manualAnimationFrames
      ? (id) => { animationFrames.delete(id); }
      : () => {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    alert: (text) => { alerts.push(String(text)); },
    confirm: () => true,
    sessionStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, String(value)),
      removeItem: (key) => store.delete(key),
    },
    localStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, String(value)),
      removeItem: (key) => store.delete(key),
    },
    __FIVEE_EDITOR__: options.config || null,
    /* Enough of `location` for a page to read its own query string. Only the
     * deep-link path uses it, and only ever to read — nothing here navigates. */
    location: { search: options.search || "", href: "http://127.0.0.1/", hash: "" },
    Headers: class { get() { return null; } },
    Blob: class { constructor(parts) { this.parts = parts; blobs.push(parts.join("")); } },
    URL: { createObjectURL: () => "blob:harness", revokeObjectURL: () => {} },
    fetch: (url, init) => {
      const options_ = init || {};
      requests.push({
        url,
        method: options_.method || "GET",
        body: options_.body ? JSON.parse(options_.body) : null,
      });
      const reply = page.reply(url, options_);
      return Promise.resolve({
        status: reply.status,
        headers: { get: () => reply.etag || null },
        text: () => Promise.resolve(JSON.stringify(reply.body)),
      });
    },
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.addEventListener = (type, fn) => documentStub.addEventListener("window:" + type, fn);
  sandbox.removeEventListener = () => {};
  vm.createContext(sandbox);
  page.sandbox = sandbox;

  vm.runInContext(rendererSrc, sandbox);
  const renderer = sandbox.FiveeRenderer;
  if (!renderer) {
    throw new Error("renderer.js defined no FiveeRenderer global in this context");
  }
  /* Wrapped before the page captures the namespace: `R` is this same object, so
   * the call site finds the wrapper either way. Every frame is recorded with
   * the fills it produced, which is the whole observation channel. */
  const realRender = renderer.render;
  renderer.render = function (ctx, doc, view, overlays) {
    fills.length = 0;
    paths.length = 0;
    realRender(ctx, doc, view, overlays);
    page.renders.push({
      doc, view, overlays, fills: fills.slice(), paths: paths.slice(),
    });
  };
  page.renderer = renderer;

  page.last = () => page.renders[page.renders.length - 1];
  /* The colour actually painted on a square — read off the fake canvas rather
   * than off the overlay, so "the picture changed" means the picture. */
  page.fillAt = (x, y, frame) => {
    const target = frame || page.last();
    if (!target) { return null; }
    const px = (x - target.view.x) * target.view.scale;
    const py = (y - target.view.y) * target.view.scale;
    const hit = target.fills.filter(
      (f) => Math.abs(f[0] - px) < 0.01 && Math.abs(f[1] - py) < 0.01
    );
    return hit.length ? hit[0][4] : null;
  };
  page.drop = async (payload, name) => {
    const file = {
      name: name || "dropped.json",
      text: () => Promise.resolve(JSON.stringify(payload)),
    };
    documentStub.dispatch("drop", { dataTransfer: { files: [file] } });
    await settle();
  };
  page.frame = (time) => {
    const pending = Array.from(animationFrames.values());
    animationFrames.clear();
    pending.forEach((cb) => cb(time));
    return pending.length;
  };
  page.run = (source) => vm.runInContext(source, sandbox);
  return page;
}

/* Both pages open a dropped file through `file.text().then(...)` and neither
 * catches, so a throw inside loadDocument or loadBundle lands as an unhandled
 * rejection on a later tick — outside any try block a caller could write, and
 * printed by node as a bare stack with no idea which case was running. Park
 * them here and let settle() raise them where the suite can explain them. */
const escaped = [];
process.on("unhandledRejection", (reason) => { escaped.push(reason); });

/* Two turns: the first lets the page's promise chain run, the second lets node
 * decide the rejection was nobody's. */
const settle = () => new Promise((resolve, reject) => {
  setImmediate(() => setImmediate(() => {
    if (escaped.length) { reject(escaped.shift()); return; }
    resolve();
  }));
});
const copy = (value) => JSON.parse(JSON.stringify(value));

/* --- renderer.js ---------------------------------------------------------- */

const RENDERER_DOC = {
  grid: { width: 8, height: 6 },
  legend: { ".": "floor", "#": "wall" },
  tiles: ["########", "#......#", "#......#", "#......#", "#......#", "########"],
  features: [
    { id: "spawn-a", kind: "spawn", at: [1, 1] },
    { id: "stair", kind: "stairs_down", at: [2, 1] },
    {
      id: "sluice", kind: "door", at: [4, 3], orientation: "horizontal",
      state: "closed",
      terrain: { closed: "wall", open: "water" },
      affects: [
        { cells: [[1, 4], [2, 4]], terrain: { closed: "floor", open: "water" } },
        { cells: [[6, 4]], terrain: { closed: "difficult", open: "wall" } },
        { cells: [[5, 4]], elevation: { closed: 0, open: -5 } },
      ],
    },
  ],
};

await suite("renderer.js: the override channel", "the renderer sandbox", async () => {
  const page = makePage({ canvasIds: ["map"] });
  const R = page.renderer;

  const absent = RENDERER_EXPORTS.filter((name) => typeof R[name] !== "function");
  check(
    "renderer.js exports the " + RENDERER_EXPORTS.length + " functions this harness calls",
    absent.length === 0,
    "missing or not a function: " + absent.join(", ")
  );

  /* 1. With no live map, the document's own `state` decides. */
  const authored = R.terrainOverridesFor(RENDERER_DOC, undefined);
  check("closed: the fixture's own square takes its closed kind",
    authored["4,3"] === "wall", authored["4,3"]);
  check("closed: the overlay cells take their closed kinds",
    authored["1,4"] === "floor" && authored["6,4"] === "difficult", show(authored));
  check("a height-only group contributes no terrain",
    authored["5,4"] === undefined, show(authored["5,4"]));
  check("a feature carrying no state decides nothing",
    authored["1,1"] === undefined && authored["2,1"] === undefined, show(authored));

  /* 2. A live map wins over the authored state, in both directions. */
  const open = R.terrainOverridesFor(RENDERER_DOC, { sluice: true });
  check("open: the fixture's own square flips", open["4,3"] === "water", open["4,3"]);
  check("open: the room floods",
    open["1,4"] === "water" && open["2,4"] === "water", show(open));
  check("open: the wheel's square turns impassable", open["6,4"] === "wall", open["6,4"]);
  const shut = R.terrainOverridesFor(RENDERER_DOC, { sluice: false });
  check("an explicit false is honoured, not read as absent",
    shut["4,3"] === "wall", shut["4,3"]);

  const authoredOpen = copy(RENDERER_DOC);
  authoredOpen.features[2].state = "open";
  check("a document authored open shows open under an empty live map",
    R.terrainOverridesFor(authoredOpen, {})["1,4"] === "water",
    show(R.terrainOverridesFor(authoredOpen, {})));

  /* 3. A hand-opened file's malformed shapes cost their own group, not the
   *    frame: this runs per redraw, and a throw here blanks the canvas. */
  const hostile = [
    ["features is not an array", (d) => { d.features = "nope"; }],
    ["a null feature", (d) => { d.features.push(null); }],
    ["affects is a string", (d) => { d.features[2].affects = "nope"; }],
    ["a null group", (d) => { d.features[2].affects = [null]; }],
    ["cells is not an array", (d) => {
      d.features[2].affects = [{ cells: "1,2", terrain: { closed: "a", open: "b" } }];
    }],
    ["a cell of the wrong length", (d) => { d.features[2].affects[0].cells = [[1]]; }],
    ["a terrain pair missing a side", (d) => { d.features[2].terrain = { closed: "wall" }; }],
    ["at is missing", (d) => { delete d.features[2].at; }],
  ];
  for (const [label, wreck] of hostile) {
    const wrecked = copy(RENDERER_DOC);
    wreck(wrecked);
    let threw = null;
    try { R.terrainOverridesFor(wrecked, { sluice: true }); } catch (e) { threw = e.message; }
    check("the derivation survives " + label, threw === null, "threw: " + threw);
  }

  /* 4. The tile loop actually consults the channel. Only a fake canvas can
   *    show this: the override has to reach fillStyle, not just the map. */
  const view = { x: 0, y: 0, scale: 20, width: 160, height: 120 };
  R.render(page.context, RENDERER_DOC, view, {});
  const dryFrame = page.last();
  R.render(page.context, RENDERER_DOC, view, { terrainOverrides: open });
  const wetFrame = page.last();
  check("the tile loop consults the channel",
    page.fillAt(1, 4, dryFrame) !== null && page.fillAt(1, 4, wetFrame) !== null
      && page.fillAt(1, 4, dryFrame) !== page.fillAt(1, 4, wetFrame),
    show([page.fillAt(1, 4, dryFrame), page.fillAt(1, 4, wetFrame)]));
  check("a square the channel does not name is painted as before",
    page.fillAt(3, 2, dryFrame) === page.fillAt(3, 2, wetFrame),
    show([page.fillAt(3, 2, dryFrame), page.fillAt(3, 2, wetFrame)]));
});

/* --- the door glyph -------------------------------------------------------
 * A door is a leaf on hinges, and an open one has to read as a leaf that swung.
 * The shape shipped wrong for as long as nothing asserted it: the open branch
 * drew two stubs pulled back into both jambs, which is a pocket door sliding
 * into the walls, and every door case in this file until now asserted state or
 * terrain and never geometry.
 *
 * The leaf is a fillRect, so the fake canvas can see it — this is a claim about
 * what the page *decided* to paint, which is the kind this harness can make.
 * The swing arc beside it is a stroked path, and it used to be invisible here;
 * the recorder now captures path construction and its commit, so the arc has a
 * case of its own below. That case is also the recorder's self-check: delete
 * the `ctx.arc(...)` in drawSwing and it is the one that goes red. */

const DOOR_INK = "#6b4f2a";  /* the leaf's light-theme ink; the matchMedia stub says light */
const DOOR_VIEW = { x: 0, y: 0, scale: 20, width: 160, height: 120 };
const DOOR_AT = [3, 2];

/* One door per document. The ink is shared by every door, so a second one in
 * the same frame could only be told from the first by the geometry these cases
 * exist to check. */
function doorDoc(orientation, state, hinge, swing) {
  const feature = {
    id: "d", kind: "door", at: [DOOR_AT[0], DOOR_AT[1]],
    orientation: orientation, state: state,
  };
  if (hinge !== undefined) { feature.hinge = hinge; }
  if (swing !== undefined) { feature.swing = swing; }
  return {
    grid: { width: 8, height: 6 },
    legend: { ".": "floor", "#": "wall" },
    tiles: ["########", "#......#", "#......#", "#......#", "#......#", "########"],
    features: [feature],
  };
}

await suite("renderer.js: the door glyph", "the renderer sandbox", async () => {
  const page = makePage({ canvasIds: ["map"] });
  const R = page.renderer;
  const leaves = (orientation, state, hinge, swing) => {
    R.render(page.context, doorDoc(orientation, state, hinge, swing), DOOR_VIEW, {});
    return page.last().fills.filter((f) => f[4] === DOOR_INK);
  };
  const near = (a, b) => Math.abs(a - b) < 0.01;
  const px = DOOR_AT[0] * DOOR_VIEW.scale;
  const py = DOOR_AT[1] * DOOR_VIEW.scale;

  /* 1. Closed is the half that was always right, and it is what "swung" is
   *    measured against, so it is pinned first. */
  const shutH = leaves("horizontal", "closed");
  /* Named before anything reads shutH[0]. Every case here filters the frame by
   * one ink, so retuning the leaf's colour empties all of them at once — and
   * without this, the first case fails claiming the door has the wrong shape
   * and the third dies on `undefined`, neither of which is what happened. */
  if (!shutH.length) {
    throw new Error(
      "no door was painted in " + DOOR_INK + ". The leaf's ink changed, so this suite is"
      + " filtering the frame on a colour renderer.js no longer uses — update DOOR_INK in"
      + " scripts/check-editor-behaviour.mjs. (The door glyph itself may be fine.)"
    );
  }
  check("a closed door is one leaf, lying along the wall run it fills",
    shutH.length === 1 && shutH[0][2] > shutH[0][3], show(shutH));
  const shutV = leaves("vertical", "closed");
  check("and a closed vertical door is that same leaf, turned",
    shutV.length === 1 && shutV[0][3] > shutV[0][2], show(shutV));

  /* 2. Open is one leaf that swung, not two that slid apart. */
  const openH = leaves("horizontal", "open");
  check("an open door is one leaf, not two stubs retracted into the jambs",
    openH.length === 1, show(openH));
  check("and the leaf has swung a quarter turn, across the doorway",
    openH.length === 1 && openH[0][3] > openH[0][2], show(openH));
  check("it is the same leaf, rotated rather than redrawn",
    openH.length === 1 && near(openH[0][3], shutH[0][2]) && near(openH[0][2], shutH[0][3]),
    show([openH[0], shutH[0]]));

  /* 3. Which jamb it hangs on, and which way it opens. Fixed rules, so a later
   *    change cannot flip a door silently. */
  check("hinged at the west jamb: the leaf pivots, it does not slide",
    openH.length === 1 && near(openH[0][0], shutH[0][0])
      && near(openH[0][1] + openH[0][3], shutH[0][1] + shutH[0][3]),
    show([openH[0], shutH[0]]));
  check("and swings north, out into the passage the door interrupts",
    openH.length === 1 && openH[0][1] < py, show([openH[0][1], py]));

  const openV = leaves("vertical", "open");
  check("a vertical door swings the same way about its north jamb",
    openV.length === 1 && openV[0][2] > openV[0][3]
      && near(openV[0][1], shutV[0][1]) && near(openV[0][0] + openV[0][2],
        shutV[0][0] + shutV[0][2]),
    show([openV[0], shutV[0]]));
  check("out west, into its own passage",
    openV.length === 1 && openV[0][0] < px, show([openV[0][0], px]));

  /* 4. Authored hinge and swing directions cover every jamb and side. The
   *    omitted fields above remain the historical west/north and north/west
   *    defaults, while these cases prove the document can override each axis. */
  const eastH = leaves("horizontal", "open", "east", "north");
  check("a horizontal door can hang on the east jamb",
    eastH.length === 1 && near(eastH[0][0] + eastH[0][2], shutH[0][0] + shutH[0][2]),
    show([eastH[0], shutH[0]]));
  const southH = leaves("horizontal", "open", "west", "south");
  check("a horizontal door can swing south",
    southH.length === 1 && southH[0][1] > py, show([southH[0][1], py]));
  const eastV = leaves("vertical", "open", "north", "east");
  check("a vertical door can swing east",
    eastV.length === 1 && eastV[0][0] > px, show([eastV[0][0], px]));
  const southV = leaves("vertical", "open", "south", "west");
  check("a vertical door can hang on the south jamb",
    southV.length === 1 && near(southV[0][1] + southV[0][3],
      shutV[0][1] + shutV[0][3]), show([southV[0], shutV[0]]));

  /* 5. The swing arc borrows alpha, and it has to give it back. The context
   *    outlives the frame and render() never resets alpha, so a lost restore
   *    does not spoil one door — it leaves the whole map, and every frame after
   *    it, painted at three-tenths. Two frames, because the leak shows on
   *    whatever is drawn next and this document draws nothing after the door. */
  R.render(page.context, doorDoc("horizontal", "open"), DOOR_VIEW, {});
  R.render(page.context, doorDoc("horizontal", "open"), DOOR_VIEW, {});
  const faint = page.last().fills.filter((fill) => fill[5] !== 1);
  check("a door that swings hands the context back at full opacity",
    faint.length === 0, "painted under a borrowed alpha: " + show(faint.slice(0, 3)));

  /* 6. The arc itself, which no case could see until the recorder learned to
   *    capture paths. This is the recorder's own self-check as much as the
   *    door's: delete the ctx.arc() in drawSwing and only these go red. */
  R.render(page.context, doorDoc("horizontal", "open"), DOOR_VIEW, {});
  const arcs = page.last().paths.filter(
    (path) => path.kind === "stroke" && path.ops.some((op) => op[0] === "arc"));
  check("an open door strokes a swing arc, and it is drawn as an arc",
    arcs.length === 1, show(page.last().paths));
  /* The centre is the hinge, which sits mid-edge on the jamb the leaf hangs
   * from — not the square's corner and not its centre. Asserted as "on this
   * square, in its west half" rather than as the exact pixel: the recorder
   * hands over coordinates, and pinning the inset would pin the leaf's
   * thickness to this case for no gain. */
  const arcAt = arcs.length === 1 ? arcs[0].ops[0] : null;
  check("the arc is struck about the hinge, at the leaf's own ink",
    arcAt !== null && arcs[0].ink === DOOR_INK
      && arcAt[1] >= px && arcAt[1] < px + DOOR_VIEW.scale / 2
      && near(arcAt[2], py + DOOR_VIEW.scale / 2),
    show(arcs));
  check("and it is the faint one — the arc is what borrows the alpha",
    arcs.length === 1 && arcs[0].alpha < 1, show(arcs));

  /* Both orientations, because they are separate branches in drawFeature: a
   * case naming only one leaves the other free to stroke an arc a shut door has
   * no business drawing. Found by mutation — the first version of this case
   * checked "horizontal" alone and survived an arc added to the vertical
   * branch. */
  const shutArcs = (orientation) => {
    R.render(page.context, doorDoc(orientation, "closed"), DOOR_VIEW, {});
    return page.last().paths.filter((path) => path.ops.some((op) => op[0] === "arc"));
  };
  /* Each rendered once and held: calling shutArcs() again for the failure
   * detail would report a *different* frame than the one that failed, and this
   * suite exists to catch renders that are not idempotent (case 5 above turns
   * on exactly that). */
  const shutArcsH = shutArcs("horizontal");
  const shutArcsV = shutArcs("vertical");
  check("a closed door strokes no arc: there is nothing to show swinging",
    shutArcsH.length === 0, show(shutArcsH));
  check("and a closed vertical door strokes none either",
    shutArcsV.length === 0, show(shutArcsV));
});

/* --- facing ---------------------------------------------------------------
 * One vocabulary on three carriers — a creature, a map feature, and the map
 * itself — and every one of them draws as a path rather than a rect, so none of
 * this was observable here until the recorder learned to capture path
 * construction. A feature and the compass draw a chevron; a creature draws the
 * sight cone that replaced its own chevron, which is a fill rather than a
 * stroke and the reason there are two predicates below.
 *
 * What makes these cases mean anything is that renderer.js draws these glyphs in
 * **absolute coordinates**. The recorder stores moveTo/lineTo arguments as
 * passed, so a glyph drawn under a translate/rotate pair would record the
 * identical points for all eight facings, and every direction case below would
 * pass against a renderer that ignored the facing entirely. It is also why the
 * cone's two arcs are runs of lineTo rather than ctx.arc: an arc struck under a
 * rotated context records one centre and one pair of angles whatever the facing,
 * which is the same blind spot one call wider. tests/test_web_assets.py holds
 * the source to those rules; these cases spend them.
 *
 * The other half is telling these glyphs' paths from the dozen others a frame
 * contains — the token circle, the HP ring, the dead cross, the door's arc. The
 * facing ink is used by nothing else, a chevron is exactly three ops, and the
 * cone is the one filled many-sided path drawn in that ink, so `chevronsIn` and
 * `conesIn` each name one shape and not "some path exists". */

const FACING_INK = "#a8462a";  /* the light-theme facing ink; the stub says light */
const FACING_VIEW = { x: 0, y: 0, scale: 20, width: 160, height: 120 };
const FACING_AT = [3, 2];

/* The eight names and the grid step each points along: north is −y. Written out
 * rather than imported, because a table this file shared with renderer.js could
 * not catch renderer.js getting it wrong. */
const FACING_STEPS = {
  north: [0, -1], northeast: [1, -1], east: [1, 0], southeast: [1, 1],
  south: [0, 1], southwest: [-1, 1], west: [-1, 0], northwest: [-1, -1],
};

const inkPaths = (frame) => frame.paths.filter((path) => path.ink === FACING_INK);
/* A chevron: two wings and a tip, stroked, in the facing ink. The tip is the
 * middle op — moveTo(wing), lineTo(tip), lineTo(wing). */
const chevronsIn = (frame) => inkPaths(frame).filter(
  (path) => path.kind === "stroke" && path.ops.length === 3
    && path.ops[0][0] === "moveTo" && path.ops[1][0] === "lineTo"
    && path.ops[2][0] === "lineTo"
);
const tipOf = (chevron) => [chevron.ops[1][1], chevron.ops[1][2]];

/* A sight cone: the wedge along a creature's facing with its apex clipped off
 * at an inner radius — an annular sector — filled in that same facing ink. Two
 * arcs of eight lineTo segments each, joined by a radial at either end, and the
 * shape admits exactly two forms: eighteen vertices written the obvious way
 * (moveTo, eight, the radial, eight) and closed by ctx.closePath(), or nineteen
 * with that closing radial drawn as a vertex of its own. The window is stated as
 * a window because that one join is the thing an implementation may count either
 * way; what it names is the shape, and nothing else in a frame is a many-sided
 * filled path in this ink.
 *
 * It used to admit three, and the third was nobody's intent: eighteen vertices
 * and no closure at all. A mutation run reached it by deleting ctx.closePath()
 * and killed no case in the suite. The wash hides that — fill() closes the
 * subpath for you — but the rim does not, because stroke() leaves the wedge open
 * along the closing radial, so the shape would ship with one edge missing. Hence
 * `isClosedPath`, and hence it is written as "closed one way or the other"
 * rather than as "there is a closePath": the latter would forbid the
 * nineteen-vertex form this window was widened to allow.
 *
 * The commit is what makes "exactly one cone" a fact rather than a coin flip:
 * the cone commits its path twice, once filled and once stroked along the edge
 * in the same ink, so a predicate that did not name the commit would count every
 * cone as two. The shape test is therefore shared and the commit named on top of
 * it — `conesIn` for the wash, `coneEdgesIn` for the rim, which is a claim of
 * its own and asserted rather than merely tolerated. */
const CONE_ARC_SEGMENTS = 8;
const CONE_MIN_VERTICES = 2 * CONE_ARC_SEGMENTS + 1;
const CONE_MAX_VERTICES = 2 * CONE_ARC_SEGMENTS + 3;
const isVertex = (op) => op[0] === "moveTo" || op[0] === "lineTo";
/* Compared with a tolerance rather than for equality: the two ends of the wedge
 * are reached by different arithmetic — one straight off the start bearing, the
 * other after eight additions of the step — and floating point does not promise
 * they land on the same bits. */
const CLOSURE_TOLERANCE = 0.01;
const isClosedPath = (path) => {
  if (!path.ops.length) { return false; }
  if (path.ops[path.ops.length - 1][0] === "closePath") { return true; }
  const points = path.ops.filter(isVertex);
  if (points.length < 2) { return false; }
  const first = points[0];
  const last = points[points.length - 1];
  return Math.abs(first[1] - last[1]) < CLOSURE_TOLERANCE
    && Math.abs(first[2] - last[2]) < CLOSURE_TOLERANCE;
};
/* The outline without the closure claim. `hasConeShape` rejects an open wedge
 * and an absent one alike, and this is what tells the two apart in a failure's
 * detail — without it, deleting the closure would report "the cone is gone". */
const hasWedgeOutline = (path) => {
  if (path.ink !== FACING_INK) { return false; }
  if (!path.ops.length || path.ops[0][0] !== "moveTo") { return false; }
  if (path.ops.some((op) => !isVertex(op) && op[0] !== "closePath")) { return false; }
  const vertices = path.ops.filter(isVertex).length;
  return vertices >= CONE_MIN_VERTICES && vertices <= CONE_MAX_VERTICES;
};
const hasConeShape = (path) => hasWedgeOutline(path) && isClosedPath(path);
const openWedgesIn = (frame) => frame.paths.filter(
  (path) => hasWedgeOutline(path) && !isClosedPath(path));
const isCone = (path) => path.kind === "fill" && hasConeShape(path);
const isConeEdge = (path) => path.kind === "stroke" && hasConeShape(path);
const conesIn = (frame) => frame.paths.filter(isCone);
const coneEdgesIn = (frame) => frame.paths.filter(isConeEdge);
const verticesOf = (cone) => cone.ops.filter(isVertex);
/* The mean of those vertices. The sector is symmetric about the facing, so the
 * centroid sits on that axis and its bearing from the token's centre is the
 * direction the cone was aimed — which is the one thing a fixed wedge cannot
 * fake. */
const centroidOf = (cone) => {
  const points = verticesOf(cone);
  return [
    points.reduce((sum, op) => sum + op[1], 0) / points.length,
    points.reduce((sum, op) => sum + op[2], 0) / points.length,
  ];
};
/* Zero has a sign in floating point, and the four cardinal facings land on it:
 * a north cone's centroid is a rounding error east of the token, which
 * Math.sign alone would report as east. */
const bearingSign = (offset) => (Math.abs(offset) < 0.01 ? 0 : Math.sign(offset));
/* The token's own disc — one arc, filled, in the team colour. No facing glyph
 * has that signature, which is how a case can say the token was drawn while
 * saying nothing about the cone. */
const isDisc = (path) => path.kind === "fill" && path.ink !== FACING_INK
  && path.ops.length === 1 && path.ops[0][0] === "arc";
const discsIn = (frame) => frame.paths.filter(isDisc);

function facingDoc(options) {
  const feature = { id: "slit", kind: "opening", at: [FACING_AT[0], FACING_AT[1]] };
  if (options.facing !== undefined) { feature.facing = options.facing; }
  const doc = {
    grid: { width: 8, height: 6 },
    legend: { ".": "floor", "#": "wall" },
    tiles: ["########", "#......#", "#......#", "#......#", "#......#", "########"],
    features: options.bare ? [] : [feature],
  };
  if (options.compass !== undefined) { doc.compass = options.compass; }
  return doc;
}

await suite("renderer.js: a feature's facing", "the renderer sandbox", async () => {
  const page = makePage({ canvasIds: ["map"] });
  const R = page.renderer;
  const s = FACING_VIEW.scale;
  const cx = FACING_AT[0] * s + s / 2;
  const cy = FACING_AT[1] * s + s / 2;
  const draw = (facing) => {
    R.render(page.context, facingDoc({ facing: facing }), FACING_VIEW, {});
    return chevronsIn(page.last());
  };

  /* Named rather than thrown, unlike the door suite's ink guard: three suites
   * filter on this ink, so a retuned colour would empty all of them, and the
   * two causes are indistinguishable from the frame — the hint therefore rides
   * the failure of the case it explains, and the rest of the suite still
   * runs. */
  const east = draw("east");
  check("a feature that states a facing is drawn with one chevron",
    east.length === 1,
    "no three-op stroked path in " + FACING_INK + ": either the facing ink changed, and"
    + " FACING_INK in scripts/check-editor-behaviour.mjs is stale, or the chevron is gone."
    + "\n  every path in this frame: " + show(page.last().paths));
  check("and it is struck about the feature's own square, not the canvas",
    east.length === 1 && east[0].ops.every(
      (op) => Math.abs(op[1] - cx) <= s && Math.abs(op[2] - cy) <= s),
    show(east));

  /* The negative that makes the case above mean anything: a chevron drawn for
   * every feature would satisfy it while saying nothing about the key. */
  R.render(page.context, facingDoc({}), FACING_VIEW, {});
  check("a feature that states none is drawn without one",
    chevronsIn(page.last()).length === 0, show(page.last().paths));

  /* And the key is read, not merely present: a name outside the eight draws
   * nothing rather than guessing, and `constructor` is a string a hand-opened
   * file may carry into an object lookup. */
  R.render(page.context, facingDoc({ facing: "up" }), FACING_VIEW, {});
  check("a name outside the eight draws no chevron at all",
    chevronsIn(page.last()).length === 0, show(page.last().paths));
  R.render(page.context, facingDoc({ facing: "constructor" }), FACING_VIEW, {});
  check("and neither does a name off the prototype chain",
    chevronsIn(page.last()).length === 0, show(page.last().paths));

  /* All eight in one case, because the claim is that the *name* decides the
   * direction. A renderer that drew a fixed arrow would pass every case above
   * this one. */
  const misaimed = [];
  const tips = new Set();
  Object.keys(FACING_STEPS).forEach((name) => {
    const found = draw(name);
    if (found.length !== 1) {
      misaimed.push(name + ": " + found.length + " chevrons");
      return;
    }
    const tip = tipOf(found[0]);
    tips.add(tip.join(","));
    const step = FACING_STEPS[name];
    if (Math.sign(tip[0] - cx) !== step[0] || Math.sign(tip[1] - cy) !== step[1]) {
      misaimed.push(name + " tips toward " + show([tip[0] - cx, tip[1] - cy]));
    }
  });
  check("each of the eight names aims the chevron its own way",
    misaimed.length === 0, misaimed.join("; "));
  check("and no two of them describe the same path",
    tips.size === 8, show(Array.from(tips)));
});

/* A creature's facing draws as a sight cone: a 90° wedge along the bearing,
 * translucent, and under every token in the frame. It claims nothing about what
 * the creature can see — no wall stops it, deliberately, because a
 * line-of-sight implementation here would be a second and wrong copy of the
 * engine's own in kernel/grid.py. So these cases check the *drawing*: one shape,
 * clear of the token's own furniture, aimed by the name, suppressed where a
 * facing stops being a fact, and painted under the tokens rather than over
 * them. */
await suite("renderer.js: a token's sight cone", "the renderer sandbox", async () => {
  const page = makePage({ canvasIds: ["map"] });
  const R = page.renderer;
  const s = FACING_VIEW.scale;
  const cx = FACING_AT[0] * s + s / 2;
  const cy = FACING_AT[1] * s + s / 2;
  /* The HP ring is stroked on r + max(1.5, 0.07·s) at that width, so its outer
   * edge is half a width further out. "Outside the ring" is measured against
   * that, not against the token's own radius. */
  const ringEdge = s * 0.36 + Math.max(1.5, s * 0.07) * 1.5;
  const reach = s * 6;   /* how far the cone is drawn from the token's centre */
  const away = (point) => Math.sqrt(
    (point[1] - cx) * (point[1] - cx) + (point[2] - cy) * (point[2] - cy));
  /* Each token carries its own `at`, so a frame can hold two of them, and the
   * overlays are merged over the token list — which is how `sightCones: false`
   * gets in without a second render helper. */
  const frameFor = (tokens, overlays) => {
    R.render(page.context, facingDoc({ bare: true }), FACING_VIEW, Object.assign({
      tokens: tokens.map(
        (token) => Object.assign({ at: FACING_AT, label: "Hero", team: "party" }, token)),
    }, overlays || {}));
    return page.last();
  };
  const draw = (token) => conesIn(frameFor([token]));

  /* Named rather than thrown, like the feature suite's: three suites filter on
   * this ink, so a retuned colour would empty all of them, and the two causes
   * are indistinguishable from the frame. */
  const north = draw({ hpFraction: 0.5, facing: "north" });
  /* Held rather than re-read, because the cases below this one assert on the
   * same frame and a later `draw` would replace what `page.last()` answers. */
  const northFrame = page.last();
  const radii = north.length === 1 ? verticesOf(north[0]).map(away) : [];
  const nearest = radii.length ? Math.min(...radii) : NaN;
  const farthest = radii.length ? Math.max(...radii) : NaN;
  check("a token that carries a facing is drawn with one sight cone",
    north.length === 1,
    "no closed filled path of " + CONE_MIN_VERTICES + " to " + CONE_MAX_VERTICES
    + " vertices in " + FACING_INK + ": either the facing ink changed, and FACING_INK"
    + " in scripts/check-editor-behaviour.mjs is stale, or the cone is gone, or it is"
    + " drawn and left open — which the case below tells apart from the other two."
    + "\n  every path in this frame: " + show(northFrame.paths));
  /* Closure, named on its own so an open wedge reads as an open wedge rather
   * than as a missing one. Invisible in the wash, since fill() closes the
   * subpath for you, and plain in the rim, which stroke() leaves open along the
   * closing radial. Nothing in this suite saw it until this case existed:
   * deleting ctx.closePath() from drawSightCone killed no case at all. */
  check("and the wedge is a closed shape, not an outline left open at the rim",
    north.length === 1 && openWedgesIn(northFrame).length === 0,
    show(openWedgesIn(northFrame).map((path) => path.ops)));
  check("and every part of it lies outside the HP ring",
    north.length === 1 && verticesOf(north[0]).every((op) => away(op) > ringEdge),
    show([north.map((cone) => verticesOf(cone).map(away)), ringEdge]));
  /* Bounded below as well, because "outside the ring" alone is met by a
   * clearance that exists only in the arithmetic: an inner radius of
   * `ringEdge + 0.001` passed every case in this suite while the wash lay on
   * the hit-point ring it is meant to stay clear of. One device pixel is the
   * smallest gap a canvas can express, so that is the floor asserted — the
   * renderer leaves max(2, 0.06·s), which is two at this scale. The headroom
   * between the two numbers is deliberate: what this pins is that the clearance
   * is visible, not the formula that produces it. */
  check("and it clears that ring by a visible margin, not by a rounding error",
    north.length === 1 && nearest >= ringEdge + 1,
    show([nearest, ringEdge, ringEdge + 1]));
  check("and none of it is drawn past the reach it claims",
    north.length === 1 && verticesOf(north[0]).every((op) => away(op) <= reach + 0.01),
    show([north.map((cone) => verticesOf(cone).map(away)), reach]));
  /* And bounded below too, for the same reason the clearance is: the upper
   * bound alone is met by any cone shorter than it claims, and a two-square
   * reach passed every case in this suite. The outer arc is struck at the reach
   * exactly, so this is an equality and not a range, and the six squares it
   * pins are the distance drawSightCone's own comment argues for. Retuning that
   * distance turns this red on purpose — recalibrate deliberately, the way the
   * fight constants in scripts/check-api-smoke.py are. */
  check("and it is drawn out to that reach rather than a stub of it",
    north.length === 1 && Math.abs(farthest - reach) <= 0.01,
    show([farthest, reach]));

  /* The negative that makes the case above mean anything: a cone drawn for
   * every token would satisfy it while saying nothing about the key. */
  check("a token that carries none is drawn without one",
    draw({ hpFraction: 0.5 }).length === 0, show(page.last().paths));
  /* And the key is read, not merely present: a name outside the eight draws
   * nothing rather than guessing, and `constructor` is a string a hand-opened
   * file may carry into an object lookup. */
  check("a name outside the eight draws no cone at all",
    draw({ hpFraction: 0.5, facing: "up" }).length === 0, show(page.last().paths));
  check("and neither does a name off the prototype chain",
    draw({ hpFraction: 0.5, facing: "constructor" }).length === 0, show(page.last().paths));
  /* The token overlay is the only carrier with a state where the direction
   * stops being a fact: a body is not facing anywhere, and the square already
   * says so with a cross. */
  check("and a dead one loses its cone rather than keeping the last bearing",
    draw({ hpFraction: 0, dead: true, facing: "north" }).length === 0,
    show(page.last().paths));

  /* All eight in one case, because the claim is that the *name* decides the
   * direction. A renderer that drew a fixed wedge would pass every case above
   * this one and every case below it. Details are evaluated eagerly, so this
   * reads through `found[0]` only after its own length test — a failure that
   * dereferenced a missing cone would take the rest of the suite with it and
   * hide its own cause. */
  const misaimed = [];
  const shapes = new Set();
  const bearings = [];
  Object.keys(FACING_STEPS).forEach((name) => {
    const found = draw({ hpFraction: 0.5, facing: name });
    if (found.length !== 1) {
      misaimed.push(name + ": " + found.length + " cones");
      return;
    }
    shapes.add(show(found[0].ops));
    const middle = centroidOf(found[0]);
    const offset = [middle[0] - cx, middle[1] - cy];
    bearings.push(name + " -> " + show(offset.map((value) => Math.round(value))));
    const step = FACING_STEPS[name];
    if (bearingSign(offset[0]) !== step[0] || bearingSign(offset[1]) !== step[1]) {
      misaimed.push(name + " aims toward " + show(offset));
    }
  });
  check("each of the eight names aims the cone its own way",
    misaimed.length === 0, misaimed.join("; "));
  check("and no two of them describe the same path",
    shapes.size === 8, show(bearings));

  /* The caller's opt-out, and the reason the second half of it is asserted: a
   * renderer that dropped the whole token pass would satisfy "no cone" without
   * honouring anything at all. */
  const suppressed = frameFor([{ hpFraction: 0.5, facing: "north" }], { sightCones: false });
  check("a caller that turns the cones off is drawn none",
    conesIn(suppressed).length === 0, show(suppressed.paths));
  check("and the token itself is still drawn",
    discsIn(suppressed).length === 1, show(suppressed.paths));

  /* Two tokens, because the claim is about the pass rather than about one
   * glyph: a cone drawn inside drawToken sits under its own token and over the
   * one beside it, which reads as one creature seeing through another's back.
   * The discs are identified by their own signature, so this case says nothing
   * about the cone twice over. */
  const pair = frameFor([
    { hpFraction: 0.5, facing: "north" },
    {
      at: [FACING_AT[0] + 2, FACING_AT[1]], hpFraction: 0.5, facing: "south",
      label: "Foe", team: "foes",
    },
  ]);
  const coneAt = pair.paths.map((path, index) => (isCone(path) ? index : -1))
    .filter((index) => index >= 0);
  const discAt = pair.paths.map((path, index) => (isDisc(path) ? index : -1))
    .filter((index) => index >= 0);
  check("every cone in a frame is committed before every token disc",
    coneAt.length === 2 && discAt.length === 2
      && Math.max(...coneAt) < Math.min(...discAt),
    show([coneAt, discAt]));

  /* The cone is a wash, and the context outlives the frame: a glyph that
   * borrows globalAlpha and loses the restore leaves everything drawn after it
   * faint — this frame's tokens, and every frame after it, since render() never
   * resets alpha. The disc is committed after the cone (the case above), which
   * is what makes it the witness. */
  const washed = frameFor([{ hpFraction: 0.5, facing: "north" }]);
  const cone = conesIn(washed);
  const discs = discsIn(washed);
  check("the cone is painted as a wash rather than a solid",
    cone.length === 1 && cone[0].alpha > 0 && cone[0].alpha < 0.5,
    show(cone.map((path) => path.alpha)));
  check("and it hands the context back: the token drawn over it is at full alpha",
    discs.length === 1 && discs.every((disc) => disc.alpha === 1),
    show(discs.map((disc) => disc.alpha)));

  /* The rim, which the wash does not imply: at a tenth opacity a cone with no
   * edge reads as a smudge, and every case above this one is satisfied without
   * it. It is one path committed twice rather than a second shape in the same
   * ink — the vertex lists have to match — and it carries the firmer alpha,
   * which is what keeps the boundary legible where the wash barely registers.
   * The detail lists every path's commit, ink and alpha, so a rim stroked in
   * the wrong ink reads off the failure rather than looking like an absent
   * one. */
  const edge = coneEdgesIn(washed);
  check("the cone's edge is stroked in the same ink, not only filled",
    edge.length === 1,
    show(washed.paths.map((path) => [path.kind, path.ink, path.alpha])));
  check("and it traces the fill rather than a second shape",
    edge.length === 1 && cone.length === 1
      && show(verticesOf(edge[0])) === show(verticesOf(cone[0])),
    show([edge.map(verticesOf), cone.map(verticesOf)]));
  check("and it is drawn firmer than the wash, and still short of opaque",
    edge.length === 1 && cone.length === 1
      && edge[0].alpha >= cone[0].alpha * 2 && edge[0].alpha < 1,
    show([cone.map((path) => path.alpha), edge.map((path) => path.alpha)]));
});

await suite("renderer.js: the document's compass", "the renderer sandbox", async () => {
  const page = makePage({ canvasIds: ["map"] });
  const R = page.renderer;
  const draw = (compass, view) => {
    R.render(page.context, facingDoc({ bare: true, compass: compass }),
      view || FACING_VIEW, {});
    return page.last();
  };

  const east = draw("east");
  const eastRose = chevronsIn(east);
  check("a document that states a compass draws a rose",
    eastRose.length === 1, show(east.paths));
  check("and the rose is a needle as well as a head",
    inkPaths(east).length === 2, show(inkPaths(east)));
  /* A property of the map, not of a square: it belongs to the corner of the
   * view, and no cell can be drawn over it. */
  check("it is struck in the corner of the view rather than on a square",
    eastRose.length === 1 && tipOf(eastRose[0])[0] > FACING_VIEW.width * 0.6
      && tipOf(eastRose[0])[1] < FACING_VIEW.height * 0.4, show(eastRose));
  /* Every detail below reads through `.map`, not `[0]`: they are evaluated
   * whether the case passed or not, and a rose that is simply missing must not
   * be reported as this file throwing. */
  const panned = draw("east", { x: 2.5, y: 1.5, scale: 32, width: 160, height: 120 });
  /* The length is asserted as well as the equality: two empty lists are equal,
   * so an unqualified comparison here would be satisfied by a rose that was
   * never drawn at all — which is exactly the mutant the case beside it kills. */
  check("and it stays put when the map pans and zooms under it",
    chevronsIn(panned).length === 1 && show(chevronsIn(panned)) === show(eastRose),
    show([chevronsIn(panned), eastRose]));

  check("a document that states none draws no rose at all",
    inkPaths(draw(undefined)).length === 0, show(draw(undefined).paths));
  check("and a compass outside the eight names draws none either",
    inkPaths(draw("toward the sea")).length === 0, show(draw("toward the sea").paths));

  const west = chevronsIn(draw("west"));
  check("the rose turns with the compass",
    west.length === 1 && eastRose.length === 1
      && tipOf(west[0])[0] < tipOf(eastRose[0])[0],
    show([west.map(tipOf), eastRose.map(tipOf)]));

  /* The rose borrows alpha the way the door's swing arc does, and the context
   * outlives the frame: a lost restore leaves every later frame — the whole map,
   * not one ornament — painted at eighty-five hundredths. Two frames, because
   * the rose is the last thing this document draws. */
  draw("east");
  const second = draw("east");
  const faint = second.fills.filter((fill) => fill[5] !== 1);
  check("a drawn rose hands the context back at full opacity",
    faint.length === 0, "painted under a borrowed alpha: " + show(faint.slice(0, 3)));
});

/* --- viewer.html ---------------------------------------------------------- */

function replayMap() {
  return {
    grid: { width: 8, height: 6, cell_feet: 5 },
    legend: { ".": "floor", "#": "wall" },
    tiles: ["########", "#......#", "#......#", "#......#", "#......#", "########"],
    features: [
      { id: "spawn-a", kind: "spawn", at: [1, 1] },
      {
        id: "door-1", kind: "door", at: [6, 1], orientation: "vertical",
        state: "closed", terrain: { closed: "wall", open: "floor" },
      },
      {
        id: "sluice", kind: "sluice", at: [4, 3], state: "open",
        terrain: { closed: "wall", open: "water" },
        affects: [{ cells: [[1, 4], [2, 4]], terrain: { closed: "floor", open: "water" } }],
      },
    ],
  };
}

function replayBundle(openList) {
  return {
    format: "fivee-sim-replay",
    format_version: 1,
    name: "sluice fight",
    seed: 7,
    map: replayMap(),
    initial: {
      creatures: [{ name: "Hero", team: "party", position: [10, 10], hp: 9, max_hp: 9 }],
      map_open_features: openList,
    },
    events: [
      { kind: "round", round: 1, turn: "", actor: "", data: {} },
      { kind: "turn_start", round: 1, turn: "Hero", actor: "Hero", data: {} },
      { kind: "interact", round: 1, turn: "Hero", actor: "Hero",
        data: { feature: "sluice", open: false } },
      { kind: "interact", round: 1, turn: "Hero", actor: "",
        data: { feature: "sluice", open: true, automatic: true, triggered_by: "lever" } },
    ],
  };
}

function compareCodePoints(left, right) {
  const leftPoints = Array.from(left);
  const rightPoints = Array.from(right);
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = leftPoints[index].codePointAt(0) - rightPoints[index].codePointAt(0);
    if (difference) { return difference; }
  }
  return leftPoints.length - rightPoints.length;
}

function canonicalJson(value) {
  if (value === null || typeof value !== "object") { return JSON.stringify(value); }
  if (Array.isArray(value)) { return "[" + value.map(canonicalJson).join(",") + "]"; }
  return "{" + Object.keys(value).sort(compareCodePoints).map((key) => (
    JSON.stringify(key) + ":" + canonicalJson(value[key])
  )).join(",") + "}";
}

function canonicalHash(value) {
  return createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}

function sealReplayV2(bundle) {
  bundle.checkpoints.forEach((checkpoint) => {
    checkpoint.state_hash = canonicalHash(checkpoint.state);
  });
  const unhashedContent = {};
  Object.keys(bundle.content).forEach((key) => {
    if (key !== "sha256") { unhashedContent[key] = bundle.content[key]; }
  });
  bundle.content.sha256 = canonicalHash(unhashedContent);
  bundle.integrity = {
    algorithm: "sha256",
    map: canonicalHash(bundle.map),
    initial: canonicalHash(bundle.initial),
    events: canonicalHash(bundle.events),
    actions: canonicalHash(bundle.actions),
    checkpoints: canonicalHash(bundle.checkpoints),
    latest_state: canonicalHash(bundle.latest_state),
    content: bundle.content.sha256,
  };
  return bundle;
}

function replayV2() {
  const bundle = replayBundle([]);
  bundle.format_version = 2;
  bundle.map.levels = [{
    index: 1, name: "gallery",
    tiles: ["........", "........", "........", "........", "........", "........"],
    features: [], elevation: { default: 10, squares: [] },
  }];
  const hero = {
    name: "Hero", team: "party", position: [10, 10], hp: 9, max_hp: 9, ac: 17,
    level: 0, conditions: ["Prone"], concentrating_on: "Ward", dodging: true,
    disengaged: false, reaction_available: false, conscious: true, dead: false,
    stable: false, death_saves: { successes: 1, failures: 2 },
    spell_slots: { 1: 2 }, items: { Potion: 1 },
  };
  bundle.initial.state = { round: 1, turn: "Hero", combatants: [hero] };
  bundle.initial.combatants = [copy(hero)];
  bundle.events = [{
    seq: 0, kind: "move", round: 1, turn: "Hero", actor: "Hero", target: "",
    timestamp: "2026-01-01T00:00:02Z",
    data: {
      origin: [10, 10], destination: [20, 10], from_level: 0, to_level: 1,
      completed: true,
    },
  }];
  const upstairs = copy(hero);
  upstairs.position = [20, 10];
  upstairs.level = 1;
  upstairs.conditions = [];
  bundle.checkpoints = [{
    index: 1, event_count: 1, timestamp: "2026-01-01T00:00:02Z",
    state: { round: 1, turn: "Hero", combatants: [upstairs] }, state_hash: "test",
  }];
  bundle.attempts = [
    {
      index: 0, operation: "check", status: "refused",
      timestamp: "2026-01-01T00:00:00Z", arguments: { skill: "intimidation" },
      error: "the sentry is already hostile",
    },
    {
      index: 1, operation: "encounter_note", status: "success",
      timestamp: "2026-01-01T00:00:01Z",
      arguments: { category: "negotiation", text: "The bridge is unsafe." }, result: {},
    },
  ];
  bundle.actions = [];
  bundle.latest_state = copy(bundle.checkpoints[0].state);
  bundle.content = { "\ue000": 1, "😀": 2 };
  bundle.encounter = { id: "enc-test", seed: 7, movement_rule: "5-5-5" };
  return sealReplayV2(bundle);
}

function setPath(target, dotted, value) {
  const path = dotted.split(".");
  let cursor = target;
  path.slice(0, -1).forEach((key) => { cursor = cursor[key]; });
  cursor[path[path.length - 1]] = value;
}

await suite("viewer.html: folding a replay's fixtures", "the page sandbox in makePage()",
  async () => {
    const page = makePage({ canvasIds: ["stage"], seed: { "embedded-data": "null" } });
    page.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    check("the page boots with no bundle and draws nothing",
      page.renders.length === 0 && page.alerts.length === 0, show(page.alerts));

    const overridesOf = (frame) => (frame && frame.overlays.terrainOverrides) || {};
    const scrubTo = (n) => {
      page.element("scrub").value = String(n);
      page.element("scrub").dispatch("input");
    };
    const forward = () => page.element("btn-forward").click();

    /* 1. Seeding is kind-general. A sluice is not a door, and while the seeding
     *    was door-gated it was never seeded at all. */
    await page.drop(replayBundle(["sluice"]), "sluice.json");
    check("a dropped bundle reaches the canvas", page.renders.length > 0,
      String(page.renders.length));
    check("the readout counts the events", page.element("readout").textContent.indexOf("0/4") > 0,
      page.element("readout").textContent);
    check("a fixture the live list opens floods the room at event 0",
      overridesOf(page.last())["1,4"] === "water", show(page.last().overlays.terrainOverrides));
    check("and recolours the fixture's own square",
      overridesOf(page.last())["4,3"] === "water", overridesOf(page.last())["4,3"]);
    check("a door in the same bundle is drawn shut",
      overridesOf(page.last())["6,1"] === "wall", overridesOf(page.last())["6,1"]);

    /* 2. The live list beats the document. A bundle's `map` and its
     *    `map_open_features` are independent fields, and a list naming the door
     *    but not the sluice says the sluice starts shut — whatever the map
     *    authored. While the seeding was door-gated the sluice fell through to
     *    the authored fallback and flooded the room anyway. */
    await page.drop(replayBundle(["door-1"]), "door-only.json");
    check("a live list that contradicts the document wins",
      overridesOf(page.last())["1,4"] === "floor", show(page.last().overlays.terrainOverrides));
    check("and the door it does name is drawn open",
      overridesOf(page.last())["6,1"] === "floor", overridesOf(page.last())["6,1"]);

    /* 3. An interact on a non-door moves the overlay. */
    await page.drop(replayBundle(["sluice"]), "sluice.json");
    const floodedFill = page.fillAt(1, 4);
    scrubTo(3);
    check("an interact on a non-door drains the room",
      overridesOf(page.last())["1,4"] === "floor" && overridesOf(page.last())["2,4"] === "floor",
      show(page.last().overlays.terrainOverrides));
    check("and reverts its own square", overridesOf(page.last())["4,3"] === "wall",
      overridesOf(page.last())["4,3"]);
    check("the square the replay paints actually changes colour",
      floodedFill !== null && page.fillAt(1, 4) !== null && floodedFill !== page.fillAt(1, 4),
      show([floodedFill, page.fillAt(1, 4)]));
    forward();
    check("the next interact floods it again",
      overridesOf(page.last())["1,4"] === "water", show(page.last().overlays.terrainOverrides));
    check("a door no event touched keeps the state it was seeded with",
      overridesOf(page.last())["6,1"] === "wall", overridesOf(page.last())["6,1"]);

    /* 4. Forward is the incremental fold; a scrub is a rebuild from zero. The
     *    two must agree, or scrubbing back shows a different fight. */
    const byIncrement = show(page.last().overlays.terrainOverrides);
    scrubTo(0);
    scrubTo(4);
    check("scrubbing to an event matches folding forward to it",
      show(page.last().overlays.terrainOverrides) === byIncrement,
      byIncrement + "\n vs \n" + show(page.last().overlays.terrainOverrides));
    scrubTo(3);
    check("and scrubbing back returns the earlier overrides",
      overridesOf(page.last())["1,4"] === "floor", show(page.last().overlays.terrainOverrides));

    /* 5. A linked-door event names its mate explicitly. Folding it changes
     *    both leaves as one state transition, including a rebuild by scrub. */
    const linked = replayBundle([]);
    linked.map.features = [
      { id: "left", kind: "door", at: [2, 2], orientation: "horizontal",
        state: "closed", linked_to: "right", terrain: { closed: "wall", open: "floor" } },
      { id: "right", kind: "door", at: [3, 2], orientation: "horizontal",
        state: "closed", linked_to: "left", terrain: { closed: "wall", open: "floor" } },
    ];
    linked.events = [{ kind: "interact", round: 1, turn: "Hero", actor: "Hero",
      data: { feature: "left", linked: ["right"], open: true } }];
    await page.drop(linked, "linked.json");
    scrubTo(1);
    check("a linked-door interaction folds both leaves together",
      page.last().overlays.featureStates.left === true
        && page.last().overlays.featureStates.right === true,
      show(page.last().overlays.featureStates));

    /* 6. Nothing to override hands the renderer nothing. The renderer guards
     *    its per-cell lookup on the channel's *presence*, so an empty map
     *    handed down would cost a concat and a miss per visible cell on every
     *    replay that has no fixtures at all. */
    const mapless = replayBundle([]);
    mapless.map = null;
    await page.drop(mapless, "mapless.json");
    check("a mapless replay still folds and draws", page.last() !== undefined,
      String(page.renders.length));
    check("and hands down no channel at all",
      page.last().overlays.terrainOverrides === undefined,
      show(page.last().overlays.terrainOverrides));
    const plain = replayBundle([]);
    plain.map.features = [{ id: "spawn-a", kind: "spawn", at: [1, 1] }];
    await page.drop(plain, "plain.json");
    check("a fixture-less map hands down no channel either",
      page.last().overlays.terrainOverrides === undefined,
      show(page.last().overlays.terrainOverrides));

    /* 7. Anything may be dropped on this page. */
    await page.drop({ format: "not-a-replay" }, "wrong.json");
    check("a bundle that is not a replay is refused, not rendered",
      page.alerts.length === 1 && page.alerts[0].indexOf("fivee-sim-replay") !== -1,
      show(page.alerts));
  });

await suite("viewer.html: replay v2 state and validation", "the page sandbox in makePage()",
  async () => {
    const page = makePage({ canvasIds: ["stage"], seed: { "embedded-data": "null" } });
    page.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    page.element("follow-level").checked = true;
    const bundle = replayV2();
    await page.drop(bundle, "v2.json");
    check("v2 exposes full combatant state instead of only hit points",
      page.element("combatant-state").textContent.indexOf("AC 17") !== -1
        && page.element("combatant-state").textContent.indexOf("1✓/2✗") !== -1
        && page.element("combatant-state").textContent.indexOf("concentrating: Ward") !== -1,
      page.element("combatant-state").textContent);
    check("refusals and notes share the audit timeline",
      page.element("ticker").textContent.indexOf("intimidation refused") !== -1
        && page.element("ticker").textContent.indexOf("The bridge is unsafe") !== -1,
      page.element("ticker").textContent);

    page.element("scrub").value = "1";
    page.element("scrub").dispatch("input");
    check("a cross-storey checkpoint follows the actor to its resulting plane",
      page.element("level-select").value === "1"
        && page.last().overlays.tokens.length === 1
        && show(page.last().overlays.tokens[0].at) === show([4, 2]),
      show([page.element("level-select").value, page.last().overlays.tokens]));
    check("the selected storey is the plane handed to the renderer",
      page.last().doc.elevation.default === 10, show(page.last().doc.elevation));

    const tampered = replayV2();
    tampered.latest_state.round = 99;
    const beforeTamper = page.alerts.length;
    await page.drop(tampered, "tampered-v2.json");
    check("v2 rejects state whose integrity hash no longer matches",
      page.alerts.length === beforeTamper + 1
        && page.alerts[page.alerts.length - 1].indexOf("integrity.latest_state") !== -1,
      show(page.alerts.slice(beforeTamper)));

    const specialNames = replayV2();
    const namedLikeAPrototype = copy(specialNames.initial.state.combatants[0]);
    namedLikeAPrototype.name = "__proto__";
    namedLikeAPrototype.position = [25, 10];
    specialNames.initial.state.combatants.push(copy(namedLikeAPrototype));
    specialNames.initial.combatants.push(copy(namedLikeAPrototype));
    specialNames.checkpoints[0].state.combatants.push(copy(namedLikeAPrototype));
    specialNames.latest_state.combatants.push(copy(namedLikeAPrototype));
    sealReplayV2(specialNames);
    await page.drop(specialNames, "special-names-v2.json");
    check("combatant names that resemble prototype keys remain ordinary replay data",
      page.element("combatant-state").textContent.indexOf("__proto__") !== -1
        && page.last().overlays.tokens.length === 2,
      show([page.element("combatant-state").textContent, page.last().overlays.tokens]));

    for (const invalid of replayInvalidCorpus) {
      const broken = replayV2();
      setPath(broken, invalid.path, invalid.value);
      const before = page.alerts.length;
      await page.drop(broken, invalid.name + ".json");
      check("shared invalid corpus: " + invalid.name,
        page.alerts.length === before + 1
          && page.alerts[page.alerts.length - 1].indexOf(invalid.diagnostic_path) !== -1,
        show(page.alerts.slice(before)));
    }
  });

/* The viewer builds its token model twice — once from the bundle's initial
 * state and again from whatever authoritative state a scrub lands on — and the
 * two are separate assignments in separate functions. A pass-through added to
 * one of them looks entirely correct from a replay that never leaves event 0,
 * so these cases give the two frames *different* facings and read both. */
await suite("viewer.html: a replay's facing", "the page sandbox in makePage()",
  async () => {
    const page = makePage({ canvasIds: ["stage"], seed: { "embedded-data": "null" } });
    page.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    page.element("follow-level").checked = true;
    /* The markup ships this one `checked`, but the stub DOM does not parse HTML
     * — it invents an element the first time the page asks for one, and every
     * invented checkbox starts unchecked. Stated here for the same reason
     * `follow-level` is, or the cone would be suppressed by the harness and the
     * case below would fail claiming the viewer never drew one. */
    page.element("sight-cones").checked = true;

    const turning = replayV2();
    turning.initial.state.combatants[0].facing = "east";
    turning.checkpoints[0].state.combatants[0].facing = "south";
    turning.latest_state = copy(turning.checkpoints[0].state);
    sealReplayV2(turning);
    await page.drop(turning, "facing-v2.json");
    const tokenNow = () => page.last().overlays.tokens[0];
    check("the facing a bundle's initial state carries reaches the token overlay",
      tokenNow() !== undefined && tokenNow().facing === "east", show(tokenNow()));
    /* A creature's facing draws as a sight cone; the chevron this case used to
     * name is the old design, and still the feature and compass glyph. */
    check("and it is drawn, not merely carried",
      conesIn(page.last()).length === 1, show(page.last().paths));

    page.element("scrub").value = "1";
    page.element("scrub").dispatch("input");
    check("a checkpoint's facing replaces it, through the second build site",
      tokenNow() !== undefined && tokenNow().facing === "south", show(tokenNow()));

    /* Every replay written before this key existed, which is all of them: the
     * overlay must carry no facing rather than a default one, or every archived
     * fight acquires a bearing the engine never recorded. Asserted as "no path
     * in the facing ink" rather than as "no chevron", which is what it used to
     * say: this fixture's map states no compass and gives no feature a facing,
     * so anything drawn in that ink is the token's, and naming only the glyph
     * that is no longer drawn would have stopped noticing a cone entirely. */
    await page.drop(replayV2(), "no-facing-v2.json");
    check("a bundle that carries no facing hands the renderer none",
      tokenNow() !== undefined && !tokenNow().facing, show(tokenNow()));
    check("and nothing is drawn for it — no cone, no chevron",
      inkPaths(page.last()).length === 0, show(page.last().paths));

    /* The viewer's own suppression, which is a property of the page rather than
     * of the renderer: the checkbox decides what reaches the overlay. Driven
     * through a redraw the page already performs, because whether the toggle
     * itself redraws is wiring this case does not claim — what it claims is
     * that a frame drawn with the box unticked carries no cone, and that the
     * creature is still on the map without it. */
    await page.drop(turning, "facing-v2.json");
    page.element("sight-cones").checked = false;
    page.element("sight-cones").dispatch("change");
    page.element("scrub").value = "0";
    page.element("scrub").dispatch("input");
    check("a viewer with its sight cones switched off draws none",
      conesIn(page.last()).length === 0, show(page.last().paths));
    check("and the creature is still drawn without one",
      tokenNow() !== undefined && tokenNow().facing === "east"
        && discsIn(page.last()).length === 1, show(page.last().paths));
  });

await suite("viewer.html: animated playback", "the manual animation clock in makePage()",
  async () => {
    const page = makePage({
      canvasIds: ["stage"], seed: { "embedded-data": "null" }, manualAnimationFrames: true,
    });
    page.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    const moving = replayBundle([]);
    moving.map.features = [];
    moving.events = [{
      kind: "move", round: 1, turn: "Hero", actor: "Hero", target: "",
      data: { origin: [10, 10], destination: [20, 10], cost: 10 },
    }];
    await page.drop(moving, "moving.json");
    page.frame(0);
    page.element("speed").value = "1";

    const tokenAt = () => page.last().overlays.tokens[0].at;
    check("the loaded replay starts at the recorded origin",
      show(tokenAt()) === show([2, 2]), show(tokenAt()));

    page.element("btn-play").click();
    page.frame(0);
    check("a move begins on the origin instead of jumping to its result",
      show(tokenAt()) === show([2, 2]), show(tokenAt()));

    page.frame(250);
    check("halfway through the event the token is halfway between cells",
      show(tokenAt()) === show([3, 2]), show(tokenAt()));

    page.frame(500);
    check("the animation settles on the replay's deterministic folded state",
      show(tokenAt()) === show([4, 2]), show(tokenAt()));

    const chained = copy(moving);
    chained.events.push({
      kind: "move", round: 1, turn: "Hero", actor: "Hero", target: "",
      data: { origin: [20, 10], destination: [30, 10], cost: 10 },
    });
    const speedPage = makePage({
      canvasIds: ["stage"], seed: { "embedded-data": "null" }, manualAnimationFrames: true,
    });
    speedPage.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await speedPage.drop(chained, "chained.json");
    speedPage.frame(0);
    speedPage.element("speed").value = "2";
    speedPage.element("btn-play").click();
    speedPage.frame(0);
    speedPage.frame(125);
    check("the speed control scales the active transition duration",
      show(speedPage.last().overlays.tokens[0].at) === show([3, 2]),
      show(speedPage.last().overlays.tokens[0].at));
    speedPage.frame(250);
    speedPage.frame(251);
    speedPage.frame(376);
    check("playback chains the next event without jumping over its midpoint",
      show(speedPage.last().overlays.tokens[0].at) === show([5, 2]),
      show(speedPage.last().overlays.tokens[0].at));
    speedPage.frame(501);
    check("the last chained event settles and stops playback",
      show(speedPage.last().overlays.tokens[0].at) === show([6, 2])
        && speedPage.element("btn-play").textContent === "Play"
        && speedPage.element("readout").textContent.indexOf("2/2") > 0,
      show([speedPage.last().overlays.tokens[0].at,
        speedPage.element("btn-play").textContent,
        speedPage.element("readout").textContent]));

    const damaged = replayBundle([]);
    damaged.map.features = [];
    damaged.events = [{
      kind: "damage", round: 1, turn: "Hero", actor: "", target: "Hero",
      data: { amount: 6, hp: 3, max_hp: 9 },
    }];
    const damagePage = makePage({
      canvasIds: ["stage"], seed: { "embedded-data": "null" }, manualAnimationFrames: true,
    });
    damagePage.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await damagePage.drop(damaged, "damage.json");
    damagePage.frame(0);
    damagePage.element("speed").value = "1";
    damagePage.element("btn-play").click();
    damagePage.frame(0);
    check("damage starts with the hit-point ring at its previous value",
      damagePage.last().overlays.tokens[0].hpFraction === 1,
      damagePage.last().overlays.tokens[0].hpFraction);
    check("an action pulse starts transparent",
      damagePage.last().overlays.marks.length === 1
        && damagePage.last().overlays.marks[0].alpha === 0,
      show(damagePage.last().overlays.marks));
    damagePage.frame(250);
    check("damage drains the hit-point ring over the event",
      Math.abs(damagePage.last().overlays.tokens[0].hpFraction - (2 / 3)) < 0.001,
      damagePage.last().overlays.tokens[0].hpFraction);
    check("damage pulses the affected token at the event midpoint",
      damagePage.last().overlays.marks.length === 1
        && damagePage.last().overlays.marks[0].alpha > 0.4,
      show(damagePage.last().overlays.marks));
    const damagePulseColor = damagePage.last().overlays.marks[0].color;
    damagePage.frame(500);
    check("damage settles on the recorded resulting hit points",
      Math.abs(damagePage.last().overlays.tokens[0].hpFraction - (1 / 3)) < 0.001,
      damagePage.last().overlays.tokens[0].hpFraction);

    const healed = replayBundle([]);
    healed.map.features = [];
    healed.initial.creatures[0].hp = 3;
    healed.events = [{
      kind: "heal", round: 1, turn: "Hero", actor: "Hero", target: "Hero",
      data: { amount: 6, hp: 9, max_hp: 9 },
    }];
    const healPage = makePage({
      canvasIds: ["stage"], seed: { "embedded-data": "null" }, manualAnimationFrames: true,
    });
    healPage.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await healPage.drop(healed, "heal.json");
    healPage.frame(0);
    healPage.element("speed").value = "1";
    healPage.element("btn-play").click();
    healPage.frame(0);
    healPage.frame(250);
    check("healing fills the hit-point ring over the event",
      Math.abs(healPage.last().overlays.tokens[0].hpFraction - (2 / 3)) < 0.001,
      healPage.last().overlays.tokens[0].hpFraction);
    check("healing uses its own positive pulse cue",
      healPage.last().overlays.marks.length === 1
        && healPage.last().overlays.marks[0].alpha > 0.4
        && healPage.last().overlays.marks[0].color !== damagePulseColor,
      show(healPage.last().overlays.marks));

    const attacked = replayBundle([]);
    attacked.map.features = [];
    attacked.initial.creatures.push(
      { name: "Foe", team: "monsters", position: [20, 10], hp: 8, max_hp: 8 }
    );
    attacked.events = [{
      kind: "attack", round: 1, turn: "Hero", actor: "Hero", target: "Foe",
      data: { attack: "Blade", hit: true },
    }];
    const attackPage = makePage({
      canvasIds: ["stage"], seed: { "embedded-data": "null" }, manualAnimationFrames: true,
    });
    attackPage.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await attackPage.drop(attacked, "attack.json");
    attackPage.frame(0);
    attackPage.element("speed").value = "1";
    attackPage.element("btn-play").click();
    attackPage.frame(0);
    check("an attack cue begins transparent on both participants",
      attackPage.last().overlays.marks.length === 2
        && attackPage.last().overlays.marks.every((mark) => mark.alpha === 0),
      show(attackPage.last().overlays.marks));
    attackPage.frame(250);
    check("an attack cue pulses both participants at the midpoint",
      attackPage.last().overlays.marks.length === 2
        && attackPage.last().overlays.marks.every((mark) => mark.alpha > 0.4),
      show(attackPage.last().overlays.marks));

    const malformedTargets = copy(attacked);
    malformedTargets.events[0].data.targets = "Foe";
    malformedTargets.events[0].data.center = "nowhere";
    const malformedPage = makePage({
      canvasIds: ["stage"], seed: { "embedded-data": "null" }, manualAnimationFrames: true,
    });
    malformedPage.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await malformedPage.drop(malformedTargets, "malformed-targets.json");
    malformedPage.frame(0);
    malformedPage.element("speed").value = "1";
    malformedPage.element("btn-play").click();
    malformedPage.frame(0);
    malformedPage.frame(250);
    check("malformed optional cast targets cost their cues, not the animation frame",
      malformedPage.last().overlays.marks.length === 2,
      show(malformedPage.last().overlays.marks));

    const interacted = replayBundle([]);
    interacted.events = [{
      kind: "interact", round: 1, turn: "Hero", actor: "Hero", target: "",
      data: { feature: "sluice", open: false },
    }];
    const interactPage = makePage({
      canvasIds: ["stage"], seed: { "embedded-data": "null" }, manualAnimationFrames: true,
    });
    interactPage.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await interactPage.drop(interacted, "interact.json");
    interactPage.frame(0);
    interactPage.element("speed").value = "1";
    interactPage.element("btn-play").click();
    interactPage.frame(0);
    interactPage.frame(250);
    check("an interaction pulses the operated fixture",
      interactPage.last().overlays.marks.some(
        (mark) => show(mark.at) === show([4, 3]) && mark.alpha > 0.4
      ), show(interactPage.last().overlays.marks));

    const reducedPage = makePage({
      canvasIds: ["stage"], seed: { "embedded-data": "null" },
      manualAnimationFrames: true, reducedMotion: true,
    });
    reducedPage.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await reducedPage.drop(moving, "moving.json");
    reducedPage.frame(0);
    reducedPage.element("speed").value = "1";
    reducedPage.element("btn-play").click();
    reducedPage.frame(0);
    check("reduced motion applies the folded event without interpolation",
      show(reducedPage.last().overlays.tokens[0].at) === show([4, 2]),
      show(reducedPage.last().overlays.tokens[0].at));

    const pausePage = makePage({
      canvasIds: ["stage"], seed: { "embedded-data": "null" }, manualAnimationFrames: true,
    });
    pausePage.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await pausePage.drop(moving, "moving.json");
    pausePage.frame(0);
    pausePage.element("speed").value = "1";
    pausePage.element("btn-play").click();
    pausePage.frame(0);
    pausePage.frame(250);
    pausePage.element("btn-play").click();
    pausePage.frame(300);
    check("pausing settles the active event instead of leaving an in-between state",
      show(pausePage.last().overlays.tokens[0].at) === show([4, 2])
        && pausePage.element("btn-play").textContent === "Play",
      show([pausePage.last().overlays.tokens[0].at,
        pausePage.element("btn-play").textContent]));
  });

/* Every declared family, driven the same way. The bespoke suites above read
 * one family each in depth — the exact eased hit-point fraction, the exact
 * cell a move is halfway between — and they stay, because depth is what
 * catches an easing or folding defect. This loop asserts only the shallow
 * thing they cannot: that *nothing in the declaration animates nothing*. A
 * family added to viewer.html and to the fixture, but wired to no dispatch
 * branch that paints or folds, fails here rather than passing three files
 * later as a showcase event nobody can see. */
await suite("viewer.html: every declared animated family", "the page sandbox in makePage()",
  async () => {
    for (const family of animatedFamilies) {
      const bundle = replayBundle([]);
      Object.assign(bundle.initial.creatures[0], family.initial || {});
      bundle.events = [{
        kind: family.kind,
        round: 1,
        turn: "Hero",
        actor: family.event.actor || "",
        target: family.event.target || "",
        data: family.event.data || {},
      }];

      const page = makePage({
        canvasIds: ["stage"], seed: { "embedded-data": "null" },
        manualAnimationFrames: true,
      });
      page.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
      await page.drop(bundle, family.kind + ".json");
      page.frame(0);
      page.element("speed").value = "1";
      const token = () => page.last().overlays.tokens[0];
      const before = token() && show(token()[family.changes]);

      page.element("btn-play").click();
      page.frame(0);
      page.frame(250);
      if (family.pulse) {
        check(family.kind + " pulses — " + family.name,
          page.last().overlays.marks.length > 0,
          "no mark painted at the event midpoint · " + show(page.last().overlays.marks));
      }

      page.frame(500);
      if (family.changes) {
        check(family.kind + " moves the token's " + family.changes,
          token() !== undefined && show(token()[family.changes]) !== before,
          family.changes + " never moved off " + before);
      }
      if (family.becomes) {
        check(family.kind + " sets the token's " + family.becomes,
          token() !== undefined && token()[family.becomes] === true,
          family.becomes + " is " + show(token() && token()[family.becomes]));
      }
      if (family.panel) {
        const panel = page.element("combatant-state").textContent;
        check(family.kind + " reaches the state panel as " + show(family.panel),
          panel.indexOf(family.panel) !== -1,
          "state panel never showed " + show(family.panel) + " · " + show(panel));
      }
    }
  });

const VIEWER_HIDDEN = hiddenElementIds(viewerHtml);

await suite("viewer.html: the served replay list", "the page sandbox in makePage()",
  async () => {
    /* The viewer is now a page of the running service as well as a standalone
     * export, and those two lives share one file. Everything below is about
     * the seam between them: what the served page reaches for, and — the case
     * that matters most — what the exported page must never reach for.
     *
     * That negative is exactly the claim text cannot make. A grep can see that
     * a fetch sits behind `if (window.__FIVEE_EDITOR__)`; it cannot see whether
     * the boot block actually took that branch. Here the stub counts requests,
     * so "no config, no request" is an observation. */
    const listed = {
      replays: [
        { id: "gatehouse", name: "gatehouse brawl", seed: 61, events: 4, format_version: 2 },
        { id: "cellar", name: "cellar ambush", seed: 62, events: 4, format_version: 2 },
      ],
    };

    /* 1. Standalone: no injected config, so no network, at all. */
    const offline = makePage({ canvasIds: ["stage"], seed: { "embedded-data": "null" },
      hiddenIds: VIEWER_HIDDEN });
    offline.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    /* The same depth the positive cases use, and for a sharper reason: a
     * "no request was made" assertion is only worth its turn budget. Checked
     * at one turn this passed against a page that really did fetch, two turns
     * late — the exact defect the offline guarantee exists to prevent. */
    await flush();
    check("an exported page issues no request when there is no server config",
      offline.requests.length === 0, show(offline.requests));
    check("and keeps its server-only controls hidden",
      offline.element("served-replays").hidden === true
        && offline.element("link-editor").hidden === true,
      show([offline.element("served-replays").hidden,
        offline.element("link-editor").hidden]));

    /* 2. Standalone with an embedded bundle: still no network. The embedded
     *    slot wins outright, and winning must not mean "fetch anyway". */
    const embedded = makePage({
      canvasIds: ["stage"],
      seed: { "embedded-data": JSON.stringify(replayBundle(["door-1"])) },
      hiddenIds: VIEWER_HIDDEN,
    });
    embedded.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    check("an embedded replay plays without any request",
      embedded.renders.length > 0 && embedded.requests.length === 0,
      show([embedded.renders.length, embedded.requests]));

    /* 3. Served: the chooser fills from /api/replays and carries the token. */
    const served = makePage({
      canvasIds: ["stage"],
      seed: { "embedded-data": "null" },
      config: { token: "launch-token", apiBase: "/api", version: "test" },
      hiddenIds: VIEWER_HIDDEN,
    });
    served.reply = (url) => (
      url === "/api/replays"
        ? { status: 200, body: listed }
        : { status: 200, body: replayBundle(["door-1"]) }
    );
    served.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    check("a served page asks the server for its replays",
      served.requests.length === 1 && served.requests[0].url === "/api/replays",
      show(served.requests));
    check("the chooser lists every replay plus the empty choice",
      served.element("replay-select").children.length === 3,
      String(served.element("replay-select").children.length));
    check("and reveals itself and the link back to the editor",
      served.element("served-replays").hidden === false
        && served.element("link-editor").hidden === false,
      show([served.element("served-replays").hidden,
        served.element("link-editor").hidden]));
    check("the empty state names the control that just appeared",
      served.element("empty-hint").textContent.indexOf("Pick a replay above") === 0,
      served.element("empty-hint").textContent);
    check("nothing is drawn until a replay is chosen",
      served.renders.length === 0, String(served.renders.length));

    /* 4. Choosing one goes through the same loadBundle the file picker uses. */
    served.element("replay-select").value = "cellar";
    served.element("replay-select").dispatch("change");
    await flush();
    check("choosing a replay fetches it by id",
      served.requests.length === 2 && served.requests[1].url === "/api/replays/cellar",
      show(served.requests));
    check("and it reaches the canvas through the shared load path",
      served.renders.length > 0 && served.alerts.length === 0,
      show([served.renders.length, served.alerts]));

    /* 5. A refused bundle names itself. A corrupt replay is *listed* — that is
     *    deliberate, so the user can see which file is broken — so the refusal
     *    on load is the only place the name is ever said. */
    const refused = makePage({
      canvasIds: ["stage"],
      seed: { "embedded-data": "null" },
      config: { token: "launch-token", apiBase: "/api", version: "test" },
      hiddenIds: VIEWER_HIDDEN,
    });
    refused.reply = (url) => (
      url === "/api/replays"
        ? { status: 200, body: listed }
        : {
          status: 422,
          body: { detail: "broken.json is not a playable replay bundle: 1 problem(s)" },
        }
    );
    refused.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    refused.element("replay-select").value = "gatehouse";
    refused.element("replay-select").dispatch("change");
    await flush();
    check("a refused bundle reports the server's own words, not a status code",
      refused.alerts.length === 1
        && refused.alerts[0].indexOf("is not a playable replay bundle") !== -1,
      show(refused.alerts));
    check("and nothing is drawn from it",
      refused.renders.length === 0, String(refused.renders.length));

    /* 6. A server with no /api/replays leaves the page usable. The chooser is
     *    a convenience; the file picker is the guarantee. */
    const older = makePage({
      canvasIds: ["stage"],
      seed: { "embedded-data": "null" },
      config: { token: "launch-token", apiBase: "/api", version: "test" },
      hiddenIds: VIEWER_HIDDEN,
    });
    older.reply = () => ({ status: 404, body: { detail: "no route for /api/replays" } });
    older.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    check("a server without the replay route raises no alarm",
      older.alerts.length === 0, show(older.alerts));
    check("and leaves the chooser hidden rather than empty and clickable",
      older.element("served-replays").hidden === true,
      String(older.element("served-replays").hidden));
    await older.drop(replayBundle(["door-1"]), "hand-opened.json");
    check("and a hand-opened file still plays",
      older.renders.length > 0, String(older.renders.length));

    /* 7. ?replay=<id> deep-links, which is what replay_export hands back. */
    const linked = makePage({
      canvasIds: ["stage"],
      seed: { "embedded-data": "null" },
      config: { token: "launch-token", apiBase: "/api", version: "test" },
      search: "?replay=cellar",
      hiddenIds: VIEWER_HIDDEN,
    });
    linked.reply = (url) => (
      url === "/api/replays"
        ? { status: 200, body: listed }
        : { status: 200, body: replayBundle(["door-1"]) }
    );
    linked.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    check("a deep link plays its replay without a click",
      linked.requests.length === 2
        && linked.requests[1].url === "/api/replays/cellar"
        && linked.renders.length > 0,
      show([linked.requests, linked.renders.length]));
    check("and the chooser shows what is playing",
      linked.element("replay-select").value === "cellar",
      linked.element("replay-select").value);

    /* 8. A deep link naming something the server does not have is ignored
     *    rather than fetched: a stale bookmark should land on the chooser. */
    const stale = makePage({
      canvasIds: ["stage"],
      seed: { "embedded-data": "null" },
      config: { token: "launch-token", apiBase: "/api", version: "test" },
      search: "?replay=deleted-last-week",
      hiddenIds: VIEWER_HIDDEN,
    });
    stale.reply = () => ({ status: 200, body: listed });
    stale.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    check("an unknown deep link asks for nothing and draws nothing",
      stale.requests.length === 1 && stale.renders.length === 0,
      show([stale.requests, stale.renders.length]));
  });

function adventureEnvelope(chapters) {
  const envelope = {
    format: "fivee-sim-adventure-replay",
    format_version: 1,
    engine_version: "test",
    adventure: {
      id: "adv-1", name: "the sunken road", created_at: "2026-08-05T00:00:00Z",
      status: "finalized",
    },
    chapters: chapters.map((replay, index) => ({
      index,
      encounter_id: "enc-" + (index + 1),
      linked_at: "2026-08-05T00:0" + index + ":00Z",
      carried: index ? ["Hero"] : [],
      replay,
    })),
  };
  /* Composed the way the service composes it, hashes included — not because
   * the page checks them (it deliberately does not; the envelope is Python's
   * to grade) but so this fixture cannot quietly drift into a shape the real
   * exporter would never write. */
  envelope.integrity = {
    algorithm: "sha256",
    adventure: canonicalHash(envelope.adventure),
    chapters: canonicalHash(envelope.chapters),
  };
  return envelope;
}

await suite("viewer.html: an adventure's chapters", "the page sandbox in makePage()",
  async () => {
    /* An adventure's replay nests whole fights. The picker that moves between
     * them is the one viewer control that must work with *no server*: an
     * envelope is never in the served listing — `list_replays` filters on the
     * replay format — so it only ever arrives as a file or the embedded slot.
     *
     * So every case below runs with no injected config, and each one asserts
     * `requests.length === 0` alongside what it is really checking. That is the
     * claim text cannot make: a grep can see there is no fetch in the chapter
     * code, but only a run can say the boot block reached it without one. */
    const first = replayBundle(["door-1"]);
    const second = replayBundle([]);
    second.name = "the second stand";
    second.seed = 88;

    /* 1. Embedded: the first chapter plays and the picker offers them all. */
    const run = makePage({
      canvasIds: ["stage"],
      seed: { "embedded-data": JSON.stringify(adventureEnvelope([first, second])) },
      hiddenIds: VIEWER_HIDDEN,
    });
    run.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    check("an embedded adventure plays its first chapter with no request",
      run.renders.length > 0 && run.requests.length === 0 && run.alerts.length === 0,
      show([run.renders.length, run.requests, run.alerts]));
    check("and the title is the fight's, not the run's",
      run.element("title").textContent === "sluice fight",
      run.element("title").textContent);
    check("the chapter picker reveals itself, one option per chapter",
      run.element("adventure-chapters").hidden === false
        && run.element("chapter-select").children.length === 2,
      show([run.element("adventure-chapters").hidden,
        run.element("chapter-select").children.length]));
    check("and each option names the fight it will play",
      /* The length is part of the claim: `every` on an empty picker is true,
         so without it this case passes against a control with nothing in it. */
      run.element("chapter-select").children.length === 2
        && run.element("chapter-select").children.every(
        (option, index) => option.textContent.indexOf("enc-" + (index + 1)) !== -1),
      show(run.element("chapter-select").children.map((o) => o.textContent)));

    /* 2. Choosing a later chapter swaps the fight, through the shared loader. */
    const drawn = run.renders.length;
    run.element("chapter-select").value = "1";
    run.element("chapter-select").dispatch("change");
    await flush();
    check("choosing a chapter plays that fight instead",
      run.element("title").textContent === "the second stand"
        && run.element("seed").textContent === "seed 88",
      show([run.element("title").textContent, run.element("seed").textContent]));
    check("and it reached the canvas without asking a server for anything",
      run.renders.length > drawn && run.requests.length === 0,
      show([drawn, run.renders.length, run.requests]));

    /* 3. An ordinary replay must not grow a picker with nothing in it. */
    const plain = makePage({
      canvasIds: ["stage"],
      seed: { "embedded-data": JSON.stringify(replayBundle(["door-1"])) },
      hiddenIds: VIEWER_HIDDEN,
    });
    plain.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    check("a plain replay leaves the chapter picker hidden",
      plain.renders.length > 0 && plain.element("adventure-chapters").hidden === true,
      show([plain.renders.length, plain.element("adventure-chapters").hidden]));

    /* 4. A chapter that is not a playable bundle is refused by the validator
     *    this page already carries, and refused *by name* — "chapter 2" rather
     *    than one opaque complaint about the file. */
    const broken = adventureEnvelope([first, second]);
    delete broken.chapters[1].replay.events;
    const refused = makePage({
      canvasIds: ["stage"],
      seed: { "embedded-data": JSON.stringify(broken) },
      hiddenIds: VIEWER_HIDDEN,
    });
    refused.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    refused.element("chapter-select").value = "1";
    refused.element("chapter-select").dispatch("change");
    await flush();
    check("a chapter that is not a playable bundle names itself",
      refused.alerts.length === 1 && refused.alerts[0].indexOf("chapter 2") !== -1,
      show(refused.alerts));

    /* 5. An envelope with no chapters at all is a file, not a crash. */
    const empty = makePage({
      canvasIds: ["stage"],
      seed: { "embedded-data": JSON.stringify(adventureEnvelope([])) },
      hiddenIds: VIEWER_HIDDEN,
    });
    empty.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    check("an adventure with no chapters says so instead of throwing",
      empty.alerts.length === 1 && empty.renders.length === 0
        && empty.element("adventure-chapters").hidden === true,
      show([empty.alerts, empty.renders.length]));

    /* 6. The same envelope dropped as a file, which is how one actually
     *    arrives: `adventure.replay` writes a .json nobody has embedded. */
    const dropped = makePage({ canvasIds: ["stage"], seed: { "embedded-data": "null" },
      hiddenIds: VIEWER_HIDDEN });
    dropped.run(inlineScript(viewerHtml, "viewer.html", "function loadBundle("));
    await flush();
    await dropped.drop(adventureEnvelope([first, second]), "the-sunken-road-adv-1.json");
    check("a dropped adventure file opens the same way an embedded one does",
      dropped.renders.length > 0
        && dropped.element("chapter-select").children.length === 2
        && dropped.requests.length === 0,
      show([dropped.renders.length, dropped.element("chapter-select").children.length]));
  });

/* --- editor.html ---------------------------------------------------------- */

const SLUICE_MAP = {
  format: "fivee-sim-map",
  format_version: 1,
  name: "sluice hall",
  grid: { width: 10, height: 8, cell_feet: 5 },
  legend: { ".": "floor", "#": "wall", "~": "water", ":": "difficult" },
  tiles: [
    "##########", "#........#", "#........#", "#........#",
    "#........#", "#........#", "#........#", "##########",
  ],
  elevation: { default: 0, squares: [[8, 6, 10]] },
  features: [
    { id: "spawn-1", kind: "spawn", at: [1, 1] },
    { id: "door-1", kind: "door", at: [5, 0], orientation: "vertical", state: "closed" },
    {
      id: "sluice", kind: "door", at: [4, 3], orientation: "horizontal",
      state: "closed",
      terrain: { closed: "wall", open: "water" },
      elevation: { closed: 0, open: -5 },
      affects: [
        {
          cells: [[1, 4], [2, 4], [3, 4]],
          terrain: { closed: "floor", open: "water" },
          elevation: { closed: 0, open: -5 },
        },
        { cells: [[6, 4]], terrain: { closed: "difficult", open: "wall" } },
      ],
      requires: ["lever"],
      trigger: { when: { lever: "open" }, set: "open", mode: "edge" },
      costs_action: true,
      check: { ability: "strength", dc: 15 },
    },
    { id: "lever", kind: "door", at: [8, 1], orientation: "vertical", state: "open" },
  ],
  provenance: { generator: "hand", seed: 1, params: {}, edited: false, source: "test" },
};

const FLAT_MAP = {
  format: "fivee-sim-map",
  format_version: 1,
  name: "empty room",
  grid: { width: 6, height: 5, cell_feet: 5 },
  legend: { ".": "floor", "#": "wall" },
  tiles: ["######", "#....#", "#....#", "#....#", "######"],
  features: [{ id: "spawn-1", kind: "spawn", at: [1, 1] }],
  provenance: { generator: "hand", seed: 2, params: {}, edited: false, source: "test" },
};

const TOWER_MAP = {
  format: "fivee-sim-map",
  format_version: 1,
  name: "tower",
  grid: { width: 8, height: 8, cell_feet: 5 },
  legend: { ".": "floor", "#": "wall", "~": "water" },
  tiles: [
    "########", "#......#", "#......#", "#......#",
    "#......#", "#......#", "#......#", "########",
  ],
  features: [
    {
      id: "ground-sluice", kind: "door", at: [2, 2], orientation: "horizontal",
      state: "closed", terrain: { closed: "floor", open: "water" },
    },
  ],
  levels: [{
    index: 1,
    name: "gallery",
    tiles: [
      "########", "#......#", "#......#", "#......#",
      "#......#", "#......#", "#......#", "########",
    ],
    features: [{
      id: "upper-gate", kind: "door", at: [6, 6], orientation: "vertical",
      state: "closed", terrain: { closed: "wall", open: "floor" },
    }],
  }],
  provenance: { generator: "hand", seed: 3, params: {}, edited: false, source: "test" },
};

const ENVIRONMENT_MAP = {
  format: "fivee-sim-map",
  format_version: 1,
  name: "dark mill",
  grid: { width: 8, height: 8, cell_feet: 5 },
  legend: { ".": "floor", "#": "wall" },
  tiles: [
    "########", "#......#", "#......#", "#......#",
    "#......#", "#......#", "#......#", "########",
  ],
  ambient_light: "darkness",
  features: [{
    id: "roof-opening", kind: "opening", at: [3, 3],
    sight_to_levels: [1],
    light: { bright: 20, dim: 40, color: "#ffcc66" },
  }],
  levels: [{
    index: 1,
    name: "rafters",
    tiles: [
      "########", "#......#", "#......#", "#......#",
      "#......#", "#......#", "#......#", "########",
    ],
    ambient_light: "dim",
    features: [],
  }],
  provenance: { generator: "hand", seed: 6, params: {}, edited: false, source: "test" },
};

const LINKED_DOOR_MAP = {
  format: "fivee-sim-map",
  format_version: 1,
  name: "double doors",
  grid: { width: 7, height: 6, cell_feet: 5 },
  legend: { ".": "floor", "#": "wall" },
  tiles: ["#######", "#.....#", "#.....#", "#.....#", "#.....#", "#######"],
  features: [
    { id: "left", kind: "door", at: [3, 3], orientation: "horizontal", state: "closed" },
    { id: "right", kind: "door", at: [4, 3], orientation: "horizontal", state: "closed" },
  ],
  provenance: { generator: "hand", seed: 5, params: {}, edited: false, source: "test" },
};

/* The resize fixture carries a storey on purpose: the corruption path runs the
 * plane loop after snapshot(), so a throw on the second plane leaves the first
 * one rewritten and the grid still describing the old frame. */
const RESIZE_MAP = {
  format: "fivee-sim-map",
  format_version: 1,
  name: "sluice cellar",
  grid: { width: 6, height: 5, cell_feet: 5 },
  legend: { ".": "floor", "#": "wall", "~": "water" },
  tiles: ["######", "#....#", "#....#", "#....#", "######"],
  features: [
    { id: "spike", kind: "door", at: [1, 1], orientation: "vertical", state: "closed" },
    {
      id: "gate", kind: "door", at: [4, 2], orientation: "vertical",
      state: "closed", requires: ["spike"],
      affects: [{
        cells: [[2, 2], [3, 2], [5, 4]],
        terrain: { closed: "floor", open: "water" },
      }],
    },
  ],
  levels: [{
    index: 1,
    name: "cellar",
    tiles: ["######", "#....#", "#....#", "#....#", "######"],
    features: [{
      id: "hatch", kind: "door", at: [2, 3], orientation: "horizontal", state: "closed",
      affects: [{ cells: [[1, 3]], terrain: { closed: "floor", open: "water" } }],
    }],
  }],
  provenance: { generator: "hand", seed: 4, params: {}, edited: false, source: "test" },
};

function makeEditorPage() {
  const El = makeElementClass(() => null);
  const tools = ["pan", "brush", "rect", "line", "fill", "feature", "height"].map((name) => {
    const el = new El("button");
    el.dataset.tool = name;
    el.className = "tool";
    return el;
  });
  /* Two selector shapes, which is all the page uses: the tool buttons as a set
   * and one of them by name. Anything else answers empty, deliberately — a
   * page that starts selecting by a third shape should fail here, saying so,
   * rather than quietly acting on nothing. */
  const selectAll = (selector) => {
    const text = String(selector);
    const named = /\[data-tool="([^"]+)"\]/.exec(text);
    if (named) { return tools.filter((each) => each.dataset.tool === named[1]); }
    if (text.indexOf(".tool") !== -1 || text.indexOf("[data-tool]") !== -1) { return tools; }
    return [];
  };
  const page = makePage({
    canvasIds: ["map"],
    config: { token: "harness", apiBase: "/api", version: "test" },
    selectAll,
  });
  page.run(inlineScript(editorHtml, "editor.html", "function loadDocument("));
  page.rows = () => page.element("fixture-list").children;
  page.boxFor = (id) => {
    const row = page.rows().find(
      (each) => each.children[1].textContent.indexOf(id + " ") === 0
    );
    return row ? row.children[0] : null;
  };
  /* Explicit, never a flip: the rows survive a lens toggle, so a flip would
   * depend on what an earlier case left ticked. */
  page.tickBox = (id, want) => {
    const box = page.boxFor(id);
    if (!box) { throw new Error("no fixture row for " + id); }
    box.checked = want === undefined ? true : want;
    box.dispatch("change");
  };
  page.overrides = () => (page.last() || { overlays: {} }).overlays.terrainOverrides;
  /* What the Download button would write. This is the exact path the
   * half-resized document reached disk by, so it is the one the resize cases
   * read their answer from. */
  page.downloaded = () => {
    page.element("btn-download").click();
    return page.blobs[page.blobs.length - 1];
  };
  return page;
}

await suite("editor.html: the preview lens", "the page sandbox in makePage()", async () => {
  const page = makeEditorPage();

  /* 1. A fresh open draws what the document authored, and nothing else. */
  await page.drop(copy(SLUICE_MAP));
  check("a dropped document reaches the canvas", page.renders.length > 0,
    String(page.renders.length));
  check("the lens is off on a fresh open",
    page.overrides() === undefined && page.last().overlays.featureStates === undefined,
    show(page.last().overlays));
  check("the toggle is live on a map that has fixtures",
    page.element("btn-preview").disabled === false,
    String(page.element("btn-preview").disabled));

  /* 2. The list is every feature carrying a state, and only those. A kind
   *    check here would list doors and miss every lever and sluice gate. */
  const listed = page.rows().map((row) => row.children[1].textContent).join(" | ");
  check("every feature carrying a state is listed, and nothing else",
    listed === "door-1 · door | sluice · door | lever · door", listed);
  check("a box starts at the state the document authored",
    page.boxFor("sluice").checked === false && page.boxFor("lever").checked === true,
    show([page.boxFor("sluice").checked, page.boxFor("lever").checked]));

  /* 3. The lens changes what is drawn. */
  const shutFill = page.fillAt(1, 4);
  page.element("btn-preview").click();
  const authored = page.overrides();
  check("switching the lens on hands render an override map",
    authored !== undefined && authored["4,3"] === "wall" && authored["1,4"] === "floor"
      && authored["6,4"] === "difficult", show(authored));
  check("and the live-state channel with it",
    page.last().overlays.featureStates !== undefined,
    show(page.last().overlays.featureStates));

  const dryFrame = page.last();
  page.tickBox("sluice");
  const opened = page.overrides();
  check("ticking a fixture floods its own square and its overlay",
    opened["4,3"] === "water" && opened["1,4"] === "water" && opened["2,4"] === "water"
      && opened["3,4"] === "water", show(opened));
  check("and turns the wheel's square impassable, in the other direction",
    opened["6,4"] === "wall", opened["6,4"]);
  check("the square the preview paints actually changes colour",
    shutFill !== null && page.fillAt(1, 4) !== null && shutFill !== page.fillAt(1, 4),
    show([shutFill, page.fillAt(1, 4)]));
  check("a square no fixture governs is painted as before",
    page.fillAt(5, 5) === page.fillAt(5, 5, dryFrame),
    show([page.fillAt(5, 5, dryFrame), page.fillAt(5, 5)]));

  /* A door carries no terrain pair, so its preview travels on the other
   * channel — the renderer's own live-state map. */
  page.tickBox("door-1");
  check("a ticked door reaches the renderer's live-state channel",
    page.last().overlays.featureStates["door-1"] === true,
    show(page.last().overlays.featureStates));
  check("a fixture nobody ticked is left to the document",
    page.last().overlays.featureStates.lever === undefined,
    show(page.last().overlays.featureStates));

  /* 4. And changes nothing else. The lens is drawing only: nothing in it may
   *    write to doc, call snapshot(), or move the dirty baseline. */
  check("the document the Download button writes is byte-identical to the one opened",
    page.downloaded() === JSON.stringify(SLUICE_MAP, null, 2) + "\n",
    String(page.downloaded()).slice(0, 200));
  page.reply = () => ({ status: 200, body: { provenance: {}, sha256: "x", warnings: [] } });
  page.element("btn-save").click();
  await settle();
  const put = page.requests.filter((each) => each.method === "PUT").pop();
  check("a save after previewing does not stamp the map as edited",
    put !== undefined && put.body.provenance.edited === false,
    show(put && put.body.provenance));

  /* 5. The documented limit: the Heights overlay reads doc.elevation, so a
   *    fixture's height override never reaches the wash even though its
   *    terrain reaches the fill. Pinned so it stays a known limit. */
  const litMarks = show(page.last().overlays.marks);
  page.element("btn-preview").click();
  check("the relief overlay is identical with the flood previewed and without",
    show(page.last().overlays.marks) === litMarks, litMarks.slice(0, 200));
  const covers = (marks, x, y) => (marks || []).some(
    (mark) => mark.at && mark.at[0] <= x && x < mark.at[0] + (mark.w || 1)
      && mark.at[1] <= y && y < mark.at[1] + (mark.h || 1)
  );
  check("nothing shades the flooded room, which is the limit to document",
    !covers(page.last().overlays.marks, 1, 4), show(page.last().overlays.marks));
  check("switching the lens off takes both channels away",
    page.overrides() === undefined && page.last().overlays.featureStates === undefined,
    show(page.last().overlays));

  /* 6. loadDocument resets it — the layer checklist's third entry. Two maps'
   *    ids may well collide, so a lens left set would not even look wrong. */
  page.element("btn-preview").click();
  page.tickBox("sluice", true);
  check("the lens comes back on with the ticks it had",
    page.overrides()["1,4"] === "water", show(page.overrides()));
  await page.drop(copy(SLUICE_MAP));
  check("reopening a map switches the lens off", page.overrides() === undefined,
    show(page.overrides()));
  page.element("btn-preview").click();
  check("and forgets which fixtures were ticked",
    page.overrides()["1,4"] === "floor" && page.overrides()["4,3"] === "wall",
    show(page.overrides()));
  check("the boxes are back at the authored states",
    page.boxFor("sluice").checked === false, String(page.boxFor("sluice").checked));

  /* 7. A map with no fixtures stands the control down. */
  await page.drop(copy(FLAT_MAP));
  check("a map with no fixtures disables the toggle",
    page.element("btn-preview").disabled === true,
    String(page.element("btn-preview").disabled));
  check("and says so where the list was",
    page.element("fixture-list").textContent === "no fixtures on this level",
    page.element("fixture-list").textContent);
  page.element("btn-preview").click();
  check("a disabled toggle does nothing at all", page.overrides() === undefined,
    show(page.overrides()));

  /* 8. The preview is the storey being drawn, not the ground. */
  await page.drop(copy(TOWER_MAP));
  page.element("btn-preview").click();
  check("on the ground, the ground's fixture decides",
    page.overrides()["2,2"] === "floor" && page.overrides()["6,6"] === undefined,
    show(page.overrides()));
  page.element("level-select").value = "1";
  page.element("level-select").dispatch("change");
  check("upstairs, only that storey's own fixture decides",
    page.overrides()["6,6"] === "wall" && page.overrides()["2,2"] === undefined,
    show(page.overrides()));
  const upstairs = page.rows().map((row) => row.children[1].textContent).join(" | ");
  check("and the list moved with it", upstairs === "upper-gate · door", upstairs);
  page.tickBox("upper-gate");
  check("a storey's fixture previews from its own plane",
    page.overrides()["6,6"] === "floor", show(page.overrides()));
  page.element("level-select").value = "0";
  page.element("level-select").dispatch("change");
  check("and coming back down, the ground's does again",
    page.overrides()["2,2"] === "floor" && page.overrides()["6,6"] === undefined,
    show(page.overrides()));
});

await suite("editor.html: the inspector", "the page sandbox in makePage()", async () => {
  const page = makeEditorPage();
  await page.drop(copy(SLUICE_MAP));

  /* Selecting is a click that does not pan: pointerdown then pointerup on the
   * same square, which is how the page picks a feature. */
  const canvas = page.element("map");
  const view = page.last().view;
  const atCell = (x, y) => ({
    clientX: (x - view.x) * view.scale + view.scale / 2,
    clientY: (y - view.y) * view.scale + view.scale / 2,
    button: 0, pointerId: 1,
  });
  const select = (x, y) => {
    canvas.dispatch("pointerdown", atCell(x, y));
    canvas.dispatch("pointerup", atCell(x, y));
    return page.element("feature-info").textContent;
  };

  const expected = [
    "id: sluice", "kind: door", "at: [4,3]", "orientation: horizontal",
    "state: closed", "terrain: wall → water", "elevation: 0 → -5 ft",
    "affects: 2 group(s), 4 square(s)", "requires: lever",
    "trigger: edge → open when lever=open",
    "costs_action: yes", "check: strength DC 15",
  ].join("\n");
  const info = select(4, 3);
  check("the inspector names every fixture key the record carries",
    info === expected, show(info) + "\n wanted \n" + show(expected));
  check("a bare door still shows only what it carries",
    select(5, 0) === "id: door-1\nkind: door\nat: [5,0]\norientation: vertical\n"
      + "state: closed",
    show(select(5, 0)));

  /* A hand-opened file may carry anything in those fields. */
  const hostile = copy(SLUICE_MAP);
  hostile.features[2].affects = "nope";
  hostile.features[2].requires = 7;
  hostile.features[2].terrain = "wall";
  hostile.features[2].check = "str 15";
  hostile.features[2].costs_action = false;
  await page.drop(hostile);
  let threw = null;
  try {
    select(4, 3);
    page.element("btn-preview").click();
    page.tickBox("sluice");
  } catch (error) { threw = error.message; }
  check("a malformed fixture costs its own lines, not the page", threw === null,
    "threw: " + threw);
  const partial = page.element("feature-info").textContent;
  check("and the inspector still reports the rest of the record",
    partial.indexOf("costs_action: no") !== -1 && partial.indexOf("affects:") === -1,
    show(partial));
});

await suite("editor.html: environment authoring", "the page sandbox in makePage()", async () => {
  const page = makeEditorPage();
  await page.drop(copy(ENVIRONMENT_MAP));

  check("a loaded plane selects its authored ambient light",
    page.element("ambient-light").value === "darkness",
    page.element("ambient-light").value);
  const wash = page.last().fills.filter(
    (fill) => fill[4] === "#07111f" && Math.abs(fill[5] - 0.38) < 0.001
  );
  check("darkness reaches the renderer as the stronger ambient wash",
    wash.length === 1, show(wash));

  page.element("ambient-light").value = "dim";
  page.element("ambient-light").dispatch("change");
  let saved = JSON.parse(page.downloaded());
  check("changing ambient light writes the active plane",
    saved.ambient_light === "dim", show(saved.ambient_light));
  page.element("btn-undo").click();
  saved = JSON.parse(page.downloaded());
  check("ambient light participates in undo",
    saved.ambient_light === "darkness", show(saved.ambient_light));

  const canvas = page.element("map");
  const view = page.last().view;
  const point = {
    clientX: (3 - view.x) * view.scale + view.scale / 2,
    clientY: (3 - view.y) * view.scale + view.scale / 2,
    button: 0, pointerId: 1,
  };
  canvas.dispatch("pointerdown", point);
  canvas.dispatch("pointerup", point);
  check("selecting an opening reveals its environment controls",
    page.element("feature-config").hidden === false,
    String(page.element("feature-config").hidden));
  check("the controls read authored visibility and light",
    page.element("feature-sight-levels").value === "1"
      && page.element("feature-light-bright").value === 20
      && page.element("feature-light-dim").value === 40
      && page.element("feature-light-color").value === "#ffcc66",
    show([
      page.element("feature-sight-levels").value,
      page.element("feature-light-bright").value,
      page.element("feature-light-dim").value,
      page.element("feature-light-color").value,
    ]));

  page.element("feature-sight-levels").value = "1, 0, 1";
  page.element("feature-light-bright").value = "30";
  page.element("feature-light-dim").value = "60";
  page.element("feature-light-color").value = "#66ccff";
  page.element("feature-light-color").dispatch("change");
  saved = JSON.parse(page.downloaded());
  const opening = saved.features.find((feature) => feature.id === "roof-opening");
  check("visibility is de-duplicated and light edits round-trip",
    show(opening.sight_to_levels) === "[0,1]"
      && show(opening.light) === '{"bright":30,"dim":60,"color":"#66ccff"}',
    show(opening));

  page.element("level-select").value = "1";
  page.element("level-select").dispatch("change");
  check("changing storeys selects that plane's ambient light",
    page.element("ambient-light").value === "dim",
    page.element("ambient-light").value);
  page.element("ambient-light").value = "bright";
  page.element("ambient-light").dispatch("change");
  saved = JSON.parse(page.downloaded());
  check("bright is canonicalised by omitting only the active plane's key",
    saved.ambient_light === "darkness"
      && saved.levels[0].ambient_light === undefined,
    show([saved.ambient_light, saved.levels[0].ambient_light]));
});

await suite("editor.html: door configuration", "the page sandbox in makePage()", async () => {
  const page = makeEditorPage();
  const select = (x, y) => {
    const view = page.last().view;
    const event = {
      clientX: (x - view.x) * view.scale + view.scale / 2,
      clientY: (y - view.y) * view.scale + view.scale / 2,
      button: 0, pointerId: 1,
    };
    page.element("map").dispatch("pointerdown", event);
    page.element("map").dispatch("pointerup", event);
  };
  const saved = () => JSON.parse(page.downloaded());
  const feature = (document_, id) => document_.features.find((each) => each.id === id);

  await page.drop(copy(SLUICE_MAP));
  select(5, 0);
  check("selecting a door reveals its configuration controls",
    page.element("door-config").hidden === false,
    String(page.element("door-config").hidden));
  check("omitted metadata is shown as the compatible historical defaults",
    page.element("door-orientation").value === "vertical"
      && page.element("door-hinge").value === "north"
      && page.element("door-swing").value === "west",
    show([page.element("door-orientation").value, page.element("door-hinge").value,
      page.element("door-swing").value]));
  page.element("door-orientation").value = "horizontal";
  page.element("door-orientation").dispatch("change");
  page.element("door-hinge").value = "east";
  page.element("door-hinge").dispatch("change");
  page.element("door-swing").value = "south";
  page.element("door-swing").dispatch("change");
  const configured = feature(saved(), "door-1");
  check("orientation, hinge, and swing choices are written to the document",
    configured.orientation === "horizontal" && configured.hinge === "east"
      && configured.swing === "south", show(configured));

  await page.drop(copy(LINKED_DOOR_MAP));
  select(3, 3);
  const options = page.element("door-linked").children.map((each) => each.value);
  check("the link control offers the adjacent compatible door",
    options.indexOf("right") !== -1, show(options));
  page.element("door-linked").value = "right";
  page.element("door-linked").dispatch("change");
  const linked = saved();
  check("linking writes one reciprocal pair with outer hinges",
    feature(linked, "left").linked_to === "right"
      && feature(linked, "right").linked_to === "left"
      && feature(linked, "left").hinge === "west"
      && feature(linked, "right").hinge === "east",
    show(linked.features));
  page.tickBox("left", true);
  check("previewing either linked leaf opens both",
    page.boxFor("left").checked === true && page.boxFor("right").checked === true,
    show(page.last().overlays.featureStates));
  page.element("btn-delete-feature").click();
  const deleted = saved();
  check("deleting either linked leaf removes the reciprocal pair",
    feature(deleted, "left") === undefined && feature(deleted, "right") === undefined,
    show(deleted.features));
  await page.drop(copy(linked));
  select(3, 3);
  page.element("door-linked").value = "";
  page.element("door-linked").dispatch("change");
  const unlinked = saved();
  check("unlinking clears both sides atomically",
    feature(unlinked, "left").linked_to === undefined
      && feature(unlinked, "right").linked_to === undefined,
    show(unlinked.features));
});

/* The editor's half of the same vocabulary: a feature's facing and the
 * document's compass, both authored through selects and both reaching the
 * canvas through the renderer above. The interesting claims are the two the
 * format imposes rather than the page — that a door is offered no facing at
 * all, because the format refuses the key on one, and that north is written by
 * being left out. */
const FACING_MAP = {
  format: "fivee-sim-map",
  format_version: 1,
  name: "arrow slit",
  grid: { width: 6, height: 5, cell_feet: 5 },
  legend: { ".": "floor", "#": "wall" },
  tiles: ["######", "#....#", "#....#", "#....#", "######"],
  features: [
    { id: "slit", kind: "opening", at: [2, 2], facing: "east" },
    { id: "gate", kind: "door", at: [4, 1], orientation: "vertical", state: "closed" },
  ],
  provenance: { generator: "hand", seed: 7, params: {}, edited: false, source: "test" },
};

await suite("editor.html: facing and the compass", "the page sandbox in makePage()",
  async () => {
    const page = makeEditorPage();
    const select = (x, y) => {
      const view = page.last().view;
      const event = {
        clientX: (x - view.x) * view.scale + view.scale / 2,
        clientY: (y - view.y) * view.scale + view.scale / 2,
        button: 0, pointerId: 1,
      };
      page.element("map").dispatch("pointerdown", event);
      page.element("map").dispatch("pointerup", event);
    };
    const saved = () => JSON.parse(page.downloaded());
    const featureNamed = (id) => saved().features.find((each) => each.id === id);
    /* The editor fits the view to the map, so its scale and origin are the
     * page's business rather than this file's — a cell's centre has to be
     * computed from the frame that was actually drawn. */
    const centreOf = (x, y) => {
      const view = page.last().view;
      return [(x + 0.5 - view.x) * view.scale, (y + 0.5 - view.y) * view.scale];
    };

    await page.drop(copy(FACING_MAP));
    check("an authored facing reaches the canvas on a fresh open",
      chevronsIn(page.last()).length === 1, show(page.last().paths));

    select(2, 2);
    check("selecting a feature reveals the facing control at its authored value",
      page.element("facing-config").hidden === false
        && page.element("feature-facing").value === "east",
      show([page.element("facing-config").hidden, page.element("feature-facing").value]));
    check("and the inspector says so in the document's own key order",
      page.element("feature-info").textContent
        === "id: slit\nkind: opening\nat: [2,2]\nfacing: east",
      show(page.element("feature-info").textContent));

    /* The one refusal the format imposes on this control: a door already says
     * where it points three ways over, so it is offered no fourth answer. */
    select(4, 1);
    check("selecting a door hides the facing control entirely",
      page.element("facing-config").hidden === true
        && page.element("door-config").hidden === false,
      show([page.element("facing-config").hidden, page.element("door-config").hidden]));

    select(2, 2);
    page.element("feature-facing").value = "northwest";
    page.element("feature-facing").dispatch("change");
    const turned = chevronsIn(page.last());
    check("choosing a facing writes it to the document and turns the chevron",
      featureNamed("slit").facing === "northwest" && turned.length === 1
        && tipOf(turned[0])[0] < centreOf(2, 2)[0]
        && tipOf(turned[0])[1] < centreOf(2, 2)[1],
      show([featureNamed("slit"), turned.map(tipOf), centreOf(2, 2)]));
    page.element("btn-undo").click();
    check("and it participates in undo", featureNamed("slit").facing === "east",
      show(featureNamed("slit")));

    select(2, 2);
    page.element("feature-facing").value = "";
    page.element("feature-facing").dispatch("change");
    check("clearing it omits the key rather than writing an empty one",
      Object.prototype.hasOwnProperty.call(featureNamed("slit"), "facing") === false,
      show(featureNamed("slit")));
    check("and the chevron goes with it",
      chevronsIn(page.last()).length === 0, show(page.last().paths));

    /* The compass is document-wide and canonicalised by omission, the way
     * `bright` ambient light is: north is what a file that says nothing means,
     * so writing it back would make the document differ from the one the server
     * would have written. */
    await page.drop(copy(FACING_MAP));
    check("a document with no compass selects north and draws no rose",
      page.element("map-compass").value === "north"
        && inkPaths(page.last()).length === 1,
      show([page.element("map-compass").value, page.last().paths]));
    page.element("map-compass").value = "east";
    page.element("map-compass").dispatch("change");
    check("choosing a compass writes it and draws the rose",
      saved().compass === "east" && inkPaths(page.last()).length === 3,
      show([saved().compass, inkPaths(page.last())]));
    page.element("map-compass").value = "north";
    page.element("map-compass").dispatch("change");
    check("choosing north takes the key back out of the document",
      Object.prototype.hasOwnProperty.call(saved(), "compass") === false,
      show(saved().compass));
    page.element("btn-undo").click();
    check("and the compass participates in undo too", saved().compass === "east",
      show(saved().compass));
  });

await suite("editor.html: the resize dialog", "the page sandbox in makePage()", async () => {
  const page = makeEditorPage();

  const resize = async (document_, width, height, anchor) => {
    await page.drop(copy(document_));
    page.element("rs-width").value = String(width);
    page.element("rs-height").value = String(height);
    page.element("rs-anchor").value = anchor;
    page.element("rs-fill").value = "wall";
    page.element("dlg-resize-go").click();
    return JSON.parse(page.downloaded());
  };
  const featureNamed = (doc, id) => {
    const planes = [doc].concat(doc.levels || []);
    for (const plane of planes) {
      const found = (plane.features || []).find((each) => each.id === id);
      if (found) { return found; }
    }
    return null;
  };
  /* The half-resize is not a throw, it is a document: the grid still describes
   * the old frame while some planes already carry the new one. Nothing on the
   * Download path would notice, so the check has to be here. */
  const inconsistency = (doc) => {
    const planes = [doc].concat(doc.levels || []);
    for (const plane of planes) {
      if (plane.tiles.length !== doc.grid.height) {
        return (plane.name || "ground") + " has " + plane.tiles.length
          + " rows, grid says " + doc.grid.height;
      }
      const wrong = plane.tiles.find((row) => row.length !== doc.grid.width);
      if (wrong !== undefined) {
        return (plane.name || "ground") + " has a row of " + wrong.length
          + ", grid says " + doc.grid.width;
      }
    }
    return null;
  };

  /* 1. The anchor moves everything the document locates by coordinate — and a
   *    fixture's overlay cells are coordinates nested inside a record. */
  const grown = await resize(RESIZE_MAP, 8, 7, "bottom-right");
  check("a bottom-right grow moves the fixture",
    show(featureNamed(grown, "gate").at) === "[6,4]",
    show(featureNamed(grown, "gate").at));
  check("and moves its overlay cells with it",
    show(featureNamed(grown, "gate").affects[0].cells) === "[[4,4],[5,4],[7,6]]",
    show(featureNamed(grown, "gate").affects[0].cells));
  check("and a storey's overlay cells too, not just the ground's",
    show(featureNamed(grown, "hatch").affects[0].cells) === "[[3,5]]",
    show(featureNamed(grown, "hatch").affects[0].cells));
  check("the grown document is internally consistent",
    inconsistency(grown) === null, inconsistency(grown));

  const put = await resize(RESIZE_MAP, 8, 7, "top-left");
  check("a top-left grow leaves the coordinates put",
    show(featureNamed(put, "gate").affects[0].cells) === "[[2,2],[3,2],[5,4]]",
    show(featureNamed(put, "gate").affects[0].cells));

  /* 2. Shrinking drops what falls outside, cell by cell. */
  const shrunk = await resize(RESIZE_MAP, 5, 5, "top-left");
  check("a shrink crops the overlay cells that fall outside",
    show(featureNamed(shrunk, "gate").affects[0].cells) === "[[2,2],[3,2]]",
    show(featureNamed(shrunk, "gate").affects[0].cells));
  check("the shrunk document is internally consistent",
    inconsistency(shrunk) === null, inconsistency(shrunk));

  /* 3. A fixture another one requires cannot be dropped: the document would
   *    stop parsing, and the save would fail naming the missing prerequisite
   *    rather than the resize that removed it. */
  const refused = await resize(RESIZE_MAP, 3, 3, "bottom-right");
  check("dropping a required fixture is refused, naming both ends",
    page.element("status").textContent.indexOf("requires it; move or remove it first") !== -1
      && page.element("status").textContent.indexOf("spike") !== -1,
    show(page.element("status").textContent));
  check("and the refusal changes nothing at all",
    JSON.stringify(refused) === JSON.stringify(RESIZE_MAP),
    "the document moved under a refusal");

  const triggered = copy(RESIZE_MAP);
  delete featureNamed(triggered, "gate").requires;
  featureNamed(triggered, "gate").trigger = {
    when: { spike: "open" }, set: "open", mode: "maintained",
  };
  const triggerRefused = await resize(triggered, 3, 3, "bottom-right");
  check("dropping a trigger dependency is refused, naming both ends",
    page.element("status").textContent.indexOf("trigger that observes it") !== -1
      && page.element("status").textContent.indexOf("spike") !== -1,
    show(page.element("status").textContent));
  check("and a trigger-dependency refusal changes nothing",
    JSON.stringify(triggerRefused) === JSON.stringify(triggered),
    "the trigger document moved under a refusal");

  /* 4. A linked door pair is another indivisible reference: keeping one leaf
   *    while cropping its mate would leave a document that cannot be loaded. */
  const linked = copy(LINKED_DOOR_MAP);
  featureNamed(linked, "left").linked_to = "right";
  featureNamed(linked, "right").linked_to = "left";
  const linkedRefused = await resize(linked, 4, 6, "top-left");
  check("dropping one linked door leaf is refused, naming both ends",
    page.element("status").textContent.indexOf("linked door 'right'") !== -1
      && page.element("status").textContent.indexOf("'left' survives") !== -1,
    show(page.element("status").textContent));
  check("and a linked-pair resize refusal changes nothing",
    JSON.stringify(linkedRefused) === JSON.stringify(linked),
    "the linked document moved under a refusal");

  /* 5. The corruption path. Every one of these shapes reaches the plane loop,
   *    which runs after snapshot() and rewrites planes in order — so a throw
   *    leaves a half-resized document that the Download button writes out
   *    without the server ever seeing it. */
  const hostile = [
    ["a null entry in affects", (d) => { d.features[1].affects = [null]; }],
    ["an affects entry that is a string", (d) => { d.features[1].affects = ["nope"]; }],
    ["cells that are not an array", (d) => { d.features[1].affects = [{ cells: "1,2" }]; }],
    ["string coordinates", (d) => { d.features[1].affects = [{ cells: [["5", 3]] }]; }],
    ["requires as a bare string", (d) => { d.features[1].requires = "spike"; }],
    ["trigger when as a bare string",
      (d) => { d.features[1].trigger = { when: "spike", set: "open", mode: "edge" }; }],
    ["a malformed overlay on a storey, not the ground",
      (d) => { d.levels[0].features[0].affects = [{ cells: [[1]] }]; }],
    ["a __proto__ key on a feature record", (d) => {
      d.features[1] = JSON.parse('{"id":"gate","kind":"door","at":[4,2],"state":"closed",'
        + '"__proto__":{"polluted":1}}');
    }],
  ];
  for (const [label, wreck] of hostile) {
    const wrecked = copy(RESIZE_MAP);
    wreck(wrecked);
    let after = null;
    let threw = null;
    try { after = await resize(wrecked, 8, 7, "bottom-right"); } catch (e) { threw = e.message; }
    check("a resize survives " + label, threw === null, "threw: " + threw);
    check("and writes a whole document to disk despite " + label,
      after !== null && after.grid.width === 8 && inconsistency(after) === null,
      after === null ? "nothing reached the Download path" : inconsistency(after));
  }
  check("no prototype was polluted along the way", ({}).polluted === undefined,
    "Object.prototype carries `polluted`");
});

/* --- home.html ------------------------------------------------------------ */

const HOME_HIDDEN = hiddenElementIds(homeHtml);

/* The operation index this page is handed. Deliberately *not* in alphabetical
 * order, and deliberately not the real one: the page must reproduce whatever
 * order the server listed — the route table's order is curated — so a group
 * sort would show up here as `alpha` first. Fake names for the same reason the
 * text contract forbids real ones in the markup: nothing about this page is
 * allowed to know what the engine's operations are called. */
const OPERATION_INDEX = {
  version: "test",
  base: "/api",
  openapi: "/api/openapi.json",
  /* Deliberately disagreeing with `operations.length`. The page renders the
   * list it was given, so its own tally must come from that list — with a
   * truthful count here, "4 operations" would pass just as well against a page
   * that echoed this field, and the case below would prove nothing. */
  count: 99,
  operations: [
    { operation: "server.ping", method: "GET", path: "/api/ping", summary: "Liveness." },
    { operation: "server.operations", method: "GET", path: "/api/operations",
      summary: "This index." },
    { operation: "zebra.stripe", method: "POST", path: "/api/zebras", summary: "Stripe one." },
    { operation: "alpha.first", method: "GET", path: "/api/alphas", summary: "First." },
  ],
};

const homePage = (config, reply) => {
  const page = makePage({ config, hiddenIds: HOME_HIDDEN });
  if (reply) { page.reply = reply; }
  return page;
};
const groupsOf = (page) => page.element("operations").children.map((group) => ({
  name: group.children[0].textContent,
  rows: group.children[1].children.map((row) => row.children.map((td) => td.textContent)),
}));

await suite("home.html: the landing page", "the page sandbox in makePage()", async () => {
  /* 1. Served: one request, carrying the launch token, and the answer becomes
   *    the page. The token assertion is the one a grep cannot make — the
   *    header is read off what the stub was actually called with. */
  let sentHeaders = null;
  const served = homePage(
    { token: "launch-token", apiBase: "/api", version: "2026.8.99" },
    (url, init) => { sentHeaders = (init || {}).headers || null; return { status: 200, body: OPERATION_INDEX }; }
  );
  served.run(inlineScript(homeHtml, "home.html", "function loadOperations("));
  await flush();

  check("the landing page asks the server for its operation index, once",
    served.requests.length === 1 && served.requests[0].url === "/api/operations",
    show(served.requests));
  check("and sends the launch token with it",
    sentHeaders !== null && sentHeaders["X-Fivee-Editor-Token"] === "launch-token",
    show(sentHeaders));
  check("the version shown is the one this launch injected, not a baked-in string",
    served.element("engine-version").textContent === "engine 2026.8.99",
    served.element("engine-version").textContent);

  const groups = groupsOf(served);
  check("every operation is rendered, grouped by its prefix",
    groups.length === 3
      && groups.reduce((total, group) => total + group.rows.length, 0) === 4,
    show(groups.map((group) => [group.name, group.rows.length])));
  check("in the order the server listed them, not sorted",
    groups.map((group) => group.name).join(",") === "server,zebra,alpha",
    show(groups.map((group) => group.name)));
  check("each row carries the operation, its request line, and its summary",
    show(groups[1].rows[0]) === show(["zebra.stripe", "POST /api/zebras", "Stripe one."]),
    show(groups[1].rows[0]));
  check("the count comes from the list rendered, not from the payload's own count",
    served.element("ops-count").textContent === "4 operations",
    served.element("ops-count").textContent + " (the payload claims 99)");
  check("and the loading line is gone once there is an index to show",
    served.element("ops-status").hidden === true,
    show([served.element("ops-status").hidden, served.element("ops-status").textContent]));
  check("the OpenAPI link is revealed and built from the injected api base",
    served.element("link-openapi").hidden === false
      && served.element("link-openapi").href === "/api/openapi.json",
    show([served.element("link-openapi").hidden, served.element("link-openapi").href]));

  /* 2. A refusal reports the server's own words. A status code alone would not
   *    tell the user that the token is what is wrong. */
  const refused = homePage(
    { token: "stale", apiBase: "/api", version: "test" },
    () => ({ status: 401, body: { detail: "missing or invalid editor token" } })
  );
  refused.run(inlineScript(homeHtml, "home.html", "function loadOperations("));
  await flush();
  check("a refused index reports the server's detail, not just its status",
    refused.element("ops-status").className === "error"
      && refused.element("ops-status").textContent.indexOf(
        "missing or invalid editor token") !== -1,
    refused.element("ops-status").textContent);

  /* 3. The server stopped underneath the page. A network-level rejection has
   *    to land as a status line; unhandled, it would leave "Loading…" on
   *    screen with the reason only in a console nobody has open. */
  const dropped = homePage({ token: "launch-token", apiBase: "/api", version: "test" });
  dropped.sandbox.fetch = () => Promise.reject(new Error("connection refused"));
  dropped.run(inlineScript(homeHtml, "home.html", "function loadOperations("));
  await flush();
  check("a dropped connection becomes a status line rather than an unhandled rejection",
    dropped.element("ops-status").className === "error"
      && dropped.element("ops-status").textContent.indexOf("not answering") !== -1,
    dropped.element("ops-status").textContent);
});

/* Deliberately its own suite rather than a fourth case in the one above.
 * Deleting the page's config gate makes it read through a null CONFIG and
 * *throw*, and a suite that throws takes its remaining cases with it — so as a
 * trailing case the guard's own assertion would never run, and the mutation
 * would be reported against whichever case happened to be next. Alone, the
 * failure names the guard that was removed. */
await suite("home.html: opened without a server", "the page sandbox in makePage()", async () => {
  /* The page still exists offline — both page links are plain markup — but it
   * must not reach the network, and it must say why the index is missing
   * rather than sitting on "Loading…" forever. */
  const offline = homePage(null);
  offline.run(inlineScript(homeHtml, "home.html", "function loadOperations("));
  await flush();
  check("with no injected config the page issues no request at all",
    offline.requests.length === 0, show(offline.requests));
  check("and says why the index is absent instead of loading forever",
    offline.element("ops-status").className === "error"
      && offline.element("ops-status").textContent.indexOf("fivee serve") !== -1,
    show([offline.element("ops-status").className,
      offline.element("ops-status").textContent]));
  check("and leaves the OpenAPI link hidden, because there is nothing to link to",
    offline.element("link-openapi").hidden === true,
    show(offline.element("link-openapi").hidden));
  check("nothing is rendered into the operation list",
    offline.element("operations").children.length === 0,
    show(offline.element("operations").children.length));
});

/* --- totals --------------------------------------------------------------- */

/* Anything a page threw that no settle() collected. A silent one would leave
 * the run green with a stack printed above the totals. */
await settle().catch((error) => {
  report(false, "a page threw after its case had finished", explain(error, "the page sandbox"));
});
escaped.forEach((error) => {
  report(false, "a page threw after its case had finished", explain(error, "the page sandbox"));
});

console.log("\n" + passed + " passed, " + failed + " failed");
process.exit(failed ? 1 : 0);
