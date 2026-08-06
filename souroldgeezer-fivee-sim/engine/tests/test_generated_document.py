"""Shared CLI mechanics for the two committed generated Markdown reports."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from fivee_sim import coverage, rulings

ReportMain = Callable[[list[str] | None], int]
REPORTS: tuple[tuple[ReportMain, str, Callable[[], str]], ...] = (
    (coverage.main, "COVERAGE.md", coverage.render_markdown),
    (rulings.main, "RULINGS.md", rulings.render_markdown),
)


@pytest.mark.parametrize(("command", "filename", "render"), REPORTS)
def test_public_report_commands_default_to_their_committed_documents(
    command: ReportMain,
    filename: str,
    render: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected_target = Path(__file__).resolve().parents[2] / "docs" / filename
    writes: list[tuple[Path, str, str | None]] = []

    def capture_write(path: Path, document: str, encoding: str | None = None) -> int:
        writes.append((path, document, encoding))
        return len(document)

    monkeypatch.setattr(sys, "argv", ["report"])
    monkeypatch.setattr(Path, "write_text", capture_write)
    result = command(None)

    assert result == 0
    assert writes == [(expected_target, render(), "utf-8")]
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"wrote {expected_target}\n"


@pytest.mark.parametrize(("command", "_filename", "render"), REPORTS)
def test_public_report_commands_use_the_first_explicit_target(
    command: ReportMain,
    _filename: str,
    render: Callable[[], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "requested" / "nested" / "REPORT.md"
    # Compatibility contract: the first target wins and historical surplus
    # arguments stay ignored across the consolidation.
    result = command([str(target), "ignored"])

    assert result == 0
    assert target.read_text(encoding="utf-8") == render()
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"wrote {target}\n"


@pytest.mark.parametrize(("command", "_filename", "_render"), REPORTS)
def test_public_report_command_write_failures_propagate_without_success_output(
    command: ReportMain,
    _filename: str,
    _render: Callable[[], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(IsADirectoryError):
        command([str(tmp_path)])

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""
