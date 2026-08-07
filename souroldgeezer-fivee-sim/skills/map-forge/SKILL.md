---
name: map-forge
description: Use when creating, editing, or managing battle maps for 5E-compatible combat — generating dungeon, cave, or overland maps under a seed, rendering and verbally tweaking them, saving map files and reading them back, launching the interactive browser editor for hand-tuning, putting a fight on a saved map, exporting a finished fight as a shareable replay, composing a whole adventure's finalized chapters — fights and interludes alike — into one replay bundle, or handing a map to another virtual tabletop via Universal VTT export. Drives the souroldgeezer-fivee-sim engine's map and replay operations with the bundled `fivee` command; running the fight itself belongs to the encounter-sim skill.
---

# Map Forge

Battle maps as first-class documents: generated under a seed, tweaked verbally or
by hand, written to disk as JSON, fought on, and replayed.

**A map is a file, and its id is its filename.** There is no in-memory session
copy to keep in step with disk — `fivee --run <adv-id> map.list` names that
run's overlay, and every other operation takes the id and reads the file. Two
servers bound to the same run therefore cannot disagree about a map; servers
bound to different runs are isolated deliberately. What *can* go stale is a
hash you are holding, which is what the guarded write below is about.

## Running the command

Everything below is a Bash call to the absolute launcher: `python3
<skill dir>/../../scripts/fivee.py`, where `<skill dir>` is the directory the
harness named when it loaded this skill. Resolve that once, against the
announced directory, into an absolute path and reuse it for every call —
nothing expands a `${...}` placeholder in this prose. Use this form always: a
bare `fivee` on `PATH` or a path relative to the working directory will not
match the Bash grant an `encounter-sim` or `game-master` agent profile holds.

```bash
echo "python3 <skill dir>/../../scripts/fivee.py"
```

Nothing has to be started: every command finds the engine's local server or starts
one. `fivee help` lists every operation and `fivee help map.put` gives one its
arguments and a line to paste, both read off the running server. Results are JSON
on stdout and nothing else; prose, refusals, and the `etag` note go to stderr.

A whole document goes in `--json '{...}'` or `--json -` from stdin. That is how a
map is written, because no flag grammar should try to spell one.

## Select the adventure run

Maps are mutable run artifacts, including map-only work. Start the workspace
without a selector, then carry the returned id on every later command:

```bash
fivee adventure.create --name "Map work"             # returns adv-1
fivee --run adv-1 map.generate --kind dungeon --save-as first-draft
```

The general form is **`fivee --run <adv-id> ...`**. Project maps, scenes, and
replays are shared read-only overlay inputs; a run-local id wins. A guarded edit
of a shared document is copy-on-write into the run, so the shared bytes do not
change. An unscoped command may inspect shared inputs but refuses a write, and
`--run legacy` is explicit read-only access to the pre-run stores. There is no
publish or promote operation: run artifacts stay inside the run unless an
export explicitly names another path.

## The workflow

1. **`fivee --run <adv-id> map.generate --kind dungeon`** — `--kind` is `dungeon`, `caves`, or
   `overland`; `--params` overrides that kind's defaults (an unknown key is refused
   with the valid list). **The seed is always reported — quote it.** The same kind,
   params, and seed reproduce the map exactly, and the document's provenance
   records all three, so a map is never an unrepeatable accident.

   It returns the whole document **unsaved**, so you can look before you keep it.
   Add `--save-as <id>` to write it under that id in the same call; an id already
   in use is refused rather than replaced.

   ```bash
   fivee --run adv-1 map.generate --kind dungeon --seed 71203941 --params '{"width": 40, "height": 30}'
   fivee --run adv-1 map.generate --kind caves --save-as goblin-warren
   ```
2. **`fivee --run <adv-id> map.render`** to look at it — `--map-id <id>` for a saved map or
   `--json '{"document": {...}}'` for one you have not kept. Respect the viewport
   discipline: a render over 10 000 cells is refused, so view a large map through
   `--x`/`--y`/`--width`/`--height` or a `--downsample` factor rather than asking
   for the whole thing. Overlay glyphs: `+` closed door, `/` open door, `<` `>`
   stairs, `@` spawn. Pass `--show-elevation` to get the ground heights back as a
   second set of rows, lettered from the lowest ground in view upward with a legend
   giving each its feet.
