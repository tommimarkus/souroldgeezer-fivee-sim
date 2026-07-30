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
# when the venv is missing or unusable, is bounded so it can never hang session
# start, and on failure reports once and exits rather than looping. Session start is
# non-blocking, so only a turn that actually needs a tool waits on a cold build.
#
# plugin.json points UV_PROJECT_ENVIRONMENT and UV_CACHE_DIR at ${CLAUDE_PLUGIN_DATA},
# because the installed plugin directory is a shared, versioned cache that must not
# be written into.
set -euo pipefail

log() { printf 'fivee-sim-mcp: %s\n' "$1" >&2; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
engine_dir="$script_dir/../engine"
venv_dir="${UV_PROJECT_ENVIRONMENT:-$engine_dir/.venv}"
server_bin="$venv_dir/bin/fivee-sim-mcp"

if [ ! -f "$engine_dir/pyproject.toml" ]; then
  log "engine not found at $engine_dir; server not started."
  exit 1
fi

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

if ! venv_is_usable; then
  if [ -e "$venv_dir" ] && [ -x "$server_bin" ]; then
    log "virtual environment at $venv_dir is stale (interpreter missing, usually a moved directory); rebuilding..."
    rm -rf "$venv_dir"
  else
    log "building the virtual environment (first run for this plugin version)..."
  fi

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
fi

exec "$server_bin"
