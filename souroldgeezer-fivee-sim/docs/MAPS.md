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
| `elevation` | Optional ground height — see below. Absent means flat. |
| `features` | Doors, stairs, spawn hints — see below. |
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
below. Ids are unique across the whole document, not per level.

**Terrain kinds are strings**, resolved against loaded content exactly like
conditions: the built-in table covers `floor`, `wall`, `difficult`, `water`,
`plain`, `forest`, `hill`, `mountain`, the cover kinds, and door terrain, and
a content pack may define more. A kind nothing defines is a validation error
naming what is available.

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
| `add_feature` | `feature: {id, kind, at, orientation?, state?, team?}` |
| `remove_feature` | `id` |
| `toggle_door` | `at` — flips the recorded default state |
| `resize` | `width`, `height`, `anchor?` (default top-left), `fill?` (default wall) |
| `set_legend` | `glyph`, `terrain` — reserved glyphs refused |
| `set_name` | `name` |
| `set_elevation` | `rect` **or** `cells`, plus `feet`; **or** `default` alone, which moves the height every unnamed square sits at |
| `adjust_elevation` | `rect` **or** `cells`, plus `by` — relative to what is there |

Every operation that acts on one storey also takes `level` (default `0`, the
ground): `set_terrain`, `paint`, `line`, `carve_corridor`, `add_feature`,
`remove_feature`, `toggle_door`, `set_elevation`, `adjust_elevation`. The other
three are document-wide by nature and take no level — `set_name` and
`set_legend` because a floor has neither of its own, and `resize` because every
storey shares the grid, so it translates them all together.

Terrain named in an operation must already have a glyph in the document's
legend (`set_legend` first if not). A successful edit marks the document
`edited` and, in the session, bumps the map's generation.

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
painting a square to the datum value clears it rather than recording it. The
Heights toggle overlays tints and per-square feet, switching itself on when a
loaded map carries relief, and resizing translates and crops heights with the
chosen anchor, exactly as the `resize` operation does. Relative adjustment is
not in the page — that stays with the `adjust_elevation` edit operation.

**Storeys.** The Level control picks the floor being edited; every tool paints
the one selected, and the canvas draws it. The control is disabled on a map
with no storeys. Undo, save, and resize carry the whole document, so editing
the gallery never costs the ground below it.

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
  writes `"image": ""` instead, deliberately. The palette is engine policy:
  bundled kinds have fixed colors, and a pack-defined kind gets a
  deterministic hue hashed from its name using the same fallback formula the
  editor renderer documents — the PNG has exactly one theme, so pixel parity
  with the themed canvas is not promised.

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
