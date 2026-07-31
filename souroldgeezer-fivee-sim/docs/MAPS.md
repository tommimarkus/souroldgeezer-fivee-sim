# Battle maps

Your maps, as documents the engine generates, validates, edits, and fights on.

A map is one JSON file with a lifecycle — generated, hand-edited, played — and
**the file is the source of truth** once a hand has touched it. Maps are not a
content-pack section: packs are merged named-record registries, and merging two
maps by name is meaningless. A map validates on every load through the same
diagnostic machinery packs use, so a broken file names every problem at once.

## Quick start

A complete, valid document — a walled room with a door and a stair:

```json
{
  "format": "fivee-sim-map",
  "format_version": 1,
  "name": "guard room",
  "grid": { "width": 6, "height": 5, "cell_feet": 5 },
  "legend": { ".": "floor", "#": "wall", "%": "difficult" },
  "tiles": [
    "######",
    "#....#",
    "#.%...",
    "#....#",
    "######"
  ],
  "features": [
    { "id": "door-east", "kind": "door", "at": [5, 2],
      "orientation": "vertical", "state": "closed" },
    { "id": "stair-1", "kind": "stairs_down", "at": [1, 3] }
  ],
  "provenance": {
    "generator": "hand",
    "seed": 0,
    "params": {},
    "edited": false,
    "source": "Original content; 5E-compatible"
  }
}
```

Squares are zero-based `[x, y]`, origin top-left, y downward; `tiles` lists the
top row first, one character per square, each resolved through the document's
own `legend`. Save it under the maps directory and `map_load` it by path, or
hand the object to `map_load` inline.

## Where maps live

In precedence order:

1. **`FIVEE_SIM_MAPS`** — an `os.pathsep`-separated list of files or
   directories. When set, it wins outright.
2. **`$CLAUDE_PROJECT_DIR/.fivee-sim/maps/`**, used only when the variable is
   unset — the maps analogue of the content-pack convention.
3. The same `.fivee-sim/maps/` under the current directory, as a last resort.

The first configured root is also where `map_save` writes by default
(`<slug-of-name>.json`) and where `replay_export` puts its files (under
`replays/`).

## The document, field by field

Every unknown key is a hard error, everywhere — a mistyped field must never
silently become a default.

| Field | Meaning |
| --- | --- |
| `format` | Always `"fivee-sim-map"`. |
| `format_version` | Always `1`. |
| `name` | Display name; `map_save`'s default filename is its slug. |
| `grid` | `width` and `height` in squares (1–512 each), `cell_feet` fixed at 5. |
| `legend` | Single character → terrain-kind string. The glyphs `+` `/` `<` `>` `@` are reserved for renderer overlays and may not be claimed. |
| `tiles` | One string per row, top row first, every character defined in the legend, every row exactly `width` long. |
| `palette` | Optional terrain colors — see below. Absent means the renderers choose. |
| `elevation` | Optional ground height — see below. Absent means flat. |
| `features` | Doors, stairs, spawn hints, and the fixtures a fight can operate — see below. |
| `levels` | Optional storeys above and below the ground — see below. Absent means a single plane. |
| `provenance` | `generator`, `seed`, fully resolved `params`, the `edited` flag, and a `source` string. |

A document is refused past 4 MB or a 512-square side.

**Features** carry `id` (unique), `kind`, `at`, and optionally `orientation`,
`state`, `team`. A `door` requires `orientation` (`horizontal`/`vertical`) and
`state` (`open`/`closed`) — that state is the door's *default*; what a door is
doing mid-fight lives in the encounter's overlay, never in the file. Door
squares are ordinary floor in `tiles`; the feature supplies the blocking.
Other bundled kinds: `stairs_up`, `stairs_down`, `spawn` (placement hint,
optionally with a `team`). A feature may also carry `to_level`, which is what
turns a drawn stairway into one a fight can actually walk — see **Levels**
below. Ids are unique across the whole document, not per level. A feature
carrying a `state` is a **fixture** the fight can operate, and a door is only
the common case of one — see **Fixtures** below.

