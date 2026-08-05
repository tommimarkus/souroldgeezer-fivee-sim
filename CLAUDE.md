# Repository guidance

A Claude Code and Codex marketplace plugin providing a 5E-compatible simulation
engine: a pure Python rules kernel served over a local HTTP API, the `fivee`
command that drives it, plus the skills that teach the active assistant to use
them. See [README.md](README.md) for the project overview.

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
own `~/.claude/settings.json` — see "Local development hooks" below.

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
# The primary checkout's cache, wherever this clone lives — from inside a
# worktree `--git-common-dir` resolves to the primary checkout's `.git`.
# Never hard-code an absolute path here: this file is published.
primary="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
export UV_CACHE_DIR="$primary/souroldgeezer-fivee-sim/engine/.cache/uv"
uv sync
```

Closeout is ordinary:

```bash
git branch --merged                             # confirm before deleting
git worktree remove --force .worktrees/<name>
git worktree prune
git branch -d <branch>
```

Three of those four print errors and succeed anyway, and one of them exits 255
while doing it. None of that means closeout failed. **Verify with `git worktree
list` and `git branch`, never with an exit code.**

- `git worktree remove --force` prints `error: failed to delete
  '.git/worktrees/<name>': Device or resource busy` and exits **255**, having
  succeeded in every way that matters: the checkout is gone, the branch is
  intact, `git worktree list` no longer shows it. All it failed at was deleting
  the registration directory. An agent that reads 255 as a failed closeout and
  starts repairing things will do damage the error never asked for.
- `git worktree prune` then offers to clear that leftover registration, fails
  the same way, and exits 0 — one such line per leftover, whatever worktree you
  were removing.
- `git branch -d` prints `could not lock config file .git/config` and
  `warning: update of config-file failed`, because `.git/config` is itself a
  read-only mount. It still prints `Deleted branch <name>`, and it is deleted.

Those leftovers are permanent, and there is one per worktree ever created. The
devcontainer mounts `/dev/null` over every registration's `config.worktree`, and
a bind-mounted file cannot be unlinked, so the directory holding it cannot be
removed — not by `remove`, not by `prune`, not by hand. `remove` does delete
`gitdir` first, which is both why prune keeps offering to clear them and why
`git worktree list` never shows them: it reads `gitdir`. Expected under this
devcontainer, not a failed closeout. Leave them, and do not try to unmount them.

A worktree's own checkout is clean — no character devices anywhere inside it.
Only its `.git/worktrees/<name>/` registration is mounted over.

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

Game rules content comes from the **System Reference Document 5.2.1**, released by
Wizards of the Coast LLC under **CC-BY-4.0**. Three rules follow, and they are
not negotiable:

**1. The attribution ships verbatim.** [NOTICE](NOTICE) opens with the exact
required statement, on a single line so it can be matched byte-for-byte. Never
reword or re-wrap that sentence.

NOTICE also carries two statements about **our** work, which are required and must
not be dropped. CC-BY-4.0 §3(a)(1)(B) obliges us to indicate that we modified the
licensed material — we transcribe a facts-only catalog and a smaller executable
subset into JSON while omitting source prose and unsupported mechanics — and the
licence split has to be explicit so the MIT grant is not read
as covering the SRD material. Neither is additional attribution to Wizards, so
neither conflicts with rule 2.

Both copies must stay identical: the repo-root one and `souroldgeezer-fivee-sim/NOTICE`, which
is the copy that actually ships to installs.

**2. No branding in published metadata.** The SRD's own legal page states:
*"Please do not include any other attribution to Wizards or its parent or
affiliates other than that provided above. You may, however, include a statement
on your work indicating that it is 'compatible with fifth edition' or '5E
compatible.'"*

So the `name` and `description` fields of both host manifests,
`marketplace.json`, and every skill/agent frontmatter use **5E-compatible**
wording only — never
"Dungeons & Dragons", "D&D", "DnD", "5.5e", or "Wizards of the Coast". The
local checkout's directory name is local to this machine and is never published.

Descriptive nominative reference in repo-internal prose (this file, the README)
is fine and is why those files are not scanned for marks.

**3. Non-SRD content never enters engine data.** SRD 5.2.1 omits parts of the 2024
ruleset — the Artificer class, the Aasimar species, and the Beholder are known
examples. Content outside the SRD is not licensed to us. Every data record
carries a provenance field naming SRD 5.2.1; if a name cannot be traced to the
SRD, it does not ship.

This constrains what **we redistribute**, not what a user may load. A campaign's
own content packs are outside the repo by design and are not subject to our
denylist — their content is theirs. That is why the local hook scopes its non-SRD
name check to `souroldgeezer-fivee-sim/engine/src/fivee_sim/data/`: extending it to
user packs would be both useless and wrong.

The built-in catalog carries structured facts only: source names and IDs,
classifications, numbers, formulas, relationships, atomic table cells, and
structured omission codes. Descriptive, flavor, and rules prose does not ship in
catalog records. Contributor review packets may temporarily contain source prose
under `/tmp`, but the machine-local extraction path and its text are never committed.
The official source pin is
`https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf`, SHA-256
`8974902d109d6e63672d7c490bde9ccf052410503d9cfa768237154fbc5e3d87`.

Before publishing, run the `souroldgeezer-audit:ip-hygiene` skill over the
plugin surface as the release gate. The local hook is a tripwire, not a
substitute for it.

## Local development hooks

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

`scripts/hooks/stop-audit-check.sh` is a companion `Stop` hook, activated the
same way by `.stop-audit-local.conf`. At most once per session, when the
session has touched surfaces listed in `STOP_AUDIT_GLOBS` — derived from the
session transcript, with git-dirty state as the fallback — it blocks the stop
with instructions to run the `souroldgeezer-audit:test-quality-audit` and
`souroldgeezer-audit:devsecops-audit` skills scoped to the touched files.
Silence means allow. Its wiring lives in the developer's
`~/.claude/settings.json` like the ip-hygiene tripwire's; test with
`bash scripts/hooks/test-stop-audit-check.sh`.

## Architecture

**The engine lives under the plugin root**, at `souroldgeezer-fivee-sim/engine/`.
This is not cosmetic: each host packages only the plugin directory, so an engine
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

**There is one adapter, and it is HTTP.** `fivee_sim.web` validates input from
a route table, calls into `service/`, and serialises results as JSON or
problem+json. No rules logic belongs there. An MCP stdio server sat beside it
until the surface was consolidated; deleting it is why the engine now declares
**zero runtime dependencies** — `pyproject.toml`'s `dependencies` list is empty
and should stay that way, because a dependency there is one every install
inherits.

**`fivee_sim.client` is the other half of that decision.** The CLI reaches the
engine over HTTP and by no other route — `tests/test_layering.py` pins that it
imports nothing from this package but `fivee_sim.paths`. So every feature the
command demonstrably has is a feature `/api/v1` demonstrably serves, and "the
REST surface can do everything" stops being a claim a reader has to audit two
call graphs for.

`souroldgeezer-fivee-sim/scripts/fivee.sh` is the launcher and the plugin's whole
entry point: it builds or finds the environment and `exec`s the venv's `fivee`
console script with every argument passed through. Its own diagnostics go to
`stderr`, because stdout belongs to `fivee` and callers capture it.

**Layer boundaries.** `kernel/` holds the primitives — dice, resolution,
conditions, attacks, spells, items — and knows nothing about creatures; callers pass
the handful of values a roll depends on. `model/` owns creatures and is the only
place combat state changes. Spell and item definitions live in `kernel/` rather than
a separate layer because they are resolution primitives like the rest.

**`service/` holds the operation bodies, and the adapter goes through it.** This
includes catalog search and lookup in `catalog.py` alongside `common.py`,
`durable.py`, `encounter_journal.py`, `errors.py`, `maps.py`, `replay.py`, and
`uvtt.py`.
Nothing in it may import HTTP or any transport's error type: a function
takes plain values — a document, a terrain table, a seed — and raises plain
`ValueError`-family errors. That rule outlived the second adapter it was written
for, and it is what keeps the one that remains thin. `web/http_server.py` maps those errors onto problem+json and does nothing
more than that and serialisation; `tests/api.py` is the suite's in-process door
to the same functions and translates nothing at all. An operation body written
into an adapter belongs here instead.

**`service/` also owns durable-write concurrency, because it owns the state.**
Several processes reach these files — every engine server on a host resolves the
same encounter and map roots, and each is a threading HTTP server — so
`durable.py` separates two guarantees. *Integrity* is unconditional: a `flock`
makes read-modify-write atomic across processes and `atomic_write` publishes by
rename, so a reader never sees a prefix. *Ownership* is a precondition the caller
opts into: pass the version you read and a `StaleWriteError` refuses the write
when someone else got there first.

Serialising alone would be a false fix. Two servers holding divergent copies of
one fight would produce an interleaved journal that replays as neither, so the
lock protects the bytes and only the precondition protects the meaning. That is
also why a stale writer is refused rather than merged, and why the refusal is
*not* opt-in in practice: an encounter session tracks its journal head and a map
session the bytes it last saw on disk, so both supply their own precondition and
a caller has to pass `"*"` to deliberately take a file over.

**Five modules sit beside the packages, and that tier is deliberate.**
`catalog.py`, `content.py`, `map_document.py`, `validation.py`, and `coverage.py`
live directly in `src/fivee_sim/`. What belongs there
is a cross-cutting concern that is neither a rules primitive nor creature state:
the immutable catalog model, how content enters the engine and how any file it
reads is validated, the on-disk map document format, and the generated coverage
report. Nothing in `kernel/`,
`model/`, or `analytics/` imports any of them, and that is the property to keep —
a module only the rules layers need is not a root module, it is a `kernel/` or
`model/` one.

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

**Every operation reports its seed.** An operation called without one picks a
seed and returns it, so no result is ever irreproducible.

**Three pages are served, and `/` is the index rather than a tool.** `/editor`
is the map editor, `/viewer` the replay viewer, and `/` a landing page that
links to both and renders the operation list it fetches from `GET
/api/v1/operations`. The editor held `/` while `map_editor_serve` existed to
open a browser on it; it does not any more, and `fivee serve` reports
`editor_url` alongside `url` because a caller that hands `url` to someone
asking for the editor now sends them to the index. The landing page never
spells an operation into its own markup — the list is whatever the route table
answered, which is the same reason `routes.py` is the one declaration.

**The browser assets are checked in two halves, and the split is the point.**
This paragraph used to read "checked as text, not driven … a renderer defect
ships green. That is deliberate." That posture is reversed, and it was reversed
for cause: during the fixtures work it let three real `editor.html` defects
through, one of them a document-corruption path where a malformed `affects`
array threw mid-resize — after `snapshot()`, after earlier planes had been
rewritten — leaving a half-resized document that `btn-download` writes to disk
without the server ever validating it.

So `home.html`, `editor.html`, `viewer.html` and `renderer.js` are now checked twice, and
each half owns one kind of claim. **Text contracts stay in
`tests/test_web_assets.py`**: injection slots, balanced tags, the offline
guarantee — properties of the source, asserted as source. **Behaviour lives in
`scripts/check-editor-behaviour.mjs`**, outside pytest. It reads the shipped
assets — never a copy — runs `renderer.js` in a `node:vm` context, runs each
page's own inline script in that context against a stub DOM, and then drops a
document on the page and clicks its buttons. The assertions read what the fake
canvas was painted and what the Download button would have written, so the
resize corruption above is now a named failing case rather than a defect nobody
can see.

**What neither half covers, stated because it is invisible from a green run.**
There is no browser anywhere in this. No DOM layout, no CSS, no real canvas, no
pixels, no `file://`, no network, no event ordering a real page would impose —
the script drives stubs this repo defines. It can tell you what a page
*decided*; it cannot tell you what a page *looks like*. That is still a
boundary. It is a much narrower one than "nothing ever executes them", and it is
the one to keep in mind when a case here passes and the editor still looks
wrong.

