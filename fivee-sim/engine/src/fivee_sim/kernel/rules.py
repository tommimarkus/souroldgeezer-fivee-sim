"""Core resolution: d20 tests, attack rolls, saving throws, damage.

Functions here are pure. They take an explicit ``Random`` and return a result
object describing what happened; nothing is mutated and nothing is logged. State
changes are applied by the model layer, which keeps the rules auditable and lets
the analytics replay the identical code path.

All provenance: SRD 5.2 (see NOTICE).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from random import Random

from .dice import Advantage, D20Roll, Dice, DiceRoll, roll_d20, roll_dice


class Ability(StrEnum):
    STRENGTH = "strength"
    DEXTERITY = "dexterity"
    CONSTITUTION = "constitution"
    INTELLIGENCE = "intelligence"
    WISDOM = "wisdom"
    CHARISMA = "charisma"


class DamageType(StrEnum):
    ACID = "acid"
    BLUDGEONING = "bludgeoning"
    COLD = "cold"
    FIRE = "fire"
    FORCE = "force"
    LIGHTNING = "lightning"
    NECROTIC = "necrotic"
    PIERCING = "piercing"
    POISON = "poison"
    PSYCHIC = "psychic"
    RADIANT = "radiant"
    SLASHING = "slashing"
    THUNDER = "thunder"


def ability_modifier(score: int) -> int:
    """Ability modifier for a score, floor-dividing so 9 gives -1 rather than 0."""
    return (score - 10) // 2


def proficiency_bonus(level: int) -> int:
    if level < 1:
        raise ValueError(f"level must be at least 1: {level}")
    return 2 + (level - 1) // 4


@dataclass(frozen=True, slots=True)
class D20Test:
    """A d20 roll compared against a DC."""

    roll: D20Roll
    modifier: int
    dc: int
    auto_failed: bool = False

    @property
    def total(self) -> int:
        return self.roll.natural + self.modifier

    @property
    def rolled_twenty(self) -> bool:
        return self.roll.natural == 20

    @property
    def rolled_one(self) -> bool:
        return self.roll.natural == 1

    @property
    def success(self) -> bool:
        if self.auto_failed:
            return False
        return self.total >= self.dc

    def describe(self) -> str:
        if self.auto_failed:
            return "auto-fail"
        return f"{self.roll.describe()} {self.modifier:+d} = {self.total} vs DC {self.dc}"


def make_d20_test(
    rng: Random,
    *,
    modifier: int,
    dc: int,
    advantage: Advantage = Advantage.NONE,
    auto_fail: bool = False,
) -> D20Test:
    """Roll a d20 test. An auto-failed test still consumes a roll, keeping streams aligned.

    Rolling even when the outcome is forced matters: the analytics replay the same
    RNG stream as live play, and skipping a roll here would desynchronise them.
    """
    return D20Test(
        roll=roll_d20(rng, advantage),
        modifier=modifier,
        dc=dc,
        auto_failed=auto_fail,
    )


@dataclass(frozen=True, slots=True)
class AttackRoll:
    """An attack roll against an Armor Class."""

    roll: D20Roll
    attack_bonus: int
    target_ac: int
    forced_critical: bool = False

    @property
    def total(self) -> int:
        return self.roll.natural + self.attack_bonus

    @property
    def critical(self) -> bool:
        """A natural 20 always crits; a forced critical needs the attack to land."""
        if self.roll.natural == 20:
            return True
        return self.forced_critical and self.hit

    @property
    def hit(self) -> bool:
        if self.roll.natural == 20:
            return True
        if self.roll.natural == 1:
            return False
        return self.total >= self.target_ac

    def describe(self) -> str:
        outcome = "critical hit" if self.critical else ("hit" if self.hit else "miss")
        return (
            f"{self.roll.describe()} {self.attack_bonus:+d} = {self.total} "
            f"vs AC {self.target_ac} -> {outcome}"
        )


def resolve_attack_roll(
    rng: Random,
    *,
    attack_bonus: int,
    target_ac: int,
    advantage: Advantage = Advantage.NONE,
    forced_critical: bool = False,
) -> AttackRoll:
    """Resolve an attack roll.

    A natural 20 hits and crits whatever the AC; a natural 1 misses whatever the
    bonus. ``forced_critical`` covers Paralyzed and Unconscious targets, where a
    melee hit becomes a critical hit — it upgrades a hit, it does not create one.
    """
    return AttackRoll(
        roll=roll_d20(rng, advantage),
        attack_bonus=attack_bonus,
        target_ac=target_ac,
        forced_critical=forced_critical,
    )


def roll_damage(dice: Dice, rng: Random, *, critical: bool = False) -> DiceRoll:
    """Roll damage. On a critical hit the dice double; the flat modifier does not."""
    return roll_dice(dice, rng, critical=critical)


def effective_damage(
    amount: int,
    *,
    resisted: bool = False,
    vulnerable: bool = False,
    immune: bool = False,
) -> int:
    """Apply immunity, Resistance, and Vulnerability to a damage total.

    Resistance halves and rounds down; Vulnerability doubles. Holding both leaves
    the damage unchanged rather than halving and then doubling, which would lose a
    point to rounding on odd totals.
    """
    if immune:
        return 0
    if resisted and vulnerable:
        return amount
    if resisted:
        return amount // 2
    if vulnerable:
        return amount * 2
    return amount


def concentration_dc(damage: int) -> int:
    """DC to keep Concentration after taking damage: half the damage, minimum 10."""
    return max(10, damage // 2)
