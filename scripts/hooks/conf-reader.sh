#!/usr/bin/env bash
# conf-reader.sh — parses a declarative NAME=value / NAME=(...) config file
# WITHOUT ever sourcing or executing it. Shared by ip-hygiene-check.sh and
# stop-audit-check.sh, whose conf files are tracked in git and therefore
# reachable by checking out an untrusted branch and editing any file under
# the resolved artifact root's ancestry — `. "$conf"` would run whatever
# shell that file contained as the developer. See CLAUDE.md "Local
# development hooks".
#
# Grammar accepted, and nothing else:
#
#   # a full-line comment                 (top level only)
#   NAME='a literal string'               (single-quoted scalar, no expansion)
#   NAME="a literal string"               (double-quoted scalar; $HOME is the
#                                          only substitution honoured, and it
#                                          is a literal text replacement, not
#                                          shell expansion)
#   NAME=(                                (array open)
#     "element"                           (double- or single-quoted, one per
#     'element'                           line)
#   )                                     (array close)
#
# Anything else — a bare command, an unquoted assignment, `` ` `` or `$(...)`
# command substitution, a multi-line string, anything on the same line as an
# array's `(` or `)` — is not part of this grammar and is silently ignored,
# never executed. A NAME the caller has not allowlisted is silently ignored
# too, whether scalar or array.
#
# The caller defines four functions before calling read_declarative_conf,
# each told the bare NAME so it can allow or reject it and, if allowed,
# assign it into its own namespace:
#   _conf_is_scalar NAME              — return 0 if NAME may be read as a scalar
#   _conf_is_array NAME               — return 0 if NAME may be read as an array
#   _conf_set_scalar NAME VALUE
#   _conf_set_array NAME [ELEMENT...]
#
# Usage: read_declarative_conf FILE

read_declarative_conf() {
  local file="$1" line trimmed name val
  local in_array="" array_name="" elements=()

  [ -f "$file" ] || return 0

  while IFS= read -r line || [ -n "$line" ]; do
    trimmed="${line#"${line%%[![:space:]]*}"}"

    if [ -n "$in_array" ]; then
      if [ "$trimmed" = ")" ]; then
        if _conf_is_array "$array_name"; then
          _conf_set_array "$array_name" "${elements[@]}"
        fi
        in_array=""
        array_name=""
        elements=()
        continue
      fi
      case "$trimmed" in
        '' | '#'*) continue ;;
      esac
      if [[ "$trimmed" =~ ^\"([^\"]*)\"$ ]]; then
        elements+=("${BASH_REMATCH[1]}")
      elif [[ "$trimmed" =~ ^\'([^\']*)\'$ ]]; then
        elements+=("${BASH_REMATCH[1]}")
      fi
      # Anything else inside an array block (an unquoted line, a stray
      # command) is not a recognised element and is dropped, not executed.
      continue
    fi

    case "$trimmed" in
      '' | '#'*) continue ;;
    esac

    if [[ "$trimmed" =~ ^([A-Za-z_][A-Za-z0-9_]*)=\($ ]]; then
      array_name="${BASH_REMATCH[1]}"
      in_array=1
      elements=()
      continue
    fi

    if [[ "$trimmed" =~ ^([A-Za-z_][A-Za-z0-9_]*)=\'([^\']*)\'$ ]]; then
      name="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      if _conf_is_scalar "$name"; then
        _conf_set_scalar "$name" "$val"
      fi
      continue
    fi

    if [[ "$trimmed" =~ ^([A-Za-z_][A-Za-z0-9_]*)=\"([^\"]*)\"$ ]]; then
      name="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      val="${val//\$HOME/$HOME}"
      if _conf_is_scalar "$name"; then
        _conf_set_scalar "$name" "$val"
      fi
      continue
    fi

    # A bare command, an unquoted assignment, backticks, $(...) — none of
    # this matches the grammar above, so it is ignored rather than executed.
  done <"$file"

  # An array left open at EOF (no closing paren) still hands back whatever
  # was parsed, rather than silently dropping it.
  if [ -n "$in_array" ] && _conf_is_array "$array_name"; then
    _conf_set_array "$array_name" "${elements[@]}"
  fi
}
