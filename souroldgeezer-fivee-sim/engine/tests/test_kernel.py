"""Kernel tests: dice, d20 resolution, damage adjustment, conditions.

These pin the rules that are easy to implement subtly wrong — critical hits
doubling modifiers, Advantage and Disadvantage stacking instead of cancelling,
Resistance rounding — rather than restating what the code obviously does.
"""

from __future__ import annotations

from inspect import signature
from random import Random

import pytest

from fivee_sim.kernel import conditions as condition_rules
from fivee_sim.kernel.actions import (
    MELEE_THRESHOLD,
    compute_attack_advantage,
    melee_hit_is_critical,
)
from fivee_sim.kernel.conditions import (
    EFFECTS,
    Condition,
    UnknownCondition,
    compute_save_advantage,
    effect_of,
    is_incapacitated,
    speed_is_zero,
    speed_reduction,
)
from fivee_sim.kernel.dice import (
    Advantage,
    Dice,
    DiceError,
    resolve_advantage,
    roll_d20,
    roll_dice,
)
from fivee_sim.kernel.rules import (
    Ability,
    AttackRoll,
    Size,
    ability_modifier,
    concentration_dc,
    effective_damage,
    fits_within,
    make_d20_test,
    proficiency_bonus,
    resolve_attack_roll,
)

from .conftest import FixedRandom


class TestDiceParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1d6", Dice(1, 6, 0)),
            ("d20", Dice(1, 20, 0)),
            ("2d6+3", Dice(2, 6, 3)),
            ("8d6", Dice(8, 6, 0)),
            ("1d8 + 1", Dice(1, 8, 1)),
            ("1d4-1", Dice(1, 4, -1)),
        ],
    )
    def test_parses_expected_forms(self, text: str, expected: Dice) -> None:
        assert Dice.parse(text) == expected

    @pytest.mark.parametrize("text", ["", "6", "d", "2x6", "2d"])
    def test_rejects_nonsense(self, text: str) -> None:
        with pytest.raises(DiceError):
            Dice.parse(text)

    def test_zero_faces_rejected(self) -> None:
        with pytest.raises(DiceError):
            Dice(count=1, faces=0)


class TestCriticalDamage:
    def test_critical_doubles_dice_but_not_the_modifier(self) -> None:
        dice = Dice(count=2, faces=6, modifier=4)
        normal = roll_dice(dice, Random(1))
        critical = roll_dice(dice, Random(1), critical=True)
        assert len(normal.rolls) == 2
        assert len(critical.rolls) == 4
        assert normal.modifier == critical.modifier == 4

    def test_damage_never_goes_below_zero(self) -> None:
        # 1d4-6 always totals negative before flooring.
        assert roll_dice(Dice(1, 4, -6), Random(7)).total == 0


class TestAdvantage:
    def test_advantage_and_disadvantage_cancel_however_many_sources(self) -> None:
        assert resolve_advantage(advantage_sources=3, disadvantage_sources=1) is Advantage.NONE
        assert resolve_advantage(advantage_sources=1, disadvantage_sources=5) is Advantage.NONE

    def test_single_sided_sources_resolve(self) -> None:
        assert resolve_advantage(advantage_sources=2, disadvantage_sources=0) is Advantage.ADVANTAGE
        assert (
            resolve_advantage(advantage_sources=0, disadvantage_sources=1)
            is Advantage.DISADVANTAGE
        )

    def test_advantage_keeps_the_higher_of_two_rolls(self) -> None:
        with_advantage = roll_d20(Random(4), Advantage.ADVANTAGE)
        assert with_advantage.natural == max(with_advantage.rolls)
        assert len(with_advantage.rolls) == 2

    def test_disadvantage_keeps_the_lower(self) -> None:
        with_disadvantage = roll_d20(Random(4), Advantage.DISADVANTAGE)
        assert with_disadvantage.natural == min(with_disadvantage.rolls)

    def test_advantage_beats_flat_beats_disadvantage_across_many_seeds(self) -> None:
        seeds = range(400)
        flat = sum(roll_d20(Random(s)).natural for s in seeds)
        high = sum(roll_d20(Random(s), Advantage.ADVANTAGE).natural for s in seeds)
        low = sum(roll_d20(Random(s), Advantage.DISADVANTAGE).natural for s in seeds)
        assert low < flat < high


