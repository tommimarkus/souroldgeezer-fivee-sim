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
   combatants in every later call. `arrival_round` schedules a reinforcement:
   before that round it is absent, untargetable, and unable to act, but its side
   still keeps the encounter open. A position is `[x, y]` in feet on a flat plane
   (a bare number still means feet along the x-axis), and `encounter_state`
   reports positions in the same `[x, y]` form. Diagonals cost 5 ft by default;
   pass `movement_rule: "5-10-5"` for the every-second-diagonal-costs-double
   variant. Give creation and later state-changing calls a stable `request_id`
   whenever a host may retry them; the engine returns the first recorded result
   instead of acting twice.
2. **`encounter_state`** to see whose turn it is and what the situation is.
3. **`encounter_act`** for each action: `attack`, `cast`, `use_item`, `move`,
   `dash`, `disengage`, `dodge`, `stand` (up from Prone — half Speed in
   movement, no action), or `surrender`. A creature whose pack lists Dash or
   Disengage in `bonus_actions` uses it with `as_bonus_action=true`. It returns
   the events it generated plus fresh state.
4. **`encounter_advance`** to end the turn. Death saves for dying creatures are
   rolled automatically at the start of their turn.
5. Repeat until `state["over"]` is true; `state["winner"]` names the surviving side.

Past events are never lost: **`encounter_log`** pages the whole history
(`since`/`limit`), each event stamped with its round and turn, plus the action
records that — with the reported seed — reproduce the fight exactly. Recap
earlier rounds from it rather than from memory; `encounter_state` stays the view
of *now*.

The history survives the MCP process. Creation, every attempt, and every result
are fsynced into a hash-chained journal under `.fivee-sim/encounters/` (or
`FIVEE_SIM_ENCOUNTERS`). Use `encounter_list` to discover active/finalized
fights, `encounter_resume` after a restart, and `encounter_finalize` when play is
done; finalization writes replay v2 and retains the journal. If narration or an
adjudication must be part of the record, use `encounter_note` rather than leaving
it only in prose.

`roll`, `check`, and `save` accept an `encounter_id` and `request_id`. A scoped
check can name `ability` and `skill` (for example Charisma/Persuasion or
Charisma/Intimidation); this is audit metadata around the supplied modifier, not
a proficiency system. Scoped primitives are recorded without consuming the
encounter's combat RNG or advancing its turn.

An illegal action is **refused with a reason** — out of reach, no slots left, no
attacks remaining, none of that potion left, speed 0 while Grappled. Read the
reason and adapt. Do not retry the same call hoping for a different answer, and do
not narrate the action as though it happened.

For a portable record, `replay_export` defaults to version 2: normalized starting
combatants, captured inline/loaded maps and storeys, captured content, successful
actions, refused attempts, timestamps, full state checkpoints, and integrity
hashes. `replay_validate` and the viewer verify the nested schema and hashes;
the hashes detect alteration but are not author signatures. Use
`format_version=1` only for a legacy consumer.

## Fighting on a map

`encounter_create` takes an optional `map` of 5-ft squares: `{"width", "height",
"rows": [".#..", ...], "legend": {".": "normal", "#": "wall"}, "features":
[{"name": "door", "square": [1, 1]}]}` — rows top-first, one character per
square. With a map the engine charges terrain for movement, routes moves around
walls and enemies (pass-through opportunity attacks apply), grades cover (+2/+5
to AC and to Dexterity saves, against a weapon swing and a spell alike; total
cover refuses an attack or a named-target spell outright and shelters a creature
from an area entirely), and blocks sight. Positions snap to
square centres, and `state["map"]` reports dimensions and door state.

`encounter_act(kind="interact", feature="door")` opens or closes a door — free,
once per turn, from the feature's square or one next to it and on its own
storey. `simulate_rounds` accepts the same `map` and `movement_rule`, so batches
fight on the terrain too.

A door is the simple case of a **fixture**: any map feature carrying a state is
one, and a loaded map may hold levers, spikes, and sluice gates as well. A
fixture can govern squares beyond its own, so working it changes terrain *and*
ground height immediately, under whoever is standing there. Read
`state["map"]["features"]` before promising anything — a fixture reports
`affects`, `requires`, `blocked_by`, `trigger`, `costs_action`, and `check`
whenever it carries them, so you can tell the party what a thing will cost
before they spend a turn on it. Five things to state out loud rather than let a
player assume:

- **`costs_action` spends the action**, not the free interaction, so a chain of
  three such fixtures is three actions — three turns unless the party splits
  the work. A failed check spends it and moves nothing.
- **The check is a raw ability check.** There are no skill proficiencies
  anywhere in this engine, so a `check` of `{"ability": "strength", "dc": 15}`
  is a flat Strength check against 15 — no Athletics, no proficiency bonus, no
  Help.
- **`blocked_by` names what is still shut.** Prerequisites gate *opening* only;
  closing is never blocked.
- **A `trigger` is automatic fixture logic.** `when` is an AND predicate over
  fixture states. `edge` fires on false→true and rearms after false;
  `maintained` holds its configured state while true and refuses a contrary
  manual interaction before spending anything. When maintained becomes false,
  it leaves the fixture where it is. Automatic `interact` events have an empty
  actor and carry `automatic: true` plus `triggered_by`; narrate the mechanism,
  not an invisible creature operating it.
- **Nobody is moved by a fixture.** A creature standing where the ground turns
  impassable stays and may walk out — entry cost governs entering a square, not
  remaining in one, and there is no forced movement to shove it.

