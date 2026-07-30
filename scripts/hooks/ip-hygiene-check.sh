#!/usr/bin/env bash
# ip-hygiene-check.sh — local-only PostToolUse tripwire for this project's IP
# boundary (SRD 5.2 / CC-BY-4.0). See CLAUDE.md "Local development hook".
#
# Contract: reads a PostToolUse payload on stdin, inspects the single file the
# tool just touched, and either exits 0 in silence or exits 2 with findings on
# stderr — which Claude Code feeds back so the problem is fixed in-loop.
#
# Scope discipline is the whole design here. The checks fire on *published
# branding metadata* and *engine data*, never on repo prose: CLAUDE.md, the
# README, and .ip-hygiene-local.conf all necessarily quote the forbidden
# strings in order to document or match them, and a whole-file grep would
# reject the very files that explain the rule.
#
# This is a mechanical tripwire, NOT a substitute for the
# souroldgeezer-audit:ip-hygiene skill, which remains the publish gate.
#
# Exit codes:
#   0  clean, or not applicable (wrong project, no marker, unparseable payload)
#   2  one or more findings, detail on stderr

set -uo pipefail

root="${CLAUDE_PROJECT_DIR:-$PWD}"
conf="$root/.ip-hygiene-local.conf"

# Activation marker. Also checked in the settings.json wiring, on purpose: that
# wiring is user-global, so without a guard there it would try to execute
# "$CLAUDE_PROJECT_DIR/scripts/hooks/..." in unrelated repositories.
[ -f "$conf" ] || exit 0

payload="$(cat)"
file_path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$file_path" ] || exit 0

case "$file_path" in
  /*) abs="$file_path" ;;
  *)  abs="$root/$file_path" ;;
esac

# Stay inside the project, and ignore deletes/moves that leave nothing to read.
case "$abs" in
  "$root"/*) ;;
  *) exit 0 ;;
esac
[ -f "$abs" ] || exit 0

rel="${abs#"$root"/}"

# shellcheck source=/dev/null
. "$conf"

findings=()

# Match a project-relative path against a list of globs. `case` globbing lets
# `*` span `/`, which is what we want for "*/agents/*.md".
matches_any() {
  local path="$1" glob
  shift
  for glob in "$@"; do
    # shellcheck disable=SC2254
    case "$path" in
      $glob) return 0 ;;
    esac
  done
  return 1
}

# Pull only the *branding* values out of a metadata file — never whole-file
# text. JSON: top-level name/description plus every marketplace plugin entry.
# Markdown: the name/description keys inside YAML frontmatter only.
extract_branding() {
  local f="$1"
  case "$f" in
    *.json)
      jq -r '
        [ .name?, .description?, (.plugins[]? | .name?, .description?) ]
        | map(select(type == "string")) | .[]
      ' "$f" 2>/dev/null
      ;;
    *.md)
      awk 'NR == 1 && $0 == "---" { inside = 1; next }
           inside && $0 == "---" { exit }
           inside' "$f" \
        | grep -iE '^[[:space:]]*(name|description)[[:space:]]*:'
      ;;
  esac
}

# --- Check 1: marks in published branding metadata -------------------------
if matches_any "$rel" "${IP_METADATA_GLOBS[@]}"; then
  branding="$(extract_branding "$abs")"
  if [ -n "$branding" ]; then
    for pattern in "${IP_MARK_PATTERNS[@]}"; do
      if printf '%s' "$branding" | grep -qiF -- "$pattern"; then
        findings+=("published branding metadata contains \"$pattern\" — use 5E-compatible wording instead")
      fi
    done
  fi
fi

# --- Check 2: attribution integrity ---------------------------------------
# Fires when rules data changes (the attribution must still be shipping) or
# when NOTICE itself is edited (it must not drift).
if matches_any "$rel" "${IP_DATA_GLOBS[@]}" || [ "$rel" = "$IP_ATTRIBUTION_FILE" ]; then
  notice="$root/$IP_ATTRIBUTION_FILE"
  if [ ! -f "$notice" ]; then
    findings+=("$IP_ATTRIBUTION_FILE is missing — CC-BY-4.0 requires the SRD 5.2 attribution to ship")
  elif ! grep -qF -- "$IP_ATTRIBUTION_STRING" "$notice"; then
    findings+=("$IP_ATTRIBUTION_FILE no longer contains the SRD 5.2 attribution byte-for-byte — it must not be reworded or re-wrapped")
  fi
fi

# --- Check 3: non-SRD names in engine data --------------------------------
if matches_any "$rel" "${IP_DATA_GLOBS[@]}"; then
  for name in "${IP_NON_SRD_NAMES[@]}"; do
    if grep -qiE "(^|[^[:alnum:]])${name}([^[:alnum:]]|\$)" "$abs"; then
      findings+=("rules data references \"$name\", which is not in SRD 5.2 and is therefore not licensed to us")
    fi
  done
fi

# --- Check 4: objective asset/vendored/schema candidates -------------------
# Delegated to the audit plugin's existing scanner rather than reimplemented.
# A missing sibling plugin is not an error; the check is simply skipped.
resolve_prefilter() {
  local candidate base version
  if [ -n "${IP_PREFILTER:-}" ] && [ -x "${IP_PREFILTER}" ]; then
    printf '%s' "$IP_PREFILTER"
    return 0
  fi
  candidate="${IP_PREFILTER_REPO_PATH:-}"
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    printf '%s' "$candidate"
    return 0
  fi
  base="$HOME/.claude/plugins/cache/souroldgeezer/souroldgeezer-audit"
  if [ -d "$base" ]; then
    for version in $(ls -1 "$base" 2>/dev/null | sort -Vr); do
      [ -f "$base/$version/.orphaned_at" ] && continue
      candidate="$base/$version/skills/ip-hygiene/references/scripts/ip-prefilter.sh"
      if [ -x "$candidate" ]; then
        printf '%s' "$candidate"
        return 0
      fi
    done
  fi
  return 1
}

if prefilter="$(resolve_prefilter)"; then
  prefilter_rc=0
  prefilter_out="$("$prefilter" --format text -- "$abs" 2>/dev/null)" || prefilter_rc=$?
  # rc 1 means candidates found; rc 2 is a usage error and is not our problem.
  if [ "$prefilter_rc" -eq 1 ] && [ -n "$prefilter_out" ]; then
    findings+=("bundled-asset/vendored/schema candidate — review with the ip-hygiene triage questions: ${prefilter_out//$'\n'/; }")
  fi
fi

# --- Report ----------------------------------------------------------------
if [ "${#findings[@]}" -gt 0 ]; then
  {
    printf 'ip-hygiene tripwire: %d finding(s) in %s\n' "${#findings[@]}" "$rel"
    printf '  - %s\n' "${findings[@]}"
    printf '\nThe boundary is documented in CLAUDE.md "Licence boundary".\n'
    printf 'Tuning lives in .ip-hygiene-local.conf.\n'
  } >&2
  exit 2
fi

exit 0
