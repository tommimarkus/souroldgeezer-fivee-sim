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
own `legend`. Write it under the maps directory with `map.put` and every other
operation can name it by id, or hand the object inline as the `document` of a
`map.render`, `map.query`, `map.uvtt`, or `map.validate` call.

## Where maps live

The CLI auto-discovers the nearest `.fivee-sim/config.toml` by walking upward
from the invocation workspace. The global `--config PATH` option selects a file
explicitly instead. Storage roots are declared in that file:

```toml
format_version = 1

[storage]
maps = ["maps", "../shared-maps"]
replays = "replays"
scenes = "scenes"
encounters = "encounters"
```

Relative paths resolve against the directory containing `config.toml` — normally
`.fivee-sim/`. `maps` and `replays` may each be a string or an array of strings;
`scenes` and `encounters` take one string each. When omitted, all four default to
the sibling `maps/`, `replays/`, `scenes/`, and `encounters/` directories.

The first configured maps root is where `map.put` and
`map.generate --save-as` write (`<id>.json`); reads and `map.list` cover all map
roots. Replay writes use the first configured replays root, independently of the
maps roots.

A selected file owns these project-facing settings. For compatibility, and only
when no file is selected, `FIVEE_SIM_PROJECT_DIR`, `FIVEE_SIM_MAPS`,
`FIVEE_SIM_REPLAYS`, `FIVEE_SIM_SCENES`, and `FIVEE_SIM_ENCOUNTERS` retain their
previous meanings. `fivee content.status` names the configuration source and
path; `fivee serve` and `fivee server.ping` report the resolved storage roots.

## The document, field by field

Every unknown key is a hard error, everywhere — a mistyped field must never
silently become a default.

| Field | Meaning |
| --- | --- |
| `format` | Always `"fivee-sim-map"`. |
| `format_version` | Always `1`. |
| `name` | Display name. The **id** is the filename, not this — `map.generate --save-as` and `map.put` both take the id directly. |
| `grid` | `width` and `height` in squares (1–512 each), `cell_feet` fixed at 5. |
| `legend` | Single character → terrain-kind string. The glyphs `+` `/` `<` `>` `@` are reserved for renderer overlays and may not be claimed. |
| `tiles` | One string per row, top row first, every character defined in the legend, every row exactly `width` long. |
| `palette` | Optional terrain colors — see below. Absent means the renderers choose. |
| `ambient_light` | Optional `bright`, `dim`, or `darkness` for the ground plane. Bright is the omitted default. |
| `elevation` | Optional ground height — see below. Absent means flat. |
| `features` | Doors, stairs, spawn hints, and the fixtures a fight can operate — see below. |
| `levels` | Optional storeys above and below the ground — see below. Absent means a single plane. |
| `provenance` | `generator`, `seed`, fully resolved `params`, the `edited` flag, and a `source` string. |

A document is refused past 4 MB or a 512-square side.

**Features** carry `id` (unique), `kind`, `at`, and optionally `orientation`,
`state`, `team`. A `door` requires `orientation` (`horizontal`/`vertical`) and
`state` (`open`/`closed`) — that state is the door's *default*; what a door is
doing mid-fight lives in the encounter's overlay, never in the file. A door may
also say where it is hinged and where it opens: a horizontal door takes
`hinge: west|east` and `swing: north|south`; a vertical one takes
`hinge: north|south` and `swing: west|east`. Omitting them preserves the
historical drawing — horizontal west/north, vertical north/west. These fields
describe the leaf on the map; an open leaf does not occupy another combat
square. Door
squares are ordinary floor in `tiles`; the feature supplies the blocking.
Other bundled kinds: `stairs_up`, `stairs_down`, `spawn` (placement hint,
optionally with a `team`), `opening`, and `light`. A feature may carry
`sight_to_levels`, a list of levels visible through its square, and a `light`
object with non-negative `bright` and `dim` ranges in feet plus a `#rrggbb`
`color`. Either field is valid on any feature; `opening` and `light` are the
editor's purpose-specific glyphs. A feature may also carry `to_level`, which is what
turns a drawn stairway into one a fight can actually walk — see **Levels**
below. Ids are unique across the whole document, not per level. A feature
carrying a `state` is a **fixture** the fight can operate, and a door is only
the common case of one — see **Fixtures** below.

