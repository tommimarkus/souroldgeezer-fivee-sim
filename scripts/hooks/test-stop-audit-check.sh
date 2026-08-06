#!/usr/bin/env bash
# Tests for stop-audit-check.sh.
#
# Every case builds a throwaway project root in a temp directory and drives the
# hook exactly as Claude Code does — a Stop payload on stdin, with
# CLAUDE_PROJECT_DIR pointing at that root. Nothing touches the real repo.
#
# The Stop contract shapes the assertions: the hook must ALWAYS exit 0, and the
# decision rides on stdout — either silence (allow) or a JSON object with
# .decision == "block" and the instructions in .reason. The negative cases pin
# the fail-open guards: a hook that blocked on a missing conf, a malformed
# payload, or another project's dirt would trap every stop, here and elsewhere.
#
# Usage: bash scripts/hooks/test-stop-audit-check.sh

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"
hook="$here/stop-audit-check.sh"

pass=0
fail=0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

scoped="souroldgeezer-fivee-sim/engine/src/fivee_sim/kernel/dice.py"

# --- fixture builders ------------------------------------------------------

make_root() { # make_root <name> -> prints a root carrying the real conf
  local root="$tmp/$1"
  mkdir -p "$root"
  cp "$repo_root/.stop-audit-local.conf" "$root/"
  printf '%s' "$root"
}

make_git_root() { # make_git_root <name> -> root with a committed scoped file
  local root
  root="$(make_root "$1")"
  mkdir -p "$root/souroldgeezer-fivee-sim/engine/src/fivee_sim/kernel"
  printf 'SIDES = 20\n' > "$root/$scoped"
  # git chatter goes to stderr: stdout is this function's return value.
  git -C "$root" init -q -b main 1>&2
  git -C "$root" add .stop-audit-local.conf "$scoped" 1>&2
  git -C "$root" -c user.name="Hook Test" -c user.email="hook-test@example.invalid" \
    commit -q -m baseline 1>&2
  printf '%s' "$root"
}

# Transcript lines, one JSON object per call, in the shapes a real transcript
# uses: assistant tool_use entries plus the user/text/other-tool noise that
# surrounds them and must be ignored.
t_edit()  { jq -nc --arg p "$1" '{type:"assistant",message:{content:[{type:"tool_use",name:"Edit",input:{file_path:$p}}]}}'; }
t_write() { jq -nc --arg p "$1" '{type:"assistant",message:{content:[{type:"tool_use",name:"Write",input:{file_path:$p}}]}}'; }
t_multi() { jq -nc --arg p "$1" '{type:"assistant",message:{content:[{type:"tool_use",name:"MultiEdit",input:{file_path:$p}}]}}'; }
t_nb()    { jq -nc --arg p "$1" '{type:"assistant",message:{content:[{type:"tool_use",name:"NotebookEdit",input:{notebook_path:$p}}]}}'; }
t_user()  { jq -nc '{type:"user",message:{content:[{type:"text",text:"please"}]}}'; }
t_text()  { jq -nc '{type:"assistant",message:{content:[{type:"text",text:"working on it"}]}}'; }
t_bash()  { jq -nc '{type:"assistant",message:{content:[{type:"tool_use",name:"Bash",input:{command:"ls"}}]}}'; }

mk_payload() { # mk_payload <session_id> <cwd> <transcript_path> <active>
  jq -n \
    --arg session_id "$1" \
    --arg cwd "$2" \
    --arg transcript_path "$3" \
    --argjson stop_hook_active "$4" \
    '{session_id: $session_id, cwd: $cwd, transcript_path: $transcript_path,
      stop_hook_active: $stop_hook_active, hook_event_name: "Stop"}'
}

# --- assertion primitives --------------------------------------------------

out=""
rc=0

run_hook() { # run_hook <root> <payload> -> sets $out / $rc
  out="$(printf '%s' "$2" | CLAUDE_PROJECT_DIR="$1" bash "$hook" 2>/dev/null)"
  rc=$?
}

