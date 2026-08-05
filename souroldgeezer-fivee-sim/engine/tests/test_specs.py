"""``service/specs.py``: what the wire edge refuses, and in which order.

The order is the subject here rather than an implementation detail. A caller
probing ``encounter.create`` for the first time writes one combatant, so
whichever check runs first is the only diagnosis most callers ever read — and if
that check is the arity one, a spec missing a required field is answered with a
complaint about the count and nothing at all about the field.
"""

from __future__ import annotations

from typing import Any

import pytest

from fivee_sim.service.errors import RequestError

from . import api

HERO: dict[str, Any] = {"name": "Thora", "team": "party", "ac": 16, "max_hp": 30}
GOBLIN: dict[str, Any] = {"name": "Goblin", "team": "monsters", "ac": 15, "max_hp": 7}


class TestCombatantSpecsAreDiagnosedBeforeTheyAreCounted:
    def test_a_lone_malformed_combatant_names_the_field_it_is_missing(self) -> None:
        # The defect: the arity check used to run first, so the single combatant
        # a caller starts with answered "an encounter needs at least two
        # combatants" and said nothing about the 'ac' that was never there.
        # Adding a second combatant was the only way to be shown the real fault,
        # which is exactly backwards — the first probe got the least useful
        # answer, and the count was the one thing the caller already knew.
        with pytest.raises(RequestError, match="combatant spec is missing 'ac'"):
            api.encounter_create([{"name": "Thora", "team": "party", "max_hp": 30}])

    def test_a_lone_combatant_with_an_unreadable_key_names_the_key(self) -> None:
        # The other refusal ``creature_from_spec`` owns, to show the reorder put
        # the whole shape check in front of the count rather than one branch.
        with pytest.raises(RequestError, match="unknown combatant key 'speeed'"):
            api.encounter_create([{**HERO, "speeed": 30}])

    def test_an_empty_list_still_gets_the_arity_message(self) -> None:
        # The one case with nothing to diagnose: no spec means no field can be
        # missing, so the count is the only true thing left to say.
        with pytest.raises(
            RequestError, match="an encounter needs at least two combatants"
        ):
            api.encounter_create([])

    def test_one_well_formed_combatant_is_still_too_few(self) -> None:
        with pytest.raises(
            RequestError, match="an encounter needs at least two combatants"
        ):
            api.encounter_create([dict(HERO)])

    def test_a_malformed_second_combatant_is_diagnosed_as_well(self) -> None:
        # The ordinary case the reorder must not lose: with enough combatants to
        # pass the count, each one is still checked and named.
        with pytest.raises(RequestError, match="combatant spec is missing 'max_hp'"):
            api.encounter_create(
                [dict(HERO), {"name": "Goblin", "team": "monsters", "ac": 15}]
            )

    def test_two_well_formed_combatants_are_accepted(self) -> None:
        # The floor under the three refusals above: the reorder refuses nothing
        # a valid pair used to be allowed to do.
        created = api.encounter_create([dict(HERO), dict(GOBLIN)], seed=7)

        assert {row["name"] for row in created["state"]["combatants"]} == {
            "Thora",
            "Goblin",
        }
