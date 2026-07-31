"""Regression coverage for the rules gaps exposed by The Drowned Mill playtest.

These are deliberately engine-level fixtures.  Campaign records exercise the same
public fields through content validation in ``test_content``; this file pins the
authoritative encounter state so analytics cannot implement a different answer.
"""

from __future__ import annotations

from fivee_sim.analytics.montecarlo import auto_action, simulate_rounds
from fivee_sim.kernel.actions import AttackKind
from fivee_sim.kernel.dice import Dice
from fivee_sim.kernel.grid import TERRAIN, MovementMode, TerrainEffect
from fivee_sim.kernel.items import ActionCost, ItemEffect
from fivee_sim.kernel.rules import DamageType
from fivee_sim.kernel.spells import Spell
from fivee_sim.map_document import as_payload, parse_document, to_grid
from fivee_sim.model.battlemap import BattleMap, MapPlane
from fivee_sim.model.creature import AttackOption, Creature, DeathRule
from fivee_sim.model.encounter import Action, ActionKind, Encounter
from fivee_sim.service.uvtt import to_uvtt

from .conftest import FIXTURE, FixedRandom, advance_to, fighter


def _mapped_encounter(
    combatants: list[Creature],
    *,
    terrain: dict[tuple[int, int], str] | None = None,
    levels: dict[int, MapPlane] | None = None,
) -> Encounter:
    battle_map = (
        BattleMap(name="fixture", width=8, height=4, levels=levels, provenance=FIXTURE)
        if levels is not None
        else BattleMap.flat(
            name="fixture",
            width=8,
            height=4,
            terrain=terrain,
            provenance=FIXTURE,
        )
    )
    return Encounter(
        combatants,
        FixedRandom(10),
        battle_map=battle_map,
        terrain_effects={
            "normal": TerrainEffect(),
            "water": TerrainEffect(move_cost_multiplier=2, underwater=True),
            "grain": TerrainEffect(move_cost_multiplier=2),
        },
    )


class TestUnderwaterCombat:
    def test_non_piercing_melee_attack_has_disadvantage_underwater(self) -> None:
        attacker = fighter("Harrow", position=(2, 2))
        target = fighter("Marauder", team="monsters", position=(7, 2))
        target.darkvision = 60
        encounter = _mapped_encounter(
            [attacker, target], terrain={(0, 0): "water", (1, 0): "water"}
        )
        advance_to(encounter, "Harrow", FixedRandom(10))

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Marauder", attack="Longsword"),
            FixedRandom(10),
        )

        attack = next(event for event in events if event.kind == "attack")
        assert attack.data["advantage"] == "disadvantage"
        assert attack.data["underwater"] is True

    def test_piercing_melee_attack_is_not_penalised_underwater(self) -> None:
        attacker = fighter("Harrow", position=(2, 2))
        attacker.attacks = (
            AttackOption(
                name="Spear",
                attack_bonus=5,
                damage=Dice(1, 6, 3),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.MELEE,
                provenance=FIXTURE,
            ),
        )
        target = fighter("Marauder", team="monsters", position=(7, 2))
        encounter = _mapped_encounter(
            [attacker, target], terrain={(0, 0): "water", (1, 0): "water"}
        )
        advance_to(encounter, "Harrow", FixedRandom(10))

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Marauder", attack="Spear"),
            FixedRandom(10),
        )

        attack = next(event for event in events if event.kind == "attack")
        assert attack.data["advantage"] == "none"

    def test_fire_damage_is_resisted_by_an_underwater_target(self) -> None:
        attacker = fighter("Harrow", position=(2, 2))
        attacker.attacks = (
            AttackOption(
                name="Flame Blade",
                attack_bonus=20,
                damage=Dice(1, 6),
                damage_type=DamageType.FIRE,
                provenance=FIXTURE,
            ),
        )
        target = fighter("Marauder", team="monsters", position=(7, 2))
        encounter = _mapped_encounter(
            [attacker, target], terrain={(0, 0): "water", (1, 0): "water"}
        )
        advance_to(encounter, "Harrow", FixedRandom(10))

        encounter.act(
            Action(kind=ActionKind.ATTACK, target="Marauder", attack="Flame Blade"),
            FixedRandom(6),
        )

        assert target.hp == target.max_hp - 3


