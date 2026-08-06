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


def test_benchmark_fixture_is_project_authored_without_external_source_claims() -> None:
    fixture_path = "engine/tests/benchmark-fixture-pack.json"
    benchmark_test_path = "engine/tests/test_benchmark_fixture.py"
    fixture = _json(fixture_path)

    assert fixture["provenance"] == (
        "Project-authored benchmark regression fixture using SRD 5.2.1 game "
        "statistics; see NOTICE."
    )
    shipped_surface = _text(fixture_path) + _text(benchmark_test_path)
    assert "battlecast" not in shipped_surface.casefold()
    assert "never shipped" not in shipped_surface.casefold()


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

    for skill in ("encounter-sim", "map-forge", "play"):
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


def test_play_skill_points_at_both_packaged_role_profiles() -> None:
    skill_path = PLUGIN_ROOT / "skills/play/SKILL.md"
    skill = skill_path.read_text(encoding="utf-8")
    targets = set(re.findall(r"\[[^]]+\]\(([^)]+)\)", skill))
    expected = {"../../agents/game-master.md", "../../agents/typical-player.md"}

    assert expected <= targets
    for target in expected:
        assert (skill_path.parent / target).is_file(), target


def test_play_skill_dispatches_the_shared_roles_for_each_host() -> None:
    skill = _text("skills/play/SKILL.md")

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
    seating = _text("skills/play/references/seating-and-pauses.md")
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
    skill = _text("skills/play/SKILL.md")

    assert re.search(
        r"`require-none`.{0,100}\bdoes not\b.{0,100}\b(?:remove|disable)\b"
        r".{0,100}\btools\b",
        skill,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert "Under `require-none`, agent seats hold no engine access" not in skill


def test_play_skill_makes_testing_an_explicit_optional_mode() -> None:
    skill = _text("skills/play/SKILL.md")
    frontmatter = re.match(r"---\s*\n(?P<body>.*?)\n---", skill, flags=re.DOTALL)

    assert frontmatter is not None
    metadata = frontmatter.group("body")
    assert re.search(r"^name:\s*play\s*$", metadata, flags=re.MULTILINE)
    assert "playing" in metadata.lower()
    assert "playtesting" in metadata.lower()

    mode = _markdown_section(skill, "## Choose the mode")
    assert re.search(r"\bdefault\b.{0,100}\bplay\b", mode, flags=re.IGNORECASE | re.DOTALL)
    assert re.search(
        r"\b(?:test|playtest)\b.{0,200}\bplaytest\b",
        mode,
        flags=re.IGNORECASE | re.DOTALL,
    )

    test_only = _markdown_section(skill, "## Playtest only")
    for obligation in (
        "findings.jsonl",
        "report.md",
        "references/report-format.md",
        "fivee analytics.rounds",
    ):
        assert obligation in test_only
    assert re.search(
        r"\bdo not\b.{0,200}\b(?:ordinary|plain) play\b",
        test_only,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_packaged_player_profile_still_declares_no_tools() -> None:
    player = _text("agents/typical-player.md")
    frontmatter = re.match(r"---\s*\n(?P<body>.*?)\n---", player, flags=re.DOTALL)

    assert frontmatter is not None
    assert re.search(r"^tools:\s*\[\]\s*$", frontmatter.group("body"), flags=re.MULTILINE)


def test_player_role_has_rules_literacy_and_a_bounded_reference_protocol() -> None:
    player = _text("agents/typical-player.md")
    framework = _markdown_section(player, "## Your rules framework")

    assert "2024" in framework
    assert "do not need to know rules text" not in player.lower()
    for decision_input in (
        "goal",
        "position",
        "action economy",
        "sheet",
        "resources",
        "risk",
        "temperament",
    ):
        assert decision_input in framework.lower()
    assert re.search(
        r"\bmove\b.{0,100}\bone action\b.{0,200}\bBonus Action\b.{0,200}\bReaction\b",
        framework,
        flags=re.DOTALL,
    )
    assert re.search(
        r"\bAttack\b.{0,300}\bDash\b.{0,200}\bDisengage\b.{0,200}\bDodge\b",
        framework,
        flags=re.DOTALL,
    )
    assert re.search(r"\bengine\b.{0,200}\b(?:arithmetic|outcome)s?\b", framework)

    reference = _markdown_section(player, "## Asking for a rule")
    assert re.search(
        r"\bask\b.{0,200}\bharness\b.{0,200}\bexact\b.{0,200}\bplayer-facing\b"
        r".{0,200}\bSRD\b",
        reference,
        flags=re.IGNORECASE | re.DOTALL,
    )
    reference_plain = " ".join(reference.lower().split())
    for withheld in ("adventure", "hidden state", "monster statistics"):
        assert withheld in reference_plain

    protocol = _markdown_section(
        _text("skills/play/SKILL.md"), "### Rules questions from a player"
    )
    for command in (
        "fivee catalog.search",
        "fivee catalog.get",
        "fivee catalog.table",
    ):
        assert command in protocol
    assert re.search(
        r"\bplayer-facing\b.{0,400}\bbefore\b.{0,200}\b(?:choose|decide)s?\b",
        protocol,
        flags=re.IGNORECASE | re.DOTALL,
    )
    protocol_plain = " ".join(protocol.lower().split())
    for withheld in ("adventure", "hidden state", "monster statistics"):
        assert withheld in protocol_plain


def test_game_master_owns_basic_rules_lookup_and_only_flags_material_gaps() -> None:
    game_master = _text("agents/game-master.md")

    framework = _markdown_section(game_master, "## Your rules framework")
    framework_plain = " ".join(framework.lower().split())
    for obligation in (
        "2024",
        "general rule",
        "specific rule",
        "d20 test",
        "ability check",
        "meaningful consequence",
        "action",
        "bonus action",
        "reaction",
        "attack",
        "dash",
        "disengage",
        "dodge",
        "influence",
        "search",
        "study",
        "utilize",
        "movement",
        "damage",
        "healing",
        "rest",
        "death",
        "character sheet",
    ):
        assert obligation in framework_plain

    lookup = _markdown_section(game_master, "## Looking up an SRD rule")
    for command in (
        "fivee catalog.search",
        "fivee catalog.get",
        "fivee catalog.table",
    ):
        assert command in lookup
    lookup_plain = " ".join(lookup.lower().split())
    for evidence_field in ("provenance", "pages", "fact_status"):
        assert evidence_field in lookup_plain
    assert "no_structured_facts" in lookup
    assert re.search(
        r"one search.{0,300}\b(?:silent|silence)\b",
        lookup,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"`rules\.lookup`.{0,400}\b(?:executable|engine)\b",
        lookup,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"model recollection.{0,200}\b(?:never|not|no)\b|"
        r"\b(?:never|not|no)\b.{0,200}model recollection",
        lookup,
        flags=re.IGNORECASE | re.DOTALL,
    )

    protocol = _markdown_section(
        _text("skills/play/SKILL.md"), "### Rules questions from a player"
    )
    assert re.search(
        r"coordinator.{0,300}\brelay\w*\b.{0,300}\bgame master\b",
        protocol,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"game master.{0,400}\b(?:owns?|performs?)\b.{0,300}\blookup\b",
        protocol,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"game master.{0,500}\bplayer-facing\b.{0,200}\banswer\b",
        protocol,
        flags=re.IGNORECASE | re.DOTALL,
    )

    adjudication = _markdown_section(game_master, "## Adjudicating")
    report = _markdown_section(
        _text("skills/play/references/report-format.md"),
        "### Adjudication notes",
    )
    divergence = _markdown_section(
        _text("skills/play/references/report-format.md"), "### Divergences"
    )
    finding_guidance = " ".join((adjudication + report + divergence).lower().split())
    assert re.search(
        r"(?:ordinary|normal) srd-supported action.{0,300}\b(?:not|isn't)\b"
        r".{0,200}\b(?:finding|adjudication note|divergence)\b",
        finding_guidance,
    )
    for material_gap in (
        "module-specific fact",
        "procedure",
        "dc",
        "consequence",
        "material route assumption",
        "engine limitation",
        "catalog limitation",
    ):
        assert material_gap in finding_guidance
    assert re.search(
        r"(?:record|put).{0,160}(?:engine|catalog) limitation.{0,160}adjudication note",
        adjudication,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:reserve|use).{0,100}divergence.{0,200}materially different"
        r".{0,100}(?:route|approach)",
        adjudication,
        flags=re.IGNORECASE | re.DOTALL,
    )
