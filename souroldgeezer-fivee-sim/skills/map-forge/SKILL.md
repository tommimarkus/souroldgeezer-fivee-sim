---
name: map-forge
description: Use when creating, editing, or managing battle maps for 5E-compatible combat — generating dungeon, cave, or overland maps under a seed, rendering and verbally tweaking them, saving and re-loading map files, launching the interactive browser editor for hand-tuning, putting a fight on a loaded map, exporting a finished fight as a shareable replay, or handing a map to another virtual tabletop via Universal VTT export. Drives the souroldgeezer-fivee-sim MCP engine's map and replay tools; running the fight itself belongs to the encounter-sim skill.
---

# Map Forge

Battle maps as first-class documents: generated under a seed, tweaked verbally
or by hand, saved as JSON, fought on, and replayed. The engine owns every map
in the session; your job is to drive the tools and keep straight which copy —
session or file — is currently the truth.

## The workflow

1. **`map_generate`** — `kind` is `dungeon`, `caves`, or `overland`; `params`
   overrides that kind's defaults (an unknown key is refused with the valid
   list). **The seed is always reported — quote it.** The same kind, params,
   and seed reproduce the map exactly, and the document's provenance records
   all three, so a map is never an unrepeatable accident.
2. **`map_render`** to look at it. Respect the viewport discipline: a render
   over 10 000 cells is refused, so view a large map through `x`/`y`/`width`/
   `height` viewports or a `downsample` factor rather than asking for the
   whole thing. Overlay glyphs: `+` closed door, `/` open door, `<` `>`
   stairs, `@` spawn. Pass `show_elevation` to get the ground heights back as
   a second set of rows, lettered from the lowest ground in view upward with a
   legend giving each its feet.
3. **Verbal tweaks are `map_edit`** — the user says "wall off the north
   passage" and you translate it into operations: `set_terrain` (a rect),
   `paint` (cells), `line`, `carve_corridor`, `add_feature`,
   `remove_feature`, `toggle_door`, `resize`, `set_legend`, `set_name`,
   `set_palette`, `set_elevation`, `adjust_elevation`. The whole list applies
   atomically; a bad operation names its index and changes nothing. Render the
   result back so the user sees what changed.
4. **`map_save`** writes canonical JSON and refuses to overwrite unless told
   to. Quote the path and the sha256.
5. **Hand-tuning: `map_editor_serve`** starts the interactive editor and
   returns its URL — hand that URL to the user to open in a browser. The
   served page configures its own access token; there is nothing else to
   pass along. `map_editor_stop` shuts it down.
6. **After GUI edits, `map_load` before further use.** Once hand-edited, the
   file is the source of truth — re-load, never assume. When the user says
   they have saved in the editor, call `map_load` with `path` and `replace`
   set to the existing map id, which re-reads the file and bumps the
   session's generation. The session copy you rendered before their edits is
   stale the moment they save.
7. **`encounter_create(map_id=...)`** puts a fight on the loaded map (and
   `simulate_rounds` accepts the same). The fight captures the document by
   value: a later `map_edit` never reaches into it, and
   `encounter_state["map_source"].stale` turning true means the live map has
   moved on — re-create the encounter when the new layout should apply.
   Running the fight is the encounter-sim skill's ground.
8. **After the fight, `replay_export`.** It bundles the seed, the captured
   map, the starting roster, and the whole event log. For a file to hand the
   user, call it with `embed` true: the result is a single self-contained
   HTML page that plays the fight back in any browser — no server, no
   install. Report the written path; small plain bundles come back inline
   instead.
9. **Hand a map to another virtual tabletop with `uvtt_export`.** It writes
   the loaded map as a Universal VTT JSON file (default
   `<maps root>/uvtt/<slug>.uvtt`, replaced on re-export) carrying wall
   polylines derived from the tiles, one portal per door, and a rendered PNG
   of the map — always a file, never inline; quote the path. Lights and
   elevation are not exported: the format has no place for the ground heights
   the engine now models, and nothing here models lights. The
   image side is capped at 4096 pixels: lower `pixels_per_grid` for very
   large maps, or pass `include_image` false when the importer does not need
   the picture.

## Seeds and reproducibility

Every generating tool reports the seed it used, whether or not one was given.
Always repeat it to the user next to the result ("dungeon, seed 71203941"),
because the seed plus the recorded params is the map's identity — provenance
keeps both, and a hand-edited map keeps them too, plus an `edited` flag
marking that the file has diverged from what the generator would produce.

## Terrain and content

Terrain kinds are strings resolved against the loaded content, exactly like
conditions: the built-in table covers `floor`, `wall`, `difficult`, `water`,
`plain`, `forest`, `hill`, `mountain`, cover kinds, and doors, and a content
pack may define more. `lookup_rule` on a terrain name reports its effects;
`content_status` says what is loaded. A document's legend maps single
characters to kinds, and the glyphs `+` `/` `<` `>` `@` are reserved for
overlays — a legend claiming one is refused.

