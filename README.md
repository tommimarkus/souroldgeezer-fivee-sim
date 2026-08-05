# souroldgeezer-fivee-sim

A 5E-compatible combat simulation engine for Claude Code™ and Codex, by Sour Old
Geezer. This repository is the marketplace source and the shared plugin tree.

Compatible with fifth edition (2024 rules).

> **Status: v1 complete.** Engine, HTTP service, the `fivee` command, plugin,
> skills, the complete bundled SRD structured catalog, and user-defined content
> packs all work end to end.

## What this is

A model asked to track a fight will drift: hit points wander, a condition gets
forgotten a round later, a die roll conveniently favours the narrative. Here the
rules live in a Python engine served over a local HTTP API, so hit points,
dice, initiative order, and conditions are computed and owned by the engine
rather than recalled. Ask for the same seed twice and you get the same fight
twice.

The assistant drives it with **`fivee`**, a command that starts the engine if
nothing is serving, reads the operation list off the server it is about to call,
and prints JSON. There is nothing to launch first and no tool list to memorise:
`fivee help` is the whole surface.

One kernel answers two questions:

- **"What happens in this fight?"** — the encounter operations step a fight round
  by round, and the assistant narrates what the engine reports.
- **"Is this build actually good?"** — the analytics operations replay that same
  stepper across thousands of seeded iterations and report a distribution.

Because both run on the one kernel, the statistics cannot drift from the rules
live play uses.

| Plugin | Version | Skill | Claude agent |
|---|---:|---|---|
| `souroldgeezer-fivee-sim` | `2026.08.41` | [encounter-sim](souroldgeezer-fivee-sim/skills/encounter-sim/SKILL.md) | [encounter-sim](souroldgeezer-fivee-sim/agents/encounter-sim.md) |

## Install