**Terrain kinds are strings**, resolved against loaded content exactly like
conditions: the built-in table covers `floor`, `wall`, `difficult`, `water`,
`plain`, `forest`, `hill`, `mountain`, the cover kinds, and door terrain, and
a content pack may define more. A kind nothing defines is a validation error
naming what is available.

**Fixtures** are features a fight can *operate*, and carrying a `state` is the
whole test. A door has always had one; a lever, a spike, or a sluice gate may
have one too. A feature without a state — a drawn stairway, a spawn hint —
stays document-level exactly as before: renderers and placement logic read it,
and a fight never asks about it. A state is `open` or `closed` and nothing else:
fixtures are two-valued, and the file records the one it is authored in. Six
optional keys say what operating a fixture does and what it costs, and every one
of them requires a `state`, because a fixture nothing can operate would flip
nothing, silently:

| Key | Meaning |
| --- | --- |
| `terrain` | `{"closed", "open"}` terrain kinds for the fixture's **own** square. Absent: `door-closed`/`door-open` for a `door`, and otherwise the tile it stands on in *both* states — so a lever driven into a wall leaves a wall behind it whichever way it is thrown. |
| `elevation` | `{"closed", "open"}` ground height in feet for that same square. Absent: the plane's height, unmoved. |
| `affects` | Overlay groups, each naming the `cells` it governs plus a `terrain` pair, an `elevation` pair, or both. Cells are squares on the fixture's own level. |
| `requires` | Ids of other fixtures that must stand **open** before this one may be opened. |
| `costs_action` | `true` spends the action; absent is the free object interaction. |
| `check` | `{"ability", "dc"}` — an ability check the operator must pass to move it. |

Kinds named in a pair are checked against *loaded content*, never against this
document's `legend`: what a square becomes is not a drawing question, so a
sluice may flood a room with a kind the map paints nowhere. A sluice gate that
will not budge until two spikes are pulled, and floods the room behind it when
it does:

```json
"grid": { "width": 7, "height": 5, "cell_feet": 5 },
"legend": { ".": "floor", "#": "wall" },
"tiles": [
  "#######",
  "#.#...#",
  "#.....#",
  "#.#...#",
  "#######"
],
"features": [
  { "id": "north spike", "kind": "spike", "at": [2, 1], "state": "closed",
    "costs_action": true, "check": { "ability": "strength", "dc": 15 } },
  { "id": "south spike", "kind": "spike", "at": [2, 3], "state": "closed",
    "costs_action": true, "check": { "ability": "strength", "dc": 15 } },
  { "id": "sluice gate", "kind": "door", "at": [2, 2],
    "orientation": "vertical", "state": "closed",
    "requires": ["north spike", "south spike"],
    "costs_action": true,
    "terrain":   { "closed": "door-closed", "open": "water" },
    "elevation": { "closed": 0, "open": -5 },
    "affects": [
      { "cells": [[3, 1], [4, 1], [3, 2], [4, 2], [3, 3], [4, 3]],
        "terrain":   { "closed": "floor", "open": "water" },
        "elevation": { "closed": 0,       "open": -5 } },
      { "cells": [[5, 1], [5, 2], [5, 3]],
        "terrain": { "closed": "floor", "open": "difficult" } }
    ] }
]
```

The spikes stand on wall squares and carry no `terrain`, so pulling one changes
no ground — correct, and the reason the default is the tile underneath. The
gate costs an action, waits on both spikes, and when it opens it turns its own
square and six floor squares to `water` five feet lower, and the three squares
of the wheel's race to `difficult`. **The wheel is not a second mechanism**: it is
another overlay group on the gate, which is what collapses "and the wheel starts
turning" into the same flip. Authoring the gate as a `door` is deliberate — it
inherits the door glyph, the browser editor's door drawing, and the UVTT portal
for free.

The document stores `cells` and never a rect. A rect is an `add_feature` input
for the author who would rather type one than forty pairs, expanded before it
reaches the file, so the format has exactly one shape — which is what lets a
`resize` translate an overlay square by square with the frame.

