# Repository guidance

A Claude Code and Codex marketplace plugin providing a 5E-compatible simulation
engine: a pure Python rules kernel served over a local HTTP API, the `fivee`
command that drives it, plus the skills that teach the active assistant to use
them. See [README.md](README.md) for the project overview.

## Environment hazards — read before any `git add`

This environment can bind-mount selected workspace paths as **character
devices** (`/dev/null`, major/minor `1,3`) instead of regular files. Which
paths are mounted is session- and worktree-dependent: a path that is a regular
file or absent in one checkout may be a device in another.

Before writing or staging any configuration path, inspect the **exact target**
rather than inferring its type from the directory or a sibling:

```bash
stat --format='%F %t:%T %n' -- <exact-path>
```

Paths observed as mounts include, in the repo root, `.bashrc`, `.bash_profile`,
`.profile`, `.zshrc`, `.zprofile`, `.gitconfig`, `.gitmodules`, `.mcp.json`,
`.ripgreprc`, `.idea`, and `.vscode`; and under `.claude/`, `settings.json`,
`settings.local.json`, `hooks/`, `skills/`, `agents/`, and `commands/`. The
root paths are listed in `.gitignore`.

When an exact path is mounted as a character device, it is not usable as a
regular project file or committable and **cannot host project configuration
while mounted**. Do not try to write it; the content may silently go to
`/dev/null`. Local development hooks live in `scripts/hooks/` and are wired
from the user's own `~/.claude/settings.json` — see "Local development hooks"
below.

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

A fresh worktree needs no setup to **run** the engine — `python3
souroldgeezer-fivee-sim/scripts/fivee.py help` works in one immediately, with no
venv and no network, because the launcher builds nothing.

Running the **tests** is what still needs an environment. A fresh worktree has no
`.venv`, and uv's cache is project-relative (`cache-dir = ".cache/uv"`), so a new
one starts empty and `uv sync` would want network it does not have. Point it at
the primary checkout's cache instead, and keep passing the variable to `uv run`
in that worktree:

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

**A worktree's checkout is usually clean, but do not rely on it.** This section
used to say the mounts were confined to `.git/worktrees/<name>/`. They are not:
a worktree removed on 2026-08-06 had 11 character devices *inside its checkout*
— `.mcp.json` and most of `.claude/` — so `git worktree remove --force` failed
on the **checkout directory** rather than only on the registration, and left the
whole tree behind.

Same cause and same remedy: a bind-mounted file cannot be unlinked, so the
directory holding it cannot be removed. Expect a leftover checkout at
`.worktrees/<name>` after some closeouts, leave it, and do not try to unmount
it or `rm -rf` it into a half-deleted husk. It is invisible to `git worktree
list` and `git branch`, so it misleads no tooling — it is only disk.

The mounts come and go with other sessions, which is why the old flat claim was
true when written. **Verify closeout by `git worktree list` and `git branch`,
never by whether the directory is gone.**

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

**1. The attribution and source-supplied notice ship verbatim.** [NOTICE](NOTICE)
opens with the exact required attribution, on a single line so it can be matched
byte-for-byte. It also retains the source's exact CC-BY-4.0 Section 5 disclaimer
notice on a single line. Never reword or re-wrap either sentence.

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
knobs (surface globs, mark denylist, non-SRD denylist, expected attribution and
source-supplied disclaimer strings).

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

`souroldgeezer-fivee-sim/scripts/fivee.py` is the launcher and the plugin's whole
entry point: it puts the engine source on `sys.path` and calls the client, with
every argument passed through. Its own diagnostics go to `stderr`, because stdout
belongs to `fivee` and callers capture it.

**It builds nothing, and that follows from the empty `dependencies` list above.**
A virtual environment here would install exactly one thing — this package, whose
source is already on disk beside the launcher — so `python -m` is the whole
mechanism, and `uv` is not involved in running anything. It exports `PYTHONPATH`
as well as setting `sys.path`, because the client spawns its server as a child
interpreter that inherits the environment and not this process's path.

**Layer boundaries.** `kernel/` holds the primitives — dice, resolution,
conditions, attacks, spells, items — and knows nothing about creatures; callers pass
the handful of values a roll depends on. `model/` owns creatures and is the only
place combat state changes. Spell and item definitions live in `kernel/` rather than
a separate layer because they are resolution primitives like the rest.

**`service/` holds the operation bodies, and the adapter goes through it.** This
includes catalog search and lookup in `catalog.py` alongside `common.py`,
`adventures.py`, `blobs.py`, `durable.py`, `encounter_journal.py`, `errors.py`, `maps.py`,
`replay.py`, `scenes.py`, `uvtt.py`, and `views.py`.
Nothing in it may import HTTP or any transport's error type: a function
takes plain values — a document, a terrain table, a seed — and raises plain
`ValueError`-family errors. That rule outlived the second adapter it was written
for, and it is what keeps the one that remains thin. `web/http_server.py` maps those errors onto problem+json and does nothing
more than that and serialisation; `tests/api.py` is the suite's in-process door
to the same functions and translates nothing at all. An operation body written
into an adapter belongs here instead.

**A scene is a saved `encounter.create` body, and `scenes.py` validates the
envelope only.** That bound is the point: whether a combatant spec is legal is
`encounter.create`'s to say, and a second copy of that rule in `scenes.py` would
drift the first time a spec field is added. So a scene can be saved that will not
start — an editor buffer is a draft — and the refusal arrives, in full, at Play.
There is deliberately **no `scene.play`**: Play posts the stored body to
`encounter.create`, so one code path starts every fight. The one key that is not
posted is `name`, a label rather than a fight, and the smoke gate asserts the
seam is exactly that one key wide in both directions.

**The player's brief is an allowlist, and it must stay one.** It lives on
`Encounter` — `brief`, `brief_of`, `brief_events`, `unseen_by` in
`model/encounter.py` — rather than in a service module, and the reason is one
line of it: a creature behind **total cover** is omitted, and total cover is a
relationship between two creatures and a map that no snapshot carries. A
projection written over `encounter.state`'s output cannot compute it, which is
what settled a period when two of these existed side by side. `service/` calls
into it and translates its refusal; that is all `service/` does with it.

