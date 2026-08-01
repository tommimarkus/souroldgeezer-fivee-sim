#!/usr/bin/env bash
# Tests for the venv freshness decision in fivee-sim-mcp.sh.
#
# The launcher straddles two directories with different lifetimes:
# The plugin root is replaced per version, while the host's plugin-data directory
# — where the launcher puts the venv — is durable. Because `uv sync` installs the
# engine editable, the venv pins a version-specific source path, so an upgrade
# leaves a venv that works perfectly and answers from the *previous* version.
# The cases below are mostly about that: what the launcher must notice, and what
# it must not waste a process on.
#
# Hermetic and offline. Each case plants a throwaway plugin root and drives the
# real launcher against a fake `uv` that materialises just enough of a venv for
# the launcher's own checks — a console script whose shebang resolves, echoing
# the engine it was built from so a test can tell v1 from v2. Nothing here
# touches the real repo, the real venv, or the network.
#
# Usage: bash scripts/test-launcher-freshness.sh

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
launcher="$repo_root/souroldgeezer-fivee-sim/scripts/fivee-sim-mcp.sh"

pass=0
fail=0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

uvlog="$tmp/uv-invocations"
: > "$uvlog"

# --- the fake uv ----------------------------------------------------------
mkdir -p "$tmp/bin"
cat > "$tmp/bin/uv" <<'FAKE_UV'
#!/usr/bin/env bash
# Records the invocation, then writes a stub console script. The engine path is
# resolved here so a test can compare it whether the launcher passed an absolute
# path or one still carrying `..`.
set -uo pipefail
printf 'args=%s cache=%s venv=%s\n' \
  "$*" "${UV_CACHE_DIR:-}" "${UV_PROJECT_ENVIRONMENT:-}" >> "${UV_LOG:?}"
if [ "${FAKE_UV_FAIL:-0}" = "1" ]; then
  printf 'fake uv: refusing to sync\n' >&2
  exit 1
fi
if [ -n "${FAKE_UV_READY:-}" ]; then
  : > "$FAKE_UV_READY"
fi
if [ -n "${FAKE_UV_GATE:-}" ]; then
  IFS= read -r _ < "$FAKE_UV_GATE"
fi
project=""
while [ $# -gt 0 ]; do
  case "$1" in
    --project) project="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done
project="$(cd "$project" && pwd)"
mkdir -p "${UV_PROJECT_ENVIRONMENT:?}/bin"
{
  printf '#!/bin/sh\n'
  printf 'printf "launch=%%s\\n" "$$" >> "${SERVER_LOG:-/dev/null}"\n'
  printf 'echo engine=%s\n' "$project"
} > "$UV_PROJECT_ENVIRONMENT/bin/fivee-sim-mcp"
chmod +x "$UV_PROJECT_ENVIRONMENT/bin/fivee-sim-mcp"
FAKE_UV
chmod +x "$tmp/bin/uv"

# A PATH with the launcher's utilities but no uv at all, for the two cases that
# pin what happens when the environment cannot be built. Trimming the real PATH
# would not do it: uv lives in /usr/bin here.
mkdir -p "$tmp/minbin"
for util in bash cat env dirname head sed cut mkdir rm sleep timeout cksum; do
  resolved="$(command -v "$util" 2>/dev/null)"
  if [ -z "$resolved" ]; then
    printf 'FATAL: %s is not on PATH; the no-uv cases need it.\n' "$util" >&2
    exit 1
  fi
  ln -sf "$resolved" "$tmp/minbin/$util"
done
if PATH="$tmp/minbin" command -v uv >/dev/null 2>&1; then
  printf 'FATAL: the no-uv PATH still finds uv; the last two cases would not test anything.\n' >&2
  exit 1
fi

# --- harness --------------------------------------------------------------
plant() { # plant <label> — a throwaway plugin root at $tmp/<label>
  local root="$tmp/$1"
  mkdir -p "$root/scripts" "$root/engine"
  cp "$launcher" "$root/scripts/fivee-sim-mcp.sh"
  printf '[project]\nname = "fivee-sim"\nversion = "0.0.0"\n' > "$root/engine/pyproject.toml"
  printf 'version = 1\nrequires-python = ">=3.11"\n' > "$root/engine/uv.lock"
  printf '%s' "$root"
}

