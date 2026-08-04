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
const replayInvalidCorpus = JSON.parse(readFileSync(path.join(
  REPO_ROOT, "souroldgeezer-fivee-sim", "engine", "tests", "fixtures",
  "replay-invalid.json"
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
  "door-linked",
];
const VIEWER_IDS = [
  "stage", "scrub", "ticker", "readout", "title", "seed", "empty-note",
  "btn-play", "btn-back", "btn-forward", "speed", "embedded-data", "level-select",
  "follow-level", "combatant-state",
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

/* The fake 2D context. Every drawing call is a no-op except fillRect, which is
 * how "the picture changed" is observed: the colour a square was painted, and
 * the alpha it was painted at.
 *
 * Alpha is recorded because the context outlives the frame. A glyph that sets
 * globalAlpha inside a save()/restore() pair and loses the restore leaves every
 * later fill — this frame and every frame after it, since render() never resets
 * alpha — painted at the value it borrowed. Reading fillStyle alone cannot see
 * that: the colour string is unchanged and the whole map just goes faint. */
function makeContext(fills) {
  const state = {
    fillStyle: "", strokeStyle: "", lineWidth: 1, font: "",
    textAlign: "", textBaseline: "", globalAlpha: 1, lineCap: "", lineJoin: "",
    canvas: null,
  };
  const saved = [];
  return new Proxy(state, {
    get(target, prop) {
      if (prop === "fillRect") {
        return (x, y, w, h) => fills.push([x, y, w, h, target.fillStyle, target.globalAlpha]);
      }
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
  const context = makeContext(fills);
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
  context.canvas = element(options.canvasIds[0]);

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
    fills, context, element, elements, documentStub, requests, blobs, alerts,
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
    realRender(ctx, doc, view, overlays);
    page.renders.push({ doc, view, overlays, fills: fills.slice() });
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
 * The swing arc drawn beside it is not: `arc` and `stroke` are no-ops here, and
 * no case below pretends otherwise. */

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
