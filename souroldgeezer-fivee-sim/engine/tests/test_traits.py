"""Creature traits: Pack Tactics and Undead Fortitude.

Two SRD 5.2 stat-block lines drive this file. The wolf's: Advantage on an attack
roll "if at least one of the wolf's allies is within 5 feet of the creature and
the ally doesn't have the Incapacitated condition." The zombie's: on damage that
reduces it to 0 hit points, "a Constitution saving throw (DC 5 plus the damage
taken) unless the damage is Radiant or from a Critical Hit", standing at 1 hit
point on a success.

The bypass tests are the point of most of the fortitude half: Radiant in either
of an attack's pools, a critical hit, and overkill past the maximum must all
skip the save entirely — no roll, no event — because a roll that should not
happen would also desynchronise the RNG stream between live play and a replay.

Damage in these tests rides d1 dice (``Dice(1, 1, 7)`` is 1d1+7), so every
amount — and with it every fortitude DC — is exact without scripting the dice.
"""

from __future__ import annotations

import json
from pathlib import Path
from random import Random
from typing import Any

from fivee_sim.analytics.montecarlo import auto_action
from fivee_sim.content import load_packs
from fivee_sim.data import make_creature
from fivee_sim.kernel.conditions import EFFECTS, Condition, ConditionEffect, ConditionTable
from fivee_sim.kernel.dice import Advantage, Dice
from fivee_sim.kernel.items import ItemEffect
from fivee_sim.kernel.rules import Ability, DamageType
from fivee_sim.model.creature import AttackOption, Creature
from fivee_sim.model.encounter import Action, ActionKind, Encounter, Event, build_encounter

from .conftest import FixedRandom, advance_to

FIXTURE = "synthetic test fixture, not SRD content"

#: An attack bonus no AC in these tests can refuse; only a natural 1 misses.
SURE_HIT = 100

#: 1d1+7 — deterministic 8 damage, so every fortitude DC in this file is exact.
FLAT_EIGHT = Dice(1, 1, 7)


def strike(
    *,
    damage: Dice = FLAT_EIGHT,
    damage_type: DamageType = DamageType.PIERCING,
    bonus_damage: Dice | None = None,
    bonus_damage_type: DamageType | None = None,
) -> AttackOption:
    return AttackOption(
        name="Strike",
        attack_bonus=SURE_HIT,
        damage=damage,
        damage_type=damage_type,
        bonus_damage=bonus_damage,
        bonus_damage_type=bonus_damage_type,
        provenance=FIXTURE,
    )


def creature(
    name: str,
    *,
    team: str,
    position: int | tuple[int, int] = 0,
    ac: int = 10,
    max_hp: int = 60,
    hp: int | None = None,
    attacks: tuple[AttackOption, ...] = (),
    pack_tactics: bool = False,
    undead_fortitude: bool = False,
    save_bonuses: dict[Ability, int] | None = None,
    resistances: frozenset[DamageType] = frozenset(),
    conditions: set[str] | None = None,
    items: dict[str, int] | None = None,
) -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=ac,
        max_hp=max_hp,
        hp=max_hp if hp is None else hp,
        speed=30,
        attacks=attacks,
        pack_tactics=pack_tactics,
        undead_fortitude=undead_fortitude,
        save_bonuses=save_bonuses or {},
        resistances=resistances,
        conditions=conditions or set(),
        items=items or {},
        position=position,
        provenance=FIXTURE,
    )


