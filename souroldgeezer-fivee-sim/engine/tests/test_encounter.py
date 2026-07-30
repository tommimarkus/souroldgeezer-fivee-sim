"""Encounter tests: initiative, turns, damage, reactions, spell resources.

Because the generator is passed to each call rather than held by the encounter,
these tests build a fight with an ordinary seed and then resolve a specific action
with a forced generator. That is how a single attack's outcome gets pinned without
contriving the whole fight.
"""

from __future__ import annotations

from collections.abc import Sequence
from random import Random

import pytest

from fivee_sim.data import make_monster, spellbook
from fivee_sim.kernel.actions import AttackKind
from fivee_sim.kernel.conditions import Condition
from fivee_sim.kernel.dice import Dice
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.model.creature import AttackOption, Creature
from fivee_sim.model.encounter import (
    Action,
    ActionKind,
    Encounter,
    EncounterError,
    Event,
)

from .test_kernel import FixedRandom

FIXTURE = "synthetic test fixture, not SRD content"


def fighter(
    name: str = "Thora",
    *,
    position: int = 0,
    hp: int | None = None,
    max_hp: int = 30,
    team: str = "party",
    attacks_per_action: int = 1,
) -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=16,
        max_hp=max_hp,
        hp=max_hp if hp is None else hp,
        speed=30,
        abilities={
            Ability.STRENGTH: 16,
            Ability.DEXTERITY: 14,
            Ability.CONSTITUTION: 14,
            Ability.INTELLIGENCE: 10,
            Ability.WISDOM: 12,
            Ability.CHARISMA: 8,
        },
        attacks=(
            AttackOption(
                name="Longsword",
                attack_bonus=5,
                damage=Dice(1, 8, 3),
                damage_type=DamageType.SLASHING,
                kind=AttackKind.MELEE,
                provenance=FIXTURE,
            ),
        ),
        attacks_per_action=attacks_per_action,
        position=position,
        provenance=FIXTURE,
    )


def caster(name: str = "Wren", *, position: int = 0, team: str = "party") -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=13,
        max_hp=24,
        speed=30,
        abilities={
            Ability.CONSTITUTION: 14,
            Ability.DEXTERITY: 12,
            Ability.INTELLIGENCE: 16,
        },
        spells=("Fireball", "Hold Person"),
        spell_slots={2: 1, 3: 1},
        spell_save_dc=15,
        spell_attack_bonus=6,
        position=position,
        provenance=FIXTURE,
    )


def advance_to(encounter: Encounter, name: str, rng: Random, limit: int = 24) -> None:
    for _ in range(limit):
        if encounter.current_name == name:
            return
        encounter.advance(rng)
    raise AssertionError(f"{name} never got a turn")


def kinds(events: Sequence[Event]) -> list[str]:
    return [event.kind for event in events]


class TestInitiative:
    def test_the_same_seed_produces_the_same_order(self) -> None:
        first = Encounter([fighter(), make_monster("Wolf")], Random(7))
        second = Encounter([fighter(), make_monster("Wolf")], Random(7))
        assert first.order == second.order

    def test_ties_break_on_name_when_dexterity_matches(self) -> None:
        # A forced generator gives everyone the same d20, and identical Dexterity
        # leaves only the name to separate them — never randomness.
        encounter = Encounter(
            [fighter("Bravo", team="a"), fighter("Alpha", team="b")],
            FixedRandom(10),
        )
        assert encounter.order == ["Alpha", "Bravo"]

    def test_an_encounter_needs_two_combatants(self) -> None:
        with pytest.raises(EncounterError, match="at least two"):
            Encounter([fighter()], Random(1))

    def test_duplicate_names_are_refused(self) -> None:
        with pytest.raises(EncounterError, match="unique"):
            Encounter([fighter("Same"), fighter("Same", team="b")], Random(1))


class TestAttacking:
    def test_a_hit_reduces_hit_points(self) -> None:
        rng = Random(3)
        target = make_monster("Ogre", label="Ogre", position=5)
        encounter = Encounter([fighter(), target], rng)
        advance_to(encounter, "Thora", rng)
        before = target.hp
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20)
        )
        assert "attack" in kinds(events)
        assert target.hp < before

    def test_an_attack_beyond_reach_does_not_consume_the_attack(self) -> None:
        rng = Random(3)
        far = make_monster("Ogre", label="Ogre", position=60)
        encounter = Encounter([fighter(), far], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), rng)
        assert "cannot reach" in events[0].detail
        assert far.hp == far.max_hp

    def test_extra_attack_allows_a_second_swing_but_not_a_third(self) -> None:
        rng = Random(5)
        target = make_monster("Ogre", label="Ogre", position=5)
        encounter = Encounter([fighter(attacks_per_action=2), target], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20))
        encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20))
        with pytest.raises(EncounterError, match="no attacks left"):
            encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), FixedRandom(20))

    def test_dodging_imposes_disadvantage_on_incoming_attacks(self) -> None:
        rng = Random(2)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Goblin", rng)
        encounter.act(Action(kind=ActionKind.DODGE), rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.ATTACK, target="Goblin"), Random(4))
        assert "disadvantage" in events[0].detail

    def test_unknown_attack_name_is_reported_with_the_options(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="Longsword"):
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Wolf", attack="Halberd"), rng
            )


