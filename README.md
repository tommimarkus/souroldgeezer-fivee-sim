# dndsim — a 5E-compatible simulation engine for Claude

A Claude Code marketplace and plugin that let Claude **run** tabletop combat
rather than imagine it. The rules live in a Python engine exposed over the Model
Context Protocol, so hit points, dice, initiative order, and conditions are
computed and owned by the engine — not recalled by a language model.

Compatible with fifth edition (2024 rules).

> **Status: v1 complete, unpublished.** The engine, MCP server, plugin, skill,
> and a starter SRD data slice all work end to end. Not yet published to a
> remote marketplace.

## Why an engine instead of prose

A model asked to track a fight will drift: hit points wander, a condition gets
forgotten a round later, a die roll conveniently favours the narrative. Moving
resolution into code makes the state authoritative and the randomness
reproducible. Ask for the same seed twice and you get the same fight twice.

The same kernel serves two different questions:

- **"What happens in this fight?"** — stateful tools step an encounter round by
  round, and Claude narrates what the engine reports.
- **"Is this build actually good?"** — analytics tools replay the same stepper
  across thousands of seeded iterations and report a distribution.

Both run on one rules kernel, so the statistics can never drift from the rules
the live encounter uses.

## Tool surface

| Group | Tools |
| --- | --- |
| Stateful | `encounter_create`, `encounter_state`, `encounter_act`, `encounter_advance` |
| Analytics | `simulate_rounds`, `simulate_dpr` |
| Primitives | `roll`, `check`, `save`, `lookup_rule` |

Every tool that consumes randomness accepts an optional `seed` and always reports
the seed it used, so no result is irreproducible after the fact.

## What is covered

Weapon attacks with reach and ranged bands, movement, Dash, Disengage, Dodge,
opportunity attacks, all fourteen conditions as a data table, death saves and
instant death, damage resistance and vulnerability, and a curated spell set with
saving throws, areas, upcasting, and concentration.

Deliberate limits, stated so they are not mistaken for bugs: geometry is a single
axis, so there is no flanking or cover; only SRD 5.2 content ships, and each
bundled stat block lists the printed traits the engine does not implement.

## Layout

```
.claude-plugin/marketplace.json   marketplace manifest
fivee-sim/                        the plugin
  .claude-plugin/plugin.json      manifest + MCP server declaration
  skills/                         how Claude drives the engine
  agents/
  engine/                         the Python rules engine
  scripts/                        MCP stdio launcher
scripts/hooks/                    local development hooks
```

## Requirements

- Python 3.11+ (developed against 3.14)
- [`uv`](https://docs.astral.sh/uv/) — the MCP launcher uses a `uv`-managed
  project environment, so no global installs are needed

## Trying it locally

Register this directory as a marketplace, then enable the plugin:

```bash
# In Claude Code, add a directory-source marketplace pointing at this repo,
# then enable the fivee-sim plugin. Verify the server independently with:
python3 scripts/check-mcp-handshake.py
```

## Development

```bash
cd fivee-sim/engine
uv run pytest && uv run ruff check . && uv run mypy
```

See [CLAUDE.md](CLAUDE.md) for the layer boundaries, the determinism rules, and
the character-device staging hazards in this workspace.

## Roadmap

- [x] Phase 0 — repo bootstrap, licence boundary, project guidance
- [x] Phase 1 — local ip-hygiene development hook
- [x] Phase 2 — marketplace + plugin skeleton
- [x] Phase 3 — rules kernel and test suite
- [x] Phase 4 — MCP server and launcher
- [x] Phase 5 — skill and agent
- [x] Phase 6 — SRD 5.2 data slice

Next, in rough order of value: widen the data slice, add
`encounter_legal_actions`, then reconsider the v1 exclusions —
encounter/monster builder, whole-adventuring-day attrition, and build-space
optimisation search.

## Licence and attribution

This repository's own code is MIT licensed — see [LICENSE](LICENSE).

Game rules content is derived from the System Reference Document 5.2, which
Wizards of the Coast LLC released under CC-BY-4.0. [NOTICE](NOTICE) carries the
required attribution verbatim, records that the material has been modified (a
subset transcribed into JSON, with some printed features not implemented), and
states that the MIT grant covers this project's code rather than the SRD material.

Note that SRD 5.2 does not cover the whole 2024 ruleset — some classes, species,
and monsters are excluded from it, and excluded content therefore cannot ship
here. [CLAUDE.md](CLAUDE.md) records the full boundary.
