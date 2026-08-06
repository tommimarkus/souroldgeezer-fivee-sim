"""Shared CLI mechanics for the two committed generated Markdown reports."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fivee_sim._generated_document import write_generated_document


def _source_file(tmp_path: Path) -> Path:
    return tmp_path / "plugin" / "engine" / "src" / "fivee_sim" / "report.py"


def test_no_argument_writes_beside_the_plugin_and_uses_sys_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_file = _source_file(tmp_path)
    target = tmp_path / "plugin" / "docs" / "REPORT.md"
    document = "# Résumé\n\nSnowman: ☃\n"
    parent_seen_by_renderer: list[bool] = []

    def render() -> str:
        parent_seen_by_renderer.append(target.parent.is_dir())
        return document

    monkeypatch.setattr(sys, "argv", ["report"])
    result = write_generated_document(
        None,
        source_file=str(source_file),
        default_filename="REPORT.md",
        render=render,
    )

    assert result == 0
    assert parent_seen_by_renderer == [True]
    assert target.read_bytes() == document.encode("utf-8")
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"wrote {target}\n"


def test_first_explicit_argument_wins_and_later_arguments_are_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "requested" / "nested" / "REPORT.md"
    document = "requested\n"

    result = write_generated_document(
        [str(target), "ignored"],
        source_file=str(_source_file(tmp_path)),
        default_filename="DEFAULT.md",
        render=lambda: document,
    )

    assert result == 0
    assert target.read_bytes() == b"requested\n"
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"wrote {target}\n"


def test_render_failure_propagates_after_the_parent_is_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "requested" / "REPORT.md"

    def fail() -> str:
        assert target.parent.is_dir()
        raise RuntimeError("render failed")

    with pytest.raises(RuntimeError, match="render failed"):
        write_generated_document(
            [str(target)],
            source_file=str(_source_file(tmp_path)),
            default_filename="DEFAULT.md",
            render=fail,
        )

    assert not target.exists()
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""