It projects the fight for one seat, to the brief `agents/game-master.md` already
specifies — positions, distances, own-side conditions, whose turn it is, health
as a plain-language band; never exact enemy hit points or AC, never a DC before a
roll, never a creature that has not arrived or cannot be seen. It names what goes
**in**. A denylist would leak every field added afterwards, and
`Encounter._creature_state` is an open, growing set — so every key of a
creature's entry, the map block, each fixture summary and each **event** belongs
to exactly one of a `*_VISIBLE_KEYS` / `*_WITHHELD_KEYS` pair. Two sets rather
than one filter, because an allowlist alone answers "is this shown?" and not "has
anybody looked at this?", and `tests/test_player_brief.py` *derives* both halves
from the model — from real payloads for the creature, map and fixture sets, and
for events by reading every `_emit` call site out of `model/encounter.py` with
`ast`, because a sampled set is only whatever the fixture happened to make
happen. A new field lands in no bucket and fails until someone decides.

`EVENT_NEVER_KEYS` is the one refinement: a named subset *inside*
`EVENT_WITHHELD_KEYS` for the handful no seat is served at all — `dc`, `check`,
the map's wiring — so that the pair stays total and the sharper rule is still
written down.

Four leaks have been caught this way and none was in the creature fields.
Fixture ability-check **DCs** rode in on an unclassified map passthrough. `turn`
**named the creature acting** even when the brief had just omitted them. Every
**write** operation answered the acting seat with the whole unredacted snapshot
until `encounter.create/.act/.advance/.resume` learned the same `as=` seat
parameter the brief takes. And then those four narrowed their `state` and handed
the same response's **events** over whole, where `damage` carries the target's
exact `hp` and `max_hp` — the brief said "hurt" and the event beside it said
6594/7700. An event's `detail` is the one field omitted outright rather than
classified: it is rendered prose, and prose cannot be allowlisted. Absent `as=`,
all four answer exactly as they always did.

**A chair-carrying write answers with the brief's own shape**, not a redacted
snapshot, and that is the whole of "one projection". A second shape would be a
second classification to keep in step and a weaker filter than this one; a caller
who wants the flat `state` omits `as=` and gets it byte for byte. The refusal is
one sentence with one owner and one status — `404`, `no combatant named 'X' in
this encounter` — from the read and from all four writes, and it deliberately
**does not list the cast**: a refusal that answers "who is in this fight?" to
anyone who guesses a wrong name discloses the ambusher the projection works
hardest to hide.

**A write composes two projections, seat then view, in that order and in one
place.** `service/views.py` narrows a write's answer to `delta` (what changed
since this seat's last payload), `live` (every combatant, sheets replaced by a
digest), or `full` — and it runs strictly after `as=`'s brief projection, inside
`encounters.py`'s `_answered`, so there is no per-operation order to keep in
step. Reversing it would be the more obvious shape — diff the whole fight, then
redact the diff — and it is wrong: a delta *names* every creature it mentions,
so a delta computed before the brief could name a creature this seat cannot
see, and that a hidden creature changed is itself the disclosure, independent
of whatever fields rode along with it. Composing the other way makes it
structural rather than a convention to remember, because the diff is then
computed generically over two briefs with no knowledge of seats or cover — and
because the two payloads' rosters actually disagree when a hidden creature
enters or leaves, get the order backwards and the diff fails loudly instead of
leaking a name.

`create` and `resume` default `view` to `full` and `act`/`advance` default it
to `delta`, because a delta needs a baseline and `create`/`resume` are what
establish one. Two properties make that default safe rather than merely
convenient. A server holding no baseline for a seat — a second server, one
restarted, or a session just recovered from its journal — answers `full`
regardless of what was asked, and says so in the response's own `view` field,
so a caller can never be served a delta silently computed against the wrong
history. And a cached, idempotent retry is always answered whole: a retry
means the caller never received the first answer, so a delta against it would
assume a baseline that was never actually held.

`state_sha256` is a real integrity check for a program — apply the delta,
hash the result, compare — and not one the skills can perform: an agent cannot
compute a SHA-256 digest. So for the skills the rule stays "the engine is the
authority, re-read"; the digest exists for `static/play.js`, the smoke gate's
`apply_delta`, and any client that actually is a program.

**It is a projection, not an access control, and nothing may cite it as one.**
`as=` is caller-asserted; the engine has one per-launch token and no per-seat
credential, so a client that can ask for a seat's brief can equally ask for
`encounter.state`. **`actor` on `encounter.act` is the same kind of field** and
inherits the same disclaimer: in an interlude it names who takes the beat, and
nothing checks that the caller is entitled to move that creature. In a fight
initiative incidentally narrowed this — only the current combatant could be
made to act — and an interlude has no initiative to narrow it, so any caller
holding the launch token may act as anybody. That is the same trust boundary as
before, not a smaller one, and per-seat credentials are still what closing it
would take. What it buys is an honest payload — a cooperating client is not
holding secrets it must remember not to draw — and that is worth having on its own
terms. It is not a boundary against a client that does not want to cooperate, and
per-seat credentials are what closing that would take.

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

**A journal records inputs and outcomes, never derived state.** Recovery
recomputes a fight by replaying its recorded actions through the same stepper
that first ran them, so a stored snapshot beside that record is a second copy
of something already derivable — and a second copy can drift while both halves
stay internally consistent, which is what makes it dangerous rather than
merely wasteful. `initial_state` used to ride in the creation record and was
read by nothing; a full `state` block used to ride in every result record at
roughly 700 bytes per combatant. `state_sha256` is what a snapshot was
actually good for: a recovered fight can be held against the one that was
recorded, which is what makes dev reload's documented sharp edge above
visible instead of silent.

