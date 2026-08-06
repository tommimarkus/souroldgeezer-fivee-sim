"""Attack riders: bonus damage, advantage-conditional dice, and on-hit conditions.

Three SRD 5.2.1 stat-block shapes drive this file: the goblin's extra 1d4 when the
attack roll had Advantage, the steam mephit's claw adding fire to its slashing,
and the giant centipede's bite poisoning "until the start of the centipede's
next turn". The expiry tests are the point of most of it: a timed condition must
end when the anchor's turn *slot* passes — even if the anchor is dead — and must
never strip the same condition when something else still imposes it.

The pack-defined condition tests mirror ``TestCustomConditions`` in
``test_content.py``: a rider condition is a plain string, and a green run over
SRD conditions alone cannot prove that, because every SRD condition is a
``StrEnum`` member and answers to both.
"""

from __future__ import annotations

import json
from pathlib import Path
from random import Random
from typing import Any

import pytest

from fivee_sim.content import load_packs, make_creature
from fivee_sim.kernel.actions import AttackKind, RiderExpiry, resolve_attack
from fivee_sim.kernel.conditions import Condition, UnknownCondition
from fivee_sim.kernel.dice import Advantage, Dice
from fivee_sim.kernel.rules import Ability, DamageType, Size
from fivee_sim.model.creature import AttackOption, Creature, DeathRule
from fivee_sim.model.encounter import (
    EVENT_KINDS,
    Action,
    ActionKind,
    Encounter,
    Event,
    build_encounter,
)

FIXTURE = "synthetic test fixture, not SRD content"

#: An attack bonus no AC in these tests can refuse; only a natural 1 misses.
SURE_HIT = 100


def bite(
    *,
    condition: str = "poisoned",
    save_dc: int | None = None,
    expiry: RiderExpiry = RiderExpiry.START_OF_ATTACKER_NEXT_TURN,
    max_size: Size | None = None,
) -> AttackOption:
    return AttackOption(
        name="Bite",
        attack_bonus=SURE_HIT,
        damage=Dice(1, 4, 2),
        damage_type=DamageType.PIERCING,
        on_hit_condition=condition,
        on_hit_save_ability=Ability.CONSTITUTION if save_dc is not None else None,
        on_hit_save_dc=save_dc or 0,
        on_hit_expiry=expiry,
        on_hit_max_size=max_size,
        provenance=FIXTURE,
    )


def creature(
    name: str,
    *,
    team: str,
    position: int = 0,
    attacks: tuple[AttackOption, ...] = (),
    conditions: set[str] | None = None,
    max_hp: int = 60,
    size: Size = Size.MEDIUM,
    condition_immunities: frozenset[str] = frozenset(),
) -> Creature:
    return Creature(
        name=name,
        team=team,
        ac=10,
        max_hp=max_hp,
        speed=30,
        attacks=attacks,
        conditions={c: 1 for c in conditions or ()},
        position=position,
        size=size,
        condition_immunities=condition_immunities,
        provenance=FIXTURE,
    )


def bite_and_advance_to_target(
    encounter: Encounter, rng: Random, attacker: str, target: str
) -> list[Event]:
    """Walk the fight to the attacker's turn and land its Bite on the target."""
    for _ in range(4):
        if encounter.current_name == attacker:
            break
        encounter.advance(rng)
    assert encounter.current_name == attacker
    events = encounter.act(
        Action(kind=ActionKind.ATTACK, target=target, attack="Bite"), rng
    )
    attack = next(event for event in events if event.kind == "attack")
    assert attack.data["hit"], "the seed must land the hit for this test to mean anything"
    return events