**Every square a fixture governs is claimed by exactly one fixture per level** —
its own `at`, and every cell of every overlay. The document refuses a second
claim, and so does an encounter adopting the map, because a battle map can be
hand-built with no document behind it. The rule earns its refusals by removing
the precedence question outright: there is no document order to consult and no
history to replay, which is what lets `map_query` resolve terrain over a bare
map — no fight, no history — and still agree with what the live encounter sees.

**`requires` gates opening only.** Closing is never gated, or the fiction that
opened a gate could never shut it: driving the spikes back in would bar the
gate's own lever. It is checked when the fixture is operated rather than held as
an invariant, so re-driving a spike later does not slam the gate shut.

**The check is a raw ability check.** Creatures here carry ability modifiers and
no skill proficiencies anywhere in the model — there is no Athletics, no
proficiency bonus, no Expertise, and no Help — so **set the DC as if the
character were untrained**. A DC pitched at a trained Athletics bonus will play
several points harder than intended. The format has no place to say otherwise on
purpose: skill proficiency is a rules feature of the creature model, not a map
one.

**A creature standing where the ground turns impassable stays there, and may
walk out.** Entry cost governs entering a square, not remaining in one. Refusing
the operation or shoving the occupant aside would each invent a rule SRD 5.2
does not have — the engine models no forced movement at all — so nothing happens
to it. That is a deliberate non-behaviour, not an oversight.

Operating a fixture is the encounter's business: `encounter_act(kind="interact",
feature=...)`, from its square or one beside it and on its own storey. `interact`
**toggles** by default; pass `set_open` to drive a fixture to the state you mean,
which is what to use when working a chain — asking to "open" a gate that already
stands open would otherwise close it. Everything is verified before anything is
spent — reach, storey, prerequisites, and whether the fixture already stands the
way you asked — so a party learns why a thing will not move without paying for
the lesson. Only the check itself costs: a failure spends the action and moves
nothing.

The refusals collect like every other diagnostic: an overlay group with neither
pair, cells off the grid, a terrain kind nothing defines (naming what is
available), a square claimed twice, a `requires` naming nothing, naming itself,
or naming a feature with no state, a requirement cycle reported as its path, and
a `dc` below 1.

**`format_version` stays 1.** All six keys are omitted from a feature that does
not carry them, so a file written before fixtures existed writes back
byte-for-byte. A reader that predates them refuses the document as an unknown
key — the loud failure this format prefers over a map that loads with its sluice
quietly inert, and the same reasoning as `palette` and `elevation` below.

**Palette** is what the map itself says its terrain looks like — a terrain kind
mapped to one color, or to a `{light, dark}` pair when the two themes want
different ones:

```json
"palette": { "lava": "#d2440f", "water": { "light": "#a9c6ce", "dark": "#1f3a44" } }
```

Colors are `#rgb` or `#rrggbb` hex and nothing else: the browser assets put them
straight into a CSS background, where a `url(...)` would reach the network and
break the editor's offline guarantee. Whatever the file spells, the saved form is
lowercase six-digit, kinds sorted, and a pair whose halves match written as the
one color it is. A kind may be colored without appearing in this map's `legend`,
so a palette survives re-legending.

Without an entry a kind still draws: the renderers fall back to the page's theme,
then a built-in color, then a hue hashed from the kind's name — so a pack-defined
kind is at least consistent everywhere. An authored color outranks all three, on
the canvas, in the replay viewer, and in the flat image a UVTT export carries
(which takes the `light` half, having only one theme). The key is omitted
entirely when empty, so a file written before colors existed writes back
byte-for-byte, and the format version does **not** move — same reasoning as
elevation below.

**Elevation** is ground height in feet — a `default` and a sparse `squares`
list of `[x, y, feet]` for the ground that departs from it:

```json
"elevation": { "default": 0, "squares": [[3, 4, 20], [4, 4, 20]] }
```

Feet may be negative: a pit floor sits below the map's datum. The key is
omitted entirely from a flat map at zero, so a file written before heights
existed writes back byte-for-byte; on save, squares already sitting at the
default are dropped and the rest sorted by row then column. The format version
does **not** move for this. A reader that predates the key refuses the document
as an unknown key, which is a loud failure rather than a map silently flattened.

