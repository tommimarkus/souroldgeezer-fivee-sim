#!/usr/bin/env bash
# Tests for ip-hygiene-check.sh.
#
# Every case builds a throwaway project root in a temp directory and drives the
# hook exactly as Claude Code does — a PostToolUse payload on stdin, with
# CLAUDE_PROJECT_DIR pointing at that root. Nothing touches the real repo.
#
# The negative cases matter as much as the positive ones. A tripwire that fires
# on repo prose would reject CLAUDE.md, which has to quote the forbidden strings
# in order to document them; one that fires on SRD-present creature names would
# block legitimate rules data. Both are pinned below.
#
# Usage: bash scripts/hooks/test-ip-hygiene-check.sh

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"
hook="$here/ip-hygiene-check.sh"

pass=0
fail=0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# A representative project root: the real conf and a byte-exact NOTICE.
make_root() {
  local root="$tmp/$1"
  mkdir -p "$root/souroldgeezer-fivee-sim/.claude-plugin" \
           "$root/souroldgeezer-fivee-sim/.codex-plugin" \
           "$root/souroldgeezer-fivee-sim/skills/encounter-sim" \
           "$root/souroldgeezer-fivee-sim/agents" \
           "$root/souroldgeezer-fivee-sim/engine/src/fivee_sim/data"
  cp "$repo_root/.ip-hygiene-local.conf" "$root/"
  # Both declared copies: the repo-root one and the one inside the plugin, which
  # is what actually ships to installs.
  cp "$repo_root/NOTICE" "$root/NOTICE"
  cp "$repo_root/NOTICE" "$root/souroldgeezer-fivee-sim/NOTICE"
  printf '%s' "$root"
}

check() { # check <label> <expected_rc> <project_root> <file_path>
  local label="$1" want="$2" root="$3" path="$4" out rc
  out="$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s"}}' "$path" \
        | CLAUDE_PROJECT_DIR="$root" bash "$hook" 2>&1)"
  rc=$?
  if [ "$rc" -eq "$want" ]; then
    pass=$((pass + 1))
    printf '  PASS  rc=%s  %s\n' "$rc" "$label"
  else
    fail=$((fail + 1))
    printf '  FAIL  rc=%s (want %s)  %s\n' "$rc" "$want" "$label"
    [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/          | /'
  fi
}

R="$(make_root main)"
P="$R/souroldgeezer-fivee-sim"

# --- published branding metadata ------------------------------------------
printf '{"name":"souroldgeezer-fivee-sim","description":"Dungeons & Dragons 2024 simulation."}\n' \
  > "$P/.claude-plugin/plugin.json"
check "mark in plugin.json description" 2 "$R" "$P/.claude-plugin/plugin.json"

printf '{"name":"souroldgeezer-fivee-sim","description":"5E-compatible combat simulation."}\n' \
  > "$P/.claude-plugin/plugin.json"
check "clean plugin.json" 0 "$R" "$P/.claude-plugin/plugin.json"

printf '{"name":"souroldgeezer-fivee-sim","description":"clean","interface":{"displayName":"Dungeons & Dragons Sim","shortDescription":"clean","longDescription":"clean","capabilities":["clean"],"defaultPrompt":["clean"]}}\n' \
  > "$P/.codex-plugin/plugin.json"
check "mark in Codex display name" 2 "$R" "$P/.codex-plugin/plugin.json"

printf '{"name":"souroldgeezer-fivee-sim","description":"clean","interface":{"displayName":"5E Sim","shortDescription":"clean","longDescription":"clean","capabilities":["Run D&D encounters"],"defaultPrompt":["clean"]}}\n' \
  > "$P/.codex-plugin/plugin.json"
check "mark in Codex capability" 2 "$R" "$P/.codex-plugin/plugin.json"

printf '{"name":"souroldgeezer-fivee-sim","description":"clean","interface":{"displayName":"5E Sim","shortDescription":"clean","longDescription":"clean","capabilities":["clean"],"defaultPrompt":["Run a D&D encounter"]}}\n' \
  > "$P/.codex-plugin/plugin.json"
check "mark in Codex default prompt" 2 "$R" "$P/.codex-plugin/plugin.json"

