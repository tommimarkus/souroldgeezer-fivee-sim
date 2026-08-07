"""Package contracts that keep live-play role context bounded."""

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER_PATH = PLUGIN_ROOT / "agents/play-controller.md"


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


def _frontmatter(markdown: str) -> tuple[str, str]:
    match = re.match(r"---\s*\n(?P<metadata>.*?)\n---(?P<body>.*)", markdown, re.DOTALL)
    assert match is not None
    return match.group("metadata"), match.group("body")


def _metadata_value(metadata: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(?P<value>.+)$", metadata, re.MULTILINE)
    assert match is not None, f"missing {key!r} metadata"
    return match.group("value")


def test_play_controller_tools_are_scoped_to_roles_references_and_play_artifacts() -> None:
    controller = CONTROLLER_PATH.read_text(encoding="utf-8")
    metadata, _ = _frontmatter(controller)

    tools = {tool.strip() for tool in _metadata_value(metadata, "tools").split(",")}
    assert tools == {
        "Agent",
        "SendMessage",
        "Bash(python3 /${CLAUDE_PLUGIN_ROOT}/scripts/fivee.py:*)",
        "Read(/${CLAUDE_PLUGIN_ROOT}/agents/**)",
        "Read(/${CLAUDE_PLUGIN_ROOT}/skills/play/references/**)",
        "Read(.fivee-sim/plays/**)",
        "Write(.fivee-sim/plays/**)",
    }
    denied = {
        tool.strip()
        for tool in _metadata_value(metadata, "disallowedTools").split(",")
    }
    assert {
        "AskUserQuestion",
        "Skill",
        "WebFetch",
        "WebSearch",
        "mcp__*",
    } <= denied


def test_play_controller_ends_at_the_first_interval_boundary() -> None:
    controller = CONTROLLER_PATH.read_text(encoding="utf-8")
    _, body = _frontmatter(controller)
    lifetime = _section(body, "## Interval lifetime")

    assert re.search(r"at most six resolved (?:decision )?beats", lifetime, re.I)
    assert re.search(r"encounter.{0,120}chapter.{0,160}boundary", lifetime, re.I | re.S)
    assert re.search(r"flush.{0,160}artifact", lifetime, re.I | re.S)
    assert re.search(r"terminate.{0,160}(?:child|descendant)", lifetime, re.I | re.S)
    assert re.search(r"(?:end|terminate).{0,120}(?:interval|yourself)", lifetime, re.I | re.S)


def test_play_controller_cannot_open_or_return_hidden_module_state() -> None:
    controller = CONTROLLER_PATH.read_text(encoding="utf-8")
    _, body = _frontmatter(controller)
    capability = _section(body, "## Capability and information boundary")

    assert "skills/play/references/" in capability
    assert re.search(r"(?:never|do not).{0,160}(?:adventure|module) text", capability, re.I | re.S)
    assert re.search(r"(?:never|do not).{0,160}hidden module state", capability, re.I | re.S)
    assert re.search(r"current module locators.{0,160}game master", capability, re.I | re.S)
    assert re.search(r"exactly one.{0,160}(?:writer|write owner)", capability, re.I | re.S)
    assert re.search(r"Agent.? tool.{0,160}blocked", capability, re.I | re.S)


def test_root_is_a_thin_supervisor_with_a_closed_return_boundary() -> None:
    skill = _text("skills/play/SKILL.md")
    supervision = _section(skill, "## 3. Supervise intervals")
    plain = " ".join(supervision.lower().split())

    assert "play-controller" in supervision
    assert re.search(
        r"root.{0,160}(?:does not|never).{0,160}"
        r"(?:write|edit).{0,100}table artifact",
        plain,
    )
    assert re.search(r"exactly one.{0,120}(?:controller|writer).{0,120}(?:owns|writes)", plain)
    for allowed_return in (
        "user-visible narration",
        "human-seat prompt",
        "blocker",
        "interval result",
    ):
        assert allowed_return in plain
    for retained_private in (
        "raw council returns",
        "commits",
        "chair payloads",
        "mechanics control frames",
        "raw engine traffic",
        "game-master private checkpoint data",
        "worker reasoning",
    ):
        assert retained_private in plain
    assert re.search(r"same.{0,100}controller.{0,160}(?:human|answer)", plain)
    assert re.search(r"800 stable-proxy tokens", plain)


def test_root_owns_run_identity_while_controller_discovers_fivee_syntax() -> None:
    skill = _text("skills/play/SKILL.md")
    engine = _section(skill, "## Engine boundary")
    plain = " ".join(engine.replace("`", "").replace("*", "").split())

    assert "adventure id" in plain.lower()
    assert "controller" in plain.lower()
    assert re.search(r"root.{0,160}never.{0,120}(?:invoke|run).{0,80}fivee", plain, re.I)
    assert re.search(
        r"(?:never|do not).{0,180}(?:construct|guess).{0,120}(?:flag|syntax|command)",
        plain,
        re.I,
    )
    assert "fivee help <operation>" not in engine


def test_controller_receives_values_and_discovers_direct_command_syntax() -> None:
    controller = CONTROLLER_PATH.read_text(encoding="utf-8")
    loop = _section(controller, "## Run the table")
    plain = " ".join(loop.replace("`", "").replace("*", "").split())

    for field in ("run id", "canonical operation name", "resource identifiers", "argument values"):
        assert field in plain.lower()
    assert re.search(r"discover.{0,120}syntax", plain, re.I)
    assert re.search(r"(?:do not|never).{0,160}guess.{0,120}(?:flag|syntax)", plain, re.I)

    table_loop = _text("skills/play/references/table-loop.md")
    assert "fivee --run" not in table_loop
    for dispatch in (
        _text("skills/play/references/dispatch-codex.md"),
        _text("skills/play/references/dispatch-claude-code.md"),
    ):
        for field in (
            "run id",
            "canonical operation name",
            "resource identifiers",
            "argument values",
        ):
            assert field in dispatch.lower()


def test_fresh_interval_rehydrates_roles_from_bounded_table_state() -> None:
    controller = CONTROLLER_PATH.read_text(encoding="utf-8")
    rehydration = _section(controller, "## Fresh interval rehydration")
    plain = " ".join(rehydration.lower().split())

    for artifact in (
        "checkpoint.json",
        "seats/<name>.md",
        "council.json",
        "brief-cursors.json",
        "module-index.json",
    ):
        assert artifact in rehydration
    assert "run-sheet.json" in rehydration
    assert re.search(r"pointer.{0,120}digest", plain)
    assert re.search(r"fresh.{0,120}game.master", plain)
    assert re.search(r"fresh.{0,120}(?:player|seat)", plain)
    assert re.search(r"fork_turns=\"none\"", rehydration)
    assert re.search(r"(?:never|do not).{0,160}full transcript", plain)


def test_host_dispatch_puts_the_controller_between_root_and_live_roles() -> None:
    codex = _text("skills/play/references/dispatch-codex.md")
    assert "../../agents/play-controller.md" in codex
    assert re.search(
        r"root.{0,240}play-controller.{0,160}fork_turns=\"none\"",
        codex,
        flags=re.I | re.S,
    )
    assert re.search(
        r"controller.{0,240}(?:game-master|typical-player)"
        r".{0,300}fork_turns=\"none\"",
        codex,
        flags=re.I | re.S,
    )
    assert re.search(r"direct.{0,160}fivee\.py", codex, re.I | re.S)
    assert re.search(r"play-mechanics.{0,180}(?:fallback|unavailable)", codex, re.I | re.S)

    claude = _text("skills/play/references/dispatch-claude-code.md")
    assert re.search(r"root.{0,200}named agent `play-controller`", claude, re.I | re.S)
    assert re.search(
        r"play-controller.{0,300}owns.{0,240}(?:game-master|typical-player)",
        claude,
        flags=re.I | re.S,
    )
    assert re.search(r"direct.{0,160}fivee\.py", claude, re.I | re.S)
    assert re.search(r"play-mechanics.{0,180}(?:fallback|unavailable)", claude, re.I | re.S)
    assert re.search(
        r"Agent.? tool.{0,240}(?:depth|nested).{0,240}blocked",
        claude,
        flags=re.I | re.S,
    )


def test_play_prepares_a_private_module_index_before_spawning_the_game_master() -> None:
    skill = _text("skills/play/SKILL.md")
    briefing = _section(skill, "## 2. Brief the seats")

    assert "references/startup.md" in briefing
    prep = _text("skills/play/references/module-prep.md")
    guidance = " ".join(prep.lower().split())

    assert "module-index.json" in guidance
    assert "module-index.json.partial" in guidance
    assert re.search(r"ordinary play.{0,240}structur", guidance)
    assert re.search(r"ordinary play.{0,500}(?:does not|never).{0,120}(?:gap|omission)", guidance)
    assert re.search(r"playtest.{0,300}(?:semantic|full).{0,200}inventory", guidance)
    assert "scripts/fivee-play.py" in guidance
    assert re.search(r"cache.{0,240}source sha-256.{0,160}indexer version", guidance)
    assert re.search(r"complete manifest.{0,240}(?:publish|rename)", guidance)
    assert re.search(r"prep-staging.{0,240}\.partial", guidance)
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


def test_live_play_uses_direct_controller_mechanics_with_conditional_fallback() -> None:
    table_loop = _text("skills/play/references/table-loop.md")
    mechanical = _section(table_loop, "## Mechanical context and briefs")

    assert "`play-mechanics`" in mechanical
    assert "fivee.py" in mechanical
    assert re.search(r"controller.{0,180}direct", mechanical, re.I | re.S)
    assert re.search(r"play-mechanics.{0,180}(?:fallback|unavailable)", mechanical, re.I | re.S)
    assert re.search(r"one decision beat", mechanical, flags=re.IGNORECASE)
    assert "--select" in mechanical
    assert "--as" in mechanical
    assert re.search(
        r"raw engine output.{0,180}(?:never|not).{0,180}(?:artifact|game master|root)",
        mechanical,
        re.I | re.S,
    )
    assert "packaged `encounter-sim` role" not in mechanical

    skill = _text("skills/play/SKILL.md")
    assert "../../agents/play-mechanics.md" in skill
    assert "encounter-sim" in _section(skill, "## 4. Carry the adventuring day")


def test_skill_descriptions_name_the_conditional_mechanics_fallback() -> None:
    for relative_path in ("skills/play/SKILL.md", "skills/encounter-sim/SKILL.md"):
        skill = _text(relative_path)
        frontmatter = re.match(r"---\s*\n(?P<body>.*?)\n---", skill, flags=re.DOTALL)

        assert frontmatter is not None
        description = frontmatter.group("body")
        assert "play-mechanics" in description
        assert re.search(
            r"play-mechanics.{0,160}(?:fallback|unavailable|do not load|not for)",
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


def test_controller_spawns_gm_and_agent_players_concurrently_after_setup() -> None:
    controller = _text("agents/play-controller.md")
    rehydration = _section(controller, "## Fresh interval rehydration")
    assert re.search(
        r"(?:concurrent|together).{0,240}game.master.{0,240}player",
        rehydration,
        flags=re.I | re.S,
    )
    assert re.search(r"game master.{0,180}lazy", rehydration, re.I | re.S)
    assert re.search(r"player.{0,180}tool inventory", rehydration, re.I | re.S)


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


def test_fallback_children_receive_no_player_private_memory() -> None:
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
