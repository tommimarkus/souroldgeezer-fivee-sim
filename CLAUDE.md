# Repository guidance

A Claude Code marketplace + plugin providing a 5E-compatible simulation engine:
a pure Python rules kernel exposed over MCP, plus the skill that teaches Claude
to drive it. See [README.md](README.md) for the project overview.

## Environment hazards — read before any `git add`

This workspace mounts a number of paths as **character devices** (`/dev/null`,
major/minor `1,3`), not regular files. They are not writable and not
committable.

**In the repo root:** `.bashrc`, `.bash_profile`, `.profile`, `.zshrc`,
`.zprofile`, `.gitconfig`, `.gitmodules`, `.mcp.json`, `.ripgreprc`, `.idea`,
`.vscode`. All are listed in `.gitignore`.

**Everything under `.claude/`** — including `settings.json`,
`settings.local.json`, `hooks/`, `skills/`, `agents/`, and `commands/`. This has
a real consequence: **this repo cannot host project-level Claude settings or
hooks.** Do not try to write them; the content silently goes to `/dev/null`.
Local development hooks live in `scripts/hooks/` and are wired from the user's
own `~/.claude/settings.json` — see "Local development hook" below.

### Worktrees: only `add` is special

Implementation work belongs in a worktree rather than a branch in the primary
checkout, so several agents can work the repo at once. The sandbox only permits
writes inside the project, so they live at `.worktrees/<name>`. Expect `main` to
move under you, and expect other `.worktrees/*` entries to belong to live agents.

**`git worktree add` fails under the sandbox**, which presents `.git/worktrees`
as read-only:

```
fatal: could not create directory of '.git/worktrees/<name>': Read-only file system
```

Run that one command with the sandbox disabled. Everything else — `remove`,
`prune`, `commit`, `merge`, `rm -rf` — works sandboxed.

A fresh worktree has no `.venv`, and uv's cache is project-relative
(`cache-dir = ".cache/uv"`), so a new one starts empty and `uv sync` would want
network it does not have. Point it at the primary checkout's cache instead, and
keep passing the variable to `uv run` in that worktree:

```bash
cd .worktrees/<name>/souroldgeezer-fivee-sim/engine
export UV_CACHE_DIR=/home/souroldgeezer/repos/dndsim/souroldgeezer-fivee-sim/engine/.cache/uv
uv sync
```

Closeout is ordinary:

```bash
git branch --merged                             # confirm before deleting
git worktree remove --force .worktrees/<name>
git worktree prune
git branch -d <branch>
```

`prune` prints `failed to delete '.git/worktrees/fivee-sim': Device or resource
busy` and still exits 0. That is one stale entry from a session predating the
rename — the devcontainer holds character-device mounts over
`.git/worktrees/fivee-sim/commondir` and `config.worktree` — and it has nothing
to do with the worktree you just removed. Leave it; it goes when the container is
recreated, and do not try to unmount it. Worktrees created today carry no such
mounts, in the checkout or in the git metadata, and remove cleanly.

### Staging discipline

Never `git add -A` or `git add .` in this repo. Staging a character device
either errors or commits something meaningless.

```bash
git status --porcelain   # must show only files you intend to commit
git add <path> ...       # always by name
```

If a character device ever appears in `git status`, extend `.gitignore` — never
stage it.

## Licence boundary — the hard constraint

Game rules content comes from the **System Reference Document 5.2**, released by
Wizards of the Coast LLC under **CC-BY-4.0**. Three rules follow, and they are
not negotiable:

**1. The attribution ships verbatim.** [NOTICE](NOTICE) opens with the exact
required statement, on a single line so it can be matched byte-for-byte. Never
reword or re-wrap that sentence.

NOTICE also carries two statements about **our** work, which are required and must
not be dropped. CC-BY-4.0 §3(a)(1)(B) obliges us to indicate that we modified the
licensed material — we transcribe a subset into JSON and omit some printed
features — and the licence split has to be explicit so the MIT grant is not read
as covering the SRD material. Neither is additional attribution to Wizards, so
neither conflicts with rule 2.

