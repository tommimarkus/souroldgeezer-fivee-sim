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

Fights can happen on a **battle map**: a JSON document you generate under a seed,
edit by voice or by hand in a browser, and save as a file. A finished fight
exports as a **replay bundle** that plays back in the browser, and a map hands off
to another virtual tabletop as Universal VTT.

| Plugin | Version | Skills | Claude Code agents |
|---|---:|---|---|
| `souroldgeezer-fivee-sim` | `2026.08.122` | [encounter-sim](souroldgeezer-fivee-sim/skills/encounter-sim/SKILL.md), [map-forge](souroldgeezer-fivee-sim/skills/map-forge/SKILL.md), [play](souroldgeezer-fivee-sim/skills/play/SKILL.md) | [adventure-prep](souroldgeezer-fivee-sim/agents/adventure-prep.md), [encounter-sim](souroldgeezer-fivee-sim/agents/encounter-sim.md), [game-master](souroldgeezer-fivee-sim/agents/game-master.md), [play-mechanics](souroldgeezer-fivee-sim/agents/play-mechanics.md), [typical-player](souroldgeezer-fivee-sim/agents/typical-player.md) |

Claude Code discovers the role profiles as named agents; Codex's play skill
spawns fresh children that read the same canonical role files. If a player child reports
tools, play stops by default rather than claiming structural isolation. Add an
explicit `test` or `playtest` request to collect findings and an author report.

## Install

