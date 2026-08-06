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
slots, never casts a spell that deals no damage, and values only healing items.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from random import Random
from typing import Any

from ..kernel.actions import MELEE_THRESHOLD
from ..kernel.conditions import ConditionTable
from ..kernel.dice import Dice
from ..kernel.grid import (
    FEET_PER_SQUARE,
    CoverGrade,
    DiagonalRule,
    MovementMode,
    Point,
    Square,
    TerrainTable,
    as_point,
    cover_ac_bonus,
    distance_feet,
    sphere_squares,
    square_center,
    to_square,
)
from ..kernel.items import ActionCost, ItemEffect
from ..kernel.spells import Spell, SpellShape
from ..model.battlemap import BattleMap
from ..model.creature import AttackOption, Creature
from ..model.encounter import Action, ActionKind, Encounter, EncounterError
from .expectation import attack_damage_expectation, save_damage_expectation

#: Safety valve: the most actions one turn may generate before we stop asking.
MAX_ACTIONS_PER_TURN = 12

CombatantFactory = Callable[[], Sequence[Creature]]


def _ammunition_names(creature: Creature) -> set[str]:
    """Item names ``creature`` fires rather than drinks, worn, or wields.

    Derived from the creature's own attacks — never a hardcoded list — so a
    pack's own ammunition splits out of ``items_spent`` exactly as the bundled
    Arrow and Bolt do.
    """
    return {
        option.ammunition for option in creature.attacks if option.ammunition is not None
    }


@dataclass(frozen=True, slots=True)
class Stats:
    samples: int
    mean: float
    median: float
    p10: float
    p90: float
    minimum: float
    maximum: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "samples": self.samples,
            "mean": round(self.mean, 3),
            "median": self.median,
            "p10": self.p10,
            "p90": self.p90,
            "min": self.minimum,
            "max": self.maximum,
        }