Both copies must stay identical: the repo-root one and `souroldgeezer-fivee-sim/NOTICE`, which
is the copy that actually ships to installs.

**2. No branding in published metadata.** The SRD's own legal page states:
*"Please do not include any other attribution to Wizards or its parent or
affiliates other than that provided above. You may, however, include a statement
on your work indicating that it is 'compatible with fifth edition' or '5E
compatible.'"*

So the `name` and `description` fields of `plugin.json`, `marketplace.json`, and
every skill/agent frontmatter use **5E-compatible** wording only — never
"Dungeons & Dragons", "D&D", "DnD", "5.5e", or "Wizards of the Coast". The
`dndsim` directory name is local to this machine and is never published.

Descriptive nominative reference in repo-internal prose (this file, the README)
is fine and is why those files are not scanned for marks.

**3. Non-SRD content never enters engine data.** SRD 5.2 omits parts of the 2024
ruleset — the Artificer class, the Aasimar species, and the Beholder are known
examples. Content outside the SRD is not licensed to us. Every data record
carries a provenance field naming SRD 5.2; if a name cannot be traced to the
SRD, it does not ship.

This constrains what **we redistribute**, not what a user may load. A campaign's
own content packs are outside the repo by design and are not subject to our
denylist — their content is theirs. That is why the local hook scopes its non-SRD
name check to `souroldgeezer-fivee-sim/engine/src/fivee_sim/data/`: extending it to
user packs would be both useless and wrong.

Before publishing, run the `souroldgeezer-audit:ip-hygiene` skill over the
plugin surface as the release gate. The local hook is a tripwire, not a
substitute for it.

## Local development hook

`scripts/hooks/ip-hygiene-check.sh` is a fast `PostToolUse` tripwire for the
three rules above. It is **activated by the presence of
`.ip-hygiene-local.conf`** in the project root, which also holds its tuning
knobs (surface globs, mark denylist, non-SRD denylist, expected attribution
string).

The wiring lives only in the developer's `~/.claude/settings.json`, never in the
plugin and never published. Because that wiring is user-global, the marker-file
guard is applied twice — once in the settings command and once in the script —
so the hook is inert in every other project.

On a finding it exits 2 with detail on stderr, which feeds the problem back for
fixing in-loop. Clean means exit 0 and silence.

Scope is the subtle part, so it is pinned by tests — run
`bash scripts/hooks/test-ip-hygiene-check.sh` after touching the hook or its
conf. The negative cases matter most: they assert that this file, which has to
quote every forbidden string, and rules data naming SRD-present creatures do
**not** trip the tripwire.

## Architecture

**The engine lives under the plugin root**, at `souroldgeezer-fivee-sim/engine/`. This is not
cosmetic: `${CLAUDE_PLUGIN_ROOT}` resolves to the plugin directory, so an engine
at the repository root would not ship to installs.

**The kernel is pure.** `fivee_sim.kernel` performs no I/O, reads no clock, and
never touches ambient randomness. Every function that rolls takes an explicit
`Random` instance. This is what makes results reproducible under a seed and the
tests meaningful — treat a module-level RNG or an `import time` in the kernel as
a defect.

**Analytics is a loop over the stepper**, not a parallel implementation.
`analytics/montecarlo.py` replays the same encounter stepper the stateful tools
use. A test pins that a 1-iteration analytics run equals a single stateful run
at the same seed; if the two ever diverge, the statistics are lying.

**The MCP layer is a thin adapter.** `fivee_sim.mcp_server.server` validates
input, calls the kernel, serialises results. No rules logic belongs there. The
package is `mcp_server`, not `mcp`, so it can never be confused with the
third-party `mcp` distribution it imports.

`stdout` of the MCP launcher is the JSON-RPC channel. Anything diagnostic must
go to `stderr`, or the protocol breaks. `scripts/check-mcp-handshake.py` exists
to catch exactly that: it requires every line the server emits on stdout to parse
as JSON.

