"""Encounter-scoped checks, saves, rolls, notes, refusals, and retry identity."""

from __future__ import annotations

import pytest

from fivee_sim.service.errors import NotFoundError

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


def test_a_note_attributes_its_line_to_a_speaker_and_carries_it_into_the_bundle() -> None:
    encounter_id = mapless_fight(seed=149)

    written = api.encounter_note(
        encounter_id,
        "Hold there. Nobody crosses the mill after dark.",
        category="dialogue",
        speaker="Thora",
    )

    assert written["speaker"] == "Thora"
    attempt = api.replay_export(encounter_id, format_version=2)["bundle"]["attempts"][-1]
    assert attempt["operation"] == "encounter_note"
    assert attempt["arguments"]["speaker"] == "Thora"


def test_a_note_from_nobody_in_this_encounter_is_refused_before_it_is_journalled() -> None:
    """The surface's one sentence, and no beat written for a creature that is not here.

    Refused ahead of the attempt record for the reason ``act`` refuses an unknown
    actor there: a mistyped name is a client's mistake rather than a table's
    event, and a journal that recorded it would replay a line nobody spoke.
    """
    encounter_id = mapless_fight(seed=151)
    before = len(api.replay_export(encounter_id, format_version=2)["bundle"]["attempts"])

    with pytest.raises(NotFoundError, match="no combatant named 'Kettle' in this encounter"):
        api.encounter_note(encounter_id, "Kettle says nothing.", speaker="Kettle")

    after = api.replay_export(encounter_id, format_version=2)["bundle"]["attempts"]
    assert len(after) == before


def test_a_note_with_no_speaker_attributes_the_line_to_nobody() -> None:
    encounter_id = mapless_fight(seed=157)

    written = api.encounter_note(encounter_id, "Rain on the mill roof.")

    assert written["speaker"] is None
    attempt = api.replay_export(encounter_id, format_version=2)["bundle"]["attempts"][-1]
    assert attempt["arguments"]["speaker"] is None


def test_an_unscoped_primitive_keeps_its_legacy_shape() -> None:
    assert set(api.check(3, 12, seed=17)) == {
        "seed", "natural", "total", "dc", "success", "detail",
    }
