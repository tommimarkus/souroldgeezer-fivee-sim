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
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

LAUNCHER = "scripts/fivee.py"


def _json(relative_path: str) -> dict[str, Any]:
    payload = json.loads((PLUGIN_ROOT / relative_path).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _text(relative_path: str) -> str:
    return (PLUGIN_ROOT / relative_path).read_text(encoding="utf-8")


def _markdown_section(markdown: str, heading: str) -> str:
    """Return one section, stopping at the next heading at the same level."""
    level = len(heading) - len(heading.lstrip("#"))
    assert level and heading[level : level + 1] == " ", heading
    next_heading = rf"(?=^#{{1,{level}}}\s|\Z)"
    match = re.search(
        rf"^{re.escape(heading)}\s*$\n(?P<body>.*?){next_heading}",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing stable guidance section {heading!r}"
    return match.group("body")


def _packaged_reference(
    skill_relative_path: str, section: str, target: str
) -> str:
    """Return a Markdown leaf that an entry-skill section conditionally loads."""
    skill_path = PLUGIN_ROOT / skill_relative_path
    assert f"]({target})" in section, f"{skill_relative_path} does not point at {target}"
    reference = skill_path.parent / target
    assert reference.is_file(), f"packaged skill reference is missing: {reference}"
    return reference.read_text(encoding="utf-8")


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
    # Asked of git, not of the filesystem, and the difference is the whole
    # point. "Ships" means "is in the repository" — both hosts package the
    # plugin directory from the tree git tracks. The filesystem cannot answer
    # that here: the development devcontainer bind-mounts this path (see
    # CLAUDE.md "Environment hazards"), so a stale copy of the deleted file is
    # still readable in the primary checkout and cannot even be unlinked
    # (EBUSY), while a fresh worktree gets /dev/null over it instead. A test
    # that consulted the filesystem would therefore fail for the maintainer and
    # pass for everyone else, which is worse than not testing it.
    tracked = subprocess.run(
        ["git", "ls-files", "--", ".mcp.json"],
        cwd=PLUGIN_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked.returncode != 0:  # no git, or not a checkout: nothing to assert
        pytest.skip("git is not available to say what the repository tracks")
    assert tracked.stdout.strip() == "", (
        "the Codex MCP config is gone with the server it described, but git "
        f"still tracks: {tracked.stdout.strip()}"
    )


def test_the_launcher_the_skills_name_is_there_and_runnable() -> None:
    """Both skills tell the reader to fall back to this path, so it must exist.

    They locate it relative to the skill directory the harness announces —
    ``../../scripts/fivee.py`` from ``skills/<name>/`` — which is this file.
    A rename that missed the skills would leave that instruction pointing at
    nothing, and the reader would find out mid-fight.
    """
    launcher = PLUGIN_ROOT / LAUNCHER
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK), f"{LAUNCHER} must be executable"

    for skill in ("encounter-sim", "map-forge"):
        text = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "../../scripts/fivee.py" in text, skill


def test_encounter_analysis_guidance_is_conditionally_packaged() -> None:
    skill_path = "skills/encounter-sim/SKILL.md"
    section = _markdown_section(_text(skill_path), "## Analysis rather than play")

    assert re.search(
        r"\bDPR\b.{0,100}\bwin-rate\b.{0,100}\brepeated seeded analysis\b",
        section,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"\bdo not load\b.{0,100}\bturn-by-turn play\b",
        section,
        flags=re.IGNORECASE | re.DOTALL,
    )

    analysis = _packaged_reference(skill_path, section, "references/analysis.md")
    analysis_plain = " ".join(
        analysis.replace("*", "").replace("`", "").split()
    )
    for obligation in (
        "# Analysis rather than play",
        "fivee analytics.rounds",
        "fivee analytics.dpr",
        "seed + i",
        "highest expected damage this turn",
        "floor for a control build",
        "never operates a map fixture",
        "does not husband spell slots",
        "closes with Dash",
        "greedy, not tactical",
        "actions breakdown",
        "p10, median, p90",
    ):
        assert obligation in analysis_plain


def test_map_adventure_replay_guidance_is_conditionally_packaged() -> None:
    skill_path = "skills/map-forge/SKILL.md"
    section = _markdown_section(_text(skill_path), "## A whole adventure as one replay")

    assert re.search(
        r"\bcompose\b.{0,100}\bvalidate\b.{0,100}\badventure replay\b",
        section,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"\bdo not load\b.{0,100}\bsingle map\b.{0,100}\bsingle-encounter replay\b",
        section,
        flags=re.IGNORECASE | re.DOTALL,
    )

    guidance = _packaged_reference(
        skill_path, section, "references/adventure-replay.md"
    )
    guidance_plain = " ".join(
        guidance.replace("*", "").replace("`", "").split()
    )
    for obligation in (
        "# A whole adventure as one replay",
        "fivee adventure.replay <adv-id>",
        "nested verbatim, in order",
        "chapter record carries its mode",
        "fivee adventure.list",
        "Every chapter must be finalized first",
        "adventure.finalize",
        "not a precondition",
        "Nothing is re-derived",
        "Chapters freeze at encounter.finalize",
        "always a file, never inline",
        "replay.list will not list it",
        "no viewer_url comes back",
        "Chapter picker",
        "fivee replay.validate",
        "fivee replay.validate --json -",
    ):
        assert obligation in guidance_plain


def test_host_manifests_identify_the_same_plugin() -> None:
    claude = _json(".claude-plugin/plugin.json")
    codex = _json(".codex-plugin/plugin.json")

    for field in ("name", "description", "author", "license"):
        assert codex[field] == claude[field]


def test_playtest_skill_points_at_both_packaged_role_profiles() -> None:
    skill_path = PLUGIN_ROOT / "skills/playtest/SKILL.md"
    skill = skill_path.read_text(encoding="utf-8")
    targets = set(re.findall(r"\[[^]]+\]\(([^)]+)\)", skill))
    expected = {"../../agents/game-master.md", "../../agents/typical-player.md"}

    assert expected <= targets
    for target in expected:
        assert (skill_path.parent / target).is_file(), target


def test_playtest_skill_dispatches_the_shared_roles_for_each_host() -> None:
    skill = _text("skills/playtest/SKILL.md")

    claude = _markdown_section(skill, "### Claude Code")
    assert "named agent" in claude.lower()
    assert "`game-master`" in claude
    assert "`typical-player`" in claude

    codex = _markdown_section(skill, "### Codex")
    assert "../../agents/game-master.md" in codex
    assert "../../agents/typical-player.md" in codex
    assert 'fork_turns="none"' in codex
    assert "role body" in codex.lower()
    assert "prompt" in codex.lower()
    assert re.search(
        r"player prompt.{0,400}\bonly\b.{0,300}\bcharacter sheet\b"
        r".{0,200}\btemperament\b.{0,200}\bvoice\b",
        codex,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for withheld in ("adventure's path", "module text", "run sheet"):
        assert withheld in codex
    assert re.search(r"\bfull\s+transcript\b", codex)
    assert re.search(
        r"tool gate.{0,300}\bbefore\b.{0,200}\b(?:scene|brief)\b",
        codex,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_player_tool_policy_is_fail_closed_unless_explicitly_overridden() -> None:
    seating = _text("skills/playtest/references/seating-and-pauses.md")
    roster_example = re.search(r"```json\s+(?P<body>.*?)```", seating, flags=re.DOTALL)
    assert roster_example is not None
    assert '"tool_policy": "require-none"' in roster_example.group("body")

    policy = _markdown_section(seating, "## Player tool policy")
    assert "`require-none`" in policy
    assert re.search(r"`require-none`.{0,500}\b(?:pause|stop)\b", policy, flags=re.DOTALL)
    assert "`allow-reported`" in policy
    assert "explicit" in policy.lower()
    assert "record" in policy.lower()
    assert "roster.json" in policy
    assert "findings.jsonl" in policy
    assert re.search(
        r"re-ask.{0,200}\bre-spawn\b.{0,100}\bresume\b"
        r".{0,300}\bapply the gate again\b.{0,200}\bbefore\b",
        policy,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_require_none_is_a_gate_not_a_capability_claim() -> None:
    skill = _text("skills/playtest/SKILL.md")

    assert re.search(
        r"`require-none`.{0,100}\bdoes not\b.{0,100}\b(?:remove|disable)\b"
        r".{0,100}\btools\b",
        skill,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert "Under `require-none`, agent seats hold no engine access" not in skill


def test_packaged_player_profile_still_declares_no_tools() -> None:
    player = _text("agents/typical-player.md")
    frontmatter = re.match(r"---\s*\n(?P<body>.*?)\n---", player, flags=re.DOTALL)

    assert frontmatter is not None
    assert re.search(r"^tools:\s*\[\]\s*$", frontmatter.group("body"), flags=re.MULTILINE)
