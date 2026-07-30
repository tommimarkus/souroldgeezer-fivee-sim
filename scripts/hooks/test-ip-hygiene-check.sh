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
  mkdir -p "$root/fivee-sim/.claude-plugin" \
           "$root/fivee-sim/skills/encounter-sim" \
           "$root/fivee-sim/agents" \
           "$root/fivee-sim/engine/src/fivee_sim/data"
  cp "$repo_root/.ip-hygiene-local.conf" "$root/"
  cp "$repo_root/NOTICE" "$root/NOTICE"
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
P="$R/fivee-sim"

# --- published branding metadata ------------------------------------------
printf '{"name":"fivee-sim","description":"Dungeons & Dragons 2024 simulation."}\n' \
  > "$P/.claude-plugin/plugin.json"
check "mark in plugin.json description" 2 "$R" "$P/.claude-plugin/plugin.json"

printf '{"name":"fivee-sim","description":"5E-compatible combat simulation."}\n' \
  > "$P/.claude-plugin/plugin.json"
check "clean plugin.json" 0 "$R" "$P/.claude-plugin/plugin.json"

printf '{"name":"t","plugins":[{"name":"x","description":"A D&D engine."}]}\n' \
  > "$R/.claude-plugin-marketplace-tmp.json"
mkdir -p "$R/.claude-plugin"
mv "$R/.claude-plugin-marketplace-tmp.json" "$R/.claude-plugin/marketplace.json"
check "mark in a marketplace plugin entry (root-level path)" 2 "$R" "$R/.claude-plugin/marketplace.json"

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
printf '{"monsters":[{"name":"Beholder","provenance":"SRD 5.2"}]}\n' > "$D/bad.json"
check "engine data naming Beholder (absent from SRD 5.2)" 2 "$R" "$D/bad.json"

printf '{"monsters":[{"name":"Roper","provenance":"SRD 5.2"}]}\n' > "$D/ok.json"
check "engine data naming Roper (present in SRD 5.2)" 0 "$R" "$D/ok.json"

printf '{"lineages":[{"name":"Drow","provenance":"SRD 5.2"}]}\n' > "$D/lineage.json"
check "engine data naming Drow (a 2024 Elf lineage)" 0 "$R" "$D/lineage.json"

# --- attribution integrity ------------------------------------------------
T="$(make_root tampered)"
echo 'Includes material from the SRD 5.2 by Wizards of the Coast.' > "$T/NOTICE"
printf '{"monsters":[{"name":"Goblin"}]}\n' > "$T/fivee-sim/engine/src/fivee_sim/data/m.json"
check "reworded NOTICE, detected on a data edit" 2 "$T" "$T/fivee-sim/engine/src/fivee_sim/data/m.json"
check "reworded NOTICE, detected on editing NOTICE itself" 2 "$T" "$T/NOTICE"

M="$(make_root missing)"
rm -f "$M/NOTICE"
printf '{"monsters":[{"name":"Goblin"}]}\n' > "$M/fivee-sim/engine/src/fivee_sim/data/m.json"
check "missing NOTICE" 2 "$M" "$M/fivee-sim/engine/src/fivee_sim/data/m.json"

# --- activation guard -----------------------------------------------------
N="$tmp/nomarker"
mkdir -p "$N/fivee-sim/.claude-plugin"
printf '{"name":"x","description":"Dungeons & Dragons."}\n' > "$N/fivee-sim/.claude-plugin/plugin.json"
check "no marker file: inert even on a would-be finding" 0 "$N" "$N/fivee-sim/.claude-plugin/plugin.json"
check "path outside the project root" 0 "$R" "/etc/hostname"
check "payload with no file_path" 0 "$R" ""

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