3. **Verbal tweaks are `fivee --run <adv-id> map.edit`** — the user says "wall off the north
   passage" and you translate it into operations: `set_terrain` (a rect), `paint`
   (cells), `line`, `carve_corridor`, `add_feature`, `set_feature`,
   `remove_feature`, `toggle_door`, `resize`, `set_legend`, `set_name`,
   `set_palette`, `set_elevation`, `adjust_elevation`. The whole list applies
   atomically; a bad operation names its index and changes nothing. Render the
   result back so the user sees what changed.

   ```bash
   fivee --run adv-1 map.edit goblin-warren --json '{"operations": [
     {"op": "set_terrain", "rect": [4, 2, 6, 1], "terrain": "wall"}
   ]}'
   ```

   Changing an existing feature is `set_feature`, not a remove-and-re-add: it edits
   the feature its record's `id` names, in place and on the storey it already
   stands on. It **writes the record whole**, so restate every field the feature is
   to keep — a key left out is a key removed. For a door's open/closed state alone,
   `toggle_door` is still the shortest thing that works. A linked double door names
   its adjacent mate reciprocally with `linked_to`; both leaves must share state and
   interaction contract, and toggling either moves both. `to_level` on a feature
   makes it a connector between storeys.
4. **A guarded write is two calls: `map.get`, then `map.put --if-match`.**
   `fivee --run <adv-id> map.get <id>` returns the document and reports its sha256 as an `etag`
   line on stderr. `fivee --run <adv-id> map.put <id> --if-match <that etag> --json -` writes the
   new bytes, and is refused with a 409 if another session or the open editor got
   there first. That is not a retry: `map.get` again, reapply your change to the
   version actually on disk, and put again.

   ```bash
   fivee --run adv-1 map.get goblin-warren > /tmp/warren.json     # keep etag
   fivee --run adv-1 map.put goblin-warren --if-match <etag> --json - < /tmp/warren-edited.json
   ```

   **The listing carries no hash on purpose**, so the version a write is
   preconditioned on is always one somebody actually read. `--if-match '*'` creates
   a new id, or takes an existing file over deliberately — say so when you do.
5. **Hand-tuning: `fivee --run <adv-id> serve`** starts the run-bound engine and prints three URLs —
   `editor_url` for the map editor, `viewer_url` for the replay viewer (step 8),
   and `url` for the landing page that links to both. Hand the user whichever
   they asked for, and **`editor_url` when they asked for the editor**: `url` is
   the index, not the editor. **Pass the URL exactly as printed, `#` and all** —
   the fragment is this launch's access token, and it is the only way the page
   gets one. Trimmed to the path, the page opens and the engine refuses every
   request it makes. There is nothing else to pass along. `fivee --run <adv-id> stop` shuts
   them down. If a server is
   already up, `serve` reports it with `already_running` true rather than
   starting a second.

   The editor page also has a **Play mode**: the user can load content, place a
   roster on the map they are drawing, and fight on it without leaving the page —
   as the whole table, or from one creature's seat. Offer it when someone wants to
   see how a map plays rather than how it looks. Running a fight *for* them is
   still the encounter-sim skill's job over `fivee`; Play mode is the user driving
   it themselves.
6. **After GUI edits, re-read before writing.** The file is the truth, and every
   operation that names a map by id already reads it fresh — so a render or a fight
   started after their save sees their work with nothing to reload. What is stale is
   any `etag` you were holding: `map.get` again before the next guarded write, or
   the put will be refused for a version the user has moved past.
7. **`fivee --run <adv-id> adventure.encounter <adv-id> --map-id <id>`** puts a
   fight on the saved map (and run-scoped `analytics.rounds` accepts the same).
   The fight captures the document by value: a
   later `map.edit` never reaches into it, and `encounter.state`'s
   `map_source.stale` turning true means the file has moved on — re-create the
   encounter when the new layout should apply. Running the fight is the
   encounter-sim skill's ground.