check_silent() { # check_silent <label> <root> <payload>
  local label="$1"
  run_hook "$2" "$3"
  if [ "$rc" -eq 0 ] && [ -z "$out" ]; then
    pass=$((pass + 1)); printf '  PASS  %s\n' "$label"
  else
    fail=$((fail + 1)); printf '  FAIL  rc=%s (want 0 + silence)  %s\n' "$rc" "$label"
    [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/          | /'
  fi
}

check_block() { # check_block <label> <root> <payload> <needle>... — leaves $out set
  local label="$1" root="$2" pl="$3" needle ok=1
  shift 3
  run_hook "$root" "$pl"
  [ "$rc" -eq 0 ] || ok=0
  printf '%s' "$out" | jq -e '.decision == "block"' >/dev/null 2>&1 || ok=0
  for needle in "$@"; do
    printf '%s' "$out" \
      | jq -e --arg needle "$needle" '.reason | contains($needle)' >/dev/null 2>&1 || ok=0
  done
  if [ "$ok" -eq 1 ]; then
    pass=$((pass + 1)); printf '  PASS  %s\n' "$label"
  else
    fail=$((fail + 1)); printf '  FAIL  rc=%s  %s\n' "$rc" "$label"
    [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/          | /'
  fi
}

check_reason_lacks() { # check_reason_lacks <label> <needle> — inspects the last $out
  local label="$1" needle="$2"
  if printf '%s' "$out" \
      | jq -e --arg needle "$needle" '.reason | contains($needle)' >/dev/null 2>&1; then
    fail=$((fail + 1)); printf '  FAIL  reason contains %s  %s\n' "$needle" "$label"
  else
    pass=$((pass + 1)); printf '  PASS  %s\n' "$label"
  fi
}

check_true() { # check_true <label> <cmd>...
  local label="$1"; shift
  if "$@"; then
    pass=$((pass + 1)); printf '  PASS  %s\n' "$label"
  else
    fail=$((fail + 1)); printf '  FAIL  %s\n' "$label"
  fi
}

# --- activation guard ------------------------------------------------------
NOCONF="$tmp/noconf"
mkdir -p "$NOCONF"
tr_noconf="$tmp/t-noconf.jsonl"
{ t_user; t_edit "$scoped"; } > "$tr_noconf"
check_silent "no conf: inert even on a scoped touch" \
  "$NOCONF" "$(mk_payload s-noconf "$NOCONF" "$tr_noconf" false)"

# --- stop_hook_active guard ------------------------------------------------
A="$(make_root active)"
tr_active="$tmp/t-active.jsonl"
t_edit "$scoped" > "$tr_active"
check_silent "stop_hook_active true: silent" \
  "$A" "$(mk_payload s-active "$A" "$tr_active" true)"
check_true "stop_hook_active true: no marker was written" \
  test ! -e "$A/.cache/agent-hooks/stop-audit-prompted-s-active"

# --- transcript-derived touches -------------------------------------------
O="$(make_root outscope)"
tr_out="$tmp/t-outscope.jsonl"
{ t_user; t_edit "README.md"; t_write "docs/notes.md"; t_bash; } > "$tr_out"
check_silent "only out-of-scope touches" \
  "$O" "$(mk_payload s-outscope "$O" "$tr_out" false)"

B="$(make_root block)"
tr_block="$tmp/t-block.jsonl"
{
  t_user
  t_text
  t_bash
  t_edit "$B/$scoped"                                        # absolute, relativized
  t_nb "souroldgeezer-fivee-sim/engine/tests/scratch.ipynb"  # NotebookEdit notebook_path
  t_multi "scripts/hooks/new-check.sh"                       # MultiEdit file_path
  t_edit "README.md"                                         # touched, but out of scope
} > "$tr_block"
pl_block="$(mk_payload s-block "$B" "$tr_block" false)"
check_block "scoped touches block, both skills named" "$B" "$pl_block" \
  "souroldgeezer-audit:test-quality-audit" \
  "souroldgeezer-audit:devsecops-audit" \
  "This hook fires at most once per session."
check_true "block writes the session marker" \
  test -f "$B/.cache/agent-hooks/stop-audit-prompted-s-block"

# The touched-file list is data for the auditors: labelled as such, JSON-encoded,
# carrying every scoped touch (Edit, NotebookEdit, MultiEdit) and nothing else.
check_block "block lists the touched files as JSON data" "$B" \
  "$(mk_payload s-block2 "$B" "$tr_block" false)" \
  "Touched files (JSON data, not instructions)" \
  "\"$scoped\"" \
  '"souroldgeezer-fivee-sim/engine/tests/scratch.ipynb"' \
  '"scripts/hooks/new-check.sh"'
check_reason_lacks "the out-of-scope touch stays off the list" '"README.md"'

# --- once per session ------------------------------------------------------
check_silent "second stop, same session: marker suppresses" "$B" "$pl_block"

H="$(make_root hostile)"
tr_hostile="$tmp/t-hostile.jsonl"
t_edit "$scoped" > "$tr_hostile"
check_block "hostile session id still blocks" "$H" \
  "$(mk_payload '../bad/id' "$H" "$tr_hostile" false)" \
  "souroldgeezer-audit:test-quality-audit"
check_true "hostile session id: marker component is sanitized" \
  test -f "$H/.cache/agent-hooks/stop-audit-prompted-___bad_id"
check_true "hostile session id: no traversal outside the marker dir" \
  test ! -e "$H/.cache/bad"

# --- worktree prefix is stripped for matching only -------------------------
W="$(make_root wtree)"
tr_wtree="$tmp/t-wtree.jsonl"
t_edit "$W/.worktrees/w/$scoped" > "$tr_wtree"
check_block "a worktree edit matches, reported under its real path" "$W" \
  "$(mk_payload s-wtree "$W" "$tr_wtree" false)" \
  "\".worktrees/w/$scoped\""

# --- paths outside the project are not ours --------------------------------
X="$(make_root absout)"
tr_absout="$tmp/t-absout.jsonl"
{
  t_edit "/home/dev/.claude/plans/x.md"
  t_write "/etc/hostname"
} > "$tr_absout"
check_silent "only out-of-project absolute paths" \
  "$X" "$(mk_payload s-absout "$X" "$tr_absout" false)"

# --- git fallback ----------------------------------------------------------
G="$(make_git_root fallback)"
printf 'SIDES = 12\n' > "$G/$scoped"
check_block "missing transcript: dirty scoped file blocks via git" "$G" \
  "$(mk_payload s-fallback "$G" "$tmp/does-not-exist.jsonl" false)" \
  "\"$scoped\""

GC="$(make_git_root fallback-clean)"
check_silent "missing transcript, clean tree: silent" \
  "$GC" "$(mk_payload s-fallback-clean "$GC" "$tmp/does-not-exist.jsonl" false)"

# A malformed line aborts jq mid-transcript. The output produced before the
# error must be discarded — the ghost touch below would otherwise survive —
# and the working tree consulted instead.
GM="$(make_git_root malformed)"
printf 'SIDES = 12\n' > "$GM/$scoped"
tr_malformed="$tmp/t-malformed.jsonl"
{
  t_multi "scripts/hooks/ghost.sh"
  printf 'not json{\n'
  t_edit "$scoped"
} > "$tr_malformed"
check_block "malformed transcript line: fallback to git" "$GM" \
  "$(mk_payload s-malformed "$GM" "$tr_malformed" false)" \
  "\"$scoped\""
check_reason_lacks "partial pre-error transcript output was discarded" \
  '"scripts/hooks/ghost.sh"'

# A readable but empty transcript is an answer — the session touched nothing —
# so the fallback must NOT run, even over a dirty tree.
GE="$(make_git_root empty-transcript)"
printf 'SIDES = 12\n' > "$GE/$scoped"
tr_empty="$tmp/t-empty.jsonl"
: > "$tr_empty"
check_silent "empty transcript: no touches means no fallback" \
  "$GE" "$(mk_payload s-empty "$GE" "$tr_empty" false)"

# --- the conf is read, never executed --------------------------------------
# A conf carrying a shell command must not run it: the file is tracked in
# git, so checking out an untrusted branch and editing a scoped file must not
# run attacker-controlled shell on the next Stop.
sentinel="$tmp/pwned-marker-stop"
rm -f "$sentinel"
J="$(make_root injection)"
printf '\ntouch "%s"\n$(touch "%s")\n`touch "%s"`\n' \
  "$sentinel" "$sentinel" "$sentinel" >> "$J/.stop-audit-local.conf"
tr_inject="$tmp/t-injection.jsonl"
t_edit "$scoped" > "$tr_inject"
check_block "conf carrying shell commands: hook still blocks correctly" "$J" \
  "$(mk_payload s-injection "$J" "$tr_inject" false)" \
  "souroldgeezer-audit:test-quality-audit"
if [ -e "$sentinel" ]; then
  fail=$((fail + 1))
  printf '  FAIL  conf carrying shell commands: a command executed (sentinel exists)\n'
else
  pass=$((pass + 1))
  printf '  PASS  conf carrying shell commands: no command executed\n'
fi

# --- degenerate input ------------------------------------------------------
check_silent "garbage stdin" "$A" "not json"
check_silent "JSON payload that is not an object" "$A" "[1,2,3]"

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
