"""Conditions expressed as data rather than scattered branches.

Every condition's combat-relevant consequences live in one table. Adding a
condition means adding a row, and the attack/save code reads the table instead of
growing another special case.

All provenance: SRD 5.2 (see NOTICE).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Condition(StrEnum):
    BLINDED = "blinded"
    CHARMED = "charmed"
    DEAFENED = "deafened"
    FRIGHTENED = "frightened"
    GRAPPLED = "grappled"
    INCAPACITATED = "incapacitated"
    INVISIBLE = "invisible"
    PARALYZED = "paralyzed"
    PETRIFIED = "petrified"
    POISONED = "poisoned"
    PRONE = "prone"
    RESTRAINED = "restrained"
    STUNNED = "stunned"
    UNCONSCIOUS = "unconscious"


@dataclass(frozen=True, slots=True)
class ConditionEffect:
    """The mechanical consequences of one condition."""

    incapacitated: bool = False
    speed_zero: bool = False
    #: Attack rolls against the afflicted creature.
    attacked_with_advantage: bool = False
    attacked_with_disadvantage: bool = False
    #: Prone is directional: advantage in melee, disadvantage at range.
    attacked_with_advantage_in_melee: bool = False
    attacked_with_disadvantage_at_range: bool = False
    #: The afflicted creature's own attack rolls.
    own_attacks_have_advantage: bool = False
    own_attacks_have_disadvantage: bool = False
    auto_fail_strength_saves: bool = False
    auto_fail_dexterity_saves: bool = False
    #: Paralyzed and Unconscious turn melee hits into critical hits.
    melee_hits_are_critical: bool = False
    resists_all_damage: bool = False


_NO_EFFECT = ConditionEffect()

EFFECTS: dict[Condition, ConditionEffect] = {
    Condition.BLINDED: ConditionEffect(
        attacked_with_advantage=True,
        own_attacks_have_disadvantage=True,
    ),
    # Charmed and Deafened carry no combat-roll consequences; they are tracked so
    # narration and targeting restrictions can see them.
    Condition.CHARMED: _NO_EFFECT,
    Condition.DEAFENED: _NO_EFFECT,
    Condition.FRIGHTENED: ConditionEffect(
        own_attacks_have_disadvantage=True,
    ),
    Condition.GRAPPLED: ConditionEffect(
        speed_zero=True,
        own_attacks_have_disadvantage=True,
    ),
    Condition.INCAPACITATED: ConditionEffect(
        incapacitated=True,
    ),
    Condition.INVISIBLE: ConditionEffect(
        attacked_with_disadvantage=True,
        own_attacks_have_advantage=True,
    ),
    Condition.PARALYZED: ConditionEffect(
        incapacitated=True,
        speed_zero=True,
        attacked_with_advantage=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
        melee_hits_are_critical=True,
    ),
    Condition.PETRIFIED: ConditionEffect(
        incapacitated=True,
        speed_zero=True,
        attacked_with_advantage=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
        resists_all_damage=True,
    ),
    Condition.POISONED: ConditionEffect(
        own_attacks_have_disadvantage=True,
    ),
    Condition.PRONE: ConditionEffect(
        own_attacks_have_disadvantage=True,
        attacked_with_advantage_in_melee=True,
        attacked_with_disadvantage_at_range=True,
    ),
    Condition.RESTRAINED: ConditionEffect(
        speed_zero=True,
        attacked_with_advantage=True,
        own_attacks_have_disadvantage=True,
        auto_fail_dexterity_saves=True,
    ),
    Condition.STUNNED: ConditionEffect(
        incapacitated=True,
        speed_zero=True,
        attacked_with_advantage=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
    ),
    Condition.UNCONSCIOUS: ConditionEffect(
        incapacitated=True,
        speed_zero=True,
        attacked_with_advantage=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
        melee_hits_are_critical=True,
    ),
}

#: Conditions that imply Incapacitated, which in turn breaks concentration.
INCAPACITATING: frozenset[Condition] = frozenset(
    condition for condition, effect in EFFECTS.items() if effect.incapacitated
)


def effects_of(conditions: Iterable[Condition]) -> tuple[ConditionEffect, ...]:
    return tuple(EFFECTS[condition] for condition in conditions)


def is_incapacitated(conditions: Iterable[Condition]) -> bool:
    return any(effect.incapacitated for effect in effects_of(conditions))


def speed_is_zero(conditions: Iterable[Condition]) -> bool:
    return any(effect.speed_zero for effect in effects_of(conditions))