Two doors may form one double door by naming each other with reciprocal
`linked_to` fields. The leaves must be adjacent along their common orientation,
on the same level, authored in the same state, and carry the same `requires`,
`trigger`, `costs_action`, and `check`. A link is exactly one pair, never a
chain. Operating either leaf makes one check, spends at most one action or
interaction, and moves both leaves together; their hinge, swing, terrain,
elevation, and overlay effects remain individual. For example:

```json
{ "id": "west leaf", "kind": "door", "at": [4, 3],
  "orientation": "horizontal", "hinge": "west", "swing": "north",
  "state": "closed", "linked_to": "east leaf" },
{ "id": "east leaf", "kind": "door", "at": [5, 3],
  "orientation": "horizontal", "hinge": "east", "swing": "north",
  "state": "closed", "linked_to": "west leaf" }
```

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
fixtures are two-valued, and the file records the one it is authored in. Seven
optional keys say what operating a fixture does and what it costs, and every one
of them requires a `state`, because a fixture nothing can operate would flip
nothing, silently:

| Key | Meaning |
| --- | --- |
| `terrain` | `{"closed", "open"}` terrain kinds for the fixture's **own** square. Absent: `door-closed`/`door-open` for a `door`, and otherwise the tile it stands on in *both* states — so a lever driven into a wall leaves a wall behind it whichever way it is thrown. |
| `elevation` | `{"closed", "open"}` ground height in feet for that same square. Absent: the plane's height, unmoved. |
| `affects` | Overlay groups, each naming the `cells` it governs plus a `terrain` pair, an `elevation` pair, or both. Cells are squares on the fixture's own level. |
| `requires` | Ids of other fixtures that must stand **open** before this one may be opened. |
| `trigger` | Target-local automation: `{"when": {fixture-id: "open"|"closed", ...}, "set": "open"|"closed", "mode": "edge"|"maintained"}`. |
| `costs_action` | `true` spends the action; absent is the free object interaction. |
| `check` | `{"ability", "dc"}`, optionally `"skill"` — an ability check the operator must pass to move it. |

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
history to replay, which is what lets `map.query` resolve terrain over a bare
map — no fight, no history — and still agree with what the live encounter sees.

**`requires` gates opening only.** Closing is never gated, or the fiction that
opened a gate could never shut it: driving the spikes back in would bar the
gate's own lever. It is checked when the fixture is operated rather than held as
an invariant, so re-driving a spike later does not slam the gate shut.

### Fixture-state triggers

A fixture may carry one target-local `trigger`. Its non-empty `when` object is
an AND predicate over other fixtures' live `open` or `closed` states; `set`
names the target's resulting state. Every referenced feature must exist and
carry a state, trigger dependencies must be acyclic, and linked door leaves
must carry identical triggers. If a trigger opens a fixture with `requires`,
its predicate must include every requirement as `open`, so automation cannot
bypass a physical prerequisite.

`mode: "edge"` fires when its predicate changes from false to true, then rearms
only after the predicate becomes false. A predicate already true when an
encounter starts does not fire. `mode: "maintained"` forces the configured
state while its predicate is true; a contrary manual interaction is refused
before an action, interaction, or check is spent. When the predicate becomes
false the target keeps its current state rather than reversing automatically.
An initially true maintained trigger must agree with the fixture's authored
state.

After a successful direct interaction, the encounter drains resulting trigger
chains in dependency order, using fixture id as the tie-breaker. Automatic
transitions bypass the target's reach, cost, and check because no creature is
operating it. Each is still an ordinary `interact` event after the event that
caused it, with an empty actor and `automatic: true`, `triggered_by`, `feature`,
and `open` in its data; linked leaves also retain `linked`. The usual
`open_features` overlay and replay folding therefore remain the only live map
state machinery. `map.query` stays a snapshot resolver: an explicitly supplied
open-state set is authoritative and it never runs triggers.

