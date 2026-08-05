---
name: encounter-sim
description: Use when running, narrating, or analysing 5E-compatible combat — starting a fight, resolving attacks, spells, movement, conditions, items, or death saves turn by turn, linking several fights into an adventure that carries the party's hit points, conditions, slots and items from one into the next, measuring a build's expected damage and a party's win rate over many seeded iterations, or loading a campaign's own creatures, spells, conditions and items as content packs. Drives the souroldgeezer-fivee-sim engine with the bundled `fivee` command, which owns the state; not for rules lookup outside combat or for character creation.
---

# Encounter Simulation

Run 5E-compatible combat through the `fivee` command. The engine resolves the
rules and owns the state; your job is to drive it and narrate what it reports.

Bundled rules content is SRD 5.2.1 under CC-BY-4.0. See the plugin's `NOTICE`. A
campaign may load its own content as well — see "What is actually loaded" below.

## Running the command

Everything below is a Bash call. Use `fivee` if it is already on `PATH`;
otherwise run `scripts/fivee.py` in this plugin with `python3`, which is
`../../scripts/fivee.py` from this skill's own directory — the one the harness
named when it loaded this skill. Resolve that against the announced directory and
use the absolute path; nothing expands a `${...}` placeholder in this prose. Check
once, then keep using whichever answered:

```bash
command -v fivee || echo "python3 <skill dir>/../../scripts/fivee.py"
```

Either way there is nothing to start first. Every command finds the engine's
local server or starts one, so ordering cannot be got wrong.

Two commands make the rest self-describing, and they read the *running* server,
so they cannot go stale:

```bash
fivee help                       # every operation, grouped
fivee help encounter.act         # one operation's arguments, and a line to paste
```

`fivee encounter.act` and `fivee encounter act` are the same command. Results are
JSON on stdout and nothing else, so `$(fivee ...)` is always parseable; prose,
refusals, and the `etag` note go to stderr. The exit code separates the four
failures that have four different fixes: **2** the command was wrong, **3** the
engine refused, **4** the engine broke, **5** nothing answered.

An argument that no flag grammar should try to spell — a creature list, a map
document — goes in `--json '{...}'`, or `--json -` to read it from stdin. Given
both, `--json` is the base and flags override its keys, so "the same fight with
one thing changed" is an edit to the command line.

## The one rule that matters

**Never state combat state from memory. Read it from the engine.**

Hit points, initiative order, conditions, remaining movement, spell slots, and
death saves all live in `encounter.state`. That is the authoritative view. If your
narration and `encounter.state` disagree, the state is right and you are wrong —
re-read it rather than reconciling from what you remember.

This is the entire reason the engine exists. A model tracking a fight in prose
drifts: hit points wander, a condition is forgotten a round later, a die roll
quietly favours the story. Delegating resolution removes that failure mode, and
narrating from memory puts it straight back.

## Running a fight

1. **`fivee encounter.create`** with the combatants and a seed. Each is either a
   bundled stat block — `{"monster": "Goblin Warrior", "label": "Goblin A",
   "team": "monsters", "position": [15, 0]}` — or an explicit build with at least
   `name`, `team`, `ac`, `max_hp`, plus `attacks`. Labels must be unique; they
   identify combatants in every later call. `arrival_round` schedules a
   reinforcement: before that round it is absent, untargetable, and unable to act,
   but its side still keeps the encounter open. A position is `[x, y]` in feet on
   a flat plane (a bare number still means feet along the x-axis), and
   `encounter.state` reports positions in the same `[x, y]` form. Diagonals cost
   5 ft by default; pass `--movement-rule 5-10-5` for the
   every-second-diagonal-costs-double variant.

   ```bash
   fivee encounter.create --seed 41 --json '{"combatants": [
     {"name": "Thora", "team": "party", "ac": 16, "max_hp": 30, "position": [0, 0],
      "attacks": [{"name": "Longsword", "attack_bonus": 5, "damage": "1d8+3",
                   "damage_type": "slashing", "kind": "melee"}]},
     {"monster": "Goblin Warrior", "label": "Goblin A", "team": "monsters",
      "position": [15, 0]}
   ]}'
   ```

   Give creation and later state-changing calls a stable `--idempotency-key`
   whenever a call may be retried; the engine returns the first recorded result
   instead of acting twice.