class TestMovementModes:
    def test_a_swim_speed_uses_ordinary_cost_in_underwater_terrain(self) -> None:
        swimmer = fighter("Ooloth", team="monsters", position=(2, 2))
        swimmer.swim_speed = 30
        target = fighter("Harrow", position=(32, 7))
        encounter = _mapped_encounter(
            [swimmer, target],
            terrain={(x, 0): "water" for x in range(6)},
        )
        advance_to(encounter, "Ooloth", FixedRandom(10))

        events = encounter.act(
            Action(
                kind=ActionKind.MOVE,
                to_position=(27, 2),
                movement_mode=MovementMode.SWIM,
            ),
            FixedRandom(10),
        )

        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 25
        assert move.data["movement_mode"] == "swim"

    def test_a_terrain_override_ignores_the_grain_multiplier(self) -> None:
        centipede = fighter("Centipede", team="monsters", position=(2, 2))
        centipede.terrain_cost_overrides = frozenset({"grain"})
        target = fighter("Harrow", position=(32, 7))
        encounter = _mapped_encounter(
            [centipede, target],
            terrain={(x, 0): "grain" for x in range(6)},
        )
        advance_to(encounter, "Centipede", FixedRandom(10))

        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=(27, 2)), FixedRandom(10)
        )

        move = next(event for event in events if event.kind == "move")
        assert move.data["cost"] == 25

    def test_flight_can_change_level_without_using_a_connector(self) -> None:
        stirge = fighter("Stirge", team="monsters", position=(2, 2))
        stirge.level = 1
        stirge.fly_speed = 40
        target = fighter("Harrow", position=(17, 7))
        levels = {
            0: MapPlane(default_elevation=0),
            1: MapPlane(default_elevation=10),
        }
        encounter = _mapped_encounter([stirge, target], levels=levels)
        advance_to(encounter, "Stirge", FixedRandom(10))

        events = encounter.act(
            Action(
                kind=ActionKind.MOVE,
                to_position=(12, 2),
                to_level=0,
                movement_mode=MovementMode.FLY,
            ),
            FixedRandom(10),
        )

        move = next(event for event in events if event.kind == "move")
        assert stirge.level == 0
        assert move.data["cost"] == 10
        assert move.data["movement_mode"] == "fly"


class TestOpeningsAndLifeCycle:
    def test_an_authored_sight_link_allows_a_cross_level_ranged_attack(self) -> None:
        archer = fighter("Whip", team="monsters", position=(2, 2))
        archer.level = 1
        archer.attacks = (
            AttackOption(
                name="Shortbow",
                attack_bonus=20,
                damage=Dice(1, 6),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.RANGED,
                normal_range=80,
                long_range=320,
                provenance=FIXTURE,
            ),
        )
        target = fighter("Harrow", position=(17, 2))
        levels = {
            0: MapPlane(),
            1: MapPlane(sight_links={(0, 0): frozenset({0})}),
        }
        encounter = _mapped_encounter([archer, target], levels=levels)
        advance_to(encounter, "Whip", FixedRandom(10))

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Harrow", attack="Shortbow"),
            FixedRandom(10),
        )

        attack = next(event for event in events if event.kind == "attack")
        assert attack.data.get("total_cover") is not True
        assert target.hp < target.max_hp

    def test_a_monster_dies_at_zero_without_rolling_death_saves(self) -> None:
        attacker = fighter("Harrow")
        attacker.attacks = (
            AttackOption(
                name="Hammer",
                attack_bonus=20,
                damage=Dice(1, 6, 20),
                damage_type=DamageType.BLUDGEONING,
                provenance=FIXTURE,
            ),
        )
        monster = Creature(
            name="Goblin",
            team="monsters",
            ac=10,
            max_hp=7,
            death_rule=DeathRule.INSTANT,
            position=5,
            provenance=FIXTURE,
        )
        rng = FixedRandom(10)
        encounter = Encounter([attacker, monster], rng)
        advance_to(encounter, "Harrow", rng)

        encounter.act(
            Action(kind=ActionKind.ATTACK, target="Goblin", attack="Hammer"), rng
        )

        assert monster.dead is True
        assert monster.dying is False
        assert not any(event.kind == "death_save" for event in encounter.log)


