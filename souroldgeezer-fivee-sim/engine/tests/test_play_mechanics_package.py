"""Package contract for the conditional live-play mechanics fallback.

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
    assert re.search(
        r"fallback.{0,200}(?:controller|launcher).{0,200}unavailable",
        body,
        re.I | re.S,
    )
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
        "run id",
        "canonical operation name",
        "argument values",
        "encounter id",
        "adjudicated request",
        "participant",
        "fivee help",
        "at most two help calls",
        "exactly one mechanical action",
        "engine owns state, rolls, and arithmetic",
        "interval controller is the single table-artifact writer",
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


def test_play_mechanics_discovers_syntax_before_execution() -> None:
    agent = _agent()
    procedure = _section(agent, "## One-beat procedure")
    failure = _section(agent, "## Failure and correction")
    plain = " ".join(procedure.replace("`", "").replace("*", "").split())

    assert re.search(
        r"fivee help <operation>.{0,240}(?:before|prior to).{0,160}"
        r"(?:execute|invoke|call)",
        plain,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"never.{0,160}(?:execute|invoke|call).{0,160}(?:discover|learn).{0,100}"
        r"(?:argument|parameter|syntax)",
        plain,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:example|help).{0,240}(?:only|exact).{0,160}(?:supplied|brief).{0,100}"
        r"(?:value|field)",
        plain,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"(?:parameter|argument).{0,120}error.{0,220}(?:blocked|transcription)",
        " ".join(failure.split()),
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_play_mechanics_retains_the_moved_roll_interlude_and_limit_contracts() -> None:
    agent = _agent()
    rolls = _section(agent, "## Rolls, and who makes them").lower()
    scenes = _section(agent, "## The scenes between the fights are chapters too").lower()
    limits = _section(agent, "## Honest limits to state out loud").lower()

    for obligation in ("human", "natural", "agent", "engine", "modifier", "outcome"):
        assert obligation in rolls
    for obligation in ("exploration", "actor", "encounter id", "speaker", "finalize"):
        assert obligation in scenes
    for obligation in (
        "mapless",
        "height",
        "frightened",
        "exhaustion",
        "rest",
        "unmodelled_facts",
    ):
        assert obligation in limits
