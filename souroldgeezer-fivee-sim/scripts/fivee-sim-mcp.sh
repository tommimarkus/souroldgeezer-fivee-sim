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
# Resolve-on-demand, bounded. A stdio server that exits at spawn gets no auto-retry:
# it stays dead until the next session or `/reload-plugins`. So the dependency sync
# runs only when the environment is missing, is bounded so it can never hang session
# start, and on failure reports once and exits rather than looping. Session start is
# non-blocking, so only a turn that actually needs a tool waits on a cold resolve.
#
# plugin.json points UV_PROJECT_ENVIRONMENT and UV_CACHE_DIR at ${CLAUDE_PLUGIN_DATA},
# because the installed plugin directory is a shared, versioned cache that must not
# be written into.
set -euo pipefail

log() { printf 'fivee-sim-mcp: %s\n' "$1" >&2; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
engine_dir="$script_dir/../engine"

if [ ! -f "$engine_dir/pyproject.toml" ]; then
  log "engine not found at $engine_dir; server not started."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  log "uv is required and is not on PATH; see https://docs.astral.sh/uv/. Server not started."
  exit 1
fi

# Bound the resolve when `timeout` is available; degrade gracefully when it is not.
bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 300 "$@"
  else
    "$@"
  fi
}

venv_dir="${UV_PROJECT_ENVIRONMENT:-$engine_dir/.venv}"
if [ ! -x "$venv_dir/bin/fivee-sim-mcp" ]; then
  log "resolving dependencies (first run for this plugin version)..."
  if ! bounded uv sync --project "$engine_dir" --frozen 1>&2; then
    log "dependency resolve failed; server not started (it will retry next session)."
    exit 1
  fi
fi

exec uv run --project "$engine_dir" --frozen --no-sync fivee-sim-mcp