Two clauses keep a result whole regardless, each for its own reason. An
operation `recover_session` does not replay has no other record of what it
rolled — a `roll` or a `check` is resolved once and never re-derived, so
dropping its result would be a deletion, not a saving. And a caller who
supplied a `request_id` bought idempotency, which `cached_request` has
nothing else to answer a retry with once the session has come back off disk.
`REPLAYED_OPERATIONS` is the one declaration both the writer and the reader
read, so an operation that joins or leaves it is judged correctly without a
second list to keep in step.

`journal_version` is a clean break, not a migration: there is no reader for
an older format, a v1 journal is refused by name at recovery, and
`encounter.list` still lists it anyway, because a hash-valid file that this
build cannot replay is not a corrupt one.

**Verification is priced where it is needed, and a look is not a read.**
`read` parses and hash-verifies every line, which is the right price to pay
before *trusting* a journal and the wrong one for the two callers that only ever
wanted its ends — `encounter.list` reports an id, two timestamps, a count and
whether the fight is over, and `creation_request` matches one field off the
creation record. Both used to buy every journal on the disk whole to get them.
`head_and_tail` reads the bytes once, counts newlines, and parses exactly the
first and last complete records; full verification stays in `recover_session`,
which is where a caller is about to act on what the journal says.

Two properties make that sound rather than merely cheap. A journal broken in the
**middle** now lists as `active` rather than `corrupt` — nothing is trusted on
the strength of a summary, and the refusal still arrives, in full, at recovery.
And **finalization is terminal**: `act`, `advance` and `audited_primitive` all
refuse a finished fight *before* `attempt_started` writes anything, so
`finalized` is the last word a journal can hold and a listing can read a status
off one line. That refusal rolled no dice and changed no state, which is what
makes dropping its record a saving rather than a deletion — the same test
`REPLAYED_OPERATIONS` applies, and the same reason `cached_request` short-circuits
above it. A refusal the *rules* make is still audited in full.

**Each fight owns a directory, and the id is the directory's name.**
`<encounters_root>/enc-7/` holds `journal.jsonl`, the lock guarding it, a
quarantined corrupt tail if there ever was one, and the frozen `replay.json`
`encounter.finalize` writes — every artifact addressed by encounter id in one
place, each named for what it is rather than for which fight it belongs to. The
**empty file is still the claim**, not the directory: `claim` creates
`journal.jsonl` with `O_EXCL`, which is what makes handing out an id atomic
across processes, and a directory that exists proves nothing about who made it.

**Adventures moved to a root of their own in the same step**, and that is the
point rather than tidiness. One root used to hold both kinds, kept apart by an
`enc-`/`adv-` id grammar in one module and an `enc-*.jsonl` glob in another —
two facts in two places obliged to stay true together, where a saved adventure
named `enc-1` would have collided with a fight. `FIVEE_SIM_ADVENTURES` /
`.fivee-sim/adventures` is the same guarantee with nothing to keep in step. The
grammar survives the move doing narrower work: `adv-` now refuses `..`, a
separator and an absolute path, because `adventure.replay` writes wherever the
caller names it to.

**Both moves are an accepted break, and nothing migrates or refuses.** A
pre-move `<encounters_root>/enc-7.jsonl` matches neither `enc-*/journal.jsonl`
nor a `prune` sweep that requires a directory, and an adventure left in the
encounters root matches no `adv-*.json` under the new one — so `encounter.list`
never mentions the fight, `encounter.resume` refuses it by name as though it had
never existed, and `adventure.list` comes back empty. That is not the treatment
`journal_version` gets, and the difference is worth stating rather than
rediscovering: a version refusal has the file *in hand* — a reader opened it and
could not replay it, so there is somewhere to put the sentence. A moved layout
has no reader at all, and giving it one means scanning a directory the engine
otherwise has no reason to open, on every launch, for ever, against a hazard
that stops existing the first time a checkout is current. The fights are working
state that does not outlive the release ending them. The adventures are the real
cost, and they are the reason this is written down: they are saved documents,
they are simply *there* in the old root, and moving one by hand is the whole of
the recovery.

**`encounter.prune` gives an id back, and it is a dry run unless asked
otherwise.** `create` claims its id before the durable work, so a failed blob
write or a dead process spends a name and leaves an empty journal behind, and
until now nothing ever reclaimed one. A dry run reads and writes nothing at all
— not even the lock file `durable.file_lock` would create on the way to taking
one — and `apply` re-checks emptiness under that lock before removing the
journal, then the lock and the directory outside it. What the lock cannot
exclude is a creation *currently* between its claim and its first append: that
id is legitimately empty for the width of that window, and reaping it there
would hand the same name out twice. So this is an operator's decision on a quiet
engine rather than a reaper on a timer, and the default answer is a list to look
at.

A refusal part-way through an `apply` **carries the ids it already reclaimed**,
and carries them in the sentence rather than only in an attribute. Those
journals are unlinked and nothing else records that they were, so raising past
them would tell an operator nothing was pruned while several had been — and an
attribute does not cross the adapter, which renders a `ValueError` into
problem+json from its message and reads nothing else. The attribute stays for a
caller that is a program and wants the ids as ids.

**A blob is the fourth storage kind, and it is defined entirely by its name.**
`service/blobs.py` writes a payload to a file named for the SHA-256 of its own
canonical bytes, and everything else follows without machinery: publishing is a
rename because the winner of a race writes what the loser was about to,
freshness needs no stamp because a file that exists already holds the only
content that name can mean, deduplication needs no index because two fights
capturing identical content compute identical names, and integrity needs no
chain and no version precondition because a blob cannot legally change — so
`get` reads one back and hashes it, and that is the whole check. This is the
`src/<source-id>` idiom the launcher already runs on, pointed at the payloads a
journal used to carry inline.

That is the same argument the paragraph above makes about derived state, made
about *duplicated* state instead, and it was worth making because the numbers
were not close. A creation record was 22,223 bytes for a six-combatant fight, of
which 14,589 was the captured content snapshot — and that snapshot was
byte-identical in every journal on the machine. It names one now, at 7,708 bytes
and one shared file. What is left is almost entirely `combatants`, which is an
*input* the replay needs, so it stays.

