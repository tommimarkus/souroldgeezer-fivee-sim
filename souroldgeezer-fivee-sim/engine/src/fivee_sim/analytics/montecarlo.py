"""Monte Carlo analytics.

These replay the encounter stepper rather than reimplementing combat. That is the
whole point: if the analytics had their own resolution code, the numbers could
drift from what live play does, and a simulator whose statistics disagree with its
own rules is worse than useless.

Iteration ``i`` uses ``seed + i``, so iteration 0 uses the seed exactly. That is
what lets a one-iteration batch be compared against a single hand-driven
encounter at the same seed — an invariant the tests pin.

The auto-play policy is deliberately simple and, importantly, deterministic: it
consumes no randomness, so the RNG stream is identical to a live encounter making
the same choices.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from random import Random
from typing import Any

from ..kernel.spells import Spell
from ..model.creature import Creature
from ..model.encounter import Action, ActionKind, Encounter, EncounterError

#: Safety valve: the most actions one turn may generate before we stop asking.
MAX_ACTIONS_PER_TURN = 12

CombatantFactory = Callable[[], Sequence[Creature]]


@dataclass(frozen=True, slots=True)
class Stats:
    samples: int
    mean: float
    median: float
    p90: float
    minimum: float
    maximum: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "samples": self.samples,
            "mean": round(self.mean, 3),
            "median": self.median,
            "p90": self.p90,
            "min": self.minimum,
            "max": self.maximum,
        }


def summarise(values: Sequence[float]) -> Stats:
    if not values:
        return Stats(samples=0, mean=0.0, median=0.0, p90=0.0, minimum=0.0, maximum=0.0)
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1)))))
    return Stats(
        samples=len(ordered),
        mean=statistics.fmean(ordered),
        median=statistics.median(ordered),
        p90=ordered[index],
        minimum=ordered[0],
        maximum=ordered[-1],
    )


def auto_action(encounter: Encounter) -> Action | None:
    """Choose the next action for the creature whose turn it is, or ``None`` to stop.

    Targeting is lowest hit points then name, so it never depends on dict ordering.
    """
    actor = encounter.current
    if not actor.active:
        return None
    enemies = encounter.enemies_of(actor.name)
    if not enemies:
        return None
    target = min(enemies, key=lambda creature: (creature.hp, creature.name))
    distance = actor.distance_to(target)

    if actor.attacks and encounter.state()["turn_state"]["attacks_left"] > 0:
        option = actor.attacks[0]
        if distance <= option.max_distance():
            return Action(kind=ActionKind.ATTACK, target=target.name, attack=option.name)

    turn = encounter.state()["turn_state"]
    if not turn["action_used"]:
        castable = _best_damaging_spell(encounter, actor, distance)
        if castable is not None:
            spell, slot_level = castable
            return Action(
                kind=ActionKind.CAST,
                spell=spell.name,
                slot_level=slot_level,
                targets=(target.name,),
            )

    if actor.attacks and turn["movement_left"] > 0:
        option = actor.attacks[0]
        reach = option.max_distance()
        if distance > reach:
            step = min(turn["movement_left"], distance - reach)
            if step > 0:
                direction = 1 if target.position > actor.position else -1
                return Action(
                    kind=ActionKind.MOVE,
                    to_position=actor.position + direction * step,
                )
    return None


def _best_damaging_spell(
    encounter: Encounter, actor: Creature, distance: int
) -> tuple[Spell, int] | None:
    """Highest slot level a known damaging spell can be cast at, if any is in range."""
    best: tuple[Spell, int] | None = None
    for name in actor.spells:
        spell = encounter.spellbook.get(name)
        if spell is None or spell.damage is None:
            continue
        if spell.range_feet and distance > spell.range_feet:
            continue
        levels = [
            level for level, count in actor.spell_slots.items()
            if count > 0 and level >= spell.level
        ]
        if spell.level == 0:
            candidate = (spell, 0)
        elif levels:
            candidate = (spell, max(levels))
        else:
            continue
        if best is None or candidate[1] > best[1]:
            best = candidate
    return best


@dataclass(frozen=True, slots=True)
class Outcome:
    winner: str | None
    rounds: int
    survivors: dict[str, int]
    timed_out: bool


def run_encounter(encounter: Encounter, rng: Random, *, max_rounds: int = 20) -> Outcome:
    """Auto-play an encounter to a conclusion or until ``max_rounds`` elapses."""
    while not encounter.over and encounter.round <= max_rounds:
        for _ in range(MAX_ACTIONS_PER_TURN):
            action = auto_action(encounter)
            if action is None:
                break
            try:
                encounter.act(action, rng)
            except EncounterError:
                break
            if encounter.over:
                break
        if encounter.over:
            break
        encounter.advance(rng)
    return Outcome(
        winner=encounter.winner,
        rounds=encounter.round,
        survivors={
            creature.name: creature.hp
            for creature in encounter.creatures.values()
            if creature.conscious
        },
        timed_out=not encounter.over,
    )


def simulate_rounds(
    factory: CombatantFactory,
    *,
    iterations: int,
    seed: int,
    max_rounds: int = 20,
    spellbook: dict[str, Spell] | None = None,
) -> dict[str, Any]:
    """Auto-play the same encounter ``iterations`` times and summarise the outcomes."""
    if iterations < 1:
        raise ValueError(f"iterations must be at least 1: {iterations}")
    wins: dict[str, int] = {}
    round_counts: list[float] = []
    timeouts = 0
    for index in range(iterations):
        rng = Random(seed + index)
        encounter = Encounter(list(factory()), rng, spellbook=spellbook)
        outcome = run_encounter(encounter, rng, max_rounds=max_rounds)
        key = outcome.winner if outcome.winner is not None else "none"
        wins[key] = wins.get(key, 0) + 1
        round_counts.append(float(outcome.rounds))
        if outcome.timed_out:
            timeouts += 1
    return {
        "iterations": iterations,
        "seed": seed,
        "max_rounds": max_rounds,
        "wins": dict(sorted(wins.items())),
        "win_rate": {
            team: round(count / iterations, 4) for team, count in sorted(wins.items())
        },
        "rounds": summarise(round_counts).as_dict(),
        "timed_out": timeouts,
    }


def simulate_dpr(
    attacker_factory: Callable[[], Creature],
    *,
    target_ac: int,
    rounds: int = 3,
    iterations: int = 1000,
    seed: int = 0,
    target_name: str = "Target",
    spellbook: dict[str, Spell] | None = None,
) -> dict[str, Any]:
    """Damage a build lands over ``rounds`` against a passive target of ``target_ac``.

    The target is a dummy with enough hit points to absorb the whole run, so damage
    is measured rather than truncated by the target dropping. It is driven through
    the real encounter stepper, so advantage, criticals, and resistances all apply
    exactly as they would in play.
    """
    if iterations < 1:
        raise ValueError(f"iterations must be at least 1: {iterations}")
    totals: list[float] = []
    for index in range(iterations):
        rng = Random(seed + index)
        attacker = attacker_factory()
        dummy = Creature(
            name=target_name,
            team="dummy",
            ac=target_ac,
            max_hp=10_000,
            speed=0,
            position=attacker.position,
            provenance="synthetic test dummy, not SRD content",
        )
        encounter = Encounter([attacker, dummy], rng, spellbook=spellbook)
        # Force the attacker to act first: initiative is irrelevant to a damage
        # measurement, and a passive dummy would otherwise waste a turn.
        encounter.order = [attacker.name, dummy.name]
        encounter.turn_index = 0
        for _ in range(rounds):
            for _ in range(MAX_ACTIONS_PER_TURN):
                action = auto_action(encounter)
                if action is None:
                    break
                try:
                    encounter.act(action, rng)
                except EncounterError:
                    break
            encounter.advance(rng)  # dummy's turn
            encounter.advance(rng)  # back to the attacker
        totals.append(float(dummy.max_hp - dummy.hp))
    summary = summarise(totals)
    return {
        "iterations": iterations,
        "seed": seed,
        "rounds": rounds,
        "target_ac": target_ac,
        "damage": summary.as_dict(),
        "damage_per_round": round(summary.mean / rounds, 3) if rounds else 0.0,
    }