**An error-branch test names the refusal, not just its status.** A status code or
an exception type alone does not identify a branch: when nine
`web/http_server.py` request guards were mutated to check the tests written
for them, four of the mutants still answered 400, so a status-only assertion
would have passed against a server with the check deleted. Every
`assert_problem(...)` therefore carries a problem+json `detail` fragment and
every `pytest.raises(api.ToolError)` a `match=`, and
`tests/test_assertion_discipline.py` parses the suite's own source to fail on a
call that omits either.

## Tooling

```bash
cd souroldgeezer-fivee-sim/engine
uv run ruff check .              # E,F,W,I,UP,B — line length 100
uv run mypy                      # strict, configured in pyproject.toml
uv run pytest

uv run python -m fivee_sim.coverage   # regenerate docs/COVERAGE.md

# From the repo root, against a contributor's verified local extraction.
python3 scripts/srd-catalog-batch.py --source-root /path/to/extracted validate

# From the repo root: the real launcher, end to end. Builds or finds the
# environment, starts the engine, prints every operation the server serves.
bash souroldgeezer-fivee-sim/scripts/fivee.sh help
bash souroldgeezer-fivee-sim/scripts/fivee.sh stop
bash scripts/test-launcher-freshness.sh

# The same launcher, checked rather than eyeballed — see below. Stdlib only.
python3 scripts/check-api-smoke.py

bash scripts/hooks/test-ip-hygiene-check.sh
bash scripts/hooks/test-stop-audit-check.sh

# The browser assets, driven rather than read. Needs node — see below.
node scripts/check-editor-behaviour.mjs
```

