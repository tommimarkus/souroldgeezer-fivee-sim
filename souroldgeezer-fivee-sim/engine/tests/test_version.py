"""One release number wears six coats, and they must agree.

The source of truth is the ``version`` field of ``.claude-plugin/plugin.json``
(calver ``YYYY.0M.build``). Its mirrors are the strict-semver Codex manifest,
the README plugin table, the engine's ``pyproject.toml``, ``uv.lock``'s entry
for the workspace package itself, and ``fivee_sim.__version__`` — the value
every client sees in the ``server.ping`` handshake's ``serverInfo``. PEP 440 and
semver strip the month's zero-padding, so agreement is checked numerically, not
textually.
"""

import json
import re
import tomllib
from pathlib import Path

import fivee_sim

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PLUGIN_ROOT.parent


def _numeric(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _plugin_version() -> str:
    manifest = json.loads(
        (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    version = manifest["version"]
    assert isinstance(version, str)
    return version


def _codex_plugin_version() -> str:
    manifest = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    version = manifest["version"]
    assert isinstance(version, str)
    return version


def test_plugin_version_is_calver() -> None:
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d+", _plugin_version())


def test_codex_plugin_version_is_strict_semver() -> None:
    assert re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", _codex_plugin_version())


def test_codex_plugin_version_matches_release_source() -> None:
    assert _numeric(_codex_plugin_version()) == _numeric(_plugin_version())


def test_package_version_matches_plugin() -> None:
    assert _numeric(fivee_sim.__version__) == _numeric(_plugin_version())


def test_pyproject_version_matches_plugin() -> None:
    pyproject = tomllib.loads(
        (PLUGIN_ROOT / "engine" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert _numeric(pyproject["project"]["version"]) == _numeric(_plugin_version())


def test_lockfile_version_matches_plugin() -> None:
    """The sixth coat, and the one that kept slipping off.

    ``uv.lock`` records the workspace package's own version, so a bump that does
    not refresh it leaves the lock describing a release that no longer exists.
    Nothing regenerates it as part of bumping — only the next ``uv sync`` or
    ``uv run`` does, whenever that happens to be — so it went stale across two
    consecutive releases with nothing going red.
    """
    lock = tomllib.loads((PLUGIN_ROOT / "engine" / "uv.lock").read_text(encoding="utf-8"))
    entries = [package for package in lock["package"] if package["name"] == "fivee-sim"]
    assert len(entries) == 1, f"expected one fivee-sim entry in uv.lock, found {len(entries)}"
    assert _numeric(entries[0]["version"]) == _numeric(_plugin_version())


def test_readme_mirror_matches_plugin() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"`souroldgeezer-fivee-sim`\s*\|\s*`(\d+(?:\.\d+)+)`", readme)
    assert match is not None, "README plugin table row not found"
    assert _numeric(match.group(1)) == _numeric(_plugin_version())