out=""; err=""; rc=0; uvcalls=0
launch() { # launch <label> <venv-name> [VAR=value ...]
  local root="$tmp/$1" venv="$tmp/venvs/$2"
  shift 2
  : > "$uvlog"
  out="$(env "$@" \
      PATH="${LAUNCH_PATH:-$tmp/bin:$PATH}" \
      UV_LOG="$uvlog" \
      UV_PROJECT_ENVIRONMENT="$venv" \
      timeout 10 bash "$root/scripts/fivee-sim-mcp.sh" 2>"$tmp/stderr")"
  rc=$?
  err="$(cat "$tmp/stderr")"
  uvcalls="$(grep -c . "$uvlog")"
}

launch_with_host_data() { # launch_with_host_data <label> [VAR=value ...]
  local root="$tmp/$1"
  shift
  : > "$uvlog"
  out="$(env -u UV_PROJECT_ENVIRONMENT -u UV_CACHE_DIR \
      -u PLUGIN_DATA -u CLAUDE_PLUGIN_DATA \
      "$@" \
      PATH="${LAUNCH_PATH:-$tmp/bin:$PATH}" \
      UV_LOG="$uvlog" \
      timeout 10 bash "$root/scripts/fivee-sim-mcp.sh" 2>"$tmp/stderr")"
  rc=$?
  err="$(cat "$tmp/stderr")"
  uvcalls="$(grep -c . "$uvlog")"
}

report() { # report <ok?> <label> <detail>
  if [ "$1" = "0" ]; then
    pass=$((pass + 1))
    printf '  PASS  %s\n' "$2"
  else
    fail=$((fail + 1))
    printf '  FAIL  %s\n' "$2"
    printf '%s\n' "$3" | sed 's/^/          | /'
  fi
}

want_stdout() { # want_stdout <label> <substring>
  case "$out" in
    *"$2"*) report 0 "$1" "" ;;
    *) report 1 "$1" "stdout: ${out:-<empty>}
wanted substring: $2
stderr: ${err:-<empty>}" ;;
  esac
}

want_rc() { # want_rc <label> <expected rc>
  if [ "$rc" -eq "$2" ]; then report 0 "$1" ""; else
    report 1 "$1" "rc=$rc (want $2)
stdout: ${out:-<empty>}
stderr: ${err:-<empty>}"
  fi
}

want_syncs() { # want_syncs <label> <expected count>
  if [ "$uvcalls" -eq "$2" ]; then report 0 "$1" ""; else
    report 1 "$1" "uv invoked $uvcalls time(s), want $2
stderr: ${err:-<empty>}"
  fi
}

want_uv_args() { # want_uv_args <label> <substring>
  if grep -Fq -- "$2" "$uvlog"; then report 0 "$1" ""; else
    report 1 "$1" "uv invocation did not contain: $2
invocation: $(cat "$uvlog")"
  fi
}

wait_for_path() { # wait_for_path <path>
  local path="$1"
  for _ in {1..50}; do
    [ -e "$path" ] && return 0
    sleep 0.1
  done
  return 1
}

wait_for_text() { # wait_for_text <path> <literal>
  local path="$1" literal="$2"
  for _ in {1..50}; do
    grep -Fq -- "$literal" "$path" 2>/dev/null && return 0
    sleep 0.1
  done
  return 1
}

mkdir -p "$tmp/venvs"
V1="$(plant v1)"
V2="$(plant v2)"

# --- host-managed durable storage ----------------------------------------
codex_data="$tmp/codex-data"
launch_with_host_data v1 PLUGIN_DATA="$codex_data"
want_rc    "Codex plugin data builds successfully" 0
want_syncs "Codex plugin data syncs once" 1
want_uv_args "Codex plugin data owns the uv cache" "cache=$codex_data/uv-cache"
if [ -x "$codex_data/venv/bin/fivee-sim-mcp" ]; then
  report 0 "Codex plugin data owns the venv" ""
else
  report 1 "Codex plugin data owns the venv" "missing: $codex_data/venv/bin/fivee-sim-mcp"
fi

claude_data="$tmp/claude-data"
launch_with_host_data v1 CLAUDE_PLUGIN_DATA="$claude_data"
want_rc    "Claude plugin data still builds successfully" 0
want_syncs "Claude plugin data still syncs once" 1
want_uv_args "Claude plugin data owns the uv cache" "cache=$claude_data/uv-cache"
if [ -x "$claude_data/venv/bin/fivee-sim-mcp" ]; then
  report 0 "Claude plugin data owns the venv" ""
