"""Usable items resolved over plain values.

An item here is a *use with a known effect* — drink it, throw it, apply it — and
nothing more. It has no weight, no attunement, no charges separate from how many
you hold, and it never derives an attack bonus or an armour class. That boundary
is deliberate: the moment an item produces numbers a stat block would otherwise
print, the data stops being a transcription and starts being a derivation the
engine has to invent rules for.

The three effects an item may have are the three the rules engine already knows
how to apply: restore hit points, deal damage (optionally against a saving
throw), and impose a condition. A pack can combine them and name the result; it
cannot introduce a fourth kind.

Like the rest of ``kernel``, this module knows nothing about creatures. The
caller extracts the defensive values a use depends on and passes them in.

Ordering matches :func:`~fivee_sim.kernel.spells.resolve_spell`: damage is rolled
once, before the saving throw, so an area-style item cannot roll differently per
target and a critical is never in play.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from random import Random

from .dice import Advantage, Dice, DiceRoll, roll_dice
from .rules import Ability, D20Test, DamageType, effective_damage, make_d20_test


class ItemError(ValueError):
    """An item cannot be used as asked."""


class ActionCost(StrEnum):
    ACTION = "action"
    BONUS_ACTION = "bonus_action"


@dataclass(frozen=True, slots=True)
class ItemEffect:
    """What using one item does.

    Every field is optional, but an effect that does nothing at all is refused at
    construction: a pack record with an empty ``use`` is a mistake, and silently
    registering it would produce an item that consumes an action for no reason.
    """

    heal: Dice | None = None
    #: Temporary Hit Points granted on use, resolved once like ``heal``
    #: rather than routed through it — SRD 5.2.1, *Temporary Hit Points*:
    #: they are never Hit Points and receiving them is never healing, so a
    #: shared field would let the two collapse into one number. See
    #: ``Creature.grant_temp_hp`` for what that separation buys.
    temp_hp: Dice | None = None
    damage: Dice | None = None
    damage_type: DamageType | None = None
    save_ability: Ability | None = None
    save_dc: int = 0
    half_on_save: bool = True
    condition: str | None = None
    #: Free-text, shown in narration and in ``lookup_rule``.
    description: str = ""
    provenance: str = "SRD 5.2.1"
    action_cost: ActionCost = ActionCost.ACTION

    def __post_init__(self) -> None:
        if (
            self.heal is None and self.damage is None and self.condition is None
            and self.temp_hp is None
        ):
            raise ItemError(
                "an item use must heal, deal damage, grant temporary hit points, "
                "or apply a condition"
            )
        if self.damage is not None and self.damage_type is None:
            raise ItemError("an item that deals damage must name a damage type")
        if self.save_ability is not None and self.save_dc < 1:
            raise ItemError("an item offering a saving throw needs a save_dc of at least 1")

    @property
    def targets_others(self) -> bool:
        """Whether the effect is aimed at someone else by default.

        Healing and a Temporary Hit Points grant are aimed at the user unless a
        target is named; damage and conditions are aimed outward. This only
        chooses the *default* target — the caller may always name one.
        """
        return self.damage is not None or self.condition is not None


@dataclass(frozen=True, slots=True)
class ItemUseResolution:
    """Everything that happened in one use, ready to be narrated or applied."""

    item: str
    target: str
    heal_roll: DiceRoll | None = None
    temp_hp_roll: DiceRoll | None = None
    damage_roll: DiceRoll | None = None
    save: D20Test | None = None
    healed: int = 0
    temp_hp_granted: int = 0
    damage_dealt: int = 0
    condition_applied: str | None = None

    def describe(self) -> str:
        parts: list[str] = []
        if self.heal_roll is not None:
            parts.append(f"heals {self.heal_roll.describe()}")
        if self.temp_hp_roll is not None:
            parts.append(f"grants {self.temp_hp_roll.describe()} temp HP")
        if self.save is not None:
            saved = "saved" if self.save.success else "failed"
            parts.append(f"{self.save.describe()} -> {saved}")
        if self.damage_roll is not None:
            parts.append(f"damage {self.damage_roll.describe()}")
            if self.damage_dealt != self.damage_roll.total:
                parts.append(f"{self.damage_dealt} after defenses")
        if self.condition_applied is not None:
            parts.append(f"gains {self.condition_applied}")
        return f"{self.item} on {self.target}: " + ("; ".join(parts) or "no effect")


def resolve_item_use(
    rng: Random,
    effect: ItemEffect,
    *,
    item: str,
    target: str,
    save_modifier: int = 0,
    auto_fail_save: bool = False,
    save_advantage: Advantage = Advantage.NONE,
    resisted: bool = False,
    vulnerable: bool = False,
    immune: bool = False,
) -> ItemUseResolution:
    """Resolve one use of ``item`` against a single target.

    Healing is reported as a rolled amount; capping it at the target's maximum hit
    points is the model layer's job, since only it knows the target.
    """
    heal_roll = roll_dice(effect.heal, rng) if effect.heal is not None else None
    temp_hp_roll = (
        roll_dice(effect.temp_hp, rng) if effect.temp_hp is not None else None
    )
    damage_roll = roll_dice(effect.damage, rng) if effect.damage is not None else None

    save: D20Test | None = None
    if effect.save_ability is not None:
        save = make_d20_test(
            rng,
            modifier=save_modifier,
            dc=effect.save_dc,
            advantage=save_advantage,
            auto_fail=auto_fail_save,
        )

    # No saving throw offered means the effect simply lands. Treating that as a
    # successful save would halve damage the item deals in full.
    failed = not save.success if save is not None else True

    dealt = 0
    if damage_roll is not None:
        raw = damage_roll.total
        if not failed:
            raw = raw // 2 if effect.half_on_save else 0
        dealt = effective_damage(raw, resisted=resisted, vulnerable=vulnerable, immune=immune)

    return ItemUseResolution(
        item=item,
        target=target,
        heal_roll=heal_roll,
        temp_hp_roll=temp_hp_roll,
        damage_roll=damage_roll,
        save=save,
        healed=heal_roll.total if heal_roll is not None else 0,
        temp_hp_granted=temp_hp_roll.total if temp_hp_roll is not None else 0,
        damage_dealt=dealt,
        condition_applied=effect.condition if failed else None,
    )