**`scripts/check-api-smoke.py` is the repository's automated end-to-end gate**,
and the only thing that checks the shipped surface the way a host uses it.
`fivee.sh help` above is a look, not a check; this is the check. It boots the
**real launcher** — never `python -m fivee_sim.web`, never the dev venv — runs a
complete seeded fight over plain HTTP, runs the identical fight in a second
server and a third time through the `fivee` binary as a subprocess, and requires
all three to agree. Then it holds `GET /api/v1/operations`, `GET
/api/v1/openapi.json` and `fivee help` against the route table's own source.

Three of those claims exist nowhere else. **Reproducibility across processes**:
every other determinism test runs in one interpreter. **That the launcher
works**: nothing in pytest execs it. **That `/api/v1` is complete**: the client
is pinned by `tests/test_layering.py` to import nothing of the engine but
`fivee_sim.paths`, so a fight it can drive end to end is a fight the REST
surface serves — which is why the command run is the load-bearing one rather
than a convenience.

**Standard library only, and no pytest**, because it has to run against an
environment that has built nothing but the plugin's own `--no-dev` runtime. It
does not import the engine either: it reads `web/routes.py` with `ast` rather
than importing it, since an imported copy is not the copy the launcher is
serving. Every server it starts is pointed at a fresh `tempfile` directory —
honouring `TMPDIR`, which is what makes it runnable where `/tmp` is read-only —
and stopped and removed in a `finally`, because a leaked detached server would
make the next run lie.