class TestResolveAttackDamageRiders:
    """The kernel's half: what the riders roll and how they defend."""

    def hit(self, seed: int = 1, **arguments: Any) -> Any:
        resolution = resolve_attack(
            Random(seed),
            attack_bonus=SURE_HIT,
            target_ac=15,
            damage=Dice(1, 6, 2),
            **arguments,
        )
        assert resolution.hit, "the seed must land the hit"
        return resolution

    def test_bonus_damage_rolls_on_every_hit_and_keeps_its_own_total(self) -> None:
        resolution = self.hit(bonus_damage=Dice(1, 4, 0))
        assert resolution.bonus_damage is not None
        assert resolution.bonus_damage_dealt == resolution.bonus_damage.total
        assert resolution.total_damage_dealt == (
            resolution.damage_dealt + resolution.bonus_damage_dealt
        )

    def test_bonus_damage_is_defended_against_its_own_type(self) -> None:
        resolution = self.hit(bonus_damage=Dice(1, 4, 0), bonus_immune=True)
        assert resolution.bonus_damage is not None, "immunity zeroes it, not skips it"
        assert resolution.bonus_damage_dealt == 0
        assert resolution.damage_dealt > 0, "the main pool is untouched by it"

    def test_the_advantage_rider_rolls_only_under_resolved_advantage(self) -> None:
        with_advantage = self.hit(
            advantage=Advantage.ADVANTAGE, advantage_bonus_damage=Dice(1, 4, 0)
        )
        assert with_advantage.advantage_damage is not None
        for state in (Advantage.NONE, Advantage.DISADVANTAGE):
            without = self.hit(advantage=state, advantage_bonus_damage=Dice(1, 4, 0))
            assert without.advantage_damage is None

    def test_the_advantage_rider_shares_the_main_pools_defenses(self) -> None:
        resolution = self.hit(
            advantage=Advantage.ADVANTAGE,
            advantage_bonus_damage=Dice(1, 4, 0),
            resisted=True,
        )
        assert resolution.advantage_damage is not None
        # One damage instance: resistance halves the sum, not each roll.
        combined = resolution.damage.total + resolution.advantage_damage.total
        assert resolution.damage_dealt == combined // 2

    def test_a_critical_doubles_every_riders_dice(self) -> None:
        resolution = self.hit(
            forced_critical=True,
            advantage=Advantage.ADVANTAGE,
            advantage_bonus_damage=Dice(1, 4, 0),
            bonus_damage=Dice(1, 4, 0),
        )
        assert resolution.critical
        assert resolution.advantage_damage is not None
        assert resolution.bonus_damage is not None
        assert len(resolution.advantage_damage.rolls) == 2
        assert len(resolution.bonus_damage.rolls) == 2


