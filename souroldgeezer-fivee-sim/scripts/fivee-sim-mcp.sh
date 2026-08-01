#!/usr/bin/env bash
# Launcher for the bundled fivee-sim Model Context Protocol (MCP) stdio server.
#
# Declared as the plugin's `fivee_sim` MCP command. The active plugin host spawns
# and owns this process when the plugin is enabled.
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
# it stays dead until the next session or a plugin reload. So the sync runs only
# when the venv is missing, unusable, or built from a different engine, is bounded
# so it can never hang session start, and on failure reports once and exits rather
# than looping. Session start is non-blocking, so only a turn that actually needs a
# tool waits on a cold build.
#
# An explicit uv environment wins; otherwise the launcher derives durable storage from
# ${PLUGIN_DATA}, then ${CLAUDE_PLUGIN_DATA}, then a host-specific fallback. Codex
# currently needs that fallback under ${CODEX_HOME}; a direct checkout still uses
# engine/.venv. A host-managed environment is a non-editable, content-addressed
# runtime copy under plugin data. Old and new sessions never mutate one another's
# runtime, and the server changes into that durable directory before exec so a
# retired plugin cache cannot invalidate its working directory.
set -euo pipefail

log() { printf 'fivee-sim-mcp: %s\n' "$1" >&2; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
engine_dir="$script_dir/../engine"

if [ ! -f "$engine_dir/pyproject.toml" ]; then
  log "engine not found at $engine_dir; server not started."
  exit 1
fi

# Absolute from here on: an explicit/development environment records this path in
# its build stamp, and the host-managed build reads source from it once.
engine_dir="$(cd "$engine_dir" && pwd)"
plugin_data="${PLUGIN_DATA:-${CLAUDE_PLUGIN_DATA:-}}"
if [ -z "$plugin_data" ] && [ "${FIVEE_SIM_PLUGIN_HOST:-}" = "codex" ]; then
  codex_home="${CODEX_HOME:-}"
  if [ -z "$codex_home" ]; then
    if [ -z "${HOME:-}" ]; then
      log "Codex runtime storage cannot be resolved because CODEX_HOME and HOME are unset; server not started."
      exit 1
    fi
    codex_home="$HOME/.codex"
  fi
  plugin_data="$codex_home/plugins/data/souroldgeezer-fivee-sim-souroldgeezer-tabletop"
fi
if [ -n "$plugin_data" ]; then
  if ! mkdir -p "$plugin_data"; then
    log "could not create the durable runtime directory at $plugin_data; server not started."
    exit 1
  fi
  plugin_data="$(cd "$plugin_data" && pwd)"
fi

# A host-managed runtime is an immutable copy of one exact engine build. The
# plugin root is replaceable — Codex may retire it when another session starts —
# so an editable install would leave a live process with imports and resources
# pointing into a directory the host is free to remove. Content-addressing the
# environment also means an upgrade builds beside an older live runtime instead
# of mutating it underneath that process.
runtime_copy=0
runtime_build_id=""
runtime_build_identity() {
  (
    cd "$engine_dir" || exit 1
    cksum pyproject.toml uv.lock
    find src -type f ! -path '*/__pycache__/*' ! -name '*.pyc' \
      | LC_ALL=C sort \
      | while IFS= read -r source_file; do
          cksum "$source_file"
        done
  )
}
runtime_build_key() {
  if command -v sha256sum >/dev/null 2>&1; then
    runtime_build_identity | sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    runtime_build_identity | shasum -a 256 | cut -d' ' -f1
  else
    # POSIX fallback for a minimal host. The full per-file checksums still feed
    # the key; the byte count makes the aggregate less collision-prone.
    local crc bytes
    read -r crc bytes _ < <(runtime_build_identity | cksum)
    printf '%s-%s\n' "$crc" "$bytes"
  fi
}
if [ -z "${UV_PROJECT_ENVIRONMENT:-}" ]; then
  if [ -n "$plugin_data" ]; then
    runtime_build_id="$(runtime_build_key)"
    export UV_PROJECT_ENVIRONMENT="$plugin_data/venvs/$runtime_build_id"
    runtime_copy=1
  else
    export UV_PROJECT_ENVIRONMENT="$engine_dir/.venv"
  fi
fi
if [ -z "${UV_CACHE_DIR:-}" ] && [ -n "$plugin_data" ]; then
  export UV_CACHE_DIR="$plugin_data/uv-cache"
fi
venv_dir="$UV_PROJECT_ENVIRONMENT"
server_bin="$venv_dir/bin/fivee-sim-mcp"
stamp_file="$venv_dir/.fivee-sim-build-stamp"
build_lock_dir="$venv_dir.fivee-sim-build-lock"
build_lock_owner="$build_lock_dir/owner"
build_lock_token=""
build_lock_acquired=0

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

# Which engine an explicit/development venv was built from. `uv sync` installs
# that case editable, so the venv pins one source directory and needs the path in
# its stamp. A host-managed runtime instead records the build identity already in
# its directory name and contains a non-editable copy.
#
# A development checkout never moves, so the path alone would miss what changes in
# place there; the manifest and lock cover dependencies, entry points, and
# requires-python. Both files are read, never executed, and only at start.
build_stamp() {
  if [ "$runtime_copy" = "1" ]; then
    printf 'mode=immutable-copy\n'
    printf 'build=%s\n' "$runtime_build_id"
    return
  fi
  printf 'engine=%s\n' "$engine_dir"
  cksum "$engine_dir/pyproject.toml" "$engine_dir/uv.lock" 2>/dev/null || true
}

venv_is_current() {
  [ -f "$stamp_file" ] || return 1
  [ "$(< "$stamp_file")" = "$(build_stamp)" ]
}

sync_reason=""
sync_is_needed() {
  if ! venv_is_usable; then
    sync_reason="unusable"
    return 0
  fi
  if ! venv_is_current; then
    sync_reason="stale"
    return 0
  fi
  sync_reason=""
  return 1
}

lock_owner_is_alive() {
  local owner owner_pid
  owner="$(cat "$build_lock_owner" 2>/dev/null || true)"
  owner_pid="${owner%%:*}"
  case "$owner_pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  kill -0 "$owner_pid" 2>/dev/null
}

release_build_lock() {
  if [ "$build_lock_acquired" != "1" ]; then
    return
  fi
  if [ -f "$build_lock_owner" ] && \
      [ "$(cat "$build_lock_owner" 2>/dev/null || true)" = "$build_lock_token" ]; then
    rm -rf "$build_lock_dir"
  fi
  build_lock_acquired=0
}

acquire_build_lock() {
  local lock_parent start_seconds observed_owner current_owner wait_logged
  lock_parent="$(dirname "$build_lock_dir")"
  if ! mkdir -p "$lock_parent"; then
    log "could not create the runtime directory at $lock_parent; server not started."
    return 1
  fi

  start_seconds=$SECONDS
  wait_logged=0
  while ! mkdir "$build_lock_dir" 2>/dev/null; do
    if [ ! -d "$build_lock_dir" ]; then
      log "could not create the environment build lock at $build_lock_dir; server not started."
      return 1
    fi

    if ! lock_owner_is_alive; then
      observed_owner="$(cat "$build_lock_owner" 2>/dev/null || true)"
      # A winning mkdir writes its owner immediately. Give that tiny window one
      # polling interval before treating an ownerless directory as abandoned.
      if [ -z "$observed_owner" ]; then
        sleep 1
      fi
      current_owner="$(cat "$build_lock_owner" 2>/dev/null || true)"
      if [ "$current_owner" = "$observed_owner" ] && ! lock_owner_is_alive; then
        log "reclaiming orphaned environment build lock at $build_lock_dir..."
        rm -rf "$build_lock_dir"
        continue
      fi
    fi

    if [ "$wait_logged" = "0" ]; then
      log "another launcher is building the environment; waiting for $build_lock_dir..."
      wait_logged=1
    fi
    if [ $((SECONDS - start_seconds)) -ge 300 ]; then
      log "timed out after 300 seconds waiting for the environment build lock; server not started."
      return 1
    fi
    sleep 1
  done

  build_lock_token="$$:${RANDOM}:${SECONDS}"
  if ! printf '%s\n' "$build_lock_token" > "$build_lock_owner"; then
    rm -rf "$build_lock_dir"
    log "could not record ownership of the environment build lock; server not started."
    return 1
  fi
  build_lock_acquired=1
  trap release_build_lock EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

# The warm path ends here: it neither invokes uv nor even opens the lock path.
if sync_is_needed; then
  if ! acquire_build_lock; then
    exit 1
  fi

  # A concurrent launcher may have completed the build while this one waited.
  # Every environment mutation stays below this second check and above release.
  if sync_is_needed; then
    if [ "$sync_reason" = "unusable" ]; then
      if [ -e "$venv_dir" ]; then
        log "virtual environment at $venv_dir is unusable or incomplete; rebuilding..."
        rm -rf "$venv_dir"
      else
        log "building the virtual environment (first run for this plugin version)..."
      fi
    else
      log "virtual environment at $venv_dir was built from a different engine; refreshing..."
    fi

    if ! command -v uv >/dev/null 2>&1; then
      log "uv is required to build the environment and is not on PATH; see https://docs.astral.sh/uv/. Server not started."
      exit 1
    fi
    # --no-dev keeps the runtime environment to what the server actually imports;
    # test and lint tooling belongs to the development environment only.
    sync_args=(sync --project "$engine_dir" --frozen --no-dev)
    if [ "$runtime_copy" = "1" ]; then
      sync_args+=(--no-editable)
    fi
    if ! bounded uv "${sync_args[@]}" 1>&2; then
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

  release_build_lock
  trap - EXIT HUP INT TERM
fi

if [ -n "$plugin_data" ] && ! cd "$plugin_data"; then
  log "durable runtime directory disappeared at $plugin_data; server not started."
  exit 1
fi
exec "$server_bin"