8. **After the fight, `fivee --run <adv-id> encounter.finalize <id>` or `fivee --run <adv-id> encounter.replay
   <id>`.** Finalization writes replay v3 and retains the encounter's hash-chained
   journal. A direct export defaults to the same v3 contract: the seed, normalized
   roster, captured content, the captured map (including inline maps and every
   storey), timestamped events and attempts, state checkpoints — the first whole
   and each later one as what moved since the one before it — and integrity
   hashes. Bundles land in the configured **replays root**, independently
   of the maps root.

   Two ways to show a fight, and they answer different asks. If a server is
   running, a written bundle comes back with a `viewer_url` — hand that over whole,
   fragment included, and it plays in the browser they already have open. For a
   file to give someone who is
   *not* at this machine, call `encounter.replay` with `--embed`: the result is a
   single self-contained HTML page that plays the fight back in any browser — no
   server, no install. Report the written path and SHA-256; small plain bundles come
   back inline instead. Use `fivee --run <adv-id> replay.validate` before accepting a bundle from
   elsewhere; the viewer also checks the nested schema and hashes before rendering.
   The check is structural — glyphs, rows, and feature placement against the
   embedded map's own grid — not a full parse: a map's terrain kinds are resolved
   against loaded content when a fight loads it, not at validation.
   Integrity hashes detect alteration but do not authenticate the file's author.
   Request `--format-version 1` only for a legacy consumer. `fivee --run <adv-id> replay.list` and
   `fivee --run <adv-id> replay.get <id>` read what is already written. If the fight is one chapter
   of an adventure, finalizing it is also the precondition for composing the whole
   run — see "A whole adventure as one replay" below.
9. **Hand a map to another virtual tabletop with `fivee --run <adv-id> map.uvtt`.** It writes the
   map as a Universal VTT JSON file (default `<maps root>/uvtt/<slug>.uvtt`,
   replaced on re-export) carrying wall polylines derived from the terrain, one
   portal per door, and a rendered PNG of the map — always a file, never inline;
   quote the path. To hand over the map a fight is on rather than the map on disk,
   pass `--open-features` with the fixtures standing open (`encounter.state`'s map
   block lists them): a raised portcullis stops being a wall and a flooded room
   exports as water. Authored ambient light and feature light sources are exported.
   Elevation is not: the format has no place for the ground heights the engine
   models. The image side is capped at 4096 pixels: lower `--pixels-per-grid` for
   very large maps, or pass `--include-image false` when the importer does not need
   the picture.

`fivee --run <adv-id> map.validate` reports a document's errors and warnings without writing
anything — worth a call before a `map.put` you expect to be marginal.

## A whole adventure as one replay

When the user asks to compose or validate an adventure replay, read
[`references/adventure-replay.md`](references/adventure-replay.md) before acting.
Do not load it for a single map or single-encounter replay.

## Seeds and reproducibility

Every generating operation reports the seed it used, whether or not one was given.
Always repeat it to the user next to the result ("dungeon, seed 71203941"),
because the seed plus the recorded params is the map's identity — provenance keeps
both, and a hand-edited map keeps them too, plus an `edited` flag marking that the
file has diverged from what the generator would produce.

## Terrain and content

Terrain kinds are strings resolved against the loaded content, exactly like
conditions: the built-in table covers `floor`, `wall`, `difficult`, `water`,
`plain`, `forest`, `hill`, `mountain`, cover kinds, and doors, and a content pack
may define more. `fivee rules.lookup --topic <terrain>` reports its effects;
`fivee content.status` says what is loaded. A document's legend maps single
characters to kinds, and the glyphs `+` `/` `<` `>` `@` are reserved for overlays
— a legend claiming one is refused.

A kind draws in a color the renderers compute, which for a pack-defined kind is a
hue hashed from its name. When the user wants a different one — "make the lava
orange", "this is a snow map" — `set_palette` writes it into the document:
`{"op": "set_palette", "terrain": "lava", "color": "#d2440f"}`, a
`{"light": ..., "dark": ...}` object where the two themes should differ, or
`"color": null` to drop back to the computed color. Hex only, `#rgb` or `#rrggbb`.
The color then travels with the map — canvas, replay viewer, and the image a UVTT
export carries — and a kind need not be on the map to be colored.

## Files