2. **`fivee encounter.state <id>`** to see whose turn it is and what the situation
   is. The id is a bare word — it is the subject of the command, so no flag is
   needed.

   **`fivee encounter.brief <id> --as "<name>"`** is the same fight as *one
   combatant* is entitled to know it, and is what you show a player. Their own
   sheet comes back whole, with their remaining movement and action economy on
   their turn and their allies unredacted; the other side is reduced to position,
   distance, visible conditions, and a described `health` band — never hit
   points, AC, slots, or items. A creature they cannot see is absent rather than
   listed. Which side is redacted follows the asker, so a monster's brief hides
   the party. Use it whenever somebody at the table should not be reading the
   referee's view, rather than paraphrasing `encounter.state` and hoping.
3. **`fivee encounter.act <id> --kind …`** for each action: `attack`, `cast`,
   `use_item`, `move`, `dash`, `disengage`, `dodge`, `stand` (up from Prone — half
   Speed in movement, no action), or `surrender`. A creature whose pack lists Dash
   or Disengage in `bonus_actions` uses it with `--as-bonus-action`. It returns the
   events it generated plus fresh state.

   ```bash
   fivee encounter.act enc-1 --kind attack --target "Goblin A" --attack Longsword
   fivee encounter.act enc-1 --kind move --to-position '[10, 0]'
   ```
4. **`fivee encounter.advance <id>`** to end the turn. Death saves for dying
   creatures are rolled automatically at the start of their turn.
5. Repeat until `state["over"]` is true; `state["winner"]` names the surviving side.

**One encounter id has one writer at a time.** Every server on the machine shares
the encounter journals, so if another session has advanced this fight since you
last acted, your next call is refused with an error saying the encounter *has
advanced since you read it*. Your copy is a different fight by then, so do not
retry: call `encounter.state` to see where the fight actually is, tell the user it
moved on elsewhere, and continue from there. Handing the same id to two agents at
once is what produces this, and is worth avoiding.

Every state-changing encounter call also accepts `--if-match`, and every response
reports the fight's new version as an `etag` line on stderr. Pass the version you
read to refuse a write that would land on a fight that has moved on — the same
guard, asked for deliberately rather than inferred.

Past events are never lost: **`fivee encounter.log <id>`** pages the whole history
(`--since`/`--limit`), each event stamped with its round and turn, plus the action
records that — with the reported seed — reproduce the fight exactly. Recap earlier
rounds from it rather than from memory; `encounter.state` stays the view of *now*.

The history survives the process. Creation, every attempt, and every result are
fsynced into a hash-chained journal under `.fivee-sim/encounters/` (or
`FIVEE_SIM_ENCOUNTERS`). Use **`fivee encounter.list`** to discover
active/finalized fights, **`fivee encounter.resume <id>`** after a restart, and
**`fivee encounter.finalize <id>`** when play is done; finalization writes replay
v2 and retains the journal. If narration or an adjudication must be part of the
record, use **`fivee encounter.note <id> --text "…"`** rather than leaving it only
in prose.

`dice.roll`, `dice.check`, and `dice.save` accept an `--encounter-id` and an
`--idempotency-key`. A scoped check can name `--ability` and `--skill` (for
example Charisma/Persuasion or Charisma/Intimidation); this is audit metadata
around the supplied modifier, not a proficiency system. Scoped primitives are
recorded without consuming the encounter's combat RNG or advancing its turn.

An illegal action is **refused with a reason** — out of reach, no slots left, no
attacks remaining, none of that potion left, speed 0 while Grappled. The reason is
the problem's `detail` on stderr, and the exit code is 3. Read the reason and
adapt. Do not retry the same call hoping for a different answer, and do not
narrate the action as though it happened.

For a portable record, **`fivee encounter.replay <id>`** defaults to version 2:
normalized starting combatants, captured inline/loaded maps and storeys, captured
content, successful actions, refused attempts, timestamps, full state checkpoints,
and integrity hashes. `replay.validate` and the viewer verify the nested schema
and hashes; the hashes detect alteration but are not author signatures. Use
`--format-version 1` only for a legacy consumer.

