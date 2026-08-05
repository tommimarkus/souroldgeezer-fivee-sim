"""``service/specs.py``: what the wire edge refuses, and in which order.

The order is the subject here rather than an implementation detail. A caller
probing ``encounter.create`` for the first time writes one combatant, so
whichever check runs first is the only diagnosis most callers ever read — and if
that check is the arity one, a spec missing a required field is answered with a
complaint about the count and nothing at all about the field.

The second subject is *carry-over*: the state a combatant walked out of the last
fight in, written back into the spec that starts the next one. The four fields
that carry it are checked against the shape ``Encounter.state()`` reports, not
against a shape invented here, because a caller who has to reshape a payload
before feeding it back is a caller who will get the reshaping wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from fivee_sim.content import ContentRegistry, builtin
from fivee_sim.service.errors import RequestError
from fivee_sim.service.specs import creature_from_spec

from . import api

HERO: dict[str, Any] = {"name": "Thora", "team": "party", "ac": 16, "max_hp": 30}
GOBLIN: dict[str, Any] = {"name": "Goblin", "team": "monsters", "ac": 15, "max_hp": 7}
ALLY: dict[str, Any] = {"name": "Bram", "team": "party", "ac": 14, "max_hp": 22}


@pytest.fixture(scope="module")
def registry() -> ContentRegistry:
    """The bundled slice, loaded once: no test here configures content."""
    return builtin()


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

    def test_a_value_outside_a_closed_vocabulary_names_the_field(self) -> None:
        """A caller's bad enum value is the caller's error, not the engine's.

        Every one of these reached ``Ability(...)``/``Size(...)`` raw and left as
        an uncaught ``ValueError``, which the HTTP adapter can only render as a
        500 ``internal`` — telling the caller the engine broke when in fact the
        request did, and offering nothing to fix.
        """
        for key, bad in (
            ("spellcasting_ability", "moxie"),
            ("size", "enormous"),
            ("death_rule", "explodes"),
        ):
            with pytest.raises(RequestError, match=f"combatant key '{key}'"):
                api.encounter_create([{**HERO, key: bad}, dict(GOBLIN)])

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


def _combatant(created: dict[str, Any], name: str) -> dict[str, Any]:
    """One combatant's slice of a created encounter's reported state."""
    rows = {str(row["name"]): row for row in created["state"]["combatants"]}
    return dict(rows[name])


class TestCarriedOverCombatantState:
    """The state a fight ends in, written back into the spec that starts the next.

    Four fields ``Encounter.state()`` reports were, until now, reportable and not
    settable, so an adventure's second fight began with everyone freshly upright.
    """

    def test_death_save_counts_are_carried_into_the_rebuilt_creature(
        self, registry: ContentRegistry
    ) -> None:
        built = creature_from_spec(
            {**HERO, "hp": 0, "death_saves": {"successes": 2, "failures": 1}}, registry
        )

        assert (built.death_save_successes, built.death_save_failures) == (2, 1)

    def test_absent_death_saves_start_the_counts_at_zero(
        self, registry: ContentRegistry
    ) -> None:
        # The floor under the field above: omitted means a fresh pair of counts,
        # which is what every spec written before this change relied on.
        built = creature_from_spec({**HERO, "hp": 0}, registry)

        assert (built.death_save_successes, built.death_save_failures) == (0, 0)

    def test_a_stabilised_creature_is_rebuilt_stable_and_not_dying(
        self, registry: ContentRegistry
    ) -> None:
        # ``dying`` is derived — ``not dead and hp == 0 and not stable`` — so the
        # field is asserted through the property that reads it. Dropping
        # ``stable`` would not merely lose a detail: it would silently flip a
        # creature somebody stabilised back into one bleeding out.
        built = creature_from_spec({**HERO, "hp": 0, "stable": True}, registry)

        assert built.stable is True
        assert built.dying is False

    def test_the_same_creature_without_the_flag_is_dying(
        self, registry: ContentRegistry
    ) -> None:
        # The control that makes the case above bite: at 0 HP dying is the
        # default, so ``dying is False`` up there can only be ``stable`` doing it.
        built = creature_from_spec({**HERO, "hp": 0}, registry)

        assert built.stable is False
        assert built.dying is True

    def test_a_dead_creature_is_rebuilt_dead(self, registry: ContentRegistry) -> None:
        built = creature_from_spec({**HERO, "hp": 0, "dead": True}, registry)

        assert built.dead is True
        assert built.conscious is False
        # And not dying: a corpse does not roll death saves.
        assert built.dying is False

    def test_a_surrendered_creature_is_rebuilt_surrendered(
        self, registry: ContentRegistry
    ) -> None:
        built = creature_from_spec({**HERO, "surrendered": True}, registry)

        assert built.surrendered is True
        # Asserted through what surrender withdraws the creature from, since the
        # flag alone is inert until something reads it.
        assert built.combat_active is False

    def test_death_saves_that_are_not_an_object_are_refused(
        self, registry: ContentRegistry
    ) -> None:
        with pytest.raises(RequestError, match="death_saves must be an object"):
            creature_from_spec({**HERO, "death_saves": 2}, registry)

    def test_a_negative_death_save_count_is_refused(
        self, registry: ContentRegistry
    ) -> None:
        with pytest.raises(
            RequestError,
            match="death_saves failures must be a whole number of 0 or more",
        ):
            creature_from_spec(
                {**HERO, "death_saves": {"successes": 1, "failures": -1}}, registry
            )

    def test_a_death_save_count_that_is_not_a_number_is_refused(
        self, registry: ContentRegistry
    ) -> None:
        with pytest.raises(
            RequestError,
            match="death_saves successes must be a whole number of 0 or more",
        ):
            creature_from_spec({**HERO, "death_saves": {"successes": "two"}}, registry)

    def test_a_misspelled_death_save_counter_is_refused(
        self, registry: ContentRegistry
    ) -> None:
        # ``.get`` with a default cannot tell "omitted" from "misspelled", so a
        # count nothing reads is named rather than quietly taken as zero.
        with pytest.raises(RequestError, match="unknown death_saves key 'succeses'"):
            creature_from_spec({**HERO, "death_saves": {"succeses": 2}}, registry)

    def test_a_carried_flag_that_is_not_a_boolean_is_refused(
        self, registry: ContentRegistry
    ) -> None:
        # ``bool("false")`` is ``True``. Coercing here would take the one word a
        # caller might write to mean "not stable" and read it as "stable".
        with pytest.raises(RequestError, match="stable must be true or false"):
            creature_from_spec({**HERO, "hp": 0, "stable": "false"}, registry)

    def test_a_misspelled_carry_over_key_is_still_refused(
        self, registry: ContentRegistry
    ) -> None:
        # Widening the allow-list must widen it by exactly four names.
        with pytest.raises(RequestError, match="unknown combatant key 'stabel'"):
            creature_from_spec({**HERO, "hp": 0, "stabel": True}, registry)


class TestTheLookupSpecStaysNarrow:
    """The description keys grew; the lookup keys must not have grown with them.

    The lookup branch returns before the constructor is reached and reads none of
    the description keys, so a key accepted there is a key ignored there.
    """

    def test_a_looked_up_combatant_still_refuses_hp(
        self, registry: ContentRegistry
    ) -> None:
        # ``make_creature`` takes no ``hp``: folding the two sets together would
        # accept this spec and hand back a Goblin Warrior at full health.
        with pytest.raises(RequestError, match="unknown combatant key 'hp'"):
            creature_from_spec({"monster": "Goblin Warrior", "hp": 3}, registry)

    def test_a_looked_up_combatant_still_refuses_a_carried_flag(
        self, registry: ContentRegistry
    ) -> None:
        # The same guard aimed at the four names this change added.
        with pytest.raises(RequestError, match="unknown combatant key 'stable'"):
            creature_from_spec({"monster": "Goblin Warrior", "stable": True}, registry)


class TestAReportedStateStartsTheNextFight:
    def test_the_reported_shape_goes_straight_back_in(self) -> None:
        # The whole point of the four fields: what one fight reported about a
        # combatant is what the next fight's spec is written from, with no
        # reshaping in between. A spec that needed the counts flattened, or the
        # flags renamed, would be a spec every caller gets wrong once.
        down = {
            **HERO,
            "hp": 0,
            "stable": True,
            "death_saves": {"successes": 3, "failures": 1},
        }
        first = api.encounter_create([down, dict(ALLY), dict(GOBLIN)], seed=7)
        reported = _combatant(first, "Thora")

        assert reported["stable"] is True
        assert reported["dying"] is False
        assert reported["dead"] is False
        assert reported["death_saves"] == {"successes": 3, "failures": 1}

        carried = {
            **HERO,
            "hp": 0,
            "stable": reported["stable"],
            "dead": reported["dead"],
            "surrendered": reported["surrendered"],
            "death_saves": reported["death_saves"],
        }
        second = api.encounter_create([carried, dict(ALLY), dict(GOBLIN)], seed=7)

        assert _combatant(second, "Thora") == reported