class TestGoingDown:
    def test_reaching_zero_knocks_a_creature_out_rather_than_killing_it(self) -> None:
        rng = Random(3)
        victim = fighter("Victim", team="foes", max_hp=40, hp=1, position=5)
        encounter = Encounter([fighter(), victim], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(20))
        assert victim.hp == 0
        assert not victim.dead
        assert victim.dying
        assert Condition.UNCONSCIOUS in victim.conditions
        assert Condition.PRONE in victim.conditions

    def test_damage_exceeding_maximum_hit_points_kills_outright(self) -> None:
        victim = fighter("Victim", team="foes", max_hp=4, hp=4)
        victim.take_damage(20)
        assert victim.dead
        assert not victim.dying

    def test_death_saves_are_rolled_at_the_start_of_a_dying_turn(self) -> None:
        # A third combatant keeps the fight alive. In a duel, dropping the only
        # opponent ends the encounter and advance() stops, so the dying creature
        # would never get the turn on which it would roll.
        rng = Random(8)
        victim = fighter("Victim", team="foes", max_hp=40, hp=1, position=5)
        ally = make_monster("Wolf", label="Wolf", team="foes", position=10)
        encounter = Encounter([fighter(), victim, ally], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Victim"), FixedRandom(20))
        assert victim.dying
        assert not encounter.over

        for _ in range(12):
            events = encounter.advance(rng)
            if encounter.current_name == "Victim":
                assert "death_save" in kinds(events)
                return
        raise AssertionError("the dying creature never took a turn")

    def test_healing_from_zero_clears_unconsciousness_and_resets_saves(self) -> None:
        victim = fighter("Victim", max_hp=20, hp=1)
        victim.take_damage(1)
        victim.death_save_failures = 2
        victim.heal(5)
        assert victim.hp == 5
        assert Condition.UNCONSCIOUS not in victim.conditions
        assert victim.death_save_failures == 0


class TestMovementAndReactions:
    def test_leaving_a_threatened_space_draws_an_opportunity_attack(self) -> None:
        rng = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" in kinds(events)

    def test_disengaging_first_prevents_the_opportunity_attack(self) -> None:
        rng = Random(6)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([fighter(), goblin], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DISENGAGE), rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), FixedRandom(20)
        )
        assert "opportunity_attack" not in kinds(events)

    def test_moving_further_than_the_remaining_speed_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="movement"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=80), rng)

    def test_dash_buys_a_second_helping_of_movement(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DASH), rng)
        encounter.act(Action(kind=ActionKind.MOVE, to_position=60), rng)
        assert encounter.creatures["Thora"].position == 60

    def test_a_grappled_creature_cannot_move(self) -> None:
        rng = Random(1)
        held = fighter("Held", position=0)
        held.add_condition(Condition.GRAPPLED)
        encounter = Encounter([held, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Held", rng)
        with pytest.raises(EncounterError, match="speed 0"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=20), rng)


class TestSpellcasting:
    def test_casting_spends_a_slot_of_the_chosen_level(self) -> None:
        rng = Random(4)
        wizard = caster()
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
            Random(9),
        )
        assert wizard.spell_slots[3] == 0

    def test_casting_without_a_slot_is_refused(self) -> None:
        rng = Random(4)
        wizard = caster()
        wizard.spell_slots[3] = 0
        goblin = make_monster("Goblin Warrior", label="Goblin", position=30)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="no level 3 slots"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=30),
                rng,
            )

    def test_an_area_spell_catches_everyone_inside_its_radius(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        near = make_monster("Goblin Warrior", label="Goblin A", position=100)
        also_near = make_monster("Goblin Warrior", label="Goblin B", position=110)
        far = make_monster("Goblin Warrior", label="Goblin C", position=300)
        encounter = Encounter([wizard, near, also_near, far], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=105),
            Random(2),
        )
        assert near.hp < near.max_hp
        assert also_near.hp < also_near.max_hp
        assert far.hp == far.max_hp

    def test_casting_an_unprepared_spell_is_refused(self) -> None:
        rng = Random(4)
        wizard = caster()
        encounter = Encounter(
            [wizard, make_monster("Wolf", position=20)], rng, spellbook=spellbook()
        )
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="does not have"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Shatter", targets=("Wolf",)), rng
            )

    def test_damage_forces_a_concentration_check(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        wizard.concentrating_on = "Hold Person"
        goblin = make_monster("Goblin Warrior", label="Goblin", position=5)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Goblin", rng)
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Wren", attack="Scimitar"),
            FixedRandom(20),
        )
        assert "concentration" in kinds(events)

    def test_being_knocked_out_ends_concentration(self) -> None:
        wizard = caster()
        wizard.concentrating_on = "Hold Person"
        wizard.take_damage(wizard.hp)
        assert wizard.concentrating_on is None


class TestTurnLegality:
    def test_an_incapacitated_creature_cannot_act(self) -> None:
        rng = Random(1)
        held = fighter("Held")
        held.add_condition(Condition.PARALYZED)
        encounter = Encounter([held, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Held", rng)
        with pytest.raises(EncounterError, match="incapacitated"):
            encounter.act(Action(kind=ActionKind.ATTACK, target="Wolf"), rng)

    def test_two_actions_in_one_turn_are_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DODGE), rng)
        with pytest.raises(EncounterError, match="already taken an action"):
            encounter.act(Action(kind=ActionKind.DASH), rng)

    def test_state_reports_the_authoritative_view(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        state = encounter.state()
        assert state["round"] == 1
        assert set(state["order"]) == {"Thora", "Wolf"}
        assert {entry["name"] for entry in state["combatants"]} == {"Thora", "Wolf"}
        assert all("hp" in entry and "conditions" in entry for entry in state["combatants"])
