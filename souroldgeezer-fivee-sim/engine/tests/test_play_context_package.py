"""Package contracts that keep live-play role context bounded."""

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _text(relative_path: str) -> str:
    return (PLUGIN_ROOT / relative_path).read_text(encoding="utf-8")


def _section(markdown: str, heading: str) -> str:
    level = len(heading) - len(heading.lstrip("#"))
    next_heading = rf"(?=^#{{1,{level}}}\s|\Z)"
    match = re.search(
        rf"^{re.escape(heading)}\s*$\n(?P<body>.*?){next_heading}",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing stable guidance section {heading!r}"
    return match.group("body")


def test_play_prepares_a_private_module_index_before_spawning_the_game_master() -> None:
    skill = _text("skills/play/SKILL.md")
    briefing = _section(skill, "## 2. Brief the seats")

    assert "references/module-prep.md" in briefing
    prep = _text("skills/play/references/module-prep.md")
    guidance = " ".join(prep.lower().split())

    assert "module-index.json" in guidance
    assert "module-index.json.partial" in guidance
    assert re.search(r"ordinary play.{0,240}structur", guidance)
    assert re.search(r"ordinary play.{0,500}(?:does not|never).{0,120}(?:gap|omission)", guidance)
    assert re.search(r"playtest.{0,300}(?:semantic|full).{0,200}inventory", guidance)
    assert re.search(r"(?:20 entries|twenty entries).{0,160}1,200", guidance)
    assert re.search(r"complete manifest.{0,240}(?:publish|rename)", guidance)
    assert re.search(r"unreadable|incomplete", guidance)
    assert re.search(r"one bounded correction.{0,160}blocked", guidance)


def test_module_index_contract_is_stable_and_run_sheet_references_it() -> None:
    prep = _text("skills/play/references/module-prep.md")
    for field in (
        "schema_version",
        "source_path",
        "source_sha256",
        "source_format",
        "entries",
        "id",
        "kind",
        "title",
        "locator",
        "related_ids",
    ):
        assert field in prep
    assert re.search(r"source[- ]order", prep, flags=re.IGNORECASE)
    assert re.search(r"stable.{0,100}(?:id|identifier)", prep, flags=re.I | re.S)
    assert re.search(r"(?:line|page).{0,100}locator", prep, flags=re.I | re.S)

    playtest = _text("skills/play/references/playtest.md")
    assert re.search(
        r"run-sheet\.json.{0,400}module-index.{0,200}(?:id|entry)",
        playtest,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_checkpoint_rehydrates_only_current_index_entries_and_rejects_source_drift() -> None:
    skill = _text("skills/play/SKILL.md")
    checkpoint = _section(skill, "## 6. Checkpoint, pause, and resume")
    resume = _text("skills/play/references/resume.md")
    guidance = " ".join((checkpoint + resume).lower().split())

    assert re.search(
        r"module-index\.json.{0,240}(?:pointer|path).{0,160}digest.{0,160}current",
        guidance,
    )
    assert re.search(r"current.{0,160}(?:ids|entries).{0,160}(?:line|page).{0,80}locator", guidance)
    assert re.search(r"source.{0,80}hash.{0,240}mismatch", guidance)
    assert re.search(r"(?:refuse|do not).{0,160}mix", guidance)
    assert re.search(r"restart.{0,120}resume.{0,120}decision", guidance)


def test_live_play_routes_one_beat_to_play_mechanics_not_encounter_sim() -> None:
    table_loop = _text("skills/play/references/table-loop.md")
    mechanical = _section(table_loop, "## Mechanical context and briefs")

    assert "`play-mechanics`" in mechanical
    assert re.search(r"one decision beat", mechanical, flags=re.IGNORECASE)
    assert re.search(
        r"(?:end|terminate).{0,120}(?:after|following).{0,120}(?:return|beat)",
        mechanical,
        flags=re.I | re.S,
    )
    assert "packaged `encounter-sim` role" not in mechanical
    for field in ("OUTCOME:", "STATE DELTA:", "RECOVERY:"):
        assert field in mechanical
    assert "RESULT:" not in mechanical
    assert "BRIEF:" not in mechanical

    skill = _text("skills/play/SKILL.md")
    assert "../../agents/play-mechanics.md" in skill
    assert "encounter-sim" in _section(skill, "## 4. Carry the adventuring day")


def test_skill_descriptions_exclude_the_self_contained_mechanics_child() -> None:
    for relative_path in ("skills/play/SKILL.md", "skills/encounter-sim/SKILL.md"):
        skill = _text(relative_path)
        frontmatter = re.match(r"---\s*\n(?P<body>.*?)\n---", skill, flags=re.DOTALL)

        assert frontmatter is not None
        description = frontmatter.group("body")
        assert "play-mechanics" in description
        assert re.search(
            r"play-mechanics.{0,160}(?:self-contained|do not load|not for)",
            description,
            flags=re.IGNORECASE | re.DOTALL,
        )


def test_each_host_dispatches_canonical_roles_without_copying_role_bodies() -> None:
    claude = _text("skills/play/references/dispatch-claude-code.md")
    for role in ("adventure-prep", "game-master", "typical-player", "play-mechanics"):
        assert f"`{role}`" in claude
    assert "named agent" in claude.lower()

    codex = _text("skills/play/references/dispatch-codex.md")
    for role in ("adventure-prep", "game-master", "typical-player", "play-mechanics"):
        assert f"agents/{role}.md" in codex
    assert re.search(r"minimal bootstrap", codex, flags=re.IGNORECASE)
    assert re.search(
        r"child.{0,240}read.{0,200}(?:own|canonical).{0,120}role",
        codex,
        flags=re.I | re.S,
    )
    assert re.search(
        r"(?:do not|never).{0,180}(?:inject|copy).{0,120}role bod",
        codex,
        flags=re.I | re.S,
    )


def test_every_host_gives_seats_their_identity_gear_and_rules_brief() -> None:
    for relative_path in (
        "skills/play/references/dispatch-claude-code.md",
        "skills/play/references/dispatch-codex.md",
    ):
        dispatch = _text(relative_path)
        assert re.search(
            r"player.{0,300}identity.{0,120}sheet.{0,120}gear.{0,120}rules brief",
            dispatch,
            flags=re.IGNORECASE | re.DOTALL,
        )
        assert re.search(
            r"game-master.{0,500}party.{0,180}rules brief",
            dispatch,
            flags=re.IGNORECASE | re.DOTALL,
        )

    resume = _text("skills/play/references/resume.md")
    player = _text("agents/typical-player.md")
    assert re.search(
        r"re-spawn each agent player.{0,300}identity.{0,120}gear.{0,120}rules brief",
        resume,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"harness gives you.{0,200}identity.{0,120}gear.{0,120}rules brief",
        player,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_preparation_and_mechanical_children_receive_no_player_private_memory() -> None:
    dispatch = (
        _text("skills/play/references/dispatch-codex.md")
        + _text("skills/play/references/dispatch-claude-code.md")
    )
    assert re.search(
        r"adventure-prep.{0,500}(?:never|no).{0,180}(?:player|seat).{0,120}(?:memory|private)",
        dispatch,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"play-mechanics.{0,500}(?:never|no).{0,180}(?:player|seat).{0,120}(?:memory|private)",
        dispatch,
        flags=re.IGNORECASE | re.DOTALL,
    )
