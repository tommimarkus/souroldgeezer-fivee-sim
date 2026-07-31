"""The stand act: getting up from Prone.

SRD 5.2, Rules Glossary, "Prone": the condition ends when the creature stands,
which costs an amount of movement equal to half its Speed — no action — and is
impossible when Speed is 0 or the movement left cannot cover the cost. The
policy half matters as much as the act: the auto-play behind the batch tools
stands a Prone creature at its first legal opportunity, through this same act,
so the analytics/stateful parity invariant keeps holding once knockdowns are in
play.

The Wolf is the bundled creature that makes Prone reachable mid-fight. SRD
5.2.1 (p. 364) applies the condition **automatically** on a Bite hit — "If the
target is a Medium or smaller creature, it has the Prone condition", with no
saving throw — so the data tests pin that the rider carries no save, and that
the record's note owns the one approximation: the engine models no creature
size, so the Medium-or-smaller gate is not enforced.
"""

from __future__ import annotations

from collections.abc import Sequence
from random import Random

import pytest

from fivee_sim.analytics.montecarlo import auto_action, run_encounter, simulate_rounds
from fivee_sim.content import make_monster, monster_records, spellbook
from fivee_sim.kernel.actions import RiderExpiry
from fivee_sim.kernel.conditions import Condition
from fivee_sim.kernel.rules import Size, fits_within
from fivee_sim.model.creature import Creature
from fivee_sim.model.encounter import (
    Action,
    ActionKind,
    Encounter,
    EncounterError,
    build_encounter,
)

from .conftest import advance_to, fighter

FIXTURE = "synthetic test fixture, not SRD content"

#: A seed on which the wolf duel below lands two knockdowns; guards pin that.
KNOCKDOWN_SEED = 20260734


def prone_fighter(name: str = "Thora", *, position: int = 0) -> Creature:
    downed = fighter(name, position=position)
    downed.conditions.add(Condition.PRONE)
    return downed


def wolf_duel() -> Sequence[Creature]:
    return [
        fighter("Thora", position=0),
        make_monster("Wolf", position=15),
    ]


class TestStandAct:
    def test_stand_removes_prone_and_charges_half_speed(self) -> None:
        thora = prone_fighter()
        encounter, rng = build_encounter(
            [thora, make_monster("Goblin Warrior", label="Goblin", position=60)],
            seed=5,
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.STAND), rng)

        assert [event.kind for event in events] == ["stand"]
        assert Condition.PRONE not in thora.conditions
        assert events[0].data["cost"] == 15  # half of Speed 30
        assert events[0].data["movement_left"] == 15
        turn = encounter.state()["turn_state"]
        assert turn["movement_left"] == 15
        # No action spent: the whole turn's action economy is still in hand.
        assert turn["action_used"] is False
        assert turn["attacks_left"] == 1

    def test_an_odd_speed_rounds_the_cost_down_to_whole_feet(self) -> None:
        skirmisher = Creature(
            name="Skirmisher",
            team="party",
            ac=14,
            max_hp=20,
            speed=45,
            conditions={Condition.PRONE},
            position=0,
            provenance=FIXTURE,
        )
        encounter, rng = build_encounter(
            [skirmisher, make_monster("Goblin Warrior", label="Goblin", position=60)],
            seed=5,
        )
        assert encounter.stand_cost("Skirmisher") == 22  # 45 // 2, never 23
        advance_to(encounter, "Skirmisher", rng)
        events = encounter.act(Action(kind=ActionKind.STAND), rng)
        assert events[0].data["cost"] == 22
        assert encounter.state()["turn_state"]["movement_left"] == 23

    def test_stand_spends_from_the_same_budget_walking_does(self) -> None:
        thora = prone_fighter()
        encounter, rng = build_encounter(
            [thora, make_monster("Goblin Warrior", label="Goblin", position=60)],
            seed=5,
        )
        advance_to(encounter, "Thora", rng)
        encounter.act(Action(kind=ActionKind.STAND), rng)
        with pytest.raises(EncounterError, match="has 15 ft of movement, needs 20 ft"):
            encounter.act(Action(kind=ActionKind.MOVE, to_position=20), rng)
        encounter.act(Action(kind=ActionKind.MOVE, to_position=15), rng)
        assert encounter.state()["turn_state"]["movement_left"] == 0


class TestStandRefusals:
    def goblin(self) -> Creature:
        return make_monster("Goblin Warrior", label="Goblin", position=60)

    def test_a_creature_that_is_not_prone_cannot_stand(self) -> None:
        encounter, rng = build_encounter([fighter(), self.goblin()], seed=5)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(EncounterError, match="Thora is not prone"):
            encounter.act(Action(kind=ActionKind.STAND), rng)

    def test_too_little_movement_left_refuses_the_stand(self) -> None:
        thora = prone_fighter()
        encounter, rng = build_encounter([thora, self.goblin()], seed=5)
        advance_to(encounter, "Thora", rng)
        # Crawl 20 of the 30 ft first, leaving less than the 15 ft cost.
        encounter.act(Action(kind=ActionKind.MOVE, to_position=20), rng)
        with pytest.raises(
            EncounterError, match="has 10 ft of movement, needs 15 ft to stand"
        ):
            encounter.act(Action(kind=ActionKind.STAND), rng)
        assert Condition.PRONE in thora.conditions

    def test_a_speed_of_zero_on_the_stat_block_refuses_the_stand(self) -> None:
        rooted = Creature(
            name="Rooted",
            team="party",
            ac=10,
            max_hp=10,
            speed=0,
            conditions={Condition.PRONE},
            position=0,
            provenance=FIXTURE,
        )
        encounter, rng = build_encounter([rooted, self.goblin()], seed=5)
        advance_to(encounter, "Rooted", rng)
        with pytest.raises(EncounterError, match="has a speed of 0 and cannot stand"):
            encounter.act(Action(kind=ActionKind.STAND), rng)

    def test_a_condition_that_zeroes_speed_refuses_the_stand(self) -> None:
        thora = prone_fighter()
        thora.conditions.add(Condition.GRAPPLED)
        encounter, rng = build_encounter([thora, self.goblin()], seed=5)
        advance_to(encounter, "Thora", rng)
        with pytest.raises(
            EncounterError, match=r"has speed 0 \(grappled, prone\) and cannot stand"
        ):
            encounter.act(Action(kind=ActionKind.STAND), rng)

    def test_an_unconscious_creature_cannot_stand(self) -> None:
        thora = fighter()
        # An ally keeps the fight alive once Thora drops, so the refusal under
        # test is hers rather than the encounter being over.
        ally = fighter("Bruni", position=30)
        encounter, rng = build_encounter([thora, ally, self.goblin()], seed=5)
        advance_to(encounter, "Thora", rng)
        thora.take_damage(30)  # down mid-turn: Unconscious and Prone
        assert Condition.PRONE in thora.conditions
        with pytest.raises(EncounterError, match="not conscious"):
            encounter.act(Action(kind=ActionKind.STAND), rng)