**Blobs are never deleted, and that is a decision rather than an omission.** A
journal names one for as long as the journal exists, and no process can know
which other one is mid-recovery on a fight it has not been told about — the same
reasoning that retains old launcher source copies. `encounter.prune` reclaims
ids and deliberately does not answer this: a journal names its blobs on the
creation record, so an id with no creation record named none, and an empty
journal holds nothing that could license removing one. Retiring a blob needs a
survey of the journals that *do* name them — a different operation against a
different hazard.

The cost is paid where it is visible: the blobs are a **sibling** root of the
journals, not a child, because the point of one is to be shared by every journal
that names it — so the two move independently, and a journal carried somewhere
its blobs were not names payloads that are not there. A named payload can be
missing where an inline one could not be. Recovery says which encounter and what
is missing rather than raising past it. And the reference goes in the journal
only: a bundle is an **export** that leaves the machine, where nothing resolves a
bare digest, so every bundle writer reads the in-memory `Session` — which
recovery repopulates from the blobs — and ships map and content by value exactly
as before.

**A replay bundle's `format_version` is not held to that rule, on purpose.** A
journal is internal state, so breaking it cleanly costs nothing outside the
process. A bundle is an export that leaves the machine, so the engine writes
the latest version and reads every version it has ever written — refusing an
older one would make every replay already on a user's disk unplayable at the
version that added the refusal. `service/replay.py`'s `READABLE_FORMAT_VERSIONS`
is the declaration `validate_replay` reads instead of a literal, and it always
contains `LATEST_FORMAT_VERSION`; a phase that bumps the writer and forgets the
reader is what would falsify that invariant, and a test pins it.

**Every place that names a bundle version is pinned to the declaration that owns
it**, because *readable*, *writable*, *enveloped* and *chained* are four
different sets and two of the namers cannot import the owner. `map_ops.py`'s
`WRITABLE_FORMAT_VERSIONS` is read off `_BUNDLE_WRITERS`, the export dispatch
itself, so a version with no writer function cannot be claimed; `viewer.html`
and the route table's `format_version` default each keep a literal copy — the
page grades a dropped file offline with no engine to ask, and `routes.py` may
not reach into `service/` — with a test holding each to its owner. Adding a
version means touching all of them or going red. The invariant across them runs
one way only, `writable ⊆ readable`: a build that reads three versions and
writes one is the normal end state of this policy, and pointing the writer at
the reader's set is how a build starts advertising a version it can only parse.

**The two sets inside `readable` are the ones a phase forgets, and forgetting
one has already shipped a hole.** `validate_replay` grades a bare v1 bundle
against the version-agnostic prefix and stops there, and the gate that stopped
it was written as `!= 2` when 2 was the only enveloped version. Adding v3
therefore returned at that line: a bundle with a missing `state_delta`, a
tampered chain, or a `latest_state` nothing in the chain reaches all validated
clean, because every envelope check below was silently skipped. So the gate
reads `ENVELOPED_FORMAT_VERSIONS`, which is `READABLE_FORMAT_VERSIONS` minus the
bare `FORMAT_VERSION` and needs no edit for v4; `CHAINED_FORMAT_VERSIONS` is the
matching declaration for "checkpoints are a keyframe and a chain of deltas", a
**set rather than a floor** because "3 and everything after" is a claim about
versions nobody has designed. A version this build cannot read at all still
falls out at that early return deliberately: grading an unknown shape against
the newest envelope buries the one diagnostic that matters under twenty that do
not.

**There are now three independent appliers of a state delta, and that is the
design rather than duplication.** `tests/test_state_views.py`,
`scripts/check-api-smoke.py` and `viewer.html` each write one from the
*published prose* — never ported from `model.apply_state_delta`, which is graded
against them — because a round trip through one function proves only that it is
its own inverse. The viewer's is the one that had to exist anyway: it reads
bundles it did not write, on a machine that may have no engine at all, so a v3
checkpoint chain is something the page reconstructs offline or not at all. Its
`STATE_ROSTERS` / `STATE_ENTRIES` are the fourth and fifth literal copies the
page keeps, pinned to `model/encounter.py`'s by the same kind of test that
pins its version arrays.

**Seven concern modules sit beside the packages, and that tier is deliberate.**
`catalog.py`, `content.py`, `map_document.py`, `map_types.py`, `validation.py`,
`coverage.py`, and `rulings.py`
live directly in `src/fivee_sim/`. What belongs there
is a cross-cutting concern that is neither a rules primitive nor creature state:
the immutable catalog model, how content enters the engine and how any file it
reads is validated, the on-disk map document's parser and the map's own types,
the generated coverage report, and the register of adjudications below. Nothing
in `kernel/`, `model/`, or `analytics/` imports any of them **but one**, and that
is the property to keep — a module only the rules layers need is not a root
module, it is a `kernel/` or `model/` one. `rulings.py` is the sharpest case: the
rules layers are exactly what it describes, and they reference it through a
`# ruling:` **comment** so the dependency never becomes an import.

One private helper, `_generated_document.py`, sits beside them only to own the
shared path, write, and diagnostic mechanics for the two generated reports. It
is plumbing, not an eighth concern or a public API.

`map_types.py` is the one exception, and it is an exception the other way —
`model/` imports it directly, not by comment — because a fight has to hold a
`MapDocument`, and `MapDocument` has to be a type before it can be anything
else. What `model/` may not import is `map_document.py`, the parser built on
top of those types: `Reader`, the diagnostic passes, `validate_document`. A
fight that could reach the parser could re-decide what a legal map is, which is
the second-owner defect the rest of this section keeps refusing everywhere
else. So the map's types and its parser are two modules rather than one
precisely so the allowlist can say "the shape, never the file" — `model/` names
`MapDocument` without dragging the machinery that reads one in behind it, and
`tests/test_layering.py` pins the boundary at exactly that width, with a
vacuity guard so it cannot pass by nobody using the carve-out.