Needs Python 3.11+, and nothing else. The engine has no runtime dependencies, so
there is nothing to install and no environment to build: the launcher puts the
bundled source on `sys.path` and runs it. [`uv`](https://docs.astral.sh/uv/) is
needed only to work *on* this repository — see [CLAUDE.md](CLAUDE.md).

The marketplace is not published yet, so point either host at a clone.

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

### Codex

```text
codex plugin marketplace add /absolute/path/to/this/repo
codex plugin add souroldgeezer-fivee-sim@souroldgeezer-tabletop
```

Start a new Codex session after installation so the bundled skills are loaded.
Once published, use the repository source instead of the local path:

```text
codex plugin marketplace add tommimarkus/souroldgeezer-fivee-sim
codex plugin add souroldgeezer-fivee-sim@souroldgeezer-tabletop
```

Under a plugin host the launcher runs the engine from a content-addressed copy in
that host's plugin-data directory, because a host may retire the plugin cache
while a session is still mid-fight. An upgrade therefore lands *beside* a live
copy rather than replacing it, and old copies are kept on purpose. To reclaim
space, close sessions using the plugin and remove directories under that
location's `src`; the launcher recreates what it needs. `PLUGIN_DATA` and
`CLAUDE_PLUGIN_DATA` override where it goes, and a plain checkout with no plugin
host runs straight from `engine/src` and copies nothing. [CLAUDE.md](CLAUDE.md)
has the full mechanism.

### Check it works

From a clone, this verifies the whole path independently of either host — it
starts the engine and prints every operation the running server serves:

```bash
python3 souroldgeezer-fivee-sim/scripts/fivee.py help
python3 souroldgeezer-fivee-sim/scripts/fivee.py stop
```

`python3 scripts/check-api-smoke.py` does the same thing without a human reading
the output. It boots the real launcher, runs a complete seeded fight over HTTP,
runs it again in a second server and once more through the `fivee` binary, and
requires all three to agree — then checks that the operations index, the OpenAPI
document and `fivee help` all describe the same contract. It needs nothing but
Python, prints `PASS`/`FAIL` per case, and exits non-zero if any of them failed.
Every server it starts is pointed at a scratch directory and stopped again, so a
run leaves nothing behind.

## Driving it

Normally you do not: you ask the assistant for a fight, and the bundled skills
teach it the rest. What follows is the same surface, by hand.

**Reaching the command.** `fivee` is the name used throughout the docs. It is on
`PATH` only if you installed the engine as a Python package; from a plugin
install or a clone, the launcher is the entry point and takes identical
arguments:

```bash
command -v fivee || echo "python3 /path/to/souroldgeezer-fivee-sim/scripts/fivee.py"
```

**The surface describes itself**, by reading the server it is about to call, so
neither of these can go stale:

```bash
fivee help                    # every operation, grouped
fivee help encounter.act      # one operation's arguments, and a line to paste
```

`fivee encounter.act` and `fivee encounter act` are the same command. Results are
JSON on **stdout** and nothing else, so `$(fivee ...)` is always parseable; prose,
refusals, and the `etag` note go to **stderr**. An argument no flag grammar should
try to spell — a creature list, a map document — goes in `--json '{...}'`, or
`--json -` to read it from stdin; given both, flags override the JSON's keys.

When a caller needs a few facts rather than the whole document, repeat
`--select NAME=/json/pointer`. The pointer is RFC 6901, missing fields are
reported as `null` without changing a successful operation's exit code, and
`--raw` prints one selected scalar without JSON quotes:

```bash
fivee --run run-1 encounter.state enc-1 --select turn=/turn --select over=/over
fivee --run run-1 encounter.state enc-1 --select turn=/turn --raw
fivee --run run-1 encounter.act enc-1 --kind dodge --select events=/events
```

Selection is local output projection after the server answers. It never adds a
second request or widens a player-safe `--as` response.

Exit codes separate the four failures that have four different fixes: **2** the
command was wrong, **3** the engine refused, **4** the engine broke, **5** nothing
answered.

### Runs, adventures, and a fight

Use `fivee run.create` for a scratch map or standalone encounter; it returns a
`run_id`, which is always the value after `--run`. `adventure.create` is different:
it requires a name and opening scene/party (with an optional seed), returns distinct
`run_id`, `adventure_id`, and `encounter_id`, and creates the opening chapter at once.
The opening scene requires a map. Its `team=party` hints are unique/unoccupied
capacity assigned in request order, ground then stored-level/feature order. The
scene mode, movement rule, map, and nonparty cast are preserved; the seed override
applies only to the opening. Old adv-* workspace selectors deliberately break.

### A fight, end to end

```bash
fivee adventure.create --name "The Sunken Bell"              # returns adv-1
fivee --run run-1 adventure.encounter adv-1 --if-match <version> --seed 41 --json '{"combatants": [
  {"name": "Thora", "team": "party", "ac": 16, "max_hp": 30, "position": [0, 0],
   "attacks": [{"name": "Longsword", "attack_bonus": 5, "damage": "1d8+3",
                "damage_type": "slashing", "kind": "melee"}]},
  {"monster": "Goblin Warrior", "label": "Goblin A", "team": "monsters",
   "position": [15, 0]}
]}'
```

Each combatant is either a bundled stat block named by `monster` or an explicit
build. The second call returns the `encounter_id` — `enc-1` in a fresh run — the
seed it used, and the full state including initiative order and whose turn it is.
From there:

```bash
fivee --run run-1 encounter.state enc-1                                    # authoritative
fivee --run run-1 encounter.act enc-1 --kind move --to-position '[5, 0]'
fivee --run run-1 encounter.act enc-1 --kind attack --target "Goblin A" --attack Longsword
fivee --run run-1 encounter.advance enc-1                                  # end the turn
fivee --run run-1 encounter.finalize enc-1                                 # freeze replay v3
```

The two calls a fight makes hundreds of times answer with what *changed*:
`encounter.act` and `encounter.advance` default to `--view delta` and carry
`state_delta` rather than `state`, with `state_sha256` over the state it stands
for. `encounter.create` and `encounter.resume` answer whole, because they are
what a delta is measured against, and `--view full` gets that payload from any
of the four. A roster inside a delta is the complete cast with each entry cut to
what moved, so a name that is missing is gone rather than unchanged.

Every event carries its arithmetic in a `detail` field, which is what makes a
fight auditable rather than asserted:

```text
Longsword: d20 [8] +5 = 13 vs AC 15 -> miss
```

An illegal action is refused with a reason on stderr and exit code 3 — out of
reach, no slots left, speed 0 while Grappled. Read the reason and adapt; do not
retry hoping for a different answer.

The history survives the process. Creation, every attempt, and every result are
fsynced into a hash-chained journal, so `fivee --run <adv-id> encounter.list`,
`fivee --run <adv-id> encounter.resume <id>`, and
`fivee --run <adv-id> encounter.log <id>` recover and page a
fight that outlived the server that ran it.

## The browser pages

`fivee serve` starts the engine and reports three URLs, along with the
directories this launch is using:

| Reported as | Page |
|---|---|
| `url` | the landing page — links to both tools and lists the operations it fetches from the running server |
| `editor_url` | the **map editor**: paint terrain, place features and fixtures, work storeys, paint ground height |
| `viewer_url` | the **replay viewer**: play a finished fight back frame by frame |

`url` is the index, **not** the editor — hand over `editor_url` when someone asked
for the editor. `fivee stop` shuts the server down.

The viewer draws each creature's facing as a **sight cone**: a 90° wedge reaching
six squares, on by default and toggleable from the page. It says which way a
creature is turned and nothing more — no wall stops it and it is not a sight
range. Line of sight is decided in the kernel, and a second implementation in the
browser would be free to disagree with the ruling that actually resolved the
fight.

An exported replay can also be `--embed`ed into a single self-contained HTML file
that plays in any browser, with no server and no install.

To see the whole-run viewer without preparing an adventure first, generate the
checked-in showcase:

```bash
fivee-sim-adventure-replay-sample
```

It writes `.fivee-sim/replays/adventure-replay-showcase.html`: a deterministic,
self-contained exploration → combat → aftermath run with forty-plus events and
continuous playback across both chapter boundaries. The existing
`fivee-sim-replay-sample` remains the focused single-fight animation showcase.

## Operations

One versioned surface under `/api/v1`, every operation reachable as
`fivee <group>.<verb>`:

| Group | What it does |
| --- | --- |
| `encounter` | start a fight, read its state, act, advance, note, resume, finalize, export a replay |
| `adventure` | link fights in order, carrying each party's ending state into the next |
| `map` | generate under a seed, render, query geometry, edit, read and write saved maps, export Universal VTT |
| `replay` | list, read, and validate replay bundles |
| `analytics` | Monte Carlo win rates, damage per round, and route-timing checks |
| `dice` | one-off rolls, ability checks, and saving throws outside a tracked fight |
| `rules` | exact-name lookup of loaded conditions, spells, creatures, items, and terrain |
| `catalog` | bounded search, one structured record, one printed table |
| `content` | what is loaded, validate a pack, load packs or switch the bundled slice |
| `server` | liveness, the operation index, the OpenAPI document, shutdown |

**No list here is the source.** `fivee help` renders `GET /api/v1/operations` from
the running server, and `fivee help <operation>` reads that operation's arguments
out of its OpenAPI document — so neither can describe an operation the server does
not route, or miss one it does. `engine/src/fivee_sim/web/routes.py` is the single
declaration all three read.

Every operation that consumes randomness accepts an optional `seed` and always
reports the seed it used, so no result is irreproducible after the fact. Durable
writes are guarded rather than merged: the state-changing encounter operations and
`map.put` take an `If-Match` carrying the version you read, and a write landing on
state that has already moved on is refused with 409 rather than silently applied.

## Where your files live

Project configuration lives in `.fivee-sim/config.toml`. The CLI walks upward
from the invocation workspace and selects the nearest one; use the global
`--config PATH` option to select a different file explicitly:

```bash
fivee --config /abs/campaign/.fivee-sim/config.toml content.status
```

Paths in the file resolve against the directory containing it — normally the
project's `.fivee-sim/` directory:

```toml
format_version = 1

[content]
builtin = "include"
paths = ["content"]

[storage]
maps = "maps"
replays = "replays"
scenes = "scenes"
encounters = "encounters"
adventures = "adventures"
blobs = "blobs"
runs = "runs"

[development]
reload = false
```

`content.builtin` is `include` or `exclude`. `storage.maps` and
`storage.replays` may each be one string or an array of strings; `storage.scenes`,
`storage.encounters`, `storage.adventures`, `storage.blobs`, and `storage.runs`
are one string each. Omitted settings default to the sibling `maps/`, `replays/`,
`scenes/`, `encounters/`, `adventures/`, `blobs/`, and `runs/` directories, bundled
content included, development reload off, and the sibling `content/` directory
when it exists.

The mutable workspace is an **adventure run**. Start it without a selector:

```bash
fivee adventure.create --name "The Sunken Bell"   # returns adv-1
```

That returned adventure id is the global selector for every later command:
`fivee --run <adv-id> ...`. Map-only work follows the same rule; create the
adventure first, then save the map inside it. A command with no run may inspect
the configured shared inputs but refuses a write. `--run legacy` exposes the old
encounter/adventure/blob roots for explicit **read-only** inspection; it is not a
migration or a writable compatibility mode.

The resulting project layout is:

| Path | Holds |
|---|---|
| `.fivee-sim/config.toml` | project configuration |
| `.fivee-sim/content/` | your own content packs |
| `.fivee-sim/maps/`, `scenes/`, `replays/` | project inputs; shared and immutable to a run |
| `.fivee-sim/runs/<run-id>/` | one isolated mutable workspace, with `run.json`, its own `maps/`, `scenes/`, `replays/`, `encounters/`, `adventures/<adv-id>.json`, and `blobs/` |
| `.fivee-sim/runtime/control/` | rendezvous for unscoped reads and `adventure.create` |
| `.fivee-sim/runtime/<adv-id>/` | rendezvous for the server bound to that run |
| `.fivee-sim/encounters/`, `adventures/`, `blobs/` | pre-run stores, readable only through `--run legacy` |

Reads use an overlay: a run-local map, scene, or replay wins over the configured
shared input with the same id, and listings label the scope. Editing a shared
map or scene is guarded copy-on-write into the run; the shared bytes never
change. Run artifacts remain run-local. There is no publish, promote, cleanup,
or automatic migration operation; copy an export elsewhere explicitly when it
needs to leave the workspace.

A selected configuration file owns all project-facing settings; environment
variables are not merged over it. For compatibility, and only when no file is
selected, the deprecated `FIVEE_SIM_PROJECT_DIR`, `FIVEE_SIM_CONTENT`, `FIVEE_SIM_BUILTIN`,
`FIVEE_SIM_MAPS`, `FIVEE_SIM_REPLAYS`, `FIVEE_SIM_SCENES`,
`FIVEE_SIM_ENCOUNTERS`, `FIVEE_SIM_ADVENTURES`, `FIVEE_SIM_BLOBS`,
`FIVEE_SIM_RUNS`, and
`FIVEE_SIM_RELOAD` retain their
previous meanings.
Variables supplied by a plugin host for its own bootstrap are process plumbing,
not project configuration. `fivee content.status` reports the selected
configuration source and path (and marks the environment fallback deprecated);
`fivee serve` and `fivee server.ping` report the
directories in use. Read those rather than assuming.

## What is covered

The bundled reference catalog carries reviewed structured facts for the whole of
SRD 5.2.1 — every section and printed table, with stat block, spell, magic item,
and glossary identities. Every entry is closed; no catalog review remains pending.
[COVERAGE.md](souroldgeezer-fivee-sim/docs/COVERAGE.md) is the generated totals
report, and it describes the **bundled** slice only — `fivee content.status` is
what a session has actually loaded.

Execution is a much smaller, explicit subset: 6 creatures, 4 spells, 14
conditions, 13 damage types, 10 action kinds, and no bundled items. Weapon attacks
with reach and ranged bands, movement, Dash, Disengage, Dodge, opportunity
attacks, death saves, and damage resistance and vulnerability all work; spells
carry saving throws, areas, upcasting, and concentration. Catalog entries
separately report whether they are `reference_only`, `partial`, or `executable`.

Limits worth knowing before you report one as a bug:

- **No character building.** No classes, species, backgrounds, feats or levelling.
  Combatants are described directly by their statistics, the way a stat block
  presents them — there is no character sheet deriving those numbers.
- **No skill proficiencies.** Creatures have ability modifiers and nothing else,
  so every check is a raw ability check: no proficiency bonus, no Expertise, no
  Help. Set a DC as if the character were untrained.
- **Geometry depends on whether there is a map.** On a battle map of 5-ft squares
  you get terrain costs, walls, line of sight, cover (+2/+5 to AC and Dexterity
  saves, with total cover refusing an attack outright), doors and stateful
  fixtures, multiple storeys, ground height, Walk/Climb/Swim/Fly speeds,
  Darkvision/Blindsight, and light. Without one the plane is open and featureless.
  Either way, **height is charged to movement alone** — a slope costs difficult
  terrain and a cliff an extra foot per foot, while sight, cover and area
  templates are measured flat, so a ridge screens nobody and standing on a tower
  is no advantage in itself. Absent throughout: falling and fall damage, jumping,
  creature size and squeezing, flanking, and forced movement — nothing can shove
  anyone off a ledge.
- **Reactions are automatic, and there are few.** Opportunity attacks fire on
  their own, as do authored stat-block reactions such as Redirect Attack. There
  are no readied actions, no reaction spells, and no legendary or lair actions.
- **Nothing models the time between fights.** No exploration, resting, or
  recovery: an encounter begins and ends, and resources do not regenerate in
  between. Standalone dice rolls and one route-timing check are the only
  operations that run outside an encounter at all.
- **Exhaustion is not implemented**, though SRD 5.2.1 defines it, and Frightened
  applies its disadvantage unconditionally because a condition does not record
  which creature caused the fear.
- **Auto-play is greedy, not tactical.** The policy behind the analytics
  operations takes the highest expected damage available each turn. It never casts
  a non-damaging spell, never operates a map fixture, and does not husband spell
  slots — so a batch is a *floor* for a control build rather than a measurement of
  one.

Use `catalog.search`, `catalog.get`, and `catalog.table` for detailed, bounded
discovery; a drift test reconciles their committed inventory with the pinned
source manifest.

## Your own content

A campaign is not limited to what ships. Its creatures, spells, conditions,
terrain and items go in a JSON **content pack** using the same format, parser and
validation as the bundled data — there is one format, not a second dialect. Put
a pack in the configured `content` directory; the default sibling
`.fivee-sim/content/` is loaded when it exists. Set `builtin = "exclude"` under
`[content]` to drop the bundled SRD content entirely, which is what lets you run
this engine on material wholly your own. Validation is strict and names never
collide silently: an unknown key is an error, because a mistyped `attack_bonus`
would otherwise produce a creature that fights wrongly and looks fine.

`fivee content.configure` remains available for a deliberate temporary overlay
in the running server. It neither edits `config.toml` nor survives a restart; use
the file for persistent project configuration.

Encounters in progress keep the content they started with, so changing packs
mid-fight cannot strip the creature currently taking its turn.

See [CONTENT-PACKS.md](souroldgeezer-fivee-sim/docs/CONTENT-PACKS.md) for the
format, the precedence rules, and a worked example.

## Documentation

| Document | Covers |
|---|---|
| [encounter-sim](souroldgeezer-fivee-sim/skills/encounter-sim/SKILL.md) | running and narrating a fight — the skill the assistant loads |
| [map-forge](souroldgeezer-fivee-sim/skills/map-forge/SKILL.md) | making, editing, and fighting on battle maps |
| [play](souroldgeezer-fivee-sim/skills/play/SKILL.md) | running a written adventure as a table with agent or human seats; explicit test/playtest mode also reports what broke |
| [MAPS.md](souroldgeezer-fivee-sim/docs/MAPS.md) | the map document field by field, edit operations, the editor, the replay bundle, UVTT |
| [CONTENT-PACKS.md](souroldgeezer-fivee-sim/docs/CONTENT-PACKS.md) | the content pack format and the rules the loader enforces |
| [COVERAGE.md](souroldgeezer-fivee-sim/docs/COVERAGE.md) | generated catalog and executable totals |
| [SECURITY.md](SECURITY.md) | what this software exposes, and how to report a vulnerability |
| [CLAUDE.md](CLAUDE.md) | contributor guidance — layer boundaries, determinism rules, test and lint commands |

## Security

The server is a single-user local tool and is built as one: it binds `127.0.0.1`
only, every `/api/*` request must carry a random per-launch token or is refused
with 401, and a request whose `Host` is neither `127.0.0.1` nor `localhost` is
refused with 403 so a DNS-rebinding page cannot reach it. The served pages receive
the launch's token by injection, so there is nothing to pass along by hand.
Replay integrity hashes detect alteration but are not author signatures, so
validate a bundle that came from somewhere else. See [SECURITY.md](SECURITY.md).

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
