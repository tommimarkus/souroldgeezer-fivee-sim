"""The Claude Code and Codex packages expose one shared plugin implementation."""

import json
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _json(relative_path: str) -> dict[str, Any]:
    payload = json.loads((PLUGIN_ROOT / relative_path).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_codex_manifest_points_at_the_shared_skill_and_mcp_config() -> None:
    manifest = _json(".codex-plugin/plugin.json")

    assert manifest["name"] == PLUGIN_ROOT.name
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert (PLUGIN_ROOT / manifest["skills"]).is_dir()
    assert (PLUGIN_ROOT / manifest["mcpServers"]).is_file()


def test_codex_mcp_config_launches_the_shared_server_from_the_plugin_root() -> None:
    config = _json(".mcp.json")
    server = config["mcpServers"]["fivee_sim"]

    assert server["command"] == "bash"
    assert server["args"] == ["./scripts/fivee-sim-mcp.sh"]
    assert server["cwd"] == "."
    assert server["env"] == {"FIVEE_SIM_PLUGIN_HOST": "codex"}
    assert server["startup_timeout_sec"] >= 300
    assert (PLUGIN_ROOT / server["args"][0]).is_file()


def test_host_manifests_identify_the_same_plugin() -> None:
    claude = _json(".claude-plugin/plugin.json")
    codex = _json(".codex-plugin/plugin.json")

    for field in ("name", "description", "author", "license"):
        assert codex[field] == claude[field]


def test_claude_mcp_lets_the_launcher_own_the_host_runtime_layout() -> None:
    manifest = _json(".claude-plugin/plugin.json")
    environment = manifest["mcpServers"]["fivee_sim"].get("env", {})

    assert "UV_PROJECT_ENVIRONMENT" not in environment
    assert "UV_CACHE_DIR" not in environment