**The check is an ability check, and it may optionally name a skill.** A
creature's `skill_bonuses` (see [CONTENT-PACKS.md](CONTENT-PACKS.md)) supplies
the *printed* modifier for the named skill in place of the raw ability
modifier — but there is still no proficiency bonus, no Expertise, and no Help
anywhere in the model, so **set the DC as if a skill-less target were
untrained**: a DC pitched at a trained Athletics bonus will still play several
points harder than intended for a creature whose sheet prints no Athletics
bonus at all. Omitting `skill` rolls the raw ability modifier exactly as
before.

**A creature standing where the ground turns impassable stays there, and may
walk out.** Entry cost governs entering a square, not remaining in one. Refusing
the operation or shoving the occupant aside would each invent a rule SRD 5.2.1
does not have — the engine models no forced movement at all — so nothing happens
to it. That is a deliberate non-behaviour, not an oversight.

Operating a fixture is the encounter's business: `encounter.act` with
`kind="interact"`,
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
or naming a feature with no state, a requirement cycle reported as its path, a
malformed trigger, a missing or stateless trigger reference, a trigger cycle,
an inconsistent linked or maintained trigger, and a `dc` below 1.

**`format_version` stays 1.** All seven keys are omitted from a feature that does
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
which SRD 5.2.1 makes Difficult Terrain — once, since Difficult Terrain is not
cumulative. Above 5 feet the face is climbed, at the SRD's extra foot per foot
(2 extra in Difficult Terrain) on top of the step into the square; climbing
down costs what climbing up costs. The 5-foot boundary and the step in cost
across it are engine policy. Line of sight, cover, and area templates ignore
height entirely.

**Levels** are storeys over one footprint. Every level shares the document's
`grid` and `legend` — floors of one building, not unrelated maps — so a level
carries only what differs: its own `tiles`, `elevation`, `features`, and optional
`ambient_light`.

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

What a level does to a fight is deliberately narrow. **A floor is opaque unless
an authored `sight_to_levels` opening says otherwise**: sight and attacks may
cross from that square to the named storey, while area templates remain on their
own plane. Everywhere else a creature on another storey has total cover and
cannot be attacked, caught in an area, or threatened with an opportunity attack.
Movement crosses at connectors or by an explicit Fly move, and
routing is per level — the pathfinder will not plan a route that takes the
stairs on the way, so cross-level movement is asked for a leg at a time.

**Provenance** makes a map reproducible: `generator` + `seed` + resolved
`params` regenerate it exactly, until `edited` flips true — from then on the
file is the truth and regeneration would lose the hand's work.

## Edit operations

`map.edit` — `POST /api/v1/maps/{id}/edits` — takes a list of operations and
applies it **atomically**: a bad operation names its index and nothing changes.
Each operation is an object with an `op` key:

| `op` | Keys |
| --- | --- |
| `set_terrain` | `rect: [x, y, w, h]`, `terrain` |
| `paint` | `cells: [[x, y], ...]`, `terrain` |
| `line` | `from`, `to`, `terrain` — Bresenham raster |
| `carve_corridor` | `from`, `to`, `terrain?` (default floor), `horizontal_first?` |
| `add_feature` | `feature: {id, kind, at, orientation?, hinge?, swing?, state?, linked_to?, team?, to_level?}`, plus the fixture keys — an overlay may be given as a `rect` instead of `cells` |
| `set_feature` | `feature` — the same record, editing in place the feature its `id` names; **writes the record whole** |
| `remove_feature` | `id` |
| `toggle_door` | `at` — flips the recorded default state; a linked pair flips together |
| `resize` | `width`, `height`, `anchor?` (default top-left), `fill?` (default wall) |
| `set_legend` | `glyph`, `terrain` — reserved glyphs refused |
| `set_name` | `name` |
| `set_palette` | `terrain`, `color` — one hex color, a `{light, dark}` pair, or `null` to drop it |
| `set_elevation` | `rect` **or** `cells`, plus `feet`; **or** `default` alone, which moves the height every unnamed square sits at |
| `adjust_elevation` | `rect` **or** `cells`, plus `by` — relative to what is there |

