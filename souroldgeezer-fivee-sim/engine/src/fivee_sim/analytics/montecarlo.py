"""Monte Carlo analytics.

These replay the encounter stepper rather than reimplementing combat. That is the
whole point: if the analytics had their own resolution code, the numbers could
drift from what live play does, and a simulator whose statistics disagree with its
own rules is worse than useless.

Iteration ``i`` uses ``seed + i``, so iteration 0 uses the seed exactly. That is
what lets a one-iteration batch be compared against a single hand-driven
encounter at the same seed — an invariant the tests pin.

The auto-play policy is greedy and, importantly, deterministic: it consumes no
randomness, so the RNG stream is identical to a live encounter making the same
choices. It picks the action with the highest expected damage this turn, using the
exact arithmetic in :mod:`.expectation`.

What the policy cannot do bounds what these numbers mean, so it is stated on
``auto_action`` itself rather than left to be discovered: it does not husband spell
slots, never casts a spell that deals no damage, and never uses an item.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from random import Random
from typing import Any

from ..kernel.conditions import ConditionTable
from ..kernel.dice import Dice
from ..kernel.items import ItemEffect
from ..kernel.spells import Spell
from ..model.creature import Creature
from ..model.encounter import Action, ActionKind, Encounter, EncounterError
from .expectation import attack_damage_expectation, save_damage_expectation

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


@dataclass(frozen=True, slots=True)
class _Option:
    """One action the policy could take, and what it is worth."""

    value: float
    tiebreak: str
    action: Action


def auto_action(encounter: Encounter) -> Action | None:
    """Choose the next action for the creature whose turn it is, or ``None`` to stop.

    The policy is greedy on **expected damage this turn**. That expectation is
    computed exactly by :mod:`.expectation` — real hit, crit, and save
    probabilities, with the kernel's own rounding — and then capped at what the
    target actually has left, so overkill is not counted as value. Ties break on a
    stable string, never on dict ordering, and nothing here consumes randomness:
    that is what keeps a batch's RNG stream identical to live play making the same
    choices.

    Three limits are deliberate, and analytics built on this inherit them:

    * **It does not husband spell slots.** The highest-value cast available is the
      one taken, so a caster spends its best slots first and fights on with a
      weapon once they are gone.
    * **It never casts a spell that deals no damage.** Hold Person is loaded,
      implemented, and still never chosen here, because valuing a condition means
      modelling the turns it buys the rest of the party — which a one-turn greedy
      policy cannot see. A batch is a floor for a control build, not a measure of it.
    * **It does not use items.** A potion is never drunk in a batch.
    """
    actor = encounter.current
    if not actor.active:
        return None
    enemies = [c for c in encounter.enemies_of(actor.name) if c.conscious]
    if not enemies:
        return None

    turn = encounter.state()["turn_state"]
    options: list[_Option] = []
    # Starting an Attack action needs the action still in hand; continuing a
    # Multiattack already under way does not.
    attack_started = turn["attacks_left"] < actor.attacks_per_action
    if turn["attacks_left"] > 0 and (attack_started or not turn["action_used"]):
        options.extend(_attack_options(encounter, actor, enemies))
    if not turn["action_used"]:
        options.extend(_spell_options(encounter, actor, enemies))

    if options:
        best = min(options, key=lambda option: (-option.value, option.tiebreak))
        if best.value > 0.0:
            return best.action
    return _closing_move(encounter, actor, enemies, turn)


def _attack_options(
    encounter: Encounter, actor: Creature, enemies: Sequence[Creature]
) -> list[_Option]:
    """Every attack the actor could make right now, valued."""
    options: list[_Option] = []
    for option in actor.attacks:
        reach = option.max_distance()
        for target in enemies:
            if actor.distance_to(target) > reach:
                continue
            expected = attack_damage_expectation(
                attack_bonus=option.attack_bonus,
                target_ac=target.ac,
                damage=option.damage,
                advantage=encounter.attack_advantage(actor, target, option),
                forced_critical=encounter.attack_forced_critical(actor, target, option),
                resisted=target.resists(option.damage_type),
                vulnerable=option.damage_type in target.vulnerabilities,
                immune=option.damage_type in target.immunities,
            )
            options.append(
                _Option(
                    value=min(expected, float(target.hp)),
                    tiebreak=f"attack:{target.name}:{option.name}",
                    action=Action(
                        kind=ActionKind.ATTACK, target=target.name, attack=option.name
                    ),
                )
            )
    return options


def _castable_slots(actor: Creature, spell: Spell) -> list[int]:
    """Slot levels this actor could cast ``spell`` at, lowest first."""
    if spell.level == 0:
        return [0]
    return sorted(
        level
        for level, count in actor.spell_slots.items()
        if count > 0 and level >= spell.level
    )


def _spell_options(
    encounter: Encounter, actor: Creature, enemies: Sequence[Creature]
) -> list[_Option]:
    """Every damaging cast the actor could make right now, valued."""
    options: list[_Option] = []
    for name in actor.spells:
        spell = encounter.spellbook.get(name)
        if spell is None or spell.damage is None:
            continue
        for slot_level in _castable_slots(actor, spell):
            dice = spell.damage_at(slot_level)
            if dice is None:
                continue
            if spell.radius:
                placed = _area_option(encounter, actor, spell, slot_level, dice)
                if placed is not None:
                    options.append(placed)
                continue
            for target in enemies:
                if spell.range_feet and actor.distance_to(target) > spell.range_feet:
                    continue
                if spell.requires_attack_roll:
                    expected = attack_damage_expectation(
                        attack_bonus=actor.spell_attack_bonus,
                        target_ac=target.ac,
                        damage=dice,
                        resisted=_resists(target, spell),
                        vulnerable=_vulnerable(target, spell),
                        immune=_immune(target, spell),
                    )
                else:
                    expected = _save_expectation(encounter, actor, spell, dice, target)
                options.append(
                    _Option(
                        value=min(expected, float(target.hp)),
                        tiebreak=f"cast:{spell.name}:{slot_level}:{target.name}",
                        action=Action(
                            kind=ActionKind.CAST,
                            spell=spell.name,
                            slot_level=slot_level,
                            targets=(target.name,),
                        ),
                    )
                )
    return options


def _area_option(
    encounter: Encounter, actor: Creature, spell: Spell, slot_level: int, dice: Dice
) -> _Option | None:
    """Best point of origin for an area spell, or ``None`` if no placement is worth it.

    Only the endpoints of each creature's catchment need testing. A creature at
    ``p`` is caught exactly when the origin lies in ``[p - radius, p + radius]``, so
    the set of creatures caught only changes at those endpoints — testing them is
    exhaustive, not a sample.

    Allies caught in the blast, the caster included, count *against* the placement.
    """
    candidates: set[int] = set()
    for creature in encounter.creatures.values():
        if not creature.conscious:
            continue
        for offset in (-spell.radius, 0, spell.radius):
            centre = creature.position + offset
            if spell.range_feet and abs(centre - actor.position) > spell.range_feet:
                continue
            candidates.add(centre)

    best: _Option | None = None
    for centre in sorted(candidates):
        value = 0.0
        for creature in encounter.creatures.values():
            if not creature.conscious:
                continue
            if abs(creature.position - centre) > spell.radius:
                continue
            expected = _save_expectation(encounter, actor, spell, dice, creature)
            share = min(expected, float(creature.hp))
            value += -share if creature.team == actor.team else share
        if best is None or value > best.value:
            best = _Option(
                value=value,
                tiebreak=f"cast:{spell.name}:{slot_level}:@{centre}",
                action=Action(
                    kind=ActionKind.CAST,
                    spell=spell.name,
                    slot_level=slot_level,
                    center=centre,
                ),
            )
    return best


def _resists(target: Creature, spell: Spell) -> bool:
    return target.resists(spell.damage_type) if spell.damage_type is not None else False


def _vulnerable(target: Creature, spell: Spell) -> bool:
    return spell.damage_type is not None and spell.damage_type in target.vulnerabilities


def _immune(target: Creature, spell: Spell) -> bool:
    return spell.damage_type is not None and spell.damage_type in target.immunities


def _save_expectation(
    encounter: Encounter, actor: Creature, spell: Spell, dice: Dice, target: Creature
) -> float:
    """Expected damage a save-based (or no-save) spell deals ``target``."""
    return save_damage_expectation(
        save_dc=actor.spell_save_dc,
        save_modifier=(
            target.save_modifier(spell.save_ability)
            if spell.save_ability is not None
            else 0
        ),
        damage=dice,
        half_on_save=spell.half_on_save,
        has_save=spell.save_ability is not None,
        auto_fail=encounter.auto_fails_save(target, spell.save_ability),
        resisted=_resists(target, spell),
        vulnerable=_vulnerable(target, spell),
        immune=_immune(target, spell),
    )


def _closing_move(
    encounter: Encounter,
    actor: Creature,
    enemies: Sequence[Creature],
    turn: dict[str, Any],
) -> Action | None:
    """Step toward the nearest enemy, if the actor has any way to threaten one."""
    if turn["movement_left"] <= 0:
        return None
    desired = _threat_range(encounter, actor)
    if desired is None:
        return None
    target = min(
        enemies, key=lambda creature: (actor.distance_to(creature), creature.name)
    )
    distance = actor.distance_to(target)
    if distance <= desired:
        return None
    step = min(turn["movement_left"], distance - desired)
    if step <= 0:
        return None
    direction = 1 if target.position > actor.position else -1
    return Action(kind=ActionKind.MOVE, to_position=actor.position + direction * step)


def _threat_range(encounter: Encounter, actor: Creature) -> int | None:
    """Furthest the actor can threaten from, or ``None`` if closing gains it nothing.

    ``None`` covers both having no way to hurt anyone and holding a spell with no
    range limit — the engine treats a falsy ``range_feet`` as unbounded, so there is
    nothing to close to.
    """
    reaches = [option.max_distance() for option in actor.attacks]
    for name in actor.spells:
        spell = encounter.spellbook.get(name)
        if spell is None or spell.damage is None:
            continue
        if not _castable_slots(actor, spell):
            continue
        if not spell.range_feet:
            return None
        reaches.append(spell.range_feet)
    return max(reaches) if reaches else None


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
        # ``advance`` ticks the round over before the guard above fails, so an
        # unfinished fight would otherwise report having run max_rounds + 1 rounds.
        rounds=min(encounter.round, max_rounds),
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
    items: dict[str, ItemEffect] | None = None,
    condition_effects: ConditionTable | None = None,
) -> dict[str, Any]:
    """Auto-play the same encounter ``iterations`` times and summarise the outcomes.

    The content tables are arguments rather than something resolved per iteration:
    a batch that reloaded content while running would stop being reproducible from
    its seed, which is the one property these numbers rest on.

    **Pass ``spellbook`` if any combatant casts.** The policy looks its spells up
    there and skips what it cannot find, so omitting it does not fail — it returns a
    plausible batch in which nobody cast anything. The MCP tool always supplies it;
    a direct caller has to remember.

    ``wins`` counts ``"none"`` only for a mutual wipe, where the last combatants on
    both sides fall together. A fight still going at ``max_rounds`` is counted in
    ``timed_out`` instead.
    """
    if iterations < 1:
        raise ValueError(f"iterations must be at least 1: {iterations}")
    wins: dict[str, int] = {}
    round_counts: list[float] = []
    timeouts = 0
    for index in range(iterations):
        rng = Random(seed + index)
        encounter = Encounter(
            list(factory()),
            rng,
            spellbook=spellbook,
            items=items,
            condition_effects=condition_effects,
        )
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
    distance: int = 5,
    target_name: str = "Target",
    spellbook: dict[str, Spell] | None = None,
    items: dict[str, ItemEffect] | None = None,
    condition_effects: ConditionTable | None = None,
) -> dict[str, Any]:
    """Damage a build lands over ``rounds`` against a passive target of ``target_ac``.

    The target is a dummy with enough hit points to absorb the whole run, so damage
    is measured rather than truncated by the target dropping. It is driven through
    the real encounter stepper, so advantage, criticals, and resistances all apply
    exactly as they would in play.

    ``distance`` is how far away the dummy stands, and it is part of the question
    rather than a detail: a build is measured at a range, not in the abstract. The
    default of 5 ft keeps a melee build in reach.

    It must be greater than zero for an area caster. The policy will not centre a
    blast that catches the caster, and on one axis a dummy standing *on* the
    attacker admits no placement that catches one and not the other — so the spell
    is never cast and the build measures as though it had none. Any real separation
    is enough: the origin goes on the far side of the target. The returned
    ``actions`` breakdown reports what the build actually did, so a case like that
    reads as "cast nothing" rather than as a mysteriously small number.
    """
    if iterations < 1:
        raise ValueError(f"iterations must be at least 1: {iterations}")
    totals: list[float] = []
    taken: dict[str, int] = {}
    for index in range(iterations):
        rng = Random(seed + index)
        attacker = attacker_factory()
        dummy = Creature(
            name=target_name,
            team="dummy",
            ac=target_ac,
            max_hp=10_000,
            speed=0,
            position=attacker.position + distance,
            provenance="synthetic test dummy, not SRD content",
        )
        encounter = Encounter(
            [attacker, dummy],
            rng,
            spellbook=spellbook,
            items=items,
            condition_effects=condition_effects,
        )
        # ``__init__`` has already begun a turn for whoever won Initiative, so the
        # budget in hand is theirs. Read that before the order is rewritten below,
        # which does not rebuild it.
        began_for = encounter.current_name
        # Force the attacker to act first: initiative is irrelevant to a damage
        # measurement, and a passive dummy would otherwise waste a turn.
        encounter.order = [attacker.name, dummy.name]
        encounter.turn_index = 0
        if began_for != attacker.name:
            # Begin the attacker's turn through the stepper's own setup — the same
            # call ``advance`` makes — rather than restating what a turn grants.
            # Without this, round 1 ran on the dummy's budget: no movement, and a
            # single swing however many the Attack action allows. Guarded because
            # ``_begin_turn`` is not idempotent — it rolls a death save for a dying
            # creature, and a turn is worth one of those, not two.
            encounter._begin_turn(rng)
        for _ in range(rounds):
            for _ in range(MAX_ACTIONS_PER_TURN):
                action = auto_action(encounter)
                if action is None:
                    break
                try:
                    encounter.act(action, rng)
                except EncounterError:
                    break
                label = action.attack or action.spell or action.kind.value
                taken[f"{action.kind.value}:{label}"] = (
                    taken.get(f"{action.kind.value}:{label}", 0) + 1
                )
            encounter.advance(rng)  # dummy's turn
            encounter.advance(rng)  # back to the attacker
        totals.append(float(dummy.max_hp - dummy.hp))
    summary = summarise(totals)
    return {
        "iterations": iterations,
        "seed": seed,
        "rounds": rounds,
        "distance": distance,
        "target_ac": target_ac,
        "damage": summary.as_dict(),
        "damage_per_round": round(summary.mean / rounds, 3) if rounds else 0.0,
        # What the build actually did. A policy blind spot shows up here as an
        # action never taken, rather than as a quietly small damage figure.
        "actions": dict(sorted(taken.items())),
    }
