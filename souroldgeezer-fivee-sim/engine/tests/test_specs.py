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
import json
from pathlib import Path
from typing import Any

import pytest

from fivee_sim.content import ContentRegistry, builtin
from fivee_sim.kernel.grid import TERRAIN
from fivee_sim.map_document import (
    DOOR_ORIENTATIONS,
    MAX_MAP_BYTES,
    MapDocument,
    as_payload,
    parse_document,
)
from fivee_sim.map_types import TerrainPair
from fivee_sim.service import specs
from fivee_sim.service.errors import RequestError
from fivee_sim.service.specs import ATTACK_SPEC_KEYS, creature_from_spec

from . import api

HERO: dict[str, Any] = {"name": "Thora", "team": "party", "ac": 16, "max_hp": 30}
GOBLIN: dict[str, Any] = {"name": "Goblin", "team": "monsters", "ac": 15, "max_hp": 7}
ALLY: dict[str, Any] = {"name": "Bram", "team": "party", "ac": 14, "max_hp": 22}


def _grid_from_spec(spec: dict[str, Any]) -> MapDocument:
    """The map an inline spec produces, which is the map a fight resolves on.

    ``document_from_spec`` is the whole producer and the document is the whole
    artifact — there is no second model behind it any more — so the cases below
    assert what they always asserted about a spec, on the thing a fight is
    actually handed.
    """
    return specs.document_from_spec(spec, TERRAIN)


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


class TestTheModeDecidesWhatARosterMustBe:
    """Two rules the wire edge applies differently for a chapter than for a fight.

    Both exist because a fight's version of them is a statement about *sides* —
    two of them to fight, and a round for a reinforcement to arrive in. An
    interlude has neither, so a rule inherited unexamined refuses something
    legitimate (a lone scout) or accepts something inert (a combatant who can
    never arrive). Each refusal names the mode that refused, because "an
    encounter needs at least two combatants" arriving from a chapter that wants
    one is a sentence a caller cannot act on.
    """

    def test_one_combatant_is_a_whole_interlude(self) -> None:
        created = api.encounter_create([dict(HERO)], seed=7, mode="exploration")

        assert [row["name"] for row in created["state"]["combatants"]] == ["Thora"]

    def test_an_interlude_with_nobody_in_it_is_still_refused(self) -> None:
        with pytest.raises(
            RequestError, match="an interlude needs at least one combatant"
        ):
            api.encounter_create([], mode="exploration")

    def test_a_fight_is_counted_the_way_it_always_was(self) -> None:
        with pytest.raises(
            RequestError, match="an encounter needs at least two combatants"
        ):
            api.encounter_create([dict(HERO)], mode="combat")

    def test_a_reinforcement_has_no_round_to_arrive_in(self) -> None:
        with pytest.raises(
            RequestError,
            match="an interlude has no rounds, so combatant 'Bram' cannot arrive",
        ):
            api.encounter_create(
                [dict(HERO), {**ALLY, "arrival_round": 3}], mode="exploration"
            )

    def test_a_fight_still_takes_a_reinforcement(self) -> None:
        # The floor under the refusal above: nothing a fight could schedule has
        # become harder to schedule.
        created = api.encounter_create(
            [dict(HERO), {**GOBLIN, "arrival_round": 3}], seed=7
        )

        assert _combatant(created, "Goblin")["present"] is False

    def test_a_mode_outside_the_closed_set_names_what_is_accepted(self) -> None:
        # The route table refuses this before the body is reached; ``tests/api``
        # has no adapter in front of it, and neither does the adventure surface
        # that will pass a mode through. So the service owns its own refusal.
        with pytest.raises(RequestError, match="mode must be one of: combat, exploration"):
            api.encounter_create([dict(HERO), dict(GOBLIN)], mode="sneaking")


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