class TestPackTactics:
    """The wolf's half: one more Advantage source, read off the whole fight."""

    def pack(
        self,
        *,
        ally_position: int | tuple[int, int] = (10, 0),
        ally_hp: int | None = None,
        ally_conditions: set[str] | None = None,
        attacker_conditions: set[str] | None = None,
        pack_tactics: bool = True,
        condition_effects: ConditionTable | None = None,
    ) -> tuple[Encounter, Creature, Creature]:
        """Alpha at the origin, Thora 5 ft away, and the ally Beta where asked."""
        alpha = creature(
            "Alpha", team="monsters", attacks=(strike(),),
            pack_tactics=pack_tactics, conditions=attacker_conditions,
        )
        thora = creature("Thora", team="party", position=(5, 0))
        beta = creature(
            "Beta", team="monsters", position=ally_position,
            hp=ally_hp, conditions=ally_conditions,
        )
        encounter = Encounter(
            [alpha, thora, beta], Random(1), condition_effects=condition_effects
        )
        return encounter, alpha, thora

    def test_no_ally_near_the_target_means_no_advantage(self) -> None:
        encounter, alpha, thora = self.pack(ally_position=(30, 0))
        assert encounter.attack_advantage(alpha, thora, alpha.attacks[0]) is Advantage.NONE

    def test_a_capable_ally_within_5_feet_grants_advantage(self) -> None:
        encounter, alpha, thora = self.pack(ally_position=(10, 0))
        assert (
            encounter.attack_advantage(alpha, thora, alpha.attacks[0])
            is Advantage.ADVANTAGE
        )

    def test_without_the_flag_the_same_ally_grants_nothing(self) -> None:
        encounter, alpha, thora = self.pack(ally_position=(10, 0), pack_tactics=False)
        assert encounter.attack_advantage(alpha, thora, alpha.attacks[0]) is Advantage.NONE

    def test_an_ally_held_by_a_pack_defined_incapacitation_does_not_count(self) -> None:
        # The condition is a plain string no enum knows, incapacitating only
        # through the table this fight was handed — the discipline
        # ``TestCustomConditions`` pins for conditions generally, applied here.
        table: ConditionTable = {**EFFECTS, "torpor": ConditionEffect(incapacitated=True)}
        encounter, alpha, thora = self.pack(
            ally_conditions={"torpor"}, condition_effects=table
        )
        assert encounter.attack_advantage(alpha, thora, alpha.attacks[0]) is Advantage.NONE

    def test_a_dropped_ally_does_not_count(self) -> None:
        encounter, alpha, thora = self.pack(ally_hp=0)
        assert encounter.attack_advantage(alpha, thora, alpha.attacks[0]) is Advantage.NONE

    def test_an_ally_beside_a_different_enemy_does_not_count(self) -> None:
        alpha = creature("Alpha", team="monsters", attacks=(strike(),), pack_tactics=True)
        thora = creature("Thora", team="party", position=(5, 0))
        mark = creature("Mark", team="party", position=(30, 0))
        beta = creature("Beta", team="monsters", position=(35, 0))
        encounter = Encounter([alpha, thora, mark, beta], Random(1))
        option = alpha.attacks[0]
        assert encounter.attack_advantage(alpha, thora, option) is Advantage.NONE
        # The same ally does count against the enemy it actually crowds.
        assert encounter.attack_advantage(alpha, mark, option) is Advantage.ADVANTAGE

    def test_the_target_is_not_its_own_ally(self) -> None:
        # Attacking a teammate: the target stands 0 ft from itself and is on the
        # attacker's team, which is exactly the pair the rule must not accept.
        alpha = creature("Alpha", team="monsters", attacks=(strike(),), pack_tactics=True)
        beta = creature("Beta", team="monsters", position=(5, 0))
        encounter = Encounter([alpha, beta], Random(1))
        assert encounter.attack_advantage(alpha, beta, alpha.attacks[0]) is Advantage.NONE

    def test_pack_advantage_cancels_an_existing_disadvantage(self) -> None:
        encounter, alpha, thora = self.pack(attacker_conditions={Condition.PRONE})
        assert encounter.attack_advantage(alpha, thora, alpha.attacks[0]) is Advantage.NONE

    def test_a_spell_attack_reads_the_same_advantage(self) -> None:
        encounter, alpha, thora = self.pack()
        assert encounter.spell_attack_advantage(alpha, thora) is Advantage.ADVANTAGE
        far, alpha, thora = self.pack(ally_position=(30, 0))
        assert far.spell_attack_advantage(alpha, thora) is Advantage.NONE

    def _policy_choice(self, *, pack_tactics: bool) -> str:
        """Which target the greedy policy picks for Alpha, flanked Zed or not."""
        alpha = creature(
            "Alpha", team="monsters", attacks=(strike(),), pack_tactics=pack_tactics
        )
        aaron = creature("Aaron", team="party", position=(0, 5), max_hp=50)
        zed = creature("Zed", team="party", position=(5, 0), max_hp=50)
        beta = creature("Beta", team="monsters", position=(10, 0))
        encounter, rng = build_encounter([alpha, aaron, zed, beta], seed=1)
        advance_to(encounter, "Alpha", rng)
        action = auto_action(encounter)
        assert action is not None and action.kind is ActionKind.ATTACK
        assert action.target is not None
        return action.target

    def test_the_policy_prefers_the_target_an_ally_flanks(self) -> None:
        # Two identical targets in reach; Beta crowds only Zed. Without the trait
        # the expectations tie and the stable tiebreak picks Aaron, so a switch
        # to Zed can only have come through ``encounter.attack_advantage``.
        assert self._policy_choice(pack_tactics=False) == "Aaron"
        assert self._policy_choice(pack_tactics=True) == "Zed"