Pass `set_open: true`/`false` to say which way rather than flipping whatever is
there. Use it whenever you drive a fixture to a known state, because `interact`
alone **toggles**: "open the sluice" on a sluice that already stands open closes
it.

Movement defaults to Walk. Pass `movement_mode` as `climb`, `swim`, or `fly` to
use that authored speed. A Swim speed avoids underwater terrain's doubled cost;
Fly can change storeys without a connector. `encounter_state` reports all four
speeds, Darkvision/Blindsight, terrain overrides, death rule, Bonus Actions, and
reaction availability plus `arrival_round`/`present`, so narration never has to
infer them from the pack. Auto-play chooses among authored movement modes when it
closes, including swimming through underwater terrain and flying between storeys.

## Maps

Six tools manage maps as first-class documents. **`map_generate`** builds a
dungeon, caves, or overland map under a seed (always reported — quote it);
**`map_render`** shows any of them as glyph rows, with `x`/`y`/`width`/`height`
viewports and `downsample` for big maps, `show_elevation` for the ground
heights as a second set of rows, and an `encounter_id` overlay that
letters the combatants; **`map_edit`** applies verbal tweaks atomically —
paint, line, carve_corridor, set_terrain, add/remove_feature, toggle_door,
resize, set_legend, set_name, set_palette, set_elevation, adjust_elevation — a
bad operation
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

`encounter_act(kind="use_item", item="Potion of Healing")` spends the item's
declared action or Bonus Action.
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
  rates, rounds, and per-team HP, casualty, spell-slot, and item-use
  distributions. Use it for "is this encounter too hard?"
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

- **It uses healing deliberately, not arbitrary items.** A downed ally is revived
  first; an ally at half HP or below may receive a healing spell or item. Other
  item effects are not valued.
- **It never casts a spell that deals no damage.** Hold Person is loaded,
  implemented, and still never chosen, because valuing a condition means modelling
  the turns it buys the rest of the party. A batch is a **floor** for a control
  build, not a measurement of it.
- **It never operates a map fixture.** No door is opened, no spike pulled, no
  sluice raised. A batch fights the map at the configuration it was handed, so
  measure a fixture by running two batches — one map authored open, one shut —
  rather than expecting the policy to find the lever.
- **It does not husband spell slots.** Best slot first, weapon afterwards.
- **It closes with Dash.** An authored Bonus Action Dash is spent before the
  action so a newly reachable attack can still happen that turn.
- **It is greedy, not tactical.** No focus-fire planning, no retreating, no
  readying. Treat a win rate as "what these statistics do when both sides swing
  hard", not as what a good table would achieve.

`simulate_dpr` returns an `actions` breakdown of what the build actually did. Read
it before trusting a damage figure — a spell that does not appear there was never
cast, and the number is measuring something narrower than you asked for.

When comparing options, hold the seed and iteration count fixed and change one
thing. Report the distribution, not just the mean: `p10`, median, `p90`, and the
resource/casualty tails are the play experience a win percentage hides.

## Primitives

`roll`, `check`, and `save` handle one-off rolls outside a tracked encounter.
Every one accepts an optional `seed` and **always reports the seed it used**, so
any result can be replayed exactly. Quote the seed when a roll matters.

`scenario_timing` handles one narrow route-level check outside combat: fixed
distance and Speed (optionally Dashing and starting late) against an authored
response delay. It reports travel rounds and the lead or deficit. It does not
carry campaign state or decide which events reinforce which encounter.

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

At first use in a workspace, check whether `.fivee-sim/content/` exists under the
workspace root. Call `content_status`; if that resolved directory is not already
represented in the loaded packs, call `content_configure` with its **absolute**
path and `add=true` before looking up content or starting an encounter. This is
the portable fallback for hosts that do not export a project-root variable;
repeating an already loaded path is harmless but unnecessary.

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
  atop it grants no bonus to hit and none to AC. Walk, Climb, Swim, and Fly speeds,
  storey-changing flight, underwater movement, Darkvision/Blindsight, local light,
  and authored sight openings are supported. Still absent either way: falling and
  fall damage, jumping, creature size and squeezing, flanking, and forced movement
  — so nothing can shove anyone off a ledge.
- **Only SRD 5.2 content *ships*.** `lookup_rule` refusing a name means it is not
  loaded — either outside the SRD, or in a pack nobody has loaded yet. Check
  `content_status` before concluding it does not exist. Either way, do not invent
  the missing stat block: say it is not available and offer a loaded alternative.
- **Stat blocks list what is not implemented.** Every creature carries an
  `unmodelled` field naming printed traits the engine skips. Check it before
  promising a trait will fire, and mention it if a player is counting on one.
  Pack creatures carry the field too, empty unless their author filled it in.
  Pack Tactics, Undead Fortitude, authored Bonus Action Dash/Disengage, last-one-
  standing surrender, Redirect Attack, attack attachment damage, and conditional
  adjacent-ally damage are modelled as explicit stat-block fields, so they fire on
  their own — do not re-apply them by hand.
- **Frightened always applies** its disadvantage. The encounter can answer simple
  sight questions, but a condition does not record which creature caused the fear,
  so it cannot test whether that particular source remains in line of sight.
- **Exhaustion is not implemented.** SRD 5.2 defines it; this engine models the
  other fourteen conditions only. Do not apply exhaustion effects by hand — say it
  is unsupported. A pack could define an exhaustion-like condition, but only out of
  the effects the engine already applies; it cannot invent a new kind of effect.
- **Character building is still absent.** Classes, species, backgrounds, feats, and
  levelling are not modelled and packs do not add them; a pack extends the
  categories the engine already has.
