"""Encounter-scoped checks, saves, rolls, notes, refusals, and retry identity."""

from __future__ import annotations

from . import api
from .conftest import mapless_fight


def test_checks_can_name_social_abilities_and_join_the_replay_timeline() -> None:
    encounter_id = mapless_fight(seed=131)

    result = api.check(
        5,
        15,
        seed=7,
        encounter_id=encounter_id,
        request_id="persuade-guard",
        ability="charisma",
        skill="persuasion",
    )

    assert result["ability"] == "charisma"
    assert result["skill"] == "persuasion"
    attempt = api.replay_export(encounter_id, format_version=2)["bundle"]["attempts"][-1]
    assert attempt["operation"] == "check"
    assert attempt["arguments"]["skill"] == "persuasion"
    assert attempt["status"] == "success"


def test_rolls_and_saves_are_encounter_scoped_without_advancing_combat() -> None:
    encounter_id = mapless_fight(seed=137)
    before = api.encounter_state(encounter_id)

    rolled = api.roll("2d6+1", seed=11, encounter_id=encounter_id)
    saved = api.save(
        2, 13, seed=12, encounter_id=encounter_id, ability="wisdom"
    )

    assert rolled["encounter_id"] == encounter_id
    assert saved["ability"] == "wisdom"
    assert api.encounter_state(encounter_id) == before
    operations = [
        entry["operation"]
        for entry in api.replay_export(encounter_id, format_version=2)["bundle"]["attempts"]
    ]
    assert operations[-2:] == ["roll", "save"]


def test_notes_are_durable_and_idempotent() -> None:
    encounter_id = mapless_fight(seed=139)

    first = api.encounter_note(
        encounter_id,
        "The sentry agrees to stand down.",
        category="negotiation",
        request_id="note-1",
    )
    second = api.encounter_note(
        encounter_id,
        "This retry must not replace the first note.",
        category="negotiation",
        request_id="note-1",
    )

    assert second == first
    notes = [
        entry
        for entry in api.replay_export(encounter_id, format_version=2)["bundle"]["attempts"]
        if entry["operation"] == "encounter_note"
    ]
    assert len(notes) == 1
    assert notes[0]["arguments"]["text"] == "The sentry agrees to stand down."


def test_an_unscoped_primitive_keeps_its_legacy_shape() -> None:
    assert set(api.check(3, 12, seed=17)) == {
        "seed", "natural", "total", "dc", "success", "detail",
    }