class TestUndeadFortitude:
    """The zombie's half: the drop-to-0 save, and everything that bypasses it."""

    def duel(
        self,
        attack: AttackOption,
        *,
        save_bonus: int,
        hp: int = 5,
        max_hp: int = 200,
        resistances: frozenset[DamageType] = frozenset(),
    ) -> tuple[Encounter, Creature, Random]:
        """Basher's turn, Shambler in reach: the next act is the dropping blow.

        ``save_bonus`` at +100 or -100 forces the save's outcome, so a test
        reads as its scenario rather than as a seed hunt.
        """
        basher = creature("Basher", team="party", attacks=(attack,))
        shambler = creature(
            "Shambler", team="monsters", position=(5, 0),
            max_hp=max_hp, hp=hp, undead_fortitude=True,
            save_bonuses={Ability.CONSTITUTION: save_bonus},
            resistances=resistances,
        )
        rng = Random(1)
        encounter = Encounter([basher, shambler], rng)
        advance_to(encounter, "Basher", rng)
        return encounter, shambler, rng

    def strike_shambler(self, encounter: Encounter, natural: int = 10) -> list[Event]:
        return encounter.act(
            Action(kind=ActionKind.ATTACK, target="Shambler", attack="Strike"),
            FixedRandom(natural),
        )

    def test_a_made_save_leaves_1_hit_point_and_no_drop(self) -> None:
        encounter, shambler, _ = self.duel(strike(), save_bonus=100)
        events = self.strike_shambler(encounter)  # 1d1+7: 8 damage into 5 hp
        assert shambler.hp == 1
        assert Condition.UNCONSCIOUS not in shambler.conditions
        assert Condition.PRONE not in shambler.conditions
        assert not shambler.dying
        assert shambler.death_save_failures == 0
        assert not any(event.kind == "down" for event in events)
        held = next(event for event in events if event.kind == "undead_fortitude")
        assert held.data["success"] is True
        assert held.data["dc"] == 13, "DC 5 plus the 8 damage taken"
        assert "holds at 1 hit point" in held.detail

    def test_a_failed_save_drops_the_creature_as_normal(self) -> None:
        encounter, shambler, _ = self.duel(strike(), save_bonus=-100)
        events = self.strike_shambler(encounter)
        assert shambler.hp == 0
        assert shambler.dying
        assert Condition.UNCONSCIOUS in shambler.conditions
        failed = next(event for event in events if event.kind == "undead_fortitude")
        assert failed.data["success"] is False
        kinds = [event.kind for event in events]
        assert kinds.index("undead_fortitude") < kinds.index("down")

    def test_radiant_main_damage_bypasses_the_save(self) -> None:
        encounter, shambler, _ = self.duel(
            strike(damage_type=DamageType.RADIANT), save_bonus=100
        )
        events = self.strike_shambler(encounter)
        assert shambler.hp == 0 and shambler.dying
        assert not any(event.kind == "undead_fortitude" for event in events)

    def test_a_radiant_rider_pool_bypasses_the_save(self) -> None:
        # The main pool is piercing; only the rider's bonus pool is Radiant.
        # Either pool being Radiant disqualifies — the rule reads "the damage",
        # and a hit is not less radiant for splitting its dice.
        encounter, shambler, _ = self.duel(
            strike(
                bonus_damage=Dice(1, 1, 0), bonus_damage_type=DamageType.RADIANT
            ),
            save_bonus=100,
        )
        events = self.strike_shambler(encounter)
        assert shambler.hp == 0 and shambler.dying
        assert not any(event.kind == "undead_fortitude" for event in events)

    def test_a_critical_hit_bypasses_the_save(self) -> None:
        encounter, shambler, _ = self.duel(strike(), save_bonus=100)
        events = self.strike_shambler(encounter, natural=20)
        attack = next(event for event in events if event.kind == "attack")
        assert attack.data["critical"] is True
        assert shambler.hp == 0 and shambler.dying
        assert not any(event.kind == "undead_fortitude" for event in events)

    def test_instant_death_overkill_bypasses_the_save(self) -> None:
        # 1d1+14 deals 15 into 5 hp: the overflow of 10 meets the maximum of 10,
        # so this is instant death, not a drop to 0 — the trait never applies.
        encounter, shambler, _ = self.duel(
            strike(damage=Dice(1, 1, 14)), save_bonus=100, hp=5, max_hp=10
        )
        events = self.strike_shambler(encounter)
        assert shambler.dead
        assert not any(event.kind == "undead_fortitude" for event in events)

    def test_the_dc_reads_the_damage_dealt_after_resistance(self) -> None:
        # 1d1+7 rolls 8; resistance halves it to 4 dealt, so the DC is 9 — the
        # save is against the damage taken, not the damage rolled.
        encounter, shambler, _ = self.duel(
            strike(), save_bonus=100, hp=3,
            resistances=frozenset({DamageType.PIERCING}),
        )
        events = self.strike_shambler(encounter)
        assert shambler.hp == 1
        damage = next(event for event in events if event.kind == "damage")
        assert damage.data["amount"] == 4
        held = next(event for event in events if event.kind == "undead_fortitude")
        assert held.data["dc"] == 9

    def test_an_items_damage_triggers_the_same_save(self) -> None:
        flask = ItemEffect(
            damage=Dice(1, 1, 7), damage_type=DamageType.ACID, provenance=FIXTURE
        )
        basher = creature("Basher", team="party", items={"Vitriol Flask": 1})
        shambler = creature(
            "Shambler", team="monsters", position=(5, 0),
            max_hp=200, hp=5, undead_fortitude=True,
            save_bonuses={Ability.CONSTITUTION: 100},
        )
        rng = Random(1)
        encounter = Encounter(
            [basher, shambler], rng, items={"Vitriol Flask": flask}
        )
        advance_to(encounter, "Basher", rng)
        events = encounter.act(
            Action(kind=ActionKind.USE_ITEM, item="Vitriol Flask", target="Shambler"),
            FixedRandom(10),
        )
        assert shambler.hp == 1
        held = next(event for event in events if event.kind == "undead_fortitude")
        assert held.data["success"] is True

    def test_the_same_seed_replays_the_same_outcome(self) -> None:
        # A ±2 save against DC 13 genuinely turns on the die, so equality of the
        # two logs is a statement about the stream, not about a forced outcome.
        def run() -> list[dict[str, Any]]:
            basher = creature("Basher", team="party", attacks=(strike(),))
            shambler = creature(
                "Shambler", team="monsters", position=(5, 0),
                max_hp=200, hp=5, undead_fortitude=True,
                save_bonuses={Ability.CONSTITUTION: 2},
            )
            encounter, rng = build_encounter([basher, shambler], seed=11)
            advance_to(encounter, "Basher", rng)
            encounter.act(
                Action(kind=ActionKind.ATTACK, target="Shambler", attack="Strike"), rng
            )
            return [event.as_dict() for event in encounter.log]

        first, second = run(), run()
        assert first == second
        assert any(event["kind"] == "undead_fortitude" for event in first), (
            "the seed must land a non-critical hit for this test to mean anything"
        )


