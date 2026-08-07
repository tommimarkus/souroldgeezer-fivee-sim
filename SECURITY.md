# Security policy

## Supported versions

Only the most recent release receives fixes. Versions are calendar-based
(`YYYY.0M.build`); the current one is in the plugin table in
[README.md](README.md). There are no long-term support branches — if you are on
an older build, upgrading is the fix.

## Reporting a vulnerability

Email **claude-marketplace-a.varsity439@passmail.net** with `SECURITY` in the
subject. Please do not open a public issue for anything exploitable.

Useful to include: the version, what an attacker gains, and the smallest steps
that reproduce it. A map file, content pack, or tool call that triggers the
problem is worth more than a description of it.

This is a single-maintainer project, so the honest commitment is best-effort
rather than a contractual SLA: acknowledgement within **7 days**, and a fix or a
written explanation of why it is not one within **30 days**. If you have not
heard back in a week, assume the mail went astray and send it again.

You are welcome to disclose publicly after a fix ships, or after 90 days if
nothing has shipped and nothing has been explained.

## What this software actually exposes

Worth stating plainly, because it shapes what counts as a vulnerability here.

- **The engine binds a port, and that is now the whole of it.** There used to be
  a stdio server here that listened on nothing, with the browser editor as the
  one component that opened a socket. The stdio server is gone: every operation
  is served by one localhost HTTP process, started on demand by the `fivee`
  command, and the landing page, map editor and replay viewer are three pages
  that process serves.
  So the controls below are not a side surface any more — they are the engine's
  only front door, and they are what a report should aim at.
- **What those controls are**, listed in the module docstring of
  `souroldgeezer-fivee-sim/engine/src/fivee_sim/web/http_server.py`: bound to
  `127.0.0.1` on an ephemeral port; every `/api/*` request must carry a
  per-launch random token, written to a state file only the launching user can
  read; a request whose `Host` is neither `127.0.0.1` nor `localhost` is refused,
  so a DNS-rebinding page cannot drive it; no CORS headers are ever emitted;
  bodies are size-bounded before being read; and map ids resolve strictly under
  the maps directory. **Anything that bypasses one of those is a real finding.**
- **Four operations take a path you give them, and are not contained.** The
  bullet above is about *ids*, and it would be easy to read it as covering
  everything. It does not. `map.uvtt` and `encounter.replay` write to any
  `path` you pass, and `content.validate` and `content.configure` read and
  parse any `paths` you pass as content packs. That is the feature — exporting
  a map to a virtual tabletop and loading a campaign's own material are the
  point of those operations — but it means **the token authorises arbitrary
  local file writes and reads under the launching user**, not only writes
  inside `.fivee-sim/`.
  
  This is a real change in who can reach that capability, and it is worth
  stating rather than discovering. Until `2026.08.34` those four were tools on
  a stdio server, so reaching them meant already being able to run code as you
  — which is out of scope below. They are now HTTP operations, so reaching them
  means holding the token, so **a way to obtain the token is a serious finding,
  and more serious than it looks**, because of what it now reaches.

  Where the token travels is therefore worth stating exactly. It is random per
  launch, `0600` at rest, and constant-time compared. It reaches the browser as
  the **fragment** of the URL `fivee serve` prints — `…/editor#<token>` — which
  the page reads off `location.hash` and then strips from the visible address.
  It is not logged, and a fragment is the one part of a URL a browser never
  sends, so it reaches no request log, no `Referer`, and no problem+json
  `instance`.

  It used to be injected into the body of `/`, `/editor` and `/viewer`
  instead, and that was a hole rather than a nuance: those pages are
  served to any client on the port *without* the token — being what tells the
  browser the token is exactly why they have to be — so any local process could
  `GET /`, read the token out of the markup, and go straight to the arbitrary
  file write above, with no filesystem access and no need to read the state
  file at all. It is fixed, and the trade that fixed it runs one way: the
  terminal that prints the URL is already inside the user's trust domain, and
  the loopback socket is not.

  What is left is what a URL costs. Anywhere a user pastes one — a bug report,
  a chat, a shared screen, a shell history — carries the token with it, and
  anything that can read the launching user's clipboard or terminal scrollback
  can read it. That is the same trust domain that could read the `0600` state
  file, which is why it is a trade rather than a new exposure; a token
  disclosed that way is still a finding worth telling us about.
- **The browser assets are offline.** `editor.html`, `viewer.html`, and
  `renderer.js` load nothing from a network; `tests/test_web_assets.py` asserts
  it.
- **Untrusted input is content packs and map files.** These are JSON, parsed and
  schema-validated on load. A pack that escapes validation, crashes the engine,
  or reaches the filesystem outside its own directory is a real finding.