Overland generation fills this layer: every cell gets its own height, read off
the same noise field the terrain bands come from, so relief varies *within* a
band and a mountain is never lower than the hill beside it. The waterline is the
datum — land rises to `relief_feet` (default 40) and water falls to
`-water_depth_feet` (default 20), quantised to 5 feet. Set both to zero for the
flat maps this generator used to produce. Dungeons and caves are flat.

Height is charged to **movement only**. A rise of under 2 feet across a square
is a gentle grade and free; from there up to 5 feet the square is a slope,
which SRD 5.2 makes Difficult Terrain — once, since Difficult Terrain is not
cumulative. Above 5 feet the face is climbed, at the SRD's extra foot per foot
(2 extra in Difficult Terrain) on top of the step into the square; climbing
down costs what climbing up costs. The 5-foot boundary and the step in cost
across it are engine policy. Line of sight, cover, and area templates ignore
height entirely.

**Levels** are storeys over one footprint. Every level shares the document's
`grid` and `legend` — floors of one building, not unrelated maps — so a level
carries only what differs: its own `tiles`, `elevation`, and `features`.

The ground is level `0`, and it stays in the document's own `tiles`,
`elevation`, and `features` keys. `levels` holds only what is above or below
it, each entry with a signed `index` (a basement is `-1`) and an optional
`name`:

```json
"levels": [
  {
    "index": 1,
    "name": "gallery",
    "tiles": ["######", "#....#", "#....#", "#....#", "######"],
    "elevation": { "default": 10, "squares": [] },
    "features": [
      { "id": "stair-head", "kind": "stairs_down", "at": [3, 3], "to_level": 0 }
    ]
  }
]
```

A level's `elevation.default` **is** its floor height: the gallery above sits
ten feet up because its unnamed squares do. There is no separate datum field to
disagree with the heights beside it. As with `elevation`, the key is omitted
entirely from a map with no storeys, so such a file writes back byte-for-byte
and the format version does not move.

`to_level` makes a connector: a creature standing on that square may step to
the *same square* on the named level, paying the rise between the two planes
through the ordinary slope-and-climb rules above — a ten-foot storey is a
climb. A connector must name a level the map has, and never its own.

What a level does to a fight is deliberately narrow. **A floor is opaque**:
sight, cover, and area templates do not cross between levels, so a creature on
another storey has total cover and cannot be attacked, caught in an area, or
threatened with an opportunity attack. Movement crosses at connectors only, and
routing is per level — the pathfinder will not plan a route that takes the
stairs on the way, so cross-level movement is asked for a leg at a time.

**Provenance** makes a map reproducible: `generator` + `seed` + resolved
`params` regenerate it exactly, until `edited` flips true — from then on the
file is the truth and regeneration would lose the hand's work.

## Edit operations

`map_edit` (MCP) and `POST /api/maps/{id}/edits` (REST) accept the same list
of operations and apply it **atomically** — a bad operation names its index
and nothing changes. Each operation is an object with an `op` key:

| `op` | Keys |
| --- | --- |
| `set_terrain` | `rect: [x, y, w, h]`, `terrain` |
| `paint` | `cells: [[x, y], ...]`, `terrain` |
| `line` | `from`, `to`, `terrain` — Bresenham raster |
| `carve_corridor` | `from`, `to`, `terrain?` (default floor), `horizontal_first?` |
| `add_feature` | `feature: {id, kind, at, orientation?, state?, team?}`, plus the fixture keys — an overlay may be given as a `rect` instead of `cells` |
| `remove_feature` | `id` |
| `toggle_door` | `at` — flips the recorded default state |
| `resize` | `width`, `height`, `anchor?` (default top-left), `fill?` (default wall) |
| `set_legend` | `glyph`, `terrain` — reserved glyphs refused |
| `set_name` | `name` |
| `set_palette` | `terrain`, `color` — one hex color, a `{light, dark}` pair, or `null` to drop it |
| `set_elevation` | `rect` **or** `cells`, plus `feet`; **or** `default` alone, which moves the height every unnamed square sits at |
| `adjust_elevation` | `rect` **or** `cells`, plus `by` — relative to what is there |

