"""Package contract for the disposable live-play mechanics role.

The full encounter skill is useful when a user asks to run or analyse a fight,
but loading it for every adventure decision beat is avoidable context.  These
tests pin the smaller role that play dispatches instead: one launcher-scoped
child, one compact brief, one mechanical beat, then termination.
"""

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
AGENT_PATH = PLUGIN_ROOT / "agents/play-mechanics.md"
LAUNCHER_BASH_PATTERN = "Bash(python3 /${CLAUDE_PLUGIN_ROOT}/scripts/fivee.py:*)"


def _agent() -> str:
    return AGENT_PATH.read_text(encoding="utf-8")


def _frontmatter(agent: str) -> tuple[str, str]:
    match = re.match(r"---\s*\n(?P<metadata>.*?)\n---(?P<body>.*)", agent, re.DOTALL)
    assert match is not None
    return match.group("metadata"), match.group("body")


def _metadata_value(metadata: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(?P<value>.+)$", metadata, re.MULTILINE)
    assert match is not None, f"missing {key!r} metadata"
    return match.group("value")


def _proxy_tokens(text: str) -> int:
    """Stable, dependency-free proxy used only to enforce the role's load budget."""
    return len(re.findall(r"\w+|[^\w\s]", text))


def test_play_mechanics_has_only_launcher_bash_and_read() -> None:
    metadata, body = _frontmatter(_agent())

    assert _metadata_value(metadata, "tools") == f"{LAUNCHER_BASH_PATTERN}, Read"
    denied = {
        tool.strip()
        for tool in _metadata_value(metadata, "disallowedTools").split(",")
    }
    assert {
        "Agent",
        "Edit",
        "Glob",
        "Grep",
        "SendMessage",
        "Skill",
        "WebFetch",
        "WebSearch",
        "Write",
        "mcp__*",
    } <= denied
    assert "Skill" not in _metadata_value(metadata, "tools")
    assert "skills/encounter-sim" not in body
    assert "encounter-sim/SKILL.md" not in body
    assert re.search(
        r"never.{0,100}(?:invoke|load|read).{0,100}\bSkill\b",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"Claude Code.{0,300}\bBash\b.{0,300}\bother hosts\b.{0,300}\bfrontmatter\b",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_play_mechanics_runs_one_bounded_decision_beat() -> None:
    agent = _agent()
    _, body = _frontmatter(agent)
    plain = " ".join(body.replace("`", "").replace("*", "").split())

    for obligation in (
        "one decision beat",
        "compact mechanical brief",
        "encounter id",
        "adjudicated request",
        "participant",
        "fivee help",
        "at most two help calls",
        "exactly one mechanical action",
        "engine owns state, rolls, and arithmetic",
        "coordinator is the single artifact writer",
        "OUTCOME:",
        "STATE DELTA:",
        "RECOVERY:",
        "then terminate",
    ):
        assert obligation.casefold() in plain.casefold(), obligation

    assert re.search(
        r"(?:do not|never|refuse).{0,180}\badventure\b.{0,180}\bmodule\b",
        plain,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"at most one.{0,180}(?:corrected|correction).{0,180}(?:blocked|terminate)",
        plain,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:no|never).{0,120}(?:identical|same).{0,80}retr(?:y|ies)",
        plain,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(r"OUTCOME:.{0,100}(?:120|160) words", plain, re.IGNORECASE)
    assert _proxy_tokens(agent) <= 2_750
