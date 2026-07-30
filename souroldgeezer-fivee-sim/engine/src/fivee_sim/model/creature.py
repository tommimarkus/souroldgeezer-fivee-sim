"""Creatures, their attack options, and the state a fight mutates.

Attack options carry an explicit bonus and damage expression rather than deriving
them from ability scores, proficiency, and class features. That is how SRD stat
blocks present attacks, so data stays a faithful transcription instead of relying
on derivation rules the engine would have to invent.

Positions are points on a plane, measured in feet. A bare int is accepted
wherever a position goes and means feet along the x-axis — the original
one-dimensional battlefield is this plane's x-axis, and a scalar caller sees
identical numbers. Distance defaults to the SRD diagonal rule; the encounter
passes its own.

All provenance: SRD 5.2 (see NOTICE).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..kernel.actions import AttackKind, RiderExpiry
from ..kernel.conditions import (
    EFFECTS,
    Condition,
    ConditionEffect,
    ConditionTable,
    effect_of,
)
from ..kernel.dice import Dice
from ..kernel.grid import DiagonalRule, Point, as_point, distance_feet
from ..kernel.rules import Ability, DamageType, ability_modifier

__all__ = ["AttackKind", "AttackOption", "Creature", "RiderExpiry"]

#: Failures that kill. Duplicated from ``model.encounter``, which owns the death
#: save *roll* but cannot be imported here — it imports this module. Damage taken
#: at 0 hit points accrues a failure too, so the threshold is needed on both sides.
DEATH_SAVES_TO_DIE = 3


@dataclass(frozen=True, slots=True)
class AttackOption:
    """One attack a creature can make, as printed on a stat block.

    Stat blocks hang riders off a hit, and three shapes cover the printed forms:
    ``bonus_damage`` is a second pool of a different type on every hit (a claw
    that adds fire to its slashing); ``advantage_bonus_damage`` is extra dice of
    the main type only when the attack roll resolved with Advantage (the goblin
    pattern); ``on_hit_condition`` is a condition the hit imposes — automatic,
    or on a failed save when ``on_hit_save_ability`` and ``on_hit_save_dc`` are
    given — that ``on_hit_expiry`` may end on a turn boundary. The condition is
    a plain string on purpose: a pack-defined condition works here exactly as an
    SRD one does.
    """

    name: str
    attack_bonus: int
    damage: Dice
    damage_type: DamageType
    kind: AttackKind = AttackKind.MELEE
    reach: int = 5
    normal_range: int = 0
    long_range: int = 0
    bonus_damage: Dice | None = None
    bonus_damage_type: DamageType | None = None
    advantage_bonus_damage: Dice | None = None
    on_hit_condition: str | None = None
    on_hit_save_ability: Ability | None = None
    on_hit_save_dc: int = 0
    on_hit_expiry: RiderExpiry = RiderExpiry.NONE
    provenance: str = "SRD 5.2"

    def __post_init__(self) -> None:
        # Refused at construction rather than discovered mid-swing: without a
        # type the bonus pool cannot be defended, and without a DC a save cannot
        # be rolled. Content validation reports these first with a diagnostic;
        # this guards direct construction the same way.
        if self.bonus_damage is not None and self.bonus_damage_type is None:
            raise ValueError(
                f"{self.name}: bonus_damage needs bonus_damage_type — the extra "
                f"damage is defended against its own type"
            )
        if self.on_hit_save_ability is not None and self.on_hit_save_dc < 1:
            raise ValueError(
                f"{self.name}: on_hit_save_ability needs an on_hit_save_dc of 1 "
                f"or more"
            )

    def max_distance(self) -> int:
        if self.kind is AttackKind.MELEE:
            return self.reach
        return self.long_range or self.normal_range

    def has_long_range_penalty(self, distance: int) -> bool:
        if self.kind is AttackKind.MELEE:
            return False
        return self.normal_range > 0 and distance > self.normal_range


@dataclass(slots=True)
class Creature:
    """A combatant. Mutable: a fight is a sequence of changes to these fields."""

    name: str
    team: str
    ac: int
    max_hp: int
    speed: int = 30
    hp: int = -1
    abilities: dict[Ability, int] = field(default_factory=dict)
    save_bonuses: dict[Ability, int] = field(default_factory=dict)
    attacks: tuple[AttackOption, ...] = ()
    attacks_per_action: int = 1
    #: Pack Tactics, as a flag: the stat block prints it, the encounter resolves
    #: it, because whether a capable ally is within 5 feet of the target is a
    #: question about the whole fight, not about this creature.
    pack_tactics: bool = False
    #: Undead Fortitude, likewise: the drop-to-0 Constitution save that leaves
    #: the creature at 1 hit point. The encounter resolves it too — the save
    #: needs the fight's dice and the dropping damage's types, and this module
    #: rolls nothing.
    undead_fortitude: bool = False
    spells: tuple[str, ...] = ()
    spell_slots: dict[int, int] = field(default_factory=dict)
    spell_save_dc: int = 10
    spell_attack_bonus: int = 0
    conditions: set[str] = field(default_factory=set)
    concentrating_on: str | None = None
    #: Usable items, name to quantity held. Quantity *is* the charge count.
    items: dict[str, int] = field(default_factory=dict)
    #: The condition table this creature's conditions are read against. An
    #: ``Encounter`` overwrites this with its own so a fight cannot end up with
    #: combatants consulting different rules; standalone use gets the SRD set.
    condition_effects: ConditionTable = field(default_factory=lambda: EFFECTS)
    resistances: frozenset[DamageType] = frozenset()
    immunities: frozenset[DamageType] = frozenset()
    vulnerabilities: frozenset[DamageType] = frozenset()
    #: A point in feet; a scalar is accepted and widened to ``(x, 0)``.
    position: Point | int = (0, 0)
    death_save_successes: int = 0
    death_save_failures: int = 0
    stable: bool = False
    dead: bool = False
    provenance: str = "SRD 5.2"

    def __post_init__(self) -> None:
        if self.hp < 0:
            self.hp = self.max_hp
        self.position = as_point(self.position)

    # --- derived values ---------------------------------------------------
    def ability_score(self, ability: Ability) -> int:
        return self.abilities.get(ability, 10)

    def ability_mod(self, ability: Ability) -> int:
        return ability_modifier(self.ability_score(ability))

    def save_modifier(self, ability: Ability) -> int:
        """Explicit save bonus if the stat block prints one, else the raw modifier."""
        if ability in self.save_bonuses:
            return self.save_bonuses[ability]
        return self.ability_mod(ability)

    @property
    def conscious(self) -> bool:
        return not self.dead and self.hp > 0

    @property
    def dying(self) -> bool:
        return not self.dead and self.hp == 0 and not self.stable

    @property
    def active(self) -> bool:
        """Able to act: conscious and not held by an incapacitating condition."""
        if not self.conscious:
            return False
        return not any(self._effect(condition).incapacitated for condition in self.conditions)

    def resists(self, damage_type: DamageType) -> bool:
        if damage_type in self.resistances:
            return True
        return any(self._effect(condition).resists_all_damage for condition in self.conditions)

    def distance_to(
        self, other: Creature, rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE
    ) -> int:
        return distance_feet(as_point(self.position), as_point(other.position), rule)

    def _effect(self, condition: str) -> ConditionEffect:
        return effect_of(condition, self.condition_effects)

    # --- mutation ---------------------------------------------------------
    def add_condition(self, condition: str) -> None:
        # Look the effect up first: an unknown name must be refused before it is
        # recorded, or the creature carries a condition nothing can resolve.
        incapacitates = self._effect(condition).incapacitated
        self.conditions.add(condition)
        if incapacitates:
            self.concentrating_on = None

    def remove_condition(self, condition: str) -> None:
        self.conditions.discard(condition)

    def take_damage(self, amount: int, *, critical: bool = False) -> None:
        """Apply damage that has already been adjusted for resistance.

        Two different rules meet here and the difference is which side of 0 the
        creature started on.

        *Dropping* to 0 knocks the creature out and begins a fresh dying state, so
        its death saves start from nothing. Damage remaining after the drop kills
        outright if it equals or exceeds the creature's maximum hit points.

        Damage taken *while already* at 0 is the other rule: it costs a death
        saving throw failure, two if it came from a critical hit, and the third
        failure kills. It resets nothing — only regaining hit points or becoming
        stable does that — so the drop-to-0 reset must not run a second time.
        """
        if amount <= 0 or self.dead:
            return
        already_down = self.hp == 0
        overflow = amount - self.hp
        self.hp = max(0, self.hp - amount)
        if self.hp > 0:
            return
        if overflow >= self.max_hp:
            self.dead = True
            self.concentrating_on = None
            self.conditions.discard(Condition.UNCONSCIOUS)
            return
        self.stable = False
        if already_down:
            self.death_save_failures += 2 if critical else 1
            if self.death_save_failures >= DEATH_SAVES_TO_DIE:
                self.dead = True
                self.conditions.discard(Condition.UNCONSCIOUS)
            return
        self.death_save_successes = 0
        self.death_save_failures = 0
        self.concentrating_on = None
        self.add_condition(Condition.UNCONSCIOUS)
        self.add_condition(Condition.PRONE)

    def heal(self, amount: int) -> None:
        if self.dead or amount <= 0:
            return
        was_down = self.hp == 0
        self.hp = min(self.max_hp, self.hp + amount)
        if was_down and self.hp > 0:
            self.stable = False
            self.death_save_successes = 0
            self.death_save_failures = 0
            self.remove_condition(Condition.UNCONSCIOUS)