else
  report 1 "Claude plugin data owns the venv" "missing: $claude_data/venv/bin/fivee-sim-mcp"
fi

preferred_data="$tmp/preferred-data"
fallback_data="$tmp/fallback-data"
launch_with_host_data v1 \
  PLUGIN_DATA="$preferred_data" CLAUDE_PLUGIN_DATA="$fallback_data"
want_rc "Codex data wins when both host variables exist" 0
if [ -x "$preferred_data/venv/bin/fivee-sim-mcp" ] && [ ! -e "$fallback_data/venv" ]; then
  report 0 "Codex data precedes the Claude fallback" ""
else
  report 1 "Codex data precedes the Claude fallback" \
    "preferred venv missing or fallback venv unexpectedly created"
fi

explicit_data="$tmp/explicit-data"
launch v1 explicit PLUGIN_DATA="$explicit_data"
want_rc "an explicit UV environment wins over host plugin data" 0
if [ -x "$tmp/venvs/explicit/bin/fivee-sim-mcp" ] && [ ! -e "$explicit_data/venv" ]; then
  report 0 "explicit UV storage precedes host storage" ""
else
  report 1 "explicit UV storage precedes host storage" \
    "explicit venv missing or host venv unexpectedly created"
fi

explicit_cache="$tmp/explicit-cache"
launch v1 explicit-cache \
  PLUGIN_DATA="$explicit_data" UV_CACHE_DIR="$explicit_cache"
want_rc "an explicit UV cache wins over host plugin data" 0
want_uv_args "explicit UV cache precedes host storage" "cache=$explicit_cache"

# Codex does not currently inject PLUGIN_DATA into bundled MCP children. The
# manifest marks the host explicitly, so the launcher must derive durable state
# outside the replaceable plugin cache without weakening any existing override.
codex_home="$tmp/codex-home"
codex_runtime="$codex_home/plugins/data/souroldgeezer-fivee-sim-souroldgeezer-tabletop"
CODEX_V1="$(plant codex-cache-v1)"
launch_with_host_data codex-cache-v1 \
  FIVEE_SIM_PLUGIN_HOST=codex CODEX_HOME="$codex_home"
want_rc    "Codex fallback storage builds successfully" 0
want_syncs "Codex fallback storage syncs once" 1
want_uv_args "Codex fallback owns the uv cache" "cache=$codex_runtime/uv-cache"
if [ -x "$codex_runtime/venv/bin/fivee-sim-mcp" ] && \
    [ ! -e "$CODEX_V1/engine/.venv" ]; then
  report 0 "Codex runtime storage is outside the plugin cache" ""
else
  report 1 "Codex runtime storage is outside the plugin cache" \
    "durable venv missing or cache-local venv unexpectedly created"
fi

# Removing the whole plugin root must leave runtime state intact. A replacement
# version then refreshes that same durable environment from its new engine.
rm -rf "$CODEX_V1"
if [ -x "$codex_runtime/venv/bin/fivee-sim-mcp" ]; then
  report 0 "Codex runtime survives plugin-root replacement" ""
else
  report 1 "Codex runtime survives plugin-root replacement" \
    "durable venv disappeared with the plugin root"
fi
CODEX_V2="$(plant codex-cache-v2)"
launch_with_host_data codex-cache-v2 \
  FIVEE_SIM_PLUGIN_HOST=codex CODEX_HOME="$codex_home"
want_rc     "replacement plugin root starts successfully" 0
want_syncs  "replacement plugin root refreshes once" 1
want_stdout "replacement plugin root runs the new engine" "engine=$CODEX_V2/engine"

codex_claude_data="$tmp/codex-claude-data"
launch_with_host_data v1 \
  FIVEE_SIM_PLUGIN_HOST=codex CODEX_HOME="$tmp/ignored-codex-home" \
  CLAUDE_PLUGIN_DATA="$codex_claude_data"
want_rc "Claude plugin data still precedes the Codex fallback" 0
if [ -x "$codex_claude_data/venv/bin/fivee-sim-mcp" ] && \
    [ ! -e "$tmp/ignored-codex-home/plugin-data" ]; then
  report 0 "host plugin data precedes the Codex fallback" ""