class TestOnHitConditionRiders:
    def test_a_condition_with_no_save_lands_on_a_hit(self) -> None:
        attacker = creature("Centipede", team="monsters", attacks=(bite(),))
        target = creature("Thora", team="party", position=5)
        encounter, rng = build_encounter([attacker, target], seed=3)
        events = bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        assert "poisoned" in target.conditions
        applied = next(event for event in events if event.kind == "effect_apply")
        assert applied.data["condition"] == "poisoned"
        assert applied.data["applied"] is True
        assert applied.data["saved"] is None
        assert "until the start of Centipede's next turn" in applied.detail

    def test_a_made_save_refuses_the_condition_and_says_so(self) -> None:
        # DC 1 with no negative modifier cannot be failed: every natural roll
        # meets it, so the outcome is deterministic whatever the seed rolls.
        attacker = creature(
            "Centipede", team="monsters", attacks=(bite(save_dc=1),)
        )
        target = creature("Thora", team="party", position=5)
        encounter, rng = build_encounter([attacker, target], seed=3)
        events = bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        assert "poisoned" not in target.conditions
        applied = next(event for event in events if event.kind == "effect_apply")
        assert applied.data["applied"] is False
        assert applied.data["saved"] is True
        assert "saves against poisoned" in applied.detail

    def test_a_failed_save_applies_the_condition(self) -> None:
        # DC 30 with a +0 modifier cannot be met — the engine gives saving
        # throws no natural-20 auto-success — so the failure is deterministic.
        attacker = creature(
            "Centipede", team="monsters", attacks=(bite(save_dc=30),)
        )
        target = creature("Thora", team="party", position=5)
        encounter, rng = build_encounter([attacker, target], seed=3)
        events = bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        assert "poisoned" in target.conditions
        applied = next(event for event in events if event.kind == "effect_apply")
        assert applied.data["applied"] is True
        assert applied.data["saved"] is False

    def test_every_rider_event_kind_is_declared(self) -> None:
        attacker = creature("Centipede", team="monsters", attacks=(bite(),))
        target = creature("Thora", team="party", position=5)
        encounter, rng = build_encounter([attacker, target], seed=3)
        bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        encounter.advance(rng)  # ends Centipede's turn
        encounter.advance(rng)  # ends Thora's turn; Centipede's next turn starts
        seen = {event.kind for event in encounter.log}
        assert seen <= EVENT_KINDS, f"undeclared kinds: {sorted(seen - EVENT_KINDS)}"
        assert {"effect_apply", "effect_end"} <= seen

    def test_an_opportunity_attack_carries_the_same_rider(self) -> None:
        attacker = creature("Centipede", team="monsters", attacks=(bite(),))
        runner = creature("Thora", team="party", position=5)
        encounter, rng = build_encounter([attacker, runner], seed=3)
        for _ in range(4):
            if encounter.current_name == "Thora":
                break
            encounter.advance(rng)
        events = encounter.act(
            Action(kind=ActionKind.MOVE, to_position=30), rng
        )
        reaction = next(
            event for event in events if event.kind == "opportunity_attack"
        )
        assert reaction.data["hit"], "the seed must land the reaction hit"
        assert "poisoned" in runner.conditions
        assert any(event.kind == "effect_apply" for event in events)


class TestConditionImmunity:
    """A stat block immune to a condition never gains it — SRD 5.2.1's Skeleton
    and Zombie, immune to Poisoned among others.

    Enforcement sits at :meth:`Creature.add_condition`, the one chokepoint
    every condition-imposing path funnels through: an attack rider, a spell,
    an item and a GM ruling all reach it — the spell and GM cases live in
    ``test_encounter.py``, next to the rest of their own paths. This class
    covers the attack-rider path and the model-level rules that make the
    chokepoint hold regardless of which path calls it.
    """

    def test_an_attack_rider_is_refused_by_immunity_and_says_so(self) -> None:
        attacker = creature("Centipede", team="monsters", attacks=(bite(),))
        target = creature(
            "Golem", team="party", position=5,
            condition_immunities=frozenset({"poisoned"}),
        )
        encounter, rng = build_encounter([attacker, target], seed=3)
        events = bite_and_advance_to_target(encounter, rng, "Centipede", "Golem")
        assert "poisoned" not in target.conditions
        assert target.hp < target.max_hp, "the bite must still deal its damage"
        refused = next(event for event in events if event.kind == "effect_apply")
        assert refused.data["applied"] is False
        assert refused.data["condition"] == "poisoned"
        assert "immune" in refused.detail

    def test_the_same_bite_still_poisons_a_target_without_the_immunity(self) -> None:
        # The regression pin: refusing the immune target above must not have
        # disabled the rider for everybody.
        attacker = creature("Centipede", team="monsters", attacks=(bite(),))
        target = creature("Thora", team="party", position=5)
        encounter, rng = build_encounter([attacker, target], seed=3)
        bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        assert "poisoned" in target.conditions

    def test_immunity_is_settled_before_any_save_is_rolled(self) -> None:
        """Same RNG-stream proof as the size gate, for the same reason.

        An immune target must not consume a saving throw it can never fail —
        rolling one would move every later draw, and a save-carrying rider
        would then behave differently at one seed than an otherwise identical
        rider with no save at all.
        """

        def stream_after_a_refused_bite(save_dc: int | None) -> float:
            attacker = creature(
                "Centipede", team="monsters", attacks=(bite(save_dc=save_dc),)
            )
            target = creature(
                "Golem", team="party", position=5,
                condition_immunities=frozenset({"poisoned"}),
            )
            encounter, rng = build_encounter([attacker, target], seed=3)
            bite_and_advance_to_target(encounter, rng, "Centipede", "Golem")
            assert "poisoned" not in target.conditions
            return rng.random()

        assert stream_after_a_refused_bite(None) == stream_after_a_refused_bite(15), (
            "immunity rolled a saving throw it should never have reached"
        )

    def test_no_ongoing_effect_is_registered_for_a_refused_condition(self) -> None:
        attacker = creature("Centipede", team="monsters", attacks=(bite(),))
        target = creature(
            "Golem", team="party", position=5,
            condition_immunities=frozenset({"poisoned"}),
        )
        encounter, rng = build_encounter([attacker, target], seed=3)
        bite_and_advance_to_target(encounter, rng, "Centipede", "Golem")
        assert encounter.state()["ongoing_effects"] == []

    def test_immunity_to_an_undefined_condition_is_legal(self) -> None:
        # Immunity is a declarative refusal, never a table lookup, so a
        # creature can be immune to a condition no loaded table defines —
        # the general property SRD 5.2.1's Zombie and Skeleton exercised
        # while Exhaustion still had no row here.
        golem = creature(
            "Golem", team="monsters",
            condition_immunities=frozenset({"petrifying_gaze"}),
        )
        assert golem.add_condition("petrifying_gaze") is False
        assert "petrifying_gaze" not in golem.conditions

    def test_a_condition_the_table_does_not_define_still_raises_when_not_immune(
        self,
    ) -> None:
        golem = creature("Golem", team="monsters")
        with pytest.raises(UnknownCondition, match="petrifying_gaze"):
            golem.add_condition("petrifying_gaze")


