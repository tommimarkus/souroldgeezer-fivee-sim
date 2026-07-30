"""Attack resolution over plain values.

Nothing here knows what a ``Creature`` is. The caller extracts the handful of
values an attack actually depends on — bonuses, AC, condition sets, distance — so
this module stays a pure function of its arguments and can be exercised without
building a combatant.

All provenance: SRD 5.2 (see NOTICE).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from random import Random

from .conditions import EFFECTS, ConditionTable, effect_of
from .dice import Advantage, Dice, DiceRoll, resolve_advantage
from .rules import AttackRoll, effective_damage, resolve_attack_roll, roll_damage


class AttackKind(StrEnum):
    MELEE = "melee"
    RANGED = "ranged"


#: A melee attack from beyond this distance cannot reach without extra reach.
MELEE_THRESHOLD = 5


def compute_attack_advantage(
    *,
    attacker_conditions: Iterable[str],
    target_conditions: Iterable[str],
    distance: int,
    long_range_penalty: bool = False,
    extra_advantage: int = 0,
    extra_disadvantage: int = 0,
    condition_effects: ConditionTable = EFFECTS,
) -> Advantage:
    """Collect every source of Advantage and Disadvantage, then collapse them.

    Sources are counted rather than short-circuited because the 2024 rule is that
    any Advantage plus any Disadvantage yields neither — so both tallies have to be
    known before deciding.

    **The directional pair is scoped by distance, not by weapon**, and takes no
    :class:`AttackKind` for the same reason :func:`melee_hit_is_critical` does not.
    Prone words it exactly as Paralyzed and Unconscious word the automatic
    critical: "An attack roll against you has Advantage if the attacker is within 5
    feet of you. Otherwise, that attack roll has Disadvantage." That names a
    distance and no melee/ranged qualifier, so a crossbow shot — or a spell attack
    — from inside 5 feet takes the Advantage half, and a reach weapon swung from
    beyond it takes the Disadvantage half. An argument the rule does not consult is
    an invitation to reintroduce a check the rule never had.

    The engine models no offsetting penalty for shooting at close quarters. SRD
    5.2, "Ranged Attacks in Close Combat", gives Disadvantage to a ranged attack
    made "within 5 feet of an enemy who can see you and doesn't have the
    Incapacitated condition" — a rule this engine does not represent, because it
    turns on visibility and on who counts as an enemy of whom. It is unmodelled
    either way; gating Prone on the weapon was not an approximation of it, since it
    fired against Prone allies and never against a non-Prone target.

    Frightened is treated as always applying. The rule conditions it on the source
    of fear being in line of sight, which a one-dimensional battlefield with no
    visibility model cannot represent.
    """
    advantage_sources = extra_advantage
    disadvantage_sources = extra_disadvantage

    for condition in attacker_conditions:
        effect = effect_of(condition, condition_effects)
        if effect.own_attacks_have_advantage:
            advantage_sources += 1
        if effect.own_attacks_have_disadvantage:
            disadvantage_sources += 1

    within_5_feet = distance <= MELEE_THRESHOLD
    for condition in target_conditions:
        effect = effect_of(condition, condition_effects)
        if effect.attacked_with_advantage:
            advantage_sources += 1
        if effect.attacked_with_disadvantage:
            disadvantage_sources += 1
        # Flag names kept: they are pack-facing, and every pack that sets them would
        # break for no change in behaviour. See ``melee_hits_are_critical`` below,
        # where the identical call was made for the identical reason.
        if effect.attacked_with_advantage_in_melee and within_5_feet:
            advantage_sources += 1
        if effect.attacked_with_disadvantage_at_range and not within_5_feet:
            disadvantage_sources += 1

    if long_range_penalty:
        disadvantage_sources += 1

    return resolve_advantage(
        advantage_sources=advantage_sources,
        disadvantage_sources=disadvantage_sources,
    )


def melee_hit_is_critical(
    *,
    target_conditions: Iterable[str],
    distance: int,
    condition_effects: ConditionTable = EFFECTS,
) -> bool:
    """Whether a landed hit is upgraded to a critical by the target's condition.

    **The rule is scoped by distance, not by weapon.** Paralyzed and Unconscious
    both word it identically in SRD 5.2: "Any attack roll that hits you is a
    Critical Hit if the attacker is within 5 feet of you." That names a distance
    and no melee/ranged qualifier, so a ranged attack — or a spell attack — made
    from inside 5 feet qualifies exactly as a sword swing does. This function
    therefore takes no :class:`AttackKind`; an argument the rule does not consult
    is an invitation to reintroduce a check the rule never had.

    The flag it reads is still called ``melee_hits_are_critical``. That name is
    historical and pack-facing, so it stays: renaming it would break every content
    pack that sets it, for no change in behaviour.
    """
    if distance > MELEE_THRESHOLD:
        return False
    return any(
        effect_of(condition, condition_effects).melee_hits_are_critical
        for condition in target_conditions
    )


@dataclass(frozen=True, slots=True)
class AttackResolution:
    """Everything that happened in one attack, ready to be narrated or applied."""

    attack: AttackRoll
    advantage: Advantage
    damage: DiceRoll | None = None
    damage_dealt: int = 0
    out_of_range: bool = False

    @property
    def hit(self) -> bool:
        return not self.out_of_range and self.attack.hit

    @property
    def critical(self) -> bool:
        return self.hit and self.attack.critical

    def describe(self) -> str:
        if self.out_of_range:
            return "out of range"
        text = self.attack.describe()
        if self.damage is not None:
            text += f"; damage {self.damage.describe()}"
            if self.damage_dealt != self.damage.total:
                text += f" -> {self.damage_dealt} after defenses"
        return text


def resolve_attack(
    rng: Random,
    *,
    attack_bonus: int,
    target_ac: int,
    damage: Dice,
    advantage: Advantage = Advantage.NONE,
    forced_critical: bool = False,
    resisted: bool = False,
    vulnerable: bool = False,
    immune: bool = False,
) -> AttackResolution:
    """Roll an attack and, if it lands, its damage."""
    attack = resolve_attack_roll(
        rng,
        attack_bonus=attack_bonus,
        target_ac=target_ac,
        advantage=advantage,
        forced_critical=forced_critical,
    )
    if not attack.hit:
        return AttackResolution(attack=attack, advantage=advantage)

    damage_roll = roll_damage(damage, rng, critical=attack.critical)
    dealt = effective_damage(
        damage_roll.total,
        resisted=resisted,
        vulnerable=vulnerable,
        immune=immune,
    )
    return AttackResolution(
        attack=attack,
        advantage=advantage,
        damage=damage_roll,
        damage_dealt=dealt,
    )
