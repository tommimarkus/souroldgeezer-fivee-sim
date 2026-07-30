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
from fivee_sim.kernel.grid import CoverGrade, Square
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.model.battlemap import BattleMap, MapFeature
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
    position: int | tuple[int, int] = 0,
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
        assert encounter.creatures["Thora"].position == (60, 0)

    def test_a_grappled_creature_cannot_move(self) -> None:
        rng = Random(1)
        held = fighter("Held", position=0)
        held.add_condition(Condition.GRAPPLED)
        encounter = Encounter([held, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Held", rng)
        with pytest.raises(EncounterError, match="speed 0"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=20), rng)


class TestPlanarMovement:
    """Movement on the plane: two-dimensional destinations, diagonal rules."""

    def test_a_diagonal_move_costs_the_longer_axis_under_the_default_rule(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 15)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["origin"] == (0, 0)
        assert move.data["destination"] == (20, 15)
        assert move.data["cost"] == 20
        assert encounter.creatures["Thora"].position == (20, 15)
        assert encounter.state()["turn_state"]["movement_left"] == 10

    def test_the_5_10_5_rule_charges_every_second_diagonal_double(self) -> None:
        from fivee_sim.kernel.grid import DiagonalRule

        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=90)], rng,
            movement_rule=DiagonalRule.FIVE_TEN_FIVE,
        )
        advance_to(encounter, "Thora", rng)
        # (25, 25) is 25 + 12 = 37 ft under 5-10-5, past a 30 ft speed.
        with pytest.raises(EncounterError, match="movement"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(25, 25)), rng)
        # (20, 20) is 20 + 10 = 30 ft: exactly the speed.
        encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 20)), rng)
        assert encounter.state()["turn_state"]["movement_left"] == 0

    def test_state_reports_positions_as_x_y_pairs(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(position=(10, 20)), make_monster("Wolf", position=5)], rng
        )
        positions = {
            entry["name"]: entry["position"]
            for entry in encounter.state()["combatants"]
        }
        assert positions == {"Thora": [10, 20], "Wolf": [5, 0]}

    def test_waypoints_are_refused_without_a_battle_map(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=90)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="battle map"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0), path=((5, 0),)), rng
            )


def archer(name: str = "Sylvi", *, position: int | tuple[int, int] = 0,
           team: str = "party") -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=14,
        max_hp=20,
        attacks=(
            AttackOption(
                name="Shortbow",
                attack_bonus=5,
                damage=Dice(1, 6, 3),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.RANGED,
                normal_range=80,
                long_range=320,
                provenance=FIXTURE,
            ),
        ),
        position=position,
        provenance=FIXTURE,
    )


def strip(
    width: int,
    height: int = 1,
    *,
    terrain: dict[Square, str] | None = None,
    features: tuple[MapFeature, ...] = (),
) -> BattleMap:
    return BattleMap(
        name="test map",
        width=width,
        height=height,
        terrain=terrain or {},
        features={feature.name: feature for feature in features},
        provenance=FIXTURE,
    )


