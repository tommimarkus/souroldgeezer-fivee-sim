"""The live adventure view is one seat's current chapter, never the run document."""

from __future__ import annotations

from typing import Any

import pytest

from fivee_sim.service.errors import NotFoundError

from . import api
from .opening import start_adventure

HERO: dict[str, Any] = {
    "name": "Thora",
    "team": "party",
    "ac": 16,
    "max_hp": 30,
    "position": [0, 0],
    "attacks": [
        {
            "name": "Longsword",
            "attack_bonus": 5,
            "damage": "1d8+3",
            "damage_type": "slashing",
            "kind": "melee",
        }
    ],
}
FOE: dict[str, Any] = {
    "name": "Bram",
    "team": "monsters",
    "ac": 13,
    "max_hp": 20,
    "position": [10, 0],
    "attacks": [
        {
            "name": "Club",
            "attack_bonus": 4,
            "damage": "1d6+2",
            "damage_type": "bludgeoning",
            "kind": "melee",
        }
    ],
}


def _forbidden_keys(value: Any) -> set[str]:
    forbidden = {
        "members",
        "carried",
        "recovery",
        "request_ids",
        "map_source",
        "replay",
        "events",
        "log",
    }
    if isinstance(value, dict):
        return (set(value) & forbidden) | {
            found for child in value.values() for found in _forbidden_keys(child)
        }
    if isinstance(value, list):
        return {found for child in value for found in _forbidden_keys(child)}
    return set()


class TestAdventureBrief:
    def test_it_is_an_allowlisted_view_of_the_last_linked_chapter(self) -> None:
        adventure_id = str(
            start_adventure(
                "The Drowned Mill", combatants=[HERO, FOE], seed=31, mode="exploration"
            )["adventure_id"]
        )
        latest = api.adventure_encounter(
            adventure_id,
            recovery={},
            recovery_note="A quiet hour beside the sluice",
            seed=32,
        )
        encounter_id = str(latest["encounter_id"])

        brief, _version = api.adventure_brief(adventure_id, "Thora")

        assert brief == {
            "adventure": {
                "id": adventure_id,
                "name": "The Drowned Mill",
                "status": "active",
                "chapter_count": 2,
            },
            "chapter": {
                "index": 1,
                "encounter_id": encounter_id,
                "mode": "combat",
                "finalized": False,
            },
            "state": api.encounter_brief(encounter_id, "Thora"),
        }
        assert "recovery_note" not in brief["chapter"]
        assert _forbidden_keys(brief) == set()

    def test_an_unknown_seat_reuses_the_encounter_briefs_not_found(self) -> None:
        adventure_id = str(
            start_adventure("The Drowned Mill", combatants=[HERO, FOE], seed=33)[
                "adventure_id"
            ]
        )

        with pytest.raises(NotFoundError, match="Nobody Here"):
            api.adventure_brief(adventure_id, "Nobody Here")