class TestAttachment:
    def test_an_attached_attack_deals_damage_at_the_start_of_the_sources_turn(self) -> None:
        proboscis = AttackOption(
            name="Proboscis",
            attack_bonus=20,
            damage=Dice(1, 1),
            damage_type=DamageType.PIERCING,
            on_hit_attach=True,
            attached_damage=Dice(2, 4),
            attached_damage_type=DamageType.NECROTIC,
            detach_after_damage=10,
            provenance=FIXTURE,
        )
        stirge = Creature(
            name="Stirge",
            team="monsters",
            ac=14,
            max_hp=2,
            attacks=(proboscis,),
            position=0,
            provenance=FIXTURE,
        )
        target = fighter("Harrow", position=5)
        rng = FixedRandom(10)
        encounter = Encounter([stirge, target], rng)
        advance_to(encounter, "Stirge", rng)
        encounter.act(
            Action(kind=ActionKind.ATTACK, target="Harrow", attack="Proboscis"), rng
        )
        hp_after_hit = target.hp
        encounter.advance(rng)
        encounter.advance(rng)

        assert target.hp == hp_after_hit - 8
        drains = [event for event in encounter.log if event.kind == "attached_damage"]
        assert drains[-1].data["damage"] == 8
        assert drains[-1].actor == "Stirge"
        assert drains[-1].target == "Harrow"


class TestHealingAndActionEconomy:
    def test_a_healing_spell_restores_hit_points_and_spends_its_slot(self) -> None:
        cleric = fighter("Wren")
        cleric.spells = ("Cure Wounds",)
        cleric.spell_slots = {1: 1}
        ally = fighter("Harrow", position=5, hp=0)
        enemy = fighter("Marauder", team="monsters", position=30)
        spell = Spell(
            name="Cure Wounds",
            level=1,
            heal=Dice(2, 8, 3),
            range_feet=5,
            provenance=FIXTURE,
        )
        rng = FixedRandom(4)
        encounter = Encounter([cleric, ally, enemy], rng, spellbook={spell.name: spell})
        advance_to(encounter, "Wren", rng)

        events = encounter.act(
            Action(
                kind=ActionKind.CAST,
                spell="Cure Wounds",
                slot_level=1,
                target="Harrow",
            ),
            rng,
        )

        assert ally.hp == 11
        assert ally.conscious
        assert cleric.spell_slots == {1: 0}
        assert next(event for event in events if event.kind == "heal").data["amount"] == 11

    def test_a_bonus_action_heal_leaves_the_action_available(self) -> None:
        fighter_with_wind = fighter("Harrow", hp=4)
        fighter_with_wind.items = {"Second Wind": 1}
        enemy = fighter("Goblin", team="monsters", position=5)
        wind = ItemEffect(
            heal=Dice(1, 10, 1),
            action_cost=ActionCost.BONUS_ACTION,
            provenance=FIXTURE,
        )
        rng = FixedRandom(5)
        encounter = Encounter(
            [fighter_with_wind, enemy], rng, items={"Second Wind": wind}
        )
        advance_to(encounter, "Harrow", rng)

        encounter.act(
            Action(
                kind=ActionKind.USE_ITEM,
                item="Second Wind",
                as_bonus_action=True,
            ),
            rng,
        )
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Goblin", attack="Longsword"), rng
        )

        assert fighter_with_wind.hp == 10
        assert any(event.kind == "attack" for event in events)

    def test_batch_policy_revives_an_ally_before_choosing_damage(self) -> None:
        cleric = fighter("Wren")
        cleric.spells = ("Cure Wounds",)
        cleric.spell_slots = {1: 1}
        ally = fighter("Harrow", position=5, hp=0)
        enemy = fighter("Marauder", team="monsters", position=10)
        spell = Spell(
            name="Cure Wounds",
            level=1,
            heal=Dice(2, 8, 3),
            range_feet=5,
            provenance=FIXTURE,
        )
        rng = FixedRandom(10)
        encounter = Encounter(
            [cleric, ally, enemy], rng, spellbook={spell.name: spell}
        )
        advance_to(encounter, "Wren", rng)

        action = auto_action(encounter)

        assert action is not None
        assert action.kind is ActionKind.CAST
        assert action.spell == "Cure Wounds"
        assert action.target == "Harrow"

    def test_an_authored_bonus_action_disengage_leaves_the_action_available(self) -> None:
        rogue = fighter("Tansy")
        rogue.bonus_actions = frozenset({"dash", "disengage"})
        enemy = fighter("Goblin", team="monsters", position=5)
        rng = FixedRandom(10)
        encounter = Encounter([rogue, enemy], rng)
        advance_to(encounter, "Tansy", rng)

        encounter.act(
            Action(kind=ActionKind.DISENGAGE, as_bonus_action=True), rng
        )
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Goblin", attack="Longsword"),
            rng,
        )

        assert encounter.state()["turn_state"] == {
            "movement_left": 30,
            "action_used": True,
            "attacks_left": 0,
            "interaction_used": False,
            "bonus_action_used": True,
        }
        assert any(event.kind == "attack" for event in events)