A kind draws in a color the renderers compute, which for a pack-defined kind is
a hue hashed from its name. When the user wants a different one — "make the lava
orange", "this is a snow map" — `set_palette` writes it into the document:
`{"op": "set_palette", "terrain": "lava", "color": "#d2440f"}`, a
`{"light": ..., "dark": ...}` object where the two themes should differ, or
`"color": null` to drop back to the computed color. Hex only, `#rgb` or
`#rrggbb`. The color then travels with the map — canvas, replay viewer, and the
image a UVTT export carries — and a kind need not be on the map to be colored.

## Files

Maps live at `$CLAUDE_PROJECT_DIR/.fivee-sim/maps/` by default, or wherever
`FIVEE_SIM_MAPS` points; replays are written under `replays/` beside them.
`map_query` answers distance, line-of-sight, and path questions over a bare
map without starting a fight.

## Ground height

A map may carry an elevation in feet per square, painted with `set_elevation`
(a rect, named cells, or `default` to move the height every unnamed square sits
at) and `adjust_elevation` (raise or lower what is already there). Negative
feet are ground below the map's datum — a pit floor, a sunken chamber.

**Say what it does when you use it**, because the half that is missing is the
half a user will assume. Height is charged to *movement* alone: a rise of a
couple of feet across a square is a slope, which costs difficult terrain; a
rise over five feet is a cliff face, climbed at an extra foot per foot, and
climbing down costs the same. Sight, cover, and area templates are measured
flat, so a ridge blocks nothing and standing on a tower is no advantage in
itself. An overland map is generated with relief already — every square its own
height, so a mountain stands above the hill beside it; tune it with the
`relief_feet` and `water_depth_feet` params, or set both to zero for flat open
country. Dungeons and caves generate flat, and height there is something you or
the user adds — with these operations, or painted by hand in the browser
editor's Height tool (step 5).

## Storeys

A map may carry more than one floor over the same footprint. Level `0` is the
ground and is always there; `levels` in the document holds the storeys above
and below it, with signed indices so a basement is `-1`. A level has its own
tiles, features, and heights, and its `elevation.default` is the height its
floor sits at.

Every op that acts on one floor takes a `level` (default the ground), as do
`map_render`, `map_query`, and `uvtt_export` — the last exports one file per
floor, because the format has no notion of storeys. `set_name`, `set_legend`,
and `resize` take none: they are document-wide, and a resize moves every floor
together.

In the browser editor the Level control picks the floor being edited, and
**Stack** ghosts the storeys either side of it through the one on screen — the
way to line a stair head up with the stair foot below without editing blind.

A stairway becomes walkable when its feature carries `to_level`. **Say what a
floor does**, as with height: a floor is opaque, so creatures on different
levels cannot see, target, or threaten each other at all, and a move between
them ends on a connector square and pays the climb. Routing is per level — ask
for the walk to the stairs and the crossing as separate legs, because the
pathfinder will not plan a route that takes the stairs on the way.

## Fixtures the fight can operate

A feature that carries a `state` is a **fixture** — something a fight can work
mid-combat. A door is the ordinary case; a lever, a spike, or a sluice gate is
the same record with more on it. A feature without a state is just an
annotation: a drawn stairway or a spawn hint, which no fight ever operates.

Six optional keys are what a fixture may carry, all of them needing that
`state`: `terrain` and `elevation` pairs for its own square, `affects` naming
further squares and what they become in each state, `requires` naming fixtures
that must stand open first, `costs_action`, and `check` (`{"ability", "dc"}`).
Build one with `add_feature`, whose overlay squares may be given as a `rect`
rather than listed cell by cell. There is no operation for editing overlays —
`remove_feature` plus `add_feature` in one atomic call replaces the fixture.

**Say what a fixture does — and what it costs — before the party commits**,
because the half that is missing is the half a user will assume:

- **Price the whole chain out loud.** Pulling two spikes and opening the gate is
  **three actions**, not one flourish: any fixture with `costs_action` spends
  the action rather than the free object interaction, so three of them is three
  turns unless three creatures split the work. A failed check spends the action
  and moves nothing, so budget for retries.
- **The check is a raw ability check.** Creatures have ability modifiers and no
  skill proficiencies anywhere in this engine — no Athletics, no proficiency
  bonus, no Expertise, no Help. **Set the DC as if the character were
  untrained**, and say so when the user pitches one at a trained bonus.
- **`requires` gates opening only.** Closing is never blocked, so a gate that
  opened can always be shut again even with the spikes back in.
- **The ground changes live, under whoever is standing there.** Terrain and
  ground height both move the instant a fixture flips. A creature standing where
  the footing turns impassable is *not* pushed anywhere — entry cost governs
  entering a square, not remaining in one, and this engine models no forced
  movement. It stays put and may walk out. Say that plainly rather than letting
  the user expect a shove.
- **One fixture per square.** Every square a fixture governs — its own and every
  overlay cell — belongs to exactly one fixture per level, refused at load
  otherwise. Two floods cannot share a room; combine them into one fixture's
  overlay groups instead.

When the fight is running, drive a fixture with
`encounter_act(kind="interact", feature=..., set_open=true|false)`. Use
`set_open` whenever you are working a chain: `interact` on its own **toggles**,
so telling the engine to "open the sluice" when it already stands open silently
closes it. Running the fight is the encounter-sim skill's ground.

For the document format, the editor's API model, and the replay bundle
schema, read [`../../docs/MAPS.md`](../../docs/MAPS.md).
