---
name: encounter-sim
description: Use when running, narrating, or analysing 5E-compatible combat — starting a fight, resolving attacks, spells, movement, conditions, items, or death saves turn by turn, measuring a build's expected damage and a party's win rate over many seeded iterations, or loading a campaign's own creatures, spells, conditions and items as content packs. Drives the souroldgeezer-fivee-sim MCP engine, which owns the state; not for rules lookup outside combat or for character creation.
---

# Encounter Simulation

Run 5E-compatible combat through the `fivee_sim` MCP engine. The engine resolves
the rules and owns the state; your job is to drive it and narrate what it reports.

Bundled rules content is SRD 5.2 under CC-BY-4.0. See the plugin's `NOTICE`. A
campaign may load its own content as well — see "What is actually loaded" below.

## The one rule that matters

**Never state combat state from memory. Read it from the engine.**

Hit points, initiative order, conditions, remaining movement, spell slots, and
death saves all live in `encounter_state`. That is the authoritative view. If your
narration and `encounter_state` disagree, the state is right and you are wrong —
re-read it rather than reconciling from what you remember.

This is the entire reason the engine exists. A model tracking a fight in prose
drifts: hit points wander, a condition is forgotten a round later, a die roll
quietly favours the story. Delegating resolution removes that failure mode, and
narrating from memory puts it straight back.

## Running a fight

1. **`encounter_create`** with the combatants and a seed. Each is either a bundled
   stat block — `{"monster": "Goblin Warrior", "label": "Goblin A", "team":
   "monsters", "position": [15, 0]}` — or an explicit build with at least `name`,
   `team`, `ac`, `max_hp`, plus `attacks`. Labels must be unique; they identify
   combatants in every later call. A position is `[x, y]` in feet on a flat plane
   (a bare number still means feet along the x-axis), and `encounter_state`
   reports positions in the same `[x, y]` form. Diagonals cost 5 ft by default;
   pass `movement_rule: "5-10-5"` for the every-second-diagonal-costs-double
   variant.
2. **`encounter_state`** to see whose turn it is and what the situation is.
3. **`encounter_act`** for each action: `attack`, `cast`, `use_item`, `move`,
   `dash`, `disengage`, `dodge`, `stand` (up from Prone — half Speed in
   movement, no action). It returns the events it generated plus fresh state.
4. **`encounter_advance`** to end the turn. Death saves for dying creatures are
   rolled automatically at the start of their turn.
5. Repeat until `state["over"]` is true; `state["winner"]` names the surviving side.

Past events are never lost: **`encounter_log`** pages the whole history
(`since`/`limit`), each event stamped with its round and turn, plus the action
records that — with the reported seed — reproduce the fight exactly. Recap
earlier rounds from it rather than from memory; `encounter_state` stays the view
of *now*.

An illegal action is **refused with a reason** — out of reach, no slots left, no
attacks remaining, none of that potion left, speed 0 while Grappled. Read the
reason and adapt. Do not retry the same call hoping for a different answer, and do
not narrate the action as though it happened.

## Fighting on a map

`encounter_create` takes an optional `map` of 5-ft squares: `{"width", "height",
"rows": [".#..", ...], "legend": {".": "normal", "#": "wall"}, "features":
[{"name": "door", "square": [1, 1]}]}` — rows top-first, one character per
square. With a map the engine charges terrain for movement, routes moves around
walls and enemies (pass-through opportunity attacks apply), grades cover (+2/+5
to AC and to Dexterity saves against areas; total cover refuses the attack and
shelters a creature from an area entirely), and blocks sight. Positions snap to
square centres, and `state["map"]` reports dimensions and door state.

`encounter_act(kind="interact", feature="door")` opens or closes a door — free,
once per turn, from the feature's square or one next to it. `simulate_rounds`
accepts the same `map` and `movement_rule`, so batches fight on the terrain too.

## Maps

