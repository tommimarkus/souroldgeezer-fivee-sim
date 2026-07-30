"""The structured event log: stamps, payloads, action records, replay.

The log is the replay foundation: every event knows where in the fight it
happened, every payload carries the facts its prose ``detail`` narrates, and the
recorded actions plus the seed reproduce the log exactly. Each class here pins
one layer of that contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from random import Random

import pytest

from fivee_sim.analytics.montecarlo import run_encounter
from fivee_sim.content import ContentRegistry, load_packs
from fivee_sim.data import make_creature, make_monster, spellbook
from fivee_sim.kernel.dice import Dice
from fivee_sim.kernel.items import ItemEffect
from fivee_sim.model.creature import Creature
from fivee_sim.model.encounter import (
    EVENT_KINDS,
    Action,
    ActionKind,
    Encounter,
    EncounterError,
    build_encounter,
)

from .test_encounter import advance_to, caster, fighter

SEED = 20260730
FIXTURE = "synthetic test fixture, not SRD content"
CORPUS = Path(__file__).parent / "packs"

ITEMS = {
    "Healing Draught": ItemEffect(heal=Dice.parse("2d4+2"), provenance=FIXTURE),
}


def brawl() -> list[Creature]:
    """Two attackers against two monsters, equipped to touch every action path."""
    thora = fighter("Thora", position=0)
    thora.items = {"Healing Draught": 2}
    return [
        thora,
        caster("Wren", position=5),
        make_monster("Ogre", label="Ogre", position=30),
        make_monster("Goblin Warrior", label="Goblin", position=35),
    ]


def played_out(seed: int = SEED) -> tuple[Encounter, Random]:
    """A finished fight: a scripted opening touching item, move, cast, and dash,
    then the auto-play policy driving attacks, damage, and deaths to a conclusion.
    """
    encounter, rng = build_encounter(
        brawl(), seed=seed, spellbook=spellbook(), items=ITEMS
    )
    advance_to(encounter, "Thora", rng)
    encounter.act(Action(kind=ActionKind.USE_ITEM, item="Healing Draught"), rng)
    encounter.act(Action(kind=ActionKind.MOVE, to_position=10), rng)
    advance_to(encounter, "Wren", rng)
    encounter.act(
        Action(kind=ActionKind.CAST, spell="Fireball", slot_level=3, center=32), rng
    )
    encounter.advance(rng)
    advance_to(encounter, "Thora", rng)
    encounter.act(Action(kind=ActionKind.DASH), rng)
    advance_to(encounter, "Wren", rng)
    encounter.act(
        Action(kind=ActionKind.CAST, spell="Hold Person", slot_level=2, target="Ogre"),
        rng,
    )
    run_encounter(encounter, rng, max_rounds=20)
    return encounter, rng


class TestEventIndexing:
    def test_seq_equals_the_position_in_the_log(self) -> None:
        encounter, _ = played_out()
        assert len(encounter.log) > 20
        for index, event in enumerate(encounter.log):
            assert event.seq == index

    def test_round_stamps_follow_the_round_events_across_a_wrap(self) -> None:
        encounter, _ = played_out()
        # Fold the expected round from the log itself: it starts at 1 and ticks at
        # each round event, which is emitted after the counter increments.
        expected = 1
        for event in encounter.log:
            if event.kind == "round":
                expected += 1
            assert event.round == expected, f"event {event.seq} ({event.kind})"
        assert expected > 1, "the fight never wrapped a round"

    def test_turn_stamps_name_the_creature_whose_turn_it_is(self) -> None:
        encounter, _ = played_out()
        # Between a turn_start and its matching turn_end, every event happens on
        # that creature's turn — including the death saves _begin_turn rolls.
        current: str | None = None
        for event in encounter.log:
            if event.kind == "turn_start":
                current = event.actor
            if current is not None and event.kind != "round":
                assert event.turn == current, f"event {event.seq} ({event.kind})"
            if event.kind == "turn_end":
                current = None

    def test_every_emitted_kind_is_a_declared_kind(self) -> None:
        assert len(EVENT_KINDS) == 19
        encounter, _ = played_out()
        seen = {event.kind for event in encounter.log}
        assert seen <= EVENT_KINDS, f"undeclared kinds: {sorted(seen - EVENT_KINDS)}"
        # The fight has to have actually exercised the interesting paths, or the
        # subset check above proves nothing.
        assert {
            "attack", "cast", "concentration", "damage", "dash", "move",
            "round", "spell_effect", "turn_end", "turn_start", "use_item", "heal",
        } <= seen


class TestStructuredPayloads:
    """The ``data`` payloads a replay consumer folds, checked against the prose.

    Positions are always 2-tuples ``(x_feet, y_feet)`` — the one forward
    commitment to the coming grid; a one-axis fight emits ``(feet, 0)``.
    """

    def test_a_move_carries_origin_destination_and_cost_as_positions(self) -> None:
        encounter, rng = build_encounter(
            [fighter("Thora", position=0), make_monster("Ogre", label="Ogre", position=60)],
            seed=SEED,
        )
        advance_to(encounter, "Thora", rng)
        events = encounter.act(Action(kind=ActionKind.MOVE, to_position=10), rng)
        move = next(e for e in events if e.kind == "move")
        assert move.data["origin"] == (0, 0)
        assert move.data["destination"] == (10, 0)
        assert move.data["cost"] == 10

    def test_damage_carries_the_amount_and_the_resulting_hit_points(self) -> None:
        encounter, _ = played_out()
        for event in encounter.log:
            if event.kind != "damage":
                continue
            creature = encounter.creatures[event.target]
            assert event.data["amount"] > 0
            assert f"{event.data['amount']} damage" in event.detail
            assert event.data["max_hp"] == creature.max_hp
            assert 0 <= event.data["hp"] <= event.data["max_hp"]

    def test_an_attack_payload_agrees_with_its_own_prose(self) -> None:
        encounter, _ = played_out()
        attacks = [
            e for e in encounter.log
            if e.kind in ("attack", "opportunity_attack") and not e.data.get("out_of_range")
        ]
        assert attacks, "the fight never swung"
        for event in attacks:
            data = event.data
            assert event.detail.startswith(f"{data['attack']}:")
            if data["critical"]:
                assert data["hit"]
                assert "critical hit" in event.detail
            elif data["hit"]:
                assert "hit" in event.detail
            else:
                assert "miss" in event.detail
                assert data["damage"] == 0
            assert 1 <= data["natural"] <= 20
            assert data["advantage"] in ("none", "advantage", "disadvantage")

    def test_a_cast_names_its_spell_slot_center_and_targets(self) -> None:
        encounter, _ = played_out()
        cast = next(e for e in encounter.log if e.kind == "cast" and e.data["center"])
        assert cast.data["spell"] == "Fireball"
        assert cast.data["slot_level"] == 3
        assert cast.data["center"] == (32, 0)
        assert set(cast.data["targets"]) == {"Ogre", "Goblin"}

    def test_every_event_serialises_to_json(self) -> None:
        encounter, _ = played_out()
        for event in encounter.log:
            payload = json.dumps(event.as_dict())
            assert json.loads(payload)["kind"] == event.kind


def replayed(original: Encounter, fresh: Encounter, rng: Random) -> Encounter:
    """Apply ``original``'s action records to a freshly built ``fresh``."""
    for record in original.actions:
        if record.action is None:
            fresh.advance(rng)
        else:
            fresh.act(record.action, rng)
    return fresh


