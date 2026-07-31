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
   `set_elevation`, `adjust_elevation`. The whole list applies atomically; a
   bad operation names its index and changes nothing. Render the result back so
   the user sees what changed.
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
itself. Generated maps are flat; height is something you or the user adds —
with these operations, or painted by hand in the browser editor's Height tool
(step 5).

For the document format, the editor's API model, and the replay bundle
schema, read [`../../docs/MAPS.md`](../../docs/MAPS.md).
