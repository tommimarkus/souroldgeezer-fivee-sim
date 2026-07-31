"""The expectation arithmetic, pinned against the kernel actually rolling.

This is the test that makes :mod:`fivee_sim.analytics.expectation` trustworthy. The
module exists so the auto-play policy can rank its options exactly, and an exact
model that has quietly drifted from the resolution code is worse than an obviously
rough one — it is wrong with a straight face.

So nothing here asserts a hand-computed constant. Each test rolls the *real* kernel
many thousands of times and checks the closed form lands on the empirical mean. The
tolerances are loose enough not to flake at the fixed seed and tight enough that the
rounding rules actually have to be right: dropping the floor on a halved save, or
halving an expectation instead of the roll, moves these numbers well outside them.
"""

from __future__ import annotations

import statistics
from inspect import signature
from random import Random

import pytest

from fivee_sim.analytics.expectation import (
    attack_damage_expectation,
    expected_damage,
    save_damage_expectation,
)
from fivee_sim.kernel.actions import resolve_attack
from fivee_sim.kernel.dice import Advantage, Dice, roll_dice
from fivee_sim.kernel.rules import (
    effective_damage,
    make_d20_test,
    resolve_attack_roll,
)

SEED = 20260730
SAMPLES = 40_000


def empirical(values: list[int]) -> float:
    return statistics.fmean(values)


class TestAttackSignatureParity:
    """The closed form and the roller must accept the same attack.

    Every other test here samples: it fixes a set of arguments and checks the
    two agree on *those*. That cannot see a parameter one side grew and the
    other did not — the sampling tests keep passing, because they never pass
    the new argument to either. A rider added to :func:`resolve_attack` alone
    would then be rolled in play and silently valued at zero by the policy
    ranking its options, which is precisely the straight-faced wrongness this
    module's docstring exists to prevent.
    """

    def test_the_closed_form_takes_every_argument_the_roller_does(self) -> None:
        rolled = {
            name: parameter
            for name, parameter in signature(resolve_attack).parameters.items()
            # The roller draws; the closed form integrates. That is the one
            # difference between them, and the only one allowed.
            if name != "rng"
        }
        closed = dict(signature(attack_damage_expectation).parameters)
        assert set(closed) == set(rolled)

    def test_the_two_agree_on_what_each_argument_defaults_to(self) -> None:
        rolled = signature(resolve_attack).parameters
        closed = signature(attack_damage_expectation).parameters
        assert {name: rolled[name].default for name in closed} == {
            name: parameter.default for name, parameter in closed.items()
        }


