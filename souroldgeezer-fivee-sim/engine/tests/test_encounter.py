"""Encounter tests: initiative, turns, damage, reactions, spell resources.

Because the generator is passed to each call rather than held by the encounter,
these tests build a fight with an ordinary seed and then resolve a specific action
with a forced generator. That is how a single attack's outcome gets pinned without
contriving the whole fight.
"""

from __future__ import annotations

from collections.abc import Sequence
from random import Random
from types import MappingProxyType
from typing import Any

import pytest

from fivee_sim.content import item_effects, make_monster, spellbook
from fivee_sim.kernel.actions import AttackKind
from fivee_sim.kernel.conditions import EFFECTS, Condition, ConditionEffect, UnknownCondition
from fivee_sim.kernel.dice import Advantage, Dice
from fivee_sim.kernel.grid import (
    CoverGrade,
    DiagonalRule,
    MovementMode,
    Point,
    Square,
    as_point,
    square_center,
    to_square,
)
from fivee_sim.kernel.items import ActionCost, ItemEffect
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.kernel.spells import Spell
from fivee_sim.map_types import (
    FeatureCheck,
    FeatureTrigger,
    HeightPair,
    MapDocument,
    MapElevation,
    MapFeatureRecord,
    MapGrid,
    MapLevel,
    MapOverlayRecord,
    MapProvenance,
    TerrainPair,
    TriggerMode,
)
from fivee_sim.model.creature import AttackOption, Creature
from fivee_sim.model.encounter import (
    MAX_LISTED_COMBATANTS,
    Action,
    ActionKind,
    Encounter,
    EncounterError,
    EncounterMode,
    Event,
)

from .conftest import (
    FIXTURE,
    FixedRandom,
    ScriptedRandom,
    advance_to,
    caster,
    fighter,
    fixture_provenance,
    shaped_spellbook,
    shaper,
)


def kinds(events: Sequence[Event]) -> list[str]:
    return [event.kind for event in events]


def detail_of(events: Sequence[Event], kind: str) -> str:
    """The detail of the one event of ``kind``, asserting there is exactly one."""
    matching = [event for event in events if event.kind == kind]
    assert len(matching) == 1, f"expected one {kind!r} event, got {len(matching)}"
    return matching[0].detail


def rolled_with(event: Event) -> str:
    """The Advantage state the d20 in this event was rolled under.

    Matched on the ``describe()`` token rather than by substring, because
    ``"advantage" in "disadvantage"`` is true and a substring test would pass
    whichever way the roll actually went. Every assertion about the state a d20
    was rolled under goes through here — attack rolls and saving throws alike,
    since both render the same ``[faces] <state> ->`` shape.
    """
    for state in ("disadvantage", "advantage"):
        if f"] {state} ->" in event.detail:
            return state
    return "none"


class TestInitiative:
    def test_the_same_seed_produces_the_same_order(self) -> None:
        first = Encounter([fighter(), make_monster("Wolf")], Random(7))
        second = Encounter([fighter(), make_monster("Wolf")], Random(7))
        assert first.order == second.order

    def test_a_poisoned_creature_rolls_initiative_with_disadvantage(self) -> None:
        poisoned = fighter()
        poisoned.add_condition(Condition.POISONED)
        observer = fighter("Observer", team="foes")
        encounter = Encounter(
            [poisoned, observer],
            # Poisoned keeps 1 from 20/1; Observer then rolls 10 normally.
            ScriptedRandom([20, 1, 10]),
        )
        assert encounter.initiative == {"Thora": 3, "Observer": 12}

    def test_an_incapacitated_creature_rolls_initiative_with_disadvantage(self) -> None:
        incapacitated = fighter()
        incapacitated.add_condition(Condition.INCAPACITATED)
        observer = fighter("Observer", team="foes")
        encounter = Encounter(
            [incapacitated, observer],
            # Incapacitated keeps 1 from 20/1; Observer then rolls 10 normally.
            ScriptedRandom([20, 1, 10]),
        )
        assert encounter.initiative == {"Thora": 3, "Observer": 12}

    def test_an_invisible_creature_rolls_initiative_with_advantage(self) -> None:
        invisible = fighter()
        invisible.add_condition(Condition.INVISIBLE)
        observer = fighter("Observer", team="foes")
        encounter = Encounter(
            [invisible, observer],
            # Invisible keeps 20 from 1/20; Observer then rolls 10 normally.
            ScriptedRandom([1, 20, 10]),
        )
        assert encounter.initiative == {"Thora": 22, "Observer": 12}

    def test_ties_break_on_name_when_dexterity_matches(self) -> None:
        # A forced generator gives everyone the same d20, and identical Dexterity
        # leaves only the name to separate them — never randomness.
        encounter = Encounter(
            [fighter("Bravo", team="a"), fighter("Alpha", team="b")],
            FixedRandom(10),
        )
        assert encounter.order == ["Alpha", "Bravo"]

    def test_a_printed_initiative_bonus_replaces_the_dexterity_modifier(self) -> None:
        # SRD 5.2.1, Initiative: the stat block's printed Initiative line is the
        # authority. Aboleth prints +7 against a −1 Dexterity modifier — this is
        # that shape, not the bundled six, none of which differ.
        printed = fighter()
        printed.initiative_bonus = 7
        observer = fighter("Observer", team="foes")
        encounter = Encounter([printed, observer], FixedRandom(10))
        # Dexterity modifier would have given 12; the printed bonus gives 17.
        assert encounter.initiative == {"Thora": 17, "Observer": 12}

    def test_a_creature_with_no_printed_bonus_rolls_exactly_as_it_did_before(
        self,
    ) -> None:
        # Regression pin: ``initiative_bonus`` defaults to ``None`` and falls back
        # to the Dexterity modifier, unchanged.
        plain = fighter()
        assert plain.initiative_bonus is None
        encounter = Encounter([plain, fighter("Observer", team="foes")], FixedRandom(10))
        assert encounter.initiative == {"Thora": 12, "Observer": 12}

    def test_a_printed_bonus_of_zero_is_honoured_and_not_treated_as_absent(
        self,
    ) -> None:
        # ``0`` is a legitimate printed bonus and must be distinguishable from
        # "not stated" — fighter's Dexterity modifier is +2, so a fallback would
        # give 12 rather than the printed 10.
        zeroed = fighter()
        zeroed.initiative_bonus = 0
        encounter = Encounter([zeroed, fighter("Observer", team="foes")], FixedRandom(10))
        assert encounter.initiative == {"Thora": 10, "Observer": 12}

    def test_the_tie_break_still_reads_dexterity_even_under_a_printed_bonus(
        self,
    ) -> None:
        # The SRD tie-break is Dexterity modifier in its own right, not a
        # stand-in for the printed bonus — it must keep reading Dexterity even
        # for a creature whose total came from a printed bonus instead.
        plain = fighter("Alpha", team="a")  # Dexterity 14, modifier +2.
        printed = fighter("Bravo", team="b")
        printed.abilities[Ability.DEXTERITY] = 18  # Modifier +4: would win ties.
        printed.initiative_bonus = 2  # Ties the total with Alpha's +2 modifier.
        encounter = Encounter([plain, printed], FixedRandom(10))
        assert encounter.initiative == {"Alpha": 12, "Bravo": 12}
        assert encounter.order == ["Bravo", "Alpha"]

    def test_an_encounter_needs_two_combatants(self) -> None:
        with pytest.raises(EncounterError, match="at least two"):
            Encounter([fighter()], Random(1))

    def test_duplicate_names_are_refused(self) -> None:
        with pytest.raises(EncounterError, match="unique"):
            Encounter([fighter("Same"), fighter("Same", team="b")], Random(1))


class TestRulingConditions:
    """A condition the table imposes, with nothing in the rules to end it.

    Every other condition here arrives from something that models its own
    ending: a spell holds it under concentration, an attack rider anchors it to
    a turn boundary, prone ends by standing. A ruling has none of those, so it
    registers no ongoing effect and lasts until the table lifts it.

    Until this existed a condition was effectively write-once — a combatant
    could start a fight carrying one and nothing could ever take it off.
    """

    def test_a_ruling_imposes_and_lifts_a_condition(self) -> None:
        encounter = Encounter([fighter(), make_monster("Wolf")], Random(7))

        encounter.set_condition("Thora", Condition.POISONED, applied=True)
        assert Condition.POISONED in encounter.creatures["Thora"].conditions

        encounter.set_condition("Thora", Condition.POISONED, applied=False)
        assert Condition.POISONED not in encounter.creatures["Thora"].conditions

    def test_a_ruling_registers_no_ongoing_effect(self) -> None:
        # The distinction that makes it a ruling: nothing sustains it, so nothing
        # can expire it or break it by losing concentration.
        encounter = Encounter([fighter(), make_monster("Wolf")], Random(7))
        encounter.set_condition("Thora", Condition.POISONED, applied=True)

        assert encounter.state()["ongoing_effects"] == []

    def test_lifting_is_recorded_in_the_log(self) -> None:
        encounter = Encounter([fighter(), make_monster("Wolf")], Random(7))
        encounter.set_condition("Thora", Condition.POISONED, applied=True)
        encounter.set_condition("Thora", Condition.POISONED, applied=False)

        kinds = [event.kind for event in encounter.log if event.target == "Thora"]
        assert kinds[-2:] == ["effect_apply", "effect_end"]

    def test_lifting_also_ends_the_spell_effect_sustaining_the_condition(self) -> None:
        # The branch the class docstring promises and nothing else reaches.
        # Without it the ledger still holds an effect naming a condition the
        # creature no longer has, so the spell's grip outlives the ruling meant
        # to end it — and the next thing to consult the ledger reimposes it.
        wren = caster(position=0)
        victim = fighter("Bandit0", team="foes", position=10)
        victim.abilities[Ability.WISDOM] = 6
        rng = Random(11)
        encounter = Encounter([wren, victim], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target="Bandit0"),
            FixedRandom(1),
        )
        assert Condition.PARALYZED in victim.conditions
        assert encounter.state()["ongoing_effects"] != []

        encounter.set_condition("Bandit0", Condition.PARALYZED, applied=False)

        assert Condition.PARALYZED not in victim.conditions
        assert encounter.state()["ongoing_effects"] == []

    def test_lifting_leaves_an_unrelated_effect_alone(self) -> None:
        # And only that condition: a ruling is a scalpel, not a dispel.
        wren = caster(position=0)
        victim = fighter("Bandit0", team="foes", position=10)
        victim.abilities[Ability.WISDOM] = 6
        rng = Random(11)
        encounter = Encounter([wren, victim], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target="Bandit0"),
            FixedRandom(1),
        )

        encounter.set_condition("Bandit0", Condition.POISONED, applied=False)

        assert Condition.PARALYZED in victim.conditions
        assert encounter.state()["ongoing_effects"] != []

    def test_an_unknown_condition_is_refused(self) -> None:
        encounter = Encounter([fighter(), make_monster("Wolf")], Random(7))
        with pytest.raises(UnknownCondition, match="no condition named 'bewildered'"):
            encounter.set_condition("Thora", "bewildered", applied=True)

    def test_an_unknown_combatant_is_refused(self) -> None:
        encounter = Encounter([fighter(), make_monster("Wolf")], Random(7))
        with pytest.raises(EncounterError, match="no combatant named 'Nobody'"):
            encounter.set_condition("Nobody", Condition.POISONED, applied=True)

    def test_a_ruling_is_refused_by_immunity_and_says_so(self) -> None:
        # The GM path is the fourth funnel into ``Creature.add_condition``: a
        # ruling on an immune combatant must be refused exactly as an attack
        # rider, a spell or an item's condition would be.
        immune = fighter()
        immune.condition_immunities = frozenset({Condition.POISONED})
        encounter = Encounter([immune, make_monster("Wolf")], Random(7))

        encounter.set_condition("Thora", Condition.POISONED, applied=True)

        assert Condition.POISONED not in encounter.creatures["Thora"].conditions
        applied = next(event for event in encounter.log if event.kind == "effect_apply")
        assert applied.data["applied"] is False
        assert applied.data["condition"] == Condition.POISONED
        assert "immune" in applied.detail


class TestConditionLevels:
    """The level machinery: SRD 5.2.1 p.179's Exhaustion exception, generalised
    onto the effect row as ``cumulative`` rather than keyed on a name.

    Both numeric effects exist now (T10c, T10d); the bundled Exhaustion row
    does not yet. This pins only that a condition can be *held* at a level,
    and that the level survives every checkpoint a fight's state passes
    through.
    """

    #: A pack-declared cumulative condition, not an SRD one — the acceptance
    #: check is deliberately not run against a bundled row.
    TABLE = dict(EFFECTS) | {
        "marked": ConditionEffect(cumulative=True),
    }

    @pytest.mark.parametrize("levels", [0, -1, -100])
    def test_imposing_fewer_than_one_level_is_refused(self, levels: int) -> None:
        """``Creature.conditions`` values are documented as always 1 or more.

        The invariant is load-bearing rather than tidy: every numeric effect
        resolves as ``per_level * level``, so a negative level inverts the sign
        of the thing it scales. An Exhaustion level of -100 does not make a
        creature slightly less tired — it grants +200 on every D20 Test and
        530 feet of Speed. ``add_condition`` is the documented chokepoint every
        imposing path funnels through, so the floor belongs here.
        """
        target = fighter("Thora")
        target.condition_effects = self.TABLE

        with pytest.raises(ValueError, match="levels must be at least 1"):
            target.add_condition("marked", levels=levels)

        assert target.conditions == {}

    @pytest.mark.parametrize("levels", [0, -1])
    def test_removing_fewer_than_one_level_is_refused(self, levels: int) -> None:
        """The mirror of the floor above, for the same reason.

        ``remove_condition(levels=None)`` still means *drop it outright* — that
        is the default every existing caller relies on. It is a stated count
        below one that is refused, because subtracting a negative would raise
        the level through the path that exists to lower it.
        """
        target = fighter("Thora")
        target.condition_effects = self.TABLE
        target.add_condition("marked", levels=2)

        with pytest.raises(ValueError, match="levels must be at least 1"):
            target.remove_condition("marked", levels=levels)

        assert target.conditions == {"marked": 2}

    def test_three_impositions_reach_level_three(self) -> None:
        target = fighter("Thora")
        target.condition_effects = self.TABLE

        target.add_condition("marked")
        target.add_condition("marked")
        target.add_condition("marked")

        assert target.level_of("marked") == 3
        assert target.conditions["marked"] == 3

    def test_a_non_cumulative_condition_stays_at_one_on_reimposition(self) -> None:
        target = fighter("Thora")
        target.add_condition(Condition.POISONED)
        target.add_condition(Condition.POISONED)

        assert target.level_of(Condition.POISONED) == 1

    def test_remove_condition_with_no_levels_argument_removes_outright(self) -> None:
        target = fighter("Thora")
        target.condition_effects = self.TABLE
        target.add_condition("marked")
        target.add_condition("marked")

        target.remove_condition("marked")

        assert target.level_of("marked") == 0
        assert "marked" not in target.conditions

    def test_remove_condition_with_levels_decrements(self) -> None:
        target = fighter("Thora")
        target.condition_effects = self.TABLE
        target.add_condition("marked", levels=3)

        target.remove_condition("marked", levels=1)

        assert target.level_of("marked") == 2

    def test_remove_condition_drops_the_entry_once_it_reaches_zero(self) -> None:
        target = fighter("Thora")
        target.condition_effects = self.TABLE
        target.add_condition("marked")

        target.remove_condition("marked", levels=1)

        assert "marked" not in target.conditions

    def test_level_of_is_zero_when_not_held(self) -> None:
        target = fighter("Thora")
        assert target.level_of("marked") == 0

    def test_condition_levels_is_empty_for_a_fight_with_no_leveled_condition(
        self,
    ) -> None:
        encounter = Encounter([fighter(), make_monster("Wolf")], Random(7))
        state = encounter.state()
        for combatant in state["combatants"]:
            assert combatant["condition_levels"] == {}

    def test_srd_conditions_serialise_byte_identically(self) -> None:
        # The invariant this step must not disturb: every one of the 14 SRD
        # conditions still serialises to exactly the same state shape it did
        # before condition_levels existed, aside from the new key itself.
        encounter = Encounter([fighter(), make_monster("Wolf")], Random(7))
        for name in Condition:
            encounter.set_condition("Thora", name, applied=True)
        state = encounter.state()
        held = next(c for c in state["combatants"] if c["name"] == "Thora")
        assert held["conditions"] == sorted(str(c) for c in Condition)
        assert held["condition_levels"] == {}

    def test_a_leveled_condition_reaches_encounter_state(self) -> None:
        encounter = Encounter(
            [fighter(), make_monster("Wolf")], Random(7), condition_effects=self.TABLE
        )
        thora = encounter.creatures["Thora"]
        thora.add_condition("marked")
        thora.add_condition("marked")
        thora.add_condition("marked")

        state = encounter.state()
        held = next(c for c in state["combatants"] if c["name"] == "Thora")
        assert held["condition_levels"] == {"marked": 3}
        assert "marked" in held["conditions"]

    def test_a_ruling_can_impose_more_than_one_level(self) -> None:
        encounter = Encounter(
            [fighter(), make_monster("Wolf")], Random(7), condition_effects=self.TABLE
        )
        encounter.set_condition("Thora", "marked", applied=True, levels=3)

        assert encounter.creatures["Thora"].level_of("marked") == 3


class TestD20TestPenalty:
    """SRD 5.2.1 p.180: a condition that "affects D20 Tests" affects ability
    checks, attack rolls, and saving throws alike, so the penalty is folded
    into the accessors every one of those roll-assembly sites reads from
    rather than re-applied at each site.
    """

    #: A pack-declared, cumulative, leveled condition — not an SRD one, the
    #: same posture ``TestConditionLevels.TABLE`` takes.
    TABLE = dict(EFFECTS) | {
        "weary": ConditionEffect(d20_test_penalty_per_level=2, cumulative=True),
    }

    def weary(self, levels: int = 2, **kwargs: Any) -> Creature:
        target = fighter(**kwargs)
        target.condition_effects = self.TABLE
        target.add_condition("weary", levels=levels)
        return target

    def test_save_modifier_subtracts_the_penalty(self) -> None:
        target = self.weary()
        assert target.save_modifier(Ability.CONSTITUTION) == (
            target.ability_mod(Ability.CONSTITUTION) - 4
        )

    def test_check_modifier_subtracts_the_penalty(self) -> None:
        target = self.weary()
        assert target.check_modifier(Ability.WISDOM) == (
            target.ability_mod(Ability.WISDOM) - 4
        )

    def test_attack_modifier_subtracts_the_penalty(self) -> None:
        target = self.weary()
        assert target.attack_modifier(5) == 1

    def test_an_unafflicted_creature_is_unchanged(self) -> None:
        target = fighter()
        assert target.attack_modifier(5) == 5
        assert target.save_modifier(Ability.CONSTITUTION) == target.ability_mod(
            Ability.CONSTITUTION
        )
        assert target.check_modifier(Ability.WISDOM) == target.ability_mod(
            Ability.WISDOM
        )

    def test_a_weary_attackers_attack_roll_carries_the_penalty(self) -> None:
        # attack_bonus is 5, so a natural 10 lands as 15 unafflicted and 11 weary.
        attacker = self.weary(name="Thora")
        target = fighter(name="Target", position=5, team="monsters")
        rng = Random(3)
        encounter = Encounter([attacker, target], rng, condition_effects=self.TABLE)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Target"), FixedRandom(10)
        )
        attack = next(e for e in events if e.kind == "attack")
        assert attack.data["natural"] == 10
        assert attack.data["total"] == 11

    def test_initiative_carries_the_penalty(self) -> None:
        weary_creature = fighter(name="Thora")
        weary_creature.condition_effects = self.TABLE
        weary_creature.add_condition("weary", levels=2)
        other = fighter(name="Other", team="monsters")
        # FixedRandom clamps every d20 to the same natural, so both roll the
        # same face and only the penalty tells them apart.
        encounter = Encounter(
            [weary_creature, other], FixedRandom(10), condition_effects=self.TABLE
        )
        dex_mod = weary_creature.ability_mod(Ability.DEXTERITY)
        assert encounter.initiative["Thora"] == 10 + dex_mod - 4
        assert encounter.initiative["Other"] == 10 + other.ability_mod(Ability.DEXTERITY)

    def test_a_death_save_carries_the_penalty(self) -> None:
        weary_creature = fighter(name="Thora", hp=0)
        weary_creature.condition_effects = self.TABLE
        weary_creature.add_condition("weary", levels=2)
        encounter = Encounter(
            [weary_creature, fighter(name="Other", team="monsters")],
            Random(9),
            condition_effects=self.TABLE,
        )
        # A natural 14 would ordinarily succeed (DC 10); the -4 penalty drops
        # the total to 10, which still succeeds — 13 fails only with the
        # penalty applied.
        encounter._death_save(weary_creature, FixedRandom(13))
        event = next(e for e in encounter.log if e.kind == "death_save")
        assert event.data["natural"] == 13
        assert "9 vs DC 10 — failure" in event.detail
        assert weary_creature.death_save_failures == 1

    def test_a_death_save_detail_is_byte_identical_with_no_penalty(self) -> None:
        target = fighter(name="Thora", hp=0)
        encounter = Encounter([target, fighter(name="Other", team="monsters")], Random(9))
        encounter._death_save(target, FixedRandom(13))
        event = next(e for e in encounter.log if e.kind == "death_save")
        assert event.detail == "13 vs DC 10 — success"


class TestSpeedReduction:
    """SRD 5.2.1, Exhaustion: "Your Speed is reduced by a number of feet
    equal to 5 times your Exhaustion level." Grappled's identical wording
    ("Your Speed is 0") already reaches every movement mode in this engine —
    see ``_do_move``'s unconditional refusal — so the ruling this pins is
    that a numeric reduction reaches every mode too, not the walking Speed
    alone.
    """

    #: A pack-declared, cumulative, leveled condition — never an SRD one.
    TABLE = dict(EFFECTS) | {
        "weary": ConditionEffect(speed_reduction_feet_per_level=5, cumulative=True),
    }

    def weary(self, levels: int = 2, **kwargs: Any) -> Creature:
        target = fighter(**kwargs)
        target.climb_speed = 20
        target.swim_speed = 20
        target.fly_speed = 30
        target.burrow_speed = 10
        target.condition_effects = self.TABLE
        target.add_condition("weary", levels=levels)
        return target

    def test_speed_for_reduces_every_movement_mode(self) -> None:
        target = self.weary()  # -10 ft
        assert target.speed_for(MovementMode.WALK) == 20
        assert target.speed_for(MovementMode.CLIMB) == 10
        assert target.speed_for(MovementMode.SWIM) == 10
        assert target.speed_for(MovementMode.FLY) == 20
        assert target.speed_for(MovementMode.BURROW) == 0

    def test_speed_for_clamps_at_zero_never_negative(self) -> None:
        target = self.weary(levels=10)  # -50 ft, dwarfing every printed speed
        for mode in MovementMode:
            assert target.speed_for(mode) == 0

    def test_an_unafflicted_creature_is_unchanged(self) -> None:
        target = fighter()
        target.climb_speed = 20
        assert target.speed_for(MovementMode.WALK) == target.speed
        assert target.speed_for(MovementMode.CLIMB) == target.climb_speed

    def test_begin_turn_grants_the_reduced_movement_budget(self) -> None:
        weary_creature = self.weary(name="Thora")  # -10 ft; fly 30 -> 20 is the max
        encounter = Encounter(
            [weary_creature, fighter(name="Other", team="monsters")],
            Random(9),
            condition_effects=self.TABLE,
        )
        advance_to(encounter, "Thora", Random(9))
        assert encounter._turn.movement_left == 20

    def test_movement_speed_reads_the_reduced_budget(self) -> None:
        weary_creature = self.weary(name="Thora")
        encounter = Encounter(
            [weary_creature, fighter(name="Other", team="monsters")],
            Random(9),
            condition_effects=self.TABLE,
        )
        assert encounter._movement_speed(weary_creature, MovementMode.CLIMB) == 10

    def test_movement_speed_refuses_a_mode_reduced_to_zero(self) -> None:
        weary_creature = self.weary(name="Thora")
        encounter = Encounter(
            [weary_creature, fighter(name="Other", team="monsters")],
            Random(9),
            condition_effects=self.TABLE,
        )
        with pytest.raises(EncounterError, match="no burrow speed"):
            encounter._movement_speed(weary_creature, MovementMode.BURROW)

    def test_stand_cost_halves_the_reduced_speed(self) -> None:
        weary_creature = self.weary(name="Thora")  # walk 30 -> 20
        encounter = Encounter(
            [weary_creature, fighter(name="Other", team="monsters")],
            Random(9),
            condition_effects=self.TABLE,
        )
        assert encounter.stand_cost("Thora") == 10

    def test_can_stand_reads_the_reduced_speed(self) -> None:
        weary_creature = self.weary(name="Thora", levels=10)  # walk reduced to 0
        weary_creature.add_condition(Condition.PRONE)
        encounter = Encounter(
            [weary_creature, fighter(name="Other", team="monsters")],
            Random(9),
            condition_effects=self.TABLE,
        )
        advance_to(encounter, "Thora", Random(9))
        assert not encounter.can_stand("Thora")

    def test_do_stand_refuses_a_creature_reduced_to_speed_zero(self) -> None:
        weary_creature = self.weary(name="Thora", levels=10)  # walk reduced to 0
        weary_creature.add_condition(Condition.PRONE)
        encounter = Encounter(
            [weary_creature, fighter(name="Other", team="monsters")],
            Random(9),
            condition_effects=self.TABLE,
        )
        advance_to(encounter, "Thora", Random(9))
        with pytest.raises(EncounterError, match="speed of 0 and cannot stand"):
            encounter._do_stand(weary_creature)

    def test_creature_state_speeds_reports_the_reduced_budget(self) -> None:
        weary_creature = self.weary(name="Thora")
        encounter = Encounter(
            [weary_creature, fighter(name="Other", team="monsters")],
            Random(9),
            condition_effects=self.TABLE,
        )
        speeds = encounter._creature_state(weary_creature)["speeds"]
        assert speeds == {"walk": 20, "climb": 10, "swim": 10, "fly": 20, "burrow": 0}

    def test_creature_state_speeds_are_unchanged_for_an_unafflicted_creature(
        self,
    ) -> None:
        target = fighter(name="Thora")
        encounter = Encounter(
            [target, fighter(name="Other", team="monsters")], Random(9)
        )
        speeds = encounter._creature_state(target)["speeds"]
        assert speeds == {"walk": 30, "climb": 0, "swim": 0, "fly": 0, "burrow": 0}


class TestExhaustionRow:
    """Exhaustion (SRD 5.2.1 p.181) as the bundled fifteenth condition: the
    D20 Test penalty and Speed reduction channels already reach every
    consumer (T10c, T10d); this pins that Exhaustion's own row wires them,
    and that reaching level 6 kills — visibly.
    """

    def test_level_two_costs_four_on_every_d20_test_and_ten_feet_of_speed(
        self,
    ) -> None:
        target = fighter(name="Thora")
        target.add_condition(Condition.EXHAUSTION, levels=2)
        assert target.attack_modifier(5) == 1
        assert target.save_modifier(Ability.CONSTITUTION) == (
            target.ability_mod(Ability.CONSTITUTION) - 4
        )
        assert target.check_modifier(Ability.WISDOM) == (
            target.ability_mod(Ability.WISDOM) - 4
        )
        assert target.speed_for(MovementMode.WALK) == 20

    def test_a_ruling_imposing_the_sixth_level_kills_and_announces_it(
        self,
    ) -> None:
        target = fighter(name="Thora")
        encounter = Encounter(
            [target, fighter(name="Other", team="monsters")], Random(9)
        )
        encounter.set_condition("Thora", Condition.EXHAUSTION, applied=True, levels=6)
        assert target.dead
        deaths = [e for e in encounter.log if e.kind == "death"]
        assert len(deaths) == 1
        assert deaths[0].actor == "Thora"

    def test_apply_condition_also_announces_the_sixth_level_death(self) -> None:
        # ``_apply_condition`` is the second funnel into ``add_condition`` and
        # must carry the same ``was_dead`` reading as ``set_condition`` — this
        # pins it directly rather than through a bundled rider that happens
        # to grant Exhaustion.
        target = fighter(name="Thora")
        encounter = Encounter(
            [target, fighter(name="Other", team="monsters")], Random(9)
        )
        target.add_condition(Condition.EXHAUSTION, levels=5)
        encounter._apply_condition(
            target, target, Condition.EXHAUSTION,
            effect_name="a sixth exhaustion level", concentration=False,
        )
        assert target.dead
        assert any(e.kind == "death" for e in encounter.log)

    def test_a_fifth_level_by_ruling_does_not_kill_or_announce(self) -> None:
        target = fighter(name="Thora")
        encounter = Encounter(
            [target, fighter(name="Other", team="monsters")], Random(9)
        )
        encounter.set_condition("Thora", Condition.EXHAUSTION, applied=True, levels=5)
        assert not target.dead
        assert not any(e.kind == "death" for e in encounter.log)


class TestDodgeLostAtNumericSpeedZero:
    """SRD 5.2.1, Dodge: "you don't gain this benefit if your Speed is 0."

    ``_dodge_benefits`` used to consult only the ``speed_zero`` flag, so a
    creature reduced to Speed 0 purely by ``speed_reduction_feet_per_level`` —
    Exhaustion's own shape — kept the Dodge benefit the SRD denies it.
    """

    #: A pack-declared, cumulative condition — never an SRD one — carrying no
    #: ``speed_zero`` flag at all, so only the numeric reduction can catch it.
    TABLE = dict(EFFECTS) | {
        "weary": ConditionEffect(speed_reduction_feet_per_level=10, cumulative=True),
    }

    def _dodging_weary(self, *, levels: int) -> tuple[Encounter, Creature]:
        target = fighter(name="Thora")
        target.condition_effects = self.TABLE
        target.add_condition("weary", levels=levels)
        encounter = Encounter(
            [target, fighter(name="Other", team="monsters")],
            Random(9),
            condition_effects=self.TABLE,
        )
        advance_to(encounter, "Thora", Random(9))
        encounter.act(Action(kind=ActionKind.DODGE), Random(9))
        return encounter, target

    def test_numeric_speed_zero_loses_the_dodge_benefit(self) -> None:
        encounter, target = self._dodging_weary(levels=3)  # walk 30 -> 0
        assert target.speed_for(MovementMode.WALK) == 0
        assert encounter._dodge_benefits(target) is False

    def test_a_reduction_that_does_not_reach_zero_keeps_the_benefit(self) -> None:
        # The no-op guard: a Speed reduced but not to zero must not lose the
        # benefit, or the fix would be over-broad rather than narrow.
        encounter, target = self._dodging_weary(levels=1)  # walk 30 -> 20
        assert target.speed_for(MovementMode.WALK) == 20
        assert encounter._dodge_benefits(target) is True