class TestConditionLevelsSpec:
    """``condition_levels`` on an inline spec, overlaid onto ``conditions``."""

    def test_a_level_overlays_onto_a_held_condition(
        self, registry: ContentRegistry
    ) -> None:
        built = creature_from_spec(
            {
                **HERO,
                "conditions": ["poisoned"],
                "condition_levels": {"poisoned": 3},
            },
            registry,
        )

        assert built.conditions == {"poisoned": 3}

    def test_a_condition_with_no_level_stated_defaults_to_one(
        self, registry: ContentRegistry
    ) -> None:
        built = creature_from_spec({**HERO, "conditions": ["poisoned"]}, registry)

        assert built.conditions == {"poisoned": 1}

    @pytest.mark.parametrize("level", [0, -1, -100])
    def test_a_level_below_one_is_refused(
        self, registry: ContentRegistry, level: int
    ) -> None:
        """A held condition is held at level 1 or more, and nothing else.

        Not a tidiness rule.  Every numeric condition effect is applied as
        ``per_level * level``, so a negative level does not weaken a penalty —
        it *inverts* it.  Before this refusal existed, a combatant spec
        carrying ``{"exhaustion": -100}`` produced a creature with +200 on
        every saving throw and 530 feet of walking speed.
        """
        with pytest.raises(RequestError, match="condition_levels.*at least 1"):
            creature_from_spec(
                {
                    **HERO,
                    "conditions": ["poisoned"],
                    "condition_levels": {"poisoned": level},
                },
                registry,
            )

    def test_a_level_naming_a_condition_not_held_is_refused(
        self, registry: ContentRegistry
    ) -> None:
        with pytest.raises(
            RequestError, match="condition_levels names 'frightened'.*not in"
        ):
            creature_from_spec(
                {
                    **HERO,
                    "conditions": ["poisoned"],
                    "condition_levels": {"frightened": 2},
                },
                registry,
            )

    def test_an_explicitly_empty_overlay_is_not_an_error(
        self, registry: ContentRegistry
    ) -> None:
        """Distinct from omitting the key: a round-tripped spec carries ``{}``.

        ``Encounter.state()`` emits ``condition_levels`` unconditionally, so
        every carried combatant arrives with the key present and usually
        empty. Sending it back must be as legal as never having sent it.
        """
        built = creature_from_spec(
            {**HERO, "conditions": ["poisoned"], "condition_levels": {}}, registry
        )

        assert built.conditions == {"poisoned": 1}


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


class TestPassivePerceptionSpec:
    """``passive_perception`` on an inline spec: the same separate
    construction path as ``TestInitiativeBonusSpec`` above, whose shape it
    follows exactly.
    """

    def test_an_inline_spec_carrying_passive_perception_builds_a_creature_that_has_it(
        self, registry: ContentRegistry
    ) -> None:
        built = creature_from_spec({**HERO, "passive_perception": 15}, registry)

        assert built.passive_perception == 15

    def test_passive_perception_defaults_to_none(self, registry: ContentRegistry) -> None:
        built = creature_from_spec(dict(HERO), registry)

        assert built.passive_perception is None


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


class TestAMapSpecCanSayHowItsDoorsHang:
    """``orientation`` and ``linked_to``: the two keys the spec could not say.

    They reach no rule the fight resolves — a door blocks a square the same way
    whichever way it hangs — which is why they went missing for as long as they
    did. What they reach is the *document*: the journal and a v2 replay bundle
    both capture an inline spec as a map document, and the format refuses a door
    that does not say how it hangs, so a spec that cannot express orientation
    produces a fight that cannot be recovered and a bundle that will not parse.

    The vocabulary is the format's, not a second opinion: these tests read
    :data:`DOOR_ORIENTATIONS` off ``map_document`` rather than spelling the two
    words again, so a spec that drifted from the format would fail here.
    """

    @staticmethod
    def _spec(*features: dict[str, Any]) -> dict[str, Any]:
        return {"width": 4, "height": 3, "features": list(features)}

    @classmethod
    def _door(cls, **extra: Any) -> dict[str, Any]:
        """One door, hung vertically unless the case under test says otherwise."""
        feature: dict[str, Any] = {
            "name": "gate", "square": [1, 1], "orientation": "vertical",
        }
        feature.update(extra)
        return cls._spec(feature)

    def test_a_door_may_declare_how_it_hangs(self) -> None:
        built = _grid_from_spec(self._door(orientation="vertical"))
        assert built.fixtures()["gate"].orientation == "vertical"

    def test_every_orientation_the_format_knows_is_accepted(self) -> None:
        for orientation in DOOR_ORIENTATIONS:
            built = _grid_from_spec(self._door(orientation=orientation))
            assert built.fixtures()["gate"].orientation == orientation

    def test_an_orientation_the_format_does_not_know_names_what_was_written(self) -> None:
        # The refusal has to carry the caller's own word back: "must be one of"
        # alone reads identically whether the engine saw 'sideways', a typo, or
        # nothing at all, and the caller is looking for which of their features
        # is wrong.
        with pytest.raises(RequestError, match="got 'sideways'"):
            _grid_from_spec(self._door(orientation="sideways"))

    def test_an_orientation_that_is_not_even_text_is_refused_the_same_way(self) -> None:
        with pytest.raises(RequestError, match="got 90"):
            _grid_from_spec(self._door(orientation=90))

    def test_a_door_may_name_the_leaf_it_swings_with(self) -> None:
        built = _grid_from_spec(self._spec(
            {"name": "left", "square": [1, 1], "orientation": "horizontal",
             "linked_to": "right"},
            {"name": "right", "square": [2, 1], "orientation": "horizontal",
             "linked_to": "left"},
        ))
        assert built.fixtures()["left"].linked_to == "right"
        assert built.fixtures()["right"].linked_to == "left"

    def test_a_linked_leaf_must_be_named_by_non_empty_text(self) -> None:
        with pytest.raises(RequestError, match="linked_to must name a feature"):
            _grid_from_spec(self._door(linked_to=" "))


