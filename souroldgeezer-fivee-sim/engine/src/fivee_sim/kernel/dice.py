"""Dice and d20 tests.

Every function that rolls takes an explicit ``Random``. There is no module-level
RNG on purpose: reproducibility under a seed is the property the whole engine
rests on, and an ambient generator would quietly destroy it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from random import Random

_DICE_RE = re.compile(r"^\s*(\d*)\s*d\s*(\d+)\s*(?:([+-])\s*(\d+))?\s*$", re.IGNORECASE)


class DiceError(ValueError):
    """This module was handed a dice expression it cannot roll."""


@dataclass(frozen=True, slots=True)
class Dice:
    """A dice expression such as ``2d6+3``."""

    count: int
    faces: int
    modifier: int = 0

    def __post_init__(self) -> None:
        if self.count < 0:
            raise DiceError(f"dice count must not be negative: {self.count}")
        if self.faces < 1:
            raise DiceError(f"dice must have at least one face: {self.faces}")

    @classmethod
    def parse(cls, expression: str) -> Dice:
        match = _DICE_RE.match(expression)
        if match is None:
            raise DiceError(f"not a dice expression: {expression!r}")
        count_text, faces_text, sign, modifier_text = match.groups()
        modifier = int(modifier_text) if modifier_text else 0
        if sign == "-":
            modifier = -modifier
        return cls(count=int(count_text) if count_text else 1, faces=int(faces_text),
                   modifier=modifier)

    def __str__(self) -> str:
        text = f"{self.count}d{self.faces}"
        if self.modifier:
            text += f"{self.modifier:+d}"
        return text


@dataclass(frozen=True, slots=True)
class DiceRoll:
    """The outcome of rolling a :class:`Dice` expression, individual faces kept."""

    dice: Dice
    rolls: tuple[int, ...]
    modifier: int
    critical: bool = False

    @property
    def total(self) -> int:
        return max(0, sum(self.rolls) + self.modifier)

    def describe(self) -> str:
        faces = " + ".join(str(value) for value in self.rolls) or "0"
        text = f"[{faces}]"
        if self.modifier:
            text += f" {self.modifier:+d}"
        return f"{text} = {self.total}"


def roll_dice(dice: Dice, rng: Random, *, critical: bool = False) -> DiceRoll:
    """Roll ``dice``, doubling the *dice* on a critical hit but never the modifier."""
    count = dice.count * 2 if critical else dice.count
    rolls = tuple(rng.randint(1, dice.faces) for _ in range(count))
    return DiceRoll(dice=dice, rolls=rolls, modifier=dice.modifier, critical=critical)


class Advantage(StrEnum):
    NONE = "none"
    ADVANTAGE = "advantage"
    DISADVANTAGE = "disadvantage"


def resolve_advantage(*, advantage_sources: int, disadvantage_sources: int) -> Advantage:
    """Collapse competing sources into a single state.

    Under the 2024 rules a roll with both Advantage and Disadvantage has neither,
    however many sources of each apply — they do not accumulate or outweigh.
    """
    has_advantage = advantage_sources > 0
    has_disadvantage = disadvantage_sources > 0
    if has_advantage and has_disadvantage:
        return Advantage.NONE
    if has_advantage:
        return Advantage.ADVANTAGE
    if has_disadvantage:
        return Advantage.DISADVANTAGE
    return Advantage.NONE


@dataclass(frozen=True, slots=True)
class D20Roll:
    """A d20 roll before any DC comparison."""

    natural: int
    rolls: tuple[int, ...]
    advantage: Advantage

    def describe(self) -> str:
        if len(self.rolls) == 1:
            return f"d20 [{self.natural}]"
        kept = "/".join(str(value) for value in self.rolls)
        return f"d20 [{kept}] {self.advantage.value} -> {self.natural}"


def faces_wanted(advantage: Advantage) -> int:
    """How many d20s this roll puts on the table: two for either lopsided one."""
    return 1 if advantage is Advantage.NONE else 2


def roll_d20(
    rng: Random,
    advantage: Advantage = Advantage.NONE,
) -> D20Roll:
    """Roll the d20s this advantage state requires and keep the applicable face."""
    wanted = faces_wanted(advantage)
    rolls = tuple(rng.randint(1, 20) for _ in range(wanted))
    natural = (
        rolls[0]
        if advantage is Advantage.NONE
        else (max(rolls) if advantage is Advantage.ADVANTAGE else min(rolls))
    )
    return D20Roll(natural=natural, rolls=rolls, advantage=advantage)