class TestActionRecords:
    def test_each_successful_call_appends_exactly_one_record(self) -> None:
        encounter, rng = build_encounter(
            [fighter("Thora", position=0), make_monster("Ogre", label="Ogre", position=5)],
            seed=SEED,
        )
        assert encounter.actions == []
        advance_to(encounter, "Thora", rng)
        advances = len(encounter.actions)
        assert all(r.action is None for r in encounter.actions)
        encounter.act(Action(kind=ActionKind.ATTACK, target="Ogre"), rng)
        encounter.act(Action(kind=ActionKind.MOVE, to_position=15), rng)
        assert len(encounter.actions) == advances + 2
        assert [r.index for r in encounter.actions] == list(range(advances + 2))
        last = encounter.actions[-1]
        assert last.actor == "Thora"
        assert last.action == Action(kind=ActionKind.MOVE, to_position=15)

    def test_a_refused_action_records_nothing(self) -> None:
        encounter, rng = build_encounter(
            [fighter("Thora", position=0), make_monster("Ogre", label="Ogre", position=5)],
            seed=SEED,
        )
        advance_to(encounter, "Thora", rng)
        before = list(encounter.actions)
        with pytest.raises(EncounterError, match="no combatant"):
            encounter.act(Action(kind=ActionKind.ATTACK, target="Nobody"), rng)
        assert encounter.actions == before

    def test_records_tile_the_log_with_no_gaps(self) -> None:
        encounter, _ = played_out()
        position = 0
        for record in encounter.actions:
            assert record.first_event == position
            position += record.event_count
        assert position == len(encounter.log)

    def test_a_record_slices_the_log_to_its_own_events(self) -> None:
        encounter, _ = played_out()
        swings = [
            r for r in encounter.actions
            if r.action is not None and r.action.kind is ActionKind.ATTACK
        ]
        assert swings, "the fight never attacked"
        for record in swings:
            events = encounter.log[record.first_event:record.first_event + record.event_count]
            assert events[0].kind == "attack"
            assert events[0].actor == record.actor

    def test_a_record_serialises_with_empty_fields_dropped(self) -> None:
        encounter, _ = played_out()
        for record in encounter.actions:
            payload = record.as_dict()
            json.dumps(payload)
            if record.action is None:
                assert payload["action"] is None
            else:
                assert payload["action"]["kind"] == record.action.kind.value
                assert None not in payload["action"].values()