Its fight constants are **golden values for one seed**, so a change to the rules
or the dice stream turns it red on purpose; reproduce, then recalibrate
deliberately. One thing it deliberately does *not* compare is the whole-file
sha256 of an exported replay: a v2 bundle stamps every event and checkpoint with
the wall clock, so it is not byte-reproducible and never will be. The
timestamp-free integrity hashes — initial state, actions, latest state, map,
content — are compared instead, and those are what the seed determines.

**`node` is a dependency of exactly one check.**
`scripts/check-editor-behaviour.mjs` is the only thing in the repo that wants
it: Node 20 or newer, builtins only, no `package.json` and no `npm install`.
That constraint is the price of admission — a browser toolchain has no business
in a Python repository, and the moment this check needs one it should be
argued for rather than installed. **The Python suite is unaffected**: it neither
imports nor spawns `node`, so an environment without it runs `ruff`, `mypy`,
and `pytest` exactly as before, and only this one command is unavailable. Run it
after touching anything under
`engine/src/fivee_sim/web/static/`.

It takes an optional static-directory argument, and that argument is its own
self-check: copy the static directory somewhere scratch, delete a guard, and
confirm the case that names the guard fails. Every other run reads the shipped
path, because verifying a copy would verify nothing.

**`docs/COVERAGE.md` is generated, never hand-edited.** Adding or advancing a
catalog record, table, or executable record means regenerating it;
`tests/test_coverage.py` fails otherwise. Keep it compact: source inventory,
category, progress, simulation-support, and executable totals only. Detailed
identity and table lookup belongs to the bounded catalog tools.

It describes the **bundled** slice only. What a session actually has loaded is
`content.status`'s answer, and the skill says so — a generated document cannot
know about a pack it has never seen.

`uv`'s cache is redirected to `souroldgeezer-fivee-sim/engine/.cache/uv` because the default
`~/.cache/uv` is read-only in the sandboxed development environment.

### The virtual environment

`uv` builds it; nothing else should. `uv sync` in the engine directory creates
`.venv` with the dev group included, which is what `uv run pytest` and `uv run
mypy` use.

At runtime the launcher **execs the venv's own console script** rather than going
through `uv run`. That keeps uv out of the spawn path — one process instead of
two, and uv is needed only when the environment has to be built.
`bash souroldgeezer-fivee-sim/scripts/fivee.sh help` succeeds with `uv` removed
from `PATH` entirely once the environment exists, which is the test of it.

The launcher syncs with `--no-dev`, so a venv it built has no test tooling. That
only happens on a cold start; `uv run pytest` re-syncs the dev group
automatically, so the two uses do not fight.

**A venv is not portable.** Its console scripts hard-code an absolute interpreter
path in their shebang, so moving or renaming the plugin directory leaves a venv
that exists and looks executable but cannot start — this bit the rename to
`souroldgeezer-fivee-sim`. The launcher now detects it by checking the shebang
still resolves, and rebuilds. If you relocate the repo and something behaves
oddly, `rm -rf` the venv rather than debugging it.