class TestAttacking:
    def test_a_hit_reduces_hit_points(self) -> None:
        rng = Random(3)
        target = make_monster("Ogre", label="Ogre", position=5)
        encounter = Encounter([fighter(), target], rng)
        advance_to(encounter, "Thora", rng)
        before = target.hp
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20)
        )
        assert "attack" in kinds(events)
        assert target.hp < before

    def test_an_attack_beyond_reach_does_not_consume_the_attack(self) -> None:
        rng = Random(3)
        far = make_monster("Ogre", label="Ogre", position=60)
        encounter = Encounter([fighter(), far], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), rng)
        assert "cannot reach" in events[0].detail
        assert far.hp == far.max_hp

    def test_extra_attack_allows_a_second_swing_but_not_a_third(self) -> None:
        rng = Random(5)
        target = make_monster("Ogre", label="Ogre", position=5)
        encounter = Encounter([fighter(attacks_per_action=2), target], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20))
        encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20))
        with pytest.raises(EncounterError, match="no attacks left"):
            encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20))

    def test_dodging_imposes_disadvantage_on_incoming_attacks(self) -> None:
        rng = Random(2)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Goblin", rng)
        encounter.act(Action(kind=ActionKind.DODGE), rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.ATTACK, target="Goblin"), Random(4))
        assert "disadvantage" in events[0].detail

    def test_a_visible_enemy_within_5_feet_hinders_a_ranged_attack(self) -> None:
        bow = AttackOption(
            name="Shortbow",
            attack_bonus=5,
            damage=Dice(1, 6, 2),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=80,
            long_range=320,
            provenance=FIXTURE,
        )
        shooter = fighter("Shooter")
        shooter.attacks = (bow,)
        nearby = fighter("Nearby", team="foes", position=5)
        target = fighter("Target", team="foes", position=30)
        encounter = Encounter([shooter, nearby, target], Random(1))
        assert encounter.attack_advantage(shooter, target, bow) is Advantage.DISADVANTAGE

    def test_an_incapacitated_enemy_does_not_hinder_a_ranged_attack(self) -> None:
        bow = AttackOption(
            name="Shortbow",
            attack_bonus=5,
            damage=Dice(1, 6, 2),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=80,
            long_range=320,
            provenance=FIXTURE,
        )
        shooter = fighter("Shooter")
        shooter.attacks = (bow,)
        nearby = fighter("Nearby", team="foes", position=5)
        nearby.add_condition(Condition.INCAPACITATED)
        target = fighter("Target", team="foes", position=30)
        encounter = Encounter([shooter, nearby, target], Random(1))
        assert encounter.attack_advantage(shooter, target, bow) is Advantage.NONE

    def test_point_blank_and_prone_cancel_for_a_ranged_attack(self) -> None:
        """SRD 5.2.1 Rules Glossary, Prone, "Attacks Affected": "An attack roll
        against you has Advantage if the attacker is within 5 feet of you.
        Otherwise, that attack roll has Disadvantage."

        The clause names a distance and no weapon, exactly as the
        Paralyzed/Unconscious automatic critical does. A ranged attack also has
        Disadvantage when a capable enemy can see the attacker within 5 feet, so
        the two sources cancel here.
        """
        rng = Random(2)
        archer = fighter("Archer")
        shortbow = AttackOption(
            name="Shortbow",
            attack_bonus=5,
            damage=Dice(1, 6, 2),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=80,
            long_range=320,
            provenance=FIXTURE,
        )
        archer.attacks = (shortbow,)
        target = fighter("Mark", team="foes", position=5)
        target.add_condition(Condition.PRONE)
        encounter = Encounter([archer, target], rng)
        assert (
            encounter.attack_advantage(archer, target, shortbow) is Advantage.NONE
        )
        # The other half of the same clause is likewise the distance: the same bow
        # from across the room still gets Disadvantage, and no long-range penalty is
        # in play at 60 ft to confuse the reading.
        target.position = 60
        assert (
            encounter.attack_advantage(archer, target, shortbow)
            is Advantage.DISADVANTAGE
        )

    def test_a_nearby_ally_does_not_hinder_a_ranged_attack(self) -> None:
        archer = fighter("Archer")
        bow = AttackOption(
            name="Shortbow",
            attack_bonus=5,
            damage=Dice(1, 6, 2),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=80,
            long_range=320,
            provenance=FIXTURE,
        )
        archer.attacks = (bow,)
        ally = fighter("Ally", position=5)
        target = fighter("Target", team="foes", position=30)
        encounter = Encounter([archer, ally, target], Random(1))
        assert encounter.attack_advantage(archer, target, bow) is Advantage.NONE

    def test_an_enemy_on_another_storey_cannot_hinder_a_ranged_attack(self) -> None:
        archer = fighter("Archer")
        bow = AttackOption(
            name="Shortbow",
            attack_bonus=5,
            damage=Dice(1, 6, 2),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=80,
            long_range=320,
            provenance=FIXTURE,
        )
        archer.attacks = (bow,)
        nearby = fighter("Nearby", team="foes", position=5)
        nearby.level = 1
        target = fighter("Target", team="foes", position=15)
        encounter = Encounter(
            [archer, nearby, target], Random(1), map_document=tower()
        )
        assert encounter.cover_between("Nearby", "Archer") is CoverGrade.TOTAL
        assert encounter.attack_advantage(archer, target, bow) is Advantage.NONE

    def test_an_enemy_that_cannot_see_the_attacker_does_not_hinder_the_shot(
        self,
    ) -> None:
        archer = fighter("Archer")
        archer.add_condition(Condition.INVISIBLE)
        bow = AttackOption(
            name="Shortbow",
            attack_bonus=5,
            damage=Dice(1, 6, 2),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=80,
            long_range=320,
            provenance=FIXTURE,
        )
        archer.attacks = (bow,)
        nearby = fighter("Nearby", team="foes", position=5)
        target = fighter("Target", team="foes", position=30)
        encounter = Encounter([archer, nearby, target], Random(1))
        # Invisible still grants its ordinary attack Advantage; no point-blank
        # Disadvantage cancels it because the nearby enemy cannot see the archer.
        assert encounter.attack_advantage(archer, target, bow) is Advantage.ADVANTAGE

    def test_a_blinded_nearby_enemy_does_not_hinder_a_ranged_attack(self) -> None:
        archer = fighter("Archer")
        bow = AttackOption(
            name="Shortbow",
            attack_bonus=5,
            damage=Dice(1, 6, 2),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=80,
            long_range=320,
            provenance=FIXTURE,
        )
        archer.attacks = (bow,)
        nearby = fighter("Nearby", team="foes", position=5)
        nearby.add_condition(Condition.BLINDED)
        target = fighter("Target", team="foes", position=30)
        encounter = Encounter([archer, nearby, target], Random(1))
        assert encounter.attack_advantage(archer, target, bow) is Advantage.NONE

    def test_a_reach_weapon_beyond_5_feet_gets_the_prone_disadvantage(self) -> None:
        # The mirror case, and the one the old gate got right by accident: a melee
        # attack made from beyond 5 feet is not "within 5 feet of you" either.
        rng = Random(2)
        pikeman = fighter("Pikeman")
        pike = AttackOption(
            name="Pike",
            attack_bonus=5,
            damage=Dice(1, 10, 3),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.MELEE,
            reach=10,
            provenance=FIXTURE,
        )
        pikeman.attacks = (pike,)
        target = fighter("Mark", team="foes", position=10)
        target.add_condition(Condition.PRONE)
        encounter = Encounter([pikeman, target], rng)
        assert (
            encounter.attack_advantage(pikeman, target, pike) is Advantage.DISADVANTAGE
        )

    def test_unknown_attack_name_is_reported_with_the_options(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="Longsword"):
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Wolf", attack="Halberd"), rng
            )

    def test_unknown_target_name_is_reported_with_the_combatants(self) -> None:
        # The sibling above names the attacks the actor has, and the map's
        # unknown-feature refusal names the features it has. This one used to
        # stop at "no combatant named 'Bob'", so a caller who mistyped a label —
        # or guessed at one a lookup spec assigned for them — had nothing to
        # correct it against and no way to tell a typo from an absent creature.
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(
            EncounterError, match="no combatant named 'Bob'; the fight has: Thora, Wolf"
        ):
            encounter.act(Action(kind=ActionKind.ATTACK, target="Bob"), rng)

    def test_a_crowded_fight_names_a_bounded_slice_and_counts_the_rest(self) -> None:
        # A mass battle's whole roster would bury the name that was actually
        # wrong, so the list stops at the bound and says how many it left out.
        # Both halves are derived from the bound rather than written out: a test
        # that spelled 12 would pin the message against nothing but itself.
        hero = fighter()
        crowd = [
            fighter(f"Extra{index:02d}", team="foes", position=5)
            for index in range(MAX_LISTED_COMBATANTS + 8)
        ]
        encounter = Encounter([hero, *crowd], Random(1))
        names = sorted(creature.name for creature in [hero, *crowd])

        with pytest.raises(EncounterError, match="no combatant named 'Bob'") as raised:
            encounter.act(Action(kind=ActionKind.ATTACK, target="Bob"), Random(1))

        message = str(raised.value)
        assert all(name in message for name in names[:MAX_LISTED_COMBATANTS])
        assert names[MAX_LISTED_COMBATANTS] not in message
        assert f"and {len(names) - MAX_LISTED_COMBATANTS} more" in message


class TestInvisibleStopsHelpingAgainstAnObserverThatSees:
    """SRD 5.2.1, Invisible, "Attacks Affected", the sentence that was missing.

    "Attack rolls against you have Disadvantage, and your attack rolls have
    Advantage. **If a creature can somehow see you, you don't gain this benefit
    against that creature.**"

    The withdrawal is a relationship between two creatures — and, once a map is
    involved, between them and the light and cover on it — so no per-condition
    row in the kernel table can state it. What states it is
    :meth:`Encounter._can_see`, which already answers ``True`` for an observer
    with Blindsight in range and ``False`` for an unseen subject. These cases
    pin that the withdrawal is per-observer rather than global: the same
    Invisible creature is a harder target for one enemy and an ordinary one for
    the enemy standing beside it.

    Blindsight is the observer used throughout because it is the sight the SRD
    itself offers as the way a creature "can somehow see you" — the Rules
    Glossary entry says a creature with it "can see within a specific range
    without eyes", and the condition's Concealed clause is what it defeats.
    """

    def sighted_fight(self) -> tuple[Encounter, Creature, Creature, Creature]:
        """One Invisible creature, one enemy with Blindsight, one without.

        Both enemies stand on the same square at the same distance, so nothing
        but sight separates the two answers.
        """
        ghost = fighter("Ghost", position=0)
        ghost.add_condition(Condition.INVISIBLE)
        seer = fighter("Seer", team="foes", position=5)
        seer.blindsight = 60
        blind_to_it = fighter("Sighted", team="foes", position=5)
        encounter = Encounter([ghost, seer, blind_to_it], Random(3))
        return encounter, ghost, seer, blind_to_it

    def test_blindsight_within_range_denies_the_target_its_disadvantage(self) -> None:
        # The defect: an attacker that can see the Invisible creature was still
        # taking Disadvantage, because the condition row asserted it outright.
        encounter, ghost, seer, _ = self.sighted_fight()
        assert seer.distance_to(ghost, encounter.movement_rule) <= seer.blindsight
        assert encounter.attack_advantage(
            seer, ghost, seer.attacks[0]
        ) is Advantage.NONE

    def test_the_enemy_beside_it_without_blindsight_still_takes_disadvantage(
        self,
    ) -> None:
        # "you don't gain this benefit against **that creature**" — withdrawn for
        # the one that sees, not for the fight. Same encounter, same square, same
        # distance: only sight differs.
        encounter, ghost, _, blind_to_it = self.sighted_fight()
        assert encounter.attack_advantage(
            blind_to_it, ghost, blind_to_it.attacks[0]
        ) is Advantage.DISADVANTAGE

    def test_the_invisible_creature_gains_no_advantage_against_what_sees_it(
        self,
    ) -> None:
        encounter, ghost, seer, _ = self.sighted_fight()
        assert encounter.attack_advantage(
            ghost, seer, ghost.attacks[0]
        ) is Advantage.NONE

    def test_it_keeps_its_advantage_against_the_enemy_that_cannot(self) -> None:
        encounter, ghost, _, blind_to_it = self.sighted_fight()
        assert encounter.attack_advantage(
            ghost, blind_to_it, ghost.attacks[0]
        ) is Advantage.ADVANTAGE

    def test_blindsight_beyond_its_range_sees_nothing(self) -> None:
        # The range is what makes Blindsight a sight rather than a flag, so the
        # withdrawal has to stop with it: step outside and the ordinary pair
        # comes back, in both directions.
        ghost = fighter("Ghost", position=0)
        ghost.add_condition(Condition.INVISIBLE)
        short_sighted = fighter("Seer", team="foes", position=30)
        short_sighted.blindsight = 10
        encounter = Encounter([ghost, short_sighted], Random(3))
        assert encounter.attack_advantage(
            short_sighted, ghost, short_sighted.attacks[0]
        ) is Advantage.DISADVANTAGE
        assert encounter.attack_advantage(
            ghost, short_sighted, ghost.attacks[0]
        ) is Advantage.ADVANTAGE

    def test_the_cast_path_reads_the_withdrawal_the_same_way(self) -> None:
        # The drift guard the neighbouring TestSpellAttackAdvantage states: no
        # source of Advantage distinguishes a spell attack from a swing, so the
        # two methods have to land on the same answer against the same pair.
        encounter, ghost, seer, blind_to_it = self.sighted_fight()
        bolt = Spell(
            name="Unseen Bolt",
            level=1,
            requires_attack_roll=True,
            attack_kind=AttackKind.MELEE,  # No point-blank penalty to confound it.
            damage=Dice(1, 8),
            damage_type=DamageType.FORCE,
            range_feet=5,
            provenance=FIXTURE,
        )
        assert encounter.spell_attack_advantage(
            seer, ghost, bolt
        ) is Advantage.NONE
        assert encounter.spell_attack_advantage(
            blind_to_it, ghost, bolt
        ) is Advantage.DISADVANTAGE
        assert encounter.spell_attack_advantage(
            ghost, seer, bolt
        ) is Advantage.NONE
        assert encounter.spell_attack_advantage(
            ghost, blind_to_it, bolt
        ) is Advantage.ADVANTAGE

    def opportunity_attack(
        self, *, mover_invisible: bool, attacker_invisible: bool,
        blindsight_on: str | None,
    ) -> Event | None:
        """Walk out of a goblin's reach and return the Opportunity Attack, if any."""
        rng = Random(6)
        thora = fighter()
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        if mover_invisible:
            thora.add_condition(Condition.INVISIBLE)
        if attacker_invisible:
            goblin.add_condition(Condition.INVISIBLE)
        if blindsight_on == "mover":
            thora.blindsight = 60
        elif blindsight_on == "attacker":
            goblin.blindsight = 60
        encounter = Encounter([thora, goblin], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        attacks = [event for event in events if event.kind == "opportunity_attack"]
        return attacks[0] if attacks else None

    def test_an_invisible_mover_seen_by_blindsight_is_struck_at_neither(self) -> None:
        # The Opportunity Attack is already gated on "a creature that you can
        # see", so an attacker that cannot see the mover never swings at all —
        # which means the *only* reachable swing against an Invisible mover is
        # one whose attacker can see it, and the SRD withdraws the Disadvantage
        # in exactly that case. This is where the swing used to read
        # "disadvantage" for an attacker demonstrably looking straight at it.
        event = self.opportunity_attack(
            mover_invisible=True, attacker_invisible=False, blindsight_on="attacker"
        )
        assert event is not None
        assert event.data["advantage"] == Advantage.NONE.value

    def test_an_invisible_attacker_keeps_its_advantage_on_the_reaction(self) -> None:
        # The other half at the same call site, and the one a careless removal
        # of the condition flags would silently drop: the mover cannot see the
        # goblin, so the benefit is not withdrawn.
        event = self.opportunity_attack(
            mover_invisible=False, attacker_invisible=True, blindsight_on=None
        )
        assert event is not None
        assert event.data["advantage"] == Advantage.ADVANTAGE.value

    def test_a_mover_with_blindsight_takes_that_advantage_away(self) -> None:
        event = self.opportunity_attack(
            mover_invisible=False, attacker_invisible=True, blindsight_on="mover"
        )
        assert event is not None
        assert event.data["advantage"] == Advantage.NONE.value


class TestTremorsenseIsNotASightRung:
    """SRD 5.2.1, Tremorsense: it pinpoints a creature within range but
    "doesn't count as a form of sight". Unlike Truesight and Blindsight it
    is deliberately not one of :meth:`Encounter._can_see`'s rungs — the
    engine has no "knows the location but cannot see" state, so granting it
    sight here would wrongly cancel the unseen-target Disadvantage on a
    creature that, by the SRD's own words, cannot see its target at all.
    """

    def test_within_range_it_still_gives_no_sight_of_an_invisible_target(self) -> None:
        # The regression pin: 30 ft of Tremorsense on a target 20 ft away
        # pinpoints the ghost's square but is not sight, so Disadvantage
        # stands.
        ghost = fighter("Ghost", position=0)
        ghost.add_condition(Condition.INVISIBLE)
        seer = fighter("Seer", team="foes", position=20)
        seer.tremorsense = 30
        encounter = Encounter([ghost, seer], Random(3))
        assert seer.distance_to(ghost, encounter.movement_rule) <= seer.tremorsense
        assert encounter.attack_advantage(
            seer, ghost, seer.attacks[0]
        ) is Advantage.DISADVANTAGE

    def test_beyond_its_range_the_invisible_target_keeps_its_advantage(self) -> None:
        ghost = fighter("Ghost", position=0)
        ghost.add_condition(Condition.INVISIBLE)
        seer = fighter("Seer", team="foes", position=40)
        seer.tremorsense = 30
        encounter = Encounter([ghost, seer], Random(3))
        assert seer.distance_to(ghost, encounter.movement_rule) > seer.tremorsense
        assert encounter.attack_advantage(
            seer, ghost, seer.attacks[0]
        ) is Advantage.DISADVANTAGE


class TestTruesightOutranksBlindsightsLimits:
    """SRD 5.2.1, Truesight: "your vision pierces through" Darkness and
    Invisibility within range. It takes the top rung on the ladder — checked
    before Blindsight — but unlike Blindsight it carries no clause exempting
    it from the observer's own Blinded condition, so a blinded observer gets
    nothing from it even in range.
    """

    def test_it_sees_an_invisible_target_within_range(self) -> None:
        ghost = fighter("Ghost", position=0)
        ghost.add_condition(Condition.INVISIBLE)
        seer = fighter("Seer", team="foes", position=20)
        seer.truesight = 30
        encounter = Encounter([ghost, seer], Random(3))
        assert encounter.attack_advantage(
            seer, ghost, seer.attacks[0]
        ) is Advantage.NONE

    def test_beyond_its_range_the_invisible_target_keeps_its_advantage(self) -> None:
        ghost = fighter("Ghost", position=0)
        ghost.add_condition(Condition.INVISIBLE)
        seer = fighter("Seer", team="foes", position=40)
        seer.truesight = 30
        encounter = Encounter([ghost, seer], Random(3))
        assert encounter.attack_advantage(
            seer, ghost, seer.attacks[0]
        ) is Advantage.DISADVANTAGE

    def test_unlike_blindsight_it_grants_nothing_to_an_observer_that_cannot_see(
        self,
    ) -> None:
        # The narrowing that puts it above Blindsight rather than replacing it:
        # Blindsight's SRD text says "even if you have the Blinded condition";
        # Truesight's does not, so an observer whose own condition blocks
        # sight (``cannot_see``) still cannot see through Invisible with
        # Truesight alone. A condition with only ``cannot_see`` set, rather
        # than the bundled Blinded, isolates the sight ladder from Blinded's
        # own blanket attack-roll Disadvantage, which would otherwise
        # dominate the assertion regardless of what the ladder does.
        table = dict(EFFECTS) | {"sightless": ConditionEffect(cannot_see=True)}
        ghost = fighter("Ghost", position=0)
        ghost.add_condition(Condition.INVISIBLE)
        seer = fighter("Seer", team="foes", position=20)
        seer.truesight = 30
        seer.condition_effects = table
        seer.add_condition("sightless")
        encounter = Encounter([ghost, seer], Random(3), condition_effects=table)
        assert encounter.attack_advantage(
            seer, ghost, seer.attacks[0]
        ) is Advantage.DISADVANTAGE


class TestAmmunition:
    """A shot spends a piece of what it fires, and an empty quiver refuses one.

    The quantity in ``Creature.items`` *is* the count, exactly as it is for a
    potion, so nothing new holds the arrows. What is new is that the refusal
    **raises** where being out of range or behind total cover only emits: an
    empty quiver is a fact about the shooter's own sheet, which the caller can
    read back off ``state()``, while geometry is the engine's to know and worth
    reporting rather than refusing.
    """

    def bow(
        self, *, ammunition: str | None = "Arrow", loading: bool = False
    ) -> AttackOption:
        return AttackOption(
            name="Shortbow",
            attack_bonus=5,
            damage=Dice(1, 6, 3),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            normal_range=80,
            long_range=320,
            ammunition=ammunition,
            loading=loading,
            provenance=FIXTURE,
        )

    def duel(
        self, *, arrows: int = 3, ammunition: str | None = "Arrow"
    ) -> Encounter:
        """An archer with a quiver, and something to shoot at 30 feet."""
        rng = Random(3)
        shooter = fighter("Sylvi", position=0)
        shooter.attacks = (self.bow(ammunition=ammunition),)
        shooter.items = {"Arrow": arrows}
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        encounter = Encounter([shooter, goblin], rng)
        advance_to(encounter, "Sylvi", rng)
        return encounter

    def test_an_empty_quiver_refuses_the_shot_and_spends_nothing(self) -> None:
        # The defender is a Redirect Attack boss with a minion beside it, which
        # is what makes the *ordering* of the refusal visible rather than a
        # matter of trust. ``_redirect_attack_target`` is not a query: it swaps
        # the two creatures' squares and spends the boss's reaction. A refusal
        # placed after it would charge the defender for a shot that was never
        # taken, and ``state()`` reports both halves of that bill.
        rng = Random(3)
        shooter = fighter("Sylvi", position=0)
        shooter.attacks = (self.bow(),)
        shooter.items = {"Arrow": 0}
        boss = fighter("Snagfinger", team="monsters", position=30)
        boss.redirect_attack = True
        minion = fighter("House Goblin", team="monsters", position=35)
        encounter = Encounter([shooter, boss, minion], rng)
        advance_to(encounter, "Sylvi", rng)
        before = encounter.state()
        undrawn = rng.getstate()

        with pytest.raises(EncounterError, match="no Arrow left to fire Shortbow"):
            encounter.act(Action(kind=ActionKind.ATTACK, target="Snagfinger"), rng)

        assert encounter.state() == before
        # And no die was drawn. A refusal that quietly rolled would leave the
        # next roll of this fight different, which no state comparison can see.
        assert rng.getstate() == undrawn

        # The reaction really was armed: one arrow later the same swing does
        # redirect, so the untouched state above is the refusal's doing rather
        # than a boss who was never going to react.
        shooter.items["Arrow"] = 1
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Snagfinger"), FixedRandom(4)
        )
        assert kinds(events)[0] == "redirect_attack"

    def test_a_shot_spends_one_piece_and_reports_what_is_left(self) -> None:
        encounter = self.duel(arrows=3)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(20)
        )

        swing = next(event for event in events if event.kind == "attack")
        assert swing.data["ammunition_remaining"] == 2
        assert encounter.creatures["Sylvi"].items == {"Arrow": 2}

    def test_an_attack_that_names_no_ammunition_carries_no_count(self) -> None:
        # The other half, and the reason the key is conditional: every attack in
        # the bundled catalog is this one, so an unconditional key would change
        # the shape of every attack event ever emitted.
        encounter = self.duel(arrows=3, ammunition=None)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(20)
        )

        swing = next(event for event in events if event.kind == "attack")
        assert "ammunition_remaining" not in swing.data
        assert encounter.creatures["Sylvi"].items == {"Arrow": 3}

    def test_use_item_on_an_ammunition_name_refuses_and_spends_nothing(self) -> None:
        # "Arrow" is not an item, and ``use_item`` saying "not defined by the
        # loaded content" is true but useless — it invites defining one, which
        # ``ItemEffect.__post_init__`` refuses because ammunition has no ``use``
        # block. The refusal has to name what "Arrow" actually is instead.
        encounter = self.duel(arrows=3)
        before = encounter.state()
        undrawn = Random(3)
        rng = Random(3)
        undrawn_state = undrawn.getstate()

        with pytest.raises(EncounterError, match="ammunition"):
            encounter.act(Action(kind=ActionKind.USE_ITEM, item="Arrow"), rng)

        assert encounter.state() == before
        assert rng.getstate() == undrawn_state
        assert encounter.creatures["Sylvi"].items == {"Arrow": 3}


class TestThrownWeapons:
    """SRD 5.2.1 prints one attack that resolves two ways.

    The Ogre's Javelin is *"**Melee or Ranged** Attack Roll: +6, reach 5 ft. or
    range 30/120 ft."*, and twenty stat blocks carry that shape. It is the
    Thrown property (catalog ``583-9-4-8-thrown``) written out: the Javelin sits
    under **Simple Melee Weapons** with ``Thrown (Range 30/120)``, so it is a
    melee weapon that *enables* a ranged attack rather than a bow that happens
    to reach.

    The engine's ``kind`` stays ``ranged`` — the mode beyond reach — and
    ``thrown`` says the same weapon is still in hand inside it. Every case here
    is about which of the two a given distance picks.
    """

    def javelin(
        self,
        *,
        thrown: bool = True,
        reach: int = 5,
        ammunition: str | None = "Javelin",
    ) -> AttackOption:
        return AttackOption(
            name="Javelin",
            attack_bonus=6,
            damage=Dice(2, 6, 4),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.RANGED,
            reach=reach,
            normal_range=30,
            long_range=120,
            ammunition=ammunition,
            thrown=thrown,
            provenance=FIXTURE,
        )

    def skirmisher(
        self, *, position: int | tuple[int, int] = 5, javelins: int = 3, **option: Any
    ) -> Creature:
        """A thrower whose *only* attack is the javelin.

        Deliberately not the Ogre: the Ogre also carries a Greatclub, so it
        would satisfy every "can this creature fight in melee" question through
        the wrong option and prove nothing about the thrown one.
        """
        thrower = fighter("Skirmisher", team="monsters", position=position)
        thrower.attacks = (self.javelin(**option),)
        thrower.items = {"Javelin": javelins}
        return thrower

    def test_the_bundled_ogre_stabs_rather_than_throws_at_five_feet(self) -> None:
        # The reproduction, against the shipped record rather than a fixture.
        # Before the change the Ogre threw a javelin at a target it was standing
        # on top of and ate ``_ranged_close_combat_penalty`` for it:
        #   Javelin: d20 [13/2] disadvantage -> 2 +6 = 8 vs AC 10 -> miss
        # RAW it simply makes the attack as a melee one and takes no penalty.
        rng = Random(6)
        ogre = make_monster("Ogre", label="Ogre", position=5)
        encounter = Encounter([fighter(position=0), ogre], rng)
        advance_to(encounter, "Ogre", rng)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Thora", attack="Javelin"),
            FixedRandom(13),
        )

        swing = next(event for event in events if event.kind == "attack")
        assert swing.data["advantage"] == "none"

    def test_a_thrown_weapon_past_its_reach_is_still_a_ranged_attack(self) -> None:
        # The other side of the boundary, and the reason ``kind`` stays
        # ``ranged``: at 30 ft the throw is a shot, and a shot made with an
        # enemy breathing down the thrower's neck has Disadvantage. Bram stands
        # beside the Ogre, not beside the target.
        rng = Random(6)
        ogre = make_monster("Ogre", label="Ogre", position=30)
        bram = fighter("Bram", position=35)
        encounter = Encounter([fighter(position=0), bram, ogre], rng)
        advance_to(encounter, "Ogre", rng)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Thora", attack="Javelin"),
            FixedRandom(13),
        )

        swing = next(event for event in events if event.kind == "attack")
        assert swing.data["advantage"] == "disadvantage"

    def test_a_thrown_weapon_beyond_its_normal_range_takes_the_long_range_penalty(
        self,
    ) -> None:
        # Nobody is adjacent to anyone here, so the only Disadvantage available
        # is the long-range band — which pins that ``has_long_range_penalty``
        # still reads for a thrown option beyond its reach.
        rng = Random(6)
        ogre = make_monster("Ogre", label="Ogre", position=60)
        encounter = Encounter([fighter(position=0), ogre], rng)
        advance_to(encounter, "Ogre", rng)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Thora", attack="Javelin"),
            FixedRandom(13),
        )

        swing = next(event for event in events if event.kind == "attack")
        assert swing.data["advantage"] == "disadvantage"

    def test_a_thrown_weapon_still_reaches_its_full_range(self) -> None:
        # ``max_distance()`` must keep answering with the throw and not the
        # reach: a thrown option that reported 5 ft would be refused at every
        # range it is printed with.
        assert self.javelin().max_distance() == 120

    def test_stabbing_with_a_thrown_weapon_spends_nothing(self) -> None:
        # The ruling: a javelin thrown leaves the hand, a javelin used to stab
        # does not. So a melee-resolved use spends no ammunition and reports no
        # count — the same shape as an attack that names no ammunition at all.
        rng = Random(6)
        encounter = Encounter([fighter(position=0), self.skirmisher()], rng)
        advance_to(encounter, "Skirmisher", rng)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Thora"), FixedRandom(20)
        )

        swing = next(event for event in events if event.kind == "attack")
        assert "ammunition_remaining" not in swing.data
        assert encounter.creatures["Skirmisher"].items == {"Javelin": 3}

    def test_throwing_the_same_weapon_does_spend_one(self) -> None:
        # The control for the case above: the count is untouched by a stab
        # because the stab is a stab, not because the spend was deleted.
        rng = Random(6)
        encounter = Encounter(
            [fighter(position=0), self.skirmisher(position=30)], rng
        )
        advance_to(encounter, "Skirmisher", rng)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Thora"), FixedRandom(20)
        )

        swing = next(event for event in events if event.kind == "attack")
        assert swing.data["ammunition_remaining"] == 2
        assert encounter.creatures["Skirmisher"].items == {"Javelin": 2}

    def test_a_thrower_with_nothing_left_cannot_stab_either(self) -> None:
        # The other half of the ruling, and the reason ``_require_loaded`` is
        # left alone: the count is the javelins held, not a magazine beside
        # them. Having thrown all three there is nothing in hand to stab with,
        # so possession is still required even where nothing is spent.
        rng = Random(6)
        encounter = Encounter(
            [fighter(position=0), self.skirmisher(javelins=0)], rng
        )
        advance_to(encounter, "Skirmisher", rng)
        before = encounter.state()

        with pytest.raises(EncounterError, match="no Javelin left to fire Javelin"):
            encounter.act(Action(kind=ActionKind.ATTACK, target="Thora"), rng)

        assert encounter.state() == before

    def test_a_thrown_weapon_is_the_opportunity_attack_a_thrower_makes(self) -> None:
        # The T4 seam. ``_opportunity_attack`` swings the first option that can
        # resolve in melee and the threat radius is derived from that same
        # option, so a creature carrying nothing but a javelin threatens the
        # square beside it rather than nothing at all.
        rng = Random(6)
        encounter = Encounter([fighter(position=0), self.skirmisher()], rng)
        advance_to(encounter, "Thora", rng)

        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )

        provoked = [e for e in events if e.kind == "opportunity_attack"]
        assert [e.data["attack"] for e in provoked] == ["Javelin"]

    def test_the_threat_radius_comes_from_the_thrown_option_it_swings(self) -> None:
        # Same seam, pinned where the two could disagree: a reach-10 thrown
        # option must provoke on the step out of 10 ft, not out of 5. No SRD
        # weapon is both Reach and Thrown — this is a fixture built to make the
        # derivation visible, because at reach 5 the shared default hides it.
        rng = Random(6)
        thrower = self.skirmisher(position=(15, 8), reach=10)
        encounter = Encounter([fighter(position=0), thrower], rng)
        advance_to(encounter, "Thora", rng)

        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )

        assert "opportunity_attack" in kinds(events)

    def test_a_plain_ranged_weapon_threatens_nobody(self) -> None:
        # The regression pin under the two above: making thrown options
        # melee-capable must not hand an opportunity attack to every archer.
        rng = Random(6)
        archer = self.skirmisher(thrown=False)
        encounter = Encounter([fighter(position=0), archer], rng)
        advance_to(encounter, "Thora", rng)

        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )

        assert "opportunity_attack" not in kinds(events)

    def test_a_plain_ranged_weapon_in_close_combat_is_unchanged(self) -> None:
        # And the same pin on the attack roll: the close-combat Disadvantage a
        # bow eats at 5 ft is exactly the behaviour the thrown branch must not
        # generalise away.
        rng = Random(6)
        archer = self.skirmisher(thrown=False)
        encounter = Encounter([fighter(position=0), archer], rng)
        advance_to(encounter, "Skirmisher", rng)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Thora"), FixedRandom(13)
        )

        swing = next(event for event in events if event.kind == "attack")
        assert swing.data["advantage"] == "disadvantage"
        assert swing.data["ammunition_remaining"] == 2


