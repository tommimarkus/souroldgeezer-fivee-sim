"""Usable items: the kernel resolver and the action that spends one.

The interesting cases are the boundaries — an item with none left, an item aimed at
someone out of reach, a healing potion used on a dying ally — because those are
where "consumes an action for nothing" would otherwise hide.
"""

from __future__ import annotations

from random import Random

import pytest

from fivee_sim.kernel.conditions import EFFECTS, Condition
from fivee_sim.kernel.dice import Dice
from fivee_sim.kernel.items import ItemEffect, ItemError, resolve_item_use
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.model.creature import AttackOption, Creature
from fivee_sim.model.encounter import Action, ActionKind, Encounter, EncounterError

POTION = ItemEffect(heal=Dice.parse("2d4+2"), provenance="test")
FIRE = ItemEffect(
    damage=Dice.parse("2d6"),
    damage_type=DamageType.FIRE,
    save_ability=Ability.DEXTERITY,
    save_dc=13,
    half_on_save=True,
    provenance="test",
)
TOXIN = ItemEffect(condition=Condition.POISONED, provenance="test")
WARD = ItemEffect(temp_hp=Dice.parse("2d4+2"), provenance="test")
ITEMS = {
    "Potion of Healing": POTION, "Alchemist's Fire": FIRE, "Vale Toxin": TOXIN,
    "Ward Tonic": WARD,
}


def fighter(**overrides: object) -> Creature:
    defaults: dict[str, object] = {
        "name": "Thora",
        "team": "heroes",
        "ac": 16,
        "max_hp": 30,
        "abilities": {Ability.DEXTERITY: 14, Ability.CONSTITUTION: 14},
        "attacks": (
            AttackOption(
                name="Longsword", attack_bonus=5,
                damage=Dice.parse("1d8+3"), damage_type=DamageType.SLASHING,
            ),
        ),
    }
    defaults.update(overrides)
    return Creature(**defaults)  # type: ignore[arg-type]


def dummy(**overrides: object) -> Creature:
    defaults: dict[str, object] = {
        "name": "Target", "team": "monsters", "ac": 12, "max_hp": 25,
        "abilities": {Ability.DEXTERITY: 10},
    }
    defaults.update(overrides)
    return Creature(**defaults)  # type: ignore[arg-type]


class TestItemEffect:
    def test_an_effect_that_does_nothing_is_refused(self) -> None:
        # A pack record with an empty "use" would otherwise register an item that
        # costs an action and has no consequence.
        with pytest.raises(
            ItemError, match="heal, deal damage, grant temporary hit points, or apply a condition"
        ):
            ItemEffect()

    def test_damage_without_a_type_is_refused(self) -> None:
        with pytest.raises(ItemError, match="damage type"):
            ItemEffect(damage=Dice.parse("1d4"))

    def test_a_saving_throw_needs_a_dc(self) -> None:
        with pytest.raises(ItemError, match="save_dc"):
            ItemEffect(
                damage=Dice.parse("1d4"),
                damage_type=DamageType.FIRE,
                save_ability=Ability.DEXTERITY,
            )

    def test_healing_defaults_to_the_user_and_damage_outward(self) -> None:
        assert POTION.targets_others is False
        assert FIRE.targets_others is True
        assert TOXIN.targets_others is True