Every operation that acts on one storey also takes `level` (default `0`, the
ground): `set_terrain`, `paint`, `line`, `carve_corridor`, `add_feature`,
`remove_feature`, `toggle_door`, `set_elevation`, `adjust_elevation`. The other
five are document-wide by nature and take no level — `set_name`, `set_legend`
and `set_palette` because a floor has none of the three of its own, and `resize`
because every storey shares the grid, so it translates them all together.
`set_feature` is the fifth for a different reason: it edits the feature its
record's `id` names wherever that feature stands, and a `level` would be the
power to rehouse a fixture one storey up.

Terrain named in an operation that *paints* must already have a glyph in the
document's legend (`set_legend` first if not); `set_legend` and `set_palette`
merely name a kind, so they check it against loaded content instead and a
colored kind need never appear on the map. A successful edit marks the document
`edited` and, in the session, bumps the map's generation.

### Editing a feature

`set_feature` is how any of a feature's fields change after it is written —
what a sluice floods, which way a door hangs, where a lever stands, whether a
stairway leads anywhere. It finds the feature by the `id` in the record it is
given, and keeps two things: the feature's **position** in the features array,
and the **storey** it stands on. That is the whole reason it exists; the
`remove_feature` + `add_feature` pair it replaces reorders the array, and
`add_feature` takes a `level`, so the pair could quietly move a fixture to
another floor.

**It writes the record whole.** A key the call does not name is a key the
feature no longer has — not a key it keeps. So a `set_feature` that means to
change a door's orientation must still name its `state`, and one that means to
keep a gate's `affects` must name the overlay again:

```json
{ "op": "set_feature", "feature": {
    "id": "sluice gate", "kind": "door", "at": [3, 2],
    "orientation": "vertical", "state": "closed",
    "affects": [ { "rect": [4, 1, 2, 3],
                   "terrain": { "closed": "floor", "open": "water" } } ] } }
```

Replacement is the choice because it makes the result a function of the call
alone: a merge would depend on state the call never mentions, and there is no
delete convention among the feature keys, so a fixture's `affects`, `requires`,
`trigger`, or `check` could never be cleared at all. The price is paid where it
is cheapest to notice — every merge-shaped call omits `kind` or `at`, so every
merge-shaped call is refused, and the refusal says which semantics it got.
`toggle_door` stays for the one-key case it was already good at: flipping a
door's recorded state, by square, without restating anything. For a linked
double door it updates both reciprocal leaves in the same atomic edit.

Both feature operations accept an overlay's squares as a `rect: [x, y, w, h]`
as well as a list of `cells`, and expand it to cells before the document is
written — the file keeps one shape, so a later `resize` translates and crops
those squares with the frame exactly as it does heights.

`to_level` is settable by both, which is what makes a connector authorable
without hand-editing the file. Which level it may name — one the map has, never
its own — stays the document's refusal, so a bad connector is reported by the
format rather than by the operation.

## The interactive editor

The editor is a page the engine serves, so starting it is starting the engine:

- **`fivee serve`** starts one and prints `editor_url` (the editor),
  `viewer_url` (the replay viewer) and `url` (the landing page that links to
  both), or reports the running one with `already_running` true.
  `fivee stop` shuts it down. Any other `fivee` command starts one too if
  nothing is serving — `serve` exists for when the URL is what you want.
- **`fivee-sim-server [--maps-dir DIR] [--port N]`** runs it in the foreground
  from the engine's own environment, for development.

**Which engine you are looking at.** The footer's right corner names the
serving engine's version, and keeps naming it — the status line beside it is
for the last thing that happened. It arrives in the injected launch
configuration rather than over `/api/v1/ping`, so it is on screen before any
request finishes; a page opened from disk has no server to have been told by,
and says nothing there rather than guessing. This is the cheapest way to see
that an install is serving the engine you think it is — the failure recorded
under "A venv also outlives the engine it was built from" in
[CLAUDE.md](../../CLAUDE.md) reached users precisely because a stale engine
looks identical to a fresh one from the outside.

