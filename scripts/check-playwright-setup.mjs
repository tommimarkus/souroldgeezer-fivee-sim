#!/usr/bin/env node

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const configPath = join(repoRoot, ".playwright", "cli.config.json");
const launcherPath = join(repoRoot, "scripts", "playwright-firefox.sh");
const dotGitPath = join(repoRoot, ".git");
let gitDir = dotGitPath;
if (statSync(dotGitPath).isFile()) {
  const pointer = readFileSync(dotGitPath, "utf8").trim();
  assert.match(pointer, /^gitdir: /);
  gitDir = resolve(repoRoot, pointer.slice("gitdir: ".length));
}
const commonDirPath = join(gitDir, "commondir");
const gitCommonDir = statSync(commonDirPath, { throwIfNoEntry: false })?.isFile()
  ? resolve(gitDir, readFileSync(commonDirPath, "utf8").trim())
  : gitDir;
const sharedCacheRoot = dirname(gitCommonDir);
const config = JSON.parse(readFileSync(configPath, "utf8"));

assert.equal(config.browser?.browserName, "firefox");
assert.equal(config.browser?.isolated, true);
assert.equal(config.browser?.launchOptions?.headless, true);
assert.deepEqual(config.browser?.launchOptions?.firefoxUserPrefs, {
  "app.normandy.api_url": "",
  "app.normandy.enabled": false,
  "app.shield.optoutstudies.enabled": false,
  "browser.crashReports.unsubmittedCheck.autoSubmit2": false,
  "browser.crashReports.unsubmittedCheck.enabled": false,
  "browser.discovery.enabled": false,
  "browser.newtabpage.activity-stream.asrouter.userprefs.cfr.addons": false,
  "browser.newtabpage.activity-stream.asrouter.userprefs.cfr.features": false,
  "browser.newtabpage.activity-stream.feeds.telemetry": false,
  "browser.newtabpage.activity-stream.showSponsored": false,
  "browser.newtabpage.activity-stream.showSponsoredTopSites": false,
  "browser.newtabpage.activity-stream.telemetry": false,
  "browser.newtabpage.activity-stream.telemetry.privatePing.enabled": false,
  "browser.ping-centre.telemetry": false,
  "browser.places.speculativeConnect.enabled": false,
  "browser.shopping.experience2023.enabled": false,
  "browser.pocket.enabled": false,
  "browser.tabs.crashReporting.sendReport": false,
  "browser.translations.enable": false,
  "browser.urlbar.quicksuggest.enabled": false,
  "browser.urlbar.speculativeConnect.enabled": false,
  "browser.urlbar.suggest.quicksuggest.all": false,
  "browser.urlbar.quicksuggest.dataCollection.enabled": false,
  "browser.urlbar.suggest.quicksuggest.sponsored": false,
  "datareporting.healthreport.uploadEnabled": false,
  "datareporting.policy.dataSubmissionEnabled": false,
  "datareporting.usage.uploadEnabled": false,
  "extensions.systemAddon.update.enabled": false,
  "network.captive-portal-service.enabled": false,
  "network.connectivity-service.enabled": false,
  "network.dns.disablePrefetch": true,
  "network.dns.disablePrefetchFromHTTPS": true,
  "network.http.speculative-parallel-limit": 0,
  "network.prefetch-next": false,
  "nimbus.rollouts.enabled": false,
  "nimbus.telemetry.targetingContextEnabled": false,
  "security.sandbox.content.level": 0,
  "security.sandbox.gmp.level": 0,
  "security.sandbox.gpu.level": 0,
  "security.sandbox.rdd.level": 0,
  "security.sandbox.socket.process.level": 0,
  "toolkit.telemetry.archive.enabled": false,
  "toolkit.telemetry.bhrPing.enabled": false,
  "toolkit.telemetry.enabled": false,
  "toolkit.telemetry.firstShutdownPing.enabled": false,
  "toolkit.telemetry.healthping.enabled": false,
  "toolkit.telemetry.newProfilePing.enabled": false,
  "toolkit.telemetry.reportingpolicy.firstRun": false,
  "toolkit.telemetry.server": "",
  "toolkit.telemetry.shutdownPingSender.enabled": false,
  "toolkit.telemetry.unified": false,
  "toolkit.telemetry.updatePing.enabled": false,
});
assert.equal(config.outputDir, ".cache/playwright/output");
assert.equal(config.outputMode, "stdout");
assert.equal(JSON.stringify(config).includes("chrom"), false);

const scratch = mkdtempSync(join(tmpdir(), "fivee-playwright-check-"));
const stubDir = join(scratch, "bin");
const capturePath = join(scratch, "capture.json");