class TestMorale:
    def test_the_last_authored_holdout_surrenders_and_ends_the_encounter(self) -> None:
        whip = fighter("Whip", team="monsters", hp=5)
        whip.surrender_when_last = True
        fallen = fighter("Goblin", team="monsters", hp=0)
        fallen.death_rule = DeathRule.INSTANT
        fallen.dead = True
        party = fighter("Harrow", position=5)
        rng = FixedRandom(10)
        encounter = Encounter([whip, fallen, party], rng)
        advance_to(encounter, "Whip", rng)

        action = auto_action(encounter)

        assert action is not None
        assert action.kind is ActionKind.SURRENDER
        events = encounter.act(action, rng)
        assert [event.kind for event in events] == ["surrender"]
        assert encounter.over is True
        assert encounter.winner == "party"
        state = next(
            creature
            for creature in encounter.state()["combatants"]
            if creature["name"] == "Whip"
        )
        assert state["surrendered"] is True


class TestDistributionEvidence:
    def test_batch_reports_team_hp_casualty_and_resource_distributions(self) -> None:
        def combatants() -> list[Creature]:
            cleric = fighter("Wren", max_hp=12)
            cleric.spells = ("Cure Wounds",)
            cleric.spell_slots = {1: 1}
            cleric.items = {"Potion": 1}
            foe = fighter("Skeleton", team="monsters", position=10, max_hp=13)
            return [cleric, foe]

        result = simulate_rounds(
            combatants,
            iterations=20,
            seed=2026073120,
            max_rounds=10,
            spellbook={
                "Cure Wounds": Spell(
                    name="Cure Wounds",
                    level=1,
                    heal=Dice(2, 8, 3),
                    range_feet=5,
                    provenance=FIXTURE,
                )
            },
            items={"Potion": ItemEffect(heal=Dice(2, 4, 2), provenance=FIXTURE)},
        )

        party = result["teams"]["party"]
        assert party["hp_fraction"]["samples"] == 20
        assert 0 <= party["hp_fraction"]["p10"] <= party["hp_fraction"]["p90"] <= 1
        assert party["defeated"]["max"] >= 0
        assert party["spell_slots_spent"]["max"] >= 0
        assert party["items_spent"]["max"] >= 0