class TestExpectedDamage:
    @pytest.mark.parametrize(
        "expression",
        ["1d6", "2d6+3", "8d6", "1d8+4", "3d8", "1d4-2", "10d10+7"],
    )
    def test_matches_the_kernel_rolling(self, expression: str) -> None:
        dice = Dice.parse(expression)
        rng = Random(SEED)
        rolled = [roll_dice(dice, rng).total for _ in range(SAMPLES)]
        assert expected_damage(dice) == pytest.approx(empirical(rolled), abs=0.15)

    def test_a_critical_doubles_the_dice_and_not_the_modifier(self) -> None:
        dice = Dice.parse("2d6+3")
        rng = Random(SEED)
        rolled = [roll_dice(dice, rng, critical=True).total for _ in range(SAMPLES)]
        assert expected_damage(dice, critical=True) == pytest.approx(
            empirical(rolled), abs=0.15
        )

    def test_a_negative_modifier_is_clamped_at_zero_not_left_negative(self) -> None:
        # 1d4-6 can never deal damage, and the kernel floors the total at zero
        # rather than letting it go negative. Averaging the raw arithmetic would
        # give -3.5 here.
        dice = Dice.parse("1d4-6")
        assert expected_damage(dice) == 0.0

    def test_halving_floors_the_roll_rather_than_the_expectation(self) -> None:
        # The distinction this whole module exists for: E[floor(x/2)] < E[x]/2
        # whenever an odd total is possible.
        dice = Dice.parse("3d8")
        rng = Random(SEED)
        rolled = [roll_dice(dice, rng).total // 2 for _ in range(SAMPLES)]
        halved = expected_damage(dice, halved=True)
        assert halved == pytest.approx(empirical(rolled), abs=0.15)
        assert halved < expected_damage(dice) / 2

    def test_resistance_halves_and_rounds_down_per_roll(self) -> None:
        dice = Dice.parse("2d6+3")
        rng = Random(SEED)
        rolled = [
            effective_damage(roll_dice(dice, rng).total, resisted=True)
            for _ in range(SAMPLES)
        ]
        assert expected_damage(dice, resisted=True) == pytest.approx(
            empirical(rolled), abs=0.15
        )

    def test_immunity_is_zero_and_vulnerability_doubles(self) -> None:
        dice = Dice.parse("2d6+3")
        assert expected_damage(dice, immune=True) == 0.0
        assert expected_damage(dice, vulnerable=True) == pytest.approx(
            2 * expected_damage(dice)
        )


class TestAttackExpectation:
    @pytest.mark.parametrize("target_ac", [10, 15, 20, 25])
    @pytest.mark.parametrize(
        "advantage", [Advantage.NONE, Advantage.ADVANTAGE, Advantage.DISADVANTAGE]
    )
    def test_matches_the_kernel_resolving_attacks(
        self, target_ac: int, advantage: Advantage
    ) -> None:
        dice = Dice.parse("1d8+4")
        bonus = 6
        rng = Random(SEED)
        dealt = []
        for _ in range(SAMPLES):
            attack = resolve_attack_roll(
                rng, attack_bonus=bonus, target_ac=target_ac, advantage=advantage
            )
            if not attack.hit:
                dealt.append(0)
                continue
            dealt.append(roll_dice(dice, rng, critical=attack.critical).total)
        assert attack_damage_expectation(
            attack_bonus=bonus,
            target_ac=target_ac,
            damage=dice,
            advantage=advantage,
        ) == pytest.approx(empirical(dealt), abs=0.25)

    def test_a_natural_twenty_still_lands_against_an_unreachable_ac(self) -> None:
        # AC 40 is unhittable on the arithmetic; only the natural 20 gets through,
        # and it crits. Expected damage must therefore be exactly 1/20 of a crit.
        dice = Dice.parse("1d8+4")
        expected = attack_damage_expectation(
            attack_bonus=0, target_ac=40, damage=dice
        )
        assert expected == pytest.approx(expected_damage(dice, critical=True) / 20)

    def test_a_natural_one_misses_however_large_the_bonus(self) -> None:
        # AC 2 is hit by everything except the natural 1, which always misses.
        dice = Dice.parse("1d8+4")
        expected = attack_damage_expectation(
            attack_bonus=20, target_ac=2, damage=dice
        )
        normal, critical = expected_damage(dice), expected_damage(dice, critical=True)
        assert expected == pytest.approx((18 * normal + critical) / 20)


class TestAttackRiderExpectation:
    """The riders, pinned against ``resolve_attack`` actually rolling them.

    The kernel path is the full attack — roll, riders, defenses — so these run it
    whole rather than reassembling it from pieces, and the closed form has to land
    on the empirical mean under every advantage state.
    """

    MAIN = Dice(1, 6, 2)
    RIDER = Dice(1, 4, 0)
    BONUS = Dice(1, 4, 0)

    @pytest.mark.parametrize(
        "advantage", [Advantage.NONE, Advantage.ADVANTAGE, Advantage.DISADVANTAGE]
    )
    def test_riders_match_the_kernel_resolving_attacks(
        self, advantage: Advantage
    ) -> None:
        rng = Random(SEED)
        dealt = []
        for _ in range(SAMPLES):
            resolution = resolve_attack(
                rng,
                attack_bonus=4,
                target_ac=15,
                damage=self.MAIN,
                advantage=advantage,
                advantage_bonus_damage=self.RIDER,
                bonus_damage=self.BONUS,
                bonus_resisted=True,
            )
            dealt.append(resolution.total_damage_dealt)
        assert attack_damage_expectation(
            attack_bonus=4,
            target_ac=15,
            damage=self.MAIN,
            advantage=advantage,
            advantage_bonus_damage=self.RIDER,
            bonus_damage=self.BONUS,
            bonus_resisted=True,
        ) == pytest.approx(empirical(dealt), abs=0.25)

    def test_resistance_rounds_the_combined_pool_once(self) -> None:
        # The advantage rider joins the main pool before resistance halves, so
        # the closed form must model E[floor((a+b)/2)] — not floor each part.
        rng = Random(SEED)
        dealt = []
        for _ in range(SAMPLES):
            resolution = resolve_attack(
                rng,
                attack_bonus=4,
                target_ac=15,
                damage=self.MAIN,
                advantage=Advantage.ADVANTAGE,
                advantage_bonus_damage=self.RIDER,
                resisted=True,
            )
            dealt.append(resolution.total_damage_dealt)
        assert attack_damage_expectation(
            attack_bonus=4,
            target_ac=15,
            damage=self.MAIN,
            advantage=Advantage.ADVANTAGE,
            advantage_bonus_damage=self.RIDER,
            resisted=True,
        ) == pytest.approx(empirical(dealt), abs=0.25)

    @pytest.mark.parametrize("advantage", [Advantage.NONE, Advantage.DISADVANTAGE])
    def test_the_advantage_rider_is_worthless_without_advantage(
        self, advantage: Advantage
    ) -> None:
        with_rider = attack_damage_expectation(
            attack_bonus=4, target_ac=15, damage=self.MAIN, advantage=advantage,
            advantage_bonus_damage=self.RIDER,
        )
        without = attack_damage_expectation(
            attack_bonus=4, target_ac=15, damage=self.MAIN, advantage=advantage,
        )
        assert with_rider == without

    def test_the_advantage_rider_raises_the_advantaged_expectation(self) -> None:
        with_rider = attack_damage_expectation(
            attack_bonus=4, target_ac=15, damage=self.MAIN,
            advantage=Advantage.ADVANTAGE, advantage_bonus_damage=self.RIDER,
        )
        without = attack_damage_expectation(
            attack_bonus=4, target_ac=15, damage=self.MAIN,
            advantage=Advantage.ADVANTAGE,
        )
        assert with_rider > without

    def test_a_critical_doubles_every_riders_dice_in_the_arithmetic(self) -> None:
        # AC 40 is unhittable on the arithmetic; only the natural 20 gets
        # through, and it crits. With no defenses in play the pools are
        # independent, so the crit value decomposes into per-pool expectations —
        # each with its dice doubled — and the whole expectation is 1/20 of it.
        expected = attack_damage_expectation(
            attack_bonus=0, target_ac=40, damage=self.MAIN,
            advantage_bonus_damage=self.RIDER, bonus_damage=self.BONUS,
        )
        # advantage_bonus_damage contributes nothing here: the state is NONE.
        crit = expected_damage(self.MAIN, critical=True) + expected_damage(
            self.BONUS, critical=True
        )
        assert expected == pytest.approx(crit / 20)

    def test_bonus_damage_is_defended_against_its_own_type(self) -> None:
        immune = attack_damage_expectation(
            attack_bonus=4, target_ac=15, damage=self.MAIN,
            bonus_damage=self.BONUS, bonus_immune=True,
        )
        plain = attack_damage_expectation(
            attack_bonus=4, target_ac=15, damage=self.MAIN,
        )
        assert immune == pytest.approx(plain)


class TestSaveExpectation:
    @pytest.mark.parametrize("save_modifier", [-1, 2, 5, 9])
    def test_matches_the_kernel_resolving_saves(self, save_modifier: int) -> None:
        dice = Dice.parse("8d6")
        dc = 15
        rng = Random(SEED)
        dealt = []
        for _ in range(SAMPLES):
            damage = roll_dice(dice, rng).total
            save = make_d20_test(rng, modifier=save_modifier, dc=dc)
            dealt.append(damage // 2 if save.success else damage)
        assert save_damage_expectation(
            save_dc=dc, save_modifier=save_modifier, damage=dice
        ) == pytest.approx(empirical(dealt), abs=0.35)

    def test_no_half_on_save_deals_nothing_when_the_save_lands(self) -> None:
        dice = Dice.parse("8d6")
        rng = Random(SEED)
        dealt = []
        for _ in range(SAMPLES):
            damage = roll_dice(dice, rng).total
            save = make_d20_test(rng, modifier=3, dc=15)
            dealt.append(0 if save.success else damage)
        assert save_damage_expectation(
            save_dc=15, save_modifier=3, damage=dice, half_on_save=False
        ) == pytest.approx(empirical(dealt), abs=0.35)

    def test_an_auto_failed_save_takes_the_damage_in_full(self) -> None:
        dice = Dice.parse("8d6")
        assert save_damage_expectation(
            save_dc=15, save_modifier=20, damage=dice, auto_fail=True
        ) == pytest.approx(expected_damage(dice))

    def test_a_spell_offering_no_save_simply_lands(self) -> None:
        # resolve_spell treats the absence of a saving throw as a failed one, not a
        # successful one. Getting this backwards would halve every such spell.
        dice = Dice.parse("3d8")
        assert save_damage_expectation(
            save_dc=15, save_modifier=0, damage=dice, has_save=False
        ) == pytest.approx(expected_damage(dice))