class TestMapMovement:
    def test_difficult_terrain_charges_double_for_every_entered_square(self) -> None:
        rng = Random(1)
        battle_map = strip(6, terrain={(2, 0): "difficult", (3, 0): "difficult"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(25, 0))], rng,
            battle_map=battle_map,
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(20, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 30  # 5 + 10 + 10 + 5
        assert move.data["squares"] == [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0]]
        assert encounter.state()["turn_state"]["movement_left"] == 0

    def test_a_move_the_terrain_makes_unaffordable_is_refused(self) -> None:
        rng = Random(1)
        battle_map = strip(7, terrain={(2, 0): "difficult", (3, 0): "difficult"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(30, 0))], rng,
            battle_map=battle_map,
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="needs 35 ft"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(25, 0)), rng)

    def test_a_wall_forces_the_route_around_it(self) -> None:
        rng = Random(1)
        battle_map = strip(4, 3, terrain={(1, 0): "wall", (1, 1): "wall"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 10))], rng,
            battle_map=battle_map,
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 20
        walked = {tuple(square) for square in move.data["squares"]}
        assert not walked & {(1, 0), (1, 1)}

    def test_a_walled_off_destination_is_refused(self) -> None:
        rng = Random(1)
        battle_map = strip(4, terrain={(1, 0): "wall"})
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            battle_map=battle_map,
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no route"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)

    def test_a_move_may_not_end_on_a_conscious_creature(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(10, 0))], rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="occupied by Wolf"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 0)), rng)

    def test_a_move_off_the_map_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(10, 0))], rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="off the 5x1 map"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(40, 0)), rng)

    def test_allies_can_be_crossed_but_not_stopped_on(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [
                fighter(),
                fighter("Ally", position=(10, 0)),
                make_monster("Wolf", position=(20, 0)),
            ],
            rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=(15, 0)), rng)
        move = next(event for event in events if event.kind == "move")
        assert [2, 0] in move.data["squares"]  # straight through the ally

    def test_an_enemy_blocks_the_only_route(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [
                fighter(),
                make_monster("Goblin Warrior", label="Goblin", position=(10, 0)),
                make_monster("Wolf", position=(20, 0)),
            ],
            rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no route"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(15, 0)), rng)

    def test_passing_through_reach_provokes_even_when_the_move_ends_clear(self) -> None:
        # The 1-D endpoint check never caught this: start and end both out of
        # reach, with the walk crossing the goblin's threat on the way.
        rng = Random(6)
        battle_map = strip(5, 2)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=(10, 5))
        encounter = Encounter([fighter(), goblin], rng, battle_map=battle_map)
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(20, 0)), FixedRandom(20)
        )
        assert "opportunity_attack" in kinds(events)

    def test_disengage_suppresses_the_pass_through_attack(self) -> None:
        rng = Random(6)
        battle_map = strip(5, 2)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=(10, 5))
        encounter = Encounter([fighter(), goblin], rng, battle_map=battle_map)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.DISENGAGE), rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(20, 0)), FixedRandom(20)
        )
        assert "opportunity_attack" not in kinds(events)

    def test_an_explicit_path_is_honoured_when_legal(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            battle_map=strip(5, 2),
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(
            Action(
                kind=ActionKind.MOVE,
                to_position=(10, 0),
                path=((5, 5), (10, 5), (10, 0)),
            ),
            rng,
        )
        move = next(event for event in events if event.kind == "move")
        assert move.data["squares"] == [[0, 0], [1, 1], [2, 1], [2, 0]]
        assert move.data["cost"] == 15

    def test_a_path_with_a_gap_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="not to an adjacent square"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0), path=((10, 0),)),
                rng,
            )

    def test_a_path_through_a_wall_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))], rng,
            battle_map=strip(4, terrain={(1, 0): "wall"}),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="impassable 'wall'"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0),
                       path=((5, 0), (10, 0))),
                rng,
            )

    def test_a_path_must_end_at_the_destination(self) -> None:
        rng = Random(1)
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(20, 0))], rng,
            battle_map=strip(5),
        )
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="ends at"):
            encounter.act(
                Action(kind=ActionKind.MOVE, to_position=(10, 0), path=((5, 0),)),
                rng,
            )