class TestGoingDown:
    def test_reaching_zero_knocks_a_creature_out_rather_than_killing_it(self) -> None:
        rng = Random(3)
        victim = fighter("Victim", team="foes", max_hp=40, hp=1, position=5)
        encounter = Encounter([fighter(), victim], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(20))
        assert victim.hp == 0
        assert not victim.dead
        assert victim.dying
        assert Condition.UNCONSCIOUS in victim.conditions
        assert Condition.PRONE in victim.conditions

    def test_damage_exceeding_maximum_hit_points_kills_outright(self) -> None:
        victim = fighter("Victim", team="foes", max_hp=4, hp=4)
        victim.take_damage(20)
        assert victim.dead
        assert not victim.dying

    def test_death_saves_are_rolled_at_the_start_of_a_dying_turn(self) -> None:
        # A third combatant keeps the fight alive. In a duel, dropping the only
        # opponent ends the encounter and advance() stops, so the dying creature
        # would never get the turn on which it would roll.
        rng = Random(8)
        victim = fighter("Victim", team="foes", max_hp=40, hp=1, position=5)
        ally = make_monster("Wolf", label="Wolf", team="foes", position=10)
        encounter = Encounter([fighter(), victim, ally], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(20))
        assert victim.dying
        assert not encounter.over

        for _ in range(12):
            events = encounter.advance(rng)
            if encounter.current_name == "Victim":
                assert "death_save" in kinds(events)
                return
        raise AssertionError("the dying creature never took a turn")

    @staticmethod
    def _dying_hero() -> tuple[Encounter, Creature]:
        """A fight whose Hero is at 0 hit points, paused on the attacker's turn."""
        rng = Random(8)
        hero = fighter("Hero", max_hp=30, hp=1, position=0)
        foe = fighter("Foe", team="foes", position=5)
        ally = fighter("Ally", position=40)  # keeps the fight from ending
        encounter = Encounter([hero, foe, ally], rng)
        advance_to(encounter, "Foe", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Hero"), FixedRandom(20))
        assert hero.dying
        return encounter, hero

    def test_stabilising_clears_both_death_save_counters(self) -> None:
        """SRD 5.2.1, "Playing the Game" -> "Death Saving Throws", Three
        Successes/Failures: "The successes and failures don't need to be
        consecutive; keep track of both until you collect three of a kind. The
        number of both is reset to zero when you regain any Hit Points or become
        Stable."

        ``Creature.heal`` already honours the first half. The roll that stabilises
        set ``stable`` and left the counters standing.
        """
        encounter, hero = self._dying_hero()
        hero.death_save_successes = 2
        hero.death_save_failures = 1
        # A forced 15 succeeds: the third success, which stabilises.
        advance_to(encounter, "Hero", FixedRandom(15))
        assert hero.stable
        assert hero.death_save_successes == 0
        assert hero.death_save_failures == 0

    def test_a_stabilised_creature_knocked_down_again_starts_from_nothing(
        self,
    ) -> None:
        # What the stale counters bought: with three successes still on the sheet, a
        # *failed* death save re-stabilised the creature. The failure took it to two,
        # short of the three that kill, and the untouched successes then tripped the
        # stabilise branch immediately below.
        encounter, hero = self._dying_hero()
        hero.death_save_successes = 2
        advance_to(encounter, "Hero", FixedRandom(15))
        assert hero.stable

        hero.take_damage(3)
        assert not hero.stable
        assert hero.death_save_successes == 0
        assert hero.death_save_failures == 1

        # A forced 5 fails. It must not stabilise anything.
        encounter.advance(FixedRandom(5))
        advance_to(encounter, "Hero", FixedRandom(5))
        assert not hero.stable
        assert hero.death_save_failures == 2
        assert hero.death_save_successes == 0

    def test_a_natural_20_revival_leaves_the_full_movement_budget(self) -> None:
        """SRD 5.2.1, "Death Saving Throws", Rolling 20: "If you roll a 20 on the
        d20, you regain 1 Hit Point." The save is rolled at the start of the
        creature's own turn, so the revived creature is conscious for the rest of
        it — and a conscious creature may move up to its Speed on its turn.
        Deriving the budget before the save froze ``movement_left`` at 0 while
        the attack budget was granted regardless.
        """
        encounter, hero = self._dying_hero()
        # A forced 20 is the natural 20: regain 1 hit point and wake.
        advance_to(encounter, "Hero", FixedRandom(20))
        assert hero.conscious
        assert hero.hp == 1
        # Revived, not tidied up: still Prone, and standing costs half Speed.
        assert Condition.PRONE in hero.conditions
        assert encounter.state()["turn_state"]["movement_left"] == hero.speed

    def test_a_still_dying_creature_has_no_movement_budget(self) -> None:
        encounter, hero = self._dying_hero()
        # A forced 15 succeeds without reviving: one success, still down.
        advance_to(encounter, "Hero", FixedRandom(15))
        assert not hero.conscious
        assert hero.dying
        assert encounter.state()["turn_state"]["movement_left"] == 0

    def test_healing_from_zero_clears_unconsciousness_and_resets_saves(self) -> None:
        victim = fighter("Victim", max_hp=20, hp=1)
        victim.take_damage(1)
        victim.death_save_failures = 2
        victim.heal(5)
        assert victim.hp == 5
        assert Condition.UNCONSCIOUS not in victim.conditions
        assert victim.death_save_failures == 0


class TestDamageAtZeroHitPoints:
    """Damage taken *while already* at 0 hit points is its own rule.

    SRD 5.2.1, "Damage at 0 Hit Points": any damage costs a death saving throw
    failure, a critical hit costs two, and damage equalling or exceeding the hit
    point maximum kills outright. Nothing there resets the counters — only
    regaining hit points or becoming stable does that — so these tests are what
    keep the drop-to-0 reset from being applied a second time to a creature that
    was already down.
    """

    @staticmethod
    def _downed(failures: int = 0, successes: int = 0, max_hp: int = 30) -> Creature:
        victim = fighter("Victim", max_hp=max_hp, hp=1)
        victim.take_damage(1)
        assert victim.dying
        victim.death_save_failures = failures
        victim.death_save_successes = successes
        return victim

    def test_damage_while_down_costs_one_failure_and_keeps_the_rest(self) -> None:
        victim = self._downed(failures=1, successes=2)
        victim.take_damage(3)
        assert victim.hp == 0
        assert victim.death_save_failures == 2
        # Successes survive: only healing or stabilising resets them.
        assert victim.death_save_successes == 2
        assert victim.dying and not victim.dead

    def test_a_critical_hit_while_down_costs_two_failures(self) -> None:
        victim = self._downed()
        victim.take_damage(3, critical=True)
        assert victim.death_save_failures == 2
        assert victim.dying and not victim.dead

    def test_a_third_failure_from_damage_kills(self) -> None:
        victim = self._downed(failures=2)
        victim.take_damage(3)
        assert victim.dead
        assert not victim.dying
        # The rolled-failure death path discards unconsciousness; so must this one.
        assert Condition.UNCONSCIOUS not in victim.conditions

    def test_a_critical_hit_finishes_a_creature_that_has_failed_once(self) -> None:
        victim = self._downed(failures=1)
        victim.take_damage(3, critical=True)
        assert victim.dead

    def test_damage_equal_to_the_maximum_kills_outright_rather_than_by_failure(
        self,
    ) -> None:
        victim = self._downed(max_hp=30)
        victim.take_damage(30)
        assert victim.dead
        # Killed by the massive-damage rule, so no failure was ever accrued.
        assert victim.death_save_failures == 0
        assert Condition.UNCONSCIOUS not in victim.conditions

    def test_damage_ends_stability_and_still_costs_a_failure(self) -> None:
        # A stable creature has 0 hit points, so both rules apply at once: it stops
        # being stable *and* it takes the failure.
        victim = self._downed()
        victim.stable = True
        victim.take_damage(3)
        assert not victim.stable
        assert victim.death_save_failures == 1
        assert victim.dying

    def test_dropping_to_zero_still_clears_the_counters(self) -> None:
        # The behaviour the fix must not regress: the *drop* is a fresh dying state.
        victim = fighter("Victim", max_hp=30, hp=10)
        victim.death_save_failures = 2
        victim.death_save_successes = 1
        victim.take_damage(10)
        assert victim.hp == 0
        assert victim.death_save_failures == 0
        assert victim.death_save_successes == 0
        assert Condition.UNCONSCIOUS in victim.conditions
        assert Condition.PRONE in victim.conditions

    def test_damage_to_a_corpse_changes_nothing(self) -> None:
        victim = self._downed(failures=2)
        victim.take_damage(3)
        assert victim.dead
        victim.take_damage(3)
        assert victim.dead
        assert victim.death_save_failures == 3
        assert Condition.UNCONSCIOUS not in victim.conditions

    @staticmethod
    def _fight_with_a_dying_creature(
        item: ItemEffect, *, max_hp: int = 30
    ) -> tuple[Encounter, Creature]:
        """A fight paused on Thug's turn, with Victim at 0 hit points.

        A third combatant keeps the encounter running — dropping the only opponent
        would end it, and ``advance`` would stop. Every death save on the way back
        round is forced to 15, a success, so the victim can neither die nor
        stabilise before the item lands.
        """
        rng = Random(11)
        thug = fighter("Thug", team="foes", position=0)
        thug.items = {"Vial": 2}
        victim = fighter("Victim", max_hp=max_hp, hp=1, position=5)
        ally = fighter("Ally", position=40)
        encounter = Encounter([thug, victim, ally], rng, items={"Vial": item})
        advance_to(encounter, "Thug", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(20))
        assert victim.dying
        encounter.advance(FixedRandom(15))
        advance_to(encounter, "Thug", FixedRandom(15))
        return encounter, victim

    @staticmethod
    def _throw_the_vial(encounter: Encounter) -> list[Event]:
        return encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Vial", target="Victim"), Random(3)
        )

    def test_a_third_failure_from_damage_is_announced_as_a_death(self) -> None:
        """Killing an already-unconscious creature has to narrate.

        ``_apply_damage`` decided whether to announce anything from
        ``was_conscious and not conscious``, which is false for a creature that was
        already at 0. So the creature died, the state said so, and the log said
        nothing — the one event a narrator most needs.
        """
        fire = ItemEffect(
            damage=Dice.parse("2d6"),
            damage_type=DamageType.FIRE,
            save_ability=Ability.DEXTERITY,
            save_dc=13,
            provenance=FIXTURE,
        )
        encounter, victim = self._fight_with_a_dying_creature(fire)
        victim.death_save_failures = 2
        events = self._throw_the_vial(encounter)
        assert victim.dead
        assert victim.death_save_failures == 3
        assert "death" in kinds(events)
        assert "failed death save" in detail_of(events, "death")

    def test_massive_damage_to_a_dying_creature_is_announced_as_a_death(self) -> None:
        # The second route to ``dead`` from 0 hit points, and it was equally silent:
        # damage at 0 that equals or exceeds the maximum kills without ever
        # accruing a failure.
        bomb = ItemEffect(
            damage=Dice(4, 6, 40), damage_type=DamageType.FIRE, provenance=FIXTURE
        )
        encounter, victim = self._fight_with_a_dying_creature(bomb, max_hp=25)
        events = self._throw_the_vial(encounter)
        assert victim.dead
        # Killed by the massive-damage rule, so no failure was accrued...
        assert victim.death_save_failures == 0
        assert "death" in kinds(events)
        # ...and the narration says which rule did it.
        assert detail_of(events, "death") == "damage exceeded maximum hit points"

    def test_a_creature_that_survives_the_hit_is_not_announced_dead(self) -> None:
        # The guard on the fix: a dying creature that merely takes another failure
        # still produces no death event.
        fire = ItemEffect(
            damage=Dice.parse("2d6"),
            damage_type=DamageType.FIRE,
            save_ability=Ability.DEXTERITY,
            save_dc=13,
            provenance=FIXTURE,
        )
        encounter, victim = self._fight_with_a_dying_creature(fire)
        events = self._throw_the_vial(encounter)
        assert victim.dying and not victim.dead
        assert "death" not in kinds(events)
        assert "down" not in kinds(events)

    def test_a_damaging_item_on_a_dying_creature_costs_a_failure(self) -> None:
        # An item was once the *only* route to this rule: an attack refused an
        # unconscious target and a spell filtered the area down to conscious
        # creatures, while an item only ever refused a corpse. Attacks and spells
        # now reach it too — see ``TestADownedCreatureIsStillATarget`` — so this
        # covers the item path rather than standing in for all three.
        fire = ItemEffect(
            damage=Dice.parse("2d6"),
            damage_type=DamageType.FIRE,
            save_ability=Ability.DEXTERITY,
            save_dc=13,
            provenance=FIXTURE,
        )
        rng = Random(11)
        thug = fighter("Thug", team="foes", position=0)
        thug.items = {"Alchemist's Fire": 2}
        victim = fighter("Victim", max_hp=30, hp=1, position=5)
        ally = fighter("Ally", position=40)  # a third combatant keeps the fight alive
        encounter = Encounter(
            [thug, victim, ally], rng, items={"Alchemist's Fire": fire}
        )

        def death_saves() -> dict[str, int]:
            for row in encounter.state()["combatants"]:
                if row["name"] == "Victim":
                    saves: dict[str, int] = row["death_saves"]
                    return saves
            raise AssertionError("Victim is not in the state")

        advance_to(encounter, "Thug", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(20))
        assert victim.dying

        # Accrue one real failure: a forced natural 5 fails every death save.
        for _ in range(12):
            encounter.advance(FixedRandom(5))
            if victim.death_save_failures:
                break
        assert death_saves() == {"successes": 0, "failures": 1}

        # A forced 15 succeeds, so reaching the thug's turn cannot add a failure
        # and cannot reach the three successes that would stabilise.
        advance_to(encounter, "Thug", FixedRandom(15))
        before = death_saves()
        events = encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Alchemist's Fire", target="Victim"),
            Random(3),
        )
        after = death_saves()

        assert "damage" in kinds(events)
        assert victim.hp == 0
        assert after["failures"] == before["failures"] + 1
        assert after["successes"] == before["successes"]
        assert victim.dying and not victim.dead


class TestADownedCreatureIsStillATarget:
    """A creature at 0 hit points is a legal target; only a corpse is not.

    SRD 5.2.1, Rules Glossary, "Unconscious [Condition]": "Attacks Affected. Attack
    rolls against you have Advantage." and "Automatic Critical Hits. Any attack
    roll that hits you is a Critical Hit if the attacker is within 5 feet of you."
    Both clauses are dead text if the stepper refuses the attack, which is what it
    used to do — and the Unconscious clause "Saving Throws Affected. You
    automatically fail Strength and Dexterity saving throws" is likewise dead if an
    area effect filters the creature out before rolling one.

    What the damage then costs is the other rule. SRD 5.2.1, "Playing the Game" ->
    "Damage at 0 Hit Points": "If you take any damage while you have 0 Hit Points,
    you suffer a Death Saving Throw failure. If the damage is from a Critical Hit,
    you suffer two failures instead. If the damage equals or exceeds your Hit Point
    maximum, you die."

    The hit point maximums here are deliberately far above anything the fixtures
    can roll, because the massive-damage clause is checked first: a fixture small
    enough to die would pin instant death rather than the failure count. The one
    test that *wants* that ordering sizes itself to reach it.
    """

    @staticmethod
    def _paused_on_the_attackers_turn(
        attacker: Creature, victim: Creature, *others: Creature
    ) -> Encounter:
        """A fight held on ``attacker``'s turn, with a third combatant to sustain it.

        The ally exists because ``Encounter.over`` counts only conscious creatures:
        without it, dropping the victim would end the fight and every action after
        would be refused for that reason rather than the one under test. It stands
        500 ft away so nothing under test can reach it.
        """
        rng = Random(8)
        ally = fighter("Ally", team=victim.team, position=500)
        encounter = Encounter(
            [attacker, victim, ally, *others], rng, spellbook=spellbook()
        )
        advance_to(encounter, attacker.name, rng)
        return encounter

    @staticmethod
    def _archer(name: str = "Archer", *, team: str = "foes") -> Creature:
        archer = fighter(name, team=team, position=0)
        archer.attacks = (
            AttackOption(
                name="Shortbow",
                attack_bonus=5,
                damage=Dice(1, 6, 2),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.RANGED,
                normal_range=80,
                long_range=320,
                provenance=FIXTURE,
            ),
        )
        return archer

    def test_the_unconscious_condition_reaches_an_attack_on_a_downed_target(
        self,
    ) -> None:
        # The condition table already carried both clauses; nothing could consult
        # them, because the only target they apply to was refused outright.
        thug = fighter("Thug", team="foes", position=0)
        victim = fighter("Victim", max_hp=200, hp=1, position=5)
        encounter = self._paused_on_the_attackers_turn(thug, victim)
        victim.take_damage(1)
        assert victim.dying

        option = thug.attacks[0]
        assert encounter.attack_advantage(thug, victim, option) is Advantage.ADVANTAGE
        assert encounter.attack_forced_critical(thug, victim) is True
        # The critical is scoped by distance, so it lapses out of melee while the
        # Advantage from Unconscious does not.
        victim.position = 30
        assert encounter.attack_forced_critical(thug, victim) is False

    def test_an_attack_on_a_dying_creature_lands_and_costs_one_failure(self) -> None:
        archer = self._archer()
        victim = fighter("Victim", max_hp=200, hp=1, position=30)
        encounter = self._paused_on_the_attackers_turn(archer, victim)
        victim.take_damage(1)
        assert victim.dying

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19)
        )
        assert "damage" in kinds(events)
        assert victim.hp == 0
        assert victim.death_save_failures == 1
        assert victim.dying and not victim.dead
        # From 30 ft the hit is an ordinary one, which is the point of the range:
        # a melee swing would force the critical and cost two.
        assert "critical" not in detail_of(events, "attack")

    def test_a_critical_hit_on_a_dying_creature_costs_two_failures(self) -> None:
        thug = fighter("Thug", team="foes", position=0)
        victim = fighter("Victim", max_hp=200, hp=1, position=5)
        encounter = self._paused_on_the_attackers_turn(thug, victim)
        victim.take_damage(1)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19)
        )
        # Not a natural 20: the critical comes from the target's condition.
        assert "critical hit" in detail_of(events, "attack")
        assert victim.death_save_failures == 2
        assert victim.dying and not victim.dead

    def test_a_critical_reaching_the_maximum_kills_instead_of_costing_failures(
        self,
    ) -> None:
        # The ordering inside ``take_damage``: massive damage is checked before the
        # failure count, so a forced critical big enough to reach the maximum kills
        # outright and accrues nothing. A doubled 1d8+3 tops out at 19.
        thug = fighter("Thug", team="foes", position=0)
        victim = fighter("Victim", max_hp=19, hp=1, position=5)
        encounter = self._paused_on_the_attackers_turn(thug, victim)
        victim.take_damage(1)

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19)
        )
        assert victim.dead
        assert victim.death_save_failures == 0
        assert detail_of(events, "death") == "damage exceeded maximum hit points"

    def test_three_failures_from_damage_kill_a_dying_creature(self) -> None:
        archer = self._archer()
        victim = fighter("Victim", max_hp=200, hp=1, position=30)
        encounter = self._paused_on_the_attackers_turn(archer, victim)
        victim.take_damage(1)
        victim.death_save_failures = 2

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19)
        )
        assert victim.dead
        assert detail_of(events, "death") == "a third failed death save"

    def test_an_attack_on_a_stable_creature_starts_its_death_saves_again(self) -> None:
        """SRD 5.2.1, "Stabilizing a Character": "A Stable creature doesn't make Death
        Saving Throws even though it has 0 Hit Points, but it still has the
        Unconscious condition. If the creature takes damage, it stops being Stable
        and starts making Death Saving Throws again."

        ``Creature.take_damage`` already did this; nothing could deliver the damage
        by attack, because a Stable creature is not conscious either.
        """
        archer = self._archer()
        victim = fighter("Victim", max_hp=200, hp=1, position=30)
        encounter = self._paused_on_the_attackers_turn(archer, victim)
        victim.take_damage(1)
        victim.stable = True
        assert not victim.dying

        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19))
        assert not victim.stable
        assert victim.dying
        assert victim.death_save_failures == 1

    def test_a_corpse_is_refused_as_an_attack_target(self) -> None:
        thug = fighter("Thug", team="foes", position=0)
        victim = fighter("Victim", max_hp=30, hp=1, position=5)
        encounter = self._paused_on_the_attackers_turn(thug, victim)
        victim.take_damage(1)
        victim.dead = True

        with pytest.raises(EncounterError, match="dead"):
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(19)
            )

    def test_an_area_spell_centred_on_a_dying_creature_damages_it(self) -> None:
        wren = caster("Wren", team="foes", position=0)
        victim = fighter("Victim", max_hp=200, hp=1, position=30)
        # A second creature inside the blast: the old behaviour damaged this one and
        # left the dying creature at the exact point of origin untouched.
        standing = fighter("Standing", max_hp=200, position=35)
        encounter = self._paused_on_the_attackers_turn(wren, victim, standing)
        victim.take_damage(1)
        assert victim.dying
        assert encounter.auto_fails_save(victim, Ability.DEXTERITY) is True

        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
            FixedRandom(1),
        )
        # Eight d6 forced to 1, and the dying creature fails the save automatically.
        assert victim.hp == 0
        assert victim.death_save_failures == 1
        assert standing.hp == standing.max_hp - 8
        touched = {event.target for event in events if event.kind == "spell_effect"}
        assert touched == {"Victim", "Standing"}

    def test_a_corpse_is_not_caught_in_an_area_spell(self) -> None:
        wren = caster("Wren", team="foes", position=0)
        victim = fighter("Victim", max_hp=30, hp=1, position=30)
        encounter = self._paused_on_the_attackers_turn(wren, victim)
        victim.take_damage(1)
        victim.dead = True
        # The ally sits at 500 ft; only the corpse is anywhere near the blast, so a
        # spell that still caught corpses would report an effect on it.
        with pytest.raises(EncounterError, match="no valid targets"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
                FixedRandom(1),
            )

    def test_a_spell_attack_critical_on_a_dying_creature_costs_two_failures(
        self,
    ) -> None:
        # The cast path's own critical. ``_do_cast`` reads it off the per-target
        # attack roll the kernel already produced; without that, Guiding Bolt loosed
        # point-blank at a downed creature doubled its dice and still cost one.
        wren = caster("Wren", team="foes", position=0)
        wren.spells = ("Guiding Bolt",)
        wren.spell_slots = {1: 2}
        victim = fighter("Victim", max_hp=200, hp=1, position=5)
        encounter = self._paused_on_the_attackers_turn(wren, victim)
        victim.take_damage(1)

        events = encounter.act(
            Action(
                kind=ActionKind.CAST,
                spell="Guiding Bolt",
                slot_level=1,
                target="Victim",
            ),
            FixedRandom(19),
        )
        assert "critical hit" in detail_of(events, "spell_effect")
        assert victim.death_save_failures == 2
        assert victim.dying and not victim.dead

    def test_each_target_of_one_cast_carries_its_own_critical(self) -> None:
        """The critical is read per target, because the kernel rolls it per target.

        ``resolve_spell`` makes a separate attack roll for each name, and the forced
        critical is scoped by the distance from the caster to *that* creature — so a
        single spell-wide flag would be wrong in both directions. Two downed targets
        in one cast, one adjacent and one across the room, separate the two: the near
        one is a critical and costs two failures, the far one is an ordinary hit and
        costs one. No bundled spell names more than one target, so this needs a
        fixture spell.
        """
        twin = Spell(
            name="Twin Bolt",
            level=1,
            requires_attack_roll=True,
            damage=Dice(1, 6, 0),
            damage_type=DamageType.RADIANT,
            range_feet=120,
            max_targets=2,
            provenance=FIXTURE,
        )
        wren = caster("Wren", team="foes", position=0)
        wren.spells = ("Twin Bolt",)
        wren.spell_slots = {1: 2}
        near = fighter("Near", max_hp=200, hp=1, position=5)
        far = fighter("Far", max_hp=200, hp=1, position=60)
        rng = Random(8)
        ally = fighter("Ally", position=500)
        encounter = Encounter(
            [wren, near, far, ally], rng, spellbook={"Twin Bolt": twin}
        )
        advance_to(encounter, "Wren", rng)
        near.take_damage(1)
        far.take_damage(1)

        encounter.act(
            Action(
                kind=ActionKind.CAST,
                spell="Twin Bolt",
                slot_level=1,
                targets=("Near", "Far"),
            ),
            FixedRandom(19),
        )
        assert near.death_save_failures == 2
        assert far.death_save_failures == 1


class TestMovementAndReactions:
    def test_leaving_a_threatened_space_draws_an_opportunity_attack(self) -> None:
        rng = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" in kinds(events)

    def test_a_reach_weapon_threatens_beyond_5_feet(self) -> None:
        # The pikeman sits at (15, 8): never within the old hardcoded 5 ft
        # threshold at any point on Thora's walk (the y offset alone is 8),
        # but within its own 10 ft reach while Thora passes x=5..25. Leaving
        # that band — the step from x=25 (distance 10) to x=30 (distance 15)
        # — should draw an opportunity attack the old MELEE_THRESHOLD gate
        # can never see, because the enemy is never "threatening" under it.
        rng = Random(6)
        pikeman = fighter("Pikeman", position=(15, 8), team="foes")
        pike = AttackOption(
            name="Pike",
            attack_bonus=5,
            damage=Dice(1, 10, 3),
            damage_type=DamageType.PIERCING,
            kind=AttackKind.MELEE,
            reach=10,
            provenance=FIXTURE,
        )
        pikeman.attacks = (pike,)
        encounter = Encounter([fighter(), pikeman], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" in kinds(events)

    def test_a_reach_5_attacker_still_gates_on_5_feet(self) -> None:
        # Regression pin: a reach-5 attacker must not gain any new opportunity
        # attack from the change above. Placed the same way as the reach-10
        # case (offset (15, 8)) it never threatens at all, since even Thora's
        # closest approach (x=15, distance 8) is outside its 5 ft reach.
        rng = Random(6)
        swordsman = fighter("Swordsman", position=(15, 8), team="foes")
        encounter = Encounter([fighter(), swordsman], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" not in kinds(events)

        # And it does provoke normally when the mover actually leaves 5 ft,
        # pinning today's ordinary behaviour untouched by the reach change.
        rng2 = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter2 = Encounter([fighter(), goblin], rng2)
        advance_to(encounter2, "Thora", rng2)
        events2 = encounter2.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" in kinds(events2)

    def test_an_unseen_mover_draws_no_opportunity_attack_and_keeps_the_reaction(
        self,
    ) -> None:
        # The SRD gates an opportunity attack on a creature "that you can see".
        # Thora turns invisible before moving, so the goblin — sighted, no
        # blindsight, no darkvision need here — should neither get the attack
        # nor spend the reaction it would need for something else.
        rng = Random(6)
        thora = fighter()
        thora.add_condition(Condition.INVISIBLE)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([thora, goblin], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" not in kinds(events)
        goblin_state = next(
            c for c in encounter.state()["combatants"] if c["name"] == "Goblin"
        )
        assert goblin_state["reaction_available"] is True

    def test_disengaging_first_prevents_the_opportunity_attack(self) -> None:
        rng = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DISENGAGE), rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" not in kinds(events)

    def test_passing_straight_through_reach_provokes_without_a_map(self) -> None:
        # The endpoint check never caught this: start and end both out of the
        # goblin's reach, with the straight walk crossing it on the way.
        rng = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=10)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert kinds(events).count("opportunity_attack") == 1

        # The reaction is spent, observed through the public surface rather than
        # through _reaction_available: Dash buys enough movement to walk back
        # across the goblin's reach in the same round, and that second provoking
        # pass draws nothing. The goblin's reaction only refreshes when its own
        # turn begins, which has not happened yet.
        encounter.act(Action(kind=ActionKind.DASH), rng)
        again = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=0), FixedRandom(20)
        )
        assert encounter.creatures["Thora"].position == (0, 0)
        assert "opportunity_attack" not in kinds(again)

    def test_a_disengaged_pass_through_does_not_provoke_without_a_map(self) -> None:
        rng = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=10)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DISENGAGE), rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" not in kinds(events)

    def test_a_mover_dropped_by_the_attack_stops_at_the_leave_point(self) -> None:
        # The move event still declares the full 30 ft, but the state is the
        # truth: Thora falls at (20, 0), the first sample beyond the goblin's
        # reach, not at the destination she never got to.
        rng = Random(6)
        thora = fighter(hp=1)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=10)
        encounter = Encounter([thora, goblin], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" in kinds(events)
        assert not thora.conscious
        assert as_point(thora.position) == (20, 0)

    def test_moving_further_than_the_remaining_speed_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="movement"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=80), rng)

    def test_dash_buys_a_second_helping_of_movement(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DASH), rng)
        encounter.act(Action(kind=ActionKind.MOVE, to_position=60), rng)
        assert encounter.creatures["Thora"].position == (60, 0)

    def test_a_grappled_creature_cannot_move(self) -> None:
        rng = Random(1)
        held = fighter("Held", position=0)
        held.add_condition(Condition.GRAPPLED)
        encounter = Encounter([held, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Held", rng)
        with pytest.raises(EncounterError, match="speed 0"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=20), rng)


class TestPlanarMovement:
    """Movement on the plane: two-dimensional destinations, diagonal rules."""

    def test_a_diagonal_move_costs_the_longer_axis_under_the_default_rule(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 15)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["origin"] == (0, 0)
        assert move.data["destination"] == (20, 15)
        assert move.data["cost"] == 20
        assert encounter.creatures["Thora"].position == (20, 15)
        assert encounter.state()["turn_state"]["movement_left"] == 10

    def test_the_5_10_5_rule_charges_every_second_diagonal_double(self) -> None:
        from fivee_sim.kernel.grid import DiagonalRule

        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=90)], rng,
            movement_rule=DiagonalRule.FIVE_TEN_FIVE,
        )
        advance_to(encounter, "Thora", rng)
        # (25, 25) is 25 + 12 = 37 ft under 5-10-5, past a 30 ft speed.
        with pytest.raises(EncounterError, match="movement"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(25, 25)), rng)
        # (20, 20) is 20 + 10 = 30 ft: exactly the speed.
        encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 20)), rng)
        assert encounter.state()["turn_state"]["movement_left"] == 0

    def test_state_reports_positions_as_x_y_pairs(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(position=(10, 20)), make_monster("Wolf", position=5)], rng
        )
        positions = {
            entry["name"]: entry["position"]
            for entry in encounter.state()["combatants"]
        }
        assert positions == {"Thora": [10, 20], "Wolf": [5, 0]}

    def test_waypoints_are_refused_without_a_battle_map(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="battle map"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0), path=((5, 0),)), rng
            )