**Layer boundaries.** `kernel/` holds the primitives — dice, resolution,
conditions, attacks, spells, items — and knows nothing about creatures; callers pass
the handful of values a roll depends on. `model/` owns creatures and is the only
place combat state changes. Spell and item definitions live in `kernel/` rather than
a separate layer because they are resolution primitives like the rest.

**Content is data, and the bundled slice is not privileged.** `content.py` loads
every pack — including `data/srd/*.json` — through one parser and one validator, and
returns an immutable `ContentRegistry`. There is one exception, and it is forced:
the SRD **condition table lives in `kernel/conditions.py`**, because it is the
default every kernel function falls back to and the kernel may not do I/O.
`content.py` renders that table as a synthetic pack so it still goes through the
same validation.

**Conditions are strings, not enum members.** `Condition` remains as constants for
the SRD set, but a pack's condition is a plain `str`. Two consequences: never call
`.value` on a condition, and never look one up in a module-level table — every
function that consults conditions takes the table, for the same reason every
rolling function takes a `Random`. A green test suite does **not** prove this
works, because every built-in condition is a `StrEnum` member and answers to both;
`tests/test_content.py::TestCustomConditions` is the test that does.

**An encounter captures its content tables by value.** `content_configure` builds a
new registry rather than mutating the live one, so a fight in progress finishes
under the content it started with — switching to `exclude` mid-fight would otherwise
strip the creature currently taking its turn. `Encounter.__init__` also *injects*
its condition table into every combatant, which is load-bearing rather than tidy:
`analytics/montecarlo.py` builds the `simulate_dpr` dummy itself, where no caller
can pass a table.

**Every tool reports its seed.** A tool called without one picks a seed and
returns it, so no result is ever irreproducible.

## Tooling

```bash
cd souroldgeezer-fivee-sim/engine
uv run ruff check .              # E,F,W,I,UP,B — line length 100
uv run mypy                      # strict, configured in pyproject.toml
uv run pytest

uv run python -m fivee_sim.coverage   # regenerate docs/COVERAGE.md

# From the repo root: real JSON-RPC against the real launcher.
python3 scripts/check-mcp-handshake.py
bash scripts/hooks/test-ip-hygiene-check.sh
```

**`docs/COVERAGE.md` is generated, never hand-edited.** Adding a creature, spell,
condition, or action means regenerating it; `tests/test_coverage.py` fails
otherwise. The "not supported" section is the exception — it is prose in
`coverage.py`, because absence cannot be derived from the data and it is the part a
reader most needs.

It describes the **bundled** slice only. What a session actually has loaded is the
`content_status` tool's answer, and the skill says so — a generated document cannot
know about a pack it has never seen.

`uv`'s cache is redirected to `souroldgeezer-fivee-sim/engine/.cache/uv` because the default
`~/.cache/uv` is read-only in the sandboxed development environment.

### The virtual environment

`uv` builds it; nothing else should. `uv sync` in the engine directory creates
`.venv` with the dev group included, which is what `uv run pytest` and `uv run
mypy` use.

At runtime the launcher **execs the venv's own console script** rather than going
through `uv run`. That keeps uv out of the spawn path — one process instead of
two, and uv is needed only when the environment has to be built. The handshake
check passes with `uv` removed from `PATH` entirely, which is the test of it.

The launcher syncs with `--no-dev`, so a venv it built has no test tooling. That
only happens on a cold start; `uv run pytest` re-syncs the dev group
automatically, so the two uses do not fight.

**A venv is not portable.** Its console scripts hard-code an absolute interpreter
path in their shebang, so moving or renaming the plugin directory leaves a venv
that exists and looks executable but cannot start — this bit the rename to
`souroldgeezer-fivee-sim`. The launcher now detects it by checking the shebang
still resolves, and rebuilds. If you relocate the repo and something behaves
oddly, `rm -rf` the venv rather than debugging it.

## Conventions

Mirrors the sibling `souroldgeezer` marketplace at `../skills`:
date-based plugin versions (`2026.07.1`); `plugin.json` carrying `name`,
`version`, `description`, `author`, `license`; `AGENTS.md` as a pointer to this
file rather than a second copy of it.