printf '{"name":"souroldgeezer-fivee-sim","description":"5E-compatible simulation","interface":{"displayName":"5E Sim","shortDescription":"Seeded combat","longDescription":"Run reproducible encounters","capabilities":["Run seeded encounters"],"defaultPrompt":["Run a 5E-compatible encounter"]}}\n' \
  > "$P/.codex-plugin/plugin.json"
check "clean Codex public metadata" 0 "$R" "$P/.codex-plugin/plugin.json"

mkdir -p "$R/.claude-plugin"
printf '{"name":"t","plugins":[{"name":"x","description":"A D&D engine."}]}\n' \
  > "$R/.claude-plugin/marketplace.json"
check "mark in a marketplace plugin entry (root-level path)" 2 "$R" "$R/.claude-plugin/marketplace.json"

# The marketplace's own description is published branding too, not just the
# entries nested under it.
printf '{"name":"t","description":"Dungeons & Dragons tooling.","plugins":[{"name":"x","description":"clean"}]}\n' \
  > "$R/.claude-plugin/marketplace.json"
check "mark in the marketplace top-level description" 2 "$R" "$R/.claude-plugin/marketplace.json"

printf '{"name":"t","description":"5E-compatible tooling.","plugins":[{"name":"x","description":"clean"}]}\n' \
  > "$R/.claude-plugin/marketplace.json"
check "clean marketplace.json" 0 "$R" "$R/.claude-plugin/marketplace.json"

printf -- '---\nname: encounter-sim\ndescription: Use when running a D&D encounter.\n---\nBody.\n' \
  > "$P/skills/encounter-sim/SKILL.md"
check "mark in SKILL.md frontmatter" 2 "$R" "$P/skills/encounter-sim/SKILL.md"

printf -- '---\nname: encounter-sim\ndescription: Use for 5E-compatible encounters.\n---\nProse may say Dungeons & Dragons freely.\n' \
  > "$P/agents/encounter-sim.md"
check "clean frontmatter, mark only in body prose" 0 "$R" "$P/agents/encounter-sim.md"

# --- repo prose must never be scanned -------------------------------------
printf 'Never use "Dungeons & Dragons", "D&D", "DnD", "5.5e" or "Wizards of the Coast".\n' \
  > "$R/CLAUDE.md"
check "CLAUDE.md documenting the forbidden strings" 0 "$R" "$R/CLAUDE.md"
check "the conf file, which lists every pattern" 0 "$R" "$R/.ip-hygiene-local.conf"

# --- non-SRD names in engine data -----------------------------------------
D="$P/engine/src/fivee_sim/data"
printf '{"monsters":[{"name":"Beholder","provenance":"SRD 5.2.1"}]}\n' > "$D/bad.json"
check "engine data naming Beholder (absent from SRD 5.2.1)" 2 "$R" "$D/bad.json"

printf '{"monsters":[{"name":"Roper","provenance":"SRD 5.2.1"}]}\n' > "$D/ok.json"
check "engine data naming Roper (present in SRD 5.2.1)" 0 "$R" "$D/ok.json"

printf '{"lineages":[{"name":"Drow","provenance":"SRD 5.2.1"}]}\n' > "$D/lineage.json"
check "engine data naming Drow (a 2024 Elf lineage)" 0 "$R" "$D/lineage.json"

# --- attribution integrity ------------------------------------------------
T="$(make_root tampered)"
echo 'Includes material from the SRD 5.2.1 by Wizards of the Coast.' > "$T/NOTICE"
printf '{"monsters":[{"name":"Goblin"}]}\n' > "$T/souroldgeezer-fivee-sim/engine/src/fivee_sim/data/m.json"
check "reworded NOTICE, detected on a data edit" 2 "$T" "$T/souroldgeezer-fivee-sim/engine/src/fivee_sim/data/m.json"
check "reworded NOTICE, detected on editing NOTICE itself" 2 "$T" "$T/NOTICE"

M="$(make_root missing)"
rm -f "$M/NOTICE"
printf '{"monsters":[{"name":"Goblin"}]}\n' > "$M/souroldgeezer-fivee-sim/engine/src/fivee_sim/data/m.json"
check "missing repo-root NOTICE" 2 "$M" "$M/souroldgeezer-fivee-sim/engine/src/fivee_sim/data/m.json"