def archer(name: str = "Sylvi", *, position: int | tuple[int, int] = 0,
           team: str = "party") -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=14,
        max_hp=20,
        attacks=(
            AttackOption(
                name="Shortbow",
                attack_bonus=5,
                damage=Dice(1, 6, 3),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.RANGED,
                normal_range=80,
                long_range=320,
                provenance=FIXTURE,
            ),
        ),
        position=position,
        provenance=FIXTURE,
    )


def fixture(
    name: str,
    square: Square,
    *,
    kind: str = "door",
    state: str = "closed",
    **extra: Any,
) -> MapFeatureRecord:
    """One fixture record, in the two words these tests keep saying about one.

    A fixture is a feature carrying a ``state``, so ``state`` is what this
    supplies and what makes every record it builds one the fight owns. The rest
    of the record's keys pass through: a door that names no ``terrain`` is a
    door in both states, and anything else that names none takes the tile it
    stands on — that resolution is ``MapFeatureRecord.own_terrain``'s, and it is
    deliberately not repeated here.
    """
    return MapFeatureRecord(id=name, kind=kind, at=square, state=state, **extra)


def strip(
    width: int,
    height: int = 1,
    *,
    terrain: dict[Square, str] | None = None,
    elevation: dict[Square, int] | None = None,
    features: tuple[MapFeatureRecord, ...] = (),
) -> MapDocument:
    return MapDocument.flat(
        name="test map",
        width=width,
        height=height,
        terrain=terrain or {},
        elevation=elevation or {},
        features=features,
        provenance=fixture_provenance(FIXTURE),
    )


#: The one glyph :func:`tower` and its neighbours draw with. A hand-built
#: document needs a legend that covers its tiles, and these fixtures are one
#: kind of ground throughout.
FLOOR_LEGEND = {".": "floor", "#": "wall", "%": "difficult", "~": "water"}


def storeys(
    *levels: MapLevel, width: int = 4, height: int = 1, name: str = "tower"
) -> MapDocument:
    """A multi-storey document over one footprint, which ``flat`` cannot build.

    ``MapDocument.flat`` is the one-plane constructor, so every fight here that
    needs a floor above it builds the document directly — and that is also what
    makes these fixtures the unvalidated hand-built maps ``_adopt_map`` exists
    to refuse.
    """
    return MapDocument(
        name=name,
        grid=MapGrid(width=width, height=height),
        legend=FLOOR_LEGEND,
        provenance=fixture_provenance(FIXTURE),
        levels=MappingProxyType({level.index: level for level in levels}),
    )


def floor(
    index: int,
    *,
    width: int = 4,
    height: int = 1,
    terrain: dict[Square, str] | None = None,
    feet: int = 0,
    features: tuple[MapFeatureRecord, ...] = (),
) -> MapLevel:
    """One storey of :func:`storeys`, all floor but for the squares named."""
    glyphs = {kind: glyph for glyph, kind in FLOOR_LEGEND.items()}
    named = terrain or {}
    return MapLevel(
        index=index,
        name=f"level-{index}",
        tiles=tuple(
            "".join(glyphs[named.get((x, y), "floor")] for x in range(width))
            for y in range(height)
        ),
        features=features,
        elevation=MapElevation(default=feet),
    )


def stair(name: str, square: Square, to_level: int) -> MapFeatureRecord:
    """A drawn stairway that leads somewhere — a connector and no fixture."""
    return MapFeatureRecord(id=name, kind="stairs_up", at=square, to_level=to_level)


def tower(
    *,
    stair_at: Square = (1, 0),
    upper_feet: int = 10,
    ground_terrain: dict[Square, str] | None = None,
    upper_terrain: dict[Square, str] | None = None,
) -> MapDocument:
    """Two 4x1 floors over one footprint, joined by a stair, the upper one raised."""
    return storeys(
        floor(0, terrain=ground_terrain, features=(stair("stair-up", stair_at, 1),)),
        floor(
            1,
            terrain=upper_terrain,
            feet=upper_feet,
            features=(stair("stair-down", stair_at, 0),),
        ),
    )


class TestLevels:
    def test_a_creature_stands_on_the_ground_unless_it_says_otherwise(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, map_document=tower()
        )
        assert [c["level"] for c in encounter.state()["combatants"]] == [0, 0]

    def test_two_creatures_may_hold_one_square_on_different_levels(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(0, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(position=(0, 0)), upstairs], rng, map_document=tower())
        # Both resolve to the same square; only the level tells them apart. On
        # one plane this pair is refused ("both start in square").
        thora, wolf = encounter.creatures["Thora"], encounter.creatures["Wolf"]
        assert to_square(as_point(thora.position)) == to_square(as_point(wolf.position))
        assert (thora.level, wolf.level) == (0, 1)

    def test_a_floor_is_total_cover(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(0, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(position=(0, 0)), upstairs], rng, map_document=tower())
        assert encounter.cover_between("Thora", "Wolf") is CoverGrade.TOTAL

    def test_an_attack_through_a_floor_finds_no_line(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(0, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(position=(0, 0)), upstairs], rng, map_document=tower())
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wolf"), rng
        )
        attack = next(event for event in events if event.kind == "attack")
        assert attack.data["total_cover"] is True

    def test_an_enemy_upstairs_is_not_an_enemy_within_reach(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(0, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(position=(0, 0)), upstairs], rng, map_document=tower())
        assert encounter.enemies_of("Thora") == []

    def test_a_connector_carries_a_mover_up(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, map_document=tower()
        )
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng
        )
        thora = encounter.creatures["Thora"]
        assert thora.level == 1
        assert to_square(as_point(thora.position)) == (1, 0)

    def test_climbing_a_storey_costs_the_climb(self) -> None:
        # 5 ft to walk to the stair, then a 10-foot rise: over CLIMB_FEET, so
        # 5 ft of horizontal plus 2 ft per foot climbed = 25. Exactly a speed.
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, map_document=tower()
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng
        )
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 30
        assert move.data["to_level"] == 1
        assert encounter.state()["turn_state"]["movement_left"] == 0

    def test_a_storey_too_high_to_climb_is_refused(self) -> None:
        # 5 ft to the stair plus a 40-foot rise at 2 ft per foot climbed, on top
        # of the 5-foot step in: 5 + 5 + 80 = 90, three times a fighter's speed.
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            map_document=tower(upper_feet=40),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="needs 90 ft"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng)

    def test_a_move_to_a_level_needs_a_connector_on_the_square(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, map_document=tower()
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="nothing at .* leads to level 1"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0), to_level=1), rng)

    def test_a_move_to_a_level_the_map_does_not_have_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, map_document=tower()
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no level 7"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=7), rng)

    def test_a_wall_upstairs_does_not_block_the_ground(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            map_document=tower(upper_terrain={(2, 0): "wall"}),
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 10

    def test_a_creature_upstairs_reports_the_storeys_height(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(0, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(position=(0, 0)), upstairs], rng, map_document=tower())
        wolf = next(c for c in encounter.state()["combatants"] if c["name"] == "Wolf")
        assert (wolf["level"], wolf["elevation"]) == (1, 10)

    def test_a_connector_arriving_in_a_wall_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            map_document=tower(upper_terrain={(1, 0): "wall"}),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="arrives on impassable 'wall'"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng)

    def test_a_connector_arriving_on_an_occupied_square_is_refused(self) -> None:
        rng = Random(1)
        upstairs = make_monster("Wolf", position=(5, 0))
        upstairs.level = 1
        encounter = Encounter([fighter(), upstairs], rng, map_document=tower())
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="on level 1 is occupied by Wolf"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng)

    def test_a_connector_to_a_level_the_map_lacks_is_refused_at_adoption(self) -> None:
        # A hand-built battle map can carry one; the document parser refuses it
        # earlier, but the map need not have come from a document.
        rng = Random(1)
        broken = storeys(
            floor(0, features=(stair("stair-up", (1, 0), 3),)), name="broken tower"
        )
        with pytest.raises(
            EncounterError,
            match=(
                r"feature 'stair-up' leads to level 3, but there is no level 3 in "
                r"this map\. Declared: 0"
            ),
        ):
            Encounter([fighter(), make_monster("Wolf", position=(15, 0))], rng,
                      map_document=broken)

    def test_a_combatant_placed_on_a_level_the_map_lacks_is_refused(self) -> None:
        rng = Random(1)
        stray = make_monster("Wolf", position=(15, 0))
        stray.level = 3
        with pytest.raises(EncounterError, match="level 3"):
            Encounter([fighter(), stray], rng, map_document=tower())

    def test_the_map_summary_lists_every_level(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, map_document=tower()
        )
        levels = encounter.state()["map"]["levels"]
        assert [level["index"] for level in levels] == [0, 1]
        assert levels[1]["elevation"]["default"] == 10


class TestMapMovement:
    def test_difficult_terrain_charges_double_for_every_entered_square(self) -> None:
        rng = Random(1)
        map_document = strip(6, terrain={(2, 0): "difficult", (3, 0): "difficult"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(25, 0))], rng,
            map_document=map_document,
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 30  # 5 + 10 + 10 + 5
        assert move.data["squares"] == [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0]]
        assert encounter.state()["turn_state"]["movement_left"] == 0

    def test_a_move_the_terrain_makes_unaffordable_is_refused(self) -> None:
        rng = Random(1)
        map_document = strip(7, terrain={(2, 0): "difficult", (3, 0): "difficult"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(30, 0))], rng,
            map_document=map_document,
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="needs 35 ft"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(25, 0)), rng)

    def test_a_wall_forces_the_route_around_it(self) -> None:
        rng = Random(1)
        map_document = strip(4, 3, terrain={(1, 0): "wall", (1, 1): "wall"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 10))], rng,
            map_document=map_document,
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 20
        walked = {tuple(square) for square in move.data["squares"]}
        assert not walked & {(1, 0), (1, 1)}

    def test_a_walled_off_destination_is_refused(self) -> None:
        rng = Random(1)
        map_document = strip(4, terrain={(1, 0): "wall"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            map_document=map_document,
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no route"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)

    def test_a_move_may_not_end_on_a_conscious_creature(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(10, 0))], rng,
            map_document=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="occupied by Wolf"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)

    def test_a_move_off_the_map_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(10, 0))], rng,
            map_document=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="off the 5x1 map"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(40, 0)), rng)

    def test_allies_can_be_crossed_but_not_stopped_on(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [
                fighter(),
                fighter("Ally", position=(10, 0)),
                make_monster("Wolf", position=(20, 0)),
            ],
            rng,
            map_document=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(15, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert [2, 0] in move.data["squares"]  # straight through the ally

    def test_an_enemy_blocks_the_only_route(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [
                fighter(),
                make_monster("Goblin Warrior", label="Goblin", position=(10, 0)),
                make_monster("Wolf", position=(20, 0)),
            ],
            rng,
            map_document=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no route"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(15, 0)), rng)

    def test_passing_through_reach_provokes_even_when_the_move_ends_clear(self) -> None:
        # The 1-D endpoint check never caught this: start and end both out of
        # reach, with the walk crossing the goblin's threat on the way.
        rng = Random(6)
        map_document = strip(5, 2)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=(10, 5))
        encounter = Encounter([fighter(), goblin], rng, map_document=map_document)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(20, 0)), FixedRandom(20)
        )
        assert "opportunity_attack" in kinds(events)

    def test_disengage_suppresses_the_pass_through_attack(self) -> None:
        rng = Random(6)
        map_document = strip(5, 2)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=(10, 5))
        encounter = Encounter([fighter(), goblin], rng, map_document=map_document)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DISENGAGE), rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(20, 0)), FixedRandom(20)
        )
        assert "opportunity_attack" not in kinds(events)

    def test_an_explicit_path_is_honoured_when_legal(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            map_document=strip(5, 2),
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(
                kind=ActionKind.MOVE,
                to_position=(10, 0),
                path=((5, 5), (10, 5), (10, 0)),
            ),
            rng,
        )
        move = next(event for event in events if event.kind == "move")
        assert move.data["squares"] == [[0, 0], [1, 1], [2, 1], [2, 0]]
        assert move.data["cost"] == 15

    def test_a_path_with_a_gap_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            map_document=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="not to an adjacent square"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0), path=((10, 0),)),
                rng,
            )

    def test_a_path_through_a_wall_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            map_document=strip(4, terrain={(1, 0): "wall"}),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="impassable 'wall'"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0),
                       path=((5, 0), (10, 0))),
                rng,
            )

    def test_a_path_must_end_at_the_destination(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            map_document=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="ends at"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0), path=((5, 0),)),
                rng,
            )


class TestMapElevation:
    """Ground height on a fight's map: slopes, climbs, and what it does not touch."""

    def fight(
        self, map_document: MapDocument, rng: Random, wolf: Point = (25, 10)
    ) -> Encounter:
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=wolf)], rng,
            map_document=map_document,
        )
        advance_to(encounter, "Thora", rng)
        return encounter

    def moving(
        self,
        map_document: MapDocument,
        to: Point,
        rng: Random,
        wolf: Point = (25, 10),
        **kwargs: Any,
    ) -> dict[str, Any]:
        encounter = self.fight(map_document, rng, wolf=wolf)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=to, **kwargs), rng)
        return next(event for event in events if event.kind == "move").data

    def test_a_slope_is_difficult_terrain(self) -> None:
        # A one-row corridor, so the route cannot decline the grade.
        rng = Random(1)
        map_document = strip(6, elevation={(2, 0): 5, (3, 0): 10, (4, 0): 10})
        move = self.moving(map_document, (20, 0), rng, wolf=(25, 0))
        assert move["cost"] == 5 + 10 + 10 + 5  # only the two rises cost double

    def test_a_slope_through_rough_going_is_not_doubled_twice(self) -> None:
        # SRD 5.2.1: Difficult Terrain "isn't cumulative" — a slope over
        # undergrowth is the same 10 feet a slope over grass is, not 20.
        rng = Random(1)
        map_document = strip(
            6,
            terrain={(2, 0): "difficult"},
            elevation={(2, 0): 5, (3, 0): 5, (4, 0): 5},
        )
        move = self.moving(map_document, (20, 0), rng, wolf=(25, 0))
        assert move["cost"] == 5 + 10 + 5 + 5

    def test_a_cliff_costs_the_climb(self) -> None:
        rng = Random(1)
        # A 10-foot face: the step into it costs the square plus a foot for each
        # foot climbed, which is most of a 30-foot Speed for one square.
        move = self.moving(strip(6, 3, elevation={(1, 0): 10}), (5, 0), rng)
        assert move["cost"] == 5 + 20
        assert move["squares"] == [[0, 0], [1, 0]]

    def test_climbing_down_costs_what_climbing_up_costs(self) -> None:
        rng = Random(1)
        # Thora starts at (0, 0), which this map puts on a 10-foot ledge.
        move = self.moving(strip(6, 3, elevation={(0, 0): 10}), (5, 0), rng)
        assert move["cost"] == 5 + 20

    def test_a_route_walks_round_a_cliff_to_reach_its_top(self) -> None:
        rng = Random(1)
        # A 20-foot plateau along the top row, walled off head-on but reachable
        # up a ramp that rises five feet a square through row 1.
        map_document = strip(
            6, 2,
            elevation={
                (2, 0): 20, (3, 0): 20, (4, 0): 20, (5, 0): 20,
                (2, 1): 5, (3, 1): 10, (4, 1): 15, (5, 1): 20,
            },
        )
        route = self.fight(map_document, rng, wolf=(0, 5)).route("Thora", (5, 0))
        assert route is not None
        walked = set(route.squares)
        assert not walked & {(2, 0), (3, 0)}  # never up the face
        assert route.cost_feet == 5 + 10 * 4  # one level step, then four slopes

    def test_a_climb_beyond_the_budget_is_refused(self) -> None:
        rng = Random(1)
        encounter = self.fight(strip(6, 3, elevation={(1, 0): 60}), rng)
        with pytest.raises(EncounterError, match="needs 125 ft"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0)), rng)

    def test_an_explicit_path_is_charged_the_same_climb(self) -> None:
        rng = Random(1)
        move = self.moving(
            strip(6, 3, elevation={(2, 0): 10}), (10, 0), rng,
            path=((5, 0), (10, 0)),
        )
        assert move["cost"] == 5 + (5 + 20)

    def test_sight_and_cover_are_measured_flat(self) -> None:
        # The limit this version keeps: a ridge between two creatures screens
        # neither of them.
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            map_document=strip(6, elevation={(2, 0): 40}),
        )
        assert encounter.cover_between("Thora", "Wolf") is CoverGrade.NONE

    def test_state_reports_the_ground_underfoot(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            map_document=strip(6, elevation={(4, 0): 25}),
        )
        state = encounter.state()
        heights = {c["name"]: c["elevation"] for c in state["combatants"]}
        assert heights == {"Thora": 0, "Wolf": 25}
        assert state["map"]["elevation"]["flat"] is False
        assert (state["map"]["elevation"]["min"], state["map"]["elevation"]["max"]) == (0, 25)

    def test_a_fight_without_a_map_reports_no_ground_at_all(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=(20, 0))], rng)
        assert all("elevation" not in c for c in encounter.state()["combatants"])


class TestMapPlacement:
    def test_starting_inside_a_wall_is_refused(self) -> None:
        with pytest.raises(EncounterError, match="impassable 'wall'"):
            Encounter(
                [fighter(position=(5, 0)), make_monster("Wolf", position=(15, 0))],
                Random(1),
                map_document=strip(4, terrain={(1, 0): "wall"}),
            )

    def test_starting_off_the_map_is_refused(self) -> None:
        with pytest.raises(EncounterError, match="off the 4x1 map"):
            Encounter(
                [fighter(position=(25, 0)), make_monster("Wolf", position=(15, 0))],
                Random(1),
                map_document=strip(4),
            )

    def test_two_combatants_may_not_share_a_square(self) -> None:
        with pytest.raises(EncounterError, match="both start in square"):
            Encounter(
                [fighter(position=(0, 0)), make_monster("Wolf", position=(2, 2))],
                Random(1),
                map_document=strip(4),
            )

    def test_positions_snap_to_the_centre_of_their_square(self) -> None:
        encounter = Encounter(
            [fighter(position=(7, 3)), make_monster("Wolf", position=(15, 0))],
            Random(1),
            map_document=strip(4),
        )
        assert encounter.creatures["Thora"].position == (5, 0)

    def test_a_map_naming_unknown_terrain_is_refused_with_the_loaded_kinds(
        self,
    ) -> None:
        with pytest.raises(EncounterError, match="vale-lava"):
            Encounter(
                [fighter(), make_monster("Wolf", position=(15, 0))],
                Random(1),
                map_document=strip(4, terrain={(2, 0): "vale-lava"}),
            )

    def test_the_map_block_appears_in_state(self) -> None:
        door = fixture(name="crypt door", square=(1, 0))
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))],
            Random(1),
            map_document=strip(4, features=(door,)),
        )
        block = encounter.state()["map"]
        assert block == {
            "name": "test map",
            "width": 4,
            "height": 1,
            "movement_rule": "5-5-5",
            "elevation": {
                "default": 0,
                "min": 0,
                "max": 0,
                "flat": True,
                "affects": "movement only; sight, cover, and areas are measured flat",
            },
            "levels": [
                {
                    "index": 0,
                    "elevation": {
                        "default": 0,
                        "min": 0,
                        "max": 0,
                        "flat": True,
                        "affects": (
                            "movement only; sight, cover, and areas are measured flat"
                        ),
                    },
                    "connectors": [],
                },
            ],
            "features": {
                "crypt door": {
                    "square": [1, 0], "kind": "door", "level": 0, "open": False,
                },
            },
        }

    def test_a_mapless_fight_reports_no_map(self) -> None:
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], Random(1))
        assert encounter.state()["map"] is None


class TestAMalformedDocumentIsRefusedRatherThanRaised:
    """Three failures the battle-map bridge used to absorb before a fight saw them.

    ``to_grid`` walked every square through the legend and rebuilt the tiles as
    a sparse mapping, so a glyph with no legend entry and a row that did not
    reach the grid's width were both spent by the time ``_adopt_map`` ran. There
    is no bridge now: ``MapLevel.terrain_at`` reads ``legend[tiles[y][x]]`` at
    the moment a fight asks, and both of those raise ``LookupError`` rather than
    ``ValueError``. The third is ``ambient_light``, which
    ``_adopt_map`` reads through ``LightLevel(level.ambient_light)`` — a bare
    ``ValueError``, which is not an ``EncounterError`` and so is caught by
    nothing between here and the adapter.

    That distinction is the whole point of these cases. ``EncounterError`` is a
    ``ValueError``, which ``web/http_server.py`` answers as problem+json; a
    ``KeyError``, an ``IndexError`` or a bare ``ValueError`` escaping
    ``Encounter.__init__`` is a 500 —
    ``service/encounters.py`` catches ``EncounterError`` by type.
    So a map a person could plausibly hand-build would turn a refusal the caller
    can act on into a server fault, *and* the first two would not surface at
    construction at all — the first creature to look at that square would raise
    it, several turns into a fight.

    A hand-built document is exactly as unvalidated as a hand-built battle map
    was: ``parse_document`` lives in ``map_document``, which ``model`` may not
    import. These build the document directly for that reason — ``MapDocument.flat``
    allocates a legend that covers its own tiles and writes rows to width, so it
    cannot express any of the three.

    That is also the honest bound on all three: a document arriving over
    ``/api/v1`` is parsed first, and the parser refuses each of them by the same
    rule. What these cover is the library caller who builds a ``MapDocument``
    and hands it to ``Encounter`` — a supported use, and the one ``_adopt_map``
    asks the plane rules for.
    """

    def broken(
        self,
        *,
        legend: dict[str, str],
        tiles: tuple[str, ...],
        ambient_light: str = "bright",
    ) -> MapDocument:
        return MapDocument(
            name="broken",
            grid=MapGrid(width=4, height=2),
            legend=legend,
            provenance=MapProvenance(
                generator="hand", seed=0, params={}, edited=False, source=FIXTURE
            ),
            levels=MappingProxyType(
                {
                    0: MapLevel(
                        index=0,
                        name="ground",
                        tiles=tiles,
                        features=(),
                        ambient_light=ambient_light,
                    )
                }
            ),
        )

    def roster(self) -> list[Creature]:
        return [fighter(position=(0, 0)), make_monster("Wolf", position=(15, 0))]

    def test_a_glyph_the_legend_does_not_define_is_refused(self) -> None:
        document = self.broken(
            legend={".": "normal"}, tiles=("..?.", "....")
        )
        with pytest.raises(
            EncounterError, match=r"level 0 draws '\?', which this map's legend"
        ):
            Encounter(self.roster(), Random(1), map_document=document)

    def test_the_refusal_names_the_glyphs_the_legend_does_define(self) -> None:
        # A refusal that says only "unknown glyph" leaves the author guessing
        # which character they meant, exactly as the terrain refusal beside it
        # lists the loaded kinds.
        document = self.broken(
            legend={".": "normal", "#": "wall"}, tiles=("..?.", "....")
        )
        with pytest.raises(EncounterError, match=r"the legend has: '#', '\.'"):
            Encounter(self.roster(), Random(1), map_document=document)

    def test_a_row_short_of_the_grids_width_is_refused(self) -> None:
        document = self.broken(legend={".": "normal"}, tiles=("....", "..."))
        with pytest.raises(
            EncounterError, match=r"level 0 row 1 is 3 squares wide on a 4x2 map"
        ):
            Encounter(self.roster(), Random(1), map_document=document)

    def test_a_level_short_of_the_grids_height_is_refused(self) -> None:
        document = self.broken(legend={".": "normal"}, tiles=("....",))
        with pytest.raises(
            EncounterError, match=r"level 0 has 1 row on a 4x2 map"
        ):
            Encounter(self.roster(), Random(1), map_document=document)

    def test_an_ambient_light_outside_the_vocabulary_is_refused(self) -> None:
        # Not a ``LookupError`` like the two above: ``LightLevel('dusk')`` is a
        # bare ``ValueError``, so without ``plane_findings``' guard it travels
        # all the way out as one and the caller is told 'ValueError: ...' with a
        # 500 rather than which levels are lit and how.
        document = self.broken(
            legend={".": "normal"}, tiles=("....", "...."), ambient_light="dusk"
        )
        with pytest.raises(
            EncounterError,
            match=r"level 0 is lit 'dusk', which is not a light level; "
            r"the light levels are: bright, dim, darkness",
        ):
            Encounter(self.roster(), Random(1), map_document=document)

    def test_a_well_formed_document_of_the_same_shape_still_starts(self) -> None:
        # The vacuity guard: three refusals over a document shape that never
        # worked would prove nothing about the two defects being named.
        document = self.broken(legend={".": "normal"}, tiles=("....", "...."))

        encounter = Encounter(self.roster(), Random(1), map_document=document)

        assert encounter._terrain_at_level(0, (2, 1)) == "normal"


class TestCoverChangesTheAttack:
    #: A full wall column with no gap: the only geometry that seals sight, since
    #: the corner rule sees past a lone pillar.
    WALL_COLUMN = {(2, 0): "wall", (2, 1): "wall", (2, 2): "wall"}

    def duel(self, terrain: dict[Square, str]) -> Encounter:
        rng = Random(3)
        encounter = Encounter(
            [archer(position=(0, 5)),
             make_monster("Goblin Warrior", label="Goblin", position=(20, 5))],
            rng,
            map_document=strip(5, 3, terrain=terrain),
        )
        advance_to(encounter, "Sylvi", rng)
        return encounter

    def test_half_cover_turns_a_pinned_hit_into_a_miss(self) -> None:
        # Natural 11 + 5 = 16: a hit against AC 15 in the open, a miss against
        # 15 + 2 behind the half-cover pillar. Same roll, different fight.
        open_ground = self.duel({})
        events = open_ground.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(11)
        )
        assert events[0].data["hit"]
        assert events[0].data["cover"] == 0

        covered = self.duel({(2, 1): "half-cover"})
        events = covered.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(11)
        )
        assert not events[0].data["hit"]
        assert events[0].data["cover"] == 1
        assert "half cover, +2 AC" in events[0].detail

    def test_total_cover_refuses_without_consuming_the_attack(self) -> None:
        sealed = self.duel(self.WALL_COLUMN)
        before = sealed.state()["turn_state"]
        events = sealed.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(20)
        )
        assert events[0].data["total_cover"]
        assert "total cover" in events[0].detail
        goblin = sealed.creatures["Goblin"]
        assert goblin.hp == goblin.max_hp
        assert sealed.state()["turn_state"] == before

    def test_cover_between_is_the_public_authority(self) -> None:
        assert self.duel({}).cover_between("Sylvi", "Goblin") is CoverGrade.NONE
        assert self.duel({(2, 1): "half-cover"}).cover_between(
            "Sylvi", "Goblin"
        ) is CoverGrade.HALF
        assert self.duel(self.WALL_COLUMN).cover_between(
            "Sylvi", "Goblin"
        ) is CoverGrade.TOTAL

    def test_an_intervening_creature_grants_half_cover(self) -> None:
        rng = Random(3)
        encounter = Encounter(
            [
                archer(position=(0, 5)),
                fighter("Ally", position=(10, 5)),
                make_monster("Goblin Warrior", label="Goblin", position=(20, 5)),
            ],
            rng,
            map_document=strip(5, 3),
        )
        assert encounter.cover_between("Sylvi", "Goblin") is CoverGrade.HALF


class TestCoverShieldsSaves:
    """Cover on Dexterity saves against areas, measured from the effect's origin."""

    def goblin_effect(self, events: Sequence[Event]) -> Event:
        return next(
            event for event in events
            if event.kind == "spell_effect" and event.target == "Goblin"
        )

    def fireball_at_own_feet(self, terrain: dict[Square, str]) -> Sequence[Event]:
        """Wren drops a Fireball on her own square; the goblin sits 20 ft out,
        with whatever ``terrain`` puts between the origin and it."""
        rng = Random(3)
        encounter = Encounter(
            [caster(position=(0, 5)),
             make_monster("Goblin Warrior", label="Goblin", position=(20, 5))],
            rng,
            spellbook=spellbook(),
            map_document=strip(5, 3, terrain=terrain),
        )
        advance_to(encounter, "Wren", rng)
        return encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                   center=(0, 5)),
            FixedRandom(12),
        )

    def test_half_cover_flips_a_pinned_dexterity_save(self) -> None:
        # Natural 12 + 2 (Dex) = 14: a failure against DC 15 in the open. Behind
        # the half-cover pillar the same roll gains +2 and saves at 16.
        in_the_open = self.goblin_effect(self.fireball_at_own_feet({}))
        assert in_the_open.data["saved"] is False
        assert "cover" not in in_the_open.data

        behind_cover = self.goblin_effect(
            self.fireball_at_own_feet({(2, 1): "half-cover"})
        )
        assert behind_cover.data["saved"] is True
        assert behind_cover.data["cover"] == 1

    def test_a_non_dexterity_save_gets_no_cover_bonus(self) -> None:
        # Shatter saves on Constitution: the goblin's half cover is reported in
        # the payload but grants nothing. Natural 13 + 0 (Con) = 13 fails DC 15;
        # were the +2 wrongly applied, 15 would save.
        rng = Random(3)
        wizard = caster(position=(0, 5))
        wizard.spells = ("Shatter",)
        encounter = Encounter(
            [wizard, make_monster("Goblin Warrior", label="Goblin",
                                  position=(20, 5))],
            rng,
            spellbook=spellbook(),
            map_document=strip(5, 3, terrain={(3, 1): "half-cover"}),
        )
        advance_to(encounter, "Wren", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Shatter", slot_level=2,
                   center=(10, 5)),
            FixedRandom(13),
        )
        effect = self.goblin_effect(events)
        assert effect.data["cover"] == 1
        assert effect.data["saved"] is False

    def test_total_cover_from_the_origin_excludes_the_target(self) -> None:
        # The sealed goblin is inside the template — 15 ft from the origin — but
        # a full-height wall stands between; the blast does not reach around it.
        rng = Random(3)
        encounter = Encounter(
            [
                caster(position=(0, 5)),
                make_monster("Goblin Warrior", label="Near", position=(10, 5)),
                make_monster("Goblin Warrior", label="Sealed", position=(30, 5)),
            ],
            rng,
            spellbook=spellbook(),
            map_document=strip(
                8, 3, terrain={(4, 0): "wall", (4, 1): "wall", (4, 2): "wall"}
            ),
        )
        advance_to(encounter, "Wren", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                   center=(15, 5)),
            Random(2),
        )
        struck = {e.target for e in events if e.kind == "spell_effect"}
        assert "Near" in struck
        assert "Sealed" not in struck
        sealed = encounter.creatures["Sealed"]
        assert sealed.hp == sealed.max_hp


