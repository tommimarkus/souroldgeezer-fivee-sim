---
name: encounter-sim
description: Use when running, narrating, or analysing 5E-compatible combat — starting a fight, resolving attacks, spells, movement, conditions, or death saves turn by turn, or measuring a build's expected damage and a party's win rate over many seeded iterations. Drives the souroldgeezer-fivee-sim MCP engine, which owns the state; not for rules lookup outside combat or for character creation.
---

# Encounter Simulation

Run 5E-compatible combat through the `fivee_sim` MCP engine. The engine resolves
the rules and owns the state; your job is to drive it and narrate what it reports.

Rules content is SRD 5.2 under CC-BY-4.0. See the plugin's `NOTICE`.

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
   "monsters", "position": 15}` — or an explicit build with at least `name`,
   `team`, `ac`, `max_hp`, plus `attacks`. Labels must be unique; they identify
   combatants in every later call. Positions are feet along one axis.
2. **`encounter_state`** to see whose turn it is and what the situation is.
3. **`encounter_act`** for each action: `attack`, `cast`, `move`, `dash`,
   `disengage`, `dodge`. It returns the events it generated plus fresh state.
4. **`encounter_advance`** to end the turn. Death saves for dying creatures are
   rolled automatically at the start of their turn.
5. Repeat until `state["over"]` is true; `state["winner"]` names the surviving side.

An illegal action is **refused with a reason** — out of reach, no slots left, no
attacks remaining, speed 0 while Grappled. Read the reason and adapt. Do not
retry the same call hoping for a different answer, and do not narrate the action
as though it happened.

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
  AC. Use it for "is this build actually better?"

Both replay the same stepper live play uses, so their numbers cannot drift from
the rules. Iteration `i` uses `seed + i`, so one iteration reproduces a single
hand-played fight at that seed — handy when a batch result looks wrong and you
want to watch the actual fight.

When comparing options, hold the seed and iteration count fixed and change one
thing. Report the distribution, not just the mean: `p90` and `max` are what a
player feels on a lucky round.

## Primitives

`roll`, `check`, and `save` handle one-off rolls outside a tracked encounter.
Every one accepts an optional `seed` and **always reports the seed it used**, so
any result can be replayed exactly. Quote the seed when a roll matters.

`lookup_rule` returns bundled conditions, spells, and stat blocks. Call it with no
topic to see everything available.

For a full written catalogue — including what is deliberately absent —
read [`../../docs/COVERAGE.md`](../../docs/COVERAGE.md). Prefer it when a user asks
"what do you support?", because it states the unmodelled areas that `lookup_rule`
cannot show you: there is no entry for a class or a potion to miss on.

## Honest limits

State these when they bear on a ruling rather than papering over them:

- **Geometry is one axis.** Distance, reach, ranged penalties, and spell radii all
  work; flanking, cover, and difficult terrain do not exist.
- **Only SRD 5.2 content ships.** `lookup_rule` refusing a name usually means it
  is outside the SRD, not misspelled. Do not invent the missing stat block — say
  it is not available and offer a bundled alternative.
- **Stat blocks list what is not implemented.** Every bundled monster carries an
  `unmodelled` field naming printed traits the engine skips — Undead Fortitude,
  Pack Tactics, Nimble Escape. Check it before promising a trait will fire, and
  mention it if a player is counting on one.
- **Frightened always applies** its disadvantage; there is no visibility model to
  condition it on line of sight.
- **Exhaustion is not implemented.** SRD 5.2 defines it; this engine models the
  other fourteen conditions only. Do not apply exhaustion effects by hand — say it
  is unsupported.
