# Playwright CLI

This repository runs Playwright CLI with Firefox only. Use the launcher from the
repository root so its configuration and workspace-local caches are applied:

```bash
scripts/playwright-firefox.sh install-browser
scripts/playwright-firefox.sh open http://127.0.0.1:8000/editor
scripts/playwright-firefox.sh snapshot
scripts/playwright-firefox.sh close
```

The launcher uses the `npx` supplied by the active Node.js runtime (including an
`fnm`-selected runtime). It does not add a `package.json` or install Chromium.
Browser binaries, npm downloads, session data, and artifacts stay under the
ignored `.cache/playwright/` directory so Playwright does not need to write to a
read-only home directory. Normal commands run npm in offline mode after
`install-browser` populates that cache, so the outer sandbox does not need
registry access for each browser action.

The Firefox process sandboxes are disabled because the development container's
outer sandbox does not permit them to initialize. This reduces browser process
isolation: use this launcher only for trusted local development pages and test
targets.

Playwright CLI also requires a Unix-domain socket for its background session.
The Codex outer sandbox denies every socket `listen` with `EPERM`, regardless of
whether the socket is under `/tmp` or the workspace. Run the launcher with the
host's sandbox-disabled approval; the narrow reusable command prefix is
`scripts/playwright-firefox.sh`. This is required for the CLI transport even
though its files and Firefox caches are workspace-local.

Run the setup check with:

```bash
node scripts/check-playwright-setup.mjs
```