class TestCoverReachesNamedTargetSpells:
    """Cover on a spell aimed at a named creature, measured from the caster.

    SRD 5.2.1, "Cover" (p. 179): Half Cover is "+2 bonus to AC and Dexterity saving
    throws", Three-Quarters Cover "+5", and Total Cover "can't be targeted
    directly" — *directly* being the word that separates this from an area, which
    reaches whoever its template catches. The rule names no attack/spell split, so
    a spell aimed at a creature is shielded exactly as an arrow is.

    ``TestCoverShieldsSaves`` covers the area branch; this is the named one, which
    used to consult cover nowhere at all.
    """

    #: Copied rather than aliased: the sibling class holds a mutable dict, and a
    #: shared binding across two classes is one careless ``[...] = ...`` away from
    #: cross-class pollution that no test would attribute correctly.
    WALL_COLUMN = dict(TestCoverChangesTheAttack.WALL_COLUMN)

    def duel(self, terrain: dict[Square, str], *, spell: str = "Guiding Bolt",
             book: dict[str, Spell] | None = None) -> Encounter:
        rng = Random(3)
        wren = caster(position=(0, 5))
        wren.spells = (spell,)
        wren.spell_slots = {1: 1}
        encounter = Encounter(
            [wren, make_monster("Goblin Warrior", label="Goblin", position=(20, 5))],
            rng,
            spellbook=spellbook() if book is None else book,
            map_document=strip(5, 3, terrain=terrain),
        )
        advance_to(encounter, "Wren", rng)
        return encounter

    def test_total_cover_refuses_a_named_target_spell(self) -> None:
        sealed = self.duel(self.WALL_COLUMN)
        before = sealed.state()["turn_state"]
        with pytest.raises(EncounterError, match="total cover"):
            sealed.act(
                Action(kind=ActionKind.CAST, spell="Guiding Bolt", slot_level=1,
                       target="Goblin"),
                FixedRandom(20),
            )
        goblin = sealed.creatures["Goblin"]
        assert goblin.hp == goblin.max_hp
        # Refused before anything is spent, exactly as an out-of-range cast is.
        assert sealed.creatures["Wren"].spell_slots[1] == 1
        assert sealed.state()["turn_state"] == before

    def test_half_cover_raises_ac_against_a_spell_attack_roll(self) -> None:
        # Natural 9 + 6 = 15: a hit against the goblin's AC 15 in the open, a
        # miss against 15 + 2 behind the pillar. Same roll, same seed.
        in_the_open = self.duel({}).act(
            Action(kind=ActionKind.CAST, spell="Guiding Bolt", slot_level=1,
                   target="Goblin"),
            FixedRandom(9),
        )
        struck = next(e for e in in_the_open if e.kind == "spell_effect")
        assert struck.data["affected"] and struck.data["damage"]
        assert "vs AC 15 -> hit" in struck.detail

        behind_cover = self.duel({(2, 1): "half-cover"}).act(
            Action(kind=ActionKind.CAST, spell="Guiding Bolt", slot_level=1,
                   target="Goblin"),
            FixedRandom(9),
        )
        shielded = next(e for e in behind_cover if e.kind == "spell_effect")
        assert not shielded.data["affected"] and shielded.data["damage"] == 0
        assert shielded.data["cover"] == 1
        # The raised AC is in the log, not just the outcome: a bare "miss" would
        # pass against a build that rolled worse rather than one that applied +2.
        assert "vs AC 17 -> miss" in shielded.detail

    def test_half_cover_shields_a_named_dexterity_save(self) -> None:
        # No bundled spell aims a Dexterity save at a named creature, so this
        # needs a fixture. Natural 12 + 2 (Dex) = 14 fails DC 15; the +2 for half
        # cover makes the same roll a 16 and a save.
        ray = Spell(
            name="Searing Ray",
            level=1,
            save_ability=Ability.DEXTERITY,
            damage=Dice(3, 6, 0),
            damage_type=DamageType.FIRE,
            range_feet=120,
            provenance=FIXTURE,
        )
        book = {"Searing Ray": ray}
        in_the_open = self.duel({}, spell="Searing Ray", book=book).act(
            Action(kind=ActionKind.CAST, spell="Searing Ray", slot_level=1,
                   target="Goblin"),
            FixedRandom(12),
        )
        assert next(
            e for e in in_the_open if e.kind == "spell_effect"
        ).data["saved"] is False

        behind_cover = self.duel(
            {(2, 1): "half-cover"}, spell="Searing Ray", book=book
        ).act(
            Action(kind=ActionKind.CAST, spell="Searing Ray", slot_level=1,
                   target="Goblin"),
            FixedRandom(12),
        )
        shielded = next(e for e in behind_cover if e.kind == "spell_effect")
        assert shielded.data["saved"] is True
        assert shielded.data["cover"] == 1

    def test_a_non_dexterity_named_save_gets_no_cover_bonus(self) -> None:
        """Half cover is *reported* for a Wisdom save and grants nothing.

        The roll is chosen so the +2 straddles the DC, which is the whole point of
        the case: the Goblin Warrior's Wisdom save modifier is **-1**, so natural
        14 resolves to 13 against DC 15 and fails, while a wrongly-applied cover
        bonus would make it exactly 15 and save. An earlier version of this test
        used natural 12 — 11 against DC 15, a four-point margin the +2 could not
        cross — so it passed whether or not the bonus was applied.
        """
        covered = self.duel({(2, 1): "half-cover"}, spell="Hold Person")
        covered.creatures["Wren"].spell_slots = {2: 1}
        events = covered.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", slot_level=2,
                   target="Goblin"),
            FixedRandom(14),
        )
        effect = next(e for e in events if e.kind == "spell_effect")
        assert effect.data["cover"] == 1
        assert effect.data["saved"] is False
        # The total is in the log, so a future reader can see the margin is one
        # point rather than having to re-derive the modifier.
        assert "-1 = 13 vs DC 15" in effect.detail

    def test_three_quarters_cover_raises_a_named_target_by_five(self) -> None:
        """The third degree of the contract; ``duel``'s straight line cannot reach it.

        The corner rule caps the grade at what the blocker carries *and* at how
        many of the four corner lines it blocks, so a lone square on a straight
        line between two combatants is half cover however strong its terrain. The
        offset here — caster at (0,0), goblin at (4,2) — is the geometry
        ``test_grid.py::TestCover`` uses for the same grade: three of four lines
        blocked. Natural 9 + 6 = 15 hits AC 15 in the open and misses AC 20 here.
        """
        rng = Random(3)
        wren = caster(position=(0, 0))
        wren.spells = ("Guiding Bolt",)
        wren.spell_slots = {1: 1}
        encounter = Encounter(
            [wren, make_monster("Goblin Warrior", label="Goblin", position=(20, 10))],
            rng,
            spellbook=spellbook(),
            map_document=strip(5, 3, terrain={(2, 1): "three-quarters-cover"}),
        )
        advance_to(encounter, "Wren", rng)
        assert encounter.cover_between("Wren", "Goblin") is CoverGrade.THREE_QUARTERS
        shielded = next(
            e for e in encounter.act(
                Action(kind=ActionKind.CAST, spell="Guiding Bolt", slot_level=1,
                       target="Goblin"),
                FixedRandom(9),
            )
            if e.kind == "spell_effect"
        )
        assert shielded.data["cover"] == 2
        assert not shielded.data["affected"]
        assert "vs AC 20 -> miss" in shielded.detail

    def test_a_storey_seals_a_spell_as_it_seals_an_arrow(self) -> None:
        """The field symptom, named: a floor stopped weapons and not spells.

        ``_cover_from_square`` has always returned TOTAL across levels, but only
        the weapon path consulted it — so a cleric could shoot anything on any
        storey from anywhere while the archer beside her could not, and a map
        author reading "levels give total cover" was told something true of half
        the actions.
        """
        rng = Random(3)
        wren = caster(position=(0, 0))
        wren.spells = ("Guiding Bolt",)
        wren.spell_slots = {1: 1}
        upstairs = make_monster("Goblin Warrior", label="Upstairs", position=(15, 0))
        upstairs.level = 1
        encounter = Encounter(
            [wren, upstairs], rng, spellbook=spellbook(), map_document=tower()
        )
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="Upstairs.*total cover"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Guiding Bolt", slot_level=1,
                       target="Upstairs"),
                FixedRandom(20),
            )
        assert upstairs.hp == upstairs.max_hp
        assert wren.spell_slots[1] == 1

    def test_the_refusal_names_which_of_several_targets_is_sealed(self) -> None:
        """A multi-target cast says *who* it cannot reach, not just that it failed.

        The whole cast is refused rather than quietly shrinking to the reachable
        names: a caller who aimed at three creatures and silently hit two has been
        given a wrong answer, not a partial one.
        """
        twin = Spell(
            name="Twin Bolt",
            level=1,
            requires_attack_roll=True,
            damage=Dice(1, 6, 0),
            damage_type=DamageType.RADIANT,
            range_feet=120,
            max_targets=2,
            provenance=FIXTURE,
        )
        rng = Random(3)
        wren = caster(position=(0, 5))
        wren.spells = ("Twin Bolt",)
        wren.spell_slots = {1: 1}
        encounter = Encounter(
            [
                wren,
                make_monster("Goblin Warrior", label="Open", position=(5, 5)),
                make_monster("Goblin Warrior", label="Sealed", position=(20, 5)),
            ],
            rng,
            spellbook={"Twin Bolt": twin},
            map_document=strip(5, 3, terrain=self.WALL_COLUMN),
        )
        advance_to(encounter, "Wren", rng)
        open_goblin = encounter.creatures["Open"]
        with pytest.raises(EncounterError, match="Sealed.*total cover"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Twin Bolt", slot_level=1,
                       targets=("Open", "Sealed")),
                FixedRandom(20),
            )
        assert wren.spell_slots[1] == 1
        # The reachable target is the half the docstring is about: a cast that
        # shrank to it would still raise and still leave the slot alone.
        assert open_goblin.hp == open_goblin.max_hp


class TestInteract:
    def corridor(self) -> tuple[Encounter, Random]:
        """A doorway in an otherwise solid wall: walls above and below, door in
        the middle row, archer on one side and goblin on the other."""
        rng = Random(3)
        door = fixture(name="door", square=(1, 1))
        encounter = Encounter(
            [archer(position=(0, 5)),
             make_monster("Goblin Warrior", label="Goblin", position=(15, 5))],
            rng,
            map_document=strip(
                4, 3, terrain={(1, 0): "wall", (1, 2): "wall"}, features=(door,)
            ),
        )
        advance_to(encounter, "Sylvi", rng)
        return encounter, rng

    def double_corridor(
        self, *, actor_position: Point = (0, 5), checked: bool = False
    ) -> tuple[Encounter, Random]:
        """Two adjacent door leaves with one interaction contract and state."""
        check = FeatureCheck(ability=Ability.DEXTERITY, dc=10) if checked else None
        left = fixture(
            name="door-left",
            square=(1, 1),
            orientation="horizontal",
            linked_to="door-right",
            costs_action=checked,
            check=check,
        )
        right = fixture(
            name="door-right",
            square=(2, 1),
            orientation="horizontal",
            linked_to="door-left",
            costs_action=checked,
            check=check,
        )
        opponent = (20, 5) if actor_position == (0, 5) else (0, 5)
        rng = Random(3)
        encounter = Encounter(
            [
                archer(position=actor_position),
                make_monster("Goblin Warrior", label="Goblin", position=opponent),
            ],
            rng,
            map_document=strip(
                5,
                3,
                terrain={(1, 0): "wall", (1, 2): "wall", (2, 0): "wall", (2, 2): "wall"},
                features=(left, right),
            ),
        )
        advance_to(encounter, "Sylvi", rng)
        return encounter, rng

    def test_a_closed_door_blocks_sight_and_passage_until_opened(self) -> None:
        encounter, rng = self.corridor()
        assert encounter.cover_between("Sylvi", "Goblin") is CoverGrade.TOTAL
        with pytest.raises(EncounterError, match="no route"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 5)), rng)

        events = encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert events[0].kind == "interact"
        assert events[0].data == {"feature": "door", "open": True}
        assert encounter.state()["map"]["features"]["door"]["open"] is True
        assert encounter.cover_between("Sylvi", "Goblin") is CoverGrade.NONE
        encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 5)), rng)
        assert encounter.creatures["Sylvi"].position == (10, 5)

    @pytest.mark.parametrize(
        ("feature", "other", "position"),
        [
            ("door-left", "door-right", (0, 5)),
            ("door-right", "door-left", (15, 5)),
        ],
    )
    def test_either_leaf_operates_both_linked_doors(
        self, feature: str, other: str, position: Point
    ) -> None:
        encounter, rng = self.double_corridor(actor_position=position)
        events = encounter.act(Action(kind=ActionKind.INTERACT, feature=feature), rng)

        assert events[0].data == {"feature": feature, "open": True, "linked": [other]}
        features = encounter.state()["map"]["features"]
        assert features[feature]["open"] is True
        assert features[other]["open"] is True
        assert features[feature]["linked_to"] == other
        assert features[other]["linked_to"] == feature
        assert encounter.state()["turn_state"]["interaction_used"] is True

    def test_a_linked_pair_shares_one_action_and_one_check(self) -> None:
        encounter, _ = self.double_corridor(checked=True)
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="door-left"), FixedRandom(10)
        )

        assert events[0].data == {
            "feature": "door-left",
            "open": True,
            "linked": ["door-right"],
            "success": True,
            "check": "d20 [10] +0 = 10 vs DC 10",
        }
        assert encounter.state()["turn_state"]["action_used"] is True
        assert encounter.map_state is not None
        assert encounter.map_state.open_features == {"door-left", "door-right"}

    def test_a_malformed_runtime_link_is_refused_before_combat(self) -> None:
        left = fixture(
            name="door-left",
            square=(1, 0),
            orientation="horizontal",
            linked_to="door-right",
        )
        right = fixture(name="door-right", square=(2, 0), orientation="horizontal")
        with pytest.raises(EncounterError, match="must link back to 'door-left'"):
            Encounter(
                [archer(), make_monster("Goblin Warrior", label="Goblin", position=20)],
                Random(3),
                map_document=strip(5, features=(left, right)),
            )

    def test_a_runtime_link_must_follow_the_shared_door_orientation(self) -> None:
        upper = fixture(
            name="door-upper",
            square=(1, 0),
            orientation="horizontal",
            linked_to="door-lower",
        )
        lower = fixture(
            name="door-lower",
            square=(1, 1),
            orientation="horizontal",
            linked_to="door-upper",
        )
        with pytest.raises(
            EncounterError,
            match="linked doors must be adjacent along their shared orientation",
        ):
            Encounter(
                [archer(), make_monster("Goblin Warrior", label="Goblin", position=20)],
                Random(3),
                map_document=strip(5, 3, features=(upper, lower)),
            )

    def test_interacting_is_free_but_only_once_per_turn(self) -> None:
        encounter, rng = self.corridor()
        encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert not encounter.state()["turn_state"]["action_used"]
        with pytest.raises(EncounterError, match="already interacted"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)

    def test_the_same_creature_can_close_it_again_next_turn(self) -> None:
        encounter, rng = self.corridor()
        encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        for _ in range(4):
            encounter.advance(rng)
            if encounter.current_name == "Sylvi":
                break
        events = encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert events[0].data == {"feature": "door", "open": False}

    def test_a_feature_out_of_reach_is_refused(self) -> None:
        rng = Random(3)
        door = fixture(name="far door", square=(3, 0))
        encounter = Encounter(
            [archer(), make_monster("Goblin Warrior", label="Goblin",
                                    position=(20, 0))],
            rng,
            map_document=strip(5, features=(door,)),
        )
        advance_to(encounter, "Sylvi", rng)
        with pytest.raises(EncounterError, match="out of reach"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="far door"), rng)

    def test_an_unknown_feature_lists_what_the_map_has(self) -> None:
        encounter, rng = self.corridor()
        with pytest.raises(EncounterError, match="the map has: door"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="portcullis"), rng)

    def test_the_refusal_lists_fixtures_only_and_never_a_spawn_hint(self) -> None:
        """A mistyped feature name may not be a way to read the map's spawns.

        This refusal is player-visible: it goes back to whoever typed the wrong
        name, and a map's spawn hints spell out which side arrives where. So the
        list is ``self._fixtures`` — the features a fight can actually work —
        and not the document's features, which is the only difference between
        this message and a free reading of the ambush.

        The hint below carries a ``team`` and no ``state``, which is what makes
        it a hint rather than a fixture, and an id that gives the whole thing
        away if it is ever printed.
        """
        rng = Random(3)
        door = fixture(name="door", square=(1, 0))
        ambush = MapFeatureRecord(
            id="spawn-monsters-behind-the-altar",
            kind="spawn",
            at=(3, 0),
            team="monsters",
        )
        encounter = Encounter(
            [archer(), make_monster("Goblin Warrior", label="Goblin",
                                    position=(20, 0))],
            rng,
            map_document=strip(5, features=(door, ambush)),
        )
        advance_to(encounter, "Sylvi", rng)

        with pytest.raises(EncounterError, match="the map has: door$") as refusal:
            encounter.act(Action(kind=ActionKind.INTERACT, feature="portcullis"), rng)
        assert "spawn" not in str(refusal.value)
        assert "altar" not in str(refusal.value)
        # The vacuity guard: a hint the document never carried could not leak.
        assert encounter.map_document is not None
        assert "spawn-monsters-behind-the-altar" in {
            one.id for one in encounter.map_document.levels[0].features
        }

    def test_interacting_without_a_map_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no battle map"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)


class TestReachAcrossStoreys:
    """A fixture is reached on its own storey, not merely at its own square.

    ``Encounter._fixtures`` merges every storey into one name table, so a reach
    test that compares squares alone lets a creature on the ground work a hatch
    directly above its head.
    """

    def two_storeys(self) -> tuple[Encounter, Random]:
        rng = Random(3)
        hatch = fixture(name="hatch", square=(0, 0))
        map_document = storeys(
            floor(0, features=(stair("stair-up", (1, 0), 1),)),
            floor(1, feet=10, features=(hatch, stair("stair-down", (1, 0), 0))),
        )
        encounter = Encounter(
            [fighter(position=(0, 0)),
             make_monster("Goblin Warrior", label="Goblin", position=(15, 0))],
            rng,
            map_document=map_document,
        )
        advance_to(encounter, "Thora", rng)
        return encounter, rng

    def test_a_fixture_one_storey_up_is_out_of_reach(self) -> None:
        encounter, rng = self.two_storeys()
        assert encounter.creatures["Thora"].level == 0
        assert encounter.map_document is not None
        assert encounter.map_document.level_of("hatch") == 1
        with pytest.raises(EncounterError, match="cannot reach it from another storey"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="hatch"), rng)
        assert encounter.state()["map"]["features"]["hatch"]["open"] is False

    def test_climbing_to_its_storey_brings_it_into_reach(self) -> None:
        encounter, rng = self.two_storeys()
        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng
        )
        encounter.act(Action(kind=ActionKind.INTERACT, feature="hatch"), rng)
        assert encounter.state()["map"]["features"]["hatch"]["open"] is True


class TestActionRecordsReplayEverything:
    """``ActionRecord.as_dict`` must carry every field ``act`` was given.

    The record is the unit of replay, and its field list is written out by
    hand — so a field added to :class:`Action` and forgotten here is silently
    dropped from a log that promises to reproduce the fight exactly.
    """

    def test_a_cross_storey_move_records_the_level_it_ended_on(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, map_document=tower()
        )
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(5, 0), to_level=1), rng
        )
        assert encounter.creatures["Thora"].level == 1
        action = encounter.actions[-1].as_dict()["action"]
        assert action["to_level"] == 1

    def test_a_move_that_stays_put_records_no_level(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng, map_document=tower()
        )
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.MOVE, to_position=(5, 0)), rng)
        assert "to_level" not in encounter.actions[-1].as_dict()["action"]


#: A metal spike takes a raw Strength check — creatures have no skill training,
#: so the DC is set as if untrained. ``fighter`` has Strength 16, a +3 modifier:
#: ``FixedRandom(15)`` clears this and ``FixedRandom(5)`` does not.
SPIKE_CHECK = FeatureCheck(ability=Ability.STRENGTH, dc=15)


def spike(name: str, square: Square) -> MapFeatureRecord:
    """One of the two spikes pinning the sluice gate.

    Its own square reads the same in both states, which is the case a fixture
    that changes nothing where it stands has to get right: pulling it moves
    terrain nowhere, only the gate's prerequisites.
    """
    return fixture(
        name=name,
        square=square,
        kind="spike",
        terrain=TerrainPair(closed="floor", open="floor"),
        costs_action=True,
        check=SPIKE_CHECK,
    )


def sluice(
    *,
    requires: tuple[str, ...] = ("north spike", "south spike"),
    gate_check: FeatureCheck | None = None,
) -> MapDocument:
    """The driving fixture: a gate that floods a room and starts a wheel turning.

    Eight by three of floor. The two spikes flank the gate at ``(2, 1)``; east
    of it ``(4, 1)`` and ``(5, 1)`` become water five feet lower, and ``(6, 1)``
    is the mill wheel, difficult ground that turns impassable. One flip, three
    kinds of change.
    """
    gate = fixture(
        name="sluice gate",
        square=(2, 1),
        requires=requires,
        costs_action=True,
        check=gate_check,
        affects=(
            MapOverlayRecord(
                cells=((4, 1), (5, 1)),
                terrain=TerrainPair(closed="floor", open="water"),
                elevation=HeightPair(closed=0, open=-5),
            ),
            MapOverlayRecord(
                cells=((6, 1),),
                terrain=TerrainPair(closed="difficult", open="mountain"),
            ),
        ),
    )
    return MapDocument.flat(
        name="sluice",
        width=8,
        height=3,
        default_terrain="floor",
        features=(
            spike("north spike", (2, 0)),
            spike("south spike", (2, 2)),
            gate,
        ),
        provenance=fixture_provenance(FIXTURE),
    )


def fixture_trigger(
    when: dict[str, bool], *, set_open: bool = True, mode: TriggerMode = TriggerMode.EDGE
) -> FeatureTrigger:
    return FeatureTrigger(
        when=tuple(sorted(when.items())), set_open=set_open, mode=mode
    )


def trigger_fixture(
    name: str,
    square: Square,
    *,
    state: str = "closed",
    trigger: FeatureTrigger | None = None,
    requires: tuple[str, ...] = (),
    costs_action: bool = False,
    check: FeatureCheck | None = None,
    linked_to: str | None = None,
    orientation: str | None = None,
    elevation: HeightPair | None = None,
    affects: tuple[MapOverlayRecord, ...] = (),
) -> MapFeatureRecord:
    return fixture(
        name=name,
        square=square,
        kind="door" if linked_to is not None else "fixture",
        orientation=orientation,
        terrain=TerrainPair(closed="floor", open="floor"),
        state=state,
        elevation=elevation,
        affects=affects,
        requires=requires,
        trigger=trigger,
        costs_action=costs_action,
        check=check,
        linked_to=linked_to,
    )


def trigger_fight(*features: MapFeatureRecord) -> tuple[Encounter, Random]:
    rng = Random(37)
    encounter = Encounter(
        [
            fighter(position=square_center((0, 1))),
            fighter("Brute", team="foes", position=square_center((7, 1))),
        ],
        rng,
        map_document=strip(8, 3, features=features),
    )
    advance_to(encounter, "Thora", rng)
    return encounter, rng


def next_turn_for(encounter: Encounter, actor: str, rng: Random) -> None:
    encounter.advance(rng)
    advance_to(encounter, actor, rng)


