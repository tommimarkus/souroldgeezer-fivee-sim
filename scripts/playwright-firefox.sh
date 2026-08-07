#!/usr/bin/env bash
set -euo pipefail

playwright_repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
playwright_config="$playwright_repo_root/.playwright/cli.config.json"
playwright_cache="$playwright_repo_root/.cache/playwright"

if ! command -v npx >/dev/null 2>&1; then
  echo "Firefox-only Playwright requires npx from Node.js/npm (an fnm runtime is fine)." >&2
  exit 1
fi

for playwright_arg in "$@"; do
  case "$playwright_arg" in
    --browser|--browser=*|--config|--config=*|--cdp|--cdp=*|--endpoint|--endpoint=*|\
      --executable-path|--executable-path=*|--extension|--extension=*)
      echo "Firefox-only Playwright does not allow browser or config overrides." >&2
      exit 2
      ;;
  esac
done

playwright_command=""
playwright_skip_value=false
for playwright_arg in "$@"; do
  if [[ "$playwright_skip_value" == true ]]; then
    playwright_skip_value=false
    continue
  fi
  case "$playwright_arg" in
    --session|-s)
      playwright_skip_value=true
      ;;
    --session=*|-s=*|--json|--raw)
      ;;
    -*)
      ;;
    *)
      playwright_command="$playwright_arg"
      break
      ;;
  esac
done

mkdir -p -- "$playwright_cache/npm" "$playwright_cache/xdg" "$playwright_cache/browsers"

export npm_config_cache="$playwright_cache/npm"
export XDG_CACHE_HOME="$playwright_cache/xdg"
export PLAYWRIGHT_BROWSERS_PATH="$playwright_cache/browsers"
export NO_UPDATE_NOTIFIER=1

# Firefox's inner process sandboxes cannot initialize inside the workspace
# sandbox. The JSON preferences cover the browser profile; these variables
# cover child processes that start before profile preferences are available.
export MOZ_DISABLE_CONTENT_SANDBOX=1
export MOZ_DISABLE_GMP_SANDBOX=1
export MOZ_DISABLE_RDD_SANDBOX=1
export MOZ_DISABLE_SOCKET_PROCESS_SANDBOX=1

playwright_npx=(npx --yes --package @playwright/cli playwright-cli)

if [[ "${1:-}" == "install-browser" ]]; then
  shift
  if [[ $# -gt 0 && "$1" != -* ]]; then
    if [[ "$1" != "firefox" ]]; then
      echo "Firefox-only Playwright refuses to install '$1'." >&2
      exit 2
    fi
    shift
  fi
  unset npm_config_offline
  exec "${playwright_npx[@]}" install-browser firefox "$@"
fi

export npm_config_offline=true
if [[ "$playwright_command" == "open" || "$playwright_command" == "attach" ]]; then
  exec "${playwright_npx[@]}" --config "$playwright_config" "$@"
fi
exec "${playwright_npx[@]}" "$@"