class TestSuppliedNaturals:
    """A face the caller rolled, used in place of the one the engine would draw.

    A person at the table rolls their own die and reports the number; the engine
    still owns the modifier, the DC, and the critical. So the face is an *input*
    to resolution, and these pin what an input may be, what it decides, and what
    it is not allowed to disturb.
    """

    def test_a_supplied_face_wins_whatever_the_stream_would_have_drawn(self) -> None:
        # Asserted across two unrelated seeds rather than against one drawn
        # value, so it cannot pass by coinciding with the draw.
        assert roll_d20(Random(1), supplied=(7,)).natural == 7
        assert roll_d20(Random(999), supplied=(7,)).natural == 7

    def test_the_supplied_face_is_reported_as_the_roll_that_happened(self) -> None:
        rolled = roll_d20(Random(1), supplied=(7,))
        assert rolled.rolls == (7,)
        assert "7" in rolled.describe()

    def test_advantage_keeps_the_higher_of_two_supplied_faces(self) -> None:
        rolled = roll_d20(Random(1), Advantage.ADVANTAGE, supplied=(4, 17))
        assert rolled.natural == 17
        assert rolled.rolls == (4, 17)

    def test_disadvantage_keeps_the_lower_of_two_supplied_faces(self) -> None:
        assert roll_d20(Random(1), Advantage.DISADVANTAGE, supplied=(4, 17)).natural == 4

    def test_one_face_is_refused_where_advantage_needs_two(self) -> None:
        with pytest.raises(DiceError, match="two faces"):
            roll_d20(Random(1), Advantage.ADVANTAGE, supplied=(11,))

    def test_two_faces_are_refused_where_a_flat_roll_needs_one(self) -> None:
        with pytest.raises(DiceError, match="one face"):
            roll_d20(Random(1), supplied=(11, 12))

    def test_no_faces_at_all_is_refused_rather_than_read_as_absent(self) -> None:
        # An empty tuple is a caller who meant to supply and supplied nothing.
        # ``None`` is the way to say "you roll it", and they must not be the same.
        with pytest.raises(DiceError, match="one face"):
            roll_d20(Random(1), supplied=())

    @pytest.mark.parametrize("face", [0, 21, -1])
    def test_a_face_the_die_does_not_have_is_refused(self, face: int) -> None:
        with pytest.raises(DiceError, match="1 and 20"):
            roll_d20(Random(1), supplied=(face,))

    def test_supplying_a_face_leaves_the_rest_of_the_stream_where_it_was(self) -> None:
        # The reason ``roll_d20`` draws and then overrides rather than skipping
        # the draw. ``make_d20_test`` already rolls for an auto-failed test to
        # keep live play and the analytics replay aligned; the same reasoning
        # applies here, and it buys something a table can feel: one player
        # rolling their own die changes their own result and nobody else's.
        plain = Random(11)
        roll_d20(plain)
        after_plain = roll_d20(plain).natural

        overridden = Random(11)
        roll_d20(overridden, supplied=(13,))
        after_overridden = roll_d20(overridden).natural

        assert after_overridden == after_plain

    def test_advantage_consumes_both_draws_it_would_have_made(self) -> None:
        plain = Random(11)
        roll_d20(plain, Advantage.ADVANTAGE)
        after_plain = roll_d20(plain).natural

        overridden = Random(11)
        roll_d20(overridden, Advantage.ADVANTAGE, supplied=(3, 19))
        after_overridden = roll_d20(overridden).natural

        assert after_overridden == after_plain