Six tools manage maps as first-class documents. **`map_generate`** builds a
dungeon, caves, or overland map under a seed (always reported — quote it);
**`map_render`** shows any of them as glyph rows, with `x`/`y`/`width`/`height`
viewports and `downsample` for big maps, `show_elevation` for the ground
heights as a second set of rows, and an `encounter_id` overlay that
letters the combatants; **`map_edit`** applies verbal tweaks atomically —
paint, line, carve_corridor, set_terrain, add/remove_feature, toggle_door,
resize, set_legend, set_name, set_elevation, adjust_elevation — a bad operation
names its index and changes nothing; **`map_save`** writes canonical JSON (refusing silent overwrites) and
**`map_load`** reads a file or inline document back, so the workflow is
generate → render → edit → save, and load by path next session.
**`map_query`** answers distance, line-of-sight, and pathing questions on a
bare map. `encounter_create(map_id=...)` and `simulate_rounds(map_id=...)` put
a fight on a loaded map; the fight captures the document as it stands, and
`encounter_state["map_source"].stale` turns true if the map is edited after —
re-create the encounter when the new layout should apply.

For the full map workflow — generation seeds, the interactive browser editor,
and exporting a fight as a shareable replay — use the **map-forge** skill.

## Aiming a spell

`cast` takes `target` for one creature, `targets` for several, or an area aim:
**`center`** for a sphere (or a cube's minimum corner) — an `[x, y]` point in
feet, not a creature — **`direction`** for a cone (one of the eight unit
offsets, such as `[1, 0]` or `[-1, 1]`), **`toward`** for a line (a combatant
name or a point). On a map, a sphere or cube also needs its point of origin in
the caster's sight.

`center` is what makes a Fireball a Fireball. Named targets are each hit
individually, so a 20-ft blast dropped with `target` catches exactly one creature
however tightly the enemy is packed. Give a point of origin instead and it catches
everything within its radius — **including allies and the caster**, which is a real
tactical cost and the engine will not protect you from it.

Range is checked against the point of origin for an area, and against each named
creature otherwise. A creature at the far edge of a blast can therefore sit past
the spell's range legitimately; the origin is what has to be reachable.

## Items

`encounter_act(kind="use_item", item="Potion of Healing")` spends the action.
Healing defaults to the user; a damaging or condition-applying item needs a
`target`, and any item used on another creature needs to be within 5 ft. Quantity
is the charge count, and `encounter_state` shows what each combatant has left.

No items ship in the bundled slice — they arrive through a content pack.

## Narrating well

Report what the engine actually rolled. Players trust a fight they can audit:

> Thora swings at the goblin — d20 [17] +5 = 22 vs AC 15, a hit. 1d8+3 → [6] +3 =
> 9 slashing. The goblin drops to 1 hit point.

The `detail` field of each event already contains the arithmetic. Use it. When an
attack had advantage or disadvantage, say which and why — the engine's condition
handling is the interesting part, and hiding it makes the fight feel arbitrary.

## Analysis rather than play

- **`simulate_rounds`** auto-plays the same encounter many times and reports win
  rates and how long fights last. Use it for "is this encounter too hard?"
- **`simulate_dpr`** measures damage a build lands over N rounds against a given
  AC, at a `distance` you choose (5 ft by default). Use it for "is this build
  actually better?"

Both replay the same stepper live play uses, so their numbers cannot drift from
the rules. Iteration `i` uses `seed + i`, so one iteration reproduces a single
hand-played fight at that seed — handy when a batch result looks wrong and you
want to watch the actual fight.

### What the auto-play policy will and will not do

It takes the action with the **highest expected damage this turn**, placing an area
spell to catch as many enemies as it can without catching an ally. Its blind spots
become yours the moment you quote one of these numbers, so state them when they
bear on the question:

- **It never uses an item.** No potion is ever drunk in a batch. If the question
  turns on one, play the fight by hand instead.
- **It never casts a spell that deals no damage.** Hold Person is loaded,
  implemented, and still never chosen, because valuing a condition means modelling
  the turns it buys the rest of the party. A batch is a **floor** for a control
  build, not a measurement of it.
- **It does not husband spell slots.** Best slot first, weapon afterwards.
- **It is greedy, not tactical.** No focus-fire planning, no retreating, no
  readying. Treat a win rate as "what these statistics do when both sides swing
  hard", not as what a good table would achieve.

`simulate_dpr` returns an `actions` breakdown of what the build actually did. Read
it before trusting a damage figure — a spell that does not appear there was never
cast, and the number is measuring something narrower than you asked for.

When comparing options, hold the seed and iteration count fixed and change one
thing. Report the distribution, not just the mean: `p90` and `max` are what a
player feels on a lucky round.

## Primitives

`roll`, `check`, and `save` handle one-off rolls outside a tracked encounter.
Every one accepts an optional `seed` and **always reports the seed it used**, so
any result can be replayed exactly. Quote the seed when a roll matters.

`lookup_rule` returns loaded conditions, spells, creatures, and items, each naming
the pack it came from in `source`. Call it with no topic to see everything
available.

For a full written catalogue — including what is deliberately absent —
read [`../../docs/COVERAGE.md`](../../docs/COVERAGE.md). Prefer it when a user asks
"what do you support?", because it states the unmodelled areas that `lookup_rule`
cannot show you: there is no entry for a class or a feat to miss on.

## What is actually loaded

**Do not assume the bundled slice is what is loaded.** A campaign can add its own
creatures, spells, conditions, and items as content packs, and can exclude the
bundled SRD content entirely to run on its own material.

- **`content_status`** — what is loaded, from where, under which mode. Call this
  before telling anyone what the engine supports, and whenever a name you expected
  is missing. It also flags any encounter still running on content from before the
  last change.
- **`content_validate`** — check a pack without loading it. The diagnostics name
  the pack, section, record, and field, so use them verbatim when helping an author
  fix their JSON.
- **`content_configure`** — load packs, or switch the bundled slice in or out.

Encounters in progress keep the content they started with; only new ones use
freshly loaded content. A failed `content_configure` changes nothing.

To help someone write a pack, read
[`../../docs/CONTENT-PACKS.md`](../../docs/CONTENT-PACKS.md) — it has the format,
the precedence rules, and a worked example.

## Honest limits

State these when they bear on a ruling rather than papering over them:

- **Height costs movement and nothing else.** Positions are `[x, y]` feet on 5-ft
  squares. Terrain, walls, sight, cover, doors, and the area shapes all work — on
  a battle map; without one the plane is open and featureless. A map may also
  carry ground heights, and those are charged only to *movement*: a slope is
  difficult terrain, a cliff is climbed at an extra foot per foot, and climbing
  down costs the same as up. Sight, cover, and areas are measured flat, so say so
  plainly when a player counts on high ground — a ridge screens nobody, and being
  atop it grants no bonus to hit and none to AC. Still absent either way: falling
  and fall damage, flying, jumping, Climb Speeds, creature size and squeezing,
  flanking, and forced movement — so nothing can shove anyone off a ledge.
- **Only SRD 5.2 content *ships*.** `lookup_rule` refusing a name means it is not
  loaded — either outside the SRD, or in a pack nobody has loaded yet. Check
  `content_status` before concluding it does not exist. Either way, do not invent
  the missing stat block: say it is not available and offer a loaded alternative.
- **Stat blocks list what is not implemented.** Every creature carries an
  `unmodelled` field naming printed traits the engine skips — Nimble Escape, the
  wolf's knock-Prone bite. Check it before promising a trait will fire, and
  mention it if a player is counting on one. Pack creatures carry the field too,
  empty unless their author filled it in. Pack Tactics and Undead Fortitude are
  modelled now, as `pack_tactics` and `undead_fortitude` flags on the stat
  block, so they fire on their own — do not re-apply them by hand.
- **Frightened always applies** its disadvantage; there is no visibility model to
  condition it on line of sight.
- **Exhaustion is not implemented.** SRD 5.2 defines it; this engine models the
  other fourteen conditions only. Do not apply exhaustion effects by hand — say it
  is unsupported. A pack could define an exhaustion-like condition, but only out of
  the effects the engine already applies; it cannot invent a new kind of effect.
- **Character building is still absent.** Classes, species, backgrounds, feats, and
  levelling are not modelled and packs do not add them; a pack extends the
  categories the engine already has.