The CLI walks upward from the invocation workspace and uses the nearest
`.fivee-sim/config.toml`; pass global `--config PATH` before the operation to
select another one. `[storage].maps` and `[storage].replays` each take one path or
an array of paths, while `scenes`, `encounters`, `adventures`, `blobs`, and
`runs` take one path. In TOML the new workspace setting is `runs = "runs"`.
Relative paths resolve against the `.fivee-sim/` directory containing the file.
Omitted values default to its sibling `maps/`, `replays/`, `scenes/`,
`encounters/`, `adventures/`, `blobs/`, and `runs/` directories.

Configured maps, scenes, and replays are shared overlay inputs. The selected
`runs/<adv-id>/` workspace receives writes and exports. `fivee content.status`
names the configuration source and path;
`fivee --run <adv-id> serve` and `fivee --run <adv-id> server.ping` report the resolved roots — read them rather
than assuming. A selected file owns these settings. Deprecated legacy `FIVEE_SIM_PROJECT_DIR`,
`FIVEE_SIM_MAPS`, `FIVEE_SIM_REPLAYS`, `FIVEE_SIM_SCENES`, `FIVEE_SIM_ENCOUNTERS`,
`FIVEE_SIM_ADVENTURES`, `FIVEE_SIM_BLOBS`, and `FIVEE_SIM_RUNS` remain compatibility fallbacks only
when no configuration file is selected.

`fivee --run <adv-id> map.query` answers distance, line-of-sight, and path questions over a bare
map without starting a fight.

## Ground height

A map may carry an elevation in feet per square, painted with `set_elevation` (a
rect, named cells, or `default` to move the height every unnamed square sits at)
and `adjust_elevation` (raise or lower what is already there). Negative feet are
ground below the map's datum — a pit floor, a sunken chamber.

**Say what it does when you use it**, because the half that is missing is the half
a user will assume. Height is charged to *movement* alone: a rise of a couple of
feet across a square is a slope, which costs difficult terrain; a rise over five
feet is a cliff face, climbed at an extra foot per foot, and climbing down costs
the same. Sight, cover, and area templates are measured flat, so a ridge blocks
nothing and standing on a tower is no advantage in itself. An overland map is
generated with relief already — every square its own height, so a mountain stands
above the hill beside it; tune it with the `relief_feet` and `water_depth_feet`
params, or set both to zero for flat open country. Dungeons and caves generate
flat, and height there is something you or the user adds — with these operations,
or painted by hand in the browser editor's Height tool (step 5).

## Storeys

A map may carry more than one floor over the same footprint. Level `0` is the
ground and is always there; `levels` in the document holds the storeys above and
below it, with signed indices so a basement is `-1`. A level has its own tiles,
features, and heights, and its `elevation.default` is the height its floor sits at.

Every op that acts on one floor takes a `level` (default the ground), as do
`map.render`, `map.query`, and `map.uvtt` — the last exports one file per floor,
because the format has no notion of storeys. `set_name`, `set_legend`,
`set_palette` and `resize` take none: they are document-wide, and a resize moves
every floor together. `set_feature` takes none either, for the opposite reason —
it edits the feature wherever it stands, so it can never move one between floors.

In the browser editor the Level control picks the floor being edited, and **Stack**
ghosts the storeys either side of it through the one on screen — the way to line a
stair head up with the stair foot below without editing blind.

A stairway becomes walkable when its feature carries `to_level`, which
`add_feature` and `set_feature` both write — a stairway drawn without one is a
glyph nobody can climb. **Say what a floor does**, as with height: a floor is
opaque unless a feature carries `sight_to_levels` naming the other plane. Sight and
attacks cross through that opening; area effects do not. A move between levels ends
on a connector square and pays the climb, unless it explicitly uses a Fly speed.
Routing is per level — ask for the walk to the stairs and the crossing as separate
legs, because the pathfinder will not plan a route that takes the stairs on the way.

Each plane may set `ambient_light` to `bright`, `dim`, or `darkness`, and any
feature may carry `light: {bright, dim, color}`. The editor exposes both plus
purpose-labelled opening and light glyphs. These fields affect combat: Darkvision
and Blindsight consult them, and UVTT export carries the ambient state and sources.

## Fixtures the fight can operate