class TestADoorMustSayHowItHangs:
    """A spec door with no orientation is refused, on ``map.edit``'s own words.

    Not because the fight needs it — it does not — but because everything
    downstream of the fight does. The journal and a v2 replay bundle both write
    the map out as a *document*, and the format refuses a door that does not say
    how it hangs, so a spec the engine accepted quietly produced a fight nobody
    could recover and a bundle nobody could open. Refusing at the point the
    author writes it turns a silent later failure into an immediate one.

    ``service.maps._feature_entry`` has refused exactly this on the ``map.edit``
    surface all along, which is what makes this a correction rather than a new
    rule: two authoring surfaces for one format were disagreeing about whether a
    door needs to hang somewhere.
    """

    def test_a_door_with_no_orientation_is_refused_by_name(self) -> None:
        with pytest.raises(RequestError, match="feature 'gate' is a door, so it needs"):
            _grid_from_spec({
                "width": 4, "height": 3,
                "features": [{"name": "gate", "square": [1, 1], "kind": "door"}],
            })

    def test_the_refusal_offers_the_two_words_that_would_satisfy_it(self) -> None:
        with pytest.raises(RequestError, match="horizontal or vertical"):
            _grid_from_spec({
                "width": 4, "height": 3,
                "features": [{"name": "gate", "square": [1, 1], "kind": "door"}],
            })

    def test_a_feature_naming_no_kind_is_told_that_is_what_made_it_a_door(self) -> None:
        # ``kind`` defaults to "door", so a caller who wrote a lever and left the
        # kind out is refused for a door they never mentioned. The refusal has to
        # say where the door came from, or the only fix it suggests is the wrong
        # one.
        with pytest.raises(RequestError, match="a feature that names no 'kind' is a door"):
            _grid_from_spec({
                "width": 4, "height": 3,
                "features": [{"name": "gate", "square": [1, 1]}],
            })

    def test_a_feature_that_is_not_a_door_needs_no_orientation(self) -> None:
        # The requirement is the format's rule about doors and nothing wider: a
        # lever hangs nowhere, and the document asks it for nothing.
        built = _grid_from_spec({
            "width": 4, "height": 3,
            "features": [{
                "name": "lever", "square": [1, 1], "kind": "lever",
                "closed_terrain": "floor", "open_terrain": "floor",
            }],
        })
        assert built.fixtures()["lever"].orientation is None

    def test_the_two_authoring_surfaces_refuse_a_bare_door_in_the_same_words(self) -> None:
        # The point of the change, asserted rather than described: whatever the
        # spec says here, ``map.edit`` says about the same mistake. Read off the
        # sibling's source so the two cannot drift apart silently.
        source = (
            Path(specs.__file__).parent / "maps.py"
        ).read_text(encoding="utf-8")
        assert "a door needs 'orientation' (horizontal or vertical)" in source

        with pytest.raises(RequestError, match=r"needs 'orientation' \(horizontal or vertical\)"):
            _grid_from_spec({
                "width": 4, "height": 3,
                "features": [{"name": "gate", "square": [1, 1], "kind": "door"}],
            })


