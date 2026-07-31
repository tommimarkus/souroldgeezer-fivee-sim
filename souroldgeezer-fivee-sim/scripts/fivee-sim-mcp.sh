#!/usr/bin/env bash
# Launcher for the bundled fivee-sim Model Context Protocol (MCP) stdio server.
#
# Declared as the plugin's `mcpServers.fivee_sim` command (plugin.json). Claude Code
# spawns and owns this process when the plugin is enabled.
#
# stdout carries JSON-RPC only. Every diagnostic here is deliberately routed to
# stderr — a single stray line on stdout corrupts the protocol stream and the
# server appears broken rather than noisy.
#
# uv builds the virtual environment; the server then runs straight out of that
# venv. Keeping uv out of the spawn path matters: `uv run` would re-resolve and
# add a process to every start, whereas exec'ing the venv's own console script is
# one process and needs uv present only when the environment has to be built.
#
# Resolve-on-demand, bounded. A stdio server that exits at spawn gets no auto-retry:
# it stays dead until the next session or `/reload-plugins`. So the sync runs only
# when the venv is missing, unusable, or built from a different engine, is bounded
# so it can never hang session start, and on failure reports once and exits rather
# than looping. Session start is non-blocking, so only a turn that actually needs a
# tool waits on a cold build.
#
# plugin.json points UV_PROJECT_ENVIRONMENT and UV_CACHE_DIR at ${CLAUDE_PLUGIN_DATA},
# because the installed plugin directory is a shared, versioned cache that must not
# be written into. That split is also this script's main hazard, and the reason for
# the build stamp below: ${CLAUDE_PLUGIN_DATA} is durable, while ${CLAUDE_PLUGIN_ROOT}
# carries the version in its path — so the venv outlives the engine it was built from.
set -euo pipefail

log() { printf 'fivee-sim-mcp: %s\n' "$1" >&2; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
engine_dir="$script_dir/../engine"

if [ ! -f "$engine_dir/pyproject.toml" ]; then
  log "engine not found at $engine_dir; server not started."
  exit 1
fi

# Absolute from here on: this path goes into the build stamp and is compared
# against on the next start, so it has to be the same string every time.
engine_dir="$(cd "$engine_dir" && pwd)"
venv_dir="${UV_PROJECT_ENVIRONMENT:-$engine_dir/.venv}"
server_bin="$venv_dir/bin/fivee-sim-mcp"
stamp_file="$venv_dir/.fivee-sim-build-stamp"

# Bound the build when `timeout` is available; degrade gracefully when it is not.
bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 300 "$@"
  else
    "$@"
  fi
}

# A venv is not portable: its console scripts hard-code an absolute interpreter
# path in their shebang, so moving or renaming the plugin directory leaves a venv
# that exists and is executable but cannot start. Checking that the shebang still
# resolves catches that in pure shell, and costs nothing on the warm path.
venv_is_usable() {
  [ -x "$server_bin" ] || return 1
  local interpreter
  interpreter="$(head -n 1 "$server_bin" | sed -e 's|^#!||' -e 's|^[[:space:]]*||' | cut -d' ' -f1)"
  [ -n "$interpreter" ] && [ -x "$interpreter" ]
}

# Which engine the venv was built from. `uv sync` installs the engine editable, so
# the venv pins one version's source directory — and an upgrade changes only that
# path, leaving everything the usability check looks at perfectly valid. Without
# this the server starts happily and answers from the previous version.
#
# A development checkout never moves, so the path alone would miss what changes in
# place there; the manifest and lock cover dependencies, entry points, and
# requires-python. Both files are read, never executed, and only at start.
build_stamp() {
  printf 'engine=%s\n' "$engine_dir"
  cksum "$engine_dir/pyproject.toml" "$engine_dir/uv.lock" 2>/dev/null || true
}

venv_is_current() {
  [ -f "$stamp_file" ] || return 1
  [ "$(< "$stamp_file")" = "$(build_stamp)" ]
}

if ! venv_is_usable; then
  if [ -e "$venv_dir" ] && [ -x "$server_bin" ]; then
    log "virtual environment at $venv_dir is stale (interpreter missing, usually a moved directory); rebuilding..."
    rm -rf "$venv_dir"
  else
    log "building the virtual environment (first run for this plugin version)..."
  fi
  sync_needed=1
elif ! venv_is_current; then
  log "virtual environment at $venv_dir was built from a different engine (usually a plugin upgrade); refreshing..."
  sync_needed=1
else
  sync_needed=0
fi

if [ "$sync_needed" = "1" ]; then
  if ! command -v uv >/dev/null 2>&1; then
    log "uv is required to build the environment and is not on PATH; see https://docs.astral.sh/uv/. Server not started."
    exit 1
  fi
  # --no-dev keeps the runtime environment to what the server actually imports;
  # test and lint tooling belongs to the development environment only.
  if ! bounded uv sync --project "$engine_dir" --frozen --no-dev 1>&2; then
    log "environment build failed; server not started (it will retry next session)."
    exit 1
  fi
  if ! venv_is_usable; then
    log "environment built but $server_bin is still not runnable; server not started."
    exit 1
  fi
  # Losing the stamp costs a redundant sync next start, which is not worth
  # refusing to serve over.
  if ! build_stamp > "$stamp_file" 2>/dev/null; then
    log "warning: could not record the build stamp at $stamp_file; the next start will sync again."
  fi
fi

exec "$server_bin"