class TestAttackRolls:
    def test_natural_twenty_hits_and_crits_regardless_of_ac(self) -> None:
        attack = self._forced_natural(20, attack_bonus=-5, target_ac=30)
        assert attack.hit
        assert attack.critical

    def test_natural_one_misses_regardless_of_bonus(self) -> None:
        attack = self._forced_natural(1, attack_bonus=50, target_ac=5)
        assert not attack.hit
        assert not attack.critical

    def test_forced_critical_upgrades_a_hit_but_does_not_create_one(self) -> None:
        # A Paralyzed target turns hits into crits; it does not make misses land.
        miss = self._forced_natural(2, attack_bonus=0, target_ac=25, forced_critical=True)
        assert not miss.hit
        assert not miss.critical
        hit = self._forced_natural(15, attack_bonus=0, target_ac=10, forced_critical=True)
        assert hit.hit
        assert hit.critical

    @staticmethod
    def _forced_natural(
        natural: int, *, attack_bonus: int, target_ac: int, forced_critical: bool = False
    ) -> AttackRoll:
        return resolve_attack_roll(
            FixedRandom(natural),
            attack_bonus=attack_bonus,
            target_ac=target_ac,
            forced_critical=forced_critical,
        )


class TestDamageAdjustment:
    def test_resistance_halves_rounding_down(self) -> None:
        assert effective_damage(7, resisted=True) == 3

    def test_vulnerability_doubles(self) -> None:
        assert effective_damage(7, vulnerable=True) == 14

    def test_immunity_zeroes(self) -> None:
        assert effective_damage(99, immune=True) == 0

    def test_resistance_and_vulnerability_leave_damage_untouched(self) -> None:
        # Halving then doubling would lose a point on odd totals.
        assert effective_damage(7, resisted=True, vulnerable=True) == 7

    def test_immunity_wins_over_vulnerability(self) -> None:
        assert effective_damage(10, vulnerable=True, immune=True) == 0