## Running an adventure: fights in a row

An **adventure** is an ordered run of encounters that carries the party between
them — chapter two opens with the hit points, conditions, spell slots, and items
chapter one left everybody holding. It is a durable document beside the encounter
journals, not a session, so a run survives a restart the way a fight does.

Six operations, and the shape is create → link a fight per chapter → finalize.

1. **`fivee adventure.create --name "The Sunken Bell"`** returns the new run's id
   — `adv-1` — and its `version`.
2. **`fivee adventure.encounter <adv-id> --if-match <version>`** starts the run's
   next fight. It answers with the `encounter_id`, who was carried, the updated
   adventure, and everything `encounter.create` would have returned; drive that
   fight exactly as above, `encounter.act` and `encounter.advance` until it is over.
3. **`fivee adventure.state <adv-id>`** lists the members in order and reports the
   version the next write must match. **`fivee adventure.list`** names every run on
   disk — `--status active` by default, or `finalized`, or `all`.
4. **`fivee adventure.finalize <adv-id> --if-match <version>`** closes the run.
   After it, linking another encounter is refused with *is finalized; start
   another one to keep playing*.

**`--if-match` is required on both writing calls**, unlike an encounter's, because
an adventure is a document rewritten whole: two unguarded links would each be told
they succeeded and one fight would be missing from a run that acknowledged it.
Read the version from `adventure.state` — it is in the body and on stderr as the
`etag` line — and pass it. A version that has moved on is refused with *has
advanced since you read it*; re-read and reapply rather than retrying.
`--if-match '*'` writes against whatever is current, so use it deliberately and
say when you have. Give a link a stable `--idempotency-key` if it may be retried:
the key is recorded on the adventure *and* handed to the encounter it creates, so
a retry re-finds the same fight instead of starting a second one.

### Who walks into the next fight

The first chapter has nothing to carry, so its `combatants` are the whole roster,
written exactly as `encounter.create` takes them. Every later chapter carries the
previous fight's cast forward:

```bash
fivee adventure.encounter adv-1 --if-match <version> --seed 20260806 --json '{
  "carry": ["Thora"],
  "recovery": {"Thora": {"hp": 30, "conditions": []}},
  "combatants": [{"monster": "Goblin Warrior", "label": "Goblin B",
                  "team": "monsters", "position": [10, 0]}]
}'
```

- **`carry` names who comes forward**, in the order they should be built. Each
  arrives as their original stat block overlaid with how the last fight left them
  — hit points, conditions, death saves, stable/dead/surrendered, spell slots,
  items, position — and with `arrival_round` reset to 1, because somebody who
  joined the last fight late is present from the start of this one. Initiative and
  concentration are deliberately *not* carried: both belonged to the fight that
  ended.
- **Omitted, `carry` is everybody that fight had — the dead included.** That is
  the honest default rather than a survivor filter, and it has a sharp edge: carry
  a fight whose only monster died and the next encounter opens with a corpse on one
  side and is over before anyone acts. Name the survivors you actually mean.
  `"carry": []` starts a fresh roster inside the same run and does not consult the
  previous fight at all.
- **`combatants` are the new arrivals**, appended after the carried ones — and a
  chapter needs two combatants like any other encounter, so carrying one survivor
  and adding nobody is refused.
- **`carry` on a run's first chapter is refused** — there is no encounter to carry
  from yet, so give `combatants`. A name the previous fight never had is refused
  too, and the refusal lists the names it did have.

**`recovery` is your statement about the interlude, not a rest the engine
simulated.** There are no hit dice, no slot-recovery table, and no rest rules
anywhere in this engine. `recovery` maps a carried combatant's name to a partial
delta over the same keys the carry-over uses — `hp`, `conditions`, `death_saves`,
`stable`, `dead`, `surrendered`, `spell_slots`, `items`, `position`, `level`,
`facing` — applied to their ending state before it composes. So say plainly what
you granted ("a long rest, so: back to 30 and slots restored") rather than
implying the engine worked it out. A key outside that set is refused and lists the
valid ones; a name that is not being carried is refused rather than quietly doing
nothing.