A feature that carries a `state` is a **fixture** — something a fight can work
mid-combat. A door is the ordinary case; a lever, a spike, or a sluice gate is the
same record with more on it. A feature without a state is just an annotation: a
drawn stairway or a spawn hint, which no fight ever operates.

Seven optional keys are what a fixture may carry, all of them needing that
`state`: `terrain` and `elevation` pairs for its own square, `affects` naming
further squares and what they become in each state, `requires` naming fixtures that
must stand open first, one `trigger`, `costs_action`, and `check`
(`{"ability", "dc"}`). A trigger is target-local:
`{"when": {"lever": "open"}, "set": "open", "mode": "edge"}`; mode is either `edge`
or `maintained`. `when` is a non-empty AND predicate. References must be stateful,
dependencies acyclic, and linked leaves must carry identical triggers. An opening
trigger must include every `requires` fixture as open. Build one with `add_feature`,
whose overlay squares may be given as a `rect` rather than listed cell by cell, and
change one with `set_feature`, which takes the same record and edits in place the
fixture its `id` names. `set_feature` writes the record whole, so restate every key
the fixture is to keep — dropping `affects` from the record is how a fixture stops
affecting anything.

The editor inspector shows a fixture's trigger but does not run it; Preview is an
authored/live-state lens. Trigger execution belongs to a running encounter. `edge`
fires only on false→true (not merely because the fight starts true) and rearms
after false. `maintained` holds its configured state while true, refuses a contrary
manual interaction before cost or check, and does not reverse when the predicate
becomes false. Automatic transitions bypass the target's reach, cost, and check,
then appear as ordinary `interact` events with an empty actor, `automatic: true`,
and `triggered_by`, so replays fold them normally.

A door may additionally carry its drawing mechanics: horizontal doors hinge
west/east and swing north/south; vertical doors hinge north/south and swing
west/east. Omitted fields preserve west/north for horizontal and north/west for
vertical. Two adjacent, aligned leaves become one double door only when each has
`linked_to` naming the other and both have the same authored state, `requires`,
`trigger`, `costs_action`, and `check`. One interaction and one check operate the
pair; hinge, swing, and terrain effects remain per leaf.

**Say what a fixture does — and what it costs — before the party commits**, because
the half that is missing is the half a user will assume:

- **Price the whole chain out loud.** Pulling two spikes and opening the gate is
  **three actions**, not one flourish: any fixture with `costs_action` spends the
  action rather than the free object interaction, so three of them is three turns
  unless three creatures split the work. A failed check spends the action and moves
  nothing, so budget for retries.
- **The check is a raw ability check.** Creatures have ability modifiers and no
  skill proficiencies anywhere in this engine — no Athletics, no proficiency bonus,
  no Expertise, no Help. **Set the DC as if the character were untrained**, and say
  so when the user pitches one at a trained bonus.
- **`requires` gates opening only.** Closing is never blocked, so a gate that opened
  can always be shut again even with the spikes back in.
- **The ground changes live, under whoever is standing there.** Terrain and ground
  height both move the instant a fixture flips. A creature standing where the
  footing turns impassable is *not* pushed anywhere — entry cost governs entering a
  square, not remaining in one, and this engine models no forced movement. It stays
  put and may walk out. Say that plainly rather than letting the user expect a shove.
- **One fixture per square.** Every square a fixture governs — its own and every
  overlay cell — belongs to exactly one fixture per level, refused at load
  otherwise. Two floods cannot share a room; combine them into one fixture's overlay
  groups instead.

When the fight is running, drive a fixture with `fivee --run <adv-id> encounter.act <id> --kind
interact --feature <id> --set-open true|false`. Use `--set-open` whenever you are
working a chain: `interact` on its own **toggles**, so telling the engine to "open
the sluice" when it already stands open silently closes it. Running the fight is
the encounter-sim skill's ground — including the fact that `encounter.act` now
answers with `state_delta` rather than `state` by default, so add `--view full`
if you are reading a fixture's new open/closed state straight off the response
instead of applying the delta. The map block is in both.

For the document format, the editor's API model, and the replay bundle schema,
read [`../../docs/MAPS.md`](../../docs/MAPS.md).