**A fight holds a `MapDocument`, and one function builds every one of them.**
`Encounter` takes the document itself rather than a runtime projection of it;
the encounter's own mutable fact about its battlefield is `MapState`, which
records only which fixtures currently stand open and layers over a document
that can be frozen and shared across any number of fights. Three producers reach
it and every one of them ends at a document — a generated map through
`document_from`, a saved file through `parse_document`, and an inline spec on
`encounter.create` through `service/specs.py`'s `document_from_spec` — so there
is one shape for a fight to hold and one writer, `as_payload`, that puts it back
on the wire. Before this, a fourth producer built a runtime map directly and an
unofficial inverse re-synthesised a document out of it, which could not express
everything the format could: that is why a spec-created door survived to the
day's play but not to its own journal recovery. The map's
rules follow the same one-function discipline from the other direction: each
is stated once in `map_types.py` as a predicate yielding a finding, and rendered
twice — `map_document.py` accumulates every finding a file fails as one
diagnostic, because an author fixing a document wants the whole list, while
`Encounter._adopt_map` raises the first as a fail-fast `EncounterError`, because
a fight either starts or it does not.

**Where the SRD does not decide, the decision is declared rather than
described.** `rulings.py` holds one entry per adjudication — the question, what
the engine does, why, its SRD citation, and a **`revisit` trigger** naming what
would make it wrong. That last field is the point: the Loading gate is correct
today *and* records that a Bonus Action attack or a ranged reaction ends its
equivalence, which is a sentence the person adding one will never find in
`_do_attack`'s docstring.

Four kinds, and they decide what an entry owes. `srd_silent` (no printed rule)
and `approximation` (printed rule, coarser model) govern code and must point at
it; `schema_ceiling` and `out_of_scope` describe something the engine cannot
express, so they have no site and that absence *is* the ruling. `superseded`
entries stay after a release closes them, because earlier reviews cite them.
Only `srd_silent` entries carry a real `concurrence` verdict — an approximation
has no rules question, so grading it against outside readings would invent a
controversy.

**It is pinned in both directions, and that is what stops it becoming
`unmodelled_facts`.** Every site is resolved against the source tree with `ast`,
so a rename turns the register red rather than stale; every governed entry has a
`# ruling: <code>` marker at its site and every marker has an entry.
`tests/test_rulings.py` derives both halves. The ledger it learned from measures
*attention* rather than omission precisely because nobody was obliged to write
an entry.

`docs/RULINGS.md` is generated by `python -m fivee_sim.rulings` and never
hand-edited, like `COVERAGE.md`. `GET /api/v1/rulings` serves the same register,
so `fivee rules.rulings --code <code>` answers mid-fight. The survey behind the
`concurrence` verdicts is `docs/RULINGS-RESEARCH.md` at the **repo root**, which
does not ship: it names third-party sources, and the register carries only our
own classification. A test asserts that split rather than trusting it.

**Content is data, and the bundled slice is not privileged.** `content.py` loads
every pack — including `data/srd/*.json` — through one parser and one validator, and
returns an immutable `ContentRegistry`. There is one exception, and it is forced:
the SRD **condition table lives in `kernel/conditions.py`**, because it is the
default every kernel function falls back to and the kernel may not do I/O.
`content.py` renders that table as a synthetic pack so it still goes through the
same validation.

**A new `Creature` field crosses nine checkpoints, and a survey of its *type*
finds two of them.** Five consecutive steps rediscovered this list one miss at a
time, so it is written down rather than relearned. `grep save_bonuses` walks the
whole of it and is the fastest way to see a worked example:

| # | Checkpoint | Applies |
|---|---|---|
| 1 | `Creature` dataclass and `from_record` (`model/creature.py`) | always |
| 2 | `_CREATURE_KEYS` and `_parse_creature` (`content.py`) | only if a **stat block** can print it |
| 3 | `DESCRIBED_SPEC_KEYS` and `creature_from_spec` (`service/specs.py`) | always |
| 4 | `normalized_combatant_payload` (`service/replay.py`) | always |
| 5 | `docs/CONTENT-PACKS.md` | always — enforced by nothing |
| 6 | `ENEMY_VISIBLE_KEYS` / `ENEMY_WITHHELD_KEYS` | only if `_creature_state` emits it as a **top-level** key |
| 7 | `CARRIED_STATE_KEYS` (`service/adventures.py`) | only if the **fight changes it** |
| 8 | a `validation.py` reader primitive | only if none of the existing ones fits |
| 9 | `SHEET_KEYS` / `LIVE_KEYS` (`model/encounter.py`) | only if `_creature_state` emits it as a **top-level** key |

Two of those conditions are the ones that get missed. **Checkpoint 2 is not
automatic**: `hp`, `position` and `temp_hp` are per-instance and deliberately
absent from `_CREATURE_KEYS`, because a stat block never prints them. And
**checkpoints 6 and 7 travel together** — a field the fight changes must be
classified in the brief *and* listed in `CARRIED_STATE_KEYS`, or it silently
fails to survive an adventure chapter boundary. A field nested inside the
existing `speeds`/`senses` dicts needs neither, because the brief test
classifies top-level keys only.

**Checkpoints 6 and 9 have the same condition and ask different questions**, and
conflating them is the mistake to avoid: 6 is the brief's *may this seat see
it*, 9 is *can the fight move it*. A key belongs to one bucket of each pair, and
`tests/test_player_brief.py` and `tests/test_state_split.py` derive their halves
from the model separately rather than either importing the other's sets. Nine is
also the one row where the wrong answer is cheap: a field nobody classified
falls to the live half, so it is re-sent rather than dropped, and a
`sheet_sha256` taken over the sheet as serialised means a declared-static field
that ever moves moves the digest with it. **The split is bandwidth and never a
claim about the rules.**