- **An adventure module is untrusted too, and the seat that holds it is
  scoped.** The packaged `game-master` agent's whole job is to read a module
  somebody else wrote, so its declared tools are the launcher command and
  nothing else — `Bash(python3 /${CLAUDE_PLUGIN_ROOT}/scripts/fivee.py:*)`, plus
  `Read` and `Skill`, with a `disallowedTools` denylist beside the allowlist
  because the two together hold whichever way a host resolves them. The
  `typical-player` seat is narrower still: one `Read` scoped to the plugin's
  `player-visible/` directory, and no shell at all.

  Two limits on that, both deliberate. **`Read` is not scoped for the game
  master** — it is handed a module path the user chooses, so confining it to a
  directory would break the feature — which means a module that talks the seat
  into reading something else is asking for a capability the seat has. Treat
  prompt injection through module text as in scope; `skills/play/SKILL.md` names
  it and tells the seat what to do about it. And **these grants are enforced by
  the host, not by us.** They are frontmatter a host reads; the engine has no
  view of them and cannot check one. On a host that ignores or does not
  implement them, they are documentation.
- **No telemetry, no accounts, no secrets.** Nothing here needs a credential to
  run, and nothing is collected or sent anywhere. The engine does make HTTP
  requests, and they are worth knowing about before you flag them: `fivee` is an
  HTTP client, so every operation it performs is a request to
  `http://127.0.0.1:<port>` with the launch token attached, as are the liveness
  ping and the shutdown it uses to find and stop a server. Loopback only, and the
  host is hardcoded — there is no other outbound call in the tree.

Out of scope: the security of Claude Code or Codex itself (report those to the
respective host vendor), `uv` and the Python packages in `uv.lock` (report
upstream, though a note here is welcome if we are pinning something
known-vulnerable), and findings that require an attacker who already has local
code execution as your user — at that point the engine is not the weakest thing
on the machine.

## Bundled dependencies

**The engine has no runtime dependencies.** `engine/pyproject.toml` declares an
empty `dependencies` list, so a run holds this package and the standard library.
There is no third-party runtime code here to report a CVE against, which is the
point of keeping it that way.

The launcher does not install anything, and that is worth stating precisely
because this section used to say it "syncs with `--no-dev`". It does not sync at
all — there is no runtime virtual environment to sync. A zero-dependency pure
Python package needs only `python -m` and a path to its own source, so the
launcher puts the source on `sys.path` and runs it. What it does build is a
content-addressed **copy** of that source under the host's plugin-data
directory, so a live server survives its plugin root being replaced; a copy is
not an install, and it introduces no third-party code.

Development tooling — pytest, mypy, ruff, and mutmut with its own dependencies —
is pinned in
[`souroldgeezer-fivee-sim/engine/uv.lock`](souroldgeezer-fivee-sim/engine/uv.lock)
and installed only into a development environment. If you are reporting a
vulnerable dependency, quote the locked version and say whether it reaches a
runtime install; the lock is the answer to "what is actually installed", not the
manifest.

## What we hold ourselves to

Stated so a reader does not have to infer it from what happens to be here.

**OWASP ASVS Level 1 is the target**, and it is the right one: L1 is the level
for software without a formal risk assessment behind it, which describes this.
The gap self-assessment is short because the surface is: the engine binds
loopback only, holds no accounts, stores no credentials, and processes JSON from
a user who already runs it. Authentication is one per-launch token, and the
disclosure path that made it reachable without filesystem access is closed
above. What ASVS would still fault is the absence of per-seat authorisation —
`as=` and `actor` are projections, not access controls, and both say so where
they are defined. Closing that means per-seat credentials, which is a design
change and not a hardening pass.

**No SLSA level is claimed, at all.** That is a consequence of the three
decisions below rather than an oversight: SLSA Build L1 wants a scripted build
run by a service that emits provenance, and there is deliberately no build
service, no tag, and no provenance here. Claiming L1 while installs track a
moving branch would be worse than claiming nothing.

### Accepted posture

Three things an audit will find missing. Each is a decision, so finding one
again is not a finding:

- **The verification gates are developer-invoked, not enforced by CI.** `ruff`,
  `mypy --strict`, `pytest`, `check-api-smoke.py`, `check-editor-behaviour.mjs`
  and both hook suites are listed in `CLAUDE.md` and run before integration.
  There is no workflow that blocks a merge on them.
- **Installs track a mutable branch.** `marketplace.json` installs by cloning,
  so what a user gets is whatever that branch says today.
- **There are no tags and no signed commits.** Nothing here is a signed,
  immutable release artifact, and a consumer cannot pin one.

The consequence worth being explicit about: **a consumer has no cryptographic
way to verify what they installed**, and the version string is a claim in a file
rather than something anchored to a signature. If that matters for your use,
pin your own clone.
