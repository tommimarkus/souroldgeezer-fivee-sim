/* The shared canvas renderer for the map editor and the replay viewer.

   One global namespace, FiveeRenderer, and nothing else. Pure functions over
   a map document payload plus a small view state {x, y, scale, width, height}:
   x and y are the world cell coordinates at the canvas's top-left corner
   (fractional while panning), scale is pixels per cell, width and height are
   the canvas size in CSS pixels.

   Terrain colors resolve in four steps, first hit wins:

     1. the document's own palette — doc.palette[kind], either one color or a
        {light, dark} pair — because a color the map author wrote down beats
        every color anyone computed for them;
     2. a CSS custom property (--terrain-<kind>, with any character outside
        [a-z0-9] in the kind mapped to "-") read off the canvas at draw time,
        so the pages theme the bundled kinds with prefers-color-scheme;
     3. a fixed fallback, which every bundled kind has;
     4. a deterministic color hashed from the kind's name — so an unknown or
        pack-defined kind is at least the same color in every session.

   Steps 2-4 need no configuration, which is the point: a map that says nothing
   about color still draws.

   No network, no fonts, no external references: everything here is drawn. */
"use strict";
var FiveeRenderer = (function () {
  /* --- palette ---------------------------------------------------------- */
  var FALLBACK = {
    /* kind: [light, dark] */
    "normal": ["#e9e4d8", "#31353b"],
    "floor": ["#e9e4d8", "#31353b"],
    "wall": ["#4d463c", "#14161a"],
    "difficult": ["#dcd3bd", "#383b34"],
    "half-cover": ["#ddd8c6", "#343a3c"],
    "three-quarters-cover": ["#d3cdb8", "#383f42"],
    "door-open": ["#e2d3b4", "#403a2c"],
    "door-closed": ["#8a6f4d", "#3d3020"],
    "water": ["#a9c6ce", "#1f3a44"],
    "plain": ["#d9dfb6", "#333d2b"],
    "forest": ["#a7c396", "#25372a"],
    "hill": ["#cfc49c", "#3e392a"],
    "mountain": ["#b3aa9d", "#48443e"]
  };

  function isDark() {
    return window.matchMedia
      && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function hashOf(text) {
    var h = 0;
    for (var i = 0; i < text.length; i++) {
      h = (h * 31 + text.charCodeAt(i)) >>> 0;
    }
    return h;
  }

  /* The deterministic fallback for kinds the palette has never heard of:
     hue = hash(kind) mod 360, fixed saturation and lightness per theme. */
  function hashedColor(kind, dark) {
    var hue = hashOf(kind) % 360;
    return dark
      ? "hsl(" + hue + ", 30%, 32%)"
      : "hsl(" + hue + ", 42%, 68%)";
  }

  function propertyName(kind) {
    return "--terrain-" + String(kind).toLowerCase().replace(/[^a-z0-9]+/g, "-");
  }

  function terrainColor(kind, dark, styles, palette) {
    var authored = palette && palette[kind];
    if (authored) {
      if (typeof authored === "string") { return authored; }
      var themed = authored[dark ? "dark" : "light"];
      if (themed) { return themed; }
    }
    if (styles) {
      var custom = styles.getPropertyValue(propertyName(kind));
      if (custom && custom.trim()) { return custom.trim(); }
    }
    var fixed = FALLBACK[kind];
    if (fixed) { return fixed[dark ? 1 : 0]; }
    return hashedColor(kind, dark);
  }

  /* Any CSS color as "#rrggbb". The canvas normalises whatever it is handed,
     which saves converting hsl() or a custom property by hand — <input
     type="color"> takes hex and nothing else. */
  function asHex(ctx, color) {
    var previous = ctx.fillStyle;
    ctx.fillStyle = color;
    var normalised = ctx.fillStyle;
    ctx.fillStyle = previous;
    return typeof normalised === "string" && normalised.charAt(0) === "#"
      ? normalised : "#000000";
  }

  function teamColor(team, dark) {
    var hue = (hashOf(String(team || "")) * 137) % 360;
    return dark
      ? "hsl(" + hue + ", 45%, 42%)"
      : "hsl(" + hue + ", 55%, 42%)";
  }

  /* --- view helpers ----------------------------------------------------- */
  function cellAt(view, px, py) {
    return [
      Math.floor(view.x + px / view.scale),
      Math.floor(view.y + py / view.scale)
    ];
  }

  function panBy(view, dxPx, dyPx) {
    view.x -= dxPx / view.scale;
    view.y -= dyPx / view.scale;
    return view;
  }

  /* Wheel zoom to the cursor: the world point under (px, py) stays put. */
  function zoomAt(view, px, py, factor) {
    var next = Math.min(96, Math.max(3, view.scale * factor));
    view.x = view.x + px / view.scale - px / next;
    view.y = view.y + py / view.scale - py / next;
    view.scale = next;
    return view;
  }

  function fitView(cellsW, cellsH, canvasW, canvasH) {
    var pad = 24;
    var scale = Math.min(
      (canvasW - 2 * pad) / Math.max(1, cellsW),
      (canvasH - 2 * pad) / Math.max(1, cellsH)
    );
    scale = Math.min(64, Math.max(3, scale));
    return {
      x: (cellsW - canvasW / scale) / 2,
      y: (cellsH - canvasH / scale) / 2,
      scale: scale,
      width: canvasW,
      height: canvasH
    };
  }

  /* The visible cell window; overlay builders (in editor.html) share this
     window with the tile loop so nothing draws blind off-screen. */
  function visibleBounds(doc, view) {
    var s = view.scale;
    return {
      x0: Math.max(0, Math.floor(view.x)),
      y0: Math.max(0, Math.floor(view.y)),
      x1: Math.min(doc.grid.width, Math.ceil(view.x + view.width / s)),
      y1: Math.min(doc.grid.height, Math.ceil(view.y + view.height / s))
    };
  }

  /* devicePixelRatio-correct sizing: the backing store follows the CSS size,
     and the context is scaled so all drawing happens in CSS pixels. */
  function resizeCanvas(canvas, ctx) {
    var rect = canvas.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    var w = Math.max(1, Math.round(rect.width * dpr));
    var h = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { width: rect.width, height: rect.height };
  }

  /* --- drawing ---------------------------------------------------------- */
  function drawHatch(ctx, px, py, size, dark) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(px, py, size, size);
    ctx.clip();
    ctx.strokeStyle = dark ? "rgba(255,255,255,0.18)" : "rgba(0,0,0,0.18)";
    ctx.lineWidth = Math.max(1, size / 16);
    var step = Math.max(3, size / 4);
    ctx.beginPath();
    for (var o = -size; o < size; o += step) {
      ctx.moveTo(px + o, py + size);
      ctx.lineTo(px + o + size, py);
    }
    ctx.stroke();
    ctx.restore();
  }

  function drawNotches(ctx, px, py, size, count, dark) {
    var n = Math.max(2, size * 0.28);
    ctx.fillStyle = dark ? "rgba(255,255,255,0.28)" : "rgba(0,0,0,0.32)";
    var corners = [
      [px, py, 1, 1], [px + size, py + size, -1, -1],
      [px + size, py, -1, 1], [px, py + size, 1, -1]
    ];
    for (var i = 0; i < Math.min(count, 4); i++) {
      var c = corners[i];
      ctx.beginPath();
      ctx.moveTo(c[0], c[1]);
      ctx.lineTo(c[0] + n * c[2], c[1]);
      ctx.lineTo(c[0], c[1] + n * c[3]);
      ctx.closePath();
      ctx.fill();
    }
  }

  function drawGrid(ctx, doc, view, dark) {
    var s = view.scale;
    var b = visibleBounds(doc, view);
    var x0 = b.x0, y0 = b.y0, x1 = b.x1, y1 = b.y1;
    var light = dark ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.08)";
    var heavy = dark ? "rgba(255,255,255,0.16)" : "rgba(0,0,0,0.18)";
    for (var pass = 0; pass < 2; pass++) {
      ctx.strokeStyle = pass ? heavy : light;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (var gx = x0; gx <= x1; gx++) {
        if ((gx % 5 === 0) !== !!pass) { continue; }
        var lx = Math.round((gx - view.x) * s) + 0.5;
        ctx.moveTo(lx, (y0 - view.y) * s);
        ctx.lineTo(lx, (y1 - view.y) * s);
      }
      for (var gy = y0; gy <= y1; gy++) {
        if ((gy % 5 === 0) !== !!pass) { continue; }
        var ly = Math.round((gy - view.y) * s) + 0.5;
        ctx.moveTo((x0 - view.x) * s, ly);
        ctx.lineTo((x1 - view.x) * s, ly);
      }
      ctx.stroke();
    }
  }

  /* A door hangs on hinges. Open, it is therefore one leaf that swung — whole,
     the same length it was shut, standing a quarter turn from the doorway, with
     a faint arc tracing the sweep. It used to be two stubs pulled back into both
     jambs, which draws a pocket door sliding into the walls.

     The hinge side is a fixed rule rather than something the document carries:
     a horizontal door hangs on its west jamb and opens north, a vertical one on
     its north jamb and opens west. So no map gains a field and none changes
     meaning. The swung leaf reaches past its own square, which is where an open
     door goes — a door sits in a wall run, so the squares it swings between are
     the passage it interrupts, not more wall. */
  function drawDoor(ctx, px, py, size, orientation, open, dark) {
    var thick = Math.max(2, size * 0.22);
    var inset = size * 0.08;
    var span = size - 2 * inset;
    var ink = dark ? "#c9a86a" : "#6b4f2a";
    ctx.fillStyle = ink;
    if (orientation === "vertical") {
      var cx = px + (size - thick) / 2;
      if (open) {
        ctx.fillRect(cx + thick - span, py + inset, span, thick);
        drawSwing(ctx, cx + thick / 2, py + inset, span, Math.PI / 2, Math.PI, ink);
      } else {
        ctx.fillRect(cx, py + inset, thick, span);
      }
    } else {
      var cy = py + (size - thick) / 2;
      if (open) {
        ctx.fillRect(px + inset, cy + thick - span, thick, span);
        drawSwing(ctx, px + inset, cy + thick / 2, span, -Math.PI / 2, 0, ink);
      } else {
        ctx.fillRect(px + inset, cy, span, thick);
      }
    }
  }

  /* The quarter circle the leaf swept, centred on the hinge and running from the
     open leaf's tip to where the shut one's tip was. Saved and restored like
     drawStairs, since it sets alpha and stroke state the glyphs after it would
     otherwise inherit. It is the one part of this glyph the behaviour harness
     cannot see — `arc` and `stroke` are no-ops on its fake canvas — so it stays
     decoration, and the leaf above carries every claim the checks make. */
  function drawSwing(ctx, hx, hy, radius, from, to, ink) {
    ctx.save();
    ctx.globalAlpha = 0.3;
    ctx.strokeStyle = ink;
    ctx.lineWidth = Math.max(1, radius * 0.05);
    ctx.beginPath();
    ctx.arc(hx, hy, radius, from, to);
    ctx.stroke();
    ctx.restore();
  }

  function drawStairs(ctx, px, py, size, up, dark) {
    /* Saved because the round cap and join are this glyph's own: left on the
       shared context they bleed into whatever strokes next, and the overlay
       edges that follow are per-cell segments that would bead at every joint. */
    ctx.save();
    ctx.strokeStyle = dark ? "#d8d4cc" : "#3a362e";
    ctx.lineWidth = Math.max(1.5, size * 0.09);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    var mid = py + size / 2;
    var h = size * 0.24;
    for (var i = 0; i < 2; i++) {
      var base = px + size * (up ? 0.36 + 0.22 * i : 0.64 - 0.22 * i);
      var tip = base + size * (up ? -0.18 : 0.18);
      ctx.beginPath();
      ctx.moveTo(base, mid - h);
      ctx.lineTo(tip, mid);
      ctx.lineTo(base, mid + h);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawSpawn(ctx, px, py, size, dark) {
    ctx.strokeStyle = dark ? "#9ecb9e" : "#3f7a3f";
    ctx.lineWidth = Math.max(1.5, size * 0.09);
    ctx.beginPath();
    ctx.arc(px + size / 2, py + size / 2, size * 0.3, 0, Math.PI * 2);
    ctx.stroke();
  }

  function drawToken(ctx, px, py, size, token, dark) {
    var cx = px + size / 2;
    var cy = py + size / 2;
    var r = size * 0.36;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fillStyle = token.down || token.dead
      ? (dark ? "#4c4f53" : "#9a9d9f")
      : teamColor(token.team, dark);
    ctx.fill();
    ctx.lineWidth = Math.max(1, size * 0.04);
    ctx.strokeStyle = dark ? "rgba(0,0,0,0.6)" : "rgba(0,0,0,0.35)";
    ctx.stroke();
    var fraction = token.hpFraction;
    if (typeof fraction === "number" && !token.dead) {
      var clamped = Math.max(0, Math.min(1, fraction));
      ctx.beginPath();
      ctx.arc(cx, cy, r + Math.max(1.5, size * 0.07), -Math.PI / 2,
        -Math.PI / 2 + clamped * Math.PI * 2);
      ctx.strokeStyle = "hsl(" + Math.round(120 * clamped) + ", 65%, "
        + (dark ? "50%" : "40%") + ")";
      ctx.lineWidth = Math.max(1.5, size * 0.07);
      ctx.stroke();
    }
    if (token.label) {
      ctx.fillStyle = "#fff";
      ctx.font = "bold " + Math.max(8, Math.round(size * 0.42))
        + "px ui-sans-serif, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(token.label).charAt(0), cx, cy + size * 0.02);
    }
    if (token.dead) {
      ctx.strokeStyle = dark ? "#e8e8e8" : "#222";
      ctx.lineWidth = Math.max(1.5, size * 0.08);
      ctx.beginPath();
      ctx.moveTo(cx - r * 0.7, cy - r * 0.7);
      ctx.lineTo(cx + r * 0.7, cy + r * 0.7);
      ctx.moveTo(cx + r * 0.7, cy - r * 0.7);
      ctx.lineTo(cx - r * 0.7, cy + r * 0.7);
      ctx.stroke();
    } else if (token.stable) {
      ctx.fillStyle = dark ? "#9ecb9e" : "#2f7a2f";
      ctx.beginPath();
      ctx.arc(cx + r * 0.8, cy - r * 0.8, Math.max(2, size * 0.09), 0, Math.PI * 2);
      ctx.fill();
    }
  }

  /* terrainOverridesFor(doc, states) — the squares this document's fixtures
     decide, keyed "x,y" to the terrain kind each shows, resolved against which
     fixtures stand open.

     A feature carrying a `state` is a fixture: it decides its own square, and
     any square named by one of its `affects` groups. `states` is the live
     {featureId: bool} the viewer folds from the log and the editor's preview
     sets; a fixture absent from it shows the state the document authored.

     Shared rather than written per page on purpose. Server-side the same
     question has exactly one answer — MapFeature.claims() — whose docstring
     names deriving it twice as how two answers drift; a copy in each page
     would make three. The ground-height half of an overlay is not here: the
     renderer draws no relief, and what the pages do with height is their own
     business.

     It reports what it can read and skips what it cannot. A hand-opened file
     may carry any shape at all, and a malformed group must cost that group,
     not the frame. */
  function terrainOverridesFor(doc, states) {
    var out = {};
    var features = (doc && doc.features) || [];
    states = states || {};
    for (var i = 0; i < features.length; i++) {
      var feature = features[i];
      if (!feature || typeof feature !== "object") { continue; }
      if (feature.state === undefined || feature.state === null) { continue; }
      var open = Object.prototype.hasOwnProperty.call(states, feature.id)
        ? !!states[feature.id]
        : feature.state === "open";
      var side = open ? "open" : "closed";
      if (Array.isArray(feature.at) && feature.at.length === 2
        && feature.terrain && typeof feature.terrain[side] === "string") {
        out[feature.at[0] + "," + feature.at[1]] = feature.terrain[side];
      }
      var groups = Array.isArray(feature.affects) ? feature.affects : [];
      for (var g = 0; g < groups.length; g++) {
        var group = groups[g];
        if (!group || typeof group !== "object") { continue; }
        if (!group.terrain || typeof group.terrain[side] !== "string") { continue; }
        var cells = Array.isArray(group.cells) ? group.cells : [];
        for (var c = 0; c < cells.length; c++) {
          var cell = cells[c];
          if (!Array.isArray(cell) || cell.length !== 2) { continue; }
          out[cell[0] + "," + cell[1]] = group.terrain[side];
        }
      }
    }
    return out;
  }

  /* render(ctx, doc, view, overlays)
     doc — a map document payload: {grid: {width, height}, legend, tiles,
       features}, plus an optional palette. Only those keys are consulted, so a
       synthesized stand-in (the viewer's mapless plane) works too — it carries
       no palette and every kind falls through to a computed color.
     overlays — all optional: {
       featureStates: {featureId: bool},  // live door state over the defaults
       marks: [{at: [x, y], w, h, color, alpha}],  // translucent cell washes;
         // w/h are a span in cells, default 1, so a caller with a long run of
         // one colour can hand down a rectangle instead of a mark per cell
       edges: [{at: [x, y], side, color, width}],  // stroke on a cell boundary,
         // side "n" (top) or "w" (left) — the two that name every interior
         // boundary of a grid exactly once
       labels: [{at: [x, y], text, color}],  // small centered per-cell text
       terrainOverrides: {"x,y": kind},  // what a square is *now*, over its
         // glyph — the channel a fixture's effect arrives through. Squares
         // and kinds only: the renderer never learns what decided them
       tokens: [{at: [x, y], label, team, hpFraction, down, dead, stable}]
     } */
  function render(ctx, doc, view, overlays) {
    overlays = overlays || {};
    var dark = isDark();
    var styles = window.getComputedStyle(ctx.canvas);
    var s = view.scale;
    var background = styles.getPropertyValue("--canvas-bg");
    ctx.fillStyle = background && background.trim()
      ? background.trim() : (dark ? "#191b1e" : "#f4f1ea");
    ctx.fillRect(0, 0, view.width, view.height);

    var bounds = visibleBounds(doc, view);
    var x0 = bounds.x0;
    var y0 = bounds.y0;
    var x1 = bounds.x1;
    var y1 = bounds.y1;

    /* Hoisted, and checked for presence once rather than per cell: a max-size
       map zoomed out puts the loop below into the hundreds of thousands of
       iterations, and most documents hand down no overrides at all. */
    var overrides = overlays.terrainOverrides;
    for (var cy = y0; cy < y1; cy++) {
      var row = doc.tiles[cy] || "";
      for (var cx = x0; cx < x1; cx++) {
        var glyph = row.charAt(cx);
        var kind = doc.legend[glyph];
        if (kind === undefined) { kind = "unknown:" + glyph; }
        if (overrides !== undefined) {
          /* Before the fill *and* before the texture branches below, which
             read `kind` again: a flooded square must stop hatching as
             difficult terrain, not merely change colour. */
          var over = overrides[cx + "," + cy];
          if (over !== undefined) { kind = over; }
        }
        var px = (cx - view.x) * s;
        var py = (cy - view.y) * s;
        ctx.fillStyle = terrainColor(kind, dark, styles, doc.palette);
        ctx.fillRect(px, py, s + 0.5, s + 0.5);
        if (kind === "difficult") { drawHatch(ctx, px, py, s, dark); }
        else if (kind === "half-cover") { drawNotches(ctx, px, py, s, 2, dark); }
        else if (kind === "three-quarters-cover") { drawNotches(ctx, px, py, s, 4, dark); }
      }
    }

    drawGrid(ctx, doc, view, dark);

    var features = doc.features || [];
    var states = overlays.featureStates || {};
    for (var fi = 0; fi < features.length; fi++) {
      var feature = features[fi];
      var fx = feature.at[0];
      var fy = feature.at[1];
      if (fx < x0 - 1 || fx > x1 || fy < y0 - 1 || fy > y1) { continue; }
      var fpx = (fx - view.x) * s;
      var fpy = (fy - view.y) * s;
      if (feature.kind === "door") {
        var open = Object.prototype.hasOwnProperty.call(states, feature.id)
          ? !!states[feature.id]
          : feature.state === "open";
        drawDoor(ctx, fpx, fpy, s, feature.orientation || "horizontal", open, dark);
      } else if (feature.kind === "stairs_up") {
        drawStairs(ctx, fpx, fpy, s, true, dark);
      } else if (feature.kind === "stairs_down") {
        drawStairs(ctx, fpx, fpy, s, false, dark);
      } else if (feature.kind === "spawn") {
        drawSpawn(ctx, fpx, fpy, s, dark);
      }
    }

    /* One save for the whole pass, not one per mark: the relief overlay hands
       down a wash for every visible square, so a max-size map zoomed out puts
       this loop into the hundreds of thousands of iterations. */
    var marks = overlays.marks || [];
    if (marks.length) {
      ctx.save();
      for (var mi = 0; mi < marks.length; mi++) {
        var mark = marks[mi];
        ctx.globalAlpha = typeof mark.alpha === "number" ? mark.alpha : 0.35;
        ctx.fillStyle = mark.color || (dark ? "#e8c76a" : "#b98a1e");
        ctx.fillRect((mark.at[0] - view.x) * s, (mark.at[1] - view.y) * s,
          (mark.w || 1) * s + 0.5, (mark.h || 1) * s + 0.5);
      }
      ctx.restore();
    }

    var edges = overlays.edges || [];
    if (edges.length) {
      ctx.save();
      /* Stated rather than inherited: these are per-cell segments that meet
         end to end, and a round cap left behind by an earlier glyph would
         bead every joint along what should read as one line. */
      ctx.lineCap = "butt";
      for (var ei = 0; ei < edges.length; ei++) {
        var edge = edges[ei];
        var ex = edge.at[0];
        var ey = edge.at[1];
        if (ex < x0 || ex > x1 || ey < y0 || ey > y1) { continue; }
        var epx = (ex - view.x) * s;
        var epy = (ey - view.y) * s;
        ctx.strokeStyle = edge.color || (dark ? "#e3e0d8" : "#2b2925");
        ctx.lineWidth = edge.width || 1;
        ctx.beginPath();
        ctx.moveTo(epx, epy);
        if (edge.side === "w") { ctx.lineTo(epx, epy + s); }
        else { ctx.lineTo(epx + s, epy); }
        ctx.stroke();
      }
      ctx.restore();
    }

    var labels = overlays.labels || [];
    for (var li = 0; li < labels.length; li++) {
      var label = labels[li];
      if (label.at[0] < x0 || label.at[0] >= x1
        || label.at[1] < y0 || label.at[1] >= y1) { continue; }
      var text = String(label.text);
      var fontPx = Math.max(8, Math.min(Math.round(s * 0.34),
        Math.floor((s * 0.92) / (0.62 * text.length))));
      ctx.save();
      ctx.fillStyle = label.color || (dark ? "#e3e0d8" : "#2b2925");
      ctx.font = "bold " + fontPx + "px ui-sans-serif, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(text, (label.at[0] - view.x) * s + s / 2,
        (label.at[1] - view.y) * s + s / 2);
      ctx.restore();
    }

    var tokens = overlays.tokens || [];
    for (var ti = 0; ti < tokens.length; ti++) {
      var token = tokens[ti];
      drawToken(ctx, (token.at[0] - view.x) * s, (token.at[1] - view.y) * s,
        s, token, dark);
    }
  }

  return {
    render: render,
    cellAt: cellAt,
    panBy: panBy,
    zoomAt: zoomAt,
    fitView: fitView,
    visibleBounds: visibleBounds,
    terrainOverridesFor: terrainOverridesFor,
    resizeCanvas: resizeCanvas,
    terrainColor: terrainColor,
    teamColor: teamColor,
    asHex: asHex,
    isDark: isDark
  };
})();
