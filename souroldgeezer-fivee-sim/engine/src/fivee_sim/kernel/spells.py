"""Spell definitions and resolution over plain values.

Two ordering rules here are easy to get wrong and are pinned by tests.

Area spells roll their damage **once** and compare each creature's save against
that single total, rather than rolling per target. And a save-based spell rolls
damage before the saves, while an attack-roll spell rolls the attack first —
because a critical hit has to be known before its damage dice are doubled.

All provenance: SRD 5.2 (see NOTICE).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from random import Random

from .dice import Advantage, Dice, DiceRoll, roll_dice
from .rules import (
    Ability,
    AttackRoll,
    D20Test,
    DamageType,
    effective_damage,
    make_d20_test,
    resolve_attack_roll,
)


class SpellShape(StrEnum):
    SINGLE = "single"
    SPHERE = "sphere"


@dataclass(frozen=True, slots=True)
class Spell:
    """A spell as far as combat resolution is concerned."""

    name: str
    level: int
    school: str = ""
    requires_attack_roll: bool = False
    save_ability: Ability | None = None
    damage: Dice | None = None
    damage_type: DamageType | None = None
    half_on_save: bool = True
    #: Dice added per slot level above the spell's base level.
    upcast_damage: Dice | None = None
    shape: SpellShape = SpellShape.SINGLE
    radius: int = 0
    range_feet: int = 0
    max_targets: int = 1
    condition: str | None = None
    concentration: bool = False
    provenance: str = "SRD 5.2"

    def damage_at(self, slot_level: int) -> Dice | None:
        """Damage dice for a given slot, scaled for upcasting."""
        if self.damage is None:
            return None
        if self.upcast_damage is None or slot_level <= self.level:
            return self.damage
        extra_levels = slot_level - self.level
        return Dice(
            count=self.damage.count + self.upcast_damage.count * extra_levels,
            faces=self.damage.faces,
            modifier=self.damage.modifier,
        )


@dataclass(frozen=True, slots=True)
class SpellTarget:
    """The defensive values one creature brings to a spell."""

    name: str
    ac: int = 10
    save_modifier: int = 0
    auto_fail_save: bool = False
    #: Per target rather than per spell: one creature in a Fireball may be
    #: Restrained and the next Dodging, so a single spell-wide value cannot serve.
    save_advantage: Advantage = Advantage.NONE
    resisted: bool = False
    vulnerable: bool = False
    immune: bool = False


@dataclass(frozen=True, slots=True)
class SpellTargetResult:
    name: str
    save: D20Test | None = None
    attack: AttackRoll | None = None
    damage_dealt: int = 0
    condition_applied: str | None = None

    @property
    def affected(self) -> bool:
        if self.attack is not None:
            return self.attack.hit
        if self.save is not None:
            return not self.save.success
        return False

    def describe(self) -> str:
        parts: list[str] = []
        if self.attack is not None:
            parts.append(self.attack.describe())
        if self.save is not None:
            saved = "saved" if self.save.success else "failed"
            parts.append(f"{self.save.describe()} -> {saved}")
        if self.damage_dealt:
            parts.append(f"{self.damage_dealt} damage")
        if self.condition_applied is not None:
            parts.append(f"gains {self.condition_applied}")
        return f"{self.name}: " + "; ".join(parts) if parts else f"{self.name}: no effect"


@dataclass(frozen=True, slots=True)
class SpellResolution:
    spell: str
    slot_level: int
    damage_roll: DiceRoll | None = None
    results: tuple[SpellTargetResult, ...] = field(default_factory=tuple)
    concentration_started: bool = False


def resolve_spell(
    rng: Random,
    spell: Spell,
    *,
    slot_level: int,
    save_dc: int,
    spell_attack_bonus: int = 0,
    targets: Sequence[SpellTarget],
    advantage: Advantage = Advantage.NONE,
) -> SpellResolution:
    """Resolve ``spell`` against ``targets`` using one slot of ``slot_level``."""
    if slot_level < spell.level:
        raise ValueError(
            f"{spell.name} is level {spell.level} and cannot be cast with a "
            f"level {slot_level} slot"
        )
    dice = spell.damage_at(slot_level)

    if spell.requires_attack_roll:
        results: list[SpellTargetResult] = []
        for target in targets:
            attack = resolve_attack_roll(
                rng,
                attack_bonus=spell_attack_bonus,
                target_ac=target.ac,
                advantage=advantage,
            )
            dealt = 0
            if attack.hit and dice is not None:
                per_target_damage = roll_dice(dice, rng, critical=attack.critical)
                dealt = effective_damage(
                    per_target_damage.total,
                    resisted=target.resisted,
                    vulnerable=target.vulnerable,
                    immune=target.immune,
                )
            results.append(
                SpellTargetResult(
                    name=target.name,
                    attack=attack,
                    damage_dealt=dealt,
                    condition_applied=spell.condition if attack.hit else None,
                )
            )
        return SpellResolution(
            spell=spell.name,
            slot_level=slot_level,
            results=tuple(results),
            concentration_started=spell.concentration,
        )

    # Save-based: one damage roll shared by every creature in the area.
    damage_roll = roll_dice(dice, rng) if dice is not None else None
    results = []
    for target in targets:
        save: D20Test | None = None
        if spell.save_ability is not None:
            save = make_d20_test(
                rng,
                modifier=target.save_modifier,
                dc=save_dc,
                advantage=target.save_advantage,
                auto_fail=target.auto_fail_save,
            )
        # A spell offering neither a saving throw nor an attack roll simply lands;
        # treating "no save" as a successful save would halve damage that the rules
        # apply in full.
        failed = not save.success if save is not None else True
        dealt = 0
        if damage_roll is not None:
            raw = damage_roll.total
            if not failed:
                raw = raw // 2 if spell.half_on_save else 0
            dealt = effective_damage(
                raw,
                resisted=target.resisted,
                vulnerable=target.vulnerable,
                immune=target.immune,
            )
        results.append(
            SpellTargetResult(
                name=target.name,
                save=save,
                damage_dealt=dealt,
                condition_applied=spell.condition if failed else None,
            )
        )
    return SpellResolution(
        spell=spell.name,
        slot_level=slot_level,
        damage_roll=damage_roll,
        results=tuple(results),
        concentration_started=spell.concentration,
    )