try {
  spawnSync("mkdir", ["-p", stubDir], { stdio: "inherit" });
  writeFileSync(
    join(stubDir, "npx"),
    `#!/usr/bin/env node\n` +
      `const fs = require("node:fs");\n` +
      `fs.writeFileSync(process.env.PW_SETUP_CAPTURE, JSON.stringify({\n` +
      `  args: process.argv.slice(2),\n` +
      `  npmCache: process.env.npm_config_cache,\n` +
      `  xdgCache: process.env.XDG_CACHE_HOME,\n` +
      `  browserCache: process.env.PLAYWRIGHT_BROWSERS_PATH,\n` +
      `  noUpdateNotifier: process.env.NO_UPDATE_NOTIFIER,\n` +
      `  npmAudit: process.env.npm_config_audit,\n` +
      `  npmFund: process.env.npm_config_fund,\n` +
      `  npmOffline: process.env.npm_config_offline,\n` +
      `  npmPreferOffline: process.env.npm_config_prefer_offline,\n` +
      `  npmProgress: process.env.npm_config_progress,\n` +
      `  npmUpdateNotifier: process.env.npm_config_update_notifier,\n` +
      `  contentSandbox: process.env.MOZ_DISABLE_CONTENT_SANDBOX,\n` +
      `  gmpSandbox: process.env.MOZ_DISABLE_GMP_SANDBOX,\n` +
      `  rddSandbox: process.env.MOZ_DISABLE_RDD_SANDBOX,\n` +
      `  socketSandbox: process.env.MOZ_DISABLE_SOCKET_PROCESS_SANDBOX,\n` +
      `}));\n`,
    { mode: 0o755 },
  );

  const run = (...args) =>
    spawnSync("bash", [launcherPath, ...args], {
      cwd: repoRoot,
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${stubDir}:${process.env.PATH}`,
        PW_SETUP_CAPTURE: capturePath,
      },
    });

  let result = run("open", "about:blank");
  assert.equal(result.status, 0, result.stderr);
  let capture = JSON.parse(readFileSync(capturePath, "utf8"));
  assert.deepEqual(capture.args, [
    "--yes",
    "--package",
    "@playwright/cli",
    "playwright-cli",
    "--config",
    configPath,
    "open",
    "about:blank",
  ]);
  assert.equal(capture.npmCache, join(sharedCacheRoot, ".cache", "playwright", "npm"));
  assert.equal(capture.xdgCache, join(sharedCacheRoot, ".cache", "playwright", "xdg"));
  assert.equal(
    capture.browserCache,
    join(sharedCacheRoot, ".cache", "playwright", "browsers"),
  );
  assert.equal(capture.noUpdateNotifier, "1");
  assert.equal(capture.npmAudit, "false");
  assert.equal(capture.npmFund, "false");
  assert.equal(capture.npmOffline, "true");
  assert.equal(capture.npmPreferOffline, "true");
  assert.equal(capture.npmProgress, "false");
  assert.equal(capture.npmUpdateNotifier, "false");
  assert.equal(capture.contentSandbox, "1");
  assert.equal(capture.gmpSandbox, "1");
  assert.equal(capture.rddSandbox, "1");
  assert.equal(capture.socketSandbox, "1");

  result = run("--session", "sandbox-smoke", "snapshot");
  assert.equal(result.status, 0, result.stderr);
  capture = JSON.parse(readFileSync(capturePath, "utf8"));
  assert.deepEqual(capture.args, [
    "--yes",
    "--package",
    "@playwright/cli",
    "playwright-cli",
    "--session",
    "sandbox-smoke",
    "snapshot",
  ]);

  result = run("install-browser");
  assert.equal(result.status, 0, result.stderr);
  capture = JSON.parse(readFileSync(capturePath, "utf8"));
  assert.deepEqual(capture.args, [
    "--yes",
    "--package",
    "@playwright/cli",
    "playwright-cli",
    "install-browser",
    "firefox",
  ]);
  assert.equal(capture.npmOffline, undefined);
  assert.equal(capture.npmPreferOffline, "true");

  for (const forbiddenArgs of [
    ["open", "--browser=chromium"],
    ["open", "--browser", "webkit"],
    ["open", "--config", "elsewhere.json"],
    ["open", "--extension"],
    ["install-browser", "chromium"],
  ]) {
    result = run(...forbiddenArgs);
    assert.notEqual(result.status, 0, `accepted forbidden arguments: ${forbiddenArgs.join(" ")}`);
    assert.match(result.stderr, /Firefox-only/);
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}

console.log("Playwright setup is Firefox-only and keeps writable state in the workspace.");