**Token model.** The server binds `127.0.0.1` only and mints a fresh token per
launch. Every `/api/v1/*` request must carry it in `X-Fivee-Editor-Token`; the
token reaches the browser only by being injected into the served page, and it
is never put in a URL, so the URL alone is safe to hand around on the machine.
Requests with a foreign `Host` header are refused, which is what keeps a
DNS-rebinding page from driving the API.

**ETag semantics.** A map's identity is the sha256 of its canonical bytes, and
that hash is its `ETag`. `PUT /api/v1/maps/{id}` requires `If-Match`: the ETag
from your last `GET` to update, or `*` to create. A stale hash is a `409`
(someone saved in between — re-`GET` and reapply), a missing header is `428`,
and an invalid document is `422` carrying the same diagnostics the validator
prints. `POST /api/v1/maps/generate` **never persists** unless it is given
`save_as` — the page reviews the result and saves the keeper with `PUT`, which
is the same two steps `fivee map.generate` and `fivee map.put` are.

The listing at `GET /api/v1/maps` deliberately carries **no hash**. A guarded
write reads the version it is about to replace: `GET` the map, take the `ETag`
off that response, `PUT` with `If-Match`. A hash in the listing would invite a
write preconditioned on a version nobody read.

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

**Stack** prints the other storeys *through* the one being edited, so a floor
can be laid out against its neighbours instead of by memory — where the stair
head above lands, whether a shaft lines up, which walls disagree. Each ghosted
floor is washed in its own terrain colors, the nearer storey more strongly than
the one beyond it, and its doors, stairs and spawns are marked with the glyphs
the format reserves (`+` `<` `>` `@`) once the zoom can carry them. It reaches
two storeys either way, because past that the washes stop reading as a building;
a floor outside that reach is named as **not drawn** in the Storeys key beside
the map, which also lists what *is* ghosted and how strongly. Where a basement
and a gallery are equally far, the upper one prints over the lower.

The stack is a view and nothing more: it is off by default, every tool still
paints the selected storey alone, and a map saved with it on is byte-identical
to the same map saved with it off.

**Terrain color.** Each legend row's swatch is a color picker: change it and
that terrain kind is colored in the document itself, for this map everywhere it
is drawn — every storey included, since one palette serves the document as one
legend does. A row with a color of its own grows a `×` that drops it back to the
theme's. On a map whose file carries a `{light, dark}` pair, the picker speaks
only for the theme the page is showing and leaves the other half alone.

**Fixtures.** The side panel lists every fixture on the storey being edited —
every feature carrying a `state` — with a checkbox each, and the Preview toggle
draws the map as if the ticked ones stood open: their own squares and every
square their overlays govern take the terrain of that state, and a ticked door
draws open. It is a lens and nothing else. It writes nothing to the document,
never marks it dirty, and a save after previewing does not stamp the map
`edited`. Reopening a map switches the lens off and forgets the ticks, because a
preview carried across an open would draw the new map through the old one's
fixtures. Selecting a fixture shows what it carries — its terrain and height
pairs, how many squares it governs, what it requires, what it costs and what it
rolls, and its authored trigger. Selecting a door also exposes orientation,
hinge, opening side, and a linked-door selector. Only an adjacent compatible
leaf is offered; linking writes both reciprocal records and assigns the outer
hinges, while unlinking clears both records. Previewing either linked leaf
previews both. The editor previews authored or selected live states but does not
execute triggers; the encounter remains the owner of automation.

**Light and openings.** Ambient light is selected per storey. Selecting any
feature exposes its visible-level list and optional bright/dim light ranges and
color; the Feature tool can place purpose-labelled `opening` and `light` glyphs.
The renderer applies ambient dimness or darkness and marks light sources.
Encounters use those same fields for Darkvision, Blindsight, and unseen-target
attack modifiers, so the document, editor, replay, and fight read one source.

