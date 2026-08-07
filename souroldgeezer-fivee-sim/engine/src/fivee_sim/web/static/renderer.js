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

  /* --- facing ------------------------------------------------------------
     The eight names and the unit step each one points along. Grid north is −y:
     the same convention the door hinge and swing words have always carried on
     disk, and the one the engine's own Facing enum states. This is that table
     spelled for a canvas, not a second vocabulary — a name absent from it draws
     nothing rather than guessing.

     One ink serves all three carriers — a creature, a map feature, and the map
     itself — because they are one vocabulary and should read as one mark. */
  var FACING_UNITS = {
    "north": [0, -1], "northeast": [1, -1], "east": [1, 0], "southeast": [1, 1],
    "south": [0, 1], "southwest": [-1, 1], "west": [-1, 0], "northwest": [-1, -1]
  };
  var FACING_INK = ["#a8462a", "#e8a488"];  /* [light, dark] */

  /* hasOwnProperty rather than a bare lookup: this reads a name out of a
     hand-opened file, and "constructor" is a string. */
  function facingUnit(name) {
    var key = String(name);
    if (!Object.prototype.hasOwnProperty.call(FACING_UNITS, key)) { return null; }
    var step = FACING_UNITS[key];
    var length = Math.sqrt(step[0] * step[0] + step[1] * step[1]);
    return [step[0] / length, step[1] / length];
  }

  function facingInk(dark) { return FACING_INK[dark ? 1 : 0]; }

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

  /* A door hangs on the document's hinge and swings toward its document side.
     Omitted metadata preserves the original drawing: horizontal west/north,
     vertical north/west. The swung leaf reaches past its own square, which is
     where an open door goes — a door sits in a wall run, so the squares it
     swings between are the passage it interrupts, not more wall. */
  function drawDoor(ctx, px, py, size, orientation, hinge, swing, open, dark) {
    var thick = Math.max(2, size * 0.22);
    var inset = size * 0.08;
    var span = size - 2 * inset;
    var ink = dark ? "#c9a86a" : "#6b4f2a";
    ctx.fillStyle = ink;
    if (orientation === "vertical") {
      hinge = hinge === "south" ? "south" : "north";
      swing = swing === "east" ? "east" : "west";
      var cx = px + (size - thick) / 2;
      if (open) {
        var vy = hinge === "north" ? py + inset : py + inset + span - thick;
        var vx = swing === "west" ? cx + thick - span : cx;
        ctx.fillRect(vx, vy, span, thick);
        drawSwing(
          ctx, cx + thick / 2,
          hinge === "north" ? py + inset : py + inset + span,
          span, hinge === "north" ? Math.PI / 2 : -Math.PI / 2,
          swing === "west" ? Math.PI : 0, ink
        );
      } else {
        ctx.fillRect(cx, py + inset, thick, span);
      }
    } else {
      hinge = hinge === "east" ? "east" : "west";
      swing = swing === "south" ? "south" : "north";
      var cy = py + (size - thick) / 2;
      if (open) {
        var hx = hinge === "west" ? px + inset : px + inset + span;
        var doorX = hinge === "west" ? hx : hx - thick;
        var doorY = swing === "north" ? cy + thick - span : cy;
        ctx.fillRect(doorX, doorY, thick, span);
        drawSwing(
          ctx, hx, cy + thick / 2, span,
          hinge === "west" ? 0 : Math.PI,
          swing === "north" ? -Math.PI / 2 : Math.PI / 2, ink
        );
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
    var clockwiseSweep = (to - from + 2 * Math.PI) % (2 * Math.PI);
    ctx.arc(hx, hy, radius, from, to, clockwiseSweep > Math.PI);
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

  function drawOpening(ctx, px, py, size, dark) {
    ctx.save();
    ctx.strokeStyle = dark ? "#79b8ff" : "#1769aa";
    ctx.lineWidth = Math.max(1.5, size * 0.08);
    ctx.setLineDash([Math.max(2, size * 0.15), Math.max(2, size * 0.1)]);
    ctx.strokeRect(px + size * 0.18, py + size * 0.18, size * 0.64, size * 0.64);
    ctx.restore();
  }

  function drawLight(ctx, px, py, size, feature) {
    var color = feature.light && feature.light.color || "#ffffff";
    ctx.save();
    ctx.fillStyle = color;
    ctx.globalAlpha = 0.75;
    ctx.beginPath();
    ctx.arc(px + size / 2, py + size / 2, size * 0.18, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  /* One arrowhead about (cx, cy): the tip sits `reach` along the facing, and
     the two wings sit `arm` back from the tip and `arm` to either side of the
     axis. Nothing is claimed by it — a facing is recorded, drawn and reported,
     and decides no square.

     **Absolute coordinates, deliberately, and this is load-bearing.** A
     translate/rotate pair would be the obvious way to write this and would draw
     the same picture, but scripts/check-editor-behaviour.mjs records moveTo and
     lineTo arguments exactly as passed: under a rotated context all eight
     facings would record the identical three points, and every check that a
     chevron points the right way would pass with the facing ignored. The
     transform this file uses is the DPR one in resizeCanvas and no other.

     The rule binds drawSightCone below for the same reason, and costs it more:
     the cone's two arcs are runs of lineTo rather than one ctx.arc, because an
     arc records a centre, a radius and a pair of angles — and struck about a
     rotated origin those are the identical numbers whichever way the creature
     is looking. Same blind spot, one call wider.

     Saved and restored like drawStairs: the round cap and join are this glyph's
     own, and left behind on the shared context they bead the per-cell overlay
     segments that stroke after it. */
  function drawChevron(ctx, cx, cy, reach, arm, facing, ink, width) {
    var unit = facingUnit(facing);
    if (!unit) { return; }
    var ux = unit[0];
    var uy = unit[1];
    var backX = cx + ux * (reach - arm);
    var backY = cy + uy * (reach - arm);
    ctx.save();
    ctx.strokeStyle = ink;
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(backX - uy * arm, backY + ux * arm);
    ctx.lineTo(cx + ux * reach, cy + uy * reach);
    ctx.lineTo(backX + uy * arm, backY - ux * arm);
    ctx.stroke();
    ctx.restore();
  }

  /* --- the sight cone ----------------------------------------------------
     What a creature's facing draws instead of the chevron a map feature wears:
     a 90° wedge about the bearing, running from `inner` out to `reach`, washed
     in and then rimmed along its own outline. An arrowhead sized to a token is
     a mark you have to go looking for; a creature's facing is read at the scale
     the fight is played at, and the wedge is what gives it that scale.

     **It claims nothing about what the creature can see.** No wall stops it,
     and none ever will. Line of sight is decided in kernel/grid.py, and a
     second implementation here — in a language that cannot call the first, from
     a document that carries no creature the kernel would recognise — would be a
     picture free to disagree with the ruling, and it would be believed. This
     says which way the creature is looking, and stops there.

     The apex is clipped off at `inner` rather than drawn to the centre, so the
     token under it, its hit-point ring and its initial stay legible through the
     wash. That makes the shape an annular sector: an inner arc, a radial, an
     outer arc, and the closing radial fill() supplies.

     One path, committed twice. The rim is not a second shape struck in the same
     ink — it is the fill's own outline, restated at an alpha that bounds the
     wedge where the low-opacity wash barely registers against the ground.

     Straight segments and absolute coordinates, for the reason set out above
     drawChevron: no ctx.arc, no rotate, no translate. */
  var CONE_HALF_ANGLE = Math.PI / 4;  /* 90° in all — a bearing, not a beam */
  /* Eight segments an arc, so a vertex every 11.25°. The chord between two of
     them sags 0.48% of the radius inside the true curve: half a pixel at the
     scale a map is usually read at, under three at the tightest zoom this
     renderer allows, and invisible either way behind a wash this faint. Eight
     is also few enough that the whole wedge stays eighteen points — a path a
     reader can count, and the vertex window the behaviour harness names. */
  var CONE_ARC_SEGMENTS = 8;
  /* [light, dark]. A light wash keeps overlapping facings subordinate to the
     map's own colours; the dark theme takes a little more because its facing
     ink is the lighter of the two and lies on darker ground. Either way this
     stays an orientation hint, not a fill that hides the terrain a fight is
     being decided on. */
  var CONE_FILL_ALPHA = [0.05, 0.07];
  /* The rim is firm enough to keep the faint wash reading as a shape, while
     remaining well behind walls, fixtures, and the token furniture. */
  var CONE_EDGE_ALPHA = 0.18;

  function drawSightCone(ctx, cx, cy, inner, reach, facing, ink, dark) {
    var unit = facingUnit(facing);
    if (!unit) { return; }
    var from = Math.atan2(unit[1], unit[0]) - CONE_HALF_ANGLE;
    var step = (2 * CONE_HALF_ANGLE) / CONE_ARC_SEGMENTS;
    ctx.beginPath();
    for (var i = 0; i <= CONE_ARC_SEGMENTS; i++) {
      var near = from + step * i;
      var nx = cx + Math.cos(near) * inner;
      var ny = cy + Math.sin(near) * inner;
      if (i === 0) { ctx.moveTo(nx, ny); } else { ctx.lineTo(nx, ny); }
    }
    /* Back down the outer arc from the angle the inner one finished on, so the
       first point of this run is the radial that joins the two. */
    for (var j = CONE_ARC_SEGMENTS; j >= 0; j--) {
      var far = from + step * j;
      ctx.lineTo(cx + Math.cos(far) * reach, cy + Math.sin(far) * reach);
    }
    ctx.closePath();
    ctx.fillStyle = ink;
    ctx.globalAlpha = CONE_FILL_ALPHA[dark ? 1 : 0];
    ctx.fill();
    /* The same path, not a new one: no beginPath between the two commits. */
    ctx.strokeStyle = ink;
    /* `reach` is three squares, so this is 0.03 of a square — the width rule the
       chevron carries, restated in the one length this function is handed. */
    ctx.lineWidth = Math.max(1, reach * 0.01);
    ctx.globalAlpha = CONE_EDGE_ALPHA;
    ctx.stroke();
  }

  /* The document's compass: where *true* north lies relative to the grid. It is
     a property of the whole map rather than of any square, so it is drawn as a
     dial in the corner of the view and not on a cell — and it redefines
     nothing. Grid north is −y whatever this says, which is why a door hinged
     north on a map whose true north is east still hangs on the same edge.

     Drawn only when the document states one. A map that carries no compass says
     nothing about true north, and a rose invented for it would claim a fact the
     file does not make — so every document that has never heard of the field
     draws exactly as it did before. */
  function drawCompass(ctx, view, compass, dark) {
    var unit = facingUnit(compass);
    if (!unit) { return; }
    var radius = Math.max(10, Math.min(18, view.width * 0.05));
    var cx = view.width - radius - 12;
    var cy = radius + 12;
    var ink = facingInk(dark);
    ctx.save();
    ctx.globalAlpha = 0.85;
    ctx.strokeStyle = dark ? "rgba(255,255,255,0.35)" : "rgba(0,0,0,0.30)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.stroke();
    ctx.strokeStyle = ink;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(cx - unit[0] * radius * 0.72, cy - unit[1] * radius * 0.72);
    ctx.lineTo(cx + unit[0] * radius * 0.72, cy + unit[1] * radius * 0.72);
    ctx.stroke();
    drawChevron(ctx, cx, cy, radius * 0.95, radius * 0.34, compass, ink, 2);
    ctx.fillStyle = ink;
    ctx.font = "bold " + Math.round(radius * 0.62)
      + "px ui-sans-serif, system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("N", cx + unit[0] * radius * 1.45, cy + unit[1] * radius * 1.45);
    ctx.restore();
  }

  function drawToken(ctx, px, py, size, token, dark) {
    var cx = px + size / 2;
    var cy = py + size / 2;
    var r = size * 0.36;
    /* The outer edge of the health stroke, whether or not this token happens
       to draw one. Status furniture sits beyond this boundary rather than
       hiding the very state the ring exists to show. */
    var healthRingWidth = Math.max(1.5, size * 0.07);
    var furnitureEdge = r + healthRingWidth * 1.5;
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
      ctx.arc(cx, cy, r + healthRingWidth, -Math.PI / 2,
        -Math.PI / 2 + clamped * Math.PI * 2);
      ctx.strokeStyle = "hsl(" + Math.round(120 * clamped) + ", 65%, "
        + (dark ? "50%" : "40%") + ")";
      ctx.lineWidth = healthRingWidth;
      ctx.stroke();
    }
    /* A creature's facing is not drawn here. It is a sight cone now, and a cone
       is translucent — drawn from inside this function it would lie over every
       token committed before it. render() paints them all in a pass of its own,
       under every token in the frame. */
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
      var stableRadius = Math.max(2, size * 0.09);
      var stableReach = (furnitureEdge + stableRadius + Math.max(1, size * 0.02))
        / Math.sqrt(2);
      ctx.fillStyle = dark ? "#9ecb9e" : "#2f7a2f";
      ctx.beginPath();
      ctx.arc(cx + stableReach, cy - stableReach, stableRadius, 0, Math.PI * 2);
      ctx.fill();
    }
    if (!token.dead && Number.isInteger(token.initiativeRank) && token.initiativeRank > 0) {
      /* A compact turn-order badge beyond the upper-left edge of the health
         ring. The viewer decides the rank; this shared renderer only presents
         the optional number it was handed. A dead creature has left initiative
         and therefore wears no rank even if a stale caller supplies one.

         A capsule rather than a fixed circle keeps a double-digit encounter
         legible without shrinking its number below the token's own initial. */
      var rank = "\u2191" + token.initiativeRank;
      var fontSize = Math.max(7, Math.min(13, Math.round(size * 0.22)));
      var badgeHeight = Math.max(9, Math.round(size * 0.26));
      ctx.save();
      ctx.font = "bold " + fontSize + "px ui-sans-serif, system-ui, sans-serif";
      var badgeWidth = Math.max(
        badgeHeight, Math.ceil(ctx.measureText(rank).width + Math.max(4, size * 0.12))
      );
      var badgeGap = Math.max(1, size * 0.02);
      var badgeTangent = furnitureEdge / Math.sqrt(2);
      var badgeX = cx - badgeTangent - badgeGap - badgeWidth;
      var badgeY = cy - badgeTangent - badgeGap - badgeHeight;
      var badgeRadius = badgeHeight / 2;
      ctx.beginPath();
      ctx.arc(
        badgeX + badgeRadius, badgeY + badgeRadius, badgeRadius,
        Math.PI / 2, Math.PI * 1.5
      );
      ctx.lineTo(badgeX + badgeWidth - badgeRadius, badgeY);
      ctx.arc(
        badgeX + badgeWidth - badgeRadius, badgeY + badgeRadius, badgeRadius,
        -Math.PI / 2, Math.PI / 2
      );
      ctx.closePath();
      ctx.fillStyle = dark ? "rgba(24,26,30,0.94)" : "rgba(250,248,242,0.96)";
      ctx.fill();
      ctx.strokeStyle = dark ? "#c9a86a" : "#7a5c2e";
      ctx.lineWidth = Math.max(1, size * 0.035);
      ctx.stroke();
      ctx.fillStyle = dark ? "#f0cf8b" : "#5e431c";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(rank, badgeX + badgeWidth / 2, badgeY + badgeHeight / 2);
      ctx.restore();
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
     question has exactly one answer — MapFeatureRecord.claims() — whose docstring
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
       features}, plus an optional palette and an optional compass, one of the
       eight facing names, saying where true north lies. Only those keys are
       consulted, so a synthesized stand-in (the viewer's mapless plane) works
       too — it carries no palette and every kind falls through to a computed
       color, and it states no compass so none is drawn.
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
       tokens: [{at: [x, y], label, team, hpFraction, down, dead, stable,
         facing, initiativeRank}],  // facing is one of the eight names, or absent for a
         // creature whose direction nobody is tracking. A facing draws a sight
         // cone under the tokens — orientation only, occluded by nothing
         // initiativeRank is an optional positive turn-order position; callers
         // decide its meaning and the renderer draws it without sorting
       sightCones: bool  // false suppresses those cones. Default on: absent
         // and true both draw them, so every caller written before the switch
         // keeps the picture it had
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

    if (doc.ambient_light === "dim" || doc.ambient_light === "darkness") {
      ctx.save();
      ctx.fillStyle = "#07111f";
      ctx.globalAlpha = doc.ambient_light === "darkness" ? 0.38 : 0.18;
      ctx.fillRect(0, 0, ctx.canvas.width, ctx.canvas.height);
      ctx.restore();
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
        drawDoor(
          ctx, fpx, fpy, s, feature.orientation || "horizontal",
          feature.hinge, feature.swing, open, dark
        );
      } else if (feature.kind === "stairs_up") {
        drawStairs(ctx, fpx, fpy, s, true, dark);
      } else if (feature.kind === "stairs_down") {
        drawStairs(ctx, fpx, fpy, s, false, dark);
      } else if (feature.kind === "spawn") {
        drawSpawn(ctx, fpx, fpy, s, dark);
      } else if (feature.kind === "opening") {
        drawOpening(ctx, fpx, fpy, s, dark);
      }
      if (feature.light) { drawLight(ctx, fpx, fpy, s, feature); }
      /* Drawn over whichever glyph the branches above chose, because a facing
         is a property of the thing rather than a thing of its own: an arrow
         slit pointing out of the corridor is still an opening. A door never
         reaches here in a document the engine wrote — the format refuses the
         key on one, which already says where it points three ways over — but
         this loop draws what it is handed and does not police the file. */
      if (feature.facing) {
        drawChevron(ctx, fpx + s / 2, fpy + s / 2, s * 0.44, s * 0.16,
          feature.facing, facingInk(dark), Math.max(1.5, s * 0.07));
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

    /* The sight cones, in a pass of their own and before the first token is
       drawn. A cone is a wash and a token is not, so a cone struck from inside
       the loop below would lie over every token already committed — one
       creature seeing through another's back, and which pair looked wrong would
       depend on the order the tokens happened to arrive in.

       One save for the whole pass rather than one per cone, like the marks
       above: a cone borrows globalAlpha, and render() never resets it, so the
       restore is what keeps the tokens drawn after this at full strength.

       Read as a comparison against false rather than as a truthiness test. The
       switch is default-on, and every caller that predates it — the editor
       included, which grows no toggle — hands down an overlays object with no
       such key; `if (overlays.sightCones)` would quietly take the cones away
       from all of them. */
    if (overlays.sightCones !== false) {
      ctx.save();
      for (var ci = 0; ci < tokens.length; ci++) {
        var seer = tokens[ci];
        /* Nothing for the dead: a body is not facing anywhere, and the cross
           its token wears is what that square has to say. */
        if (!seer.facing || seer.dead) { continue; }
        /* Clear of the token's own furniture, computed from the hit-point ring
           rather than guessed past it: the ring is stroked on r + max(1.5,
           0.07·s) at that same width, so its outer edge lies half a width
           further out again, and the wash begins outside it at every scale and
           every fraction of health.

           Three squares of reach: enough to read the bearing beside its token,
           short enough that several creatures do not lay a mesh across the
           room. It is not a sight range and does not claim to be one — see
           drawSightCone. */
        var ringEdge = s * 0.36 + Math.max(1.5, s * 0.07) * 1.5;
        drawSightCone(
          ctx, (seer.at[0] - view.x) * s + s / 2,
          (seer.at[1] - view.y) * s + s / 2,
          ringEdge + Math.max(2, s * 0.06), s * 3,
          seer.facing, facingInk(dark), dark
        );
      }
      ctx.restore();
    }

    for (var ti = 0; ti < tokens.length; ti++) {
      var token = tokens[ti];
      drawToken(ctx, (token.at[0] - view.x) * s, (token.at[1] - view.y) * s,
        s, token, dark);
    }

    /* Last, and in view coordinates rather than world ones: the dial belongs to
       the picture, not to a square, so it neither pans nor zooms and nothing
       may be drawn over it. */
    drawCompass(ctx, view, doc.compass, dark);
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
