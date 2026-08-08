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
    """Command-driving guidance points at the packaged executable launcher.

    Direct engine skills locate it relative to the skill directory —
    ``../../scripts/fivee.py`` from ``skills/<name>/`` — which is this file.
    Play delegates launcher ownership to its scoped mechanics role instead.
    """
    launcher = PLUGIN_ROOT / LAUNCHER
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK), f"{LAUNCHER} must be executable"

    for skill in ("encounter-sim", "map-forge"):
        text = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "../../scripts/fivee.py" in text, skill
    assert "scripts/fivee.py" in _text("agents/play-mechanics.md")


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
        "fivee --run <run-id> adventure.replay <adv-id>",
        "nested verbatim, in order",
        "chapter record carries its mode",
        "fivee --run run-1 adventure.list",
        "Every chapter must be finalized first",
        "adventure.finalize",
        "not a precondition",
        "Nothing is re-derived",
        "Chapters freeze at encounter.finalize",
        "always a file, never inline",
        "replay.list will not list it",
        "no viewer_url comes back",
        "Chapter picker",
        "fivee --run run-1 replay.validate",
        "fivee --run run-1 replay.validate --json -",
    ):
        assert obligation in guidance_plain


def test_shipped_guidance_carries_the_distinct_run_selector_contract() -> None:
    """Scratch work and adventures return run ids; selecting never uses adventure ids."""
    encounter = _text("skills/encounter-sim/SKILL.md")
    map_forge = _text("skills/map-forge/SKILL.md")
    play = _text("skills/play/SKILL.md")
    table_loop = _text("skills/play/references/table-loop.md")
    for entry_skill in (encounter, map_forge):
        assert "fivee run.create" in entry_skill
        assert "fivee --run <run-id>" in entry_skill

    assert "fivee adventure.create --name" in play
    assert "opening scene" in play.lower()
    assert "opening chapter already exists" in play.lower()
    assert "roster schema v3" in play.lower()
    assert "run_id" in play
    assert "encounter_id" in play
    assert "run id" in table_loop.lower()
    assert "encounter.brief" in table_loop


def test_opening_adventure_contract_is_documented_across_guidance() -> None:
    guidance = "\n".join(
        (
            _text("skills/play/SKILL.md"),
            _text("skills/map-forge/SKILL.md"),
            _text("skills/encounter-sim/SKILL.md"),
            _text("docs/MAPS.md"),
            _text("../README.md"),
        )
    )
    plain = " ".join(guidance.lower().split())

    for obligation in (
        "opening scene",
        "map requirement",
        "team=party",
        "unique/unoccupied capacity",
        "request-order assignment",
        "ground then stored-level/feature order",
        "seed override only",
        "scene mode",
        "nonparty preservation",
        "old adv-* workspace",
    ):
        assert obligation in plain


def test_shipped_run_guidance_never_selects_an_old_adventure_workspace() -> None:
    for path in (
        "../README.md",
        "docs/MAPS.md",
        "skills/play/SKILL.md",
        "skills/map-forge/SKILL.md",
        "skills/map-forge/references/adventure-replay.md",
        "skills/encounter-sim/SKILL.md",
    ):
        guidance = _text(path)
        assert "--run adv-" not in guidance, path
        assert "runs/<adv-id>" not in guidance, path


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


