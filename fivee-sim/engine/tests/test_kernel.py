"""Kernel tests: dice, d20 resolution, damage adjustment, conditions.

These pin the rules that are easy to implement subtly wrong — critical hits
doubling modifiers, Advantage and Disadvantage stacking instead of cancelling,
Resistance rounding — rather than restating what the code obviously does.
"""

from __future__ import annotations

from random import Random

import pytest

from fivee_sim.kernel.actions import (
    AttackKind,
    compute_attack_advantage,
    melee_hit_is_critical,
)
from fivee_sim.kernel.conditions import Condition, is_incapacitated, speed_is_zero
from fivee_sim.kernel.dice import (
    Advantage,
    Dice,
    DiceError,
    resolve_advantage,
    roll_d20,
    roll_dice,
)
from fivee_sim.kernel.rules import (
    AttackRoll,
    ability_modifier,
    concentration_dc,
    effective_damage,
    proficiency_bonus,
    resolve_attack_roll,
)


class FixedRandom(Random):
    """A generator that forces a chosen d20 face, for pinning edge cases.

    The value is clamped to each die's own maximum, so ``FixedRandom(20)`` yields a
    natural 20 on a d20 *and* a 6 on every d6 of the damage that follows. Without
    the clamp a d6 would come back as 20 and damage assertions would be nonsense.
    """

    def __init__(self, natural: int) -> None:
        super().__init__(0)
        self._natural = natural

    def randint(self, a: int, b: int) -> int:
        return min(self._natural, b)


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


class TestConditionInteractions:
    def test_prone_gives_melee_advantage_and_ranged_disadvantage(self) -> None:
        melee = compute_attack_advantage(
            attacker_conditions=(),
            target_conditions=(Condition.PRONE,),
            kind=AttackKind.MELEE,
            distance=5,
        )
        ranged = compute_attack_advantage(
            attacker_conditions=(),
            target_conditions=(Condition.PRONE,),
            kind=AttackKind.RANGED,
            distance=60,
        )
        assert melee is Advantage.ADVANTAGE
        assert ranged is Advantage.DISADVANTAGE

    def test_restrained_target_and_poisoned_attacker_cancel_out(self) -> None:
        assert (
            compute_attack_advantage(
                attacker_conditions=(Condition.POISONED,),
                target_conditions=(Condition.RESTRAINED,),
                kind=AttackKind.MELEE,
                distance=5,
            )
            is Advantage.NONE
        )

    def test_long_range_penalty_is_a_disadvantage_source(self) -> None:
        assert (
            compute_attack_advantage(
                attacker_conditions=(),
                target_conditions=(),
                kind=AttackKind.RANGED,
                distance=200,
                long_range_penalty=True,
            )
            is Advantage.DISADVANTAGE
        )

    def test_paralyzed_makes_melee_hits_critical_only_within_reach(self) -> None:
        assert melee_hit_is_critical(
            target_conditions=(Condition.PARALYZED,), kind=AttackKind.MELEE, distance=5
        )
        assert not melee_hit_is_critical(
            target_conditions=(Condition.PARALYZED,), kind=AttackKind.RANGED, distance=30
        )

    def test_incapacitating_and_speed_zero_conditions(self) -> None:
        assert is_incapacitated((Condition.STUNNED,))
        assert not is_incapacitated((Condition.PRONE,))
        assert speed_is_zero((Condition.GRAPPLED,))
        assert not speed_is_zero((Condition.PRONE,))