class TestResolveItemUse:
    def test_healing_reports_the_rolled_amount(self) -> None:
        result = resolve_item_use(Random(4), POTION, item="Potion", target="Thora")
        assert result.heal_roll is not None
        assert result.healed == result.heal_roll.total
        assert result.damage_dealt == 0

    def test_a_failed_save_takes_full_damage_and_a_success_takes_half(self) -> None:
        failed = resolve_item_use(
            Random(4), FIRE, item="Fire", target="Target", save_modifier=-20
        )
        saved = resolve_item_use(
            Random(4), FIRE, item="Fire", target="Target", save_modifier=+20
        )
        assert failed.damage_roll is not None and saved.damage_roll is not None
        # Same seed, same damage roll: the difference is the save, not the dice.
        assert failed.damage_roll.total == saved.damage_roll.total
        assert failed.damage_dealt == failed.damage_roll.total
        assert saved.damage_dealt == failed.damage_roll.total // 2

    def test_no_save_offered_means_the_effect_simply_lands(self) -> None:
        # Treating "no save" as a successful save would halve damage applied in full.
        effect = ItemEffect(
            damage=Dice.parse("2d6"), damage_type=DamageType.ACID, provenance="test"
        )
        result = resolve_item_use(Random(1), effect, item="Vial", target="Target")
        assert result.save is None
        assert result.damage_roll is not None
        assert result.damage_dealt == result.damage_roll.total

    def test_a_condition_lands_only_when_the_save_fails(self) -> None:
        gated = ItemEffect(
            condition=Condition.POISONED,
            save_ability=Ability.CONSTITUTION,
            save_dc=13,
            provenance="test",
        )
        failed = resolve_item_use(
            Random(4), gated, item="Toxin", target="Target", save_modifier=-20
        )
        saved = resolve_item_use(
            Random(4), gated, item="Toxin", target="Target", save_modifier=+20
        )
        assert failed.condition_applied == Condition.POISONED
        assert saved.condition_applied is None

    def test_resistance_applies_after_the_save(self) -> None:
        full = resolve_item_use(
            Random(4), FIRE, item="Fire", target="Target", save_modifier=-20
        )
        resisted = resolve_item_use(
            Random(4), FIRE, item="Fire", target="Target", save_modifier=-20, resisted=True
        )
        assert resisted.damage_dealt == full.damage_dealt // 2

    def test_immunity_zeroes_the_damage_but_the_dice_are_still_rolled(self) -> None:
        immune = resolve_item_use(
            Random(4), FIRE, item="Fire", target="Target", save_modifier=-20, immune=True
        )
        assert immune.damage_dealt == 0
        assert immune.damage_roll is not None, "the roll must still happen"

    def test_a_temp_hp_grant_reports_the_rolled_amount_and_no_healing(self) -> None:
        shield = ItemEffect(temp_hp=Dice.parse("2d4+2"), provenance="test")
        result = resolve_item_use(Random(4), shield, item="Ward", target="Thora")
        assert result.temp_hp_roll is not None
        assert result.temp_hp_granted == result.temp_hp_roll.total
        assert result.healed == 0
        assert result.heal_roll is None