class TestMapFixtures:
    """Operable fixtures that move the ground under a running fight."""

    def fight(self, map_document: MapDocument | None = None) -> tuple[Encounter, Random]:
        """Two at the spikes, two in the room the sluice floods."""
        rng = Random(3)
        encounter = Encounter(
            [
                fighter("Thora", position=square_center((1, 0))),
                fighter("Brute", position=square_center((1, 2))),
                fighter("Wader", team="foes", position=square_center((4, 1))),
                fighter("Miller", team="foes", position=square_center((6, 1))),
            ],
            rng,
            map_document=sluice() if map_document is None else map_document,
        )
        return encounter, rng

    def pull_both_spikes(self, encounter: Encounter, rng: Random) -> None:
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(15)
        )
        advance_to(encounter, "Brute", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="south spike"), FixedRandom(15)
        )

    def open_the_sluice(self, encounter: Encounter, rng: Random) -> None:
        self.pull_both_spikes(encounter, rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.INTERACT, feature="sluice gate"), rng)

    def route_cost(self, encounter: Encounter, name: str, goal: Square) -> int | None:
        path = encounter.route(name, goal)
        return None if path is None else path.cost_feet

    def elevation_of(self, encounter: Encounter, name: str) -> int:
        state = next(
            c for c in encounter.state()["combatants"] if c["name"] == name
        )
        return int(state["elevation"])

    # --- the driving scenario ---------------------------------------------
    def test_the_gate_floods_the_room_once_both_spikes_are_out(self) -> None:
        encounter, rng = self.fight()
        assert self.route_cost(encounter, "Wader", (5, 1)) == 5
        assert self.elevation_of(encounter, "Wader") == 0
        # Floor then difficult ground: the wheel is walkable while it is still.
        assert self.route_cost(encounter, "Wader", (6, 1)) == 5 + 10

        self.open_the_sluice(encounter, rng)

        # Terrain: the room walks like water, at twice the price.
        assert self.route_cost(encounter, "Wader", (5, 1)) == 10
        # Height: the water sits five feet below the floor it replaced.
        assert self.elevation_of(encounter, "Wader") == -5
        # And the wheel, on the same flip, is no longer ground at all.
        assert self.route_cost(encounter, "Wader", (6, 1)) is None
        # The wheel overlay carries no height pair, so its square keeps the
        # plane's own: an absent pair falls through rather than reading zero.
        assert self.elevation_of(encounter, "Miller") == 0

    def test_the_gate_refuses_until_both_spikes_are_out(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        with pytest.raises(
            EncounterError, match="until north spike, south spike are open"
        ):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="sluice gate"), rng)
        # Refused before the spend: the party learns why without paying.
        assert not encounter.state()["turn_state"]["action_used"]

        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(15)
        )
        advance_to(encounter, "Brute", rng)
        with pytest.raises(EncounterError, match="until south spike is open"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="sluice gate"), rng)

    def test_a_creature_standing_where_the_ground_turns_impassable_stays(self) -> None:
        """Entry cost governs entering, not remaining. No forced move exists."""
        encounter, rng = self.fight()
        self.open_the_sluice(encounter, rng)
        miller = encounter.creatures["Miller"]
        # Nothing shoved it and nothing refused the flip on its account.
        assert miller.position == square_center((6, 1))
        assert self.route_cost(encounter, "Miller", (6, 1)) == 0

        advance_to(encounter, "Miller", rng)
        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=square_center((7, 1))), rng
        )
        assert miller.position == square_center((7, 1))
        # And having stepped off it, it may not step back on.
        assert self.route_cost(encounter, "Miller", (6, 1)) is None

    def test_two_fights_over_one_map_do_not_share_its_state(self) -> None:
        """A ``MapDocument`` is frozen but its levels hold plain tuples and dicts.

        ``simulate_rounds`` hands one map to every iteration, so a fixture
        that wrote through to the map would leak the first fight's flood into
        the second.
        """
        map_document = sluice()
        first, first_rng = self.fight(map_document)
        second, _ = self.fight(map_document)
        self.open_the_sluice(first, first_rng)

        assert first.state()["map"]["features"]["sluice gate"]["open"] is True
        assert second.state()["map"]["features"]["sluice gate"]["open"] is False
        assert self.route_cost(second, "Wader", (5, 1)) == 5
        assert self.elevation_of(second, "Wader") == 0

    # --- what a claim does and does not decide ----------------------------
    def test_an_overlay_without_terrain_leaves_the_ground_it_finds(self) -> None:
        """An absent terrain pair falls through to the plane's own sparse layer.

        The riser only lifts ``(3, 0)``. That square is authored difficult, and
        it must still walk like difficult ground in both states — closed it
        costs the doubled 10 ft, open it costs the doubled 10 ft plus a 10-foot
        climb charged at the *difficult* rate of 3 ft per foot.
        """
        riser = fixture(
            name="riser",
            square=(1, 0),
            terrain=TerrainPair(closed="floor", open="floor"),
            affects=(
                MapOverlayRecord(
                    cells=((3, 0),), elevation=HeightPair(closed=0, open=10)
                ),
            ),
        )
        rng = Random(3)
        encounter = Encounter(
            [fighter(), fighter("Brute", team="foes", position=square_center((5, 0)))],
            rng,
            map_document=strip(6, terrain={(3, 0): "difficult"}, features=(riser,)),
        )
        assert self.route_cost(encounter, "Thora", (3, 0)) == 5 + 5 + 10

        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.INTERACT, feature="riser"), rng)
        assert self.route_cost(encounter, "Thora", (3, 0)) == 5 + 5 + (10 + 30)

    def test_a_square_no_fixture_claims_keeps_the_plane_underneath(self) -> None:
        """Row 0 lies outside every overlay, and the spike changes nothing.

        Six squares of plain floor at 5 ft each, before the flood and after it.
        The walk crosses the north spike's own square, which is the case a
        fixture that changes no ground has to get right.
        """
        encounter, rng = self.fight()
        assert self.route_cost(encounter, "Thora", (7, 0)) == 6 * 5
        self.open_the_sluice(encounter, rng)
        assert self.route_cost(encounter, "Thora", (7, 0)) == 6 * 5

    # --- what the map may not say -----------------------------------------
    def two_fighters(self) -> list[Creature]:
        return [fighter(), fighter("Brute", team="foes", position=square_center((5, 0)))]

    def test_an_overlay_naming_unknown_terrain_is_refused(self) -> None:
        gate = fixture(
            name="gate",
            square=(1, 0),
            affects=(
                MapOverlayRecord(
                    cells=((3, 0),),
                    terrain=TerrainPair(closed="floor", open="vale-lava"),
                ),
            ),
        )
        with pytest.raises(EncounterError, match="does not define: vale-lava"):
            Encounter(
                self.two_fighters(), Random(1), map_document=strip(6, features=(gate,))
            )

    def test_an_overlay_cell_off_the_map_is_refused(self) -> None:
        gate = fixture(
            name="gate",
            square=(1, 0),
            affects=(
                MapOverlayRecord(
                    cells=((9, 0),),
                    terrain=TerrainPair(closed="floor", open="water"),
                ),
            ),
        )
        with pytest.raises(
            EncounterError,
            match=r"feature 'gate' reaches \(9, 0\), outside the 6x1 grid",
        ):
            Encounter(
                self.two_fighters(), Random(1), map_document=strip(6, features=(gate,))
            )

    def test_two_plain_features_on_one_square_are_still_refused(self) -> None:
        with pytest.raises(
            EncounterError,
            match=(
                r"feature 'south door' claims square \(2, 0\), which feature "
                r"'north door' already governs"
            ),
        ):
            Encounter(
                self.two_fighters(),
                Random(1),
                map_document=strip(
                    6,
                    features=(
                        fixture(name="north door", square=(2, 0)),
                        fixture(name="south door", square=(2, 0)),
                    ),
                ),
            )

    def test_an_overlay_reaching_another_fixtures_square_is_refused(self) -> None:
        gate = fixture(
            name="gate",
            square=(1, 0),
            affects=(
                MapOverlayRecord(
                    cells=((3, 0),),
                    terrain=TerrainPair(closed="floor", open="water"),
                ),
            ),
        )
        lever = fixture(name="lever", square=(3, 0))
        with pytest.raises(
            EncounterError,
            match=(
                r"feature 'lever' claims square \(3, 0\), which feature 'gate' "
                r"already governs"
            ),
        ):
            Encounter(
                self.two_fighters(),
                Random(1),
                map_document=strip(6, features=(gate, lever)),
            )

    def test_a_fixture_claiming_its_own_square_twice_is_refused(self) -> None:
        gate = fixture(
            name="gate",
            square=(1, 0),
            affects=(
                MapOverlayRecord(
                    cells=((1, 0),),
                    terrain=TerrainPair(closed="floor", open="water"),
                ),
            ),
        )
        with pytest.raises(
            EncounterError, match=r"feature 'gate' claims square \(1, 0\) twice"
        ):
            Encounter(
                self.two_fighters(), Random(1), map_document=strip(6, features=(gate,))
            )

    def test_one_square_may_be_claimed_once_on_each_storey(self) -> None:
        """The rule is one claim per square *per level*, not per footprint."""
        map_document = storeys(
            floor(
                0,
                features=(
                    fixture("ground door", (0, 0)), stair("stair-up", (1, 0), 1),
                ),
            ),
            floor(
                1,
                features=(
                    fixture("upper door", (0, 0)), stair("stair-down", (1, 0), 0),
                ),
            ),
        )
        encounter = Encounter(
            [fighter(position=square_center((2, 0))),
             fighter("Brute", team="foes", position=square_center((3, 0)))],
            Random(1),
            map_document=map_document,
        )
        assert sorted(encounter.state()["map"]["features"]) == [
            "ground door", "upper door"
        ]

    def test_a_prerequisite_the_map_does_not_have_is_refused(self) -> None:
        gate = fixture(name="gate", square=(1, 0), requires=("ghost lever",))
        lever = fixture(name="lever", square=(3, 0))
        with pytest.raises(
            EncounterError,
            match=(
                r"feature 'gate' requires 'ghost lever', but there is no feature "
                r"'ghost lever' in this map\. Declared: gate, lever"
            ),
        ):
            Encounter(
                self.two_fighters(),
                Random(1),
                map_document=strip(6, features=(gate, lever)),
            )

    def test_a_prerequisite_on_another_storey_resolves(self) -> None:
        """``requires`` is a prerequisite, not a reach: it may cross a floor."""
        map_document = storeys(
            floor(
                0,
                features=(
                    fixture("gate", (0, 0), requires=("upper lever",)),
                    stair("stair-up", (1, 0), 1),
                ),
            ),
            floor(
                1,
                features=(
                    fixture("upper lever", (3, 0)), stair("stair-down", (1, 0), 0),
                ),
            ),
        )
        encounter = Encounter(
            [fighter(position=square_center((2, 0))),
             fighter("Brute", team="foes", position=square_center((3, 0)))],
            Random(1),
            map_document=map_document,
        )
        assert encounter.state()["map"]["features"]["gate"]["blocked_by"] == [
            "upper lever"
        ]

    # --- what operating one costs -----------------------------------------
    def test_a_fixture_that_costs_an_action_spends_the_action(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(15)
        )
        turn = encounter.state()["turn_state"]
        assert turn["action_used"] is True
        # It spends the action *instead of* the free interaction, not as well.
        assert turn["interaction_used"] is False

    def test_a_fixture_that_costs_an_action_is_refused_without_one(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DODGE), rng)
        with pytest.raises(EncounterError, match="already taken an action this turn"):
            encounter.act(
                Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(15)
            )

    def test_a_failed_check_spends_the_action_and_moves_nothing(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(5)
        )
        assert events[0].kind == "interact"
        assert events[0].data == {
            "feature": "north spike",
            "open": False,
            "success": False,
            "check": "d20 [5] +3 = 8 vs DC 15",
        }
        assert encounter.state()["map"]["features"]["north spike"]["open"] is False
        assert encounter.state()["turn_state"]["action_used"] is True

    def test_a_passed_check_reports_the_roll_beside_the_result(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"), FixedRandom(15)
        )
        assert events[0].data == {
            "feature": "north spike",
            "open": True,
            "success": True,
            "check": "d20 [15] +3 = 18 vs DC 15",
        }
        assert "d20 [15]" in events[0].detail

    def test_poisoned_imposes_disadvantage_on_a_fixture_check(self) -> None:
        encounter, rng = self.fight()
        advance_to(encounter, "Thora", rng)
        encounter.current.add_condition(Condition.POISONED)
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"),
            ScriptedRandom([18, 2]),
        )
        assert events[0].data["check"] == (
            "d20 [18/2] disadvantage -> 2 +3 = 5 vs DC 15"
        )

    def test_a_fixture_with_no_check_reports_no_roll(self) -> None:
        """The common case's event dict stays exactly what it was."""
        encounter, rng = self.fight()
        self.pull_both_spikes(encounter, rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="sluice gate"), rng
        )
        assert events[0].data == {"feature": "sluice gate", "open": True}

    def skill_checked_fight(self) -> tuple[Encounter, Random]:
        """One fixture whose check names a skill, on an otherwise plain map."""
        feature = fixture(
            name="ledge",
            square=(1, 1),
            kind="fixture",
            terrain=TerrainPair(closed="floor", open="floor"),
            costs_action=True,
            check=FeatureCheck(ability=Ability.STRENGTH, dc=15, skill="athletics"),
        )
        document = MapDocument.flat(
            name="ledge-test", width=3, height=3, default_terrain="floor",
            features=(feature,), provenance=fixture_provenance(FIXTURE),
        )
        rng = Random(3)
        encounter = Encounter(
            [fighter("Thora", position=square_center((1, 1))),
             fighter("Foe", team="foes", position=square_center((2, 1)))],
            rng, map_document=document,
        )
        return encounter, rng

    def test_a_check_naming_a_skill_rolls_on_the_printed_skill_bonus(self) -> None:
        # fighter's Strength modifier is +3 — the skill bonus below (+10)
        # would clear DC 15 that the modifier alone would not.
        encounter, rng = self.skill_checked_fight()
        advance_to(encounter, "Thora", rng)
        encounter.current.skill_bonuses = {"athletics": 10}
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="ledge"), FixedRandom(3)
        )
        assert events[0].data["check"] == "d20 [3] +10 = 13 vs DC 15"

    def test_a_check_naming_a_skill_the_creature_has_no_bonus_for_rolls_the_ability_modifier(
        self,
    ) -> None:
        # Regression pin: no skill bonus for the declared skill still falls back
        # to the raw ability modifier, unchanged.
        encounter, rng = self.skill_checked_fight()
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="ledge"), FixedRandom(15)
        )
        assert events[0].data["check"] == "d20 [15] +3 = 18 vs DC 15"

    # --- saying which way to move it --------------------------------------
    def test_set_open_makes_it_so_rather_than_toggling(self) -> None:
        encounter, rng = self.fight()
        self.pull_both_spikes(encounter, rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="sluice gate", set_open=True), rng
        )
        assert encounter.state()["map"]["features"]["sluice gate"]["open"] is True

    def test_set_open_matching_the_current_state_is_refused(self) -> None:
        encounter, rng = self.fight()
        self.open_the_sluice(encounter, rng)
        advance_to(encounter, "Brute", rng)
        with pytest.raises(EncounterError, match="sluice gate is already open"):
            encounter.act(
                Action(kind=ActionKind.INTERACT, feature="sluice gate", set_open=True),
                rng,
            )
        turn = encounter.state()["turn_state"]
        assert turn["action_used"] is False
        assert turn["interaction_used"] is False

    def test_closing_something_already_closed_is_refused(self) -> None:
        encounter, rng = self.corridor_fight()
        with pytest.raises(EncounterError, match="door is already closed"):
            encounter.act(
                Action(kind=ActionKind.INTERACT, feature="door", set_open=False), rng
            )
        assert encounter.state()["turn_state"]["interaction_used"] is False

    def corridor_fight(self) -> tuple[Encounter, Random]:
        rng = Random(3)
        door = fixture(name="door", square=(1, 0))
        encounter = Encounter(
            [fighter(), fighter("Brute", team="foes", position=square_center((5, 0)))],
            rng,
            map_document=strip(6, features=(door,)),
        )
        advance_to(encounter, "Thora", rng)
        return encounter, rng

    def test_the_record_carries_which_way_it_was_asked_to_move(self) -> None:
        encounter, rng = self.fight()
        self.pull_both_spikes(encounter, rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="sluice gate", set_open=True), rng
        )
        action = encounter.actions[-1].as_dict()["action"]
        assert action["set_open"] is True
        assert action["feature"] == "sluice gate"

    def test_a_toggle_records_no_direction(self) -> None:
        encounter, rng = self.corridor_fight()
        encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert "set_open" not in encounter.actions[-1].as_dict()["action"]

    # --- closing is never gated -------------------------------------------
    def test_driving_a_spike_back_in_does_not_shut_the_gate(self) -> None:
        encounter, rng = self.fight()
        self.open_the_sluice(encounter, rng)
        advance_to(encounter, "Brute", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="south spike", set_open=False),
            FixedRandom(15),
        )
        features = encounter.state()["map"]["features"]
        assert features["south spike"]["open"] is False
        assert features["sluice gate"]["open"] is True
        assert self.route_cost(encounter, "Wader", (5, 1)) == 10

    def test_the_gate_may_be_closed_with_its_prerequisites_unmet(self) -> None:
        encounter, rng = self.fight()
        self.open_the_sluice(encounter, rng)
        advance_to(encounter, "Brute", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="south spike", set_open=False),
            FixedRandom(15),
        )
        advance_to(encounter, "Wader", rng)
        # Wader is not next to the gate; Miller is not either. Come round to
        # Brute, whose spike is back in and whose action is fresh.
        advance_to(encounter, "Brute", rng)
        encounter.act(
            Action(kind=ActionKind.INTERACT, feature="sluice gate", set_open=False), rng
        )
        assert encounter.state()["map"]["features"]["sluice gate"]["open"] is False
        assert self.route_cost(encounter, "Wader", (5, 1)) == 5

    # --- what the state block says ----------------------------------------
    def test_the_state_block_describes_a_fixture_beyond_a_plain_door(self) -> None:
        encounter, rng = self.fight()
        features = encounter.state()["map"]["features"]
        assert features["north spike"] == {
            "square": [2, 0],
            "kind": "spike",
            "level": 0,
            "open": False,
            "costs_action": True,
            "check": {"ability": "strength", "dc": 15},
        }
        assert features["sluice gate"] == {
            "square": [2, 1],
            "kind": "door",
            "level": 0,
            "open": False,
            "affects": [[4, 1], [5, 1], [6, 1]],
            "requires": ["north spike", "south spike"],
            "blocked_by": ["north spike", "south spike"],
            "costs_action": True,
        }

    def test_what_is_blocking_it_narrows_as_the_spikes_come_out(self) -> None:
        encounter, rng = self.fight()
        self.pull_both_spikes(encounter, rng)
        gate = encounter.state()["map"]["features"]["sluice gate"]
        assert gate["requires"] == ["north spike", "south spike"]
        assert "blocked_by" not in gate

    # --- what the state block says about the ground ------------------------
    def test_the_map_elevation_summary_falls_with_the_flood(self) -> None:
        """One payload cannot be half live: ``features[…].open`` already is.

        The creature standing in the flooded room reports −5 through
        ``_creature_state``; a block reading the authored plane alone would
        say in the same breath that the map's lowest ground is 0.
        """
        encounter, rng = self.fight()
        assert encounter.state()["map"]["elevation"]["flat"] is True

        self.open_the_sluice(encounter, rng)

        elevation = encounter.state()["map"]["elevation"]
        assert (elevation["min"], elevation["max"]) == (-5, 0)
        assert elevation["flat"] is False
        assert self.elevation_of(encounter, "Wader") == elevation["min"]

    def test_an_invisible_creature_gets_no_advantage_pulling_a_spike(self) -> None:
        """SRD 5.2.1 p.184's Invisible Advantage is scoped to Initiative alone.

        It must not leak into an ordinary fixture check. With Advantage wrongly
        applied here, the roll would keep the 20 (23 total, a clear success)
        and consume both scripted faces; fixed, only the 1 is drawn (4 total,
        a clear failure against DC 15).
        """
        encounter, rng = self.fight()
        encounter.creatures["Thora"].add_condition(Condition.INVISIBLE)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.INTERACT, feature="north spike"),
            ScriptedRandom([1, 20]),
        )
        assert events[0].data["success"] is False


class TestFixtureTriggers:
    def test_a_true_edge_predicate_at_creation_does_not_fire(self) -> None:
        lever = trigger_fixture("lever", (1, 1), state="open")
        gate = trigger_fixture(
            "gate",
            (1, 2),
            trigger=fixture_trigger({"lever": True}),
        )

        encounter, _ = trigger_fight(lever, gate)

        assert encounter.map_state is not None
        assert encounter.map_state.open_features == {"lever"}
        assert not any(event.kind == "interact" for event in encounter.log)

    def test_an_edge_fires_rearms_only_while_false_and_fires_again(self) -> None:
        lever = trigger_fixture("lever", (1, 1))
        gate = trigger_fixture(
            "gate", (1, 2), trigger=fixture_trigger({"lever": True})
        )
        encounter, rng = trigger_fight(lever, gate)

        first = encounter.act(Action(ActionKind.INTERACT, feature="lever"), rng)
        assert [event.data["feature"] for event in first] == ["lever", "gate"]
        assert first[1].actor == ""
        assert first[1].data == {
            "automatic": True,
            "triggered_by": "lever",
            "feature": "gate",
            "open": True,
        }

        next_turn_for(encounter, "Thora", rng)
        while_active = encounter.act(
            Action(ActionKind.INTERACT, feature="gate", set_open=False), rng
        )
        assert [event.data["feature"] for event in while_active] == ["gate"]
        assert encounter.map_state is not None
        assert "gate" not in encounter.map_state.open_features

        next_turn_for(encounter, "Thora", rng)
        closing_lever = encounter.act(
            Action(ActionKind.INTERACT, feature="lever", set_open=False), rng
        )
        assert len(closing_lever) == 1

        next_turn_for(encounter, "Thora", rng)
        second = encounter.act(
            Action(ActionKind.INTERACT, feature="lever", set_open=True), rng
        )
        assert [event.data["feature"] for event in second] == ["lever", "gate"]

    def test_a_maintained_trigger_can_close_and_hold_a_fixture(self) -> None:
        lever = trigger_fixture("lever", (1, 1))
        gate = trigger_fixture(
            "gate",
            (1, 2),
            state="open",
            trigger=fixture_trigger(
                {"lever": True}, set_open=False, mode=TriggerMode.MAINTAINED
            ),
        )
        encounter, rng = trigger_fight(lever, gate)

        events = encounter.act(Action(ActionKind.INTERACT, feature="lever"), rng)
        assert [event.data["open"] for event in events] == [True, False]

        next_turn_for(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="held closed by its maintained trigger"):
            encounter.act(
                Action(ActionKind.INTERACT, feature="gate", set_open=True), rng
            )

    def test_maintained_refuses_a_contrary_interaction_before_spending_or_rolling(
        self,
    ) -> None:
        lever = trigger_fixture("lever", (1, 1), state="open")
        gate = trigger_fixture(
            "gate",
            (1, 2),
            state="open",
            trigger=fixture_trigger({"lever": True}, mode=TriggerMode.MAINTAINED),
            costs_action=True,
            check=SPIKE_CHECK,
        )
        encounter, _ = trigger_fight(lever, gate)

        with pytest.raises(EncounterError, match="held open by its maintained trigger"):
            encounter.act(
                Action(ActionKind.INTERACT, feature="gate", set_open=False),
                FixedRandom(1),
            )

        turn = encounter.state()["turn_state"]
        assert turn["action_used"] is False
        assert turn["interaction_used"] is False
        assert not any(event.kind == "interact" for event in encounter.log)

    def test_a_maintained_trigger_does_not_reverse_when_its_predicate_becomes_false(
        self,
    ) -> None:
        lever = trigger_fixture("lever", (1, 1))
        gate = trigger_fixture(
            "gate",
            (1, 2),
            trigger=fixture_trigger({"lever": True}, mode=TriggerMode.MAINTAINED),
        )
        encounter, rng = trigger_fight(lever, gate)
        encounter.act(Action(ActionKind.INTERACT, feature="lever"), rng)

        next_turn_for(encounter, "Thora", rng)
        events = encounter.act(
            Action(ActionKind.INTERACT, feature="lever", set_open=False), rng
        )

        assert len(events) == 1
        assert encounter.map_state is not None
        assert "gate" in encounter.map_state.open_features
        next_turn_for(encounter, "Thora", rng)
        encounter.act(Action(ActionKind.INTERACT, feature="gate", set_open=False), rng)
        assert "gate" not in encounter.map_state.open_features

    def test_a_failed_direct_check_causes_no_trigger_transition(self) -> None:
        lever = trigger_fixture(
            "lever", (1, 1), costs_action=True, check=SPIKE_CHECK
        )
        gate = trigger_fixture(
            "gate",
            (5, 1),
            requires=("lever",),
            trigger=fixture_trigger({"lever": True}, mode=TriggerMode.MAINTAINED),
        )
        encounter, _ = trigger_fight(lever, gate)

        events = encounter.act(
            Action(ActionKind.INTERACT, feature="lever"), FixedRandom(1)
        )

        assert len(events) == 1
        assert events[0].data["success"] is False
        assert encounter.map_state is not None
        assert encounter.map_state.open_features == set()

    def test_an_automatic_transition_bypasses_target_reach_cost_and_check(self) -> None:
        lever = trigger_fixture("lever", (1, 1))
        gate = trigger_fixture(
            "gate",
            (5, 1),
            requires=("lever",),
            trigger=fixture_trigger({"lever": True}, mode=TriggerMode.MAINTAINED),
            costs_action=True,
            check=SPIKE_CHECK,
            affects=(
                MapOverlayRecord(
                    cells=((6, 1),),
                    terrain=TerrainPair(closed="floor", open="water"),
                    elevation=HeightPair(closed=0, open=-5),
                ),
            ),
        )
        encounter, rng = trigger_fight(lever, gate)

        events = encounter.act(Action(ActionKind.INTERACT, feature="lever"), rng)

        assert events[-1].data == {
            "automatic": True,
            "triggered_by": "lever",
            "feature": "gate",
            "open": True,
        }
        turn = encounter.state()["turn_state"]
        assert turn["interaction_used"] is True
        assert turn["action_used"] is False
        route = encounter.route("Brute", (6, 1))
        assert route is not None and route.cost_feet == 10
        assert encounter.state()["map"]["elevation"]["min"] == -5

    def test_cascades_use_dependency_order_with_lexical_tie_breaking(self) -> None:
        lever = trigger_fixture("lever", (1, 1))
        zeta = trigger_fixture(
            "zeta", (4, 1), trigger=fixture_trigger({"lever": True})
        )
        alpha = trigger_fixture(
            "alpha", (3, 1), trigger=fixture_trigger({"lever": True})
        )
        final = trigger_fixture(
            "final",
            (5, 1),
            trigger=fixture_trigger({"alpha": True, "zeta": True}),
        )
        encounter, rng = trigger_fight(zeta, final, lever, alpha)

        events = encounter.act(Action(ActionKind.INTERACT, feature="lever"), rng)

        assert [event.data["feature"] for event in events] == [
            "lever", "alpha", "zeta", "final",
        ]
        assert [event.data["triggered_by"] for event in events[1:]] == [
            "lever", "lever", "zeta",
        ]

    def test_one_automatic_event_operates_both_linked_leaves(self) -> None:
        trigger = fixture_trigger({"lever": True}, mode=TriggerMode.MAINTAINED)
        lever = trigger_fixture("lever", (1, 1))
        left = trigger_fixture(
            "left door", (4, 1), trigger=trigger, linked_to="right door",
            orientation="horizontal",
        )
        right = trigger_fixture(
            "right door", (5, 1), trigger=trigger, linked_to="left door",
            orientation="horizontal",
        )
        encounter, rng = trigger_fight(right, lever, left)

        events = encounter.act(Action(ActionKind.INTERACT, feature="lever"), rng)

        assert len(events) == 2
        assert events[1].data == {
            "automatic": True,
            "triggered_by": "lever",
            "feature": "left door",
            "open": True,
            "linked": ["right door"],
        }
        assert encounter.map_state is not None
        assert encounter.map_state.open_features == {"lever", "left door", "right door"}

    def test_encounter_state_exposes_the_authored_trigger(self) -> None:
        lever = trigger_fixture("lever", (1, 1))
        gate = trigger_fixture(
            "gate",
            (1, 2),
            trigger=fixture_trigger(
                {"lever": True}, set_open=False, mode=TriggerMode.MAINTAINED
            ),
        )
        encounter, _ = trigger_fight(lever, gate)

        assert encounter.state()["map"]["features"]["gate"]["trigger"] == {
            "when": {"lever": "open"},
            "set": "closed",
            "mode": "maintained",
        }


class TestRuntimeTriggerValidation:
    def encounter_with(self, *features: MapFeatureRecord) -> Encounter:
        return Encounter(
            [fighter(), fighter("Brute", team="foes", position=square_center((7, 1)))],
            Random(1),
            map_document=strip(8, 3, features=features),
        )

    def test_a_hand_built_trigger_reference_must_exist(self) -> None:
        gate = trigger_fixture(
            "gate", (1, 1), trigger=fixture_trigger({"ghost": True})
        )
        with pytest.raises(EncounterError, match="trigger references 'ghost'"):
            self.encounter_with(gate)

    def test_a_hand_built_trigger_predicate_must_not_be_empty(self) -> None:
        gate = trigger_fixture("gate", (1, 1), trigger=fixture_trigger({}))
        with pytest.raises(EncounterError, match="must name at least one fixture"):
            self.encounter_with(gate)

    def test_a_hand_built_trigger_graph_must_be_acyclic(self) -> None:
        alpha = trigger_fixture(
            "alpha", (1, 1), trigger=fixture_trigger({"beta": True})
        )
        beta = trigger_fixture(
            "beta", (2, 1), trigger=fixture_trigger({"alpha": True})
        )
        with pytest.raises(EncounterError, match="trigger cycle: alpha -> beta -> alpha"):
            self.encounter_with(alpha, beta)

    def test_a_hand_built_opening_trigger_must_imply_requirements(self) -> None:
        lever = trigger_fixture("lever", (1, 1))
        latch = trigger_fixture("latch", (2, 1))
        gate = trigger_fixture(
            "gate", (3, 1), requires=("latch",),
            trigger=fixture_trigger({"lever": True}),
        )
        with pytest.raises(EncounterError, match="does not require 'latch' to be open"):
            self.encounter_with(lever, latch, gate)

    def test_a_hand_built_maintained_initial_state_must_be_consistent(self) -> None:
        lever = trigger_fixture("lever", (1, 1), state="open")
        gate = trigger_fixture(
            "gate", (2, 1),
            trigger=fixture_trigger({"lever": True}, mode=TriggerMode.MAINTAINED),
        )
        with pytest.raises(EncounterError, match="true initially and sets it open"):
            self.encounter_with(lever, gate)

    def test_hand_built_linked_leaves_must_have_identical_triggers(self) -> None:
        lever = trigger_fixture("lever", (1, 1))
        left = trigger_fixture(
            "left", (3, 1), trigger=fixture_trigger({"lever": True}),
            linked_to="right", orientation="horizontal",
        )
        right = trigger_fixture(
            "right", (4, 1), linked_to="left", orientation="horizontal",
        )
        with pytest.raises(EncounterError, match="identical triggers"):
            self.encounter_with(lever, left, right)


class TestMapFixtureTerrainSummaries:
    def test_a_claim_decides_a_square_the_plane_never_raised(self) -> None:
        """A claimed square never falls back, so it covers the plane too.

        The file raises three of the four squares; the gate decides the
        fourth in both its states, so nothing is left to read the default.
        0 ft, which no square stands at, must stay out of the range.
        """
        gate = fixture(
            name="floodgate",
            square=(1, 0),
            state="open",
            elevation=HeightPair(closed=20, open=15),
        )
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(0, 5))],
            Random(1),
            map_document=strip(
                2, 2, elevation={(0, 0): 10, (0, 1): 10, (1, 1): 10}, features=(gate,)
            ),
        )
        elevation = encounter.state()["map"]["elevation"]
        assert (elevation["default"], elevation["min"], elevation["max"]) == (0, 10, 15)

    def test_a_claim_does_not_let_the_default_back_into_a_covered_plane(self) -> None:
        """The ``covered`` shortcut has to survive a claim moving a height.

        Every square is raised to 10 by the file and the gate lowers its own
        to 5, so the map's range is 5 to 10. The default of 0 is still what
        no square falls back to.
        """
        gate = fixture(
            name="floodgate",
            square=(2, 0),
            state="open",
            elevation=HeightPair(closed=10, open=5),
        )
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(5, 0))],
            Random(1),
            map_document=strip(
                3,
                elevation={(0, 0): 10, (1, 0): 10, (2, 0): 10},
                features=(gate,),
            ),
        )
        elevation = encounter.state()["map"]["elevation"]
        assert (elevation["default"], elevation["min"], elevation["max"]) == (0, 5, 10)
        assert elevation["flat"] is False

    def test_a_claim_moves_only_its_own_storeys_summary(self) -> None:
        """``_feature_squares`` is keyed by ``(level, square)``, and read so.

        The gate is on the ground, and the gallery over it is untouched by
        it. A summary that matched on the square alone would drag −5 upstairs.
        """
        gate = fixture(
            name="floodgate",
            square=(2, 0),
            state="open",
            affects=(
                MapOverlayRecord(
                    cells=((0, 0),), elevation=HeightPair(closed=0, open=-5)
                ),
            ),
        )
        map_document = storeys(
            floor(0, features=(gate, stair("stair-up", (1, 0), 1))),
            floor(1, feet=10, features=(stair("stair-down", (1, 0), 0),)),
            name="flooded tower",
        )
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))],
            Random(1),
            map_document=map_document,
        )
        ground, gallery = encounter.state()["map"]["levels"]
        assert (ground["elevation"]["min"], ground["elevation"]["max"]) == (-5, 0)
        assert (gallery["elevation"]["min"], gallery["elevation"]["max"]) == (10, 10)
        assert gallery["elevation"]["flat"] is True