class TestAutoPolicyStands:
    def test_the_policy_stands_a_prone_creature_before_anything_else(self) -> None:
        thora = prone_fighter()
        encounter, rng = build_encounter(
            [thora, make_monster("Goblin Warrior", label="Goblin", position=5)],
            seed=5,
        )
        advance_to(encounter, "Thora", rng)
        first = auto_action(encounter)
        assert first is not None and first.kind is ActionKind.STAND
        encounter.act(first, rng)
        assert Condition.PRONE not in thora.conditions
        second = auto_action(encounter)
        assert second is not None and second.kind is ActionKind.ATTACK

    def test_a_wolf_knockdown_is_stood_up_on_the_victims_own_turn(self) -> None:
        rng = Random(KNOCKDOWN_SEED)
        encounter = Encounter(list(wolf_duel()), rng, spellbook=spellbook())
        run_encounter(encounter, rng, max_rounds=20)

        knockdowns = [
            event for event in encounter.log
            if event.kind == "effect_apply" and event.data.get("condition") == "prone"
        ]
        assert knockdowns, "the seed must land a Bite for this test to mean anything"
        assert all(event.target == "Thora" for event in knockdowns)
        stands = [event for event in encounter.log if event.kind == "stand"]
        assert len(stands) == len(knockdowns)
        for knock, stand in zip(knockdowns, stands, strict=True):
            assert stand.seq > knock.seq
            assert stand.actor == "Thora"
            assert stand.turn == "Thora", "standing happens on the victim's own turn"

    def test_one_iteration_still_matches_a_hand_driven_run_with_knockdowns(self) -> None:
        rng = Random(KNOCKDOWN_SEED)
        encounter = Encounter(list(wolf_duel()), rng, spellbook=spellbook())
        manual = run_encounter(encounter, rng, max_rounds=20)
        assert any(
            event.kind == "stand" for event in encounter.log
        ), "the seed must exercise the stand path for this parity check to bite"

        batch = simulate_rounds(
            wolf_duel,
            iterations=1,
            seed=KNOCKDOWN_SEED,
            max_rounds=20,
            spellbook=spellbook(),
        )
        expected_winner = manual.winner if manual.winner is not None else "none"
        assert batch["wins"] == {expected_winner: 1}
        assert batch["rounds"]["mean"] == float(manual.rounds)
        assert batch["rounds"]["min"] == batch["rounds"]["max"] == float(manual.rounds)


class TestWolfData:
    def test_the_bundled_wolf_bite_carries_the_automatic_prone_rider(self) -> None:
        wolf = make_monster("Wolf")
        bite = wolf.attacks[0]
        assert bite.name == "Bite"
        assert bite.on_hit_condition == Condition.PRONE
        # SRD 5.2.1: the condition is automatic on a hit — no saving throw.
        assert bite.on_hit_save_ability is None
        assert bite.on_hit_save_dc == 0
        # And no clock: Prone lasts until the target stands.
        assert bite.on_hit_expiry is RiderExpiry.NONE

    def test_the_record_owns_the_size_gate_and_claims_no_save(self) -> None:
        # This assertion used to read the other way round: the gate was
        # unenforced, and the test pinned the ``unmodelled`` note admitting it.
        # The gate is real now, so the record must carry it rather than apologise
        # for its absence — and must still not invent a saving throw.
        record = monster_records()["Wolf"]
        attack = record["attacks"][0]
        assert attack["on_hit_condition"] == "prone"
        assert attack["on_hit_max_size"] == "medium", (
            "SRD 5.2.1 gates the Prone rider at Medium or smaller"
        )
        assert "on_hit_save_ability" not in attack
        assert "on_hit_save_dc" not in attack
        notes = record.get("unmodelled", [])
        assert not any("Medium or smaller" in note for note in notes), (
            "the size gate is enforced; the record must not still call it unmodelled"
        )
        assert all("save" not in note.lower() for note in notes), (
            "SRD 5.2.1 gives the Bite no saving throw; the note must not invent one"
        )

    def test_the_bundled_wolf_refuses_the_rider_against_the_bundled_ogre(self) -> None:
        # The end-to-end shape of the divergence this gate closed, built only
        # from bundled records: before it, a wolf knocked a Large ogre Prone.
        wolf = make_monster("Wolf")
        assert wolf.size is Size.MEDIUM
        ogre = make_monster("Ogre", team="party")
        assert ogre.size is Size.LARGE
        assert not fits_within(ogre.size, wolf.attacks[0].on_hit_max_size or Size.GARGANTUAN)