class TestPackTraitFlags:
    """A pack's creature carries the flags exactly as the bundled ones do."""

    PACK: dict[str, Any] = {
        "pack": "trait-vale",
        "version": "1.0",
        "provenance": "Original content, synthetic test fixture",
        "creatures": [
            {
                "name": "Vale Shambler",
                "team": "monsters",
                "ac": 8,
                "max_hp": 40,
                "pack_tactics": True,
                "undead_fortitude": True,
                "save_bonuses": {"constitution": 100},
                "provenance": "Original content",
            }
        ],
    }

    def test_the_flags_load_and_the_save_fires_through_the_stepper(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "trait-vale.json"
        path.write_text(json.dumps(self.PACK), encoding="utf-8")
        registry = load_packs([path], include_environment=False)
        shambler = make_creature(
            "Vale Shambler", registry=registry, team="monsters", position=(5, 0)
        )
        assert shambler.pack_tactics is True
        assert shambler.undead_fortitude is True
        # And the flag is live, not decorative: a dropping blow through the
        # stepper rolls the save, and the +100 bonus holds it at 1 hit point.
        basher = creature(
            "Basher", team="party", attacks=(strike(damage=Dice(1, 1, 39)),)
        )
        rng = Random(1)
        encounter = Encounter(
            [basher, shambler], rng, condition_effects=registry.condition_effects
        )
        advance_to(encounter, "Basher", rng)
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Vale Shambler", attack="Strike"),
            FixedRandom(10),
        )
        assert shambler.hp == 1
        held = next(event for event in events if event.kind == "undead_fortitude")
        assert held.data["success"] is True
        assert held.data["dc"] == 45, "DC 5 plus the 40 damage taken"