class TestSpellcasting:
    def test_casting_spends_a_slot_of_the_chosen_level(self) -> None:
        rng = Random(4)
        wizard = caster()
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
            Random(9),
        )
        assert wizard.spell_slots[3] == 0

    def test_casting_without_a_slot_is_refused(self) -> None:
        rng = Random(4)
        wizard = caster()
        wizard.spell_slots[3] = 0
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="no level 3 slots"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
                rng,
            )

    def test_a_cantrip_is_at_will_and_costs_no_slots(self) -> None:
        """Cantrips are level 0 and bypass the slot-availability check.

        The slot-level check at encounter.py:3000 and the decrement at :3068 are both
        gated by ``if spell.level > 0:``, so level-0 spells skip both and consume no
        resources. This test pins that behaviour; a cantrip succeeds even with an
        empty spell_slots dict, and the dict remains empty after casting.
        """
        rng = Random(4)
        cantrip_book: dict[str, Spell] = {
            "Arcane Missile": Spell(
                name="Arcane Missile",
                level=0,
                school="Evocation",
                requires_attack_roll=True,
                damage=Dice(1, 4, 0),
                damage_type=DamageType.FORCE,
                range_feet=60,
                provenance=FIXTURE,
            )
        }
        caster_creature = caster()
        caster_creature.spells = ("Arcane Missile",)
        caster_creature.spell_slots = {}  # Empty: no slots of any level
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        encounter = Encounter(
            [caster_creature, goblin], rng, spellbook=cantrip_book
        )
        advance_to(encounter, "Wren", rng)
        # Cast succeeds with no slots to spend
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Arcane Missile", targets=("Goblin",)),
            FixedRandom(15),
        )
        # spell_slots remains empty after casting
        assert caster_creature.spell_slots == {}

    def test_a_slot_below_the_spells_level_is_refused_before_anything_is_spent(
        self,
    ) -> None:
        """A refusal must cost nothing — not the slot, and not the action.

        The check that a slot can carry the spell lives in ``resolve_spell``, which
        runs after the action is marked used and the slot decremented. So the
        refusal used to arrive having already taken both, and as a bare
        ``ValueError`` that ``encounter_act`` does not catch — escaping the
        "illegal actions are refused with the reason" contract as an unhandled
        server error.
        """
        rng = Random(4)
        wizard = caster()
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="level 3 .* level 2 slot"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=2, center=30),
                rng,
            )
        assert wizard.spell_slots == {2: 1, 3: 1}
        assert encounter.state()["turn_state"]["action_used"] is False
        # The turn is intact, so the legal cast still goes through.
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
            Random(9),
        )
        assert wizard.spell_slots == {2: 1, 3: 0}

    def test_an_area_spell_catches_everyone_inside_its_radius(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        near = make_monster("Goblin Warrior", label="Goblin A", position=100)
        also_near = make_monster("Goblin Warrior", label="Goblin B", position=110)
        far = make_monster("Goblin Warrior", label="Goblin C", position=300)
        encounter = Encounter([wizard, near, also_near, far], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=105),
            Random(2),
        )
        assert near.hp < near.max_hp
        assert also_near.hp < also_near.max_hp
        assert far.hp == far.max_hp

    def test_an_area_spell_cannot_be_dropped_beyond_its_range(self) -> None:
        # The point of origin is what the range applies to. This used to be checked
        # for no spell with a radius at all, so a 150 ft Fireball would land at any
        # distance whatsoever.
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=1000)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="beyond Fireball's 150 ft range"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=1000),
                Random(2),
            )

    def test_an_area_spell_named_at_a_target_out_of_range_is_refused(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=1000)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="beyond Fireball's 150 ft range"):
            encounter.act(
                Action(
                    kind=ActionKind.CAST,
                    spell="Fireball",
                    slot_level=3,
                    targets=("Goblin",),
                ),
                Random(2),
            )

    def test_a_creature_at_the_far_edge_of_a_blast_does_not_refuse_the_whole_spell(
        self,
    ) -> None:
        # The origin is in range; a creature caught 20 ft further out is not, and
        # must not veto a legal cast. This is why the range check is on the origin
        # rather than on each creature the radius sweeps up.
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=160)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=150),
            Random(2),
        )
        assert goblin.hp < goblin.max_hp

    def test_an_area_spell_may_be_centred_off_the_x_axis(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        high = make_monster("Goblin Warrior", label="Goblin A", position=(100, 40))
        low = make_monster("Goblin Warrior", label="Goblin B", position=100)
        encounter = Encounter([wizard, high, low], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                   center=(100, 40)),
            Random(2),
        )
        assert high.hp < high.max_hp
        assert low.hp == low.max_hp

    def test_an_area_spell_is_bounded_by_its_radius_not_by_max_targets(self) -> None:
        # Every bundled area spell leaves max_targets at its default of 1. Enforcing
        # that on an area would shrink a Fireball to a single creature.
        rng = Random(4)
        wizard = caster(position=0)
        goblins = [
            make_monster("Goblin Warrior", label=f"Goblin {letter}", position=100 + step)
            for step, letter in enumerate("ABC")
        ]
        encounter = Encounter([wizard, *goblins], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=101),
            Random(2),
        )
        assert all(goblin.hp < goblin.max_hp for goblin in goblins)

    def test_naming_more_targets_than_a_spell_allows_is_refused(self) -> None:
        # max_targets is a documented pack field. It used to be sliced with
        # max(cap, len(named)), which can never truncate, so it did nothing at all.
        rng = Random(4)
        priest = caster(position=0)
        priest.spells = ("Guiding Bolt",)
        priest.spell_slots = {1: 4}
        goblins = [
            make_monster("Goblin Warrior", label=f"Goblin {letter}", position=20 + step)
            for step, letter in enumerate("AB")
        ]
        encounter = Encounter([priest, *goblins], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="at most 1 creature"):
            encounter.act(
                Action(
                    kind=ActionKind.CAST,
                    spell="Guiding Bolt",
                    slot_level=1,
                    targets=("Goblin A", "Goblin B"),
                ),
                Random(2),
            )

    def test_casting_an_unprepared_spell_is_refused(self) -> None:
        rng = Random(4)
        wizard = caster()
        encounter = Encounter(
            [wizard, make_monster("Wolf", position=20)], rng, spellbook=spellbook()
        )
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="does not have"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Shatter", targets=("Wolf",)), rng
            )

    def test_a_condition_spell_is_refused_by_immunity_and_registers_no_effect(
        self,
    ) -> None:
        # The spell path is a second funnel into ``Creature.add_condition``,
        # separate from the attack rider one ``TestConditionImmunity`` in
        # ``test_riders.py`` covers: Hold Person must not paralyze a target
        # that is immune to Paralyzed, and it must not leave an ongoing
        # effect behind to release later.
        wren = caster(position=0)
        victim = fighter("Bandit0", team="foes", position=10)
        victim.abilities[Ability.WISDOM] = 6
        victim.condition_immunities = frozenset({Condition.PARALYZED})
        rng = Random(11)
        encounter = Encounter([wren, victim], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target="Bandit0"),
            FixedRandom(1),
        )

        assert Condition.PARALYZED not in victim.conditions
        assert encounter.state()["ongoing_effects"] == []
        refused = next(event for event in events if event.kind == "effect_apply")
        assert refused.data["applied"] is False
        assert refused.data["condition"] == Condition.PARALYZED
        assert "immune" in refused.detail

    def test_damage_forces_a_concentration_check(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        wizard.concentrating_on = "Hold Person"
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Goblin", rng)
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wren", attack="Scimitar"),
            FixedRandom(20),
        )
        assert "concentration" in kinds(events)

    def test_being_knocked_out_ends_concentration(self) -> None:
        wizard = caster()
        wizard.concentrating_on = "Hold Person"
        wizard.take_damage(wizard.hp)
        assert wizard.concentrating_on is None


class TestConcentrationDurationCap:
    """SRD 5.2.1: Hold Person is "Concentration, up to 1 minute" — 10 rounds.

    Concentration already ends the effect four other ways (a failed
    Constitution save, Incapacitated, death, starting another concentration
    effect); this is the fifth, and the only one that fires with the caster
    doing nothing at all. Neither route pre-empts the other: whichever
    arrives first ends the effect.
    """

    def hold_person(
        self, rng: Random, extra: list[Creature] | None = None
    ) -> tuple[Encounter, Creature, Creature]:
        wren = caster(position=0)
        victim = fighter("Bandit0", team="foes", position=10)
        victim.abilities[Ability.WISDOM] = 1
        encounter = Encounter([wren, victim, *(extra or [])], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target="Bandit0"),
            FixedRandom(1),
        )
        assert Condition.PARALYZED in victim.conditions, "the save must fail to set up this test"
        return encounter, wren, victim

    def test_paralysis_releases_on_the_round_its_cap_expires_and_not_before(
        self,
    ) -> None:
        rng = Random(11)
        encounter, wren, victim = self.hold_person(rng)
        cap = encounter.round + spellbook()["Hold Person"].duration_rounds
        while encounter.round < cap - 1:
            encounter.advance(rng)
        assert Condition.PARALYZED in victim.conditions, (
            f"still holds through round {encounter.round}, one short of the cap"
        )
        while encounter.round < cap:
            events = encounter.advance(rng)
        assert Condition.PARALYZED not in victim.conditions
        ended = next(event for event in events if event.kind == "effect_end")
        assert "paralyzed lifts" in ended.detail
        assert wren.concentrating_on is None

    def test_a_spell_with_no_duration_set_behaves_exactly_as_it_does_today(
        self,
    ) -> None:
        # Guiding Bolt has no ongoing condition and no duration cap; it must not
        # gain one as a side effect of this feature.
        rng = Random(4)
        priest = caster(position=0)
        priest.spells = ("Guiding Bolt",)
        priest.spell_slots = {1: 4}
        goblin = make_monster("Goblin Warrior", label="Goblin", position=20)
        encounter = Encounter([priest, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Guiding Bolt", slot_level=1,
                   targets=("Goblin",)),
            Random(2),
        )
        assert not any(event.kind == "effect_apply" for event in events)
        assert encounter.state()["ongoing_effects"] == []

    def test_a_constitution_save_still_ends_it_before_the_cap_arrives(self) -> None:
        # Concentration's own release routes are untouched by the cap: a failed
        # save still ends the effect on whichever round it happens, well before
        # the ten-round timer would. Same shape as
        # ``TestConcentrationEffects.test_failing_the_concentration_save_frees_the_target``.
        brute = fighter("Brute", team="foes", position=5, max_hp=40)
        rng = Random(11)
        encounter, wren, victim = self.hold_person(rng, extra=[brute])
        advance_to(encounter, "Brute", rng)
        # d20 15 hits AC 13; 1d8 damage; then a natural 1 on the Constitution save.
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wren", attack="Longsword"),
            ScriptedRandom([15, 5, 1]),
        )
        assert "loses Hold Person" in detail_of(events, "concentration")
        assert wren.concentrating_on is None
        assert Condition.PARALYZED not in victim.conditions


class TestAoeShapes2D:
    """Golden shape resolutions through the stepper: who is caught is the test."""

    def hit_names(self, events: Sequence[Event]) -> set[str]:
        return {event.target for event in events if event.kind == "spell_effect"}

    def cast(self, encounter: Encounter, rng: Random, **aim: Any) -> set[str]:
        advance_to(encounter, "Vesna", rng)
        events = encounter.act(Action(kind=ActionKind.CAST, **aim), Random(2))
        return self.hit_names(events)

    def test_a_cone_catches_the_wedge_and_misses_a_flank(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(),
                make_monster("Goblin Warrior", label="Front", position=(10, 0)),
                make_monster("Goblin Warrior", label="Flank", position=(5, 10)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = self.cast(encounter, rng, spell="Flame Fan", direction=(1, 0))
        assert caught == {"Front"}

    def test_a_cone_needs_one_of_the_eight_directions(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [shaper(), make_monster("Goblin Warrior", label="Front",
                                    position=(10, 0))],
            rng,
            spellbook=shaped_spellbook(),
        )
        advance_to(encounter, "Vesna", rng)
        with pytest.raises(EncounterError, match="unit offsets"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Flame Fan", direction=(2, 0)),
                rng,
            )

    def test_a_line_runs_down_the_corridor_it_is_aimed_along(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(),
                make_monster("Goblin Warrior", label="Near", position=(10, 0)),
                make_monster("Goblin Warrior", label="Far", position=(25, 0)),
                make_monster("Goblin Warrior", label="Off", position=(10, 5)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = self.cast(encounter, rng, spell="Spark Line", toward="Far")
        assert caught == {"Near", "Far"}

    def test_a_cube_is_a_block_from_its_minimum_corner(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(),
                make_monster("Goblin Warrior", label="Inside", position=(15, 5)),
                make_monster("Goblin Warrior", label="Outside", position=(5, 0)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = self.cast(encounter, rng, spell="Stone Cube", center=(10, 0))
        assert caught == {"Inside"}

    def test_a_sphere_lands_on_a_two_dimensional_cluster(self) -> None:
        rng = Random(4)
        wizard = caster(position=(0, 0))
        encounter = Encounter(
            [
                wizard,
                make_monster("Goblin Warrior", label="A", position=(100, 100)),
                make_monster("Goblin Warrior", label="B", position=(105, 105)),
                make_monster("Goblin Warrior", label="C", position=(140, 140)),
            ],
            rng,
            spellbook=spellbook(),
        )
        advance_to(encounter, "Wren", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                   center=(100, 100)),
            Random(2),
        )
        assert self.hit_names(events) == {"A", "B"}

    def test_a_wall_between_caster_and_origin_refuses_the_sphere(self) -> None:
        rng = Random(4)
        wizard = caster(position=(0, 5))
        encounter = Encounter(
            [wizard, make_monster("Goblin Warrior", label="Goblin",
                                  position=(20, 5))],
            rng,
            spellbook=spellbook(),
            map_document=strip(
                5, 3,
                terrain={(2, 0): "wall", (2, 1): "wall", (2, 2): "wall"},
            ),
        )
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="cannot see"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                       center=(20, 5)),
                rng,
            )

    def test_area_targets_is_the_membership_authority(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(),
                make_monster("Goblin Warrior", label="Front", position=(10, 0)),
                make_monster("Goblin Warrior", label="Flank", position=(5, 10)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = encounter.area_targets(
            encounter.spellbook["Flame Fan"], "Vesna", direction=(1, 0)
        )
        assert [creature.name for creature in caught] == ["Front"]


class TestEmanationAndCylinder:
    """The one crisp behavioural difference: an emanation excludes its origin,
    a cylinder includes it. SRD 5.2.1 p.181 (Emanation) and p.180 (Cylinder).
    """

    def test_a_creature_on_the_origin_square_is_excluded_from_an_emanation(
        self,
    ) -> None:
        # The emanation's origin is the caster's own square — the caster is the
        # only creature guaranteed to stand there, so it is the one the SRD's
        # exclusion clause names.
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(position=(0, 0)),
                make_monster("Goblin Warrior", label="Nearby", position=(1, 0)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = encounter.area_targets(encounter.spellbook["Warm Aura"], "Vesna")
        names = {creature.name for creature in caught}
        assert "Vesna" not in names
        assert "Nearby" in names

    def test_a_creature_on_the_origin_square_is_included_in_a_cylinder(
        self,
    ) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(position=(0, 0)),
                make_monster("Goblin Warrior", label="OnOrigin", position=(30, 0)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = encounter.area_targets(
            encounter.spellbook["Frost Pillar"], "Vesna", center=(30, 0)
        )
        assert [creature.name for creature in caught] == ["OnOrigin"]

    def test_an_emanation_catches_out_to_its_distance_and_no_further(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(position=(0, 0)),
                # 10 ft radius: two squares away is inside, three is outside.
                make_monster("Goblin Warrior", label="Inside", position=(10, 0)),
                make_monster("Goblin Warrior", label="Outside", position=(15, 0)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = encounter.area_targets(encounter.spellbook["Warm Aura"], "Vesna")
        names = {creature.name for creature in caught}
        assert "Inside" in names
        assert "Outside" not in names

    def test_a_cylinder_catches_within_its_base_radius_and_no_further(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [
                shaper(position=(0, 0)),
                make_monster("Goblin Warrior", label="Inside", position=(40, 0)),
                make_monster("Goblin Warrior", label="Outside", position=(45, 0)),
            ],
            rng,
            spellbook=shaped_spellbook(),
        )
        caught = encounter.area_targets(
            encounter.spellbook["Frost Pillar"], "Vesna", center=(30, 0)
        )
        names = {creature.name for creature in caught}
        assert "Inside" in names
        assert "Outside" not in names

    def test_an_emanation_needs_no_aim_at_all(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [shaper(position=(0, 0)),
             make_monster("Goblin Warrior", label="Goblin", position=(5, 0))],
            rng,
            spellbook=shaped_spellbook(),
        )
        advance_to(encounter, "Vesna", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Warm Aura", slot_level=1), rng
        )
        assert any(event.target == "Goblin" for event in events)

    def test_a_cylinder_needs_a_center(self) -> None:
        rng = Random(4)
        encounter = Encounter(
            [shaper(position=(0, 0)),
             make_monster("Goblin Warrior", label="Goblin", position=(5, 0))],
            rng,
            spellbook=shaped_spellbook(),
        )
        with pytest.raises(EncounterError, match="needs 'center'"):
            encounter.area_targets(encounter.spellbook["Frost Pillar"], "Vesna")


class TestSavingThrowAdvantage:
    """A saving throw carries Advantage and Disadvantage the way an attack does.

    The rule these pin is that Restrained does *not* make a Dexterity save fail —
    it makes it hard. A Restrained creature caught in a Fireball still rolls, and
    can still take half damage, which an auto-fail flag makes impossible.
    """

    def fireball_save(
        self, *, conditions: Sequence[str] = (), dodging: bool = False
    ) -> Event:
        """Cast Fireball at a Goblin and return the event describing its save."""
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        for condition in conditions:
            goblin.add_condition(condition)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        if dodging:
            advance_to(encounter, "Goblin", rng)
            encounter.act(Action(kind=ActionKind.DODGE), rng)
        advance_to(encounter, "Wren", rng)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
            Random(9),
        )
        return next(
            event
            for event in events
            if event.kind == "spell_effect" and event.target == "Goblin"
        )

    def test_a_restrained_target_saves_with_disadvantage_rather_than_failing(
        self,
    ) -> None:
        event = self.fireball_save(conditions=(Condition.RESTRAINED,))
        assert rolled_with(event) == "disadvantage"
        assert "auto-fail" not in event.detail

    def test_an_unhindered_target_saves_straight(self) -> None:
        assert rolled_with(self.fireball_save()) == "none"

    def test_a_paralyzed_target_still_fails_outright(self) -> None:
        assert "auto-fail" in self.fireball_save(conditions=(Condition.PARALYZED,)).detail

    def test_dodging_gives_advantage_on_a_dexterity_save(self) -> None:
        assert rolled_with(self.fireball_save(dodging=True)) == "advantage"

    def test_a_restrained_dodger_loses_the_benefit_rather_than_cancelling(self) -> None:
        # Dodge's benefits are lost while Speed is 0, and Restrained sets Speed 0.
        # Treating the Dodge as a live source of Advantage would cancel the
        # Disadvantage and hand the creature a straight roll it has not earned.
        event = self.fireball_save(conditions=(Condition.RESTRAINED,), dodging=True)
        assert rolled_with(event) == "disadvantage"

    def test_a_forced_failure_and_disadvantage_are_decided_independently(self) -> None:
        rng = Random(4)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        goblin.add_condition(Condition.PARALYZED)
        goblin.add_condition(Condition.RESTRAINED)
        encounter = Encounter([caster(), goblin], rng, spellbook=spellbook())
        assert encounter.auto_fails_save(goblin, Ability.DEXTERITY)
        assert encounter.save_advantage(goblin, Ability.DEXTERITY) is Advantage.DISADVANTAGE

    def test_an_items_saving_throw_carries_it_too(self) -> None:
        fire = ItemEffect(
            damage=Dice.parse("2d6"),
            damage_type=DamageType.FIRE,
            save_ability=Ability.DEXTERITY,
            save_dc=13,
            provenance=FIXTURE,
        )
        rng = Random(11)
        thug = fighter("Thug", team="foes", position=0)
        thug.items = {"Alchemist's Fire": 1}
        victim = fighter("Victim", position=5)
        victim.add_condition(Condition.RESTRAINED)
        encounter = Encounter([thug, victim], rng, items={"Alchemist's Fire": fire})
        advance_to(encounter, "Thug", rng)
        events = encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Alchemist's Fire", target="Victim"),
            Random(6),
        )
        assert "disadvantage" in events[0].detail


class TestSpellAttackAdvantage:
    """The cast path reaches the same answer about Advantage as the swing path.

    SRD 5.2.1 Rules Glossary, "Attack Roll": "An attack roll is a D20 Test that
    represents making an attack with a weapon, an Unarmed Strike, or a spell."
    None of the Advantage sources distinguishes the two, so a Blinded caster, a
    Dodging target, and a Paralyzed one have to read identically whether the
    attack came off a sword or out of a spell slot.
    """

    def bolt_caster(self, position: int = 0) -> Creature:
        wren = caster(position=position)
        wren.spells = ("Guiding Bolt",)
        wren.spell_slots = {1: 4}
        wren.spell_attack_bonus = 5
        wren.attacks = (
            AttackOption(
                name="Dagger",
                attack_bonus=5,
                damage=Dice(1, 4, 1),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.MELEE,
                provenance=FIXTURE,
            ),
        )
        return wren

    def mark(self, *, position: Point | int, conditions: Sequence[str] = ()) -> Creature:
        target = Creature(
            name="Mark",
            team="foes",
            ac=15,
            max_hp=200,
            speed=30,
            position=position,
            provenance=FIXTURE,
        )
        for condition in conditions:
            target.add_condition(condition)
        return target

    def bolt(
        self,
        *,
        target_conditions: Sequence[str] = (),
        caster_conditions: Sequence[str] = (),
        distance: int = 30,
        dodging: bool = False,
        rng: Random | None = None,
    ) -> Event:
        """Cast Guiding Bolt at a dummy and return the event describing the attack."""
        driver = Random(4)
        wren = self.bolt_caster()
        for condition in caster_conditions:
            wren.add_condition(condition)
        encounter = Encounter(
            [wren, self.mark(position=distance, conditions=target_conditions)],
            driver,
            spellbook=spellbook(),
        )
        if dodging:
            advance_to(encounter, "Mark", driver)
            encounter.act(Action(kind=ActionKind.DODGE), driver)
        advance_to(encounter, "Wren", driver)
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Guiding Bolt", targets=("Mark",)),
            Random(9) if rng is None else rng,
        )
        return next(event for event in events if event.kind == "spell_effect")

    def test_an_unhindered_target_is_attacked_straight(self) -> None:
        assert rolled_with(self.bolt()) == "none"

    def test_a_point_blank_ranged_spell_attack_has_disadvantage(self) -> None:
        assert rolled_with(self.bolt(distance=5)) == "disadvantage"

    def test_a_point_blank_melee_spell_attack_is_not_hindered(self) -> None:
        blade = Spell(
            name="Spell Blade",
            level=1,
            requires_attack_roll=True,
            attack_kind=AttackKind.MELEE,
            damage=Dice(1, 8),
            damage_type=DamageType.FORCE,
            range_feet=5,
            provenance=FIXTURE,
        )
        wren = self.bolt_caster()
        target = self.mark(position=5)
        encounter = Encounter(
            [wren, target], Random(4), spellbook={blade.name: blade}
        )
        assert encounter.spell_attack_advantage(
            wren, target, blade
        ) is Advantage.NONE

    def test_a_paralyzed_target_grants_advantage(self) -> None:
        event = self.bolt(target_conditions=(Condition.PARALYZED,))
        assert rolled_with(event) == "advantage"

    def test_a_restrained_target_grants_advantage(self) -> None:
        event = self.bolt(target_conditions=(Condition.RESTRAINED,))
        assert rolled_with(event) == "advantage"

    def test_a_blinded_caster_attacks_with_disadvantage(self) -> None:
        event = self.bolt(caster_conditions=(Condition.BLINDED,))
        assert rolled_with(event) == "disadvantage"

    def test_a_frightened_caster_attacks_with_disadvantage(self) -> None:
        event = self.bolt(caster_conditions=(Condition.FRIGHTENED,))
        assert rolled_with(event) == "disadvantage"

    def test_a_dodging_target_imposes_disadvantage(self) -> None:
        # SRD 5.2.1, Dodge: "any attack roll made against you has Disadvantage if
        # you can see the attacker". The _dodging map was never consulted on the
        # cast path, so a Dodge bought nothing against a spell.
        assert rolled_with(self.bolt(dodging=True)) == "disadvantage"

    def test_a_blinded_caster_on_a_paralyzed_target_cancels_to_neither(self) -> None:
        event = self.bolt(
            caster_conditions=(Condition.BLINDED,),
            target_conditions=(Condition.PARALYZED,),
        )
        assert rolled_with(event) == "none"

    def test_a_hit_on_a_paralyzed_target_within_5_feet_is_a_critical(self) -> None:
        # SRD 5.2.1, Paralyzed: "Any attack roll that hits you is a Critical Hit if
        # the attacker is within 5 feet of you."
        event = self.bolt(
            target_conditions=(Condition.PARALYZED,), distance=5, rng=FixedRandom(15)
        )
        assert "critical hit" in event.detail

    def test_the_same_hit_from_beyond_5_feet_is_not(self) -> None:
        event = self.bolt(
            target_conditions=(Condition.PARALYZED,), distance=30, rng=FixedRandom(15)
        )
        assert "critical hit" not in event.detail
        assert "-> hit" in event.detail
        # Only the automatic critical is distance-scoped; the Advantage the
        # condition grants applies at any range.
        assert rolled_with(event) == "advantage"

    def test_point_blank_and_prone_cancel_for_a_ranged_spell_attack(
        self,
    ) -> None:
        # SRD 5.2.1, Prone: "An attack roll against you has Advantage if the
        # attacker is within 5 feet of you. Otherwise, that attack roll has
        # Disadvantage." The clause names a distance and no weapon, so a spell
        # attack reads it exactly as a weapon does. Guiding Bolt is a ranged spell
        # attack, so its close-combat Disadvantage cancels that near Advantage.
        near = self.bolt(target_conditions=(Condition.PRONE,), distance=5)
        far = self.bolt(target_conditions=(Condition.PRONE,), distance=30)
        assert rolled_with(near) == "none"
        assert rolled_with(far) == "disadvantage"

    def test_the_cast_path_and_the_swing_path_agree_about_advantage(self) -> None:
        # The drift guard, and the half of it that still has two code paths to
        # compare: spell_attack_advantage and attack_advantage assemble their
        # arguments separately, and against this target they have to land on the
        # same answer.
        rng = Random(4)
        wren = self.bolt_caster()
        target = self.mark(position=30, conditions=(Condition.PARALYZED,))
        encounter = Encounter([wren, target], rng, spellbook=spellbook())
        dagger = wren.attacks[0]
        bolt = spellbook()["Guiding Bolt"]
        assert encounter.spell_attack_advantage(
            wren, target, bolt
        ) == encounter.attack_advantage(
            wren, target, dagger
        )
        assert encounter.spell_attack_advantage(wren, target, bolt) is Advantage.ADVANTAGE

    def test_one_forced_critical_rule_serves_both_paths(self) -> None:
        # There is deliberately no spell-specific counterpart to compare against:
        # the rule reads the target's conditions and the attacker's distance and
        # nothing about the attack, so the encounter exposes exactly one method and
        # both paths call it. What is left to pin is the distance scope itself.
        rng = Random(4)
        wren = self.bolt_caster()
        near = self.mark(position=5, conditions=(Condition.PARALYZED,))
        far = self.mark(position=30, conditions=(Condition.PARALYZED,))
        far.name = "Distant"
        encounter = Encounter([wren, near, far], rng, spellbook=spellbook())
        assert encounter.attack_forced_critical(wren, near)
        assert not encounter.attack_forced_critical(wren, far)

    def test_the_two_paths_agree_under_a_five_ten_five_diagonal(self) -> None:
        # The fight's DiagonalRule threads through every distance the stepper
        # consults, and an off-axis Prone target is where a dropped rule shows:
        # (5, 5) reads as 5 ft under the default 5-5-5 but 7 ft under this
        # fight's 5-10-5, so Prone's within-5-feet clause flips with the rule.
        # The cast path measured under the default, reading Advantage where the
        # swing path read Disadvantage for the same geometry.
        rng = Random(4)
        wren = self.bolt_caster()
        target = self.mark(position=(5, 5), conditions=(Condition.PRONE,))
        encounter = Encounter(
            [wren, target],
            rng,
            spellbook=spellbook(),
            movement_rule=DiagonalRule.FIVE_TEN_FIVE,
        )
        dagger = wren.attacks[0]
        bolt = spellbook()["Guiding Bolt"]
        assert encounter.spell_attack_advantage(
            wren, target, bolt
        ) == encounter.attack_advantage(
            wren, target, dagger
        )
        assert encounter.spell_attack_advantage(wren, target, bolt) is Advantage.DISADVANTAGE


class TestTurnLegality:
    def test_an_incapacitated_creature_cannot_act(self) -> None:
        rng = Random(1)
        held = fighter("Held")
        held.add_condition(Condition.PARALYZED)
        encounter = Encounter([held, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Held", rng)
        with pytest.raises(EncounterError, match="incapacitated"):
            encounter.act(Action(kind=ActionKind.ATTACK, target="Wolf"), rng)

    def test_attacking_after_casting_is_refused(self) -> None:
        # Casting spends the action, and starting an Attack action needs it. Only
        # attacks_left was checked here, so a caster could cast *and* swing on the
        # same turn — worth a fifth of a caster's measured damage per round.
        rng = Random(4)
        wizard = caster(position=0)
        wizard.attacks = (
            AttackOption(
                name="Dagger",
                attack_bonus=5,
                damage=Dice.parse("1d4+2"),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.MELEE,
            ),
        )
        # An Ogre, because a goblin dies to the Fireball and ends the fight before
        # the second action can be refused.
        ogre = make_monster("Ogre", label="Ogre", position=5)
        encounter = Encounter([wizard, ogre], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=25),
            Random(2),
        )
        with pytest.raises(EncounterError, match="already taken an action"):
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Ogre", attack="Dagger"), rng
            )

    def test_a_multiattack_continues_after_its_first_swing_spends_the_action(
        self,
    ) -> None:
        # The mirror of the above: later swings of a Multiattack must still land,
        # which is why the check is "no attack taken yet" rather than "action used".
        rng = Random(1)
        brute = fighter(attacks_per_action=2)
        encounter = Encounter([brute, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Wolf"), rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Wolf"), rng)
        assert encounter.state()["turn_state"]["attacks_left"] == 0

    def test_two_actions_in_one_turn_are_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DODGE), rng)
        with pytest.raises(EncounterError, match="already taken an action"):
            encounter.act(Action(kind=ActionKind.DASH), rng)

    @staticmethod
    def _crossbow_duel() -> tuple[Encounter, Random]:
        """A shooter with a Loading crossbow, a dagger, and two swings a turn.

        Two attacks per action is what makes the Loading gate observable at all:
        with one swing a turn the weapon's own restriction never binds.
        """
        rng = Random(1)
        shooter = fighter("Sylvi", attacks_per_action=2)
        shooter.attacks = (
            AttackOption(
                name="Light Crossbow",
                attack_bonus=5,
                damage=Dice(1, 8, 3),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.RANGED,
                normal_range=80,
                long_range=320,
                ammunition="Bolt",
                loading=True,
                provenance=FIXTURE,
            ),
            AttackOption(
                name="Dagger",
                attack_bonus=5,
                damage=Dice(1, 4, 3),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.MELEE,
                provenance=FIXTURE,
            ),
        )
        shooter.items = {"Bolt": 5}
        foe = fighter("Snagfinger", team="monsters", position=5)
        encounter = Encounter([shooter, foe], rng)
        advance_to(encounter, "Sylvi", rng)
        return encounter, rng

    def test_a_second_shot_from_a_loading_weapon_is_refused_this_turn(self) -> None:
        encounter, _ = self._crossbow_duel()
        shot = Action(
            kind=ActionKind.ATTACK, target="Snagfinger", attack="Light Crossbow"
        )
        encounter.act(shot, FixedRandom(1))

        with pytest.raises(EncounterError, match="Loading"):
            encounter.act(shot, FixedRandom(1))

        # Refused before anything is spent: the second swing and the second
        # bolt are both still there.
        assert encounter.state()["turn_state"]["attacks_left"] == 1
        assert encounter.creatures["Sylvi"].items == {"Bolt": 4}

    def test_a_melee_swing_after_a_loading_shot_is_still_legal(self) -> None:
        # RAW, and the reason the flag is on the turn rather than on the
        # creature's attacks: Loading caps what the *weapon* can do, not what
        # the wielder can. A gate written as "one attack after a Loading shot"
        # would silently delete the second half of every crossbow-and-blade
        # turn and still look like the rule.
        encounter, _ = self._crossbow_duel()
        encounter.act(
            Action(
                kind=ActionKind.ATTACK, target="Snagfinger", attack="Light Crossbow"
            ),
            FixedRandom(1),
        )

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Snagfinger", attack="Dagger"),
            FixedRandom(20),
        )

        assert "Dagger" in detail_of(events, "attack")
        assert encounter.state()["turn_state"]["attacks_left"] == 0

    def test_the_loading_gate_lifts_on_the_next_turn(self) -> None:
        encounter, rng = self._crossbow_duel()
        shot = Action(
            kind=ActionKind.ATTACK, target="Snagfinger", attack="Light Crossbow"
        )
        encounter.act(shot, FixedRandom(1))
        encounter.advance(rng)
        advance_to(encounter, "Sylvi", rng)

        events = encounter.act(shot, FixedRandom(1))

        assert kinds(events) == ["attack"]
        assert encounter.creatures["Sylvi"].items == {"Bolt": 3}

    def test_state_reports_the_authoritative_view(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        state = encounter.state()
        assert state["round"] == 1
        assert set(state["order"]) == {"Thora", "Wolf"}
        assert {entry["name"] for entry in state["combatants"]} == {"Thora", "Wolf"}
        assert all("hp" in entry and "conditions" in entry for entry in state["combatants"])


class TestConcentrationEffects:
    """A condition a Concentration spell imposes ends when the Concentration does.

    SRD 5.2.1, Rules Glossary, "Concentration": "Some spells and other effects require
    Concentration to remain active, as specified in their descriptions. If the
    effect's creator loses Concentration, the effect ends." Damage that fails the
    Constitution save, the Incapacitated condition, death and starting a second
    Concentration spell are the four routes named there, and every one of them has
    to reach the creature the spell is holding.

    The awkward case, and the reason this needs a ledger rather than a matching
    ``remove_condition`` next to each ``add_condition``, is two casters holding the
    same creature with the same condition. One losing Concentration must free
    nothing, because the other is still holding it.
    """

    def duel(self, *, targets: int = 1) -> tuple[Encounter, Random, dict[str, Creature]]:
        """A caster, one or two foes to hold, and a brute able to hit the caster."""
        wren = caster(position=0)
        wren.max_hp = wren.hp = 60
        wren.spells = ("Fireball", "Guiding Bolt", "Hold Person")
        wren.spell_slots = {1: 1, 2: 3, 3: 1}
        people: list[Creature] = [wren, fighter("Thora", position=0)]
        for index in range(targets):
            held = fighter(f"Bandit{index}", team="foes", position=10 + index, max_hp=40)
            # Wisdom 6: the save fails on anything but a forced high roll.
            held.abilities[Ability.WISDOM] = 6
            people.append(held)
        people.append(fighter("Brute", team="foes", position=5, max_hp=40))
        rng = Random(11)
        encounter = Encounter(people, rng, spellbook=spellbook())
        return encounter, rng, {c.name: c for c in people}

    def their_turn(self, encounter: Encounter, rng: Random, who: str) -> None:
        """Put ``who`` on a turn with its action still in hand."""
        if (encounter.current_name == who
                and encounter.state()["turn_state"]["action_used"]):
            encounter.advance(rng)
        advance_to(encounter, who, rng)

    def hold(
        self, encounter: Encounter, rng: Random, who: str, target: str,
    ) -> list[Event]:
        """``who`` casts Hold Person on ``target``, forcing the save to fail."""
        self.their_turn(encounter, rng, who)
        return encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target=target),
            FixedRandom(1),
        )

    # --- the four ways Concentration ends ---------------------------------
    def test_failing_the_concentration_save_frees_the_target(self) -> None:
        encounter, rng, who = self.duel()
        self.hold(encounter, rng, "Wren", "Bandit0")
        assert Condition.PARALYZED in who["Bandit0"].conditions

        advance_to(encounter, "Brute", rng)
        # d20 15 hits AC 13; 1d8 damage; then a natural 1 on the Constitution save.
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wren", attack="Longsword"),
            ScriptedRandom([15, 5, 1]),
        )
        assert "loses Hold Person" in detail_of(events, "concentration")
        assert who["Wren"].concentrating_on is None
        assert Condition.PARALYZED not in who["Bandit0"].conditions
        assert who["Bandit0"].active

    def test_killing_the_caster_frees_the_target(self) -> None:
        encounter, rng, who = self.duel()
        self.hold(encounter, rng, "Wren", "Bandit0")
        who["Wren"].hp = 4
        advance_to(encounter, "Brute", rng)
        # A hit for more than 4 + max_hp is massive damage: dead outright.
        who["Wren"].max_hp = 4
        encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wren", attack="Longsword"),
            FixedRandom(20),
        )
        assert who["Wren"].dead
        assert Condition.PARALYZED not in who["Bandit0"].conditions
        assert who["Bandit0"].active

    def test_incapacitating_the_caster_frees_the_target(self) -> None:
        """The rival caster holds the caster, and the caster's own hold lapses."""
        encounter, rng, who = self.duel(targets=2)
        rival = who["Bandit1"]
        rival.spells = ("Hold Person",)
        rival.spell_slots = {2: 1}
        rival.spell_save_dc = 15
        self.hold(encounter, rng, "Wren", "Bandit0")
        assert Condition.PARALYZED in who["Bandit0"].conditions

        events = self.hold(encounter, rng, "Bandit1", "Wren")
        assert Condition.PARALYZED in who["Wren"].conditions
        assert who["Wren"].concentrating_on is None
        assert Condition.PARALYZED not in who["Bandit0"].conditions
        assert who["Bandit0"].active
        assert "effect_end" in kinds(events)

    def test_starting_a_second_concentration_spell_ends_the_first(self) -> None:
        """SRD 5.2.1: Concentration is lost "the moment you start casting" another."""
        encounter, rng, who = self.duel(targets=2)
        self.hold(encounter, rng, "Wren", "Bandit0")
        self.hold(encounter, rng, "Wren", "Bandit1")
        assert who["Wren"].concentrating_on == "Hold Person"
        assert Condition.PARALYZED not in who["Bandit0"].conditions
        assert Condition.PARALYZED in who["Bandit1"].conditions

    def test_the_release_happens_before_the_new_spell_resolves(self) -> None:
        """Recasting at the old victim resolves against the post-release state.

        "The moment you start casting" is a *when*, not just a *whether*: by the
        time the new spell rolls its saves, the old spell's conditions are gone.
        Releasing after resolution instead let a caster chain-lock its own
        victim — the paralysis the first cast was still holding auto-failed the
        second cast's Dexterity save, whatever the die said. The end state
        cannot see this (the release still happened, just too late), so the
        pin is the save itself: a forced 19 + 2 beats DC 15, and there is no
        natural-20 auto-success on saves to blur what is being tested. No
        bundled concentration spell forces a Dexterity save, so this needs a
        fixture spell.
        """
        snare = Spell(
            name="Snare",
            level=1,
            save_ability=Ability.DEXTERITY,
            condition=str(Condition.RESTRAINED),
            range_feet=60,
            concentration=True,
            provenance=FIXTURE,
        )
        wren = caster(position=0)
        wren.spells = ("Hold Person", "Snare")
        wren.spell_slots = {1: 1, 2: 1}
        victim = fighter("Bandit0", team="foes", position=10)
        victim.abilities[Ability.WISDOM] = 6
        rng = Random(11)
        book = spellbook()
        book["Snare"] = snare
        encounter = Encounter([wren, victim], rng, spellbook=book)
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target="Bandit0"),
            FixedRandom(1),
        )
        assert Condition.PARALYZED in victim.conditions

        self.their_turn(encounter, rng, "Wren")
        events = encounter.act(
            Action(kind=ActionKind.CAST, spell="Snare", target="Bandit0"),
            FixedRandom(19),
        )
        detail = detail_of(events, "spell_effect")
        assert "auto-fail" not in detail, "the lapsed paralysis must not decide the save"
        assert "saved" in detail
        assert Condition.RESTRAINED not in victim.conditions
        assert Condition.PARALYZED not in victim.conditions
        assert wren.concentrating_on == "Snare"
        # The release is announced before the cast, because that is when it
        # happened: Concentration ends the moment the casting starts.
        assert kinds(events).index("effect_end") < kinds(events).index("cast")

    def test_a_spell_without_concentration_leaves_the_hold_standing(self) -> None:
        """Only a *Concentration* effect displaces one. Guiding Bolt is not one."""
        encounter, rng, who = self.duel()
        self.hold(encounter, rng, "Wren", "Bandit0")
        self.their_turn(encounter, rng, "Wren")
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Guiding Bolt", target="Brute"),
            Random(3),
        )
        assert who["Wren"].concentrating_on == "Hold Person"
        assert Condition.PARALYZED in who["Bandit0"].conditions

    # --- what must *not* be released --------------------------------------
    def test_a_second_caster_holding_the_same_target_keeps_it_held(self) -> None:
        """Requirement: one caster losing Concentration frees nothing on its own."""
        encounter, rng, who = self.duel(targets=2)
        rival = who["Bandit1"]
        rival.team = "party"
        rival.spells = ("Hold Person",)
        rival.spell_slots = {2: 1}
        rival.spell_save_dc = 15
        self.hold(encounter, rng, "Wren", "Bandit0")
        self.hold(encounter, rng, "Bandit1", "Bandit0")
        assert Condition.PARALYZED in who["Bandit0"].conditions

        # Wren alone is knocked out, which ends only Wren's Concentration.
        who["Wren"].take_damage(who["Wren"].hp)
        encounter.advance(rng)
        assert who["Wren"].concentrating_on is None
        assert who["Bandit1"].concentrating_on == "Hold Person"
        assert Condition.PARALYZED in who["Bandit0"].conditions, (
            "the second caster is still holding this creature"
        )

        # Now the second caster drops it too, and only then is the target free.
        who["Bandit1"].take_damage(who["Bandit1"].hp)
        encounter.advance(rng)
        assert Condition.PARALYZED not in who["Bandit0"].conditions

    def test_a_condition_from_an_untracked_source_survives(self) -> None:
        """A condition the ledger did not grant is not the ledger's to remove.

        The release must be shown to have *happened* — asserting only that the
        condition is still there would pass just as well against an engine that
        never releases anything, which is the defect this class exists for.
        """
        encounter, rng, who = self.duel()
        who["Bandit0"].add_condition(Condition.PARALYZED)
        self.hold(encounter, rng, "Wren", "Bandit0")
        who["Wren"].take_damage(who["Wren"].hp)
        events = encounter.advance(rng)
        assert who["Wren"].concentrating_on is None
        assert "persists" in detail_of(events, "effect_end")
        assert Condition.PARALYZED in who["Bandit0"].conditions

    def test_an_unrelated_condition_is_untouched(self) -> None:
        encounter, rng, who = self.duel()
        who["Bandit0"].add_condition(Condition.POISONED)
        self.hold(encounter, rng, "Wren", "Bandit0")
        who["Wren"].take_damage(who["Wren"].hp)
        events = encounter.advance(rng)
        assert "lifts" in detail_of(events, "effect_end")
        assert Condition.PARALYZED not in who["Bandit0"].conditions
        assert Condition.POISONED in who["Bandit0"].conditions

    def test_an_item_applied_condition_is_not_a_concentration_effect(self) -> None:
        """An item's condition has no Concentration behind it, so nothing ends it."""
        rng = Random(6)
        thrower = fighter("Thora", position=0)
        thrower.items = {"Numbing Dart": 1}
        victim = fighter("Bandit0", team="foes", position=5)
        wren = caster(position=0)
        wren.spell_slots = {2: 1}
        brute = fighter("Brute", team="foes", position=5)
        dart = ItemEffect(condition=Condition.PARALYZED, provenance=FIXTURE)
        encounter = Encounter(
            [thrower, wren, victim, brute], rng,
            spellbook=spellbook(), items={"Numbing Dart": dart},
        )
        advance_to(encounter, "Thora", rng)
        encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Numbing Dart", target="Bandit0"), rng
        )
        assert Condition.PARALYZED in victim.conditions

        # An unrelated caster now holds the same creature, then drops it. The dart's
        # condition is a different source and must outlive the spell.
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Hold Person", target="Bandit0"),
            FixedRandom(1),
        )
        wren.take_damage(wren.hp)
        events = encounter.advance(rng)
        assert wren.concentrating_on is None
        assert "persists" in detail_of(events, "effect_end")
        assert Condition.PARALYZED in victim.conditions

    # --- the log ----------------------------------------------------------
    def test_the_release_is_reported(self) -> None:
        encounter, rng, who = self.duel()
        self.hold(encounter, rng, "Wren", "Bandit0")
        advance_to(encounter, "Brute", rng)
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wren", attack="Longsword"),
            ScriptedRandom([15, 5, 1]),
        )
        detail = detail_of(events, "effect_end")
        assert "Hold Person" in detail
        assert str(Condition.PARALYZED) in detail

    def test_the_same_seed_still_produces_the_same_fight(self) -> None:
        """Releases are bookkeeping: they roll nothing and reorder nothing."""
        def transcript() -> list[dict[str, str]]:
            encounter, rng, _ = self.duel(targets=2)
            self.hold(encounter, rng, "Wren", "Bandit0")
            advance_to(encounter, "Brute", rng)
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Wren", attack="Longsword"),
                ScriptedRandom([15, 5, 1]),
            )
            for _ in range(8):
                if encounter.over:
                    break
                encounter.advance(rng)
            return [event.as_dict() for event in encounter.log]

        first = transcript()
        assert any(event["kind"] == "effect_end" for event in first), (
            "the transcript must contain a release, or it pins nothing"
        )
        assert first == transcript()