Definition of done: `uv run pytest` green with the field exercised end to end,
and every row above either edited or consciously ruled out. Checkpoints 3 and 4
are already pinned to each other by `tests/test_replay_export.py`; the unguarded
seams are `content.py` ↔ `model/creature.py` ↔ `service/specs.py`, and a field
that reaches only some of them is accepted, validated, and then dropped — which
is the `hit_dice` defect, not a new one.

**A field nothing reads is a defect unless it is declared.** `hit_dice`,
`passive_perception` and `tremorsense` are all carried and unconsumed, and each
says so in its field comment, in `docs/CONTENT-PACKS.md`, and in `rulings.py`.
Declaring keeps a faithful transcription from being re-derived later; silence is
what makes `COVERAGE.md` claim a simulation that does not exist. A declared
transcription-only field therefore does **not** close a record's
`unmodelled_facts` code.

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
is the map editor **and the play surface**, `/viewer` the replay viewer, and `/`
a landing page that links to both and renders the operation list it fetches from
`GET /api/v1/operations`.

**`/editor` has two modes, and that is deliberate rather than crowded.** Edit
authors the scene — terrain, content packs, and a roster placed on squares; Play
runs it, live, on the same canvas. One window, no export step between them. The
live loop is **not** in `editor.html`: it is `static/play.js`, namespace
`FiveePlay`, loaded lazily on first entry to Play and given a context rather than
reaching for the page — the same shared-asset seam `renderer.js` established.
**Play never writes the document buffer**, and that is enforced twice, because
once was not enough: `check-editor-behaviour.mjs` asserts the scene is
byte-identical across Play→Stop, *and* `editor.html`'s canvas handler refuses in
Play mode. The second guard is load-bearing — the editor's tools listen on the
same canvas and registered first, so no `stopPropagation` in the driver can stop
a selected Brush from painting the map on a click meant to move a creature. The editor held `/` while `map_editor_serve` existed to
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

So `home.html`, `editor.html`, `viewer.html`, `renderer.js` and `play.js` are now
checked twice, and
each half owns one kind of claim. **Text contracts stay in
`tests/test_web_assets.py`**: injection slots, balanced tags, the offline
guarantee — properties of the source, asserted as source. **Behaviour lives in
`scripts/check-editor-behaviour.mjs`**, outside pytest. It reads the shipped
assets — never a copy — runs `renderer.js` and `play.js` in a `node:vm` context,
runs each
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
# The launcher lives outside this project directory, so name it: ruff lints the
# paths it is given, and `.` alone would silently skip it. mypy reaches it from
# `files` in pyproject.toml, but only when run from here.
uv run ruff check . ../scripts/fivee.py   # E,F,W,I,UP,B — line length 100
uv run mypy                      # strict, configured in pyproject.toml
uv run pytest

uv run python -m fivee_sim.coverage   # regenerate docs/COVERAGE.md
uv run python -m fivee_sim.rulings    # regenerate docs/RULINGS.md

# Mutation testing. Opt-in, never a gate, and always aimed at one function —
# note the x_ prefix mutmut mangles into every mutant name. See below for what
# it can and cannot reach before spending time on it.
uv run mutmut run "fivee_sim.service.adventures.x_carry_forward__mutmut_*"
uv run mutmut results                 # what survived, after a run

# From the repo root, against a contributor's verified local extraction.
python3 scripts/srd-catalog-batch.py --source-root /path/to/extracted validate

# From the repo root: the real launcher, end to end. Starts the engine and
# prints every operation the server serves. Needs no venv and no uv.
python3 souroldgeezer-fivee-sim/scripts/fivee.py help
python3 souroldgeezer-fivee-sim/scripts/fivee.py stop

# The same launcher, checked rather than eyeballed — see below. Stdlib only.
python3 scripts/check-api-smoke.py

bash scripts/hooks/test-ip-hygiene-check.sh
bash scripts/hooks/test-stop-audit-check.sh