The preview shows **terrain only**. The Heights overlay reads the storey's own
height layer, so a fixture that drops a water level five feet recolors the room
without re-shading it, and the cursor readout reports the authored height there.
A fight is the authority on that: `encounter.state` reports a creature's live
elevation, and it is the number that governs movement.

After saving in the editor, the file has moved on from whatever you last read.
There is no session copy to refresh — a map **is** the file, and every operation
that names one by id reads it fresh — but a hash you are holding is stale, so
`GET` the map again before the next guarded write.

## The replay bundle

`encounter.replay` turns an encounter into a portable, self-contained audit
record.
Version 2 is the default:

```json
{
  "format": "fivee-sim-replay",
  "format_version": 2,
  "name": "guard room",
  "seed": 71203941,
  "map": { "...": "a fivee-sim-map payload, or null" },
  "encounter": { "id": "enc-1", "seed": 71203941,
    "movement_rule": "5-5-5" },
  "initial": { "creatures": [], "combatants": [], "state": {},
    "map_open_features": ["door-east"] },
  "events": [ { "kind": "round", "timestamp": "...", "...": "..." } ],
  "actions": [],
  "attempts": [],
  "checkpoints": [ { "event_count": 2, "state_hash": "...", "state": {} } ],
  "latest_state": {},
  "content": { "records": {}, "sha256": "..." },
  "integrity": { "algorithm": "sha256", "events": "...", "...": "..." }
}
```

`map` is the document **as the fight captured it** — an edit made after
`encounter.create` never changes an export. Version 2 also converts an inline
`map` spec to a complete map document and preserves every storey; only a mapless
fight carries `null`. `initial.combatants` is the normalized creation input,
including attacks and resources, while the captured content records preserve
the spells, conditions, items, and terrain the fight resolved against.

Every event has a wall-clock timestamp. Successful actions and advances are in
`actions`; `attempts` also carries refused actions, encounter-scoped rolls,
checks, saves, and `encounter_note` entries. Checkpoints hold authoritative full
state after creation and after every state-changing call, each with its own hash.
The top-level integrity block hashes the map, initial state, events, actions,
checkpoints, latest state, and content. `replay_validate` checks the nested
schema and all hashes, and the viewer performs the same checks before rendering
a dropped or embedded v2 file. These hashes make corruption and alteration
evident; they are not signatures and do not authenticate an author. Pass
`format_version=1` only when a legacy consumer needs the old seven-field bundle;
the viewer continues to accept both versions.

Encounters are journaled under the configured encounters root (the sibling
`.fivee-sim/encounters/` by default). Creation, attempt, and result records are
append-only, fsynced, and hash-chained. `request_id` makes creation, actions,
advances, and encounter-scoped primitives safe to retry. `encounter_list`
discovers active or finalized journals, `encounter_resume` recovers one after a
restart, and `encounter_finalize` writes its replay v2 file and marks it
finalized without deleting the journal. A partial crash tail is preserved beside
the journal; hash-chain tampering is refused.

The viewer replays what the fixtures did. `initial.map_open_features` says
which stood open when the fight began — for every fixture, not only doors — and
each `interact` event moves one, so stepping to the round the party opened the
sluice floods the rooms it governs and turns the wheel. Scrubbing back drains
them again: the viewer rebuilds from the start of the fight rather than undoing,
so any point in the log shows the ground as it was at that moment. A linked-door
event carries the other leaf in `data.linked`; the viewer folds both into the
same state at the same event. Replay map capture keeps fixture trigger
definitions, and automatic transitions arrive as the same `interact` events
with an empty actor, `automatic: true`, and `triggered_by`, so the viewer needs
no separate trigger executor. For v2 it also follows movement between storeys,
can pin a selected storey, applies authoritative checkpoints, shows full
combatant resources and conditions, and
places checks, notes, and refusals in the timestamped audit timeline.

Small bundles come back inline; larger ones (or any call with `path`) are
written to the configured replays root as `<name>-<seed>.json`. With `embed`
true the bundle is baked into the replay viewer instead, yielding a single
self-contained `.html` — open it in any browser, no server required. The
viewer is also served live at `/viewer` by the engine process, where it takes
a dropped bundle file. JSON and HTML results both report the SHA-256 of the
exact bytes written, and replacement is atomic.