class TestMapPlacement:
    def test_starting_inside_a_wall_is_refused(self) -> None:
        with pytest.raises(EncounterError, match="impassable 'wall'"):
            Encounter(
                [fighter(position=(5, 0)), make_monster("Wolf", position=(15, 0))],
                Random(1),
                battle_map=strip(4, terrain={(1, 0): "wall"}),
            )

    def test_starting_off_the_map_is_refused(self) -> None:
        with pytest.raises(EncounterError, match="off the 4x1 map"):
            Encounter(
                [fighter(position=(25, 0)), make_monster("Wolf", position=(15, 0))],
                Random(1),
                battle_map=strip(4),
            )

    def test_two_combatants_may_not_share_a_square(self) -> None:
        with pytest.raises(EncounterError, match="both start in square"):
            Encounter(
                [fighter(position=(0, 0)), make_monster("Wolf", position=(2, 2))],
                Random(1),
                battle_map=strip(4),
            )

    def test_positions_snap_to_the_centre_of_their_square(self) -> None:
        encounter = Encounter(
            [fighter(position=(7, 3)), make_monster("Wolf", position=(15, 0))],
            Random(1),
            battle_map=strip(4),
        )
        assert encounter.creatures["Thora"].position == (5, 0)

    def test_a_map_naming_unknown_terrain_is_refused_with_the_loaded_kinds(
        self,
    ) -> None:
        with pytest.raises(EncounterError, match="vale-lava"):
            Encounter(
                [fighter(), make_monster("Wolf", position=(15, 0))],
                Random(1),
                battle_map=strip(4, terrain={(2, 0): "vale-lava"}),
            )

    def test_the_map_block_appears_in_state(self) -> None:
        door = MapFeature(name="crypt door", square=(1, 0))
        encounter = Encounter(
            [fighter(), make_monster("Wolf", position=(15, 0))],
            Random(1),
            battle_map=strip(4, features=(door,)),
        )
        block = encounter.state()["map"]
        assert block == {
            "name": "test map",
            "width": 4,
            "height": 1,
            "movement_rule": "5-5-5",
            "features": {
                "crypt door": {"square": [1, 0], "kind": "door", "open": False},
            },
        }

    def test_a_mapless_fight_reports_no_map(self) -> None:
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], Random(1))
        assert encounter.state()["map"] is None


class TestCoverChangesTheAttack:
    #: A full wall column with no gap: the only geometry that seals sight, since
    #: the corner rule sees past a lone pillar.
    WALL_COLUMN = {(2, 0): "wall", (2, 1): "wall", (2, 2): "wall"}

    def duel(self, terrain: dict[Square, str]) -> Encounter:
        rng = Random(3)
        encounter = Encounter(
            [archer(position=(0, 5)),
             make_monster("Goblin Warrior", label="Goblin", position=(20, 5))],
            rng,
            battle_map=strip(5, 3, terrain=terrain),
        )
        advance_to(encounter, "Sylvi", rng)
        return encounter

    def test_half_cover_turns_a_pinned_hit_into_a_miss(self) -> None:
        # Natural 11 + 5 = 16: a hit against AC 15 in the open, a miss against
        # 15 + 2 behind the half-cover pillar. Same roll, different fight.
        open_ground = self.duel({})
        events = open_ground.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(11)
        )
        assert events[0].data["hit"]
        assert events[0].data["cover"] == 0

        covered = self.duel({(2, 1): "half-cover"})
        events = covered.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(11)
        )
        assert not events[0].data["hit"]
        assert events[0].data["cover"] == 1
        assert "half cover, +2 AC" in events[0].detail

    def test_total_cover_refuses_without_consuming_the_attack(self) -> None:
        sealed = self.duel(self.WALL_COLUMN)
        before = sealed.state()["turn_state"]
        events = sealed.act(
            Action(kind=ActionKind.ATTACK, target="Goblin"), FixedRandom(20)
        )
        assert events[0].data["total_cover"]
        assert "total cover" in events[0].detail
        goblin = sealed.creatures["Goblin"]
        assert goblin.hp == goblin.max_hp
        assert sealed.state()["turn_state"] == before

    def test_cover_between_is_the_public_authority(self) -> None:
        assert self.duel({}).cover_between("Sylvi", "Goblin") is CoverGrade.NONE
        assert self.duel({(2, 1): "half-cover"}).cover_between(
            "Sylvi", "Goblin"
        ) is CoverGrade.HALF
        assert self.duel(self.WALL_COLUMN).cover_between(
            "Sylvi", "Goblin"
        ) is CoverGrade.TOTAL

    def test_an_intervening_creature_grants_half_cover(self) -> None:
        rng = Random(3)
        encounter = Encounter(
            [
                archer(position=(0, 5)),
                fighter("Ally", position=(10, 5)),
                make_monster("Goblin Warrior", label="Goblin", position=(20, 5)),
            ],
            rng,
            battle_map=strip(5, 3),
        )
        assert encounter.cover_between("Sylvi", "Goblin") is CoverGrade.HALF


