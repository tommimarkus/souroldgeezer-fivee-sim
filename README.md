# souroldgeezer-fivee-sim

A 5E-compatible combat simulation engine for Claude Code™ and Codex, by Sour Old
Geezer. This repository is the marketplace source and the shared plugin tree.

Compatible with fifth edition (2024 rules).

> **Status: v1 complete.** Engine, MCP server, plugin, skill, a starter SRD data
> slice, and user-defined content packs all work end to end.

## What this is

A model asked to track a fight will drift: hit points wander, a condition gets
forgotten a round later, a die roll conveniently favours the narrative. Here the
rules live in a Python engine exposed over the Model Context Protocol, so hit
points, dice, initiative order, and conditions are computed and owned by the
engine rather than recalled. Ask for the same seed twice and you get the same
fight twice.

One kernel answers two questions:

- **"What happens in this fight?"** — stateful tools step an encounter round by
  round, and the assistant narrates what the engine reports.
- **"Is this build actually good?"** — analytics tools replay that same stepper
  across thousands of seeded iterations and report a distribution.

Because both run on the one kernel, the statistics cannot drift from the rules
live play uses.

| Plugin | Version | Skill | Claude agent |
|---|---:|---|---|
| `souroldgeezer-fivee-sim` | `2026.08.9` | [encounter-sim](souroldgeezer-fivee-sim/skills/encounter-sim/SKILL.md) | [encounter-sim](souroldgeezer-fivee-sim/agents/encounter-sim.md) |

## Install

Needs Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). The MCP launcher builds
its own `uv`-managed environment on first run, so there is nothing to install
globally.

The marketplace is not published yet, so point either host at a clone.

### Codex

```text
codex plugin marketplace add /absolute/path/to/this/repo
codex plugin add souroldgeezer-fivee-sim@souroldgeezer-tabletop
```

Start a new Codex session after installation so the bundled skills and MCP
server are loaded.

Codex keeps this plugin's generated runtime under
`${CODEX_HOME:-$HOME/.codex}/plugins/data/souroldgeezer-fivee-sim-souroldgeezer-tabletop`,
outside the versioned plugin cache. The launcher deliberately builds a fresh
environment there instead of moving any cache-local `engine/.venv`, which may be
partial after an interrupted refresh. To force a clean rebuild, close sessions
using the plugin, remove the runtime location's `venv` directory, and start a new
session; the launcher recreates it on demand. Explicit `UV_PROJECT_ENVIRONMENT`,
`UV_CACHE_DIR`, `PLUGIN_DATA`, and `CLAUDE_PLUGIN_DATA` settings still take
precedence. A direct checkout without a plugin host continues to use
`engine/.venv`.

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

From a clone, `python3 scripts/check-mcp-handshake.py` verifies the MCP server
independently of either host.

## Tools

| Group | Tools |
| --- | --- |
| Stateful | `encounter_create`, `encounter_state`, `encounter_act`, `encounter_advance`, `encounter_note`, `encounter_log`, `encounter_list`, `encounter_resume`, `encounter_finalize` |
| Replay | `replay_export`, `replay_validate` |
| Analytics | `simulate_rounds`, `simulate_dpr` |
| Primitives | `roll`, `check`, `save`, `lookup_rule` |
| Catalog | `catalog_search`, `catalog_get`, `catalog_table` |
| Content | `content_status`, `content_configure`, `content_validate` |

Every tool that consumes randomness accepts an optional `seed` and always reports
the seed it used, so no result is irreproducible after the fact.

## What is covered

The bundled reference catalog inventories all 2,062 SRD 5.2.1 sections and 227
printed tables, including links for 336 stat blocks, 339 spells, and 155 glossary
terms. Its metadata skeleton is complete; structured-fact review proceeds in
bounded batches and every entry reports its current `fact_status`.

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
totals report. Use `catalog_search`, `catalog_get`, and `catalog_table` for detailed,
bounded discovery; a drift test reconciles their committed inventory with the
pinned source manifest.

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