# The browser assets, driven rather than read. Needs node — see below.
node scripts/check-editor-behaviour.mjs
```

**`scripts/check-api-smoke.py` is the repository's automated end-to-end gate**,
and the only thing that checks the shipped surface the way a host uses it.
`fivee.py help` above is a look, not a check; this is the check. It boots the
**real launcher** — never `python -m fivee_sim.web`, never the dev venv — runs a
complete seeded fight over plain HTTP, runs the identical fight in a second
server and a third time through the `fivee` binary as a subprocess, and requires
all three to agree. It then runs a **two-encounter adventure** end to end in a
fourth server — link, fight, finalize, link again carrying the survivors, fight,
compose the run with `adventure.replay` — then a **scene round-trip** in a fifth
(`map.put`, `scene.put`, read back, list, start the fight by posting the stored
body to `encounter.create`, act once), and finally holds `GET
/api/v1/operations`, `GET /api/v1/openapi.json` and `fivee help` against the
route table's own source.

Five of those claims exist nowhere else. **Reproducibility across processes**:
every other determinism test runs in one interpreter. **That the launcher
works**: nothing in pytest execs it. **That `/api/v1` is complete**: the client
is pinned by `tests/test_layering.py` to import nothing of the engine but
`fivee_sim.paths`, so a fight it can drive end to end is a fight the REST
surface serves — which is why the command run is the load-bearing one rather
than a convenience. **That state outlives a fight**: the adventure case is the
only end-to-end proof that a party's ending hit points become the next
encounter's starting ones, and it asserts the arrival against the previous
fight's *live* ending state rather than a second copy of the number, so it
cannot pass on a run that carries nobody. **That a scene is a saved
`encounter.create` body**: the scene artifact's whole design rests on that claim,
and this is the only place it is proved against the shipped surface rather than
asserted — the case posts the stored document whole, watches the refusal name the
one key that is a label rather than a fight, then posts the derived body and
starts the encounter.

**Standard library only, and no pytest**, because it has to run against an
environment where nothing has been built at all — which, since the launcher
stopped creating virtual environments, is every environment. It does not import
the engine either: it reads `web/routes.py` with `ast` rather than importing it,
since an imported copy is not the copy the launcher is serving. Every server it starts is pointed at a fresh `tempfile` directory —
honouring `TMPDIR`, which is what makes it runnable where `/tmp` is read-only —
and stopped and removed in a `finally`, because a leaked detached server would
make the next run lie.

Its fight constants are **golden values for one seed**, so a change to the rules
or the dice stream turns it red on purpose; reproduce, then recalibrate
deliberately. One thing it deliberately does *not* compare is the whole-file
sha256 of an exported replay: an enveloped bundle stamps every event and
checkpoint with the wall clock, so it is not byte-reproducible and never will
be. The
timestamp-free integrity hashes — initial state, actions, latest state, map,
content — are compared instead, and those are what the seed determines.

**`mutmut` is opt-in, and nothing requires it.** It is in the dev group and
configured in `pyproject.toml`, so `uv run mutmut run "<glob>"` works — but it is
not a gate, no step of any workflow calls for it, and that is a conclusion from
measurement rather than an omission.

**What it reaches, measured.** 34,983 mutants across 63 files, covering
module-level functions everywhere and the methods of **plain** classes —
`model/encounter.py` alone contributes 4,823, including 101 method symbols, so
`Encounter._do_attack` and `Encounter._death_save` are genuinely mutated.

**What it does not reach is the methods of a `@dataclass`.** `model/creature.py`
yields **zero** mutants; so do `Spell`'s methods in `kernel/spells.py` and the
effect rows in `kernel/grid.py` and `kernel/conditions.py`, whose files score
only on their module-level functions. That carve-out lands squarely on
`Creature` — `add_condition`, `take_damage`, `heal`, `grant_temp_hp`,
`save_modifier`, `check_modifier`, `attack_modifier`, `speed_for` — which is a
large share of where a rules defect actually lives. There is no config switch
for it: the option surface is `source_paths`, `only_mutate`, `do_not_mutate`,
`max_stack_depth`, `also_copy` and test selection. Module-level **constants** are
out of reach too, so a frozenset like `CARRIED_STATE_KEYS` produces no mutants
at all.

The measurement is easy to get backwards, so check before trusting a summary of
it: mutmut names a plain class's method `xǁEncounterǁ_do_attack__mutmut_1` with
a `ǁ` separator rather than the `x_name__mutmut_N` used for a module-level
function, and a sorted sample of symbol names shows only the latter.

**Why nothing requires it.** All four defects the parity work actually found sit
outside that reach, and not by coincidence — three of the four are in exactly the
shapes listed above. A negative condition level was in `Creature.add_condition`, a
`@dataclass` method. A state key that failed to carry was a module-level
constant. An SRD quotation was data. A site registry that accepted any
justification was test logic. Thirteen hand-written mutations against those areas
were all killed, and so were the twelve mutants mutmut generated for
`carry_forward`. **No case in this repository has yet demonstrated that mutmut
would have caught something review did not.**

So it is required nowhere, and the falsifiable condition for that changing is
worth writing down: **the first defect found inside its reach — a module-level
function, or a plain class's method — that a test should have caught** is the
first case where mutmut would have had a chance and did not get one. Until then,
running it is a judgement call, not an obligation.

**Two traps if you do run it.** Sixteen test files are excluded in
`[tool.mutmut]` for three distinct reasons, each named there — they read the
repository around the engine, they parse the engine's source with `ast` (mutmut
rewrites every function into mutant copies, so a source walker sees hundreds of
symbols that do not exist — which is also why mutmut cannot be used to check a
derived test), or they spawn subprocesses that collide with mutmut's own child
reaping. And a whole-file run is not the small step it sounds like:
`service/adventures.py` alone is 800 mutants against a suite that takes over two
minutes.

**The dev group pays for this**: mutmut brings sixteen packages, `textual` and
`rich` among them, where the group previously had three direct entries. It
changes nothing at runtime — `[project] dependencies` stays empty — and the
Python gate is unaffected.

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

### The virtual environment is a development tool only

`uv` builds it; nothing else should. `uv sync` in the engine directory creates
`.venv` with the dev group included, which is what `uv run pytest` and `uv run
mypy` use. **Nothing at runtime touches it.** `python3
souroldgeezer-fivee-sim/scripts/fivee.py help` succeeds with no `.venv` on disk
and `uv` removed from `PATH` entirely, which is the test of it.

This section used to describe a launcher that built a venv, stamped it, locked
it, and checked its console script's shebang still resolved. All of that is
gone, and the reason it could go is one line of `pyproject.toml`: `dependencies`
is empty, so the environment being built installed exactly one thing — this
package, whose source the launcher already has a path to. A zero-dependency pure
Python package needs `python -m`, not an environment. Roughly 950 lines of shell
went with that realisation.

**Durability is the part that survived, and it was never about dependencies.**
`web/http_server.py` reads its static assets through `resources.files(...)` *at
request time*, so a live server keeps reading from wherever the package lives —
and the installed plugin root can disappear when another session refreshes the
host cache. So a host-managed launch runs from a copy.

Claude Code supplies `${CLAUDE_PLUGIN_DATA}` directly. Codex supplies neither
that nor `${PLUGIN_DATA}`, so the launcher derives its plugin-data location from
`${CODEX_HOME}` alone — never from `${HOME}/.codex`, a guess that would put a
plain checkout on the host-managed path. An explicit host variable still wins,
and a variable exported empty counts as unset rather than as the filesystem root.

The copy lives at `$plugin_data/src/<source-id>`, where the id is a SHA-256 over
`pyproject.toml` and every shipped source file, paths included. Three properties
fall out of content-addressing it, and each replaced machinery that used to be
written by hand:

- **The directory's existence is the freshness check.** No build stamp.
- **Publishing is one `os.replace`.** Two launchers racing the same id each copy
  into their own staging directory; the loser discards its copy and uses the
  winner's, which is byte-identical because the name *is* the content. No lock,
  no owner token, no dead-owner reclamation.
- **A new build lands beside a live one** instead of overwriting it. Old copies
  are deliberately retained: the launcher cannot know which other process is
  still reading one.

The launcher also changes into the durable plugin-data directory before running.
That is load-bearing: maps and encounter journals may resolve a default from
`Path.cwd()` when an operation runs, and a process left inside a retired plugin
cache would otherwise turn its next journal append into raw `ENOENT`. Before that
change of directory, the launcher discovers the nearest `.fivee-sim/config.toml`
from the invocation directory (or loads global `--config PATH`) and resolves every
relative path against the file's directory. A selected file owns all
project-facing settings; the old `FIVEE_SIM_*` user variables are deprecated
compatibility fallbacks only when no file is selected. Host plugin-data variables
remain process plumbing rather than project configuration.

That pre-`chdir` result includes **no file found**. The launcher hands the result
to the client as already resolved even when it is `None`; the client must not
repeat discovery from the durable plugin-data directory and accidentally adopt a
config belonging to the runtime cache rather than the invoking project.

`engine/tests/test_launcher.py` pins all of it. Two cases there are load-bearing
in a way that is easy to undo by accident: they drive the launcher with the
**base** interpreter rather than `sys.executable`, because under pytest that is
the dev venv and the engine is importable there through an editable `.pth` — so
a subprocess test using it passes whether or not the launcher works.
`test_the_host_interpreter_does_not_already_have_the_engine` is the guard that
says so, and `test_a_spawned_server_can_import_the_engine` is the case that
caught the real defect: `sys.path` does not cross a process boundary, so the
source root has to be exported, not merely inserted.

**Dev reload is opt-in, and it is the same content hash doing the work.** The
launcher builds nothing, but a running server still holds the engine it imported
at startup — so editing `engine/src/` and re-running `fivee` silently tests the
old build. Set `[development] reload = true` in `.fivee-sim/config.toml` and the
launcher hashes the source it is about to run and exports the private digest as
`FIVEE_SIM_SOURCE_ID`; the server reads it once at construction and answers for
it on `GET /api/v1/ping`; `ensure_server` holds the two against each other and
replaces a server that no longer matches. `FIVEE_SIM_RELOAD=1` keeps its old
meaning only on the no-file compatibility path.
Unset is *no opinion* rather than no id, and no answer from a server can override
it — which is what stops a plain command restarting an engine somebody is
mid-fight in. The id comes from the ping and never the state file: a record
outlives its process and says what a launch was asked for, while the answer comes
from the process that would be replaced.

Identity rather than mtimes, for the reason the durable copy is
content-addressed: `git checkout` back to identical content restarts nothing,
`touch` restarts nothing, a changed byte restarts once. The whole tree hashes in
about 3 ms, and only when the flag is set. Nothing hands that digest to
`ensure_durable_source`, which computes its own: that function names a directory
it will never re-verify, so a supplied name is the one way its content
addressing could be made to lie. Everything else reads the digest back off the
path, because the name *is* the digest.

With a project file, the state record lives beside `config.toml`, not beside the
first maps root. That makes discovery stable when an edit changes a storage path:
the next command finds the old process, compares the semantic configuration
digest reported by its ping, and replaces it instead of orphaning it. Comments
and TOML formatting do not change that digest. The legacy no-file path retains
the maps-adjacent state record for compatibility.

**Three things about a reload are invisible from a green run.** *Fights survive
it* — `sessions.session_for` recovers a missing session from its journal,
reseeding the RNG and replaying every recorded action, so a restart mid-fight is
transparent to the caller. *But the fight is re-derived under the new code*, so a
kernel edit can leave the recovered state disagreeing with what the journal's
result records said happened; that is the feature working, and also its sharp
edge. *A runtime `content.configure` is lost*, because `EngineState.content` is
in-memory — content named by the project file reloads with the process, while a
pack configured by an API call has to be re-issued.

Static assets never needed any of this. `web/http_server.py` reads them per
request, so an edit to `editor.html` or `renderer.js` is live on the next browser
reload against a server that is already running — the same request-time read that
lets a live server survive its plugin root being retired.

## Conventions

git-workflow-policy: feature branches in dedicated worktrees, clean worktree,
explicit-path staging, rebase before integration, fast-forward-only integration,
no merge commits, no direct `main`. A feature branch stays linear: never merge
`main` into it and never use `git pull` in a mode that can create a merge commit.
After the branch's documented verification passes, rebase it onto the current
`main`, resolve and rerun affected verification, then integrate from `main` with
`git merge --ff-only`. If `main` moves before integration, repeat the
rebase-and-verification step rather than adding a merge commit.

Rebase only local, unpublished, single-owner branches. A pushed, reviewed,
shared, or otherwise externally consumed branch is published history: do not
rewrite it or force-push it; stop and coordinate a replacement or an explicit
exception. Project-local exceptions are the `release-policy` main-only version
commit below and an explicit current-task user instruction permitting direct
`main`. The character-device checks, worktree closeout procedure, and staging
discipline above remain required environment-specific runbooks.

planning-policy: default — before new feature or build work, brainstorm the
approach in plan mode and get it approved (`ExitPlanMode`) before implementing.
The approved plan names who implements it, and the strong default is subagents:
delegate every decomposable step unless the plan states the case against —
indivisible work, work that needs the live conversation, context that will not
survive a handoff brief, or parallel edits that would collide without worktree
isolation. "I can just do it" is not one of those. The parent session keeps
integration and verification. Scope: new feature, build, or creative work.
Exceptions (logged): trivial edits, hotfixes, spikes/throwaway, work a domain
skill owns end to end. Opt out per task by saying "skip planning" (logged).
Enforcement model.

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
table in [README.md](README.md), `engine/pyproject.toml`,
`engine/uv.lock`'s entry for the workspace package itself, and
`fivee_sim.__version__` (PEP 440 and semver strip the month's zero-padding in
their mirrors; `engine/tests/test_version.py` pins all six to one number).
`uv.lock` is the one nothing regenerates as part of bumping — only the next `uv
sync` or `uv run` does — so refresh it deliberately; it went stale across two
consecutive releases before it was pinned. Bumping is
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