else
  report 1 "host plugin data precedes the Codex fallback" \
    "Claude venv missing or Codex fallback unexpectedly created"
fi

# Cases that share a venv name run in sequence and each one's end state is the
# next one's precondition — that is the subject matter, not an accident: the
# whole bug is about a venv persisting across launches. The cost is that an early
# failure cascades, so read the FIRST failure in a block and ignore the rest.

# --- cold: nothing built yet ----------------------------------------------
launch v1 upgrade
want_rc     "cold build exits 0" 0
want_syncs  "cold build syncs once" 1
want_stdout "cold build runs the v1 engine" "engine=$V1/engine"

# --- warm: nothing changed ------------------------------------------------
# The whole reason for a stamp rather than an unconditional `uv sync`: the warm
# path must stay a single exec, with uv off the spawn path entirely.
launch v1 upgrade
want_rc     "warm start exits 0" 0
want_syncs  "warm start does not invoke uv" 0
want_stdout "warm start still runs the v1 engine" "engine=$V1/engine"

# A live lock would make a cold path wait. A warm start must neither inspect nor
# disturb it; the harness's ten-second wrapper turns accidental waiting into a
# quick, named failure instead of stalling this regression script for 300 seconds.
warm_lock="$tmp/venvs/upgrade.fivee-sim-build-lock"
mkdir "$warm_lock"
printf '%s:test-owner\n' "$$" > "$warm_lock/owner"
launch v1 upgrade
want_rc     "warm start bypasses a live build lock" 0
want_syncs  "warm start with a live lock still avoids uv" 0
if [ -f "$warm_lock/owner" ]; then
  report 0 "warm start does not mutate the lock path" ""
else
  report 1 "warm start does not mutate the lock path" "lock owner was removed"
fi
rm -rf "$warm_lock"

# --- upgrade: the reported bug --------------------------------------------
# Same durable venv, engine now at a different path. The old console script is
# still perfectly executable and its interpreter still resolves, so a
# usability check alone sees nothing wrong and serves the previous version.
launch v2 upgrade
want_rc     "after an upgrade the launcher exits 0" 0
want_syncs  "an upgrade re-syncs the venv" 1
want_stdout "after an upgrade the server runs the NEW engine" "engine=$V2/engine"

launch v2 upgrade
want_syncs  "the upgraded venv is warm on the next start" 0

# --- the engine changed in place ------------------------------------------
# A development checkout never moves, so the path cannot catch a dependency,
# entry-point, or requires-python change. The lock and manifest do.
printf 'version = 1\nrequires-python = ">=3.12"\n' > "$V2/engine/uv.lock"
launch v2 upgrade
want_rc     "a changed lock exits 0" 0
want_syncs  "a changed lock re-syncs" 1

printf '[project]\nname = "fivee-sim"\nversion = "0.0.1"\n' > "$V2/engine/pyproject.toml"
launch v2 upgrade
want_rc     "a changed pyproject exits 0" 0
want_syncs  "a changed pyproject re-syncs" 1

launch v2 upgrade
want_syncs  "and then goes warm again" 0

# --- a venv whose interpreter no longer resolves --------------------------
# Pre-existing behaviour, kept: a moved plugin directory leaves console scripts
# whose hard-coded shebang points at nothing.
V3="$(plant v3)"
launch v3 moved
want_rc "a fresh venv for the moved case" 0
printf '#!/nonexistent/python\necho engine=stale\n' > "$tmp/venvs/moved/bin/fivee-sim-mcp"
chmod +x "$tmp/venvs/moved/bin/fivee-sim-mcp"
launch v3 moved
want_rc     "an unusable venv is rebuilt, not run" 0
want_syncs  "an unusable venv re-syncs" 1
want_stdout "the rebuilt venv runs the engine" "engine=$V3/engine"

# --- a venv built before the stamp existed --------------------------------
# The state every existing install is in on its first start after this change:
# the venv is usable and its engine has not moved, but the launcher that built it
# left no stamp. This is the path that makes the fix self-healing, so it is
# pinned rather than assumed. Naming the stamp file is deliberate coupling — it
# is the one implementation detail that defines "built by the old launcher", and
# only the arrange block touches it; the assertions stay behavioural.
V5="$(plant v5)"
launch v5 premigration
want_rc "a fresh venv for the pre-stamp case" 0
rm -f "$tmp/venvs/premigration/.fivee-sim-build-stamp"
launch v5 premigration
want_rc     "a venv with no stamp exits 0" 0
want_syncs  "a venv with no stamp re-syncs" 1
want_stdout "and then runs the engine" "engine=$V5/engine"