**`fivee adventure.replay <adv-id>`** composes the run's finalized encounters into
one bundle on disk. Every member must have been through `encounter.finalize`
first, and nothing is re-derived — see the **map-forge** skill, which owns
replays, for what that bundle is and is not.

## Fighting on a map

`encounter.create` takes an optional `map` of 5-ft squares: `{"width", "height",
"rows": [".#..", ...], "legend": {".": "normal", "#": "wall"}, "features":
[{"name": "door", "square": [1, 1]}]}` — rows top-first, one character per square.
With a map the engine charges terrain for movement, routes moves around walls and
enemies (pass-through opportunity attacks apply), grades cover (+2/+5 to AC and to
Dexterity saves, against a weapon swing and a spell alike; total cover refuses an
attack or a named-target spell outright and shelters a creature from an area
entirely), and blocks sight. Positions snap to square centres, and `state["map"]`
reports dimensions and door state.

`fivee encounter.act <id> --kind interact --feature door` opens or closes a door —
free, once per turn, from the feature's square or one next to it and on its own
storey. `analytics.rounds` accepts the same `map` and `movement_rule`, so batches
fight on the terrain too.

A door is the simple case of a **fixture**: any map feature carrying a state is
one, and a loaded map may hold levers, spikes, and sluice gates as well. A fixture
can govern squares beyond its own, so working it changes terrain *and* ground
height immediately, under whoever is standing there. Read
`state["map"]["features"]` before promising anything — a fixture reports
`affects`, `requires`, `blocked_by`, `trigger`, `costs_action`, and `check`
whenever it carries them, so you can tell the party what a thing will cost before
they spend a turn on it. Five things to state out loud rather than let a player
assume:

- **`costs_action` spends the action**, not the free interaction, so a chain of
  three such fixtures is three actions — three turns unless the party splits the
  work. A failed check spends it and moves nothing.
- **The check is a raw ability check.** There are no skill proficiencies anywhere
  in this engine, so a `check` of `{"ability": "strength", "dc": 15}` is a flat
  Strength check against 15 — no Athletics, no proficiency bonus, no Help.
- **`blocked_by` names what is still shut.** Prerequisites gate *opening* only;
  closing is never blocked.
- **A `trigger` is automatic fixture logic.** `when` is an AND predicate over
  fixture states. `edge` fires on false→true and rearms after false; `maintained`
  holds its configured state while true and refuses a contrary manual interaction
  before spending anything. When maintained becomes false, it leaves the fixture
  where it is. Automatic `interact` events have an empty actor and carry
  `automatic: true` plus `triggered_by`; narrate the mechanism, not an invisible
  creature operating it.
- **Nobody is moved by a fixture.** A creature standing where the ground turns
  impassable stays and may walk out — entry cost governs entering a square, not
  remaining in one, and there is no forced movement to shove it.

Pass `--set-open true`/`false` to say which way rather than flipping whatever is
there. Use it whenever you drive a fixture to a known state, because `interact`
alone **toggles**: "open the sluice" on a sluice that already stands open closes
it.

Movement defaults to Walk. Pass `--movement-mode` as `climb`, `swim`, or `fly` to
use that authored speed. A Swim speed avoids underwater terrain's doubled cost;
Fly can change storeys without a connector. `encounter.state` reports all four
speeds, Darkvision/Blindsight, terrain overrides, death rule, Bonus Actions, and
reaction availability plus `arrival_round`/`present`, so narration never has to
infer them from the pack. Auto-play chooses among authored movement modes when it
closes, including swimming through underwater terrain and flying between storeys.

## Maps

Nine operations manage maps, and a map is a **file addressed by an id** — not a
session object. `fivee map.list` names every one under the maps directory, and
that id is what every other call takes.