@pytest.fixture(scope="module")
def corpus_alone() -> ContentRegistry:
    """The fifty-pack corpus with the bundled slice excluded — every condition a
    plain string, no name secretly answering to an enum member."""
    return load_packs([CORPUS], builtin="exclude", include_environment=False)


class TestReplayFromRecords:
    """The phase contract: same seed, same combatants, apply the records in
    order, and the rebuilt log equals the original exactly."""

    def test_a_policy_driven_duel_replays_exactly(self) -> None:
        def duel() -> list[Creature]:
            return [
                fighter("Thora", position=0),
                make_monster("Goblin Warrior", label="Goblin", position=15),
            ]

        original, rng = build_encounter(duel(), seed=SEED, spellbook=spellbook())
        run_encounter(original, rng, max_rounds=20)
        assert original.over, "the duel should conclude"

        rebuilt, fresh_rng = build_encounter(duel(), seed=SEED, spellbook=spellbook())
        replayed(original, rebuilt, fresh_rng)
        assert [e.as_dict() for e in rebuilt.log] == [e.as_dict() for e in original.log]
        assert [r.as_dict() for r in rebuilt.actions] == [
            r.as_dict() for r in original.actions
        ]

    def test_a_scripted_brawl_replays_exactly(self) -> None:
        original, _ = played_out()
        rebuilt, rng = build_encounter(
            brawl(), seed=SEED, spellbook=spellbook(), items=ITEMS
        )
        replayed(original, rebuilt, rng)
        assert [e.as_dict() for e in rebuilt.log] == [e.as_dict() for e in original.log]

    def test_custom_pack_content_rides_through_replay(
        self, corpus_alone: ContentRegistry
    ) -> None:
        def combatants() -> list[Creature]:
            attacker = make_creature(
                "Shatterhorn Scree-Hawk", registry=corpus_alone, label="A", team="a"
            )
            target = make_creature(
                "Shatterhorn Ram", registry=corpus_alone, label="B", team="b"
            )
            attacker.items = {"Hawk-Feather Charm": 1}
            target.position = 5
            return [attacker, target]

        def build() -> tuple[Encounter, Random]:
            return build_encounter(
                combatants(), seed=SEED,
                spellbook=corpus_alone.spells,
                items=corpus_alone.items,
                condition_effects=corpus_alone.condition_effects,
            )

        original, rng = build()
        advance_to(original, "A", rng)
        original.act(
            Action(kind=ActionKind.USE_ITEM, item="Hawk-Feather Charm", target="A"), rng
        )
        run_encounter(original, rng, max_rounds=20)
        # The point of this fixture: a plain-string condition is in play, so the
        # replay is exercising the injected table rather than the builtin enum.
        assert any("shatterhorn-hawk-eyed" in e.detail for e in original.log)

        rebuilt, fresh_rng = build()
        replayed(original, rebuilt, fresh_rng)
        assert [e.as_dict() for e in rebuilt.log] == [e.as_dict() for e in original.log]