def test_play_guidance_never_asks_a_seat_to_supply_dice() -> None:
    game_master = _text("agents/game-master.md")
    game_master_rolls = _markdown_section(
        game_master, "## Rolls, and who makes them"
    )
    mechanics = _markdown_section(
        _text("agents/play-mechanics.md"), "## Rolls, and who makes them"
    )
    player = _markdown_section(_text("agents/typical-player.md"), "## Rolling")
    human = _text("skills/play/references/human-seats.md")
    table_loop = _markdown_section(
        _text("skills/play/references/table-loop.md"), "## Reactions to dice"
    )
    report = _markdown_section(
        _text("skills/play/references/report-format.md"), "### Reproducibility"
    )
    playtest = _text("skills/play/references/playtest.md")
    guidance = "\n".join(
        (game_master, mechanics, player, human, table_loop, report, playtest)
    )

    assert "--natural" not in guidance
    for retired_instruction in (
        "roll their own dice",
        "roll your own dice",
        "human-reported faces",
        "reported faces",
        "A human saw the die",
        'answer "you roll it"',
        "A human supplies only requested",
        "A human seat may supply only",
        "Pass them unchanged",
    ):
        assert retired_instruction not in guidance

    assert re.search(
        r"engine.{0,100}rolls every die.{0,160}human.{0,160}agent",
        game_master_rolls,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"engine.{0,100}rolls every die.{0,160}human.{0,160}agent",
        mechanics,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(r"engine rolls every die for you", player, flags=re.IGNORECASE)
    assert re.search(
        r"do not ask.{0,100}human.{0,100}(?:roll|report).{0,200}engine",
        human,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"every.{0,80}human.{0,80}agent.{0,160}engine.{0,80}(?:face|natural)",
        table_loop,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for reproducibility in (report, playtest):
        assert re.search(
            r"master seed.{0,160}fixes every roll.{0,160}engine",
            reproducibility,
            flags=re.IGNORECASE | re.DOTALL,
        )


def test_play_skill_dispatches_the_shared_roles_for_each_host() -> None:
    skill = _text("skills/play/SKILL.md")
    dispatch = _markdown_section(skill, "## 2. Brief the seats")

    assert re.search(
        r"active host.{0,300}(?:exactly|only) one",
        dispatch,
        flags=re.IGNORECASE | re.DOTALL,
    )
    claude = _packaged_reference(
        "skills/play/SKILL.md", dispatch, "references/dispatch-claude-code.md"
    )
    assert "named agent" in claude.lower()
    assert "`game-master`" in claude
    assert "`typical-player`" in claude
    assert "gpt-5.6-terra" not in claude

    codex = _packaged_reference(
        "skills/play/SKILL.md", dispatch, "references/dispatch-codex.md"
    )
    assert "../../agents/game-master.md" in codex
    assert "../../agents/typical-player.md" in codex
    assert 'fork_turns="none"' in codex
    assert re.search(
        r"game-master.{0,300}fork_turns=\"none\".{0,300}\binherit",
        codex,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"typical-player.{0,300}fork_turns=\"none\""
        r".{0,300}model=\"gpt-5\.6-terra\""
        r".{0,300}reasoning_effort=\"medium\"",
        codex,
        flags=re.IGNORECASE | re.DOTALL,
    )
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
        r"tool\s+inventory.{0,300}\bbefore\b.{0,200}\b(?:scene|brief)\b",
        codex,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_play_skill_has_a_bounded_participant_scoped_party_council() -> None:
    skill = _text("skills/play/SKILL.md")
    beat_loop = _markdown_section(skill, "## 3. Supervise intervals")
    council = _markdown_section(
        _packaged_reference(
            "skills/play/SKILL.md", beat_loop, "references/table-loop.md"
        ),
        "## Party council",
    )

    for message_kind in ("`TABLE`", "`SAY`", "`COMMIT`"):
        assert message_kind in council
    assert re.search(
        r"(?:present|eligible).{0,300}\bcommunicat",
        council,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"own\s+(?:player\s+)?brief.{0,500}\b(?:never|do not)\b"
        r".{0,200}(?:another|other)\s+(?:player|seat).{0,100}\bbrief",
        council,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"one\s+proposal\s+pass.{0,500}one\s+(?:response|revision)\s+pass",
        council,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:material|plan-breaking).{0,200}\breopen",
        council,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"coordinator.{0,300}\brelay",
        council,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"plan.{0,300}\badvisory.{0,300}(?:acting|active)\s+seat.{0,300}\bCOMMIT",
        council,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_party_council_keeps_table_talk_out_of_world_state_and_gm_bulk_context() -> None:
    skill = _text("skills/play/SKILL.md")
    beat_loop = _markdown_section(skill, "## 3. Supervise intervals")
    council = _markdown_section(
        _packaged_reference(
            "skills/play/SKILL.md", beat_loop, "references/table-loop.md"
        ),
        "## Party council",
    )
    game_master = _markdown_section(_text("agents/game-master.md"), "## Party council")
    player = _markdown_section(_text("agents/typical-player.md"), "## Party council")

    assert re.search(
        r"`TABLE`.{0,300}(?:not|never).{0,200}(?:enemy|enemies|creature|world)",
        council,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"`SAY`.{0,300}(?:world|hear|audible|encounter)",
        council,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:raw|full).{0,200}(?:discussion|council|table talk).{0,300}"
        r"(?:not|never).{0,100}game.master",
        council,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:bounded|compact).{0,100}(?:summary|plan).{0,300}table.only",
        game_master,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"table.only.{0,300}(?:monster|enemy|NPC).{0,200}(?:does not|never|not)",
        game_master,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"`COMMIT`.{0,300}\bown\b.{0,200}(?:action|turn|declaration)",
        player,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_party_council_is_resumable_for_mixed_human_and_agent_seats() -> None:
    skill = _text("skills/play/SKILL.md")
    beat_loop = _markdown_section(skill, "## 3. Supervise intervals")
    council = _markdown_section(
        _packaged_reference(
            "skills/play/SKILL.md", beat_loop, "references/table-loop.md"
        ),
        "## Party council",
    )
    human = _packaged_reference(
        "skills/play/SKILL.md",
        _markdown_section(skill, "## 1. Seat the table"),
        "references/human-seats.md",
    )
    resume = _packaged_reference(
        "skills/play/SKILL.md",
        _markdown_section(skill, "## 6. Checkpoint, pause, and resume"),
        "references/resume.md",
    )
    seating = _packaged_reference(
        "skills/play/SKILL.md",
        _markdown_section(skill, "## 1. Seat the table"),
        "references/seating-and-pauses.md",
    )

    assert "council.json" in council
    for field in ("participants", "pass", "current_plan", "open_questions"):
        assert f'"{field}"' in seating
    assert re.search(
        r"user-input.{0,300}\bTABLE\b",
        human,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"council\.json.{0,400}\bparticipant",
        resume,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:not|never).{0,200}\bfull\s+transcript\b",
        resume,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_human_seats_receive_a_fresh_unrecorded_live_adventure_view() -> None:
    skill = _text("skills/play/SKILL.md")
    seating = _markdown_section(skill, "## 1. Seat the table")
    human = _packaged_reference(
        "skills/play/SKILL.md",
        seating,
        "references/human-seats.md",
    )
    resume = _packaged_reference(
        "skills/play/SKILL.md",
        _markdown_section(skill, "## 6. Checkpoint, pause, and resume"),
        "references/resume.md",
    )

    assert re.search(
        r"adventure.{0,200}link.{0,120}(?:first|initial).{0,120}encounter"
        r".{0,300}fivee --run <adv-id> serve",
        human,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert "viewer_url" in human
    assert (
        "?adventure=<URL-encoded adventure id>&as=<URL-encoded seat>#<launch token>"
        in human
    )
    assert re.search(r"(?:before|ahead of).{0,100}#", human, flags=re.IGNORECASE)
    assert re.search(
        r"URL.encode.{0,200}adventure.{0,200}URL.encode.{0,200}seat",
        human,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:only|solely).{0,120}(?:that|named).{0,80}seat",
        human,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for artifact in ("roster", "checkpoint", "transcript", "seat memory", "report"):
        assert re.search(
            rf"(?:never|do not).{{0,240}}token.{{0,240}}{artifact}"
            rf"|(?:never|do not).{{0,240}}{artifact}.{{0,240}}token",
            human,
            flags=re.IGNORECASE | re.DOTALL,
        ), artifact
        assert re.search(
            rf"(?:never|do not).{{0,240}}live URL.{{0,240}}{artifact}"
            rf"|(?:never|do not).{{0,240}}{artifact}.{{0,240}}live URL",
            human,
            flags=re.IGNORECASE | re.DOTALL,
        ), artifact
    assert re.search(r"(?:loopback|same.machine)", human, flags=re.IGNORECASE)
    assert re.search(
        r"player.safe.{0,80}projection",
        human,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:cooperating|cooperative).{0,200}(?:not|isn't|is not).{0,100}"
        r"(?:per.seat )?authentication",
        human,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"launch token.{0,240}(?:whole|full).{0,80}(?:local )?API.{0,240}writes",
        human,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:never|do not).{0,160}hand.{0,160}(?:untrusted|separate-trust)",
        human,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"fivee --run <adv-id> serve.{0,200}fresh.{0,200}viewer_url",
        resume,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"server replacement.{0,200}launch tokens change",
        resume,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"never restore.{0,120}live URL.{0,120}saved artifact",
        resume,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_player_tool_inventory_reports_weaker_boundaries_without_pausing() -> None:
    seating = _text("skills/play/references/seating-and-pauses.md")
    roster_example = re.search(r"```json\s+(?P<body>.*?)```", seating, flags=re.DOTALL)
    assert roster_example is not None
    assert '"tool_policy"' not in roster_example.group("body")
    assert '"tool_check"' in roster_example.group("body")

    policy = _markdown_section(seating, "## Player tool inventory")
    assert "Read (player-visible/** only)" in policy
    assert re.search(
        r"Claude Code.{0,300}Read \(player-visible/\*\* only\).{0,300}\bconfined",
        policy,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"Codex.{0,300}\breported tools\b.{0,300}\bhonour-system mode\b",
        policy,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert "`require-none`" not in policy
    assert "`allow-reported`" not in policy
    assert not re.search(r"\b(?:pause|stop) the run\b", policy, flags=re.IGNORECASE)
    assert re.search(
        r"continue.{0,100}\bwithout asking for approval\b",
        policy,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert "transcript.md" in policy
    assert "findings.jsonl" in policy
    assert re.search(
        r"re-ask.{0,200}\bre-spawn\b.{0,100}\bresume\b.{0,300}\brecord\b",
        policy,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_player_tool_inventory_makes_no_capability_claim() -> None:
    skill = _text("skills/play/SKILL.md")
    seating = _packaged_reference(
        "skills/play/SKILL.md",
        _markdown_section(skill, "## 1. Seat the table"),
        "references/seating-and-pauses.md",
    )
    guidance = skill + seating

    assert "`require-none`" not in guidance
    assert "`allow-reported`" not in guidance
    assert re.search(r"reported tools.{0,200}honour-system", guidance, flags=re.I | re.S)
    assert "Under `require-none`, agent seats hold no engine access" not in guidance
    assert "tools: []" not in guidance
    assert "Read (player-visible/**" in seating


def test_play_escalates_sandbox_launch_and_pauses_without_the_engine() -> None:
    skill = _text("skills/play/SKILL.md")
    failure = _markdown_section(skill, "### Failures at an unattended table")
    failure_plain = " ".join(failure.lower().split())

    assert re.search(
        r"\bsandbox(?:ed)?\b.{0,200}\b(?:escalat|outside the sandbox)",
        skill,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"\bengine\b.{0,160}\b(?:cannot|can't|unavailable|without)\b"
        r".{0,160}\bpause\b.{0,160}\b(?:user|owner)\b",
        failure_plain,
    )
    assert "even when unattended" in failure_plain
    assert "references/unattended-failures.md" in failure

    policy = _packaged_reference(
        "skills/play/SKILL.md", failure, "references/unattended-failures.md"
    )
    policy_plain = " ".join(policy.lower().split())
    for obligation in (
        "correct the call and retry",
        "game-master seat",
        "improvised ruling",
        "exact failure",
        "checkpoint",
        "pause",
        "user",
        "transcript.md",
        "findings.jsonl",
        "replay",
        "continue the beat loop",
    ):
        assert obligation in policy_plain
    assert re.search(
        r"\b(?:do not|never)\b.{0,120}\b(?:continue|improvise)\b.{0,160}\bwithout the engine\b",
        policy_plain,
    )

    game_master = _text("agents/game-master.md")
    degradation = _markdown_section(game_master, "## When engine support fails")
    degradation_plain = " ".join(degradation.lower().split())
    for obligation in (
        "exact failure",
        "coordinator",
        "pause",
        "user",
        "improvised ruling",
        "mechanical state",
        "replay",
        "without a roll",
    ):
        assert obligation in degradation_plain
    assert re.search(
        r"\b(?:do not|never)\b.{0,120}\b(?:continue|improvise)\b.{0,160}\bwithout the engine\b",
        degradation_plain,
    )

    report = _markdown_section(
        _text("skills/play/references/report-format.md"),
        "### Adjudication notes",
    ).lower()
    assert re.search(r"engine.{0,160}unavailable.{0,160}pause", report, flags=re.DOTALL)
    assert "off-engine" not in report


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
    assert re.search(
        r"load.{0,100}only.{0,100}playtest",
        test_only,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:do not|never) load.{0,200}(?:ordinary|plain) play",
        test_only,
        flags=re.IGNORECASE | re.DOTALL,
    )
    guidance = _packaged_reference(
        "skills/play/SKILL.md", test_only, "references/playtest.md"
    )
    for obligation in (
        "findings.jsonl",
        "report.md",
        "references/report-format.md",
        "fivee analytics.rounds",
    ):
        assert obligation in guidance


def test_play_skill_uses_one_brief_baseline_then_chair_scoped_deltas() -> None:
    skill = _text("skills/play/SKILL.md")
    beat_loop = _markdown_section(skill, "## 3. Supervise intervals")
    table_loop = _packaged_reference(
        "skills/play/SKILL.md", beat_loop, "references/table-loop.md"
    )
    delivery = _markdown_section(table_loop, "## Mechanical context and briefs")
    delivery_plain = " ".join(delivery.lower().split())

    for obligation in (
        "resettable mechanical context",
        "one full baseline",
        "per-seat delta",
        "exactly one mechanical-context invocation",
        "not one invocation per chair",
        "encounter.resume",
        "`delta` view value",
        'view` is `full',
    ):
        assert obligation in delivery_plain
    assert re.search(
        r"chair identity.{0,300}\bengine.owned.{0,200}\bredact",
        delivery,
        flags=re.I | re.S,
    )
    assert re.search(
        r"(?:never|do not).{0,200}(?:derive|project).{0,200}encounter\.state",
        delivery,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"one snapshot.{0,300}(?:one named chair|one chair)",
        delivery,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:never|do not).{0,200}(?:full brief|full baseline).{0,200}(?:pass|response)",
        delivery,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_brief_delivery_cursor_forces_safe_rebaseline_after_lost_ownership() -> None:
    skill = _text("skills/play/SKILL.md")
    table_loop = _packaged_reference(
        "skills/play/SKILL.md",
        _markdown_section(skill, "## 3. Supervise intervals"),
        "references/table-loop.md",
    )
    resume = _packaged_reference(
        "skills/play/SKILL.md",
        _markdown_section(skill, "## 6. Checkpoint, pause, and resume"),
        "references/resume.md",
    )
    guidance = " ".join((table_loop + resume).lower().split())

    assert "brief-cursors.json" in guidance
    assert "state_sha256" in guidance
    assert re.search(r"only after.{0,100}successful.{0,100}relay", guidance)
    assert re.search(r"(?:unknown|missing).{0,160}(?:ack|acknowledg)", guidance)
    assert re.search(
        r"re-spawn.{0,200}encounter\.brief.{0,160}chair.safe baseline",
        guidance,
    )
    assert re.search(
        r"(?:never|do not).{0,180}(?:delta|--view delta).{0,180}(?:re-baseline|baseline)",
        guidance,
    )
    assert re.search(r"recovery exception.{0,180}(?:pass|response|council)", guidance)


def test_player_council_returns_are_bounded_and_chronology_is_out_of_band() -> None:
    player = _markdown_section(_text("agents/typical-player.md"), "## Party council")
    player_plain = " ".join(player.lower().split())

    for field in ("`TABLE`", "`SAY`", "`GM QUESTION`", "`READY`"):
        assert field in player
    assert re.search(r"\b120\s+words\b", player_plain)
    assert re.search(r"SAY.{0,120}(?:optional|omit)", player, flags=re.I | re.S)
    assert re.search(r"(?:at most|only) one.{0,120}GM QUESTION", player, flags=re.I | re.S)
    assert re.search(r"COMMIT.{0,200}(?:separate|after the council)", player, flags=re.I | re.S)
    assert re.search(
        r"(?:never|do not).{0,200}(?:transcript|history|chronology)",
        player,
        flags=re.IGNORECASE | re.DOTALL,
    )

    skill = _text("skills/play/SKILL.md")
    table_loop = _packaged_reference(
        "skills/play/SKILL.md",
        _markdown_section(skill, "## 3. Supervise intervals"),
        "references/table-loop.md",
    )
    assert re.search(
        r"coordinator.{0,300}(?:append|write).{0,300}(?:chronology|transcript)"
        r".{0,300}(?:after|then).{0,300}(?:relay|discard|drop)",
        table_loop,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_live_run_checkpoints_bound_the_interval_controller_and_game_master() -> None:
    skill = _text("skills/play/SKILL.md")
    checkpoint = _markdown_section(skill, "## 6. Checkpoint, pause, and resume")
    game_master = _markdown_section(
        _text("agents/game-master.md"), "## Live checkpoint"
    )
    combined = " ".join((checkpoint + game_master).lower().split())

    assert re.search(r"encounter.{0,120}chapter.{0,120}boundar", combined)
    assert re.search(r"six.{0,160}resolved decision beats", combined)
    assert "checkpoint.json" in combined
    assert re.search(r"(?:800\s+tokens|token cap.{0,80}800)", combined)
    for field in (
        "objective",
        "run position",
        "material decisions",
        "blockers",
        "evidence pointers",
        "next action",
    ):
        assert field in combined
    assert re.search(r"authoritative.{0,120}(?:encounter|adventure).state", combined)
    assert re.search(r"(?:never|do not).{0,120}full transcript", combined)


def test_playtest_run_sheet_is_private_durable_checkpoint_evidence() -> None:
    skill = _text("skills/play/SKILL.md")
    playtest = _packaged_reference(
        "skills/play/SKILL.md",
        _markdown_section(skill, "## Playtest only"),
        "references/playtest.md",
    )
    checkpoint = _markdown_section(skill, "## 6. Checkpoint, pause, and resume")
    resume = _packaged_reference(
        "skills/play/SKILL.md", checkpoint, "references/resume.md"
    )
    guidance = " ".join((playtest + checkpoint + resume).lower().split())

    assert "run-sheet.json" in guidance
    assert re.search(r"private.{0,160}durable", guidance)
    assert re.search(r"run-sheet\.json.{0,200}(?:pointer|digest).{0,200}position", guidance)
    assert re.search(r"(?:never|do not).{0,160}player", guidance)
    assert re.search(
        r"fresh interval.{0,120}game-master spawn.{0,240}"
        r"(?:relevant|current).{0,160}(?:entry|entries)",
        guidance,
    )
    assert re.search(r"(?:do not|never).{0,200}(?:whole|full) run sheet", guidance)

    initial = _markdown_section(
        _text("agents/game-master.md"), "## Interval spawn"
    )
    assert re.search(r"every interval spawn", initial, flags=re.I)
    assert re.search(r"run.sheet pointer", initial, flags=re.I)
    assert re.search(
        r"fresh interval spawn.{0,240}(?:do not|never).{0,240}"
        r"(?:repeat|reread|re-emit)",
        initial,
        flags=re.I | re.S,
    )


def test_human_council_extensions_checkpoint_every_two_extra_passes() -> None:
    skill = _text("skills/play/SKILL.md")
    human = _packaged_reference(
        "skills/play/SKILL.md",
        _markdown_section(skill, "## 1. Seat the table"),
        "references/human-seats.md",
    )

    assert re.search(r"two.{0,160}(?:extra|extension).{0,160}pass", human, flags=re.I | re.S)
    assert re.search(
        r"checkpoint.{0,300}before.{0,300}(?:another|further)",
        human,
        flags=re.I | re.S,
    )
    assert "council.json" in human
    for field in ("current_plan", "open_questions", "ready"):
        assert field in human
    assert re.search(r"one pass at a time", human, flags=re.I)
    assert re.search(r"(?:never|do not).{0,160}raw discussion", human, flags=re.I | re.S)


def test_play_entry_stays_within_its_line_budget() -> None:
    skill = _text("skills/play/SKILL.md")
    body = re.split(r"^---\s*$", skill, maxsplit=2, flags=re.MULTILINE)[-1]

    assert len(body.splitlines()) <= 250
    assert "### Claude Code" not in skill
    assert "### Codex" not in skill
    assert "### Establish the test inventory" not in skill


def test_packaged_player_profile_has_only_the_inert_read_scope() -> None:
    player = _text("agents/typical-player.md")
    frontmatter = re.match(r"---\s*\n(?P<body>.*?)\n---", player, flags=re.DOTALL)

    assert frontmatter is not None
    metadata = frontmatter.group("body")
    tools = re.search(r"^tools:\s*(?P<value>.+)$", metadata, flags=re.MULTILINE)
    disallowed = re.search(
        r"^disallowedTools:\s*(?P<value>.+)$", metadata, flags=re.MULTILINE
    )
    model = re.search(r"^model:\s*(?P<value>.+)$", metadata, flags=re.MULTILINE)
    effort = re.search(r"^effort:\s*(?P<value>.+)$", metadata, flags=re.MULTILINE)

    assert tools is not None
    assert tools.group("value") == "Read(/${CLAUDE_PLUGIN_ROOT}/player-visible/**)"
    assert model is not None
    assert model.group("value") == "sonnet"
    assert effort is not None
    assert effort.group("value") == "medium"
    assert (PLUGIN_ROOT / "player-visible").is_dir()
    assert re.search(
        r"Claude Code.{0,300}\bRead\b.{0,300}\bother hosts\b"
        r".{0,300}\bfrontmatter\b",
        player[frontmatter.end() :],
        flags=re.IGNORECASE | re.DOTALL,
    )

    assert disallowed is not None
    assert {tool.strip() for tool in disallowed.group("value").split(",")} == {
        "Agent",
        "Artifact",
        "AskUserQuestion",
        "Bash",
        "CronCreate",
        "CronDelete",
        "CronList",
        "Edit",
        "EndConversation",
        "EnterPlanMode",
        "EnterWorktree",
        "ExitPlanMode",
        "ExitWorktree",
        "Glob",
        "Grep",
        "ListMcpResourcesTool",
        "LSP",
        "Monitor",
        "NotebookEdit",
        "PowerShell",
        "PushNotification",
        "ReadMcpResourceTool",
        "RemoteTrigger",
        "ReportFindings",
        "ScheduleWakeup",
        "SendMessage",
        "SendUserFile",
        "ShareOnboardingGuide",
        "Skill",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
        "TodoWrite",
        "ToolSearch",
        "WaitForMcpServers",
        "WebFetch",
        "WebSearch",
        "Workflow",
        "Write",
        "mcp__*",
    }


# The shared disallowed-tools universe pinned by `typical-player`'s own test,
# minus `Read` (which every profile here grants) and `Skill` and `Bash` (which
# only `encounter-sim` grants, scoped to the launcher).
_LAUNCHER_BASH_PATTERN = "Bash(python3 /${CLAUDE_PLUGIN_ROOT}/scripts/fivee.py:*)"

_LAUNCHER_PROFILE_DISALLOWED_TOOLS = {
    "Agent",
    "Artifact",
    "AskUserQuestion",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Edit",
    "EndConversation",
    "EnterPlanMode",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "ListMcpResourcesTool",
    "LSP",
    "Monitor",
    "NotebookEdit",
    "PowerShell",
    "PushNotification",
    "ReadMcpResourceTool",
    "RemoteTrigger",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "SendUserFile",
    "ShareOnboardingGuide",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "TodoWrite",
    "ToolSearch",
    "WaitForMcpServers",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
    "mcp__*",
}


def test_encounter_sim_profile_holds_only_the_launcher_bash_scope() -> None:
    agent = _text("agents/encounter-sim.md")
    frontmatter = re.match(r"---\s*\n(?P<body>.*?)\n---", agent, flags=re.DOTALL)

    assert frontmatter is not None
    metadata = frontmatter.group("body")
    tools = re.search(r"^tools:\s*(?P<value>.+)$", metadata, flags=re.MULTILINE)
    disallowed = re.search(
        r"^disallowedTools:\s*(?P<value>.+)$", metadata, flags=re.MULTILINE
    )

    assert tools is not None
    assert tools.group("value") == f"{_LAUNCHER_BASH_PATTERN}, Read, Skill"

    assert disallowed is not None
    assert {
        tool.strip() for tool in disallowed.group("value").split(",")
    } == _LAUNCHER_PROFILE_DISALLOWED_TOOLS

    assert re.search(
        r"Claude Code.{0,300}\bBash\b.{0,300}\bother hosts\b.{0,300}\bfrontmatter\b",
        agent[frontmatter.end() :],
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_game_master_profile_cannot_retain_engine_traffic() -> None:
    agent = _text("agents/game-master.md")
    frontmatter = re.match(r"---\s*\n(?P<body>.*?)\n---", agent, flags=re.DOTALL)

    assert frontmatter is not None
    metadata = frontmatter.group("body")
    tools = re.search(r"^tools:\s*(?P<value>.+)$", metadata, flags=re.MULTILINE)
    disallowed = re.search(
        r"^disallowedTools:\s*(?P<value>.+)$", metadata, flags=re.MULTILINE
    )

    assert tools is not None and tools.group("value") == "Read"
    assert disallowed is not None
    denied = {tool.strip() for tool in disallowed.group("value").split(",")}
    assert {"Bash", "Skill", "SendMessage", "mcp__*"} <= denied
    body = agent[frontmatter.end() :]
    assert "resettable mechanical context" in body
    assert re.search(
        r"never invoke.{0,160}(?:fivee|encounter skill|map skill)",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"never.{0,160}(?:retrieve|fan out|retain).{0,160}brief",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )


@pytest.mark.parametrize(
    "doc_path",
    [
        "agents/encounter-sim.md",
        "skills/encounter-sim/SKILL.md",
        "skills/map-forge/SKILL.md",
    ],
)
def test_command_guidance_names_only_the_absolute_launcher(doc_path: str) -> None:
    doc = _text(doc_path)
    assert "fivee.py" in doc
    assert "if it is on `PATH`" not in doc
    assert "already on `PATH`" not in doc
    assert "command -v fivee" not in doc


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

    skill = _text("skills/play/SKILL.md")
    protocol = _markdown_section(
        _packaged_reference(
            "skills/play/SKILL.md",
            _markdown_section(skill, "## 3. Supervise intervals"),
            "references/table-loop.md",
        ),
        "## Rules questions from a player",
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
        assert command not in lookup
    lookup_plain = " ".join(lookup.lower().split())
    assert re.search(
        r"game master.{0,180}(?:owns|chooses|forms).{0,180}(?:query|question)",
        lookup_plain,
    )
    assert re.search(r"mechanic.{0,180}(?:executes|runs).{0,180}(?:lookup|query)", lookup_plain)
    for evidence_field in ("provenance", "pages", "fact_status"):
        assert evidence_field in lookup_plain
    assert re.search(r"bounded.{0,160}(?:evidence|answer)", lookup_plain)
    assert re.search(
        r"model recollection.{0,200}\b(?:never|not|no)\b|"
        r"\b(?:never|not|no)\b.{0,200}model recollection",
        lookup,
        flags=re.IGNORECASE | re.DOTALL,
    )

    skill = _text("skills/play/SKILL.md")
    protocol = _markdown_section(
        _packaged_reference(
            "skills/play/SKILL.md",
            _markdown_section(skill, "## 3. Supervise intervals"),
            "references/table-loop.md",
        ),
        "## Rules questions from a player",
    )
    assert re.search(
        r"coordinator.{0,300}\brelay\w*\b.{0,300}\bgame master\b",
        protocol,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"game master.{0,300}\bowns\b.{0,160}\bquery\b.{0,160}\badjudication\b",
        protocol,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"mechanical context.{0,200}\bexecutes\b.{0,200}\bcatalog\.search\b",
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


@pytest.mark.parametrize(
    ("doc_path", "heading"),
    [
        ("agents/game-master.md", "## Whose decision is whose"),
        ("agents/encounter-sim.md", "## Why your Bash is scoped"),
    ],
)
def test_both_engine_facing_roles_keep_the_turn_order_the_dice_rolled(
    doc_path: str, heading: str
) -> None:
    """A fight names no actor, so nothing refuses a request aimed at the wrong seat.

    ``Encounter.act`` rejects an ``actor`` outside an interlude and resolves as
    ``self.current``, so a declaration invited from a seat that is not up is
    performed by whichever creature *is* up rather than refused. Neither role can
    lean on the engine for this, so both are held to reading the turn first.

    Scoped to the section that carries the rule rather than the whole file, and
    parametrized rather than looped, because both shortcuts cost this test its
    meaning: searching ``encounter-sim.md`` whole let rule 4's intact
    ``fivee encounter.state`` span satisfy an assertion written for rule 5, whose
    own span was line-wrapped and unmatchable — the control passed while the
    thing it was written to require was absent.
    """
    guidance = _markdown_section(_text(doc_path), heading)

    # The turn is read, never remembered.
    assert re.search(
        r"`fivee encounter\.state[^`]*`",
        guidance,
    )
    # Why the engine cannot catch a misdirected request: a fight takes no
    # actor, so an act carries no name to check.
    assert re.search(
        r"(?:refuses|rejects|carries no|names no)\b.{0,160}\bactor\b|"
        r"\bactor\b.{0,200}(?:refused|rejected|carries no name|no name)",
        guidance,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # And what happens instead — the creature that is up acts.
    assert re.search(
        r"(?:whoever|whichever)\b.{0,60}\bup\b|wrong (?:one|creature)",
        guidance,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # A seat that is not up is handed back, not played.
    assert re.search(
        r"(?:not up|is not (?:its|their) turn|another (?:seat|creature))"
        r".{0,400}(?:not adjudicated|refused|hold|hand)",
        guidance,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # The turn only moves on advance, so one turn can hold several acts.
    assert re.search(
        r"`fivee encounter\.advance[^`]*`|`encounter\.advance`",
        guidance,
    )
    # The interlude carve-out, so the rule is not over-applied.
    assert re.search(
        r"interlude.{0,300}no initiative.{0,300}(?:actor|any)",
        guidance,
        flags=re.IGNORECASE | re.DOTALL,
    )


# =============================================================================
# RED TESTS for Copilot CLI support (triple-harness plan)
# =============================================================================
# These tests define the contract for native Copilot CLI plugin support.
# All should fail before implementation.


def test_copilot_native_root_plugin_manifest_exists() -> None:
    """Copilot CLI requires a root plugin.json at the plugin root.

    The Copilot manifest is a first-class harness alongside Claude Code and Codex,
    not nested under a platform-specific directory like .claude-plugin/ or
    .codex-plugin/.
    """
    root_manifest_path = PLUGIN_ROOT / "plugin.json"
    assert root_manifest_path.is_file(), (
        f"Copilot CLI manifest missing at {root_manifest_path.relative_to(PLUGIN_ROOT)}"
    )


def test_copilot_native_root_manifest_has_required_fields() -> None:
    """Copilot root manifest must have name, version, and shared skills/agents."""
    manifest = _json("plugin.json")

    # Required: name field (string)
    assert "name" in manifest, "manifest missing 'name' field"
    assert isinstance(manifest["name"], str), "'name' must be a string"
    assert manifest["name"] == PLUGIN_ROOT.name, "name must match plugin root directory name"

    # Required: version field (strict-semver format YYYY.0M.build)
    assert "version" in manifest, "manifest missing 'version' field"
    assert isinstance(manifest["version"], str), "'version' must be a string"
    # Validate semver format YYYY.0M.build (e.g., 2026.8.122)
    assert re.match(r"^\d{4}\.\d{1,2}\.\d+$", manifest["version"]), (
        f"'version' must be in semver format (e.g., 2026.8.122), got: {manifest['version']}"
    )

    # Required: skills field (points to shared skills directory)
    assert "skills" in manifest or "agents" in manifest, (
        "manifest must have 'skills' or 'agents' field"
    )


def test_copilot_native_manifest_no_mcp_servers() -> None:
    """Copilot CLI manifest must not declare mcpServers.

    The engine is started by the command that needs it, never spawned by the host.
    """
    manifest = _json("plugin.json")
    assert "mcpServers" not in manifest, (
        "Copilot manifest must not declare mcpServers; engine is not spawned by host"
    )


def test_mcp_json_not_tracked_in_repository() -> None:
    """.mcp.json is host-specific config, never committed to the repository.

    Verify via git ls-files that .mcp.json is not in the tracked repository.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--", ".mcp.json"],
        cwd=PLUGIN_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked.returncode != 0:
        pytest.skip("git is not available")
    assert tracked.stdout.strip() == "", (
        f".mcp.json should not be tracked in git, but git ls-files shows: {tracked.stdout.strip()}"
    )


def test_shared_skills_directory_structure() -> None:
    """Copilot must use the same shared skills/ directory as Claude and Codex.

    Skills are cross-platform; only the manifest and frontmatter differ per host.
    """
    skills_path = PLUGIN_ROOT / "skills"
    assert skills_path.is_dir(), f"skills/ directory missing at {skills_path}"

    # Verify required skill subdirectories exist with SKILL.md files
    required_skills = ["play", "encounter-sim", "map-forge"]
    for skill_name in required_skills:
        skill_dir = skills_path / skill_name
        assert skill_dir.is_dir(), f"skills/{skill_name}/ not found"

        skill_md = skill_dir / "SKILL.md"
        assert skill_md.is_file(), f"skills/{skill_name}/SKILL.md not found"


def test_canonical_agent_files_exist() -> None:
    """Canonical role descriptions are the source of truth in agents/ directory.

    These canonical files must not be moved; they remain the single source
    for role definitions across all harnesses.
    """
    agents_path = PLUGIN_ROOT / "agents"
    assert agents_path.is_dir(), f"agents/ directory missing at {agents_path}"

    required_roles = [
        "adventure-prep.md",
        "encounter-sim.md",
        "game-master.md",
        "play-controller.md",
        "play-mechanics.md",
        "typical-player.md",
    ]

    for role_file in required_roles:
        role_path = agents_path / role_file
        assert role_path.is_file(), (
            f"Canonical role file missing: agents/{role_file}"
        )


def test_copilot_agents_directory_placeholder() -> None:
    """Copilot CLI will have generated agent profiles under copilot/agents/.

    This directory holds generated .agent.md files, one per canonical role.
    These are deterministic projections of the canonical roles for Copilot.
    """
    copilot_agents_path = PLUGIN_ROOT / "copilot" / "agents"
    # This test documents the expected structure; the directory may not exist yet.
    # On first implementation, copilot/agents/ will be created and populated.
    # For now, assert it will be a directory when created.
    if copilot_agents_path.exists():
        assert copilot_agents_path.is_dir(), (
            f"copilot/agents must be a directory, got: {copilot_agents_path}"
        )


def test_copilot_generated_profiles_per_role() -> None:
    """Each canonical role must have a generated Copilot .agent.md profile.

    Generated profiles are deterministic (hash-stable for unchanged canonical role)
    and preserve the canonical description and body verbatim.
    """
    required_roles = [
        "adventure-prep",
        "encounter-sim",
        "game-master",
        "play-controller",
        "play-mechanics",
        "typical-player",
    ]

    copilot_agents_path = PLUGIN_ROOT / "copilot" / "agents"

    for role_name in required_roles:
        agent_file = copilot_agents_path / f"{role_name}.agent.md"
        assert agent_file.is_file(), (
            f"Copilot generated profile missing: copilot/agents/{role_name}.agent.md"
        )


def test_copilot_generated_profile_preserves_canonical_body() -> None:
    """Copilot-generated profile must preserve canonical role description and body verbatim.

    The frontmatter differs per host (tools, model, effort), but the role's
    canonical description and body must not be modified.
    """
    canonical_role = PLUGIN_ROOT / "agents" / "game-master.md"
    copilot_profile = PLUGIN_ROOT / "copilot" / "agents" / "game-master.agent.md"

    assert canonical_role.is_file()
    assert copilot_profile.is_file(), (
        "Copilot profile for game-master not found"
    )

    canonical_text = canonical_role.read_text(encoding="utf-8")
    copilot_text = copilot_profile.read_text(encoding="utf-8")

    # Extract bodies (everything after frontmatter)
    canonical_body = re.split(r"^---\s*$", canonical_text, flags=re.MULTILINE)[
        -1
    ].strip()
    copilot_body = re.split(r"^---\s*$", copilot_text, flags=re.MULTILINE)[-1].strip()

    assert canonical_body, "canonical role body is empty"
    assert copilot_body, "Copilot profile body is empty"
    assert canonical_body == copilot_body, (
        "Copilot profile must preserve canonical role body verbatim"
    )


def test_copilot_frontmatter_no_disallowed_tools_field() -> None:
    """Copilot frontmatter must not have disallowedTools field.

    Copilot CLI uses a different tool allowlist model than Claude Code.
    """
    copilot_profile = PLUGIN_ROOT / "copilot" / "agents" / "game-master.agent.md"
    assert copilot_profile.is_file()

    text = copilot_profile.read_text(encoding="utf-8")
    frontmatter = re.split(r"^---\s*$", text, flags=re.MULTILINE)[1]

    assert "disallowedTools" not in frontmatter, (
        "Copilot frontmatter must not have disallowedTools; use inverted tool model"
    )


def test_copilot_frontmatter_no_effort_field() -> None:
    """Copilot frontmatter must not have effort field.

    Effort is Claude-specific reasoning guidance not applicable to Copilot.
    """
    copilot_profile = PLUGIN_ROOT / "copilot" / "agents" / "game-master.agent.md"
    assert copilot_profile.is_file()

    text = copilot_profile.read_text(encoding="utf-8")
    frontmatter = re.split(r"^---\s*$", text, flags=re.MULTILINE)[1]

    assert "effort" not in frontmatter, (
        "Copilot frontmatter must not have 'effort' field; this is Claude-specific"
    )


def test_copilot_frontmatter_no_model_field() -> None:
    """Copilot frontmatter may omit model field (agents inherit model from host).

    If model is present, it must not be a Claude-specific model name.
    """
    copilot_profile = PLUGIN_ROOT / "copilot" / "agents" / "game-master.agent.md"
    assert copilot_profile.is_file()

    text = copilot_profile.read_text(encoding="utf-8")
    frontmatter = re.split(r"^---\s*$", text, flags=re.MULTILINE)[1]

    # If model field exists, ensure it's not a Claude-specific model
    if "model:" in frontmatter:
        match = re.search(r"model:\s*(\S+)", frontmatter)
        assert match is not None
        model = match.group(1).strip()
        claude_models = ["opus", "sonnet", "haiku"]
        assert not any(
            m in model.lower() for m in claude_models
        ), f"Copilot profile must not use Claude model: {model}"


def test_copilot_frontmatter_uses_native_tool_names() -> None:
    """Copilot frontmatter must use native Copilot tool aliases.

    Valid Copilot tools: agent, execute, read, edit, search, web, todo.
    Must not use Claude-scoped tools like Read(path/**) or Bash(command:*).
    """
    copilot_profile = PLUGIN_ROOT / "copilot" / "agents" / "game-master.agent.md"
    assert copilot_profile.is_file()

    text = copilot_profile.read_text(encoding="utf-8")
    frontmatter = re.split(r"^---\s*$", text, flags=re.MULTILINE)[1]

    # Check for invalid Claude-scoped tool syntax
    invalid_patterns = [
        r"Read\([^)]*\*\*",  # Read(path/**)
        r"Bash\([^)]*\*",  # Bash(command:*)
        r"Write\([^)]*\*\*",  # Write(path/**)
    ]

    for pattern in invalid_patterns:
        assert not re.search(pattern, frontmatter), (
            f"Copilot frontmatter contains Claude-scoped tool syntax: {pattern}"
        )


def test_copilot_profile_no_claude_plugin_root_var() -> None:
    """Copilot profile must not reference ${CLAUDE_PLUGIN_ROOT}.

    This is a Claude Code environment variable; Copilot CLI has no such variable.
    """
    copilot_profile = PLUGIN_ROOT / "copilot" / "agents" / "game-master.agent.md"
    if copilot_profile.is_file():
        text = copilot_profile.read_text(encoding="utf-8")
        assert (
            "${CLAUDE_PLUGIN_ROOT}" not in text
        ), "Copilot profile must not reference ${CLAUDE_PLUGIN_ROOT}"


def test_manifest_metadata_parity_across_hosts() -> None:
    """Claude, Codex, and Copilot manifests must have matching name, description, author, license.

    This ensures a user sees consistent information regardless of which harness they use.
    """
    claude_manifest = _json(".claude-plugin/plugin.json")
    codex_manifest = _json(".codex-plugin/plugin.json")

    copilot_manifest_path = PLUGIN_ROOT / "plugin.json"
    if copilot_manifest_path.is_file():
        copilot_manifest = _json("plugin.json")

        # Name must match across all three
        assert claude_manifest["name"] == codex_manifest["name"]
        assert copilot_manifest["name"] == codex_manifest["name"], (
            f"Copilot name {copilot_manifest['name']!r} does not match "
            f"Codex {codex_manifest['name']!r}"
        )

        # Description must match (or be a minor variation) across all three
        claude_desc = claude_manifest.get("description", "")
        codex_desc = codex_manifest.get("description", "")
        copilot_desc = copilot_manifest.get("description", "")

        assert claude_desc == codex_desc, "Claude and Codex descriptions must match"
        assert copilot_desc == codex_desc, (
            f"Copilot description does not match Codex. "
            f"Copilot: {copilot_desc!r}\nCodex: {codex_desc!r}"
        )

        # Author should match if present
        if "author" in claude_manifest and "author" in copilot_manifest:
            assert claude_manifest["author"] == copilot_manifest["author"], (
                "Copilot author does not match Claude"
            )

        # License should match if present
        if "license" in claude_manifest and "license" in copilot_manifest:
            assert claude_manifest["license"] == copilot_manifest["license"], (
                "Copilot license does not match Claude"
            )


def test_manifest_versions_use_consistent_semver() -> None:
    """All three manifests must use compatible version formats.

    Claude uses CalVer (2026.08.122), Codex uses strict-semver (2026.8.122).
    Copilot must use strict-semver like Codex.
    """
    codex_manifest = _json(".codex-plugin/plugin.json")
    copilot_manifest_path = PLUGIN_ROOT / "plugin.json"

    if copilot_manifest_path.is_file():
        copilot_manifest = _json("plugin.json")

        # Copilot version must be strict-semver (YYYY.0M.build without leading zero on month)
        copilot_version = copilot_manifest.get("version", "")
        assert re.match(
            r"^\d{4}\.\d{1,2}\.\d+$", copilot_version
        ), f"Copilot version must be strict-semver format, got: {copilot_version}"

        # Extract numeric components and verify parity with Codex
        # (month padding differs: Codex 2026.8.122 vs Claude 2026.08.122)
        copilot_parts = copilot_version.split(".")
        codex_parts = codex_manifest["version"].split(".")

        assert len(copilot_parts) == 3, f"Copilot version must have 3 parts: {copilot_version}"
        assert len(codex_parts) == 3, f"Codex version must have 3 parts: {codex_manifest['version']}"

        # Year and build must match exactly
        assert (
            copilot_parts[0] == codex_parts[0]
        ), f"Copilot year {copilot_parts[0]} does not match Codex {codex_parts[0]}"
        assert (
            copilot_parts[2] == codex_parts[2]
        ), f"Copilot build {copilot_parts[2]} does not match Codex {codex_parts[2]}"

        # Month should match numerically (allowing for zero-padding difference)
        assert int(copilot_parts[1]) == int(codex_parts[1]), (
            f"Copilot month {copilot_parts[1]} does not match Codex month {codex_parts[1]}"
        )