class TestAnInlineSpecBuildsADocumentLikeEveryOtherProducer:
    """``document_from_spec``: the spec's own output is now a map document.

    There used to be two ways a map reached a fight — a ``BattleMap`` built
    straight from a spec, and a document parsed from a file — with
    ``replay.battle_map_payload`` re-synthesising a document out of the first so
    the journal and the replay viewer had something to hold. This is the
    collapse: a spec produces the same artifact a file does, and the fake goes
    away with it.

    So every case below is really one claim in two halves. The document a spec
    builds must *say what the spec said*, and it must be a document
    ``parse_document`` accepts — because that document is what the encounter
    journal captures, and a journal holding a map the parser refuses is a fight
    that cannot be recovered.
    """

    @staticmethod
    def _built(spec: dict[str, Any]) -> MapDocument:
        return specs.document_from_spec(spec, TERRAIN)

    @classmethod
    def _reparsed(cls, spec: dict[str, Any]) -> MapDocument:
        """What journal recovery does to the document: write it out, read it back."""
        return parse_document(
            as_payload(cls._built(spec)), source="journal", terrain=TERRAIN
        )

    def test_rows_and_a_legend_become_dense_tiles_over_the_same_squares(self) -> None:
        built = self._built({
            "name": "room", "width": 3, "height": 2,
            "default_terrain": "floor",
            "rows": ["###", "#.#"],
            "legend": {"#": "wall", ".": "floor"},
        })

        assert built.ground.terrain_at((1, 1), built.legend) == "floor"
        assert built.ground.terrain_at((0, 0), built.legend) == "wall"

    def test_an_authors_own_glyphs_survive_into_the_document(self) -> None:
        # A legend a person wrote is theirs. Reallocating it would make the
        # captured document unreadable beside the spec that produced it.
        built = self._built({
            "width": 2, "height": 1, "default_terrain": "floor",
            "rows": ["W."], "legend": {"W": "wall", ".": "floor"},
        })

        assert built.legend == {"W": "wall", ".": "floor"}
        assert built.tiles == ("W.",)

    def test_a_glyph_the_document_reserves_is_the_one_thing_moved(self) -> None:
        # A spec may legally spell wall '+'; the document format may not, because
        # the renderers draw doors with it. Left alone, this produced a captured
        # document the parser refused.
        built = self._built({
            "width": 2, "height": 1, "default_terrain": "floor",
            "rows": ["+."], "legend": {"+": "wall", ".": "floor"},
        })

        assert "+" not in built.legend
        assert built.legend["."] == "floor"
        assert set(built.legend.values()) == {"wall", "floor"}
        assert self._reparsed({
            "width": 2, "height": 1, "default_terrain": "floor",
            "rows": ["+."], "legend": {"+": "wall", ".": "floor"},
        }).tiles == built.tiles

    def test_a_door_crosses_with_everything_the_format_demands_of_one(self) -> None:
        built = self._reparsed({
            "width": 3, "height": 1,
            "features": [{
                "name": "gate", "square": [1, 0], "orientation": "vertical",
                "initially_open": True,
            }],
        })

        gate = next(one for one in built.features if one.id == "gate")
        assert (gate.kind, gate.orientation, gate.state) == ("door", "vertical", "open")
        assert gate.terrain == TerrainPair(closed="door-closed", open="door-open")

    def test_a_linked_pair_survives_the_write_and_the_read_back(self) -> None:
        built = self._reparsed({
            "width": 4, "height": 1,
            "features": [
                {"name": "left", "square": [1, 0], "orientation": "horizontal",
                 "linked_to": "right"},
                {"name": "right", "square": [2, 0], "orientation": "horizontal",
                 "linked_to": "left"},
            ],
        })

        assert {one.id: one.linked_to for one in built.features} == {
            "left": "right", "right": "left"
        }

    def test_heights_cross_as_the_documents_datum_and_departures(self) -> None:
        built = self._reparsed({
            "width": 3, "height": 1,
            "default_elevation": 10, "elevation": [[2, 0, 25]],
        })

        assert built.elevation.default == 10
        assert built.elevation.at((2, 0)) == 25


