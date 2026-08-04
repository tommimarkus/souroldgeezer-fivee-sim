"""The Claude Code and Codex packages expose one shared plugin implementation.

The plugin used to ship an MCP server, so these tests asked whether each host
manifest named it and whether the Codex ``.mcp.json`` launched it. There is no
server for a host to spawn any more: the engine is an HTTP service the ``fivee``
command starts on demand, the skills drive that command, and what a host has to
carry is the skills and the launcher script they reach for.

So what is pinned here is the shape that replaced it — no host spawns anything,
both manifests describe the same plugin, and the launcher the skills name is
present and executable.
"""

import json
import os
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

LAUNCHER = "scripts/fivee.sh"


def _json(relative_path: str) -> dict[str, Any]:
    payload = json.loads((PLUGIN_ROOT / relative_path).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_the_codex_manifest_points_at_the_shared_skills() -> None:
    manifest = _json(".codex-plugin/plugin.json")

    assert manifest["name"] == PLUGIN_ROOT.name
    assert manifest["skills"] == "./skills/"
    assert (PLUGIN_ROOT / manifest["skills"]).is_dir()


def test_neither_host_manifest_asks_to_spawn_a_server() -> None:
    """The engine is started by the command that needs it, not by the host.

    A leftover ``mcpServers`` key would have a host spawning a stdio server that
    no longer exists — a plugin that fails at load rather than at first use, and
    fails for every session rather than for the one that asked for a fight.
    """
    for manifest_path in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        assert "mcpServers" not in _json(manifest_path), manifest_path
    # ``is_file`` rather than ``exists``, and the difference is not pedantry.
    # The claim is that no MCP config *ships*; the development devcontainer
    # mounts /dev/null over this path — see CLAUDE.md "Environment hazards" —
    # and did so the moment the real file was deleted, so ``exists()`` is true
    # for a character device that carries nothing and cannot be committed.
    # ``is_file()`` is false for that device and true for anything shippable,
    # which is the property actually being pinned.
    manifest = PLUGIN_ROOT / ".mcp.json"
    assert not manifest.is_file(), (
        "the Codex MCP config is gone with the server it described; found a "
        f"real file at {manifest}"
    )


def test_the_launcher_the_skills_name_is_there_and_runnable() -> None:
    """Both skills tell the reader to fall back to this path, so it must exist.

    They locate it relative to the skill directory the harness announces —
    ``../../scripts/fivee.sh`` from ``skills/<name>/`` — which is this file.
    A rename that missed the skills would leave that instruction pointing at
    nothing, and the reader would find out mid-fight.
    """
    launcher = PLUGIN_ROOT / LAUNCHER
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK), f"{LAUNCHER} must be executable"

    for skill in ("encounter-sim", "map-forge"):
        text = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "../../scripts/fivee.sh" in text, skill


def test_host_manifests_identify_the_same_plugin() -> None:
    claude = _json(".claude-plugin/plugin.json")
    codex = _json(".codex-plugin/plugin.json")

    for field in ("name", "description", "author", "license"):
        assert codex[field] == claude[field]
