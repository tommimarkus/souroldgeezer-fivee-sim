# dndsim — a 5E-compatible simulation engine for Claude

A Claude Code marketplace and plugin that let Claude **run** tabletop combat
rather than imagine it. The rules live in a Python engine exposed over the Model
Context Protocol, so hit points, dice, initiative order, and conditions are
computed and owned by the engine — not recalled by a language model.

Compatible with fifth edition (2024 rules).

> **Status: early development.** The bootstrap and project guidance are in
> place; the engine, plugin, and skill are being built out in phases. See
> "Roadmap" below for what exists today.

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

## Planned tool surface

| Group | Tools |
| --- | --- |
| Stateful | `encounter_create`, `encounter_state`, `encounter_act`, `encounter_advance` |
| Analytics | `simulate_rounds`, `simulate_dpr` |
| Primitives | `roll`, `check`, `save`, `lookup_rule` |

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

## Roadmap

- [x] Phase 0 — repo bootstrap, licence boundary, project guidance
- [ ] Phase 1 — local ip-hygiene development hook
- [ ] Phase 2 — marketplace + plugin skeleton
- [ ] Phase 3 — rules kernel and test suite
- [ ] Phase 4 — MCP server and launcher
- [ ] Phase 5 — skill and agent
- [ ] Phase 6 — SRD 5.2 data slice

Deliberately out of scope for v1: encounter/monster builder, whole-adventuring-day
attrition, and build-space optimisation search.

## Licence and attribution

This repository's own code is MIT licensed — see [LICENSE](LICENSE).

Game rules content is derived from the System Reference Document 5.2, which
Wizards of the Coast LLC released under CC-BY-4.0. The required attribution is
carried verbatim in [NOTICE](NOTICE) and must not be modified or extended.

Note that SRD 5.2 does not cover the whole 2024 ruleset — some classes, species,
and monsters are excluded from it, and excluded content therefore cannot ship
here. [CLAUDE.md](CLAUDE.md) records the full boundary.