class TestExhaustionDeath:
    """SRD 5.2.1 p.181, Exhaustion: "You die if your Exhaustion level is 6."

    No save, no roll — the level reaching 6 kills outright, so this lives at
    ``Creature.add_condition`` rather than anywhere ``take_damage`` looks, and
    runs regardless of ``death_rule``: the SRD names no death-save rule here.
    """

    def test_reaching_level_six_kills(self) -> None:
        target = creature("Thora", team="party")
        for _ in range(6):
            target.add_condition("exhaustion")
        assert target.dead

    def test_five_levels_does_not_kill(self) -> None:
        target = creature("Thora", team="party")
        for _ in range(5):
            target.add_condition("exhaustion")
        assert not target.dead
        assert target.level_of("exhaustion") == 5

    def test_death_ignores_the_configured_death_rule(self) -> None:
        target = creature("Thora", team="party")
        target.death_rule = DeathRule.DEATH_SAVES
        for _ in range(6):
            target.add_condition("exhaustion")
        assert target.dead

    def test_death_clears_concentration_and_unconscious(self) -> None:
        target = creature("Thora", team="party")
        target.concentrating_on = "Bless"
        target.conditions[Condition.UNCONSCIOUS] = 1
        for _ in range(6):
            target.add_condition("exhaustion")
        assert target.dead
        assert target.concentrating_on is None
        assert Condition.UNCONSCIOUS not in target.conditions