launch v5 premigration
want_syncs  "and is warm on the start after that" 0

# --- concurrent cold starts ----------------------------------------------
# Both clients should launch, but only the lock owner may mutate the shared
# environment. A FIFO holds the first sync open until the second launcher has
# had a chance to contend for the lock, without depending on wall-clock speed.
CONCURRENT_ROOT="$(plant concurrent)"
concurrent_venv="$tmp/venvs/concurrent"
server_log="$tmp/server-launches"
concurrent_gate="$tmp/concurrent-uv-gate"
concurrent_ready="$tmp/concurrent-uv-ready"
mkfifo "$concurrent_gate"
: > "$uvlog"
: > "$server_log"
env PATH="$tmp/bin:$PATH" UV_LOG="$uvlog" SERVER_LOG="$server_log" \
  UV_PROJECT_ENVIRONMENT="$concurrent_venv" \
  FAKE_UV_GATE="$concurrent_gate" FAKE_UV_READY="$concurrent_ready" \
  timeout 10 bash "$CONCURRENT_ROOT/scripts/fivee-sim-mcp.sh" \
  >"$tmp/concurrent-1.out" 2>"$tmp/concurrent-1.err" &
concurrent_pid_1=$!
wait_for_path "$concurrent_ready"
env PATH="$tmp/bin:$PATH" UV_LOG="$uvlog" SERVER_LOG="$server_log" \
  UV_PROJECT_ENVIRONMENT="$concurrent_venv" \
  timeout 10 bash "$CONCURRENT_ROOT/scripts/fivee-sim-mcp.sh" \
  >"$tmp/concurrent-2.out" 2>"$tmp/concurrent-2.err" &
concurrent_pid_2=$!
concurrent_contended=0
if wait_for_text "$tmp/concurrent-2.err" \
    "another launcher is building the environment"; then
  concurrent_contended=1
fi
printf '\n' > "$concurrent_gate"
wait "$concurrent_pid_1"; concurrent_rc_1=$?
wait "$concurrent_pid_2"; concurrent_rc_2=$?
concurrent_syncs="$(grep -c . "$uvlog")"
concurrent_launches="$(grep -c . "$server_log")"
if [ "$concurrent_contended" -eq 1 ]; then
  report 0 "the concurrent case reaches lock contention" ""
else
  report 1 "the concurrent case reaches lock contention" \
    "second launcher did not report waiting for the build lock"
fi
if [ "$concurrent_rc_1" -eq 0 ] && [ "$concurrent_rc_2" -eq 0 ]; then
  report 0 "two concurrent cold starts both succeed" ""
else
  report 1 "two concurrent cold starts both succeed" \
    "rcs: $concurrent_rc_1, $concurrent_rc_2
stderr 1: $(cat "$tmp/concurrent-1.err")
stderr 2: $(cat "$tmp/concurrent-2.err")"
fi
if [ "$concurrent_syncs" -eq 1 ]; then
  report 0 "two concurrent cold starts perform one sync" ""
else
  report 1 "two concurrent cold starts perform one sync" \
    "uv invoked $concurrent_syncs time(s), want 1"
fi
if [ "$concurrent_launches" -eq 2 ]; then
  report 0 "two concurrent cold starts launch two servers" ""
else
  report 1 "two concurrent cold starts launch two servers" \
    "server launched $concurrent_launches time(s), want 2"
fi

# --- orphaned build lock -------------------------------------------------
ORPHAN_ROOT="$(plant orphan)"
orphan_venv="$tmp/venvs/orphan"
orphan_lock="$orphan_venv.fivee-sim-build-lock"
mkdir -p "$orphan_lock"
printf '99999999:orphan\n' > "$orphan_lock/owner"
launch orphan orphan
want_rc     "an orphaned build lock is reclaimed" 0
want_syncs  "recovery from an orphaned lock syncs once" 1
want_stdout "recovery from an orphaned lock launches the server" "engine=$ORPHAN_ROOT/engine"
if [ ! -e "$orphan_lock" ]; then
  report 0 "the reclaimed build lock is released" ""