class TestFacing:
    """Where a creature is looking: derived from its move, or set outright.

    Facing changes no roll — SRD 5.2.1 has no facing rule, and inventing one
    would be shipping mechanics the licence boundary does not cover. These
    cases pin that it is recorded and reported faithfully, and nothing more.
    """

    def test_a_creature_nobody_tracks_is_reported_as_null_rather_than_north(
        self,
    ) -> None:
        """The key is always there; what is absent is a *direction*.

        The payload used to omit the key entirely. It reports ``None`` now, for
        a reason that has nothing to do with facing — see ``LIVE_KEYS`` and
        ``tests/test_state_split.py`` — and the claim this case actually owns is
        unchanged: an untracked creature is not quietly handed a bearing.
        """
        fight = Encounter([fighter("Thora"), fighter("Goblin", team="monsters")], Random(1))

        assert fight.state()["combatants"][0]["facing"] is None

    def test_a_creature_given_one_reports_it(self) -> None:
        thora = fighter("Thora")
        thora.facing = "north"
        fight = Encounter([thora, fighter("Goblin", team="monsters")], Random(1))

        reported = next(
            entry for entry in fight.state()["combatants"] if entry["name"] == "Thora"
        )
        assert reported["facing"] == "north"

    def test_a_move_turns_a_tracked_creature_the_way_it_went(self) -> None:
        thora = fighter("Thora", position=(0, 0))
        thora.facing = "north"
        goblin = fighter("Goblin", team="monsters", position=(100, 100))
        fight = Encounter([thora, goblin], Random(1))
        advance_to(fight, "Thora", Random(1))

        fight.act(Action(kind=ActionKind.MOVE, to_position=(20, 0)), Random(1))

        assert thora.facing == "east"

    def test_a_move_that_ends_where_it_began_leaves_the_facing_alone(self) -> None:
        # The case the primitive refuses a bearing for: a creature that did not
        # travel did not turn, and must not be handed north.
        thora = fighter("Thora", position=(15, 15))
        thora.facing = "west"
        goblin = fighter("Goblin", team="monsters", position=(100, 100))
        fight = Encounter([thora, goblin], Random(1))
        advance_to(fight, "Thora", Random(1))

        fight.act(Action(kind=ActionKind.MOVE, to_position=(15, 15)), Random(1))

        assert thora.facing == "west"

    def test_a_move_does_not_enrol_an_untracked_creature(self) -> None:
        thora = fighter("Thora", position=(0, 0))
        goblin = fighter("Goblin", team="monsters", position=(100, 100))
        fight = Encounter([thora, goblin], Random(1))
        advance_to(fight, "Thora", Random(1))

        fight.act(Action(kind=ActionKind.MOVE, to_position=(20, 0)), Random(1))

        assert thora.facing is None
        assert fight.state()["combatants"][0]["facing"] is None

    def test_an_explicit_facing_beats_what_the_move_derived(self) -> None:
        thora = fighter("Thora", position=(0, 0))
        thora.facing = "north"
        goblin = fighter("Goblin", team="monsters", position=(100, 100))
        fight = Encounter([thora, goblin], Random(1))
        advance_to(fight, "Thora", Random(1))

        fight.act(
            Action(kind=ActionKind.MOVE, to_position=(20, 0), facing="southwest"),
            Random(1),
        )

        assert thora.facing == "southwest"

    def test_an_explicit_facing_is_how_an_untracked_creature_gains_one(self) -> None:
        thora = fighter("Thora")
        fight = Encounter([thora, fighter("Goblin", team="monsters")], Random(1))
        advance_to(fight, "Thora", Random(1))

        fight.act(Action(kind=ActionKind.DODGE, facing="northwest"), Random(1))

        assert thora.facing == "northwest"


class TestHealingInAFight:
    """The path a pregen party takes: a cleric's Cure Wounds and a drunk potion.

    The kernel cases in ``test_spells.py`` pin the arithmetic. What is pinned
    here is the wiring the arithmetic arrives through — that the *acting
    creature's* modifier is the one added, that touch range is enforced by the
    stepper rather than merely recorded on the spell, and that a Bonus Action
    potion leaves the action in hand.
    """

    def _cleric(self, name: str = "Ilma", *, position: int | tuple[int, int] = 0,
                wisdom: int = 16) -> Creature:
        return Creature(
            name=name,
            team="party",
            ac=15,
            max_hp=24,
            speed=30,
            abilities={Ability.WISDOM: wisdom, Ability.CONSTITUTION: 13},
            spells=("Cure Wounds",),
            spell_slots={1: 2},
            spell_save_dc=13,
            spell_attack_bonus=5,
            spellcasting_ability=Ability.WISDOM,
            position=position,
            provenance=FIXTURE,
        )

    def test_a_cast_heals_by_the_acting_creatures_own_modifier(self) -> None:
        # Two clerics differing only in Wisdom, each healing an identical ally
        # from the same seed. Reading the wrong creature's modifier — the
        # target's, say — would make these two totals equal.
        healed: list[int] = []
        for wisdom in (16, 10):
            ilma = self._cleric(wisdom=wisdom)
            # Headroom matters: 4 hp of 30 leaves room for 2d8+3 to land in
            # full, so neither result is capped at max and the difference below
            # is the modifier rather than the ceiling.
            thora = fighter("Thora", hp=4, max_hp=30, position=(5, 0))
            fight = Encounter(
                [ilma, thora, fighter("Goblin", team="monsters", position=(100, 0))],
                Random(3),
                spellbook=spellbook(),
            )
            advance_to(fight, "Ilma", Random(3))
            fight.act(
                Action(kind=ActionKind.CAST, spell="Cure Wounds", slot_level=1,
                       target="Thora"),
                Random(3),
            )
            healed.append(thora.hp - 4)

        assert healed[0] - healed[1] == 3  # +3 Wisdom against +0

    def test_it_will_not_reach_an_ally_across_the_room(self) -> None:
        ilma = self._cleric(position=(0, 0))
        thora = fighter("Thora", hp=4, max_hp=30, position=(60, 0))
        fight = Encounter(
            [ilma, thora, fighter("Goblin", team="monsters", position=(100, 0))],
            Random(3),
            spellbook=spellbook(),
        )
        advance_to(fight, "Ilma", Random(3))

        with pytest.raises(EncounterError, match="5 ft range"):
            fight.act(
                Action(kind=ActionKind.CAST, spell="Cure Wounds", slot_level=1,
                       target="Thora"),
                Random(3),
            )

        assert thora.hp == 4
        assert ilma.spell_slots[1] == 2  # the refusal cost nothing

    def test_a_potion_is_a_bonus_action_and_leaves_the_attack_in_hand(self) -> None:
        thora = fighter("Thora", hp=5, max_hp=30, position=(0, 0))
        thora.items = {"Potion of Healing": 1}
        goblin = fighter("Goblin", team="monsters", position=(5, 0))
        fight = Encounter([thora, goblin], Random(3), items=item_effects())
        advance_to(fight, "Thora", Random(3))

        fight.act(
            Action(kind=ActionKind.USE_ITEM, item="Potion of Healing",
                   as_bonus_action=True),
            Random(3),
        )

        assert thora.hp >= 5 + 4  # 2d4+2, so at least 4 restored
        assert thora.items["Potion of Healing"] == 0
        assert not fight.state()["turn_state"]["action_used"]

    def _bonus_action_spellbook(self) -> dict[str, Spell]:
        """A stand-in for Healing Word, "Casting Time: Bonus Action" in SRD 5.2.1."""
        return {
            "Healing Word Test": Spell(
                name="Healing Word Test", level=1, heal=Dice(2, 4, 0),
                add_spellcasting_modifier=True, range_feet=60,
                action_cost=ActionCost.BONUS_ACTION, provenance=FIXTURE,
            ),
        }

    def test_a_bonus_action_spell_leaves_the_action_in_hand(self) -> None:
        ilma = self._cleric()
        ilma.spells = ("Healing Word Test",)
        thora = fighter("Thora", hp=4, max_hp=30, position=(5, 0))
        fight = Encounter(
            [ilma, thora, fighter("Goblin", team="monsters", position=(100, 0))],
            Random(3),
            spellbook=self._bonus_action_spellbook(),
        )
        advance_to(fight, "Ilma", Random(3))

        fight.act(
            Action(kind=ActionKind.CAST, spell="Healing Word Test", slot_level=1,
                   target="Thora", as_bonus_action=True),
            Random(3),
        )

        assert thora.hp > 4
        assert not fight.state()["turn_state"]["action_used"]
        assert fight.state()["turn_state"]["bonus_action_used"]

    def test_the_cast_event_says_which_budget_it_spent(self) -> None:
        # `use_item` has carried `action_cost` since items learned to cost a bonus
        # action; `cast` did not, so a log could show a Healing Word and a Cure
        # Wounds as the same kind of turn. The key is already in
        # `EVENT_VISIBLE_KEYS` — the budget is spent in the open at a real table.
        ilma = self._cleric()
        ilma.spells = ("Healing Word Test",)
        thora = fighter("Thora", hp=4, max_hp=30, position=(5, 0))
        fight = Encounter(
            [ilma, thora, fighter("Goblin", team="monsters", position=(100, 0))],
            Random(3),
            spellbook=self._bonus_action_spellbook(),
        )
        advance_to(fight, "Ilma", Random(3))

        events = fight.act(
            Action(kind=ActionKind.CAST, spell="Healing Word Test", slot_level=1,
                   target="Thora", as_bonus_action=True),
            Random(3),
        )

        cast = next(e for e in events if e.kind == "cast")
        assert cast.data["action_cost"] == ActionCost.BONUS_ACTION.value

    def test_a_bonus_action_spell_refuses_a_second_bonus_action_this_turn(self) -> None:
        ilma = self._cleric()
        ilma.spells = ("Healing Word Test",)
        thora = fighter("Thora", hp=4, max_hp=30, position=(5, 0))
        fight = Encounter(
            [ilma, thora, fighter("Goblin", team="monsters", position=(100, 0))],
            Random(3),
            spellbook=self._bonus_action_spellbook(),
        )
        advance_to(fight, "Ilma", Random(3))
        fight._turn.bonus_action_used = True

        with pytest.raises(EncounterError, match="already used a bonus action this turn"):
            fight.act(
                Action(kind=ActionKind.CAST, spell="Healing Word Test", slot_level=1,
                       target="Thora", as_bonus_action=True),
                Random(3),
            )

        assert thora.hp == 4  # the refusal cost nothing
        assert ilma.spell_slots[1] == 2

    def test_an_action_cost_spell_refuses_being_cast_as_a_bonus_action(self) -> None:
        ilma = self._cleric()
        thora = fighter("Thora", hp=4, max_hp=30, position=(5, 0))
        fight = Encounter(
            [ilma, thora, fighter("Goblin", team="monsters", position=(100, 0))],
            Random(3),
            spellbook=spellbook(),
        )
        advance_to(fight, "Ilma", Random(3))

        with pytest.raises(EncounterError, match="takes an action, not a bonus action"):
            fight.act(
                Action(kind=ActionKind.CAST, spell="Cure Wounds", slot_level=1,
                       target="Thora", as_bonus_action=True),
                Random(3),
            )

        assert thora.hp == 4
        assert ilma.spell_slots[1] == 2


class TestExplorationMode:
    """An interlude: a chapter with no initiative, no rounds and no end condition.

    This is the container an adventure records its non-combat beats in, and it is
    the fight's own stepper with the fight taken out rather than a second one. So
    what a move costs, what a wall refuses and what a condition forbids are the
    same facts here as anywhere else — the cases below pin that as hard as they
    pin what changed.
    """

    @staticmethod
    def _party() -> list[Creature]:
        """One team, two members. A fight would already be over.

        Thora walks; Kettle stands well clear of her route so that nothing below
        is measuring a path around an ally rather than the thing it names.
        """
        return [fighter(), fighter("Kettle", position=(45, 0))]

    def test_a_one_team_interlude_accepts_a_named_move_and_is_never_over(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            self._party(), rng,
            mode=EncounterMode.EXPLORATION,
            map_document=strip(10),
        )

        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(20, 0)), rng, actor="Thora"
        )

        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 20
        assert move.data["squares"] == [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0]]
        state = encounter.state()
        assert state["over"] is False
        assert state["winner"] is None

    def test_the_same_one_team_encounter_in_combat_refuses_the_move_as_over(self) -> None:
        """The reason an interlude needed a mode at all."""
        rng = Random(1)
        encounter = Encounter(self._party(), rng, map_document=strip(10))

        assert encounter.over is True
        with pytest.raises(EncounterError, match="the encounter is over"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 0)), rng)

    def test_a_fight_is_the_mode_nobody_had_to_ask_for(self) -> None:
        assert Encounter([fighter(), make_monster("Wolf")], Random(1)).mode is (
            EncounterMode.COMBAT
        )

    def test_the_state_reports_which_kind_of_chapter_this_is(self) -> None:
        fight = Encounter([fighter(), make_monster("Wolf")], Random(1))
        interlude = Encounter(
            self._party(), Random(1), mode=EncounterMode.EXPLORATION
        )

        assert fight.state()["mode"] == EncounterMode.COMBAT.value
        assert interlude.state()["mode"] == EncounterMode.EXPLORATION.value

    def test_an_interlude_rolls_no_initiative_and_draws_no_dice_to_start(self) -> None:
        """A seeded roll nobody reads is a divergence waiting to be found.

        The combat half of the case is what stops this passing vacuously: if
        construction ever stopped rolling initiative at all, only that assertion
        would notice.
        """
        rng = Random(1)
        undrawn = rng.getstate()

        interlude = Encounter(self._party(), rng, mode=EncounterMode.EXPLORATION)

        assert interlude.initiative == {}
        assert rng.getstate() == undrawn

        fought = Random(1)
        Encounter([fighter(), make_monster("Wolf")], fought)
        assert fought.getstate() != undrawn

    def test_an_interlude_still_lists_everybody_in_a_settled_order(self) -> None:
        """``order`` is what the brief walks, so it cannot simply be empty."""
        interlude = Encounter(
            [fighter("Kettle"), fighter("Thora"), fighter("Ambrose")],
            Random(1),
            mode=EncounterMode.EXPLORATION,
        )

        assert interlude.order == ["Ambrose", "Kettle", "Thora"]

    def test_an_act_in_an_interlude_must_name_its_actor(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            self._party(), rng, mode=EncounterMode.EXPLORATION, map_document=strip(10)
        )

        with pytest.raises(EncounterError, match="no initiative.*must name its actor"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 0)), rng)

    def test_a_fight_refuses_an_act_that_names_its_actor(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(100, 0))], rng
        )
        advance_to(encounter, "Thora", rng)

        with pytest.raises(EncounterError, match="initiative decides who acts"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=20), rng, actor="Thora"
            )

    def test_an_interlude_refuses_an_actor_it_has_no_combatant_for(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            self._party(), rng, mode=EncounterMode.EXPLORATION, map_document=strip(10)
        )

        with pytest.raises(EncounterError, match="no combatant named 'Nobody'"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(20, 0)), rng, actor="Nobody"
            )

    def test_each_named_act_opens_a_fresh_beat_for_that_actor(self) -> None:
        """Movement back to full, action and bonus action unspent.

        The third act is the assertion that matters: walking the whole 30 feet
        back is only affordable if the beat restored the budget the first act
        spent, so a beat that failed to open would refuse it outright.
        """
        rng = Random(1)
        encounter = Encounter(
            self._party(), rng, mode=EncounterMode.EXPLORATION, map_document=strip(10)
        )

        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(30, 0)), rng, actor="Thora"
        )
        assert encounter.state()["turn_state"]["movement_left"] == 0

        encounter.act(Action(kind=ActionKind.DODGE), rng, actor="Thora")
        assert encounter.state()["turn_state"]["action_used"] is True

        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(0, 0)), rng, actor="Thora"
        )
        budget = encounter.state()["turn_state"]
        assert budget["movement_left"] == 0
        assert budget["action_used"] is False

    def test_the_actor_may_change_from_one_beat_to_the_next(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            self._party(), rng, mode=EncounterMode.EXPLORATION, map_document=strip(10)
        )

        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(20, 0)), rng, actor="Thora"
        )
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(35, 0)), rng, actor="Kettle"
        )

        assert [record.actor for record in encounter.actions] == ["Thora", "Kettle"]
        assert all(event.turn == "Kettle" for event in events)

    def test_crossing_the_room_still_pays_for_the_ground(self) -> None:
        """A real move, not a note about one."""
        rng = Random(1)
        encounter = Encounter(
            self._party(), rng,
            mode=EncounterMode.EXPLORATION,
            map_document=strip(10, terrain={(2, 0): "difficult", (3, 0): "difficult"}),
        )

        with pytest.raises(EncounterError, match="needs 35 ft"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(25, 0)), rng, actor="Thora"
            )

    def test_a_wall_still_stands_in_an_interlude(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            self._party(), rng,
            mode=EncounterMode.EXPLORATION,
            map_document=strip(10, terrain={(1, 0): "wall"}),
        )

        with pytest.raises(EncounterError, match="no route"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng, actor="Thora"
            )

    def test_an_interlude_still_refuses_an_actor_who_cannot_act(self) -> None:
        rng = Random(1)
        held = fighter("Kettle", position=(45, 0))
        held.add_condition(Condition.INCAPACITATED)
        encounter = Encounter(
            [fighter(), held], rng,
            mode=EncounterMode.EXPLORATION,
            map_document=strip(10),
        )

        with pytest.raises(EncounterError, match="Kettle is incapacitated"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(35, 0)), rng, actor="Kettle"
            )

    def test_an_interlude_has_no_rounds_to_advance(self) -> None:
        rng = Random(1)
        encounter = Encounter(self._party(), rng, mode=EncounterMode.EXPLORATION)

        with pytest.raises(EncounterError, match="an interlude has no rounds"):
            encounter.advance(rng)

    def test_a_refused_act_opens_no_beat_and_costs_the_last_one_nothing(self) -> None:
        """The refusal costs nothing, as everywhere else in this file.

        Naming an actor who cannot act must not quietly hand them a fresh
        budget, nor take the floor away from whoever was standing on it.
        """
        rng = Random(1)
        held = fighter("Kettle", position=(45, 0))
        held.add_condition(Condition.INCAPACITATED)
        encounter = Encounter(
            [fighter(), held], rng,
            mode=EncounterMode.EXPLORATION,
            map_document=strip(10),
        )
        encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(30, 0)), rng, actor="Thora"
        )
        spent = encounter.state()

        with pytest.raises(EncounterError, match="Kettle is incapacitated"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(40, 0)), rng, actor="Kettle"
            )

        assert encounter.state() == spent