class TestDerivedNumbers:
    @pytest.mark.parametrize(
        ("score", "modifier"), [(1, -5), (8, -1), (9, -1), (10, 0), (11, 0), (20, 5)]
    )
    def test_ability_modifier_floors_toward_negative(self, score: int, modifier: int) -> None:
        assert ability_modifier(score) == modifier

    @pytest.mark.parametrize(("level", "bonus"), [(1, 2), (4, 2), (5, 3), (9, 4), (17, 6), (20, 6)])
    def test_proficiency_bonus_by_level(self, level: int, bonus: int) -> None:
        assert proficiency_bonus(level) == bonus

    def test_proficiency_rejects_level_zero(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            proficiency_bonus(0)

    @pytest.mark.parametrize(("damage", "dc"), [(1, 10), (19, 10), (20, 10), (22, 11), (50, 25)])
    def test_concentration_dc_is_half_damage_minimum_ten(self, damage: int, dc: int) -> None:
        assert concentration_dc(damage) == dc


class TestCreatureSize:
    """Size ordering, which a ``StrEnum`` does not give for free.

    Compared as strings these sort gargantuan < huge < large < medium < small <
    tiny — alphabetical, and the exact opposite of the answer "Medium or smaller"
    needs at both ends. Every case below would pass against a lexicographic
    comparison only by accident, so the pairs are checked in both directions.
    """

    def test_declaration_order_runs_smallest_to_largest(self) -> None:
        assert list(Size) == [
            Size.TINY,
            Size.SMALL,
            Size.MEDIUM,
            Size.LARGE,
            Size.HUGE,
            Size.GARGANTUAN,
        ]

    @pytest.mark.parametrize(
        ("size", "limit"),
        [
            (Size.TINY, Size.MEDIUM),
            (Size.SMALL, Size.MEDIUM),
            (Size.MEDIUM, Size.MEDIUM),
            (Size.TINY, Size.GARGANTUAN),
            (Size.GARGANTUAN, Size.GARGANTUAN),
        ],
    )
    def test_a_size_at_or_below_the_limit_fits(self, size: Size, limit: Size) -> None:
        assert fits_within(size, limit)

    @pytest.mark.parametrize(
        ("size", "limit"),
        [
            (Size.LARGE, Size.MEDIUM),
            (Size.HUGE, Size.MEDIUM),
            (Size.GARGANTUAN, Size.MEDIUM),
            (Size.GARGANTUAN, Size.TINY),
            (Size.SMALL, Size.TINY),
        ],
    )
    def test_a_size_above_the_limit_does_not_fit(self, size: Size, limit: Size) -> None:
        assert not fits_within(size, limit)

    def test_the_comparison_is_not_the_string_comparison(self) -> None:
        # The guard against someone "simplifying" fits_within to ``size <= limit``:
        # as strings that expression is True here, and the rule says otherwise.
        assert "large" <= "medium"
        assert not fits_within(Size.LARGE, Size.MEDIUM)


class TestConditionInteractions:
    @pytest.mark.parametrize("condition", [Condition.POISONED, Condition.FRIGHTENED])
    def test_conditions_that_hinder_ability_checks_impose_disadvantage(
        self, condition: Condition
    ) -> None:
        assert (
            condition_rules.compute_ability_check_advantage(conditions=(condition,))
            is Advantage.DISADVANTAGE
        )

    def test_an_external_ability_check_advantage_cancels_condition_disadvantage(
        self,
    ) -> None:
        assert (
            condition_rules.compute_ability_check_advantage(
                conditions=(Condition.POISONED,), extra_advantage=1
            )
            is Advantage.NONE
        )

    def test_a_condition_effect_is_not_cumulative_by_default(self) -> None:
        # SRD 5.2.1 p.179: "A condition doesn't stack with itself; a recipient
        # either has a condition or doesn't." Every SRD condition row is the
        # default that clause describes.
        assert condition_rules.ConditionEffect().cumulative is False

    def test_cumulative_is_a_recognised_effect_flag(self) -> None:
        # A pack that declares "cumulative": true on a condition's effects must
        # not be told the flag does not exist.
        assert "cumulative" in condition_rules.EFFECT_FLAGS

    def test_exhaustion_is_the_one_cumulative_bundled_condition(self) -> None:
        # SRD 5.2.1 p.179 names Exhaustion as the one exception to "a condition
        # doesn't stack with itself" — every other bundled row keeps the default.
        cumulative = {
            name for name, effect in EFFECTS.items() if effect.cumulative
        }
        assert cumulative == {Condition.EXHAUSTION}

    def test_death_at_level_is_zero_by_default(self) -> None:
        # 0 means "never" — the level a held condition cannot reach.
        assert condition_rules.ConditionEffect().death_at_level == 0

    def test_death_at_level_is_a_recognised_effect_flag(self) -> None:
        assert "death_at_level" in condition_rules.EFFECT_FLAGS

    def test_exhaustion_row(self) -> None:
        # SRD 5.2.1 p.181: "This condition is cumulative... You die if your
        # Exhaustion level is 6... the roll is reduced by 2 times your
        # Exhaustion level... your Speed is reduced by a number of feet
        # equal to 5 times your Exhaustion level."
        effect = EFFECTS[Condition.EXHAUSTION]
        assert effect.cumulative is True
        assert effect.d20_test_penalty_per_level == 2
        assert effect.speed_reduction_feet_per_level == 5
        assert effect.death_at_level == 6

    def test_a_custom_condition_can_grant_ability_check_advantage(self) -> None:
        table = {
            **condition_rules.EFFECTS,
            "focused": condition_rules.ConditionEffect(
                own_ability_checks_have_advantage=True
            ),
        }
        assert (
            condition_rules.compute_ability_check_advantage(
                conditions=("focused",), condition_effects=table
            )
            is Advantage.ADVANTAGE
        )

    def test_prone_advantage_is_scoped_by_distance_not_by_weapon(self) -> None:
        # SRD 5.2.1 Rules Glossary, Prone, "Attacks Affected": "You have Disadvantage
        # on attack rolls. An attack roll against you has Advantage if the attacker
        # is within 5 feet of you. Otherwise, that attack roll has Disadvantage."
        # The same shape as the Paralyzed/Unconscious automatic critical: it names a
        # distance and no weapon kind, which is why this function takes no
        # AttackKind. The boundary is what the old melee gate got wrong — a shot
        # fired from inside 5 feet is still made by an attacker "within 5 feet".
        assert compute_attack_advantage(
            attacker_conditions=(),
            target_conditions=(Condition.PRONE,),
            distance=MELEE_THRESHOLD,
        ) is Advantage.ADVANTAGE
        assert compute_attack_advantage(
            attacker_conditions=(),
            target_conditions=(Condition.PRONE,),
            distance=MELEE_THRESHOLD + 5,
        ) is Advantage.DISADVANTAGE
        assert compute_attack_advantage(
            attacker_conditions=(),
            target_conditions=(Condition.PRONE,),
            distance=60,
        ) is Advantage.DISADVANTAGE

    def test_compute_attack_advantage_does_not_consult_the_weapon(self) -> None:
        # The guard against reintroducing the gate: no source of Advantage in the
        # table is scoped by melee/ranged, so the function has nothing to read an
        # AttackKind for. ``long_range_penalty`` is the caller's job precisely
        # because it is the one thing that *is* weapon-shaped.
        parameters = signature(compute_attack_advantage).parameters
        assert "kind" not in parameters
        # ``from __future__ import annotations`` makes these strings, so match on the
        # name rather than the class.
        assert not any(
            "AttackKind" in str(parameter.annotation)
            for parameter in parameters.values()
        )

    def test_restrained_target_and_poisoned_attacker_cancel_out(self) -> None:
        assert (
            compute_attack_advantage(
                attacker_conditions=(Condition.POISONED,),
                target_conditions=(Condition.RESTRAINED,),
                distance=5,
            )
            is Advantage.NONE
        )

    def test_long_range_penalty_is_a_disadvantage_source(self) -> None:
        assert (
            compute_attack_advantage(
                attacker_conditions=(),
                target_conditions=(),
                distance=200,
                long_range_penalty=True,
            )
            is Advantage.DISADVANTAGE
        )

    def test_paralyzed_makes_melee_hits_critical_only_within_reach(self) -> None:
        assert melee_hit_is_critical(
            target_conditions=(Condition.PARALYZED,), distance=5
        )
        assert not melee_hit_is_critical(
            target_conditions=(Condition.PARALYZED,), distance=30
        )

    def test_the_automatic_critical_is_scoped_by_distance_not_by_weapon(self) -> None:
        # SRD 5.2.1 Rules Glossary, Paralyzed and Unconscious, both verbatim: "Any
        # attack roll that hits you is a Critical Hit if the attacker is within 5
        # feet of you." The clause names a distance and no weapon kind, which is
        # why the function takes no AttackKind: an attack that is not melee still
        # qualifies when it is made from inside 5 feet, and one that is melee does
        # not qualify from outside it.
        for condition in (Condition.PARALYZED, Condition.UNCONSCIOUS):
            assert melee_hit_is_critical(
                target_conditions=(condition,), distance=MELEE_THRESHOLD
            )
            assert not melee_hit_is_critical(
                target_conditions=(condition,), distance=MELEE_THRESHOLD + 5
            )

    def test_incapacitating_and_speed_zero_conditions(self) -> None:
        assert is_incapacitated((Condition.STUNNED,))
        assert not is_incapacitated((Condition.PRONE,))
        assert speed_is_zero((Condition.GRAPPLED,))
        assert not speed_is_zero((Condition.PRONE,))

    def test_stunned_does_not_zero_speed(self) -> None:
        # SRD 5.2.1 Stunned is Incapacitated, auto-failed Strength and Dexterity
        # saves, and Advantage on attacks against you. There is no Speed 0 clause —
        # that was the 2014 wording ("can't move"), and Incapacitated does not carry
        # one either. Paralyzed, Petrified, and Unconscious each state Speed 0
        # explicitly, which is what makes its absence here deliberate.
        assert not speed_is_zero((Condition.STUNNED,))

    def test_speed_reduction_sums_per_level_across_held_conditions(self) -> None:
        # Mirrors d20_test_penalty's own shape: a per-level field, summed over
        # a name-to-level mapping, uniform whether the condition is cumulative
        # or an ordinary one permanently at level 1.
        table = dict(EFFECTS) | {
            "weary": condition_rules.ConditionEffect(speed_reduction_feet_per_level=5),
        }
        assert speed_reduction({"weary": 3}, table) == 15
        assert speed_reduction({Condition.PRONE: 1}, table) == 0
        assert speed_reduction({}, table) == 0


class TestSavingThrowConditions:
    """Which conditions touch a saving throw, and how.

    SRD 5.2.1 divides them two ways, and the difference is not cosmetic. Paralyzed,
    Petrified, Stunned, and Unconscious make Strength and Dexterity saving throws
    fail outright. Restrained only imposes Disadvantage on Dexterity saving throws
    — the creature still rolls, and can still succeed.
    """

    def test_restrained_imposes_disadvantage_rather_than_automatic_failure(self) -> None:
        effect = effect_of(Condition.RESTRAINED)
        assert not effect.auto_fail_dexterity_saves
        assert effect.disadvantage_on_dexterity_saves

    def test_only_the_four_incapacitating_conditions_auto_fail_saves(self) -> None:
        auto_failing = {
            name
            for name, effect in EFFECTS.items()
            if effect.auto_fail_strength_saves or effect.auto_fail_dexterity_saves
        }
        assert auto_failing == {
            Condition.PARALYZED,
            Condition.PETRIFIED,
            Condition.STUNNED,
            Condition.UNCONSCIOUS,
        }

    def test_a_restrained_creature_rolls_dexterity_saves_with_disadvantage(self) -> None:
        assert (
            compute_save_advantage(
                conditions=(Condition.RESTRAINED,), ability=Ability.DEXTERITY
            )
            is Advantage.DISADVANTAGE
        )

    def test_restrained_leaves_every_other_ability_alone(self) -> None:
        for ability in (Ability.STRENGTH, Ability.CONSTITUTION, Ability.WISDOM):
            assert (
                compute_save_advantage(
                    conditions=(Condition.RESTRAINED,), ability=ability
                )
                is Advantage.NONE
            )

    def test_advantage_from_elsewhere_cancels_the_disadvantage(self) -> None:
        # Dodge is the caller that supplies it. The cancel rule is the one attack
        # rolls already use: any Advantage plus any Disadvantage yields neither.
        assert (
            compute_save_advantage(
                conditions=(Condition.RESTRAINED,),
                ability=Ability.DEXTERITY,
                extra_advantage=1,
            )
            is Advantage.NONE
        )

    def test_a_condition_the_table_does_not_define_is_reported(self) -> None:
        with pytest.raises(UnknownCondition):
            compute_save_advantage(conditions=("smitten",), ability=Ability.STRENGTH)

    def test_an_auto_failed_save_still_rolls_its_dice(self) -> None:
        # Paralyzed and Restrained together: the save fails whatever the dice say,
        # but Disadvantage still costs two rolls. Skipping them when the outcome is
        # forced would desynchronise the analytics replay from live play.
        test = make_d20_test(
            Random(1),
            modifier=3,
            dc=15,
            advantage=Advantage.DISADVANTAGE,
            auto_fail=True,
        )
        assert not test.success
        assert len(test.roll.rolls) == 2