**A host-managed runtime must outlive the plugin root without outliving its own
engine build.** Claude Code supplies `${CLAUDE_PLUGIN_DATA}` directly. Codex
supplies neither that nor `${PLUGIN_DATA}`, so the launcher derives its
plugin-data location from `${CODEX_HOME}` alone — never from `${HOME}/.codex`, a
guess that would put a plain checkout on the host-managed path. The host marker
that used to answer this arrived through the deleted `.mcp.json`. An explicit
host variable still wins. The installed plugin root can disappear when another
session refreshes the host cache, so host-managed environments live at
`$plugin_data/venvs/<engine-build-id>` and are installed with `--no-editable`.
The build identity covers `pyproject.toml`, `uv.lock`, and every shipped source
file. A new build is created beside old ones rather than refreshing a venv that a
live server still uses. Old runtimes are deliberately retained: the launcher
cannot know which other host process still owns one.

Before exec the launcher also changes into the durable plugin-data directory.
That is load-bearing: maps and encounter journals may resolve a default from
`Path.cwd()` when an operation runs, and a process left inside a retired plugin
cache would otherwise turn its next journal append into raw `ENOENT`. Explicit project-root,
content, map, and encounter environment variables continue to win.

Every cold build of the same identity is guarded by an atomic sibling-directory
lock. Only the lock owner may mutate that venv; a waiter rechecks freshness after
acquiring it, dead owners are reclaimed, and handled termination releases it.
Different builds use different environments and locks, so they cannot rewrite one
another. The warm path does not inspect the lock at all, preserving the one-process
startup this launcher is designed around. Explicit/development environments keep
the path-and-input build stamp and fail closed if a stale runtime cannot rebuild.

`bash scripts/test-launcher-freshness.sh` pins those decisions against a fake `uv`.
Keep the warm-path cases green in particular — an unconditional `uv sync` would fix
freshness by putting uv back in the spawn path, which is the thing this launcher is
built to avoid.

## Conventions

planning-policy: default — before new feature or build work, brainstorm the
approach in plan mode and get it approved (`ExitPlanMode`) before implementing.
Scope: new feature, build, or creative work. Exceptions (logged): trivial edits,
hotfixes, spikes/throwaway, work a domain skill owns end to end. Opt out per
task by saying "skip planning" (logged). Enforcement model.

software-design: when work shapes code or module structure — a new module or
layer, the kernel/model/content/service/web seams, dependency direction, coupling,
ownership of state, principle/pattern tradeoffs, or non-functional targets —
load the `souroldgeezer-design:software-design` skill (it pulls its python
extension) and do the design or review under it before writing the code. Not
needed for mechanical edits, SRD data pack records, or doc-only changes. It
composes with the lines around it: plan first, design under the skill, then
test-first implementation.

tdd-policy: test-first — a failing test precedes implementation;
RED→GREEN→REFACTOR; shipped behavior stays covered by a test that fails on
regression. Scope `souroldgeezer-fivee-sim/engine/src/**` and `scripts/**`. Exceptions
(logged): spikes/throwaway code; generated `docs/COVERAGE.md`; SRD data pack
records under `engine/src/fivee_sim/data/`, which are instead schema-validated
on load and pinned by coverage regeneration. Enforcement model.

release-policy: calver `YYYY.0M.build` (`2026.07.1`); the version source is the
`version` field of `souroldgeezer-fivee-sim/.claude-plugin/plugin.json`, mirrored by the
strict-semver `souroldgeezer-fivee-sim/.codex-plugin/plugin.json`, the plugin
table in [README.md](README.md), `engine/pyproject.toml`, and
`fivee_sim.__version__` (PEP 440 and semver strip the month's zero-padding in
their mirrors; `engine/tests/test_version.py` pins all five to one number). Bumping is
automatic and `main`-only: when integration lands plugin-surface changes
(`souroldgeezer-fivee-sim/**`) on `main`, bump the source and every mirror directly on
`main` after the repo's documented verification; a new month restarts the build
at `.1`, and a commit that only aligns or bumps version surfaces does not
re-bump. Worktrees and feature branches never touch the version — parallel
agents bumping before integration is how versions collide. Publication only
through the `souroldgeezer-audit:ip-hygiene` release gate described under
"Licence boundary" above. No git tags or provider releases exist yet — creating
the first is an explicit decision, not routine release work.

Otherwise mirrors the sibling `souroldgeezer` marketplace at `../skills`:
both host `plugin.json` files carrying `name`, `version`, `description`, `author`,
and `license`; the Codex manifest uses the same numeric release without CalVer's
month padding because its validator requires strict semver;
`AGENTS.md` as a pointer to this file rather than a second copy of it.