def _authored_map() -> dict[str, object]:
    return {
        "format": "fivee-sim-map",
        "format_version": 1,
        "name": "lit opening",
        "grid": {"width": 3, "height": 2, "cell_feet": 5},
        "legend": {".": "normal"},
        "tiles": ["...", "..."],
        "ambient_light": "darkness",
        "features": [
            {
                "id": "roof-window",
                "kind": "opening",
                "at": [0, 0],
                "sight_to_levels": [1],
                "light": {"bright": 20, "dim": 40, "color": "#ffcc66"},
            }
        ],
        "levels": [
            {
                "index": 1,
                "name": "roof",
                "tiles": ["...", "..."],
                "features": [],
            }
        ],
        "provenance": {
            "generator": "hand",
            "seed": 0,
            "params": {},
            "edited": True,
            "source": FIXTURE,
        },
    }


class TestAuthoredOpeningsAndLighting:
    def test_map_round_trip_preserves_openings_ambient_light_and_sources(self) -> None:
        document = parse_document(_authored_map(), source="fixture", terrain=TERRAIN)
        payload = as_payload(document)
        assert payload["ambient_light"] == "darkness"
        assert payload["features"][0]["sight_to_levels"] == [1]
        assert payload["features"][0]["light"] == {
            "bright": 20,
            "dim": 40,
            "color": "#ffcc66",
        }

        grid = to_grid(document)
        assert grid.ground.sight_links == {(0, 0): frozenset({1})}
        assert grid.ground.lights[0].bright == 20
        assert grid.ground.ambient_light.value == "darkness"

    def test_uvtt_export_carries_authored_ambient_light_and_sources(self) -> None:
        document = parse_document(_authored_map(), source="fixture", terrain=TERRAIN)
        exported = to_uvtt(document, terrain=TERRAIN, include_image=False)

        assert exported["environment"] == {
            "baked_lighting": False,
            "ambient_light": "000000ff",
        }
        assert exported["lights"] == [
            {
                "position": {"x": 0.5, "y": 0.5},
                "range": 8.0,
                "intensity": 0.5,
                "color": "ffcc66ff",
                "shadows": True,
            }
        ]

    def test_darkness_penalises_an_attacker_without_a_sense(self) -> None:
        raw = _authored_map()
        raw["features"] = []  # darkness without the authored torch
        document = parse_document(raw, source="fixture", terrain=TERRAIN)
        attacker = fighter("Harrow", position=(2, 2))
        target = fighter("Marauder", team="monsters", position=(7, 2))
        target.darkvision = 60
        encounter = Encounter(
            [attacker, target], FixedRandom(10), battle_map=to_grid(document)
        )
        advance_to(encounter, "Harrow", FixedRandom(10))

        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Marauder", attack="Longsword"),
            FixedRandom(10),
        )

        attack = next(event for event in events if event.kind == "attack")
        assert attack.data["advantage"] == "disadvantage"

    def test_darkvision_and_an_authored_light_each_restore_sight(self) -> None:
        for sees_by_sense in (False, True):
            raw = _authored_map()
            if sees_by_sense:
                raw["features"] = []
            document = parse_document(raw, source="fixture", terrain=TERRAIN)
            attacker = fighter("Harrow", position=(2, 2))
            if sees_by_sense:
                attacker.darkvision = 60
            target = fighter("Marauder", team="monsters", position=(7, 2))
            target.darkvision = 60
            encounter = Encounter(
                [attacker, target], FixedRandom(10), battle_map=to_grid(document)
            )
            advance_to(encounter, "Harrow", FixedRandom(10))

            events = encounter.act(
                Action(kind=ActionKind.ATTACK, target="Marauder", attack="Longsword"),
                FixedRandom(10),
            )

            attack = next(event for event in events if event.kind == "attack")
            assert attack.data["advantage"] == "none"
