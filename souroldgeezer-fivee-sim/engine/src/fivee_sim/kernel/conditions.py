"""Conditions expressed as data rather than scattered branches.

Every condition's combat-relevant consequences live in one table. Adding a
condition means adding a row, and the attack/save code reads the table instead of
growing another special case.

The table is **passed in**, not read from module state. :data:`EFFECTS` holds the
SRD conditions and is the default, but a content pack can define its own, so every
function that consults a condition takes the table it should consult. The reason is
the same one that makes every rolling function take an explicit ``Random``: state
read from ambient module scope is state a caller cannot control, and a fight that
silently consults a different table than the one it was built with is exactly the
drift this engine exists to prevent.

A condition is therefore identified by a plain ``str``. :class:`Condition` remains
as named constants for the SRD set — it is a ``StrEnum``, so its members *are*
strings and index the same table rows a pack's names do.

All provenance of :data:`EFFECTS`: SRD 5.2 (see NOTICE).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum


class UnknownCondition(KeyError):
    """A condition was referenced that the active table does not define."""


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


#: Every flag a condition may set. Content-pack validation reports this list when a
#: pack names a flag that does not exist, so it is derived rather than retyped.
EFFECT_FLAGS: tuple[str, ...] = tuple(ConditionEffect.__dataclass_fields__)

_NO_EFFECT = ConditionEffect()

# Keyed by ``str`` rather than by ``Condition`` so a pack's table and this one are
# the same type. The keys below are still enum members; ``StrEnum`` hashes by value,
# so ``EFFECTS["prone"]`` and ``EFFECTS[Condition.PRONE]`` are the same lookup.
EFFECTS: dict[str, ConditionEffect] = {
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

ConditionTable = Mapping[str, ConditionEffect]


def effect_of(condition: str, table: ConditionTable = EFFECTS) -> ConditionEffect:
    """The effects of one condition, or a report of what the table does define.

    Raising rather than defaulting to no effect is the point: a misspelled condition
    that quietly does nothing produces a fight that looks right and resolves wrongly.
    """
    try:
        return table[condition]
    except KeyError:
        available = ", ".join(sorted(table)) or "none"
        raise UnknownCondition(
            f"no condition named {condition!r}; the active content defines: {available}"
        ) from None


def effects_of(
    conditions: Iterable[str], table: ConditionTable = EFFECTS
) -> tuple[ConditionEffect, ...]:
    return tuple(effect_of(condition, table) for condition in conditions)


def is_incapacitated(conditions: Iterable[str], table: ConditionTable = EFFECTS) -> bool:
    return any(effect.incapacitated for effect in effects_of(conditions, table))


def speed_is_zero(conditions: Iterable[str], table: ConditionTable = EFFECTS) -> bool:
    return any(effect.speed_zero for effect in effects_of(conditions, table))
