"""The compact generated coverage report must match the catalog and runtime data."""

from __future__ import annotations

from pathlib import Path

import pytest

from fivee_sim.content import builtin_registry
from fivee_sim.coverage import render_markdown

DOC = Path(__file__).resolve().parents[2] / "docs" / "COVERAGE.md"


def test_the_committed_report_is_current() -> None:
    assert DOC.is_file(), f"coverage report missing at {DOC}"
    committed = DOC.read_text(encoding="utf-8")
    if committed != render_markdown():
        pytest.fail(
            "docs/COVERAGE.md is stale. Regenerate it with "
            "`uv run python -m fivee_sim.coverage`."
        )


def test_report_is_compact_totals_not_a_second_catalog() -> None:
    report = render_markdown()
    assert len(report.encode()) <= 16 * 1024
    assert "Goblin Warrior" not in report
    assert "Fireball" not in report
    assert "`fivee rules.lookup`" in report
    assert "`fivee catalog.search`" in report
    assert "`fivee catalog.get`" in report
    assert "`fivee catalog.table`" in report
    assert "`fivee content.status`" in report
    assert "(../skills/encounter-sim/SKILL.md)" in report
    assert "lookup_rule" not in report
    assert "catalog_search" not in report
    assert "catalog_get" not in report
    assert "catalog_table" not in report
    assert "content_status" not in report


def test_catalog_category_and_progress_totals_are_derived() -> None:
    registry = builtin_registry()
    report = render_markdown()
    assert f"| Source sections | {len(registry.catalog)} |" in report
    assert f"| Printed tables | {len(registry.catalog_tables)} |" in report
    for kind in ("spell", "glossary", "creature"):
        count = sum(record.kind == kind for record in registry.catalog.values())
        assert f"| {kind} | {count} |" in report
    pending = sum(
        record.fact_status.value == "pending" for record in registry.catalog.values()
    )
    assert f"| pending | {pending} |" in report


def test_execution_counts_are_derived_from_the_registry() -> None:
    registry = builtin_registry()
    report = render_markdown()
    for section, count in registry.summary()["counts"].items():
        assert f"| {section} | {count} |" in report
