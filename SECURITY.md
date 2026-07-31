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

- **The MCP server speaks stdio, not a socket.** Claude Code spawns it as a child
  process and owns both ends of the pipe. It listens on no port.
- **The map editor is the one thing that binds a port.** It is a single-user
  localhost tool, and the controls it does have are listed in the module
  docstring of `souroldgeezer-fivee-sim/engine/src/fivee_sim/editor/http_server.py`:
  bound to `127.0.0.1` on an ephemeral port; every `/api/*` request must carry a
  per-launch random token; a request whose `Host` is neither `127.0.0.1` nor
  `localhost` is refused, so a DNS-rebinding page cannot drive it; no CORS
  headers are ever emitted; bodies are size-bounded before being read; and map
  ids resolve strictly under the maps directory. **Anything that bypasses one of
  those is a real finding** — that list is the contract, and it is what a report
  should aim at.
- **The browser assets are offline.** `editor.html`, `viewer.html`, and
  `renderer.js` load nothing from a network; `tests/test_web_assets.py` asserts
  it.
- **Untrusted input is content packs and map files.** These are JSON, parsed and
  schema-validated on load. A pack that escapes validation, crashes the engine,
  or reaches the filesystem outside its own directory is a real finding.
- **No telemetry, no accounts, no secrets.** Nothing here needs a credential to
  run, and nothing is collected or sent anywhere. The engine does make HTTP
  requests, and they are worth knowing about before you flag them: the
  `map_editor_serve` and `map_editor_stop` tools ping and shut down the editor
  over `http://127.0.0.1:<port>`, with the launch token attached. Loopback only,
  hardcoded — there is no other outbound call in the tree.

Out of scope: the security of Claude Code itself (report those to Anthropic),
`uv` and the Python packages in `uv.lock` (report upstream, though a note here is
welcome if we are pinning something known-vulnerable), and findings that require
an attacker who already has local code execution as your user — at that point the
engine is not the weakest thing on the machine.

## Bundled dependencies

Runtime dependencies are pinned in
[`souroldgeezer-fivee-sim/engine/uv.lock`](souroldgeezer-fivee-sim/engine/uv.lock),
and the launcher installs strictly from that lock. If you are reporting a
vulnerable transitive dependency, quote the locked version — the lock is the
answer to "what is actually installed", not the manifest.