class TestSizeGatedRiders:
    """A rider the stat block gates on target size — SRD 5.2.1's Wolf.

    "If the target is a Medium or smaller creature, it has the Prone condition."
    The pair of a refused case and an applied case is the point: a test that only
    asserts the Large target stays upright passes just as well against a rider
    that was deleted outright, and a test that only asserts the Medium target
    falls passes against a gate that never fires.
    """

    def test_a_gated_rider_is_refused_against_a_larger_target(self) -> None:
        attacker = creature(
            "Wolf", team="monsters", attacks=(bite(condition="prone", max_size=Size.MEDIUM),)
        )
        target = creature("Ogre", team="party", position=5, size=Size.LARGE)
        encounter, rng = build_encounter([attacker, target], seed=3)
        events = bite_and_advance_to_target(encounter, rng, "Wolf", "Ogre")
        assert "prone" not in target.conditions
        assert target.hp < target.max_hp, "the bite must still deal its damage"
        refused = next(event for event in events if event.kind == "effect_apply")
        assert refused.data["applied"] is False
        assert refused.data["condition"] == "prone"
        assert "large" in refused.detail

    def test_the_same_rider_lands_on_a_target_at_the_limit(self) -> None:
        attacker = creature(
            "Wolf", team="monsters", attacks=(bite(condition="prone", max_size=Size.MEDIUM),)
        )
        target = creature("Thora", team="party", position=5, size=Size.MEDIUM)
        encounter, rng = build_encounter([attacker, target], seed=3)
        events = bite_and_advance_to_target(encounter, rng, "Wolf", "Thora")
        assert "prone" in target.conditions
        applied = next(event for event in events if event.kind == "effect_apply")
        assert applied.data["applied"] is True

    def test_the_gate_admits_a_target_below_the_limit(self) -> None:
        attacker = creature(
            "Wolf", team="monsters", attacks=(bite(condition="prone", max_size=Size.MEDIUM),)
        )
        target = creature("Snik", team="party", position=5, size=Size.SMALL)
        encounter, rng = build_encounter([attacker, target], seed=3)
        bite_and_advance_to_target(encounter, rng, "Wolf", "Snik")
        assert "prone" in target.conditions

    def test_an_ungated_rider_ignores_size_entirely(self) -> None:
        # The default path every other rider in this file takes: no gate, so a
        # Gargantuan target is as susceptible as any other.
        attacker = creature("Centipede", team="monsters", attacks=(bite(),))
        target = creature("Thora", team="party", position=5, size=Size.GARGANTUAN)
        encounter, rng = build_encounter([attacker, target], seed=3)
        bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        assert "poisoned" in target.conditions

    def test_the_gate_refuses_before_any_save_is_rolled(self) -> None:
        """The gate must precede the save, and the evidence is the RNG stream.

        A save the gate has already made moot must not be rolled: the draw would
        consume the stream and move every later roll in the fight, so the same
        seed would stop meaning the same fight. That is invisible to an
        assertion about ``conditions`` — refusing after the save looks identical
        from the target's side — so this compares two runs at one seed whose
        riders differ *only* in carrying a save. They can agree on the next draw
        only if neither rolled one.
        """

        def stream_after_a_refused_bite(save_dc: int | None) -> float:
            attacker = creature(
                "Wolf", team="monsters",
                attacks=(
                    bite(condition="prone", save_dc=save_dc, max_size=Size.MEDIUM),
                ),
            )
            target = creature("Ogre", team="party", position=5, size=Size.LARGE)
            encounter, rng = build_encounter([attacker, target], seed=3)
            bite_and_advance_to_target(encounter, rng, "Wolf", "Ogre")
            assert "prone" not in target.conditions, "the gate must refuse either way"
            return rng.random()

        assert stream_after_a_refused_bite(None) == stream_after_a_refused_bite(15), (
            "the gated rider rolled a saving throw it should never have reached"
        )

    def test_the_gate_holds_on_the_opportunity_attack_path(self) -> None:
        # The reaction reaches the rider through the same choke point; if it did
        # not, this is where a second, ungated copy would show itself.
        attacker = creature(
            "Wolf", team="monsters", attacks=(bite(condition="prone", max_size=Size.MEDIUM),)
        )
        runner = creature("Ogre", team="party", position=5, size=Size.LARGE)
        encounter, rng = build_encounter([attacker, runner], seed=3)
        for _ in range(4):
            if encounter.current_name == "Ogre":
                break
            encounter.advance(rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=30), rng)
        reaction = next(event for event in events if event.kind == "opportunity_attack")
        assert reaction.data["hit"], "the seed must land the reaction hit"
        assert "prone" not in runner.conditions