class TestUseItemAction:
    def build(self, *creatures: Creature, seed: int = 7) -> tuple[Encounter, Random]:
        rng = Random(seed)
        encounter = Encounter(
            list(creatures), rng, items=ITEMS, condition_effects=EFFECTS
        )
        return encounter, rng

    def turn_of(self, encounter: Encounter, rng: Random, name: str) -> None:
        for _ in range(len(encounter.order) * 2):
            if encounter.current_name == name:
                return
            encounter.advance(rng)
        raise AssertionError(f"{name} never got a turn")

    def test_drinking_a_potion_heals_and_spends_the_action(self) -> None:
        hero = fighter(hp=5, items={"Potion of Healing": 2})
        encounter, rng = self.build(hero, dummy(position=50))
        self.turn_of(encounter, rng, "Thora")
        encounter.act(Action(kind=ActionKind.USE_ITEM, item="Potion of Healing"), rng)
        assert hero.hp > 5
        assert hero.items["Potion of Healing"] == 1
        assert encounter.state()["turn_state"]["action_used"] is True

    def test_a_ward_tonic_grants_temp_hp_to_a_downed_ally_without_waking_them(
        self,
    ) -> None:
        # SRD 5.2.1, Temporary Hit Points: a grant "doesn't restore you to
        # consciousness" and "doesn't count as healing" — so the drinker stays
        # Unconscious and no "heal" event fires, only "grant_temp_hp".
        hero = fighter(hp=0)
        hero.conditions[Condition.UNCONSCIOUS] = 1
        ally = fighter(name="Ally", items={"Ward Tonic": 1})
        encounter, rng = self.build(ally, hero, dummy(position=50))
        self.turn_of(encounter, rng, "Ally")
        events = encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Ward Tonic", target="Thora"), rng
        )
        assert hero.temp_hp > 0
        assert hero.hp == 0
        assert Condition.UNCONSCIOUS in hero.conditions
        assert any(event.kind == "grant_temp_hp" for event in events)
        assert not any(event.kind == "heal" for event in events)

    def test_the_quantity_runs_out_and_the_next_use_is_refused(self) -> None:
        hero = fighter(hp=5, items={"Potion of Healing": 1})
        encounter, rng = self.build(hero, dummy(position=50))
        self.turn_of(encounter, rng, "Thora")
        encounter.act(Action(kind=ActionKind.USE_ITEM, item="Potion of Healing"), rng)
        assert hero.items["Potion of Healing"] == 0
        encounter.advance(rng)
        self.turn_of(encounter, rng, "Thora")
        with pytest.raises(EncounterError, match="no Potion of Healing left"):
            encounter.act(Action(kind=ActionKind.USE_ITEM, item="Potion of Healing"), rng)

    def test_an_item_not_carried_is_refused_and_says_what_is_carried(self) -> None:
        hero = fighter(items={"Potion of Healing": 1})
        encounter, rng = self.build(hero, dummy(position=50))
        self.turn_of(encounter, rng, "Thora")
        with pytest.raises(EncounterError, match="Potion of Healing"):
            encounter.act(Action(kind=ActionKind.USE_ITEM, item="Elixir of Wishes"), rng)

    def test_an_item_the_content_does_not_define_is_refused(self) -> None:
        # Carrying it is not the same as the engine knowing what it does.
        hero = fighter(items={"Unknown Draught": 1})
        encounter, rng = self.build(hero, dummy(position=50))
        self.turn_of(encounter, rng, "Thora")
        with pytest.raises(EncounterError, match="not defined by the loaded content"):
            encounter.act(Action(kind=ActionKind.USE_ITEM, item="Unknown Draught"), rng)

    def test_an_outward_item_needs_a_target(self) -> None:
        hero = fighter(items={"Alchemist's Fire": 1})
        encounter, rng = self.build(hero, dummy(position=50))
        self.turn_of(encounter, rng, "Thora")
        with pytest.raises(EncounterError, match="needs a target"):
            encounter.act(Action(kind=ActionKind.USE_ITEM, item="Alchemist's Fire"), rng)

    def test_using_an_item_on_someone_out_of_reach_is_refused(self) -> None:
        hero = fighter(items={"Potion of Healing": 1})
        ally = dummy(name="Ally", team="heroes", position=40)
        encounter, rng = self.build(hero, ally, dummy(position=80))
        self.turn_of(encounter, rng, "Thora")
        with pytest.raises(EncounterError, match="only be used on another creature"):
            encounter.act(
                Action(kind=ActionKind.USE_ITEM, item="Potion of Healing", target="Ally"),
                rng,
            )
        assert hero.items["Potion of Healing"] == 1, "a refused use must not be spent"

    def test_a_potion_revives_a_dying_ally_within_reach(self) -> None:
        hero = fighter(items={"Potion of Healing": 1})
        ally = dummy(name="Ally", team="heroes", position=5, hp=1)
        encounter, rng = self.build(hero, ally, dummy(position=80))
        ally.take_damage(1)
        assert ally.dying and Condition.UNCONSCIOUS in ally.conditions
        self.turn_of(encounter, rng, "Thora")
        encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Potion of Healing", target="Ally"), rng
        )
        assert ally.hp > 0
        assert Condition.UNCONSCIOUS not in ally.conditions

    def test_a_thrown_item_damages_and_can_drop_the_target(self) -> None:
        hero = fighter(items={"Alchemist's Fire": 1})
        target = dummy(position=5, max_hp=4, hp=4)
        encounter, rng = self.build(hero, target)
        self.turn_of(encounter, rng, "Thora")
        encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Alchemist's Fire", target="Target"), rng
        )
        assert target.hp < 4

    def test_a_condition_item_applies_its_condition(self) -> None:
        hero = fighter(items={"Vale Toxin": 1})
        target = dummy(position=5)
        encounter, rng = self.build(hero, target)
        self.turn_of(encounter, rng, "Thora")
        encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Vale Toxin", target="Target"), rng
        )
        assert Condition.POISONED in target.conditions

    def test_a_second_item_in_one_turn_is_refused(self) -> None:
        hero = fighter(hp=5, items={"Potion of Healing": 2})
        encounter, rng = self.build(hero, dummy(position=50))
        self.turn_of(encounter, rng, "Thora")
        encounter.act(Action(kind=ActionKind.USE_ITEM, item="Potion of Healing"), rng)
        with pytest.raises(EncounterError, match="already taken an action"):
            encounter.act(Action(kind=ActionKind.USE_ITEM, item="Potion of Healing"), rng)

    def test_items_appear_in_the_authoritative_state(self) -> None:
        hero = fighter(items={"Potion of Healing": 2})
        encounter, _rng = self.build(hero, dummy(position=50))
        combatant = next(
            entry for entry in encounter.state()["combatants"] if entry["name"] == "Thora"
        )
        assert combatant["items"] == {"Potion of Healing": 2}