class TestAnUnknownTerrainKindIsRefusedInTheCallersOwnWords:
    """The refusal that must not become the document parser's.

    Handing a synthesised payload to ``parse_document`` would answer an unknown
    kind with *glyph 'a' names terrain 'lava'* — a glyph the caller never wrote,
    at 422, about a document they never sent. The spec layer owns this refusal
    because the spec layer is where the caller's own word for it is still
    available.
    """

    def test_an_unknown_default_terrain_names_the_kind_and_lists_what_is_loaded(
        self,
    ) -> None:
        with pytest.raises(RequestError, match="does not define: lava. Defined:"):
            specs.document_from_spec(
                {"width": 2, "height": 1, "default_terrain": "lava"}, TERRAIN
            )

    def test_an_unknown_kind_in_a_legend_the_rows_use_is_refused_the_same_way(
        self,
    ) -> None:
        with pytest.raises(RequestError, match="does not define: lava"):
            specs.document_from_spec(
                {
                    "width": 2, "height": 1, "default_terrain": "floor",
                    "rows": ["L."], "legend": {"L": "lava", ".": "floor"},
                },
                TERRAIN,
            )

    def test_an_unknown_kind_in_a_terrain_entry_is_refused_the_same_way(self) -> None:
        with pytest.raises(RequestError, match="does not define: lava"):
            specs.document_from_spec(
                {
                    "width": 2, "height": 1,
                    "terrain": [{"kind": "lava", "squares": [[0, 0]]}],
                },
                TERRAIN,
            )

    def test_an_unknown_kind_a_fixture_names_is_refused_the_same_way(self) -> None:
        with pytest.raises(RequestError, match="does not define: lava"):
            specs.document_from_spec(
                {
                    "width": 2, "height": 1,
                    "features": [{
                        "name": "vent", "square": [0, 0], "kind": "vent",
                        "closed_terrain": "floor", "open_terrain": "lava",
                    }],
                },
                TERRAIN,
            )

    def test_the_refusal_names_no_glyph_because_the_caller_wrote_none(self) -> None:
        # The regression this class exists for: a message about a synthesised
        # glyph sends the author looking for something they did not write.
        with pytest.raises(RequestError, match="does not define: lava") as raised:
            specs.document_from_spec(
                {"width": 2, "height": 1, "default_terrain": "lava"}, TERRAIN
            )
        assert "glyph" not in str(raised.value)

    def test_the_engine_still_refuses_it_end_to_end_at_the_same_status(self) -> None:
        # Through the operation, not just the parser: an unknown kind has always
        # been a 400 about the request, and moving where it is caught must not
        # make it a 422 about a document.
        with pytest.raises(RequestError, match="does not define: lava"):
            api.encounter_create(
                [dict(HERO), dict(GOBLIN)], seed=5,
                map={"width": 4, "height": 4, "default_terrain": "lava"},
            )


class TestASpecTheEngineAcceptsStaysAcceptable:
    """The bounds question, answered rather than assumed.

    ``specs.MAX_MAP_SQUARES`` caps each side at 512 and the document format caps
    a serialised map at ``MAX_MAP_BYTES``. Densifying tiles is what puts the two
    in the same sentence for the first time: a spec that named three walls now
    writes a full grid of glyphs. These pin that the largest spec the dimension
    check admits is still a document the format admits.
    """

    def test_the_largest_grid_the_dimension_check_admits_is_well_under_the_cap(
        self,
    ) -> None:
        # Measured, not assumed: 512x512 of dense glyphs writes out to about
        # 265 KB, six percent of the cap. Densifying tiles is not what can put a
        # spec over it, which is why nothing here refuses a spec for its size
        # alone.
        side = specs.MAX_MAP_SQUARES
        built = specs.document_from_spec(
            {"width": side, "height": side, "default_terrain": "floor"}, TERRAIN
        )

        size = len(json.dumps(as_payload(built), ensure_ascii=False).encode("utf-8"))
        assert size < MAX_MAP_BYTES // 4, size
        assert parse_document(as_payload(built), source="cap", terrain=TERRAIN)

    def test_a_spec_that_would_not_fit_is_refused_in_bytes_and_refused_here(self) -> None:
        # The one way past the dimension check, and it is reachable: a height per
        # square at a height wide enough to spell. Such a spec was accepted
        # before, ran its fight, and then failed to come back from its own
        # journal, because the document it wrote there is one the parser refuses
        # to read. The refusal belongs where the spec still is.
        side = specs.MAX_MAP_SQUARES
        with pytest.raises(RequestError, match="over the 4194304 byte limit"):
            specs.document_from_spec(
                {
                    "width": side, "height": side, "default_terrain": "floor",
                    "elevation": [
                        [x, y, 1000000] for y in range(side) for x in range(side)
                    ],
                },
                TERRAIN,
            )
