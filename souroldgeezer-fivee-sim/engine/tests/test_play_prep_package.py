"""Package contracts for bounded adventure preparation and live GM context."""

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _text(relative_path: str) -> str:
    return (PLUGIN_ROOT / relative_path).read_text(encoding="utf-8")


def _section(markdown: str, heading: str) -> str:
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


def _proxy_tokens(text: str) -> int:
    """Stable dependency-free proxy used only to hold prompt-load budgets."""
    return len(re.findall(r"\w+|[^\w\s]", text))


def test_adventure_prep_is_a_disposable_read_only_role() -> None:
    prep = _text("agents/adventure-prep.md")
    frontmatter = re.match(r"---\s*\n(?P<body>.*?)\n---", prep, flags=re.DOTALL)

    assert frontmatter is not None
    metadata = frontmatter.group("body")
    assert re.search(r"^name:\s*adventure-prep\s*$", metadata, flags=re.MULTILINE)
    assert re.search(r"^tools:\s*Read\s*$", metadata, flags=re.MULTILINE)
    denied_match = re.search(
        r"^disallowedTools:\s*(?P<value>.+)$", metadata, flags=re.MULTILINE
    )
    assert denied_match is not None
    denied = {tool.strip() for tool in denied_match.group("value").split(",")}
    assert {"Agent", "Bash", "Edit", "Skill", "Write", "mcp__*"} <= denied

    body = prep[frontmatter.end() :]
    assert re.search(r"\bdisposable\b.{0,180}\bend\b", body, flags=re.I | re.S)
    assert re.search(
        r"(?:do not|never).{0,160}(?:narrate|player)", body, flags=re.I | re.S
    )


def test_prep_modes_separate_structural_indexing_from_playtest_review() -> None:
    prep = _text("agents/adventure-prep.md")
    ordinary = _section(prep, "## Ordinary play")
    playtest = _section(prep, "## Playtest mode")

    for obligation in ("structure", "cross-reference", "source order", "module index"):
        assert obligation in ordinary.lower()
    assert re.search(
        r"(?:do not|never).{0,180}(?:gap|omission).{0,100}(?:analysis|finding)",
        ordinary,
        flags=re.I | re.S,
    )
    assert re.search(r"heading.{0,160}explicit.{0,120}(?:key|label)", ordinary, re.I | re.S)
    assert re.search(
        r"(?:do not|never).{0,180}prose.only.{0,160}(?:npc|treasure|route|secret)",
        ordinary,
        flags=re.I | re.S,
    )

    playtest_plain = " ".join(playtest.lower().split())
    for obligation in (
        "scenes",
        "encounters",
        "npcs",
        "treasure",
        "stated dcs",
        "assumed route",
        "omission",
    ):
        assert obligation in playtest_plain


def test_prep_emits_a_bounded_module_index_for_coordinator_publication() -> None:
    prep = _text("agents/adventure-prep.md")
    contract = _section(prep, "## Module index contract")
    frames = _section(prep, "## Output frames")
    contract_plain = " ".join(contract.lower().split())

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
        assert field in contract_plain
    assert "module-index.json" in contract
    assert re.search(r"source[- ]ordered", contract, flags=re.I)
    assert re.search(r"stable.{0,100}\bid\b", contract, flags=re.I | re.S)
    assert re.search(r"(?:line|page).{0,100}locator", contract, flags=re.I | re.S)
    assert re.search(
        r"title.{0,180}(?:heading|key|neutral).{0,180}secret",
        contract,
        flags=re.I | re.S,
    )

    assert re.search(r"(?:at most|max(?:imum)?)\s*20\s+entries", frames, flags=re.I)
    assert re.search(r"(?:at most|max(?:imum)?)\s*1[, ]?200\s+proxy tokens", frames, flags=re.I)
    assert "complete manifest" in frames.lower()
    assert re.search(r'"?complete"?\s*:\s*true', frames, flags=re.I)
    assert re.search(r"coordinator.{0,200}\.partial", frames, flags=re.I | re.S)
    assert re.search(r"coordinator.{0,220}(?:atomic|publish)", frames, flags=re.I | re.S)
    assert re.search(
        r"(?:do not|never).{0,200}(?:write|publish).{0,100}artifact",
        frames,
        flags=re.I | re.S,
    )


def test_live_game_master_reads_only_current_indexed_module_sections() -> None:
    game_master = _text("agents/game-master.md")
    initial = _section(game_master, "## Initial spawn only")
    initial_plain = " ".join(initial.lower().split())

    assert "module-index.json" in initial
    for obligation in ("module ids", "locators", "bounded checkpoint", "lazy"):
        assert obligation in initial_plain
    assert re.search(
        r"(?:do not|never).{0,180}(?:whole|entire|end to end).{0,120}adventure",
        initial,
        flags=re.I | re.S,
    )
    assert not re.search(
        r"read the adventure once.{0,80}end to end", initial, flags=re.I | re.S
    )
    assert re.search(
        r"source hash.{0,160}mismatch.{0,220}(?:refuse|restart|resume)",
        initial,
        flags=re.I | re.S,
    )


def test_prep_and_live_gm_prompts_stay_inside_load_budgets() -> None:
    prep = _text("agents/adventure-prep.md")
    game_master = _text("agents/game-master.md")

    assert _proxy_tokens(prep) <= 3_250
    assert _proxy_tokens(game_master) <= 3_250
