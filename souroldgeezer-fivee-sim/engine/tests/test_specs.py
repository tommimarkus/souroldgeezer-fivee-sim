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

import ast
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.content import ContentRegistry, builtin
from fivee_sim.service import specs
from fivee_sim.service.errors import RequestError
from fivee_sim.service.specs import ATTACK_SPEC_KEYS, creature_from_spec

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

    def test_hp_above_max_hp_is_refused(self) -> None:
        # SRD 5.2.1 Rules Glossary: "You can't have more Hit Points than your Hit
        # Point maximum." The engine refuses it at the spec layer, naming both the
        # provided hp and the maximum.
        with pytest.raises(
            RequestError,
            match="combatant Thora: hp 500 cannot exceed max_hp 30",
        ):
            api.encounter_create([{**HERO, "hp": 500}, dict(GOBLIN)])

    def test_the_refusal_falls_between_full_health_and_one_above_it(self) -> None:
        # The boundary, both sides, because the interesting mistake here is an
        # off-by-one rather than a missing check. A `>=` would refuse every
        # combatant at full health — the commonest spec shape in the repo — and
        # `hp: 500` above is far too far from the edge to notice. Without this
        # pair the mutant is still caught, but by two adventure tests whose names
        # point nowhere near the cause.
        full = api.encounter_create([{**HERO, "hp": 30}, dict(GOBLIN)], seed=7)
        assert _combatant(full, "Thora")["hp"] == 30

        with pytest.raises(
            RequestError,
            match="combatant Thora: hp 31 cannot exceed max_hp 30",
        ):
            api.encounter_create([{**HERO, "hp": 31}, dict(GOBLIN)])


class TestConditionImmunitySpec:
    """``condition_immunities`` on an inline spec — the construction path
    separate from ``Creature.from_record``, ``creature_from_spec`` builds a
    combatant straight from an ``encounter.create`` combatant description
    with no pack record behind it at all.
    """

    def test_an_inline_spec_carrying_condition_immunities_builds_a_creature_that_has_it(
        self, registry: ContentRegistry
    ) -> None:
        built = creature_from_spec(
            {**HERO, "condition_immunities": ["poisoned", "frightened"]}, registry
        )

        assert built.condition_immunities == frozenset({"poisoned", "frightened"})

    def test_condition_immunities_defaults_to_empty(
        self, registry: ContentRegistry
    ) -> None:
        built = creature_from_spec(dict(HERO), registry)

        assert built.condition_immunities == frozenset()


class TestInitiativeBonusSpec:
    """``initiative_bonus`` on an inline spec: the same separate construction
    path as ``TestConditionImmunitySpec`` above.
    """

    def test_an_inline_spec_carrying_initiative_bonus_builds_a_creature_that_has_it(
        self, registry: ContentRegistry
    ) -> None:
        built = creature_from_spec({**HERO, "initiative_bonus": 7}, registry)

        assert built.initiative_bonus == 7

    def test_initiative_bonus_defaults_to_none(self, registry: ContentRegistry) -> None:
        built = creature_from_spec(dict(HERO), registry)

        assert built.initiative_bonus is None


class TestSkillBonusesSpec:
    """``skill_bonuses`` on an inline spec: the same separate construction
    path as ``TestConditionImmunitySpec`` above.
    """

    def test_an_inline_spec_carrying_skill_bonuses_builds_a_creature_that_has_it(
        self, registry: ContentRegistry
    ) -> None:
        built = creature_from_spec({**HERO, "skill_bonuses": {"perception": 5}}, registry)

        assert built.skill_bonuses == {"perception": 5}

    def test_skill_bonuses_defaults_to_empty(self, registry: ContentRegistry) -> None:
        built = creature_from_spec(dict(HERO), registry)

        assert built.skill_bonuses == {}


SHORTBOW: dict[str, Any] = {
    "name": "Shortbow", "attack_bonus": 5, "damage": "1d6+3",
    "damage_type": "piercing", "kind": "ranged",
}