else
  report 1 "the reclaimed build lock is released" "lock remains at $orphan_lock"
fi

# --- handled signal ------------------------------------------------------
# A launcher interrupted during sync must not strand the next session behind
# its lock. Bash defers the trap until the foreground child returns, so the FIFO
# lets the test signal the launcher while sync is active and then release the child.
SIGNAL_ROOT="$(plant signal)"
signal_venv="$tmp/venvs/signal"
signal_lock="$signal_venv.fivee-sim-build-lock"
signal_gate="$tmp/signal-uv-gate"
signal_ready_path="$tmp/signal-uv-ready"
mkfifo "$signal_gate"
: > "$uvlog"
env PATH="$tmp/bin:$PATH" UV_LOG="$uvlog" \
  UV_PROJECT_ENVIRONMENT="$signal_venv" \
  FAKE_UV_GATE="$signal_gate" FAKE_UV_READY="$signal_ready_path" \
  bash "$SIGNAL_ROOT/scripts/fivee-sim-mcp.sh" \
  >"$tmp/signal.out" 2>"$tmp/signal.err" &
signal_pid=$!
if wait_for_path "$signal_ready_path" && [ -f "$signal_lock/owner" ]; then
  kill -TERM "$signal_pid"
  printf '\n' > "$signal_gate"
  wait "$signal_pid"; signal_rc=$?
  if [ "$signal_rc" -eq 143 ]; then
    report 0 "a handled signal stops the launcher" ""
  else
    report 1 "a handled signal stops the launcher" "rc=$signal_rc, want 143"
  fi
  if [ ! -e "$signal_lock" ]; then
    report 0 "a handled signal releases the build lock" ""
  else
    report 1 "a handled signal releases the build lock" "lock remains at $signal_lock"
  fi
else
  kill -TERM "$signal_pid" 2>/dev/null || true
  wait "$signal_pid" 2>/dev/null || true
  report 1 "a handled signal stops the launcher" \
    "launcher did not enter the critical section"
  report 1 "a handled signal releases the build lock" \
    "signal cleanup could not be exercised"
fi

# --- stale, and the environment cannot be rebuilt -------------------------
# Running the wrong engine is the failure this whole file is about, so a
# launcher that cannot refresh must not fall back to serving it. Refusing to
# start puts one line on stderr; starting stale puts wrong answers in a fight.
V4="$(plant v4)"
launch v4 norebuild
want_rc "a fresh venv for the no-rebuild cases" 0

LAUNCH_PATH="$tmp/minbin" launch v2 norebuild
want_rc "stale with no uv on PATH does not start" 1
if [ -z "$out" ]; then report 0 "stale with no uv writes nothing to stdout" ""; else
  report 1 "stale with no uv writes nothing to stdout" "stdout: $out"
fi
case "$err" in
  *uv*) report 0 "stale with no uv says why on stderr" "" ;;
  *) report 1 "stale with no uv says why on stderr" "stderr: ${err:-<empty>}" ;;
esac

launch v2 norebuild FAKE_UV_FAIL=1
want_rc "stale with a failing sync does not start" 1
if [ -z "$out" ]; then report 0 "a failing sync writes nothing to stdout" ""; else
  report 1 "a failing sync writes nothing to stdout" "stdout: $out"
fi
if [ ! -e "$tmp/venvs/norebuild.fivee-sim-build-lock" ]; then
  report 0 "a failing sync releases the build lock" ""
else
  report 1 "a failing sync releases the build lock" \
    "lock remains after the failed sync"
fi

# --- warm with no uv anywhere ---------------------------------------------
# The property scripts/check-mcp-handshake.py relies on: once built, the server
# starts with uv removed from PATH entirely.
LAUNCH_PATH="$tmp/minbin" launch v4 norebuild
want_rc     "warm with no uv on PATH still starts" 0
want_stdout "warm with no uv on PATH runs the engine" "engine=$V4/engine"

# --- a plugin root with no engine -----------------------------------------
mkdir -p "$tmp/empty/scripts"
cp "$launcher" "$tmp/empty/scripts/fivee-sim-mcp.sh"
launch empty missing
want_rc "a plugin root with no engine does not start" 1

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
