"""Attack resolution over plain values.

Nothing here knows what a ``Creature`` is. The caller extracts the handful of
values an attack actually depends on — bonuses, AC, condition sets, distance — so
this module stays a pure function of its arguments and can be exercised without
building a combatant.

All provenance: SRD 5.2.1 (see NOTICE).
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


class RiderExpiry(StrEnum):
    """When an attack's on-hit condition rider ends on its own.

    Vocabulary only, the way :class:`AttackKind` is: turn boundaries belong to the
    encounter stepper, which is the layer that enforces these. ``NONE`` is the
    default — the condition lasts until something removes it, exactly as a
    condition set directly on a stat block does.
    """

    NONE = "none"
    START_OF_ATTACKER_NEXT_TURN = "start_of_attacker_next_turn"
    END_OF_TARGET_NEXT_TURN = "end_of_target_next_turn"


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

    Close-combat penalties are supplied through ``extra_disadvantage``. They turn
    on teams, positions, sight and incapacitation, which belong to the encounter
    model rather than this plain-value kernel.

    Frightened is treated as always applying. The rule conditions it on the source
    of fear being in line of sight; the engine tracks neither fear sources nor
    which source imposed a condition, so that qualifier cannot yet be evaluated.
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
    both word it identically in SRD 5.2.1: "Any attack roll that hits you is a
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
    """Everything that happened in one attack, ready to be narrated or applied.

    ``damage_dealt`` covers the main damage pool — the printed dice plus any
    Advantage rider, which shares its damage type — after the target's defenses
    against that type. ``bonus_damage_dealt`` is the secondary pool, defended
    against its own type. :attr:`total_damage_dealt` is what the target takes.
    """

    attack: AttackRoll
    advantage: Advantage
    damage: DiceRoll | None = None
    damage_dealt: int = 0
    out_of_range: bool = False
    #: The Advantage rider's roll, present only when the resolved state was
    #: Advantage and the attack landed. Its total is already inside
    #: ``damage_dealt`` — it is kept so a narrator can show the extra dice.
    advantage_damage: DiceRoll | None = None
    advantage_damage_reason: str = ""
    #: The secondary damage roll — a different type, on every hit.
    bonus_damage: DiceRoll | None = None
    bonus_damage_dealt: int = 0

    @property
    def hit(self) -> bool:
        return not self.out_of_range and self.attack.hit

    @property
    def critical(self) -> bool:
        return self.hit and self.attack.critical

    @property
    def total_damage_dealt(self) -> int:
        return self.damage_dealt + self.bonus_damage_dealt

    def describe(self) -> str:
        if self.out_of_range:
            return "out of range"
        text = self.attack.describe()
        if self.damage is not None:
            text += f"; damage {self.damage.describe()}"
            rolled = self.damage.total
            if self.advantage_damage is not None:
                reason = self.advantage_damage_reason or "advantage"
                text += f" plus {self.advantage_damage.describe()} for {reason}"
                rolled += self.advantage_damage.total
            if self.damage_dealt != rolled:
                text += f" -> {self.damage_dealt} after defenses"
        if self.bonus_damage is not None:
            text += f"; plus {self.bonus_damage.describe()}"
            if self.bonus_damage_dealt != self.bonus_damage.total:
                text += f" -> {self.bonus_damage_dealt} after defenses"
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
    advantage_bonus_damage: Dice | None = None,
    advantage_bonus_damage_applies: bool = False,
    bonus_damage: Dice | None = None,
    bonus_resisted: bool = False,
    bonus_vulnerable: bool = False,
    bonus_immune: bool = False,
) -> AttackResolution:
    """Roll an attack and, if it lands, its damage.

    Two riders extend the printed damage, and they defend differently because
    they type differently. ``advantage_bonus_damage`` is extra dice rolled only
    when ``advantage`` — the *resolved* state, after every source has combined
    and cancelled — is Advantage; it shares the main damage type, so the two
    rolls are summed **before** ``effective_damage`` halves or doubles, exactly
    as one damage instance. ``bonus_damage`` is a second pool of a different
    type, defended by the ``bonus_*`` flags on its own. Both riders double their
    dice on a critical hit, as any damage roll does.

    The roll order — main dice, Advantage rider, bonus — is load-bearing: the
    analytics replay this stream, so reordering it would desynchronise a batch
    from live play.
    """
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
    advantage_roll: DiceRoll | None = None
    if advantage_bonus_damage is not None and (
        advantage is Advantage.ADVANTAGE or advantage_bonus_damage_applies
    ):
        advantage_roll = roll_damage(advantage_bonus_damage, rng, critical=attack.critical)
    dealt = effective_damage(
        damage_roll.total + (advantage_roll.total if advantage_roll is not None else 0),
        resisted=resisted,
        vulnerable=vulnerable,
        immune=immune,
    )
    bonus_roll: DiceRoll | None = None
    bonus_dealt = 0
    if bonus_damage is not None:
        bonus_roll = roll_damage(bonus_damage, rng, critical=attack.critical)
        bonus_dealt = effective_damage(
            bonus_roll.total,
            resisted=bonus_resisted,
            vulnerable=bonus_vulnerable,
            immune=bonus_immune,
        )
    return AttackResolution(
        attack=attack,
        advantage=advantage,
        damage=damage_roll,
        damage_dealt=dealt,
        advantage_damage=advantage_roll,
        advantage_damage_reason=(
            "advantage" if advantage is Advantage.ADVANTAGE else "an adjacent ally"
        ) if advantage_roll is not None else "",
        bonus_damage=bonus_roll,
        bonus_damage_dealt=bonus_dealt,
    )