class TestTimedExpiry:
    def test_start_of_attacker_next_turn_lifts_on_schedule(self) -> None:
        attacker = creature("Centipede", team="monsters", attacks=(bite(),))
        target = creature("Thora", team="party", position=5)
        encounter, rng = build_encounter([attacker, target], seed=3)
        bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        encounter.advance(rng)  # Centipede's turn ends; Thora's begins
        assert "poisoned" in target.conditions, "the poison holds through Thora's turn"
        events = encounter.advance(rng)  # Thora's turn ends; Centipede's begins
        assert "poisoned" not in target.conditions
        ended = next(event for event in events if event.kind == "effect_end")
        assert "poisoned lifts" in ended.detail
        # The lift belongs to the start of the attacker's turn, after its
        # turn_start — not to the end of the target's.
        kinds = [event.kind for event in events]
        assert kinds.index("effect_end") > kinds.index("turn_start")

    def test_end_of_target_next_turn_lifts_at_that_turns_end(self) -> None:
        attacker = creature(
            "Centipede", team="monsters",
            attacks=(bite(expiry=RiderExpiry.END_OF_TARGET_NEXT_TURN),),
        )
        target = creature("Thora", team="party", position=5)
        encounter, rng = build_encounter([attacker, target], seed=3)
        bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        encounter.advance(rng)  # Centipede's turn ends; Thora's begins
        assert "poisoned" in target.conditions
        events = encounter.advance(rng)  # Thora's turn ends: the poison goes with it
        assert "poisoned" not in target.conditions
        kinds = [event.kind for event in events]
        assert kinds.index("effect_end") > kinds.index("turn_end")
        assert kinds.index("effect_end") < kinds.index("turn_start")

    def test_expiry_none_means_the_condition_simply_stays(self) -> None:
        attacker = creature(
            "Centipede", team="monsters", attacks=(bite(expiry=RiderExpiry.NONE),)
        )
        target = creature("Thora", team="party", position=5)
        encounter, rng = build_encounter([attacker, target], seed=3)
        bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        for _ in range(6):
            encounter.advance(rng)
        assert "poisoned" in target.conditions
        assert not any(event.kind == "effect_end" for event in encounter.log)

    def test_expiry_fires_when_the_dead_attackers_slot_passes(self) -> None:
        attacker = creature("Centipede", team="monsters", attacks=(bite(),), max_hp=10)
        target = creature("Thora", team="party", position=5)
        # A living teammate keeps the fight going once the centipede is dead, so
        # turns keep passing and the dead slot actually comes around.
        ally = creature("Rat", team="monsters", position=100)
        encounter, rng = build_encounter([attacker, target, ally], seed=3)
        bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        # Killed outright after acting: massive damage, no death saves to roll.
        encounter.creatures["Centipede"].take_damage(1000)
        assert encounter.creatures["Centipede"].dead
        assert "poisoned" in target.conditions
        # The dead centipede's turn is skipped, but its slot still passes — and
        # the slot passing, not the creature acting, is what expires the poison.
        for _ in range(len(encounter.order) + 1):
            encounter.advance(rng)
        assert "poisoned" not in target.conditions
        assert any(event.kind == "effect_end" for event in encounter.log)

    def test_a_directly_set_condition_survives_a_rider_expiry(self) -> None:
        attacker = creature("Centipede", team="monsters", attacks=(bite(),))
        target = creature(
            "Thora", team="party", position=5, conditions={"poisoned"}
        )
        encounter, rng = build_encounter([attacker, target], seed=3)
        bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        encounter.advance(rng)
        events = encounter.advance(rng)  # the rider expires here
        ended = next(event for event in events if event.kind == "effect_end")
        assert "persists" in ended.detail
        assert "poisoned" in target.conditions, (
            "the stat block set this condition; a rider's timer may not strip it"
        )

    def test_two_rider_applications_expire_independently(self) -> None:
        # Bitten in two consecutive rounds: the first timer's expiry finds the
        # second still holding the condition, and only the second lifts it.
        attacker = creature("Centipede", team="monsters", attacks=(bite(),))
        target = creature("Thora", team="party", position=5)
        encounter, rng = build_encounter([attacker, target], seed=3)
        bite_and_advance_to_target(encounter, rng, "Centipede", "Thora")
        encounter.advance(rng)
        events = encounter.advance(rng)  # first timer expires as the turn starts
        assert "poisoned" not in target.conditions
        # Bite again on this, the centipede's second turn.
        events = encounter.act(
            Action(kind=ActionKind.ATTACK, target="Thora", attack="Bite"), rng
        )
        assert next(e for e in events if e.kind == "attack").data["hit"]
        assert "poisoned" in target.conditions
        encounter.advance(rng)
        events = encounter.advance(rng)
        assert "poisoned" not in target.conditions
        lifts = [
            event for event in encounter.log
            if event.kind == "effect_end" and "poisoned lifts" in event.detail
        ]
        assert len(lifts) == 2


