"""One release number wears four coats, and they must agree.

The source of truth is the ``version`` field of ``.claude-plugin/plugin.json``
(calver ``YYYY.0M.build``). Its mirrors are the README plugin table, the
engine's ``pyproject.toml``, and ``fivee_sim.__version__`` — the value every
MCP client sees in the ``initialize`` handshake's ``serverInfo``. PEP 440
strips the month's zero-padding in the Python pair, so agreement is checked
numerically, not textually.
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


def test_plugin_version_is_calver() -> None:
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d+", _plugin_version())


def test_package_version_matches_plugin() -> None:
    assert _numeric(fivee_sim.__version__) == _numeric(_plugin_version())


def test_pyproject_version_matches_plugin() -> None:
    pyproject = tomllib.loads(
        (PLUGIN_ROOT / "engine" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert _numeric(pyproject["project"]["version"]) == _numeric(_plugin_version())


def test_readme_mirror_matches_plugin() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"`souroldgeezer-fivee-sim`\s*\|\s*`(\d+(?:\.\d+)+)`", readme)
    assert match is not None, "README plugin table row not found"
    assert _numeric(match.group(1)) == _numeric(_plugin_version())