To inspect the format without first playing an encounter, generate the shipped
v2 showcase from the repository root:

```bash
uv run --project souroldgeezer-fivee-sim/engine fivee-sim-replay-sample \
  --output .fivee-sim/replays/showcase.html
```

The command writes one offline HTML file containing a two-storey fight, full
state checkpoints, a Persuasion check, a playtest note, a refused action, and
the integrity metadata the viewer verifies before playback. The refused action
appears during the fight; the replay continues through the second-round victory
and ends on the recorded outcome.

## Universal VTT export

`map.uvtt` writes a map as a Universal VTT JSON file (`format:
0.3`) — the interchange format other virtual tabletops import — at
`<maps root>/uvtt/<slug-of-name>.uvtt` by default. The result is always a
file, never inlined (the payload embeds a base64 image), and an existing
file at the target is replaced without asking: like replay files, the export
is derived from the session's map, not an original.

What is exported:

- **Walls** (`line_of_sight`): polylines in grid-square units, derived from
  the terrain — every interior cell-side where an opaque terrain kind meets a
  non-opaque one becomes a unit edge, and the edges are chained and merged
  into runs, deterministically. Door squares are ordinary floor in `tiles`,
  so wall runs break at doorways by construction. The map boundary emits
  nothing: out of bounds counts as opaque, so a wall run along the border
  contributes only its interior-facing edge.
- **Portals**: one per door feature, ordered by feature id, spanning the
  door's square along its orientation, `closed` taken from the recorded
  default state or from `open_features` below. Naming either linked leaf open
  exports both portals open. Universal VTT has no hinge/swing fields, so that
  drawing metadata does not travel in this export.
- **Image**: a flat-color PNG of the terrain at `pixels_per_grid` pixels per
  square (default 32), one fill per terrain kind plus a one-pixel grid line
  between cells. Some importers require an image; `include_image: false`
  writes `"image": ""` instead, deliberately. A kind the document's own
  `palette` colors is exported in that color — its `light` half, the PNG having
  exactly one theme. Everything else is engine policy: bundled kinds have fixed
  colors, and an uncolored pack-defined kind gets a deterministic hue hashed
  from its name using the same fallback formula the editor renderer documents.
  Pixel parity with the themed canvas is not promised.

`map.uvtt` takes a `level` (default the ground). The format has one plane
and no notion of storeys, so a map with floors exports one file per floor
rather than a flattened picture true of neither.

`open_features` names the fixtures to export **as open** — a fight's live set,
which `encounter.state`'s map block reports. Given it, the walls, the image and
the portals all show the map that fight is on rather than the map on disk: a
raised portcullis stops being a wall, a sluice's flooded room exports as water,
and a door the party opened exports as an open portal. Omit it and the export is
the map exactly as the file has it — omitting it and passing an empty list are
different answers, because `[]` says every fixture is shut and so shuts one the
document authored open.

One square never changes either way: **a door's own**. A door travels here as a
portal, and a portal buried in solid wall is a door the importer cannot open —
so the tile under a door is what both the walls and the image read, whatever
state the door is in. What a door reaches *past* itself is spared nothing: a
sluice gate is a door whose overlay floods a room, and that room resolves like
any other fixture's.

Authored light sources export through `lights`, and the selected plane's ambient
state exports through `environment.ambient_light`. `objects_line_of_sight`
remains empty, and there is no elevation in the format. An overland map typically has no opaque
kinds at all, so it exports an image and an empty `line_of_sight` — correct,
not a bug.

The image side is capped at 4096 pixels: `width × pixels_per_grid` and
`height × pixels_per_grid` must both fit, and the refusal names the largest
`pixels_per_grid` that would (every valid document fits at 8 or fewer).

The exporter is implemented defensively from the publicly documented shape
of the format — the JSON keys importers read — with no code ported from any
tool that produces or consumes it, and nothing beyond the standard library.
