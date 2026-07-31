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
| `features` | Doors, stairs, spawn hints — see below. |
| `provenance` | `generator`, `seed`, fully resolved `params`, the `edited` flag, and a `source` string. |

A document is refused past 4 MB or a 512-square side.

**Features** carry `id` (unique), `kind`, `at`, and optionally `orientation`,
`state`, `team`. A `door` requires `orientation` (`horizontal`/`vertical`) and
`state` (`open`/`closed`) — that state is the door's *default*; what a door is
doing mid-fight lives in the encounter's overlay, never in the file. Door
squares are ordinary floor in `tiles`; the feature supplies the blocking.
Other bundled kinds: `stairs_up`, `stairs_down`, `spawn` (placement hint,
optionally with a `team`).

**Terrain kinds are strings**, resolved against loaded content exactly like
conditions: the built-in table covers `floor`, `wall`, `difficult`, `water`,
`plain`, `forest`, `hill`, `mountain`, the cover kinds, and door terrain, and
a content pack may define more. A kind nothing defines is a validation error
naming what is available.

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

Height is charged to **movement only**. A rise of under 2 feet across a square
is a gentle grade and free; from there up to 5 feet the square is a slope,
which SRD 5.2 makes Difficult Terrain — once, since Difficult Terrain is not
cumulative. Above 5 feet the face is climbed, at the SRD's extra foot per foot
(2 extra in Difficult Terrain) on top of the step into the square; climbing
down costs what climbing up costs. The 5-foot boundary and the step in cost
across it are engine policy. Line of sight, cover, and area templates ignore
height entirely.

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
| `set_palette` | `terrain`, `color` — one hex color, a `{light, dark}` pair, or `null` to drop it |
| `set_elevation` | `rect` **or** `cells`, plus `feet`; **or** `default` alone, which moves the height every unnamed square sits at |
| `adjust_elevation` | `rect` **or** `cells`, plus `by` — relative to what is there |

Terrain named in an operation that *paints* must already have a glyph in the
document's legend (`set_legend` first if not); `set_legend` and `set_palette`
merely name a kind, so they check it against loaded content instead and a
colored kind need never appear on the map. A successful edit marks the document
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

**Terrain color.** Each legend row's swatch is a color picker: change it and
that terrain kind is colored in the document itself, for this map everywhere it
is drawn. A row with a color of its own grows a `×` that drops it back to the
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