# The copy inside the plugin is the one that ships. A tripwire that only watched
# the repo-root copy would guard the file that never reaches an install.
S2="$(make_root shipped)"
rm -f "$S2/souroldgeezer-fivee-sim/NOTICE"
printf '{"monsters":[{"name":"Goblin"}]}\n' > "$S2/souroldgeezer-fivee-sim/engine/src/fivee_sim/data/m.json"
check "missing plugin NOTICE, root copy intact" 2 "$S2" "$S2/souroldgeezer-fivee-sim/engine/src/fivee_sim/data/m.json"

S3="$(make_root shipped_tampered)"
echo 'Material from SRD 5.2.1, Wizards of the Coast.' > "$S3/souroldgeezer-fivee-sim/NOTICE"
check "reworded plugin NOTICE, detected on editing it" 2 "$S3" "$S3/souroldgeezer-fivee-sim/NOTICE"

S4="$(make_root disclaimer_missing)"
sed -i '/^Section 5 of CC-BY-4.0 includes a Disclaimer of Warranties and Limitation of Liability that limits our liability to you\.$/d' "$S4/NOTICE"
check "missing source-supplied disclaimer notice" 2 "$S4" "$S4/NOTICE"

# --- worktree / nested-root resolution ------------------------------------
# Implementation happens in git worktrees under .worktrees/, which carry their
# own conf and their own NOTICE. The artifacts checked must be the ones being
# edited, not the primary checkout's.
nest() { # nest <outer_root> -> prints inner root
  local inner="$1/.worktrees/inner"
  mkdir -p "$inner/souroldgeezer-fivee-sim/engine/src/fivee_sim/data"
  cp "$repo_root/.ip-hygiene-local.conf" "$inner/"
  cp "$repo_root/NOTICE" "$inner/NOTICE"
  printf '{"monsters":[{"name":"Goblin"}]}\n' \
    > "$inner/souroldgeezer-fivee-sim/engine/src/fivee_sim/data/m.json"
  printf '%s' "$inner"
}

O="$(make_root outer)"
I="$(nest "$O")"
echo 'Material from SRD 5.2.1, Wizards of the Coast.' > "$I/souroldgeezer-fivee-sim/NOTICE"
check "nested root: the inner tampered NOTICE is what gets checked" 2 "$O" \
  "$I/souroldgeezer-fivee-sim/engine/src/fivee_sim/data/m.json"

O2="$(make_root outer_tampered)"
echo 'bogus attribution' > "$O2/NOTICE"
I2="$(nest "$O2")"
cp "$repo_root/NOTICE" "$I2/souroldgeezer-fivee-sim/NOTICE"
check "nested root: clean inner passes despite a tampered outer" 0 "$O2" \
  "$I2/souroldgeezer-fivee-sim/engine/src/fivee_sim/data/m.json"

# --- the conf is read, never executed --------------------------------------
# A conf carrying a shell command must not run it: the file is tracked in
# git, and $root is resolved from the edited file's own ancestry, so checking
# out an untrusted branch and editing any file under it must not run
# attacker-controlled shell on the next Edit.
sentinel="$tmp/pwned-marker"
rm -f "$sentinel"
J="$(make_root injection)"
printf '\ntouch "%s"\n$(touch "%s")\n`touch "%s"`\n' \
  "$sentinel" "$sentinel" "$sentinel" >> "$J/.ip-hygiene-local.conf"
printf '{"name":"x","description":"Dungeons & Dragons."}\n' \
  > "$J/souroldgeezer-fivee-sim/.claude-plugin/plugin.json"
check "conf carrying shell commands: hook still finds the real mark" 2 "$J" \
  "$J/souroldgeezer-fivee-sim/.claude-plugin/plugin.json"
if [ -e "$sentinel" ]; then
  fail=$((fail + 1))
  printf '  FAIL  conf carrying shell commands: a command executed (sentinel exists)\n'
else
  pass=$((pass + 1))
  printf '  PASS  conf carrying shell commands: no command executed\n'
fi

# --- activation guard -----------------------------------------------------
N="$tmp/nomarker"
mkdir -p "$N/souroldgeezer-fivee-sim/.claude-plugin"
printf '{"name":"x","description":"Dungeons & Dragons."}\n' > "$N/souroldgeezer-fivee-sim/.claude-plugin/plugin.json"
check "no marker file: inert even on a would-be finding" 0 "$N" "$N/souroldgeezer-fivee-sim/.claude-plugin/plugin.json"
check "path outside the project root" 0 "$R" "/etc/hostname"
check "payload with no file_path" 0 "$R" ""

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