Needs Python 3.11+, and nothing else. The engine has no runtime dependencies, so
there is nothing to install and no environment to build: the launcher puts the
bundled source on `sys.path` and runs it. [`uv`](https://docs.astral.sh/uv/) is
needed only to work *on* this repository — see [CLAUDE.md](CLAUDE.md).

The marketplace is not published yet, so point either host at a clone.

### Codex

```text
codex plugin marketplace add /absolute/path/to/this/repo
codex plugin add souroldgeezer-fivee-sim@souroldgeezer-tabletop
```

Start a new Codex session after installation so the bundled skills are loaded.

Codex keeps this plugin's durable data under
`${CODEX_HOME}/plugins/data/souroldgeezer-fivee-sim-souroldgeezer-tabletop`, outside
the versioned plugin cache. The launcher copies the engine source there, into a
directory named after a hash of that source, and runs from the copy — because a
running server reads its static assets from wherever the package lives, and the
host may retire the plugin cache while a session is still using it. An upgrade
therefore lands beside a live copy rather than replacing it, and old copies are
kept on purpose. To reclaim space, close sessions using the plugin and remove
directories under the runtime location's `src`; the launcher recreates what it
needs on demand. Explicit `PLUGIN_DATA` and `CLAUDE_PLUGIN_DATA` settings still
take precedence. A direct checkout without a plugin host runs straight from
`engine/src` and copies nothing.

Once published, use the repository source instead of the local path:

```text
codex plugin marketplace add tommimarkus/souroldgeezer-fivee-sim
codex plugin add souroldgeezer-fivee-sim@souroldgeezer-tabletop
```

### Claude Code

```json
// ~/.claude/settings.json
{
  "extraKnownMarketplaces": {
    "souroldgeezer-tabletop": {
      "source": { "source": "directory", "path": "/absolute/path/to/this/repo" }
    }
  },
  "enabledPlugins": {
    "souroldgeezer-fivee-sim@souroldgeezer-tabletop": true
  }
}
```

Once it is published, the same thing is two commands:

```text
/plugin marketplace add tommimarkus/souroldgeezer-fivee-sim
/plugin install souroldgeezer-fivee-sim@souroldgeezer-tabletop
```

From a clone, `python3 souroldgeezer-fivee-sim/scripts/fivee.py help` verifies the
whole path independently of either host: it starts the engine and prints every
operation the running server serves. Stop it again with
`python3 souroldgeezer-fivee-sim/scripts/fivee.py stop`.

`python3 scripts/check-api-smoke.py` does the same thing without a human reading
the output. It boots the real launcher, runs a complete seeded fight over HTTP,
runs it again in a second server and once more through the `fivee` binary, and
requires all three to agree — then checks that the operations index, the OpenAPI
document and `fivee help` all describe the same contract. It needs nothing but
Python, prints `PASS`/`FAIL` per case, and exits non-zero if any of them failed.
Every server it starts is pointed at a scratch directory and stopped again, so a
run leaves nothing behind.

## Operations

Thirty-nine, under `/api/v1`, each reachable as `fivee <group>.<verb>`.

| Group | Operations |
| --- | --- |
| Encounters | `encounter.create`, `encounter.state`, `encounter.act`, `encounter.advance`, `encounter.note`, `encounter.log`, `encounter.list`, `encounter.resume`, `encounter.finalize`, `encounter.replay` |
| Maps | `map.list`, `map.generate`, `map.get`, `map.put`, `map.edit`, `map.render`, `map.query`, `map.validate`, `map.uvtt` |
| Replays | `replay.list`, `replay.get`, `replay.validate` |
| Analytics | `analytics.rounds`, `analytics.dpr`, `analytics.scenario-timing` |
| Dice | `dice.roll`, `dice.check`, `dice.save` |
| Rules and catalog | `rules.lookup`, `catalog.search`, `catalog.get`, `catalog.table` |
| Content | `content.status`, `content.configure`, `content.validate` |
| The server itself | `server.ping`, `server.operations`, `server.openapi`, `server.shutdown` |

That table is a convenience, not the source: `fivee help` renders
`GET /api/v1/operations` from the running server, and `fivee help <operation>`
reads that operation's arguments out of its OpenAPI document. Neither can
describe an operation the server does not route, or miss one it does.

Every operation that consumes randomness accepts an optional `seed` and always
reports the seed it used, so no result is irreproducible after the fact.

## What is covered

The bundled reference catalog contains reviewed structured facts for all 2,062
SRD 5.2.1 sections and 227 printed tables, including links for 336 stat blocks,
339 spells, and 155 glossary terms. Every entry is closed as `complete` or
`no_structured_facts`; no catalog review remains pending.

Execution is a smaller, explicit subset: 6 creatures, 4 spells, 14 conditions,
13 damage types, 7 actions, and no items. Weapon attacks with reach and ranged
bands, movement, Dash, Disengage, Dodge, opportunity attacks, death saves, and
damage resistance and vulnerability all work; spells carry saving throws, areas,
upcasting, and concentration. Catalog entries separately report whether they are
`reference_only`, `partial`, or `executable`.

Limits worth knowing before you report one as a bug:

- **No character building.** No classes, species, backgrounds, feats or levelling.
  Combatants are described directly by their statistics, the way a stat block
  presents them — there is no character sheet deriving those numbers.
- **Geometry is a single axis.** Reach, ranged bands and spell radii work; facing,
  flanking, cover, terrain and elevation do not exist.
- **Opportunity attacks are the only reaction.** No readied actions, no Shield or
  similar reaction spells, no legendary or lair actions.
- **Nothing outside a fight.** No exploration, resting or recovery — an encounter
  begins and ends, and resources do not regenerate between them.
- **Exhaustion is not implemented**, though SRD 5.2.1 defines it, and Frightened
  applies its disadvantage unconditionally because there is no visibility model.

[COVERAGE.md](souroldgeezer-fivee-sim/docs/COVERAGE.md) is the generated compact
totals report. Use `catalog.search`, `catalog.get`, and `catalog.table` for
detailed, bounded discovery; a drift test reconciles their committed inventory
with the pinned source manifest.

## Your own content

A campaign is not limited to what ships. Its creatures, spells, conditions and
items go in a JSON **content pack** using the same format, parser and validation as
the bundled data — there is one format, not a second dialect. Drop a pack in
`.fivee-sim/content/` at the root of your campaign repository and the engine finds
it with no configuration. Setting `FIVEE_SIM_BUILTIN=exclude` drops the bundled SRD
content entirely, which is what lets you run this engine on material wholly your
own. Validation is strict and names never collide silently: an unknown key is an
error, because a mistyped `attack_bonus` would otherwise produce a creature that
fights wrongly and looks fine.

See [CONTENT-PACKS.md](souroldgeezer-fivee-sim/docs/CONTENT-PACKS.md) for the
format, the precedence rules, and a worked example.

## Contributing

[CLAUDE.md](CLAUDE.md), routed to Codex by [AGENTS.md](AGENTS.md), is the contributor
guide — layer boundaries, the determinism rules the kernel holds to, and the test
and lint commands.

## Licence

This repository's own code is MIT licensed — see [LICENSE](LICENSE).

Game rules content derives from the System Reference Document 5.2.1, which Wizards of
the Coast LLC released under CC-BY-4.0. [NOTICE](NOTICE) carries the required
attribution, records that the material has been modified, and states that the MIT
grant covers this project's code rather than the SRD material. SRD 5.2.1 covers only
part of the 2024 ruleset, and content absent from it cannot ship here.