class TestPackDefinedRiderConditions:
    """A rider condition that no enum knows: the string discipline, end to end."""

    PACK: dict[str, Any] = {
        "pack": "rider-vale",
        "version": "1.0",
        "provenance": "Original content, synthetic test fixture",
        "conditions": [
            {
                "name": "vale-toxin",
                "description": "The vale's venom saps every swing.",
                "effects": {"own_attacks_have_disadvantage": True},
                "provenance": "Original content",
            }
        ],
        "creatures": [
            {
                "name": "Vale Biter",
                "team": "monsters",
                "ac": 10,
                "max_hp": 30,
                "attacks": [
                    {
                        "name": "Bite",
                        "attack_bonus": 100,
                        "damage": "1d4+2",
                        "damage_type": "piercing",
                        "on_hit_condition": "vale-toxin",
                        "on_hit_expiry": "start_of_attacker_next_turn",
                    }
                ],
                "provenance": "Original content",
            }
        ],
    }

    def test_a_pack_condition_rides_an_attack_and_expires(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "rider-vale.json"
        path.write_text(json.dumps(self.PACK), encoding="utf-8")
        registry = load_packs([path], include_environment=False)
        attacker = make_creature(
            "Vale Biter", registry=registry, label="Biter", team="monsters"
        )
        target = make_creature(
            "Vale Biter", registry=registry, label="Victim", team="party",
            position=5,
        )
        rng = Random(3)
        encounter = Encounter(
            [attacker, target], rng,
            condition_effects=registry.condition_effects,
        )
        events = bite_and_advance_to_target(encounter, rng, "Biter", "Victim")
        assert "vale-toxin" in target.conditions
        # The event detail is what the assistant narrates from, so it must render.
        assert any("vale-toxin" in event.detail for event in events)
        state = encounter.state()
        held = next(c for c in state["combatants"] if c["name"] == "Victim")
        assert "vale-toxin" in held["conditions"]
        # And the condition actually bites: the victim's own swing has
        # disadvantage while it holds.
        assert encounter.attack_advantage(
            target, attacker, target.attacks[0]
        ) is Advantage.DISADVANTAGE
        encounter.advance(rng)
        events = encounter.advance(rng)
        assert "vale-toxin" not in target.conditions
        assert any(
            event.kind == "effect_end" and "vale-toxin lifts" in event.detail
            for event in events
        )


class TestBundledGoblinRider:
    """SRD 5.2.1 p290: "plus 2 (1d4) Slashing damage if the attack roll had
    Advantage" — modelled now, so it must be off the unmodelled list."""

    def test_both_goblin_attacks_carry_the_advantage_rider(self) -> None:
        goblin = make_creature("Goblin Warrior")
        assert [option.name for option in goblin.attacks] == ["Scimitar", "Shortbow"]
        for option in goblin.attacks:
            assert option.advantage_bonus_damage == Dice(1, 4)

    def test_structured_omissions_keep_only_the_unexecutable_hide_option(self) -> None:
        from fivee_sim.content import monster_records

        record = monster_records()["Goblin Warrior"]
        assert record["bonus_actions"] == ["disengage"]
        assert record["unmodelled_facts"] == [
            {"code": "unsupported_bonus_action", "feature": "Nimble Escape: Hide"},
            {"code": "unsupported_passive_perception", "feature": "Passive Perception 9"}
        ]
        assert record["skill_bonuses"] == {"stealth": 6}


class TestAttackOptionGuards:
    def test_bonus_damage_without_a_type_is_refused(self) -> None:
        with pytest.raises(ValueError, match="bonus_damage_type"):
            AttackOption(
                name="Claw",
                attack_bonus=4,
                damage=Dice(1, 4, 0),
                damage_type=DamageType.SLASHING,
                bonus_damage=Dice(1, 4, 0),
            )

    def test_a_save_ability_without_a_dc_is_refused(self) -> None:
        with pytest.raises(ValueError, match="on_hit_save_dc"):
            AttackOption(
                name="Bite",
                attack_bonus=4,
                damage=Dice(1, 4, 0),
                damage_type=DamageType.PIERCING,
                on_hit_condition="poisoned",
                on_hit_save_ability=Ability.CONSTITUTION,
            )

    def test_ammunition_on_a_melee_attack_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ammunition"):
            AttackOption(
                name="Claw",
                attack_bonus=4,
                damage=Dice(1, 4, 0),
                damage_type=DamageType.SLASHING,
                ammunition="Arrow",
            )

    def test_loading_on_a_melee_attack_is_refused(self) -> None:
        with pytest.raises(ValueError, match="loading"):
            AttackOption(
                name="Claw",
                attack_bonus=4,
                damage=Dice(1, 4, 0),
                damage_type=DamageType.SLASHING,
                loading=True,
            )

    def test_a_blank_ammunition_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ammunition"):
            AttackOption(
                name="Longbow",
                attack_bonus=4,
                damage=Dice(1, 8, 0),
                damage_type=DamageType.PIERCING,
                kind=AttackKind.RANGED,
                normal_range=150,
                long_range=600,
                ammunition="   ",
            )

    def test_thrown_on_a_melee_attack_is_refused(self) -> None:
        # ``thrown`` is the third rider that rides on ``kind`` being ranged, and
        # it is guarded here for the same reason the other two are: the field
        # only says what happens *inside* reach, and an option already resolving
        # in melee everywhere has nothing left for it to say.
        with pytest.raises(ValueError, match="thrown"):
            AttackOption(
                name="Handaxe",
                attack_bonus=4,
                damage=Dice(1, 6, 0),
                damage_type=DamageType.SLASHING,
                thrown=True,
            )

    def test_a_thrown_attack_with_no_range_is_refused(self) -> None:
        # A thrown weapon that cannot be thrown is a melee weapon written the
        # long way round, and it would be *worse* than one: ``max_distance()``
        # returns 0 for a ranged option with no range, so every attack past the
        # attacker's own square is refused. That is the ``range``-vs-
        # ``normal_range`` pregen defect, and this refuses it at construction.
        with pytest.raises(ValueError, match="thrown"):
            AttackOption(
                name="Handaxe",
                attack_bonus=4,
                damage=Dice(1, 6, 0),
                damage_type=DamageType.SLASHING,
                kind=AttackKind.RANGED,
                thrown=True,
            )
