"""The structured event log: stamps, payloads, action records, replay.

The log is the replay foundation: every event knows where in the fight it
happened, every payload carries the facts its prose ``detail`` narrates, and the
recorded actions plus the seed reproduce the log exactly. Each class here pins
one layer of that contract.
"""

from __future__ import annotations

from random import Random

from fivee_sim.analytics.montecarlo import run_encounter
from fivee_sim.data import make_monster, spellbook
from fivee_sim.kernel.dice import Dice
from fivee_sim.kernel.items import ItemEffect
from fivee_sim.model.creature import Creature
from fivee_sim.model.encounter import (
    EVENT_KINDS,
    Action,
    ActionKind,
    Encounter,
    build_encounter,
)

from .test_encounter import advance_to, caster, fighter

SEED = 20260730
FIXTURE = "synthetic test fixture, not SRD content"

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