Every operation that acts on one storey also takes `level` (default `0`, the
ground): `set_terrain`, `paint`, `line`, `carve_corridor`, `add_feature`,
`remove_feature`, `toggle_door`, `set_elevation`, `adjust_elevation`. The other
four are document-wide by nature and take no level — `set_name`, `set_legend`
and `set_palette` because a floor has none of the three of its own, and `resize`
because every storey shares the grid, so it translates them all together.

Terrain named in an operation that *paints* must already have a glyph in the
document's legend (`set_legend` first if not); `set_legend` and `set_palette`
merely name a kind, so they check it against loaded content instead and a
colored kind need never appear on the map. A successful edit marks the document
`edited` and, in the session, bumps the map's generation.

There is no edit operation for a fixture's overlays: changing what a sluice
floods is `remove_feature` then `add_feature` in one call, which applies
atomically like any other pair. `add_feature` accepts an overlay's squares as a
`rect: [x, y, w, h]` as well as a list of `cells`, and expands it to cells
before the document is written — the file keeps one shape, so a later `resize`
translates and crops those squares with the frame exactly as it does heights.

## The interactive editor

Two ways to start it, one server either way:

- **MCP**: `map_editor_serve` spawns a detached editor process and returns its
  URL; calling it again finds the running one (`already_running`).
  `map_editor_stop` shuts it down.
- **CLI**: `fivee-sim-editor [--maps-dir DIR] [--port N]` from the engine's
  environment, for development.

**Token model.** The server binds `127.0.0.1` only and mints a fresh token per
launch. Every `/api/*` request must carry it in `X-Fivee-Editor-Token`; the
token reaches the browser only by being injected into the served page, and it
is never put in a URL, so the URL alone is safe to hand around on the machine.
Requests with a foreign `Host` header are refused, which is what keeps a
DNS-rebinding page from driving the API.

**ETag semantics.** A map's identity is the sha256 of its canonical bytes, and
that hash is its `ETag`. `PUT /api/maps/{id}` requires `If-Match`: the ETag
from your last `GET` to update, or `*` to create. A stale hash is a `409`
(someone saved in between — re-`GET` and reapply), a missing header is `428`,
and an invalid document is `422` carrying the same diagnostics the validator
prints. `POST /api/generate` **never persists** — the page reviews the result
and saves the keeper with `PUT`, exactly as `map_generate` hands off to
`map_save`.

**Ground height.** The Height tool paints absolute feet with the same brush
sizes as terrain, and the datum control in the side panel moves the height
every unpainted square sits at. The document stays sparse against the datum:
painting a square to the datum value clears it rather than recording it.
Resizing translates and crops heights with the chosen anchor, exactly as the
`resize` operation does. Relative adjustment is not in the page — that stays
with the `adjust_elevation` edit operation.

The **Heights** toggle draws the ground as relief rather than as figures,
switching itself on when a loaded map carries any. Three marks, and each is
worth reading for something different:

- A **shaded band** on every square: about six bands across whatever range the
  plane covers, washing lighter above the datum and darker and cooler below,
  with a north-west hillshade over the top. This is the shape of the ground.
- A **step edge** on a boundary the movement rules charge for — bold where the
  face is climbed, over 5 feet, and a hairline where it is a slope, 2 feet and
  up. This is where a mover's cost changes, so it is the tactical read; the
  hairlines wait for a closer zoom, since generated relief steps by 5 feet
  almost everywhere and drawn at every zoom they would bury the map.
- A **key** under the datum control, naming the feet each band covers.

Exact feet are still one hover away — the Cursor readout names the height of
the square under the pointer, overlay or no overlay. The **ft** toggle beside
Heights puts the figure back in every square that departs from the datum, for
hand-tuning a plateau; it is off by default, because a number in every square
buries the terrain it annotates and a generated overland map gives every
square one.

**Storeys.** The Level control picks the floor being edited; every tool paints
the one selected, and the canvas draws it. The control is disabled on a map
with no storeys. Undo, save, and resize carry the whole document, so editing
the gallery never costs the ground below it.