class TestInteract:
    def corridor(self) -> tuple[Encounter, Random]:
        """A doorway in an otherwise solid wall: walls above and below, door in
        the middle row, archer on one side and goblin on the other."""
        rng = Random(3)
        door = MapFeature(name="door", square=(1, 1))
        encounter = Encounter(
            [archer(position=(0, 5)),
             make_monster("Goblin Warrior", label="Goblin", position=(15, 5))],
            rng,
            battle_map=strip(
                4, 3, terrain={(1, 0): "wall", (1, 2): "wall"}, features=(door,)
            ),
        )
        advance_to(encounter, "Sylvi", rng)
        return encounter, rng

    def test_a_closed_door_blocks_sight_and_passage_until_opened(self) -> None:
        encounter, rng = self.corridor()
        assert encounter.cover_between("Sylvi", "Goblin") is CoverGrade.TOTAL
        with pytest.raises(EncounterError, match="no route"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 5)), rng)

        events = encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert events[0].kind == "interact"
        assert events[0].data == {"feature": "door", "open": True}
        assert encounter.state()["map"]["features"]["door"]["open"] is True
        assert encounter.cover_between("Sylvi", "Goblin") is CoverGrade.NONE
        encounter.act(Action(kind=ActionKind.MOVE, to_position=(10, 5)), rng)
        assert encounter.creatures["Sylvi"].position == (10, 5)

    def test_interacting_is_free_but_only_once_per_turn(self) -> None:
        encounter, rng = self.corridor()
        encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert not encounter.state()["turn_state"]["action_used"]
        with pytest.raises(EncounterError, match="already interacted"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)

    def test_the_same_creature_can_close_it_again_next_turn(self) -> None:
        encounter, rng = self.corridor()
        encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        for _ in range(4):
            encounter.advance(rng)
            if encounter.current_name == "Sylvi":
                break
        events = encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)
        assert events[0].data == {"feature": "door", "open": False}

    def test_a_feature_out_of_reach_is_refused(self) -> None:
        rng = Random(3)
        door = MapFeature(name="far door", square=(3, 0))
        encounter = Encounter(
            [archer(), make_monster("Goblin Warrior", label="Goblin",
                                    position=(20, 0))],
            rng,
            battle_map=strip(5, features=(door,)),
        )
        advance_to(encounter, "Sylvi", rng)
        with pytest.raises(EncounterError, match="out of reach"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="far door"), rng)

    def test_an_unknown_feature_lists_what_the_map_has(self) -> None:
        encounter, rng = self.corridor()
        with pytest.raises(EncounterError, match="the map has: door"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="portcullis"), rng)

    def test_interacting_without_a_map_is_refused(self) -> None:
        rng = Random(1)
        encounter = Encounter([fighter(), make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="no battle map"):
            encounter.act(Action(kind=ActionKind.INTERACT, feature="door"), rng)


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

    def test_an_area_spell_cannot_be_dropped_beyond_its_range(self) -> None:
        # The point of origin is what the range applies to. This used to be checked
        # for no spell with a radius at all, so a 150 ft Fireball would land at any
        # distance whatsoever.
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=1000)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="beyond Fireball's 150 ft range"):
            encounter.act(
                Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=1000),
                Random(2),
            )

    def test_an_area_spell_named_at_a_target_out_of_range_is_refused(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=1000)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="beyond Fireball's 150 ft range"):
            encounter.act(
                Action(
                    kind=ActionKind.CAST,
                    spell="Fireball",
                    slot_level=3,
                    targets=("Goblin",),
                ),
                Random(2),
            )

    def test_a_creature_at_the_far_edge_of_a_blast_does_not_refuse_the_whole_spell(
        self,
    ) -> None:
        # The origin is in range; a creature caught 20 ft further out is not, and
        # must not veto a legal cast. This is why the range check is on the origin
        # rather than on each creature the radius sweeps up.
        rng = Random(4)
        wizard = caster(position=0)
        goblin = make_monster("Goblin Warrior", label="Goblin", position=160)
        encounter = Encounter([wizard, goblin], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=150),
            Random(2),
        )
        assert goblin.hp < goblin.max_hp

    def test_an_area_spell_may_be_centred_off_the_x_axis(self) -> None:
        rng = Random(4)
        wizard = caster(position=0)
        high = make_monster("Goblin Warrior", label="Goblin A", position=(100, 40))
        low = make_monster("Goblin Warrior", label="Goblin B", position=100)
        encounter = Encounter([wizard, high, low], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3,
                   center=(100, 40)),
            Random(2),
        )
        assert high.hp < high.max_hp
        assert low.hp == low.max_hp

    def test_an_area_spell_is_bounded_by_its_radius_not_by_max_targets(self) -> None:
        # Every bundled area spell leaves max_targets at its default of 1. Enforcing
        # that on an area would shrink a Fireball to a single creature.
        rng = Random(4)
        wizard = caster(position=0)
        goblins = [
            make_monster("Goblin Warrior", label=f"Goblin {letter}", position=100 + step)
            for step, letter in enumerate("ABC")
        ]
        encounter = Encounter([wizard, *goblins], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=101),
            Random(2),
        )
        assert all(goblin.hp < goblin.max_hp for goblin in goblins)

    def test_naming_more_targets_than_a_spell_allows_is_refused(self) -> None:
        # max_targets is a documented pack field. It used to be sliced with
        # max(cap, len(named)), which can never truncate, so it did nothing at all.
        rng = Random(4)
        priest = caster(position=0)
        priest.spells = ("Guiding Bolt",)
        priest.spell_slots = {1: 4}
        goblins = [
            make_monster("Goblin Warrior", label=f"Goblin {letter}", position=20 + step)
            for step, letter in enumerate("AB")
        ]
        encounter = Encounter([priest, *goblins], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        with pytest.raises(EncounterError, match="at most 1 creature"):
            encounter.act(
                Action(
                    kind=ActionKind.CAST,
                    spell="Guiding Bolt",
                    slot_level=1,
                    targets=("Goblin A", "Goblin B"),
                ),
                Random(2),
            )

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

    def test_attacking_after_casting_is_refused(self) -> None:
        # Casting spends the action, and starting an Attack action needs it. Only
        # attacks_left was checked here, so a caster could cast *and* swing on the
        # same turn — worth a fifth of a caster's measured damage per round.
        rng = Random(4)
        wizard = caster(position=0)
        wizard.attacks = (
            AttackOption(
                name="Dagger",
                attack_bonus=5,
                damage=Dice.parse("1d4+2"),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.MELEE,
            ),
        )
        # An Ogre, because a goblin dies to the Fireball and ends the fight before
        # the second action can be refused.
        ogre = make_monster("Ogre", label="Ogre", position=5)
        encounter = Encounter([wizard, ogre], rng, spellbook=spellbook())
        advance_to(encounter, "Wren", rng)
        encounter.act(
            Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=25),
            Random(2),
        )
        with pytest.raises(EncounterError, match="already taken an action"):
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Ogre", attack="Dagger"), rng
            )

    def test_a_multiattack_continues_after_its_first_swing_spends_the_action(
        self,
    ) -> None:
        # The mirror of the above: later swings of a Multiattack must still land,
        # which is why the check is "no attack taken yet" rather than "action used".
        rng = Random(1)
        brute = fighter(attacks_per_action=2)
        encounter = Encounter([brute, make_monster("Wolf", position=5)], rng)
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Wolf"), rng)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Wolf"), rng)
        assert encounter.state()["turn_state"]["attacks_left"] == 0

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
