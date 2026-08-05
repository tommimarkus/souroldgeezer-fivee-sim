"""The view one combatant is entitled to, rather than the whole fight.

``encounter.state`` reports every creature's hit points, AC, slots and items, so
handing it to a player is handing them the other side's sheet. Until now the only
answer was for a game master to redact in prose, which is a discipline rather than
a guarantee — and a discipline that has to be re-applied every single turn.

``encounter.brief`` is that redaction moved into the engine. What is asserted here
is mostly *absence*, which is the hard thing to test well: the classification is
held against ``_creature_state``'s real output so a field added there tomorrow
fails this file rather than quietly joining whichever side of the line it was
never assigned to.
"""

from __future__ import annotations

from typing import Any

import pytest

from fivee_sim.service.errors import NotFoundError, RequestError

from . import api

PARTY = {
    "name": "Thora",
    "team": "party",
    "ac": 16,
    "max_hp": 30,
    "position": [0, 0],
    "spell_slots": {"1": 2},
    "items": {"Potion of Healing": 1},
    "attacks": [
        {"name": "Longsword", "attack_bonus": 5, "damage": "1d8+3",
         "damage_type": "slashing", "kind": "melee"},
    ],
}
ALLY = dict(PARTY) | {"name": "Kesh", "position": [5, 0]}
FOE = {"monster": "Goblin Warrior", "label": "Sentry", "team": "monsters",
       "position": [20, 0]}


def _fight(**overrides: Any) -> str:
    combatants = [dict(PARTY), dict(ALLY), dict(FOE) | overrides]
    return str(api.encounter_create(combatants, seed=41)["encounter_id"])


def _enemy(brief: dict[str, Any], name: str = "Sentry") -> dict[str, Any]:
    return next(e for e in brief["enemies"] if e["name"] == name)


class TestWhatTheAskerSees:
    def test_their_own_sheet_comes_back_whole(self) -> None:
        brief = api.encounter_brief(_fight(), "Thora")
        assert brief["you"]["hp"] == 30
        assert brief["you"]["spell_slots"] == {1: 2}
        assert brief["you"]["items"] == {"Potion of Healing": 1}

    def test_an_ally_is_not_redacted(self) -> None:
        # Party members share numbers at a real table; the boundary is the
        # *other side*, not other people.
        brief = api.encounter_brief(_fight(), "Thora")
        kesh = next(a for a in brief["allies"] if a["name"] == "Kesh")
        assert kesh["hp"] == 30

    def test_the_distance_to_each_enemy_is_reported(self) -> None:
        # Without this a player cannot decide their own movement, which is the
        # whole reason the brief exists rather than a prose summary.
        brief = api.encounter_brief(_fight(), "Thora")
        assert _enemy(brief)["distance"] == 20

    def test_it_is_the_asker_s_side_that_is_privileged_not_the_party(self) -> None:
        # The mirror case. A brief that always showed "party" in full would pass
        # every other test in this class and be wrong.
        brief = api.encounter_brief(_fight(), "Sentry")
        assert brief["allies"] == [], "the sentry is alone on its side"
        assert {e["name"] for e in brief["enemies"]} == {"Thora", "Kesh"}
        assert "hp" not in _enemy(brief, "Thora")


class TestWhatIsWithheld:
    @pytest.mark.parametrize("secret", ["hp", "max_hp", "ac", "spell_slots", "items"])
    def test_an_enemy_never_carries_a_number_the_table_would_not_say(
        self, secret: str
    ) -> None:
        assert secret not in _enemy(api.encounter_brief(_fight(), "Thora"))

    def test_an_enemy_carries_a_described_condition_instead_of_hit_points(self) -> None:
        brief = api.encounter_brief(_fight(), "Thora")
        assert _enemy(brief)["health"] == "unharmed"

    def test_every_state_key_is_classified_one_way_or_the_other(self) -> None:
        # The guard that makes the absences above mean something. A key added to
        # `_creature_state` and classified nowhere would silently pick a side —
        # leaking if the brief copies by default, or vanishing if it filters by
        # default — and no other test here would notice.
        from fivee_sim.model import encounter as model

        state = api.encounter_state(_fight())
        reported = {key for c in state["combatants"] for key in c}
        classified = model.ENEMY_VISIBLE_KEYS | model.ENEMY_WITHHELD_KEYS
        assert reported <= classified, (
            f"unclassified combatant state key(s): {sorted(reported - classified)} — "
            "add each to ENEMY_VISIBLE_KEYS or ENEMY_WITHHELD_KEYS in "
            "model/encounter.py, deliberately"
        )

    def test_the_two_classifications_do_not_overlap(self) -> None:
        from fivee_sim.model import encounter as model

        assert not (model.ENEMY_VISIBLE_KEYS & model.ENEMY_WITHHELD_KEYS)


class TestSight:
    def test_a_creature_behind_total_cover_is_not_listed_at_all(self) -> None:
        # Not merely redacted — absent. Telling a player "there is something you
        # cannot see, at this distance" is itself information they do not have.
        # Total cover means *sealed*, not merely screened: a wall between two
        # creatures leaves three-quarters cover at most, because corner-to-corner
        # lines get around it. This is the box from
        # `test_grid.py::test_a_sealed_target_has_total_cover`, with the sentry
        # walled in at square (5, 5) and Thora out at (0, 0).
        walled = api.encounter_create(
            [dict(PARTY), dict(FOE) | {"position": [25, 25]}],
            seed=41,
            map={
                "width": 7, "height": 7,
                "rows": [
                    ".......",
                    ".......",
                    ".......",
                    ".......",
                    "....###",
                    "....#.#",
                    "....###",
                ],
                "legend": {".": "normal", "#": "wall"},
            },
        )["encounter_id"]
        brief = api.encounter_brief(str(walled), "Thora")
        assert brief["enemies"] == []


class TestRefusals:
    def test_an_unknown_encounter_is_a_not_found(self) -> None:
        with pytest.raises(NotFoundError, match="enc-nope"):
            api.encounter_brief("enc-nope", "Thora")

    def test_a_name_that_is_not_in_the_fight_is_refused_naming_who_is(self) -> None:
        with pytest.raises(RequestError, match="Thora"):
            api.encounter_brief(_fight(), "Nobody")