**`fivee map.generate --kind dungeon --seed 71203941`** builds a dungeon, caves,
or overland map and returns the whole document **unsaved** (the seed is always
reported — quote it); add `--save-as <id>` to write it under that id in the same
call. **`map.render`** shows a saved or inline map as glyph rows, with
`--x`/`--y`/`--width`/`--height` viewports and `--downsample` for big maps,
`--show-elevation` for the ground heights as a second set of rows, and an
`--encounter-id` overlay that letters the combatants. **`map.edit`** applies verbal
tweaks atomically — paint, line, carve_corridor, set_terrain, add/set/remove_feature,
toggle_door, resize, set_legend, set_name, set_palette, set_elevation,
adjust_elevation — a bad operation names its index and changes nothing.
**`map.query`** answers distance, line-of-sight, and pathing questions on a bare
map.

**A guarded write is two calls, and there is no third.** `fivee map.get <id>`
returns the document and reports its sha256 as an `etag` on stderr; `fivee map.put
<id> --if-match <that etag> --json -` writes the new bytes and is refused with a
409 if anything else got there first. The listing deliberately carries no hash, so
the version you write against is always one you actually read. `--if-match '*'`
creates a new id, or takes an existing file over on purpose — say so when you do.

`encounter.create --map-id <id>` and `analytics.rounds --map-id <id>` put a fight
on a saved map; the fight captures the document as it stands, and
`encounter.state`'s `map_source.stale` turns true if the map is edited after —
re-create the encounter when the new layout should apply.

For the full map workflow — generation seeds, the browser editor, and exporting a
fight as a shareable replay — use the **map-forge** skill.

## Aiming a spell