def summarise(values: Sequence[float]) -> Stats:
    if not values:
        return Stats(
            samples=0,
            mean=0.0,
            median=0.0,
            p10=0.0,
            p90=0.0,
            minimum=0.0,
            maximum=0.0,
        )
    ordered = sorted(values)
    p10_index = max(0, min(len(ordered) - 1, int(round(0.1 * (len(ordered) - 1)))))
    p90_index = max(0, min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1)))))
    return Stats(
        samples=len(ordered),
        mean=statistics.fmean(ordered),
        median=statistics.median(ordered),
        p10=ordered[p10_index],
        p90=ordered[p90_index],
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
    * **It never casts a non-healing spell that deals no damage.** Hold Person is loaded,
      implemented, and still never chosen here, because valuing a condition means
      modelling the turns it buys the rest of the party — which a one-turn greedy
      policy cannot see. A batch is a floor for a control build, not a measure of it.
    * **It uses healing items at half hit points or below.** It does not value
      other item effects.
    * **It does not value an attack's on-hit condition rider.** The damage riders
      are priced exactly — the bonus pool against its own defenses, the Advantage
      dice under the resolved advantage state — but a poison the hit would apply
      counts for nothing, for the same reason Hold Person is never cast: its worth
      lives in turns this one-turn policy cannot see. The stepper still applies it
      when the swing lands.

    One reflex comes before the greedy valuation: **a Prone creature stands
    first**, whenever the stepper would allow it. Standing costs half the
    creature's Speed and no action, and staying down costs Disadvantage on every
    attack while handing melee attackers Advantage — a trade no one-turn damage
    comparison needs to price. Legality comes from :meth:`Encounter.can_stand`,
    the stepper's own gate, and the act consumes no randomness, so the RNG
    stream stays identical to live play.
    """
    actor = encounter.current
    if not actor.active:
        return None
    if actor.surrender_when_last and not any(
        creature.team == actor.team
        and creature.name != actor.name
        and creature.combat_active
        for creature in encounter.creatures.values()
    ):
        return Action(kind=ActionKind.SURRENDER)
    enemies = [c for c in encounter.enemies_of(actor.name) if c.conscious]
    if not enemies:
        return None
    if encounter.can_stand(actor.name):
        return Action(kind=ActionKind.STAND)

    turn = encounter.state()["turn_state"]
    healing = _healing_action(encounter, actor, turn)
    if healing is not None:
        return healing
    options: list[_Option] = []
    # Starting an Attack action needs the action still in hand; continuing a
    # Multiattack already under way does not.
    attack_started = turn["attacks_left"] < actor.attacks_per_action
    if turn["attacks_left"] > 0 and (attack_started or not turn["action_used"]):
        options.extend(_attack_options(encounter, actor, enemies, turn))
    if not turn["action_used"]:
        options.extend(_spell_options(encounter, actor, enemies))

    if options:
        best = min(options, key=lambda option: (-option.value, option.tiebreak))
        if best.value > 0.0:
            return best.action
    closing = _closing_move(encounter, actor, enemies, turn)
    if closing is not None:
        return closing
    return _closing_dash(encounter, actor, enemies, turn)


def _loaded(actor: Creature, option: AttackOption, turn: dict[str, Any]) -> bool:
    """Whether the stepper would let ``actor`` make this attack right now.

    Mirrors the two refusals :meth:`Encounter._require_loaded` raises —
    an empty magazine and a Loading weapon already fired this turn — so
    neither the valuation in :func:`_attack_options` nor the reach the
    closing moves chase in :func:`_threat_range` ever counts on an attack
    the stepper would refuse.
    """
    if option.ammunition is not None and actor.items.get(option.ammunition, 0) <= 0:
        return False
    return not (option.loading and turn["loading_used"])


def _attack_options(
    encounter: Encounter, actor: Creature, enemies: Sequence[Creature], turn: dict[str, Any]
) -> list[_Option]:
    """Every attack the actor could make right now, valued.

    Cover comes from :meth:`Encounter.cover_between` — the stepper's own
    authority — so a target the stepper would refuse (total cover) is never
    proposed, and a screened one is valued at its actual, raised AC. An
    attack the stepper would refuse for want of ammunition or a spent Loading
    shot is filtered the same way, by :func:`_loaded` — otherwise the policy
    keeps proposing a refused shot, the stepper's ``EncounterError`` breaks
    the turn's action loop, and the actor silently stops acting.
    """
    options: list[_Option] = []
    for option in actor.attacks:
        if not _loaded(actor, option, turn):
            continue
        reach = option.max_distance()
        for target in enemies:
            if actor.distance_to(target, encounter.movement_rule) > reach:
                continue
            grade = encounter.cover_between(actor.name, target.name)
            if grade is CoverGrade.TOTAL:
                continue
            expected = attack_damage_expectation(
                attack_bonus=actor.attack_modifier(option.attack_bonus),
                target_ac=target.ac + cover_ac_bonus(grade),
                damage=option.damage,
                advantage=encounter.attack_advantage(actor, target, option),
                forced_critical=encounter.attack_forced_critical(actor, target),
                resisted=target.resists(option.damage_type),
                vulnerable=option.damage_type in target.vulnerabilities,
                immune=option.damage_type in target.immunities,
                advantage_bonus_damage=option.advantage_bonus_damage,
                advantage_bonus_damage_applies=(
                    option.advantage_bonus_with_adjacent_ally
                    and any(
                        ally is not actor
                        and ally.team == actor.team
                        and ally.active
                        and ally.distance_to(target, encounter.movement_rule)
                        <= MELEE_THRESHOLD
                        for ally in encounter.creatures.values()
                    )
                ),
                bonus_damage=option.bonus_damage,
                bonus_resisted=(
                    target.resists(option.bonus_damage_type)
                    if option.bonus_damage_type is not None else False
                ),
                bonus_vulnerable=(
                    option.bonus_damage_type in target.vulnerabilities
                    if option.bonus_damage_type is not None else False
                ),
                bonus_immune=(
                    option.bonus_damage_type in target.immunities
                    if option.bonus_damage_type is not None else False
                ),
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


def _healing_action(
    encounter: Encounter, actor: Creature, turn: dict[str, Any]
) -> Action | None:
    """Revive a downed ally, or shore up one at half hit points or below."""
    allies = [
        creature
        for creature in encounter.creatures.values()
        if creature.team == actor.team
        and not creature.dead
        and creature.hp < creature.max_hp
        and (creature.hp == 0 or creature.hp * 2 <= creature.max_hp)
    ]
    if not allies:
        return None
    allies.sort(
        key=lambda creature: (
            creature.hp != 0,
            creature.hp / creature.max_hp,
            creature.name,
        )
    )
    target = allies[0]
    distance = actor.distance_to(target, encounter.movement_rule)
    choices: list[tuple[float, str, Action]] = []

    if not turn["action_used"]:
        for name in actor.spells:
            spell = encounter.spellbook.get(name)
            if spell is None or spell.heal is None:
                continue
            if spell.range_feet and distance > spell.range_feet:
                continue
            for slot_level in _castable_slots(actor, spell):
                dice = spell.healing_at(slot_level)
                if dice is None:
                    continue
                value = dice.count * (dice.faces + 1) / 2 + dice.modifier
                if spell.add_spellcasting_modifier:
                    # Valued the way it will resolve. Left out, a cleric would
                    # rate its own Cure Wounds below a potion it should beat.
                    value += actor.spellcasting_modifier
                choices.append(
                    (
                        value,
                        f"spell:{name}:{slot_level}",
                        Action(
                            kind=ActionKind.CAST,
                            spell=name,
                            slot_level=slot_level,
                            target=target.name,
                        ),
                    )
                )

    for name, quantity in actor.items.items():
        effect = encounter.items.get(name)
        if quantity <= 0 or effect is None or effect.heal is None:
            continue
        if target is not actor and distance > MELEE_THRESHOLD:
            continue
        bonus = effect.action_cost is ActionCost.BONUS_ACTION
        if bonus and turn["bonus_action_used"]:
            continue
        if not bonus and turn["action_used"]:
            continue
        dice = effect.heal
        value = dice.count * (dice.faces + 1) / 2 + dice.modifier
        choices.append(
            (
                value,
                f"item:{name}",
                Action(
                    kind=ActionKind.USE_ITEM,
                    item=name,
                    target=target.name,
                    as_bonus_action=bonus,
                ),
            )
        )
    if not choices:
        return None
    return max(choices, key=lambda entry: (entry[0], entry[1]))[2]


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
            if spell.is_area:
                placed = _area_option(encounter, actor, spell, slot_level, dice)
                if placed is not None:
                    options.append(placed)
                continue
            for target in enemies:
                if spell.range_feet and (
                    actor.distance_to(target, encounter.movement_rule) > spell.range_feet
                ):
                    continue
                if spell.requires_attack_roll:
                    # Read off the encounter, never re-derived: the stepper will
                    # roll this attack under exactly these two values, and a policy
                    # that guessed them would value a Guiding Bolt at a helpless
                    # target as a flat d20 and pass over the best action it had.
                    expected = attack_damage_expectation(
                        attack_bonus=actor.attack_modifier(actor.spell_attack_bonus),
                        target_ac=target.ac,
                        damage=dice,
                        advantage=encounter.spell_attack_advantage(actor, target, spell),
                        forced_critical=encounter.attack_forced_critical(actor, target),
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


def _area_value(
    encounter: Encounter,
    actor: Creature,
    spell: Spell,
    dice: Dice,
    caught: Sequence[Creature],
) -> float:
    """What one placement is worth: enemy damage in, ally damage against.

    Each share is capped at the creature's remaining hit points, so overkill is
    not value; allies caught in the area, the caster included, subtract theirs.
    A downed creature is caught by the template and burned by the cast — that is
    the stepper's business — but it is worth nothing here: it threatens nobody,
    so the policy spends its placements on the standing.
    """
    value = 0.0
    for creature in caught:
        if not creature.conscious:
            continue
        expected = _save_expectation(encounter, actor, spell, dice, creature)
        share = min(expected, float(creature.hp))
        value += -share if creature.team == actor.team else share
    return value


def _area_option(
    encounter: Encounter, actor: Creature, spell: Spell, slot_level: int, dice: Dice
) -> _Option | None:
    """Best placement of an area spell, or ``None`` if no placement is worth it.

    Membership always comes from :meth:`Encounter.area_targets` — the same
    authority the stepper resolves the cast with — so what the policy expects to
    catch is exactly what the cast will catch. What varies by shape is the
    candidate set of placements:

    * **Spheres on a map** are exhaustive over lattice origins: any origin that
      catches at least one creature lies inside that creature's own catchment
      ball, so the union of per-creature balls (clipped to the map, the spell's
      range, and the caster's sight) contains the true optimum, and the value of
      an origin depends only on the set it catches.
    * **Spheres on the open plane** test each creature's interval endpoints —
      ``x - r``, ``x``, ``x + r`` at the creature's own ``y``. On the one-axis
      battlefield this is the historical candidate set, choice for choice; off
      the axis it is an approximation that never beats the map-borne search.
    * **Cubes** are exhaustive by the same union argument: a cube catches a
      creature exactly when its minimum corner lies in the n-by-n box below and
      left of the creature's square.
    * **Cones** have eight castable directions; trying all eight is exhaustive
      by construction.
    * **Lines** are aimed at each conscious enemy in turn — deliberately
      approximate: a line clipping two creatures while aimed between them is
      never considered, and that is documented rather than hidden.

    Candidates are iterated in sorted order and the first strict maximum wins,
    so the choice is deterministic and consumes no randomness.
    """
    match spell.effective_shape:
        case SpellShape.SPHERE:
            return _sphere_option(encounter, actor, spell, slot_level, dice)
        case SpellShape.CUBE:
            return _cube_option(encounter, actor, spell, slot_level, dice)
        case SpellShape.CONE:
            return _cone_option(encounter, actor, spell, slot_level, dice)
        case SpellShape.LINE:
            return _line_option(encounter, actor, spell, slot_level, dice)
        case _:
            return None


def _centered_option(
    encounter: Encounter,
    actor: Creature,
    spell: Spell,
    slot_level: int,
    dice: Dice,
    candidates: set[Point],
) -> _Option | None:
    """Best of the candidate centres, range- and sight-filtered, first max wins."""
    origin = as_point(actor.position)
    best: _Option | None = None
    for centre in sorted(candidates):
        if spell.range_feet and (
            distance_feet(centre, origin, encounter.movement_rule) > spell.range_feet
        ):
            continue
        if not encounter.origin_visible(actor.name, centre):
            continue
        caught = encounter.area_targets(spell, actor.name, center=centre)
        value = _area_value(encounter, actor, spell, dice, caught)
        if best is None or value > best.value:
            best = _Option(
                value=value,
                tiebreak=f"cast:{spell.name}:{slot_level}:@{centre[0]},{centre[1]}",
                action=Action(
                    kind=ActionKind.CAST,
                    spell=spell.name,
                    slot_level=slot_level,
                    center=centre,
                ),
            )
    return best


def _conscious(encounter: Encounter) -> list[Creature]:
    return [c for c in encounter.creatures.values() if c.conscious]


def _sphere_option(
    encounter: Encounter, actor: Creature, spell: Spell, slot_level: int, dice: Dice
) -> _Option | None:
    candidates: set[Point] = set()
    if encounter.battle_map is None:
        for creature in _conscious(encounter):
            px, py = as_point(creature.position)
            for offset in (-spell.radius, 0, spell.radius):
                candidates.add((px + offset, py))
    else:
        width, height = encounter.battle_map.width, encounter.battle_map.height
        squares: set[Square] = set()
        for creature in _conscious(encounter):
            squares |= sphere_squares(
                to_square(as_point(creature.position)),
                spell.radius,
                rule=encounter.movement_rule,
            )
        candidates = {
            square_center(square)
            for square in squares
            if 0 <= square[0] < width and 0 <= square[1] < height
        }
    return _centered_option(encounter, actor, spell, slot_level, dice, candidates)


def _cube_option(
    encounter: Encounter, actor: Creature, spell: Spell, slot_level: int, dice: Dice
) -> _Option | None:
    side = max(1, spell.size // FEET_PER_SQUARE)
    candidates: set[Point] = set()
    for creature in _conscious(encounter):
        sx, sy = to_square(as_point(creature.position))
        for dx in range(side):
            for dy in range(side):
                candidates.add(square_center((sx - dx, sy - dy)))
    return _centered_option(encounter, actor, spell, slot_level, dice, candidates)


def _cone_option(
    encounter: Encounter, actor: Creature, spell: Spell, slot_level: int, dice: Dice
) -> _Option | None:
    best: _Option | None = None
    for direction in sorted(_DIRECTIONS):
        caught = encounter.area_targets(spell, actor.name, direction=direction)
        value = _area_value(encounter, actor, spell, dice, caught)
        if best is None or value > best.value:
            best = _Option(
                value=value,
                tiebreak=(
                    f"cast:{spell.name}:{slot_level}:@{direction[0]},{direction[1]}"
                ),
                action=Action(
                    kind=ActionKind.CAST,
                    spell=spell.name,
                    slot_level=slot_level,
                    direction=direction,
                ),
            )
    return best


def _line_option(
    encounter: Encounter, actor: Creature, spell: Spell, slot_level: int, dice: Dice
) -> _Option | None:
    actor_square = to_square(as_point(actor.position))
    best: _Option | None = None
    for enemy in sorted(encounter.enemies_of(actor.name), key=lambda c: c.name):
        if to_square(as_point(enemy.position)) == actor_square:
            continue  # a line cannot be aimed at the caster's own square
        caught = encounter.area_targets(spell, actor.name, toward=enemy.name)
        value = _area_value(encounter, actor, spell, dice, caught)
        if best is None or value > best.value:
            best = _Option(
                value=value,
                tiebreak=f"cast:{spell.name}:{slot_level}:@{enemy.name}",
                action=Action(
                    kind=ActionKind.CAST,
                    spell=spell.name,
                    slot_level=slot_level,
                    toward=enemy.name,
                ),
            )
    return best


_DIRECTIONS: tuple[Point, ...] = (
    (-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1),
)


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
    desired = _threat_range(encounter, actor, turn)
    if desired is None:
        return None
    if encounter.battle_map is not None:
        return _closing_move_mapped(
            encounter, actor, enemies, int(turn["movement_left"]), desired
        )
    rule = encounter.movement_rule
    target = min(
        enemies, key=lambda creature: (actor.distance_to(creature, rule), creature.name)
    )
    distance = actor.distance_to(target, rule)
    if distance <= desired:
        return None
    step = min(turn["movement_left"], distance - desired)
    if step <= 0:
        return None
    # Dominant-axis stepping: the step goes along the axis with the larger gap,
    # which on y=0 is the x-axis — bit-identical to the scalar policy there. The
    # dominant gap is at least ``step + desired``, so the step never overshoots.
    ax, ay = as_point(actor.position)
    tx, ty = as_point(target.position)
    if abs(tx - ax) >= abs(ty - ay):
        destination = (ax + (step if tx > ax else -step), ay)
    else:
        destination = (ax, ay + (step if ty > ay else -step))
    return Action(kind=ActionKind.MOVE, to_position=destination)


def _closing_dash(
    encounter: Encounter,
    actor: Creature,
    enemies: Sequence[Creature],
    turn: dict[str, Any],
) -> Action | None:
    """Buy another movement budget when closing can still reach a threat.

    A creature with an authored Bonus Action Dash spends that budget first so
    its action remains available if the extra move reaches an attack.  A second
    Dash with the action remains legal and useful on a longer approach.
    """
    desired = _threat_range(encounter, actor, turn)
    if desired is None:
        return None
    movement_mode: MovementMode | None = None
    if encounter.battle_map is not None:
        movement_mode = _preferred_movement_mode(encounter, actor, enemies, desired)
        can_close = movement_mode is not None
    else:
        can_close = any(
            actor.distance_to(enemy, encounter.movement_rule) > desired
            for enemy in enemies
        )
    if not can_close:
        return None
    authored_mode = (
        movement_mode
        if movement_mode is not MovementMode.WALK
        else None
    )
    if "dash" in actor.bonus_actions and not turn["bonus_action_used"]:
        return Action(
            kind=ActionKind.DASH,
            movement_mode=authored_mode,
            as_bonus_action=True,
        )
    if not turn["action_used"]:
        return Action(kind=ActionKind.DASH, movement_mode=authored_mode)
    return None


def _movement_modes(actor: Creature) -> tuple[MovementMode, ...]:
    """Authored movement modes the creature can actually use, stable by preference."""
    speeds = (
        (MovementMode.WALK, actor.speed),
        (MovementMode.CLIMB, actor.climb_speed),
        (MovementMode.SWIM, actor.swim_speed),
        (MovementMode.FLY, actor.fly_speed),
    )
    return tuple(mode for mode, speed in speeds if speed > 0)


def _preferred_movement_mode(
    encounter: Encounter,
    actor: Creature,
    enemies: Sequence[Creature],
    desired: int,
) -> MovementMode | None:
    """Cheapest authored mode that makes progress toward a visible enemy."""
    if actor.fly_speed > 0 and any(
        enemy.level != actor.level
        and actor.distance_to(enemy, encounter.movement_rule) > desired
        for enemy in enemies
    ):
        return MovementMode.FLY
    choices: list[tuple[int, int, MovementMode]] = []
    for enemy in enemies:
        if enemy.level != actor.level:
            continue
        for rank, mode in enumerate(_movement_modes(actor)):
            path = encounter.route(
                actor.name,
                to_square(as_point(enemy.position)),
                stop_adjacent=True,
                movement_mode=mode,
            )
            if path is not None:
                choices.append((path.cost_feet, rank, mode))
    if not choices:
        return None
    return min(choices, key=lambda entry: (entry[0], entry[1]))[2]


def _cross_level_flight_move(
    encounter: Encounter,
    actor: Creature,
    enemies: Sequence[Creature],
    budget: int,
    desired: int,
) -> Action | None:
    """Best legal direct flight toward an enemy on another visible storey."""
    battle_map = encounter.battle_map
    if battle_map is None or actor.fly_speed <= 0:
        return None
    origin = as_point(actor.position)
    radius = max(0, budget // FEET_PER_SQUARE + 1)
    origin_square = to_square(origin)
    x_min = max(0, origin_square[0] - radius)
    x_max = min(battle_map.width - 1, origin_square[0] + radius)
    y_min = max(0, origin_square[1] - radius)
    y_max = min(battle_map.height - 1, origin_square[1] + radius)
    candidates: list[tuple[int, int, int, str, int, int, int]] = []
    for enemy in sorted(enemies, key=lambda creature: creature.name):
        if enemy.level == actor.level:
            continue
        current_gap = actor.distance_to(enemy, encounter.movement_rule)
        for y in range(y_min, y_max + 1):
            for x in range(x_min, x_max + 1):
                destination = square_center((x, y))
                cost = encounter.flight_cost(actor.name, destination, enemy.level)
                if cost is None or cost > budget:
                    continue
                gap = distance_feet(
                    destination,
                    as_point(enemy.position),
                    encounter.movement_rule,
                )
                if gap >= current_gap:
                    continue
                candidates.append(
                    (
                        max(0, gap - desired),
                        gap,
                        cost,
                        enemy.name,
                        y,
                        x,
                        enemy.level,
                    )
                )
    if not candidates:
        return None
    _outside, _gap, _cost, _name, y, x, level = min(candidates)
    return Action(
        kind=ActionKind.MOVE,
        to_position=square_center((x, y)),
        to_level=level,
        movement_mode=MovementMode.FLY,
    )


def _closing_move_mapped(
    encounter: Encounter,
    actor: Creature,
    enemies: Sequence[Creature],
    budget: int,
    desired: int,
) -> Action | None:
    """Close on the nearest reachable enemy along a real route.

    The nearest enemy is nearest **by route cost**, ties broken on name — a foe
    five feet away through a wall is further than one down the corridor. The
    move walks the affordable part of that route: the furthest square on it that
    the budget reaches, is not stood in, and does not overshoot the desired
    range. Every candidate stop is priced with :meth:`Encounter.route`, the same
    pathfinding the stepper charges with, so an emitted move is never refused.
    Unreachable enemies — or no affordable progress at all — mean ``None``.
    """
    cross_level = _cross_level_flight_move(
        encounter, actor, enemies, budget, desired
    )
    if cross_level is not None:
        return cross_level
    rule = encounter.movement_rule
    routed: list[
        tuple[int, str, int, MovementMode, Creature, tuple[Square, ...]]
    ] = []
    for enemy in sorted(enemies, key=lambda creature: creature.name):
        if enemy.level != actor.level:
            continue
        for rank, mode in enumerate(_movement_modes(actor)):
            path = encounter.route(
                actor.name,
                to_square(as_point(enemy.position)),
                stop_adjacent=True,
                movement_mode=mode,
            )
            if path is not None:
                routed.append(
                    (path.cost_feet, enemy.name, rank, mode, enemy, path.squares)
                )
    if not routed:
        return None
    _cost, _name, _rank, mode, enemy, squares = min(
        routed,
        key=lambda entry: (entry[0], entry[1], entry[2]),
    )
    if actor.distance_to(enemy, rule) <= desired:
        return None
    # Truncate at the first square already within the desired range: a ranged
    # attacker stops where it can shoot from rather than marching into melee.
    walk = list(squares)
    for index, square in enumerate(walk):
        if index and distance_feet(
            square_center(square), as_point(enemy.position), rule
        ) <= desired:
            walk = walk[: index + 1]
            break
    occupied = {
        to_square(as_point(creature.position))
        for creature in encounter.creatures.values()
        if creature.conscious and creature.name != actor.name
    }
    for index in range(len(walk) - 1, 0, -1):
        stop = walk[index]
        if stop in occupied:
            continue
        leg = encounter.route(
            actor.name,
            stop,
            max_cost=budget,
            movement_mode=mode,
        )
        if leg is None:
            continue
        return Action(
            kind=ActionKind.MOVE,
            to_position=square_center(stop),
            movement_mode=mode if mode is not MovementMode.WALK else None,
        )
    return None


def _threat_range(encounter: Encounter, actor: Creature, turn: dict[str, Any]) -> int | None:
    """Furthest the actor can threaten from, or ``None`` if closing gains it nothing.

    ``None`` covers both having no way to hurt anyone and holding a spell with no
    range limit — the engine treats a falsy ``range_feet`` as unbounded, so there is
    nothing to close to. An attack the stepper would refuse — an empty quiver, or a
    Loading weapon already fired this turn — is left out of the reaches the same way
    :func:`_attack_options` leaves it out of the valuation: unfiltered, a dry archer's
    empty bow still reported its printed range, so nothing ever looked worth closing
    the distance for.
    """
    reaches = [
        option.max_distance() for option in actor.attacks if _loaded(actor, option, turn)
    ]
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
    movement_rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE,
    battle_map: BattleMap | None = None,
    terrain_effects: TerrainTable | None = None,
) -> dict[str, Any]:
    """Auto-play the same encounter ``iterations`` times and summarise the outcomes.

    The content tables are arguments rather than something resolved per iteration:
    a batch that reloaded content while running would stop being reproducible from
    its seed, which is the one property these numbers rest on.

    ``battle_map`` puts every iteration on the same frozen map — safe to share,
    since a map is immutable and each :class:`Encounter` builds its own overlay
    state (door positions reset between iterations by construction).

    **Pass ``spellbook`` if any combatant casts.** The policy looks its spells up
    there and skips what it cannot find, so omitting it does not fail — it returns a
    plausible batch in which nobody cast anything. The service layer always
    supplies it; a direct caller has to remember.

    ``wins`` counts ``"none"`` only for a mutual wipe, where the last combatants on
    both sides fall together. A fight still going at ``max_rounds`` is counted in
    ``timed_out`` instead.

    ``items_spent`` excludes ammunition — a name any combatant's own attacks
    declare as ``ammunition`` — and reports it separately as
    ``ammunition_spent``. Firing a shot is not the same kind of decision as
    quaffing a potion, and a quiver of twenty would otherwise swamp the one
    healing item beside it in the same figure.
    """
    if iterations < 1:
        raise ValueError(f"iterations must be at least 1: {iterations}")
    wins: dict[str, int] = {}
    round_counts: list[float] = []
    timeouts = 0
    team_metrics: dict[str, dict[str, list[float]]] = {}
    for index in range(iterations):
        rng = Random(seed + index)
        combatants = list(factory())
        initial_slots = {
            creature.name: sum(creature.spell_slots.values()) for creature in combatants
        }
        initial_items = {
            creature.name: sum(
                quantity
                for name, quantity in creature.items.items()
                if name not in _ammunition_names(creature)
            )
            for creature in combatants
        }
        initial_ammunition = {
            creature.name: sum(
                quantity
                for name, quantity in creature.items.items()
                if name in _ammunition_names(creature)
            )
            for creature in combatants
        }
        encounter = Encounter(
            combatants,
            rng,
            spellbook=spellbook,
            items=items,
            condition_effects=condition_effects,
            movement_rule=movement_rule,
            battle_map=battle_map,
            terrain_effects=terrain_effects,
        )
        outcome = run_encounter(encounter, rng, max_rounds=max_rounds)
        key = outcome.winner if outcome.winner is not None else "none"
        wins[key] = wins.get(key, 0) + 1
        round_counts.append(float(outcome.rounds))
        if outcome.timed_out:
            timeouts += 1
        for team in sorted({creature.team for creature in combatants}):
            members = [creature for creature in combatants if creature.team == team]
            hp = sum(creature.hp for creature in members)
            maximum = sum(creature.max_hp for creature in members)
            metrics = team_metrics.setdefault(
                team,
                {
                    "hp_remaining": [],
                    "hp_fraction": [],
                    "conscious": [],
                    "defeated": [],
                    "spell_slots_spent": [],
                    "items_spent": [],
                    "ammunition_spent": [],
                },
            )
            metrics["hp_remaining"].append(float(hp))
            metrics["hp_fraction"].append(hp / maximum if maximum else 0.0)
            conscious = sum(creature.conscious for creature in members)
            metrics["conscious"].append(float(conscious))
            metrics["defeated"].append(float(len(members) - conscious))
            metrics["spell_slots_spent"].append(
                float(
                    sum(
                        initial_slots[creature.name] - sum(creature.spell_slots.values())
                        for creature in members
                    )
                )
            )
            metrics["items_spent"].append(
                float(
                    sum(
                        initial_items[creature.name]
                        - sum(
                            quantity
                            for name, quantity in creature.items.items()
                            if name not in _ammunition_names(creature)
                        )
                        for creature in members
                    )
                )
            )
            metrics["ammunition_spent"].append(
                float(
                    sum(
                        initial_ammunition[creature.name]
                        - sum(
                            quantity
                            for name, quantity in creature.items.items()
                            if name in _ammunition_names(creature)
                        )
                        for creature in members
                    )
                )
            )
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
        "teams": {
            team: {
                metric: summarise(samples).as_dict()
                for metric, samples in metrics.items()
            }
            for team, metrics in sorted(team_metrics.items())
        },
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

    A ranged attack that names ``ammunition`` is charged for it exactly as live
    play is: the factory's own ``items`` decide the count, and a finite quiver
    runs dry mid-run like any other. Pass one and the resulting damage measures
    the quiver, not the build — the same ``actions`` breakdown will show the
    fallback (melee, or nothing) the policy reached for once the ammunition
    ran out. An unlimited build wants a quiver large enough that ``rounds``
    never exhausts it.
    """
    if iterations < 1:
        raise ValueError(f"iterations must be at least 1: {iterations}")
    totals: list[float] = []
    taken: dict[str, int] = {}
    for index in range(iterations):
        rng = Random(seed + index)
        attacker = attacker_factory()
        ax, ay = as_point(attacker.position)
        dummy = Creature(
            name=target_name,
            team="dummy",
            ac=target_ac,
            max_hp=10_000,
            speed=0,
            position=(ax + distance, ay),
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
        # which does not rebuild it. ``__init__`` also announced round 1 and that
        # creature's turn_start; when the rewrite hands the turn to the attacker
        # instead, the opening turn_start is left naming the initiative winner.
        # Nothing here reads the log — only the dummy's hit points and the
        # actions taken feed the answer — so no corrective event is emitted.
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
