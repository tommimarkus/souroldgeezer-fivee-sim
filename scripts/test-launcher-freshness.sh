#!/usr/bin/env bash
# Tests for the venv freshness decision in fivee-sim-mcp.sh.
#
# The launcher straddles two directories with different lifetimes:
# ${CLAUDE_PLUGIN_ROOT} is replaced per version, while ${CLAUDE_PLUGIN_DATA} —
# where plugin.json puts the venv — is durable. Because `uv sync` installs the
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
printf '%s\n' "$*" >> "${UV_LOG:?}"
if [ "${FAKE_UV_FAIL:-0}" = "1" ]; then
  printf 'fake uv: refusing to sync\n' >&2
  exit 1
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
printf '#!/bin/sh\necho engine=%s\n' "$project" > "$UV_PROJECT_ENVIRONMENT/bin/fivee-sim-mcp"
chmod +x "$UV_PROJECT_ENVIRONMENT/bin/fivee-sim-mcp"
FAKE_UV
chmod +x "$tmp/bin/uv"

# A PATH with the launcher's utilities but no uv at all, for the two cases that
# pin what happens when the environment cannot be built. Trimming the real PATH
# would not do it: uv lives in /usr/bin here.
mkdir -p "$tmp/minbin"
for util in bash env dirname head sed cut rm timeout cksum; do
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
      bash "$root/scripts/fivee-sim-mcp.sh" 2>"$tmp/stderr")"
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

mkdir -p "$tmp/venvs"
V1="$(plant v1)"
V2="$(plant v2)"

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
