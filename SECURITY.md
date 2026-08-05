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
  means holding the token. The token is well defended (random per launch,
  `0600` at rest, constant-time compared, never logged and never in a URL), and
  we know of no way to obtain it short of code execution as the user. **A way
  to obtain it is therefore a serious finding, and more serious than it looks**,
  because of what it now reaches.
- **The browser assets are offline.** `editor.html`, `viewer.html`, and
  `renderer.js` load nothing from a network; `tests/test_web_assets.py` asserts
  it.
- **Untrusted input is content packs and map files.** These are JSON, parsed and
  schema-validated on load. A pack that escapes validation, crashes the engine,
  or reaches the filesystem outside its own directory is a real finding.
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
empty `dependencies` list, and the launcher syncs with `--no-dev`, so a runtime
environment holds this package and the standard library. There is no third-party
runtime code here to report a CVE against, which is the point of keeping it that
way.

Development tooling — pytest, mypy, ruff — is pinned in
[`souroldgeezer-fivee-sim/engine/uv.lock`](souroldgeezer-fivee-sim/engine/uv.lock)
and installed only into a development environment. If you are reporting a
vulnerable dependency, quote the locked version and say whether it reaches a
runtime install; the lock is the answer to "what is actually installed", not the
manifest.
