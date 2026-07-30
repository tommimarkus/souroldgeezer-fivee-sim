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

Both copies must stay identical: the repo-root one and `fivee-sim/NOTICE`, which
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

**The engine lives under the plugin root**, at `fivee-sim/engine/`. This is not
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
conditions, attacks, spells — and knows nothing about creatures; callers pass the
handful of values a roll depends on. `model/` owns creatures and is the only place
combat state changes. Spell definitions live in `kernel/spells.py` rather than a
separate layer because they are resolution primitives like the rest.

**Every tool reports its seed.** A tool called without one picks a seed and
returns it, so no result is ever irreproducible.

## Tooling

```bash
cd fivee-sim/engine
uv run ruff check .              # E,F,W,I,UP,B — line length 100
uv run mypy                      # strict, configured in pyproject.toml
uv run pytest

# From the repo root: real JSON-RPC against the real launcher.
python3 scripts/check-mcp-handshake.py
bash scripts/hooks/test-ip-hygiene-check.sh
```

`uv`'s cache is redirected to `fivee-sim/engine/.cache/uv` because the default
`~/.cache/uv` is read-only in the sandboxed development environment.

## Conventions

Mirrors the sibling `souroldgeezer` marketplace at `../skills`:
date-based plugin versions (`2026.07.1`); `plugin.json` carrying `name`,
`version`, `description`, `author`, `license`; `AGENTS.md` as a pointer to this
file rather than a second copy of it.
