"""The coverage report must match the data it claims to describe.

A hand-maintained coverage list is a promise that quietly stops being true. This
test makes adding a monster, spell, or condition without regenerating the report a
failure rather than a slow drift into a document that lies.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fivee_sim.content import monster_records, spellbook
from fivee_sim.coverage import UNIMPLEMENTED_CONDITIONS, render_markdown
from fivee_sim.kernel.conditions import Condition

# tests/ -> engine/ -> plugin root
DOC = Path(__file__).resolve().parents[2] / "docs" / "COVERAGE.md"


def test_the_committed_report_is_current() -> None:
    assert DOC.is_file(), f"coverage report missing at {DOC}"
    committed = DOC.read_text(encoding="utf-8")
    if committed != render_markdown():
        pytest.fail(
            "docs/COVERAGE.md is stale. Regenerate it with "
            "`uv run python -m fivee_sim.coverage`."
        )


class TestReportContents:
    def test_every_bundled_creature_and_spell_appears(self) -> None:
        report = render_markdown()
        for name in monster_records():
            assert name in report, f"{name} missing from the coverage report"
        for name in spellbook():
            assert name in report, f"{name} missing from the coverage report"

    def test_every_condition_appears(self) -> None:
        report = render_markdown()
        for condition in Condition:
            assert condition.value in report

    def test_unimplemented_conditions_are_named(self) -> None:
        report = render_markdown()
        for name in UNIMPLEMENTED_CONDITIONS:
            assert name in report

    def test_the_unsupported_areas_are_stated_not_merely_omitted(self) -> None:
        # The question this report exists to answer is "what is missing?", so these
        # must be present as explicit statements.
        report = render_markdown()
        for subject in ("Classes", "backgrounds", "potions", "flanking", "multiclassing"):
            assert subject in report, f"{subject!r} not addressed in the report"

    def test_a_size_gated_rider_says_what_it_is_gated_on(self) -> None:
        # "on hit: prone" alone would describe the Wolf's Bite as unconditional,
        # which is the reading the record carried before the gate existed. The
        # report's whole job is to not say that.
        report = render_markdown()
        wolf_row = next(line for line in report.splitlines() if line.startswith("| Wolf "))
        assert "on hit: prone" in wolf_row
        assert "medium or smaller" in wolf_row.lower(), wolf_row

    def test_an_ungated_rider_claims_no_size_limit(self) -> None:
        # The negative half: every other bundled rider is unconditional, and the
        # renderer must not decorate one that has no gate.
        report = render_markdown()
        zombie_row = next(line for line in report.splitlines() if line.startswith("| Zombie "))
        assert "or smaller" not in zombie_row, zombie_row

    def test_a_spell_without_damage_does_not_claim_half_on_save(self) -> None:
        report = render_markdown()
        hold_person_row = next(
            line for line in report.splitlines() if line.startswith("| Hold Person ")
        )
        assert "wisdom save" in hold_person_row
        assert "half on save" not in hold_person_row
