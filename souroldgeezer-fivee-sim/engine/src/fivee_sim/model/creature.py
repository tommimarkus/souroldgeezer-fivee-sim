"""Creatures, their attack options, and the state a fight mutates.

Attack options carry an explicit bonus and damage expression rather than deriving
them from ability scores, proficiency, and class features. That is how SRD stat
blocks present attacks, so data stays a faithful transcription instead of relying
on derivation rules the engine would have to invent.

Positions are a single axis measured in feet. A one-dimensional battlefield is
enough for reach, ranged distance, and spell radii, and it keeps geometry
testable; it deliberately cannot express flanking or cover.

All provenance: SRD 5.2 (see NOTICE).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..kernel.actions import AttackKind
from ..kernel.conditions import EFFECTS, Condition
from ..kernel.dice import Dice
from ..kernel.rules import Ability, DamageType, ability_modifier

__all__ = ["AttackKind", "AttackOption", "Creature"]


@dataclass(frozen=True, slots=True)
class AttackOption:
    """One attack a creature can make, as printed on a stat block."""

    name: str
    attack_bonus: int
    damage: Dice
    damage_type: DamageType
    kind: AttackKind = AttackKind.MELEE
    reach: int = 5
    normal_range: int = 0
    long_range: int = 0
    provenance: str = "SRD 5.2"

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
    spells: tuple[str, ...] = ()
    spell_slots: dict[int, int] = field(default_factory=dict)
    spell_save_dc: int = 10
    spell_attack_bonus: int = 0
    conditions: set[Condition] = field(default_factory=set)
    concentrating_on: str | None = None
    resistances: frozenset[DamageType] = frozenset()
    immunities: frozenset[DamageType] = frozenset()
    vulnerabilities: frozenset[DamageType] = frozenset()
    position: int = 0
    death_save_successes: int = 0
    death_save_failures: int = 0
    stable: bool = False
    dead: bool = False
    provenance: str = "SRD 5.2"

    def __post_init__(self) -> None:
        if self.hp < 0:
            self.hp = self.max_hp

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
        return not any(EFFECTS[condition].incapacitated for condition in self.conditions)

    def resists(self, damage_type: DamageType) -> bool:
        if damage_type in self.resistances:
            return True
        return any(EFFECTS[condition].resists_all_damage for condition in self.conditions)

    def distance_to(self, other: Creature) -> int:
        return abs(self.position - other.position)

    # --- mutation ---------------------------------------------------------
    def add_condition(self, condition: Condition) -> None:
        self.conditions.add(condition)
        if EFFECTS[condition].incapacitated:
            self.concentrating_on = None

    def remove_condition(self, condition: Condition) -> None:
        self.conditions.discard(condition)

    def take_damage(self, amount: int) -> None:
        """Apply damage that has already been adjusted for resistance.

        Reaching 0 hit points knocks the creature out. Damage remaining after that
        kills outright if it equals or exceeds the creature's maximum hit points,
        which is what makes a big critical lethal rather than merely dropping.
        """
        if amount <= 0:
            return
        overflow = amount - self.hp
        self.hp = max(0, self.hp - amount)
        if self.hp == 0:
            if overflow >= self.max_hp:
                self.dead = True
                self.concentrating_on = None
                self.conditions.discard(Condition.UNCONSCIOUS)
                return
            self.stable = False
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
