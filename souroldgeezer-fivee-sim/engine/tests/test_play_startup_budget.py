"""Cold-path proxy budgets and bootstrap data boundaries for ordinary play."""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _text(relative: str) -> str:
    return (PLUGIN_ROOT / relative).read_text(encoding="utf-8")


def _tokens(text: str) -> int:
    return len(re.findall(r"\w+|[^\w\s]", text))


def test_four_agent_prepared_markdown_cold_path_stays_under_proxy_budget() -> None:
    root_common = _text("skills/play/SKILL.md") + _text("skills/play/references/startup.md")
    root_claude = root_common + _text("skills/play/references/dispatch-claude-code.md")
    root_codex = root_common + _text("skills/play/references/dispatch-codex.md")
    controller = (
        _text("agents/play-controller.md")
        + _text("skills/play/references/table-loop.md")
        + _text("skills/play/references/seating-and-pauses.md")
    )
    game_master = _text("agents/game-master.md")
    player = _text("agents/typical-player.md")

    assert max(_tokens(root_claude), _tokens(root_codex)) <= 3_500
    assert _tokens(controller) <= 3_200
    assert _tokens(game_master) <= 1_800
    assert _tokens(player) <= 1_800
    total = max(_tokens(root_claude), _tokens(root_codex)) + _tokens(controller)
    total += _tokens(game_master) + 4 * _tokens(player)
    assert total <= 15_700


def test_prepared_markdown_common_path_has_no_prep_or_mechanics_model_child() -> None:
    startup = _text("skills/play/references/startup.md")
    dispatches = _text("skills/play/references/dispatch-claude-code.md") + _text(
        "skills/play/references/dispatch-codex.md"
    )
    common = startup + dispatches

    assert re.search(r"prepared markdown.{0,240}no `adventure-prep`", common, re.I | re.S)
    assert re.search(r"direct.{0,200}launcher.{0,240}no `play-mechanics`", common, re.I | re.S)
    assert re.search(
        r"play-mechanics.{0,200}(?:only|conditional).{0,160}fallback",
        common,
        re.I | re.S,
    )


def test_root_and_controller_bootstrap_never_embed_bulk_inputs() -> None:
    root = _text("skills/play/references/startup.md")
    controller = _text("agents/play-controller.md")
    guidance = " ".join((root + controller).lower().split())

    for bulk in ("content", "maps", "scenes", "full pregen"):
        assert re.search(
            rf"(?:never|do not).{{0,220}}(?:embed|bootstrap|payload).{{0,220}}{bulk}",
            guidance,
        ), bulk
    assert "inputs/seats/<name>.json" in controller
    assert "inputs/party-gm.json" in controller