**Terrain color.** Each legend row's swatch is a color picker: change it and
that terrain kind is colored in the document itself, for this map everywhere it
is drawn — every storey included, since one palette serves the document as one
legend does. A row with a color of its own grows a `×` that drops it back to the
theme's. On a map whose file carries a `{light, dark}` pair, the picker speaks
only for the theme the page is showing and leaves the other half alone.

After saving in the editor, the file has moved on from any session copy:
`map_load` (with `replace` to keep the same map id) re-reads it. Once
hand-edited, the file is the source of truth — re-load, never assume.

## The replay bundle

`replay_export` turns an encounter into a portable record, format
`fivee-sim-replay` version 1:

```json
{
  "format": "fivee-sim-replay",
  "format_version": 1,
  "name": "guard room",
  "seed": 71203941,
  "map": { "...": "a fivee-sim-map payload, or null" },
  "initial": {
    "creatures": [
      { "name": "Thora", "team": "party", "position": [5, 5],
        "hp": 30, "max_hp": 30 }
    ],
    "map_open_features": ["door-east"]
  },
  "events": [ { "kind": "round", "detail": "round 1 begins", "...": "..." } ]
}
```

`map` is the document **as the fight captured it** — an edit made after
`encounter_create` never changes an export. Fights created mapless or from an
inline `map` spec carry `null` and replay on a neutral plane. Positions are
`[x, y]` in feet; `events` is the structured log `encounter_log` pages, in
full.

Small bundles come back inline; larger ones (or any call with `path`) are
written to `<maps root>/replays/<name>-<seed>.json`. With `embed` true the
bundle is baked into the replay viewer instead, yielding a single
self-contained `.html` — open it in any browser, no server required. The
viewer is also served live at `/viewer` by the editor process, where it takes
a dropped bundle file.

## Universal VTT export

`uvtt_export` writes a loaded map as a Universal VTT JSON file (`format:
0.3`) — the interchange format other virtual tabletops import — at
`<maps root>/uvtt/<slug-of-name>.uvtt` by default. The result is always a
file, never inlined (the payload embeds a base64 image), and an existing
file at the target is replaced without asking: like replay files, the export
is derived from the session's map, not an original.

What is exported:

- **Walls** (`line_of_sight`): polylines in grid-square units, derived from
  the tiles — every interior cell-side where an opaque terrain kind meets a
  non-opaque one becomes a unit edge, and the edges are chained and merged
  into runs, deterministically. Door squares are ordinary floor in `tiles`,
  so wall runs break at doorways by construction. The map boundary emits
  nothing: out of bounds counts as opaque, so a wall run along the border
  contributes only its interior-facing edge.
- **Portals**: one per door feature, ordered by feature id, spanning the
  door's square along its orientation, `closed` taken from the recorded
  default state.
- **Image**: a flat-color PNG of the tiles at `pixels_per_grid` pixels per
  square (default 32), one fill per terrain kind plus a one-pixel grid line
  between cells. Some importers require an image; `include_image: false`
  writes `"image": ""` instead, deliberately. A kind the document's own
  `palette` colors is exported in that color — its `light` half, the PNG having
  exactly one theme. Everything else is engine policy: bundled kinds have fixed
  colors, and an uncolored pack-defined kind gets a deterministic hue hashed
  from its name using the same fallback formula the editor renderer documents.
  Pixel parity with the themed canvas is not promised.

`uvtt_export` takes a `level` (default the ground). The format has one plane
and no notion of storeys, so a map with floors exports one file per floor
rather than a flattened picture true of neither.

Deliberately omitted, because the engine does not model them and inventing
values would misrepresent the map: `lights` and `objects_line_of_sight` ship
empty, and there is no elevation. An overland map typically has no opaque
kinds at all, so it exports an image and an empty `line_of_sight` — correct,
not a bug.

The image side is capped at 4096 pixels: `width × pixels_per_grid` and
`height × pixels_per_grid` must both fit, and the refusal names the largest
`pixels_per_grid` that would (every valid document fits at 8 or fewer).

The exporter is implemented defensively from the publicly documented shape
of the format — the JSON keys importers read — with no code ported from any
tool that produces or consumes it, and nothing beyond the standard library.