`--kind cast` takes `--target` for one creature, `--targets` for several, or an
area aim: **`--center`** for a sphere (or a cube's minimum corner) — an `[x, y]`
point in feet, not a creature — **`--direction`** for a cone (one of the eight
unit offsets, such as `[1, 0]` or `[-1, 1]`), **`--toward`** for a line (a
combatant name or a point). On a map, a sphere or cube also needs its point of
origin in the caster's sight.

```bash
fivee encounter.act enc-1 --kind cast --spell Fireball --center '[20, 15]' --slot-level 3
```

`--center` is what makes a Fireball a Fireball. Named targets are each hit
individually, so a 20-ft blast dropped with `--target` catches exactly one
creature however tightly the enemy is packed. Give a point of origin instead and
it catches everything within its radius — **including allies and the caster**,
which is a real tactical cost and the engine will not protect you from it.

Range is checked against the point of origin for an area, and against each named
creature otherwise. A creature at the far edge of a blast can therefore sit past
the spell's range legitimately; the origin is what has to be reachable.

## Items

`fivee encounter.act <id> --kind use_item --item "Potion of Healing"` spends the
item's declared action or Bonus Action. Healing defaults to the user; a damaging
or condition-applying item needs a `--target`, and any item used on another
creature needs to be within 5 ft. Quantity is the charge count, and
`encounter.state` shows what each combatant has left.

No items ship in the bundled slice — they arrive through a content pack.

## Narrating well

Report what the engine actually rolled. Players trust a fight they can audit:

> Thora swings at the goblin — d20 [17] +5 = 22 vs AC 15, a hit. 1d8+3 → [6] +3 =
> 9 slashing. The goblin drops to 1 hit point.

The `detail` field of each event already contains the arithmetic. Use it. When an
attack had advantage or disadvantage, say which and why — the engine's condition
handling is the interesting part, and hiding it makes the fight feel arbitrary.

## Analysis rather than play

- **`fivee analytics.rounds`** auto-plays the same encounter many times and
  reports win rates, rounds, and per-team HP, casualty, spell-slot, and item-use
  distributions. Use it for "is this encounter too hard?"
- **`fivee analytics.dpr`** measures damage a build lands over N rounds against a
  given `--target-ac`, at a `--distance` you choose (5 ft by default). Use it for
  "is this build actually better?"

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

`analytics.dpr` returns an `actions` breakdown of what the build actually did. Read
it before trusting a damage figure — a spell that does not appear there was never
cast, and the number is measuring something narrower than you asked for.

When comparing options, hold the seed and iteration count fixed and change one
thing. Report the distribution, not just the mean: `p10`, median, `p90`, and the
resource/casualty tails are the play experience a win percentage hides.

## Primitives

`fivee dice.roll`, `fivee dice.check`, and `fivee dice.save` handle one-off rolls
outside a tracked encounter. Every one accepts an optional `--seed` and **always
reports the seed it used**, so any result can be replayed exactly. Quote the seed
when a roll matters.

`fivee analytics.scenario-timing` handles one narrow route-level check outside
combat: fixed distance and Speed (optionally dashing and starting late) against an
authored response delay. It reports travel rounds and the lead or deficit, and
nothing beyond that: it holds no state of its own between calls and does not
decide which events reinforce which encounter. Carrying a party from one fight
into the next is an adventure's job — see "Running an adventure" above.

`fivee rules.lookup --topic <name>` is the exact-name view of loaded executable
conditions, spells, creatures, and items, each naming the pack it came from in
`source`. With no topic it returns compact counts and search guidance, not an
unbounded name dump.

Use `fivee catalog.search --query …` for bounded discovery across both SRD
identities and loaded campaign content, `fivee catalog.get <id>` for one structured
record, and `fivee catalog.table <id>` for a paged printed table.
[`../../docs/COVERAGE.md`](../../docs/COVERAGE.md) contains only generated
category, progress, and support totals; the catalog operations are the detailed
authority.

## What is actually loaded

**Do not assume the bundled slice is what is loaded.** A campaign can add its own
creatures, spells, conditions, and items as content packs, and can exclude the
bundled SRD content entirely to run on its own material.

At first use in a workspace, check whether `.fivee-sim/content/` exists under the
workspace root. Call `fivee content.status`; if that resolved directory is not
already represented in the loaded packs, call `fivee content.configure` with its
**absolute** path and `--add` before looking up content or starting an encounter.
This is the portable fallback for hosts that do not export a project-root
variable; repeating an already loaded path is harmless but unnecessary.

```bash
fivee content.configure --add --json '{"paths": ["/abs/path/.fivee-sim/content"]}'
```

- **`content.status`** — what is loaded, from where, under which mode. Call this
  before telling anyone what the engine supports, and whenever a name you expected
  is missing. It also flags any encounter still running on content from before the
  last change.
- **`content.validate`** — check a pack without loading it. The diagnostics name
  the pack, section, record, and field, so use them verbatim when helping an author
  fix their JSON.
- **`content.configure`** — load packs, or switch the bundled slice in or out with
  `--builtin include|exclude`.

Encounters in progress keep the content they started with; only new ones use
freshly loaded content. A failed `content.configure` changes nothing.

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
- **Only SRD 5.2.1 content *ships*.** `rules.lookup` refusing a name means it is not
  loaded — either outside the SRD, or in a pack nobody has loaded yet. Check
  `content.status` before concluding it does not exist. Either way, do not invent
  the missing stat block: say it is not available and offer a loaded alternative.
- **Stat blocks list what is not implemented.** Built-in creatures carry
  structured `unmodelled_facts` entries for printed mechanics the engine skips.
  Check them before promising a trait will fire, and mention one if a player is
  counting on it. Older campaign packs may still use the legacy `unmodelled`
  string list; the loader keeps accepting and reporting it.
  Pack Tactics, Undead Fortitude, authored Bonus Action Dash/Disengage, last-one-
  standing surrender, Redirect Attack, attack attachment damage, and conditional
  adjacent-ally damage are modelled as explicit stat-block fields, so they fire on
  their own — do not re-apply them by hand.
- **Frightened always applies** its disadvantage. The encounter can answer simple
  sight questions, but a condition does not record which creature caused the fear,
  so it cannot test whether that particular source remains in line of sight.
- **Exhaustion is not implemented.** SRD 5.2.1 defines it; this engine models the
  other fourteen conditions only. Do not apply exhaustion effects by hand — say it
  is unsupported. A pack could define an exhaustion-like condition, but only out of
  the effects the engine already applies; it cannot invent a new kind of effect.
- **Resting is not modelled, and an adventure does not change that.** A run carries
  the party's ending state forward and stops there; there are no hit dice, no
  short/long rest, and no slot-recovery table. Whatever `recovery` says happened
  between two chapters is something you asserted, so attribute it to yourself
  rather than to the engine.
- **Character building is still absent.** Classes, species, backgrounds, feats, and
  levelling are not modelled and packs do not add them; a pack extends the
  categories the engine already has.
