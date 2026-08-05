"""Spell definitions and resolution over plain values.

Two ordering rules here are easy to get wrong and are pinned by tests.

Area spells roll their damage **once** and compare each creature's save against
that single total, rather than rolling per target. And a save-based spell rolls
damage before the saves, while an attack-roll spell rolls the attack first —
because a critical hit has to be known before its damage dice are doubled.

All provenance: SRD 5.2.1 (see NOTICE).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from random import Random

from .actions import AttackKind
from .dice import Advantage, Dice, DiceError, DiceRoll, roll_dice
from .items import ActionCost
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
    CONE = "cone"
    LINE = "line"
    CUBE = "cube"
    #: SRD 5.2.1 p.181: "extends in straight lines from a creature or an object
    #: in all directions"; its origin is excluded from the area unless its
    #: creator decides otherwise. This engine centres it on the caster and
    #: always excludes the caster — no bundled spell needs the "creator decides
    #: otherwise" opt-in, so there is no field for it yet.
    EMANATION = "emanation"
    #: SRD 5.2.1 p.180: "extends in straight lines from a point of origin
    #: located at the center of the circular top or bottom"; its origin *is*
    #: included. Declared by a base ``radius`` and a ``height`` (see ``Spell``)
    #: — the engine's areas are 2-D, so ``height`` is stored and required but
    #: never consulted by resolution.
    CYLINDER = "cylinder"


@dataclass(frozen=True, slots=True)
class Spell:
    """A spell as far as combat resolution is concerned.

    The area fields pair with the shape: a sphere has a ``radius``, a cone and a
    line have a ``length`` (and a line a ``width``, fixed at one square), a cube
    has a ``size``. An emanation reuses ``radius`` for the distance it extends —
    the same "how far from the origin" reading a sphere's radius already carries,
    so a second field would say nothing a sphere's doesn't already say. A cylinder
    also uses ``radius`` for its base and additionally carries ``height``, which
    resolution never consults (see ``SpellShape.CYLINDER``). Content validation
    enforces the pairing, so a loaded spell always carries the measurement its
    shape needs.
    """

    name: str
    level: int
    school: str = ""
    requires_attack_roll: bool = False
    #: Ranged by default for compatibility with packs written before attack spells
    #: declared their kind. Melee spell attacks opt in explicitly.
    attack_kind: AttackKind = AttackKind.RANGED
    save_ability: Ability | None = None
    damage: Dice | None = None
    damage_type: DamageType | None = None
    heal: Dice | None = None
    #: Default False because SRD 5.2.1 grants half damage per spell, in the spell's
    #: own text — Fireball says "half as much damage on a successful save", Sacred
    #: Flame says only "take 1d8 Radiant damage". So a record that omits this is a
    #: transcription of a spell with no such clause, and defaulting to True made
    #: every one of them quietly generous while the record still read correctly.
    half_on_save: bool = False
    #: Dice added per slot level above the spell's base level.
    upcast_damage: Dice | None = None
    upcast_heal: Dice | None = None
    #: Whether the caster's spellcasting ability modifier is added to the healing,
    #: as SRD 5.2.1 Cure Wounds and Healing Word both are. Opt-in and healing-only:
    #: this record is shared by everyone who knows the spell, so the modifier
    #: cannot live in ``heal`` — it arrives at resolution from the caster or not
    #: at all. A pack transcribing a flat number omits this and keeps that number.
    add_spellcasting_modifier: bool = False
    shape: SpellShape = SpellShape.SINGLE
    radius: int = 0
    length: int = 0
    size: int = 0
    width: int = 5
    #: A cylinder's height in feet. Required alongside ``radius`` for a
    #: cylinder (SRD 5.2.1 names both), but the engine's areas are 2-D and
    #: resolution never reads it — declared explicitly rather than silently
    #: ignored, per ``SpellShape.CYLINDER``.
    height: int = 0
    range_feet: int = 0
    max_targets: int = 1
    condition: str | None = None
    concentration: bool = False
    provenance: str = "SRD 5.2.1"
    #: Almost every spell is cast with an action; Healing Word and Mass Healing
    #: Word are SRD 5.2.1's "Casting Time: Bonus Action" exceptions. Mirrors
    #: ``ItemEffect.action_cost``, which solved the same problem for items first.
    action_cost: ActionCost = ActionCost.ACTION

    @property
    def is_area(self) -> bool:
        """Whether the spell affects an area rather than named creatures.

        A radius with no declared shape still reads as an area: that is the
        legacy encoding of a sphere, and every branch point asks this property
        rather than re-testing ``radius`` so the rule lives in one place.
        """
        return self.shape is not SpellShape.SINGLE or self.radius > 0

    @property
    def effective_shape(self) -> SpellShape:
        """The shape resolution should use, folding the legacy sphere in."""
        if self.shape is SpellShape.SINGLE and self.radius > 0:
            return SpellShape.SPHERE
        return self.shape

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

    def healing_at(self, slot_level: int) -> Dice | None:
        if self.heal is None:
            return None
        if self.upcast_heal is None or slot_level <= self.level:
            return self.heal
        extra_levels = slot_level - self.level
        return Dice(
            count=self.heal.count + self.upcast_heal.count * extra_levels,
            faces=self.heal.faces,
            modifier=self.heal.modifier,
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
    #: The attack-roll counterparts, per target for the same reason and for one
    #: stronger one: ``forced_critical`` is scoped by the distance from the caster
    #: to *this* creature, so it could not be a property of the cast even in
    #: principle. A spell attack is an attack roll — SRD 5.2.1 defines one as "a D20
    #: Test that represents making an attack with a weapon, an Unarmed Strike, or
    #: a spell" — so it carries whatever the d20 test carries.
    attack_advantage: Advantage = Advantage.NONE
    forced_critical: bool = False
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
    healed: int = 0

    @property
    def affected(self) -> bool:
        if self.attack is not None:
            return self.attack.hit
        if self.healed:
            return True
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
        if self.healed:
            parts.append(f"{self.healed} healing")
        if self.condition_applied is not None:
            parts.append(f"gains {self.condition_applied}")
        return f"{self.name}: " + "; ".join(parts) if parts else f"{self.name}: no effect"


@dataclass(frozen=True, slots=True)
class SpellResolution:
    spell: str
    slot_level: int
    damage_roll: DiceRoll | None = None
    healing_roll: DiceRoll | None = None
    results: tuple[SpellTargetResult, ...] = field(default_factory=tuple)
    concentration_started: bool = False


def resolve_spell(
    rng: Random,
    spell: Spell,
    *,
    slot_level: int,
    save_dc: int,
    spell_attack_bonus: int = 0,
    spellcasting_modifier: int = 0,
    targets: Sequence[SpellTarget],
    supplied: Sequence[int] | None = None,
) -> SpellResolution:
    """Resolve ``spell`` against ``targets`` using one slot of ``slot_level``.

    ``supplied`` carries faces a caller rolled themselves, and applies to the
    spell's attack roll. A spell that rolls an attack against *several* targets
    rolls a separate d20 for each, so one reported face cannot say which is
    which — that is refused rather than silently spread across all of them.

    ``spellcasting_modifier`` is the caster's own ability modifier, passed in
    like every other value a roll depends on. It reaches the healing only when
    the spell asked for it, and never the damage: SRD damage spells print their
    dice in full, so adding it there would be generous to every one of them.
    """
    if slot_level < spell.level:
        raise ValueError(
            f"{spell.name} is level {spell.level} and cannot be cast with a "
            f"level {slot_level} slot"
        )
    if supplied is not None and spell.requires_attack_roll and len(targets) > 1:
        raise DiceError(
            f"{spell.name} rolls a separate attack against each of "
            f"{len(targets)} targets; a reported face cannot say which roll it is. "
            "Cast at one target, or let the engine roll."
        )
    dice = spell.damage_at(slot_level)
    healing_dice = spell.healing_at(slot_level)
    if healing_dice is not None and spell.add_spellcasting_modifier:
        # Folded into the dice rather than added to the total, so the roll
        # describes itself the way the table reads it: "2d8+3".
        healing_dice = replace(
            healing_dice, modifier=healing_dice.modifier + spellcasting_modifier
        )

    if spell.requires_attack_roll:
        results: list[SpellTargetResult] = []
        for target in targets:
            attack = resolve_attack_roll(
                rng,
                attack_bonus=spell_attack_bonus,
                target_ac=target.ac,
                advantage=target.attack_advantage,
                forced_critical=target.forced_critical,
                supplied=supplied,
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
    healing_roll = roll_dice(healing_dice, rng) if healing_dice is not None else None
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
                healed=max(0, healing_roll.total) if healing_roll is not None else 0,
            )
        )
    return SpellResolution(
        spell=spell.name,
        slot_level=slot_level,
        damage_roll=damage_roll,
        healing_roll=healing_roll,
        results=tuple(results),
        concentration_started=spell.concentration,
    )