class TestAnAttackSpecIsCheckedLikeTheCombatantAroundIt:
    """``reject_unknown_keys`` guarded the combatant and stopped at its attacks.

    An attack is a spec with sixteen optional keys and the same ``.get``-with-a-
    default reads throughout, so it had the same failure the combatant guard was
    written for and none of the protection.
    """

    def test_an_unknown_key_inside_an_attack_names_the_key(self) -> None:
        """``range`` is the key that cost real content.

        The bundled pregen sheets wrote it on every bow, javelin and crossbow
        they ship; the builder reads ``normal_range``. So every one of those
        attacks was constructed with a maximum range of **0 ft** and refused at
        every distance a fight can contain — for as long as the sheets have
        existed, silently, because the combatant around the attack was checked
        and the attack was not.
        """
        with pytest.raises(RequestError, match="unknown attack key 'range'"):
            api.encounter_create(
                [{**HERO, "attacks": [{**SHORTBOW, "range": 80}]}, dict(GOBLIN)]
            )

    def test_the_refusal_is_told_apart_from_the_combatant_one(self) -> None:
        # Both refusals now exist and they name different things. A shared
        # message would send someone hunting for 'range' among the combatant
        # keys, where it is just as absent and not the answer.
        with pytest.raises(RequestError, match="unknown combatant key 'range'"):
            api.encounter_create([{**HERO, "range": 80}, dict(GOBLIN)])

    def test_the_correctly_spelt_ranges_reach_the_built_attack(
        self, registry: ContentRegistry
    ) -> None:
        # The floor under both refusals: a guard that refused every range key
        # would pass the two cases above and leave the bow exactly as broken.
        built = creature_from_spec(
            {**HERO, "attacks": [{**SHORTBOW, "normal_range": 80, "long_range": 320}]},
            registry,
        )
        option = built.attacks[0]

        assert (option.normal_range, option.long_range) == (80, 320)
        # The value the fight actually reads. It was 0, which is why a shortbow
        # answered "cannot reach (35 ft > 0 ft)" to a target 35 ft away.
        assert option.max_distance() == 320

    def test_the_allowed_keys_are_the_keys_the_builder_reads(self) -> None:
        """``ATTACK_SPEC_KEYS`` comes from the dataclass; the builder is separate.

        Deriving the set from :class:`AttackOption` removes one hand-kept list
        and creates the room for another mismatch: a field added to the record
        but never read out of the spec would be *accepted* and then silently
        defaulted — the same defect ``range`` was, one level along. So the two
        are held against each other here, by reading the builder's own source
        rather than by calling it, because only a source read sees a key that
        no fixture happens to set.
        """
        source = ast.parse(Path(specs.__file__).read_text(encoding="utf-8"))
        builder = next(
            node for node in ast.walk(source)
            if isinstance(node, ast.FunctionDef) and node.name == "attack_from_spec"
        )
        read: set[str] = set()
        for node in ast.walk(builder):
            # ``spec["x"]`` — a required key.
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name) and node.value.id == "spec"
                and isinstance(node.slice, ast.Constant)
            ):
                read.add(str(node.slice.value))
            # ``spec.get("x", ...)`` — an optional one.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "spec"
                and node.args and isinstance(node.args[0], ast.Constant)
            ):
                read.add(str(node.args[0].value))

        assert read, "the builder's spec reads could not be found"
        assert read == set(ATTACK_SPEC_KEYS)

    def test_an_attack_that_declares_no_range_is_still_accepted(
        self, registry: ContentRegistry
    ) -> None:
        # Not tightened into a requirement. A melee attack names no range at
        # all, and a ranged one that omits it is a content mistake this layer
        # has no standing to call — ``tests/test_pregens.py`` is where the
        # shipped sheets are held to declaring one.
        built = creature_from_spec({**HERO, "attacks": [dict(SHORTBOW)]}, registry)

        assert built.attacks[0].max_distance() == 0


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
