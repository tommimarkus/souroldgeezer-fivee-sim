"""The encounter: initiative, turns, and the authoritative state of a fight.

This is the only place combat state changes. The kernel decides *what* happens;
this decides *what that does to the fight*. Analytics replays this same stepper
rather than reimplementing it, which is why a batch run can never disagree with
live play.

Determinism is a requirement, not a nicety. Initiative ties break on Dexterity
modifier then name, never randomly, and forced rolls are still rolled so the RNG
stream stays aligned between a live encounter and its replay.

All provenance: SRD 5.2 (see NOTICE).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from random import Random
from typing import Any

from ..kernel.actions import (
    MELEE_THRESHOLD,
    AttackKind,
    compute_attack_advantage,
    melee_hit_is_critical,
    resolve_attack,
)
from ..kernel.conditions import (
    EFFECTS,
    Condition,
    ConditionTable,
    compute_save_advantage,
    effect_of,
    is_incapacitated,
    speed_is_zero,
)
from ..kernel.dice import Advantage, roll_d20
from ..kernel.items import ItemEffect, resolve_item_use
from ..kernel.rules import Ability, concentration_dc, make_d20_test
from ..kernel.spells import Spell, SpellTarget, resolve_spell
from .creature import AttackOption, Creature

DEATH_SAVE_DC = 10
DEATH_SAVES_TO_STABILISE = 3
DEATH_SAVES_TO_DIE = 3


class ActionKind(StrEnum):
    ATTACK = "attack"
    CAST = "cast"
    MOVE = "move"
    DASH = "dash"
    DISENGAGE = "disengage"
    DODGE = "dodge"
    USE_ITEM = "use_item"


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    target: str | None = None
    attack: str | None = None
    item: str | None = None
    spell: str | None = None
    slot_level: int | None = None
    to_position: int | None = None
    targets: tuple[str, ...] = ()
    center: int | None = None


#: Every kind of event the encounter emits. ``Event.kind`` stays a plain ``str``
#: rather than an enum — this is the checklist a log consumer can rely on, pinned
#: by test, not a constraint the model enforces.
EVENT_KINDS: frozenset[str] = frozenset({
    "attack", "cast", "concentration", "damage", "dash", "death", "death_save",
    "disengage", "dodge", "down", "effect_end", "heal", "move",
    "opportunity_attack", "round", "spell_effect", "stabilised", "turn_end",
    "turn_start", "use_item",
})


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened, stamped with where in the fight it happened.

    ``seq`` equals the event's position in ``Encounter.log``; ``round`` and
    ``turn`` are the round counter and the acting creature at emission. ``detail``
    stays the human-readable line; ``data`` carries the same facts structured, and
    every position in a payload is a 2-tuple ``(x_feet, y_feet)``.
    """

    kind: str
    actor: str = ""
    target: str = ""
    detail: str = ""
    seq: int = 0
    round: int = 0
    turn: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "actor": self.actor,
            "target": self.target,
            "detail": self.detail,
            "seq": self.seq,
            "round": self.round,
            "turn": self.turn,
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """One successful ``act`` or ``advance`` call — the unit of replay.

    ``action`` is ``None`` for an ``advance``; ``first_event`` and ``event_count``
    slice ``Encounter.log`` to exactly the events the call emitted. Refused
    actions are never recorded: they mutate nothing and consume no randomness, so
    applying the records in order against the same seed and combatants reproduces
    the log byte for byte.
    """

    index: int
    round: int
    actor: str
    action: Action | None
    first_event: int
    event_count: int

    def as_dict(self) -> dict[str, Any]:
        action: dict[str, Any] | None = None
        if self.action is not None:
            action = {"kind": self.action.kind.value}
            for name in ("target", "attack", "item", "spell", "slot_level",
                         "to_position", "center"):
                value = getattr(self.action, name)
                if value is not None:
                    action[name] = value
            if self.action.targets:
                action["targets"] = list(self.action.targets)
        return {
            "index": self.index,
            "round": self.round,
            "actor": self.actor,
            "action": action,
            "first_event": self.first_event,
            "event_count": self.event_count,
        }


class EncounterError(ValueError):
    """An illegal action, reported rather than silently ignored."""


@dataclass(slots=True)
class TurnState:
    movement_left: int = 0
    action_used: bool = False
    attacks_left: int = 0


@dataclass(frozen=True, slots=True)
class OngoingEffect:
    """One condition that one spell or item is currently imposing on one creature.

    A condition on a creature is a bare string in a set; it carries no memory of
    what put it there. That is fine until something has to *end* — SRD 5.2, Rules
    Glossary, "Concentration": "If the effect's creator loses Concentration, the
    effect ends." Answering that needs the link back to the caster, and answering
    it *safely* needs to know whether anything else is imposing the same condition
    on the same creature, so a lapsing spell does not free a creature another spell
    is still holding.

    ``id`` exists so two otherwise identical grants — the same caster, spell,
    target and condition — remain distinct entries rather than collapsing under
    equality when one of them is removed.
    """

    id: int
    #: Name of the creature sustaining the effect.
    source: str
    #: Name of the spell or item, matched against ``Creature.concentrating_on``.
    name: str
    target: str
    condition: str
    concentration: bool
    #: True when the target already had this condition from something outside this
    #: ledger — a stat block that starts with it, or the stepper's own Unconscious.
    #: Such a condition is not ours to lift, so releasing this effect leaves it.
    stacked: bool


class Encounter:
    """A fight in progress."""

    def __init__(
        self,
        combatants: Sequence[Creature],
        rng: Random,
        *,
        spellbook: Mapping[str, Spell] | None = None,
        items: Mapping[str, ItemEffect] | None = None,
        condition_effects: ConditionTable | None = None,
    ) -> None:
        if len(combatants) < 2:
            raise EncounterError("an encounter needs at least two combatants")
        names = [creature.name for creature in combatants]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise EncounterError(
                "combatant names must be unique; duplicated: " + ", ".join(sorted(duplicates))
            )
        self.creatures: dict[str, Creature] = {c.name: c for c in combatants}
        self.spellbook: dict[str, Spell] = dict(spellbook or {})
        self.items: dict[str, ItemEffect] = dict(items or {})
        # The content tables above are captured by value, so reconfiguring content
        # mid-session leaves a fight in progress resolving under what it started
        # with rather than under something swapped in beneath it.
        self.condition_effects: ConditionTable = (
            dict(condition_effects) if condition_effects is not None else EFFECTS
        )
        # Combatants are handed the encounter's table rather than trusted to carry
        # the right one. Analytics builds its damage-per-round dummy directly, with
        # no route for a caller to pass a table, so without this a fight could hold
        # combatants reading two different condition tables.
        for creature in combatants:
            creature.condition_effects = self.condition_effects
        self.round = 1
        self.log: list[Event] = []
        self.actions: list[ActionRecord] = []
        # A list, not a set or a dict: effects are released by iterating it, and an
        # unordered container would let the order of releases — and so the order of
        # log entries — vary between a live fight and its analytics replay.
        self._effects: list[OngoingEffect] = []
        self._next_effect_id = 0
        self._dodging: dict[str, bool] = {name: False for name in names}
        self._disengaged: dict[str, bool] = {name: False for name in names}
        self._reaction_available: dict[str, bool] = {name: True for name in names}
        self._turn = TurnState()

        self.initiative: dict[str, int] = {}
        for creature in combatants:
            roll = roll_d20(rng)
            self.initiative[creature.name] = roll.natural + creature.ability_mod(Ability.DEXTERITY)
        self.order: list[str] = sorted(
            names,
            key=lambda name: (
                -self.initiative[name],
                -self.creatures[name].ability_mod(Ability.DEXTERITY),
                name,
            ),
        )
        self.turn_index = 0
        self._begin_turn(rng)

    # --- queries ----------------------------------------------------------
    @property
    def current_name(self) -> str:
        return self.order[self.turn_index]

    @property
    def current(self) -> Creature:
        return self.creatures[self.current_name]

    def teams(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for creature in self.creatures.values():
            grouped.setdefault(creature.team, []).append(creature.name)
        return grouped

    def living_teams(self) -> set[str]:
        return {c.team for c in self.creatures.values() if c.conscious}

    @property
    def over(self) -> bool:
        return len(self.living_teams()) <= 1

    @property
    def winner(self) -> str | None:
        alive = self.living_teams()
        return next(iter(alive)) if len(alive) == 1 else None

    def enemies_of(self, name: str) -> list[Creature]:
        team = self.creatures[name].team
        return [c for c in self.creatures.values() if c.team != team and c.conscious]

    def state(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "turn": self.current_name,
            "over": self.over,
            "winner": self.winner,
            "order": list(self.order),
            "turn_state": {
                "movement_left": self._turn.movement_left,
                "action_used": self._turn.action_used,
                "attacks_left": self._turn.attacks_left,
            },
            "combatants": [self._creature_state(c) for c in
                           (self.creatures[n] for n in self.order)],
        }

    def _creature_state(self, creature: Creature) -> dict[str, Any]:
        return {
            "name": creature.name,
            "team": creature.team,
            "hp": creature.hp,
            "max_hp": creature.max_hp,
            "ac": creature.ac,
            "position": creature.position,
            "initiative": self.initiative[creature.name],
            "conditions": sorted(creature.conditions),
            "concentrating_on": creature.concentrating_on,
            "dodging": self._dodging[creature.name],
            "conscious": creature.conscious,
            "dying": creature.dying,
            "dead": creature.dead,
            "stable": creature.stable,
            "death_saves": {
                "successes": creature.death_save_successes,
                "failures": creature.death_save_failures,
            },
            "spell_slots": dict(sorted(creature.spell_slots.items())),
            "attacks": [option.name for option in creature.attacks],
            "spells": list(creature.spells),
            "items": dict(sorted(creature.items.items())),
        }

    # --- turn lifecycle ---------------------------------------------------
    def _begin_turn(self, rng: Random) -> None:
        creature = self.current
        self._dodging[creature.name] = False
        self._disengaged[creature.name] = False
        self._reaction_available[creature.name] = True
        self._turn = TurnState(
            movement_left=0 if not creature.conscious else creature.speed,
            action_used=False,
            attacks_left=creature.attacks_per_action,
        )
        if creature.dying:
            self._death_save(creature, rng)
        # A death save can kill, and :meth:`_death_save` marks the creature dead
        # without going through ``take_damage``, so nothing else would notice.
        self._reconcile_concentration()

    def _death_save(self, creature: Creature, rng: Random) -> None:
        roll = roll_d20(rng)
        if roll.natural == 20:
            creature.heal(1)
            self._emit("death_save", creature.name,
                       detail="natural 20 — regains 1 hit point",
                       natural=20,
                       successes=creature.death_save_successes,
                       failures=creature.death_save_failures)
            return
        if roll.natural == 1:
            creature.death_save_failures += 2
            self._emit("death_save", creature.name, detail="natural 1 — two failures",
                       natural=1,
                       successes=creature.death_save_successes,
                       failures=creature.death_save_failures)
        elif roll.natural >= DEATH_SAVE_DC:
            creature.death_save_successes += 1
            self._emit("death_save", creature.name,
                       detail=f"{roll.natural} vs DC {DEATH_SAVE_DC} — success",
                       natural=roll.natural,
                       successes=creature.death_save_successes,
                       failures=creature.death_save_failures)
        else:
            creature.death_save_failures += 1
            self._emit("death_save", creature.name,
                       detail=f"{roll.natural} vs DC {DEATH_SAVE_DC} — failure",
                       natural=roll.natural,
                       successes=creature.death_save_successes,
                       failures=creature.death_save_failures)

        if creature.death_save_failures >= DEATH_SAVES_TO_DIE:
            creature.dead = True
            creature.conditions.discard(Condition.UNCONSCIOUS)
            self._emit("death", creature.name, detail="three failed death saves")
        elif creature.death_save_successes >= DEATH_SAVES_TO_STABILISE:
            creature.stable = True
            # SRD 5.2, Death Saving Throws: "The number of both is reset to zero when
            # you regain any Hit Points or become Stable." ``Creature.heal`` already
            # covers the first clause; this is the second. Leaving the counters
            # standing let a creature that was stabilised, then knocked down again,
            # re-stabilise on its very next roll — even a *failed* one, since the
            # failure landed short of three while the stale successes still tripped
            # this branch.
            creature.death_save_successes = 0
            creature.death_save_failures = 0
            self._emit("stabilised", creature.name, detail="three successful death saves")

    def advance(self, rng: Random) -> list[Event]:
        """End the current turn and begin the next, wrapping the round."""
        before = len(self.log)
        in_round, by = self.round, self.current_name
        self._emit("turn_end", self.current_name)
        if not self.over:
            for _ in range(len(self.order)):
                self.turn_index += 1
                if self.turn_index >= len(self.order):
                    self.turn_index = 0
                    self.round += 1
                    self._emit("round", detail=f"round {self.round} begins",
                               round=self.round)
                if not self.creatures[self.current_name].dead:
                    break
            self._emit("turn_start", self.current_name)
            self._begin_turn(rng)
        # Recorded even when the fight is over: the call still emitted its
        # turn_end, and a replay that skipped it would miss that event.
        self.actions.append(ActionRecord(
            index=len(self.actions), round=in_round, actor=by, action=None,
            first_event=before, event_count=len(self.log) - before,
        ))
        return self.log[before:]

    # --- acting -----------------------------------------------------------
    def act(self, action: Action, rng: Random) -> list[Event]:
        before = len(self.log)
        actor = self.current
        if self.over:
            raise EncounterError("the encounter is over")
        if not actor.conscious:
            raise EncounterError(f"{actor.name} is not conscious and cannot act")
        if not actor.active:
            held = ", ".join(sorted(actor.conditions))
            raise EncounterError(f"{actor.name} is incapacitated ({held}) and cannot act")

        match action.kind:
            case ActionKind.ATTACK:
                self._do_attack(actor, action, rng)
            case ActionKind.CAST:
                self._do_cast(actor, action, rng)
            case ActionKind.MOVE:
                self._do_move(actor, action, rng)
            case ActionKind.DASH:
                self._require_action(actor)
                self._turn.action_used = True
                self._turn.movement_left += actor.speed
                self._emit("dash", actor.name,
                           detail=f"movement now {self._turn.movement_left} ft",
                           movement_left=self._turn.movement_left)
            case ActionKind.DISENGAGE:
                self._require_action(actor)
                self._turn.action_used = True
                self._disengaged[actor.name] = True
                self._emit("disengage", actor.name, detail="no opportunity attacks this turn")
            case ActionKind.USE_ITEM:
                self._do_use_item(actor, action, rng)
            case ActionKind.DODGE:
                self._require_action(actor)
                self._turn.action_used = True
                self._dodging[actor.name] = True
                self._emit("dodge", actor.name,
                           detail="attacks against this creature have disadvantage")
        # The fourth route: an action can land an incapacitating condition on a
        # creature that is concentrating, and ``Creature.add_condition`` clears the
        # field from inside the model, where no release could be issued. A spell or
        # item that imposes one without dealing damage reaches nothing else.
        #
        # This runs before the action is recorded so that any release it emits falls
        # inside that record's event span; recording first would leave the events
        # orphaned between one action and the next.
        self._reconcile_concentration()
        self.actions.append(ActionRecord(
            index=len(self.actions), round=self.round, actor=actor.name, action=action,
            first_event=before, event_count=len(self.log) - before,
        ))
        return self.log[before:]

    def _require_action(self, actor: Creature) -> None:
        if self._turn.action_used:
            raise EncounterError(f"{actor.name} has already taken an action this turn")

    def _resolve_target(self, name: str | None) -> Creature:
        if name is None:
            raise EncounterError("this action needs a target")
        target = self.creatures.get(name)
        if target is None:
            raise EncounterError(f"no combatant named {name!r}")
        return target

    @staticmethod
    def _require_targetable(target: Creature) -> None:
        """Refuse a corpse, and nothing else.

        A creature at 0 hit points is a legal target and the SRD says so twice
        over. Rules Glossary, "Unconscious [Condition]": "Attacks Affected. Attack
        rolls against you have Advantage" and "Automatic Critical Hits. Any attack
        roll that hits you is a Critical Hit if the attacker is within 5 feet of
        you." Neither clause can ever apply to a creature the stepper refuses to
        aim at, and refusing was exactly what it used to do — so a dying creature
        could only die by failing three death saves, never by a finishing blow.

        What a landed hit then costs is "Damage at 0 Hit Points": one death saving
        throw failure, two from a critical hit, and instant death if the damage
        equals or exceeds the hit point maximum. :meth:`Creature.take_damage` owns
        all three; this only has to let the damage through.

        ``dead`` is the one refusal that survives, because a corpse is not a
        creature a spell or an attack can target.
        """
        if target.dead:
            raise EncounterError(f"{target.name} is dead and cannot be targeted")

    def _pick_attack(self, actor: Creature, wanted: str | None) -> AttackOption:
        if not actor.attacks:
            raise EncounterError(f"{actor.name} has no attacks")
        if wanted is None:
            return actor.attacks[0]
        for option in actor.attacks:
            if option.name.casefold() == wanted.casefold():
                return option
        available = ", ".join(option.name for option in actor.attacks)
        raise EncounterError(f"{actor.name} has no attack {wanted!r}; has: {available}")

    def _do_attack(self, actor: Creature, action: Action, rng: Random) -> None:
        target = self._resolve_target(action.target)
        self._require_targetable(target)
        if self._turn.attacks_left <= 0:
            raise EncounterError(f"{actor.name} has no attacks left this turn")
        if self._turn.action_used and self._turn.attacks_left == actor.attacks_per_action:
            # Starting an Attack action costs the action. Later swings of a
            # Multiattack do not, which is why this checks that none has been taken
            # yet rather than checking action_used alone.
            raise EncounterError(f"{actor.name} has already taken an action this turn")
        option = self._pick_attack(actor, action.attack)
        distance = actor.distance_to(target)
        reach = option.max_distance()
        if distance > reach:
            self._emit("attack", actor.name, target.name,
                       f"{option.name} cannot reach ({distance} ft > {reach} ft)",
                       attack=option.name, out_of_range=True)
            return

        self._turn.attacks_left -= 1
        if self._turn.attacks_left == actor.attacks_per_action - 1:
            self._turn.action_used = True

        resolution = resolve_attack(
            rng,
            attack_bonus=option.attack_bonus,
            target_ac=target.ac,
            damage=option.damage,
            advantage=self.attack_advantage(actor, target, option),
            forced_critical=self.attack_forced_critical(actor, target),
            resisted=target.resists(option.damage_type),
            vulnerable=option.damage_type in target.vulnerabilities,
            immune=option.damage_type in target.immunities,
        )
        self._emit("attack", actor.name, target.name,
                   f"{option.name}: {resolution.describe()}",
                   attack=option.name,
                   hit=resolution.hit,
                   critical=resolution.critical,
                   natural=resolution.attack.roll.natural,
                   total=resolution.attack.total,
                   advantage=resolution.advantage.value,
                   damage=resolution.damage_dealt)
        if resolution.hit:
            self._apply_damage(
                target, resolution.damage_dealt, rng, critical=resolution.critical
            )

    def attack_advantage(
        self, actor: Creature, target: Creature, option: AttackOption
    ) -> Advantage:
        """Advantage an attack would resolve under, worked out without rolling it.

        Public because the auto-play policy has to weigh an attack before taking it.
        A policy that re-derived advantage could quietly disagree with the stepper it
        is driving, so both ask this one function instead.
        """
        distance = actor.distance_to(target)
        return compute_attack_advantage(
            attacker_conditions=actor.conditions,
            target_conditions=target.conditions,
            distance=distance,
            long_range_penalty=option.has_long_range_penalty(distance),
            extra_disadvantage=1 if self._dodge_benefits(target) else 0,
            condition_effects=self.condition_effects,
        )

    def attack_forced_critical(self, actor: Creature, target: Creature) -> bool:
        """Whether a landed hit would be upgraded to a critical one. See above.

        **One function serves the swing path and the cast path**, and takes no
        :class:`AttackOption`, because the rule reads nothing about the attack: SRD
        5.2 scopes it on the target's condition and the attacker's distance alone —
        "Any attack roll that hits you is a Critical Hit if the attacker is within 5
        feet of you." A second copy taking a weapon would be a copy that could
        disagree with this one.
        """
        return melee_hit_is_critical(
            target_conditions=target.conditions,
            distance=actor.distance_to(target),
            condition_effects=self.condition_effects,
        )

    def spell_attack_advantage(self, actor: Creature, target: Creature) -> Advantage:
        """Advantage a spell attack against ``target`` would resolve under.

        The cast path's counterpart to :meth:`attack_advantage`, and deliberately
        the same call underneath rather than a second derivation: SRD 5.2 defines
        an attack roll as "a D20 Test that represents making an attack with a
        weapon, an Unarmed Strike, or a spell", and no source of Advantage
        distinguishes them. A Blinded caster, a Dodging target and a Restrained one
        have to read the same either way, which they cannot if two functions decide
        it.

        **There is nothing left to classify.** A spell has a ``range_feet``, not a
        melee/ranged kind, and inventing one would be a data field every pack author
        had to set. It is not needed:
        :func:`~fivee_sim.kernel.actions.compute_attack_advantage` now reads only
        the distance, so this passes the same arguments the swing path does and the
        question of what kind of attack a spell is never arises.
        ``TestSpellAttackAdvantage`` pins both halves of the Prone clause so the
        point survives editing.
        """
        return compute_attack_advantage(
            attacker_conditions=actor.conditions,
            target_conditions=target.conditions,
            distance=actor.distance_to(target),
            extra_disadvantage=1 if self._dodge_benefits(target) else 0,
            condition_effects=self.condition_effects,
        )

    def _pick_item(self, actor: Creature, wanted: str) -> str:
        held = [name for name, count in actor.items.items() if count > 0]
        for name in actor.items:
            if name.casefold() == wanted.casefold():
                if actor.items[name] <= 0:
                    raise EncounterError(f"{actor.name} has no {name} left")
                return name
        carrying = ", ".join(sorted(held)) or "nothing"
        raise EncounterError(f"{actor.name} is not carrying {wanted!r}; has: {carrying}")

    def _do_use_item(self, actor: Creature, action: Action, rng: Random) -> None:
        self._require_action(actor)
        if action.item is None:
            raise EncounterError("using an item needs 'item'")
        name = self._pick_item(actor, action.item)
        effect = self.items.get(name)
        if effect is None:
            available = ", ".join(sorted(self.items)) or "none"
            raise EncounterError(
                f"{name!r} is not defined by the loaded content; defined: {available}"
            )

        if action.target is not None:
            target = self._resolve_target(action.target)
        elif effect.targets_others:
            raise EncounterError(f"{name} needs a target")
        else:
            target = actor
        if target is not actor:
            distance = actor.distance_to(target)
            if distance > MELEE_THRESHOLD:
                raise EncounterError(
                    f"{target.name} is {distance} ft away; an item can only be used on "
                    f"another creature within {MELEE_THRESHOLD} ft"
                )
        self._require_targetable(target)

        self._turn.action_used = True
        actor.items[name] -= 1

        resolution = resolve_item_use(
            rng,
            effect,
            item=name,
            target=target.name,
            save_modifier=(
                target.save_modifier(effect.save_ability)
                if effect.save_ability is not None else 0
            ),
            auto_fail_save=self.auto_fails_save(target, effect.save_ability),
            save_advantage=self.save_advantage(target, effect.save_ability),
            resisted=(
                target.resists(effect.damage_type) if effect.damage_type is not None else False
            ),
            vulnerable=(
                effect.damage_type in target.vulnerabilities
                if effect.damage_type is not None else False
            ),
            immune=(
                effect.damage_type in target.immunities
                if effect.damage_type is not None else False
            ),
        )
        self._emit(
            "use_item", actor.name, target.name,
            f"{resolution.describe()} ({actor.items[name]} left)",
            item=name, remaining=actor.items[name],
        )
        if resolution.healed:
            before = target.hp
            target.heal(resolution.healed)
            self._emit("heal", target=target.name,
                       detail=f"{target.hp - before} hit points restored, "
                              f"{target.hp}/{target.max_hp}",
                       amount=target.hp - before, hp=target.hp, max_hp=target.max_hp)
        if resolution.damage_dealt:
            self._apply_damage(target, resolution.damage_dealt, rng)
        if resolution.condition_applied is not None and target.conscious:
            # An item's condition has no Concentration behind it, so nothing ever
            # releases it. It is recorded anyway, because it *holds* the condition:
            # without the entry, an unrelated spell lapsing on the same creature
            # would lift a condition the item is still imposing.
            self._apply_condition(
                actor, target, resolution.condition_applied,
                effect_name=name, concentration=False,
            )

    def _do_cast(self, actor: Creature, action: Action, rng: Random) -> None:
        self._require_action(actor)
        if action.spell is None:
            raise EncounterError("casting needs a spell name")
        if action.spell not in actor.spells:
            raise EncounterError(f"{actor.name} does not have {action.spell!r} prepared")
        spell = self.spellbook.get(action.spell)
        if spell is None:
            raise EncounterError(f"unknown spell {action.spell!r}")
        slot_level = action.slot_level if action.slot_level is not None else spell.level
        # Every reason to refuse is gathered before a single thing is spent. The
        # slot-level check in particular used to live only inside ``resolve_spell``,
        # which runs after the action is marked used and the slot decremented — so a
        # refusal cost the caster both, and arrived as a bare ``ValueError`` the MCP
        # adapter does not catch. Validate here, in the layer that owns the state and
        # speaks ``EncounterError``.
        if slot_level < spell.level:
            raise EncounterError(
                f"{spell.name} is level {spell.level} and cannot be cast with a "
                f"level {slot_level} slot"
            )
        if spell.level > 0:
            available = actor.spell_slots.get(slot_level, 0)
            if available <= 0:
                raise EncounterError(
                    f"{actor.name} has no level {slot_level} slots remaining"
                )

        chosen = self._spell_targets(actor, spell, action)
        if not chosen:
            raise EncounterError(f"{spell.name} has no valid targets")

        self._turn.action_used = True
        if spell.level > 0:
            actor.spell_slots[slot_level] = actor.spell_slots.get(slot_level, 0) - 1

        resolution = resolve_spell(
            rng,
            spell,
            slot_level=slot_level,
            save_dc=actor.spell_save_dc,
            spell_attack_bonus=actor.spell_attack_bonus,
            targets=tuple(
                SpellTarget(
                    name=c.name,
                    ac=c.ac,
                    save_modifier=(
                        c.save_modifier(spell.save_ability)
                        if spell.save_ability is not None else 0
                    ),
                    auto_fail_save=self.auto_fails_save(c, spell.save_ability),
                    save_advantage=self.save_advantage(c, spell.save_ability),
                    # Filled for every target rather than only for attack-roll
                    # spells, the way the save fields are filled for every target
                    # of an attack-roll one. Both are cheap, neither consumes
                    # randomness, and ``resolve_spell`` reads each pair only on the
                    # branch it belongs to.
                    attack_advantage=self.spell_attack_advantage(actor, c),
                    forced_critical=self.attack_forced_critical(actor, c),
                    resisted=(
                        c.resists(spell.damage_type) if spell.damage_type is not None else False
                    ),
                    vulnerable=(
                        spell.damage_type in c.vulnerabilities
                        if spell.damage_type is not None else False
                    ),
                    immune=(
                        spell.damage_type in c.immunities
                        if spell.damage_type is not None else False
                    ),
                )
                for c in chosen
            ),
        )
        detail = f"{spell.name} (slot {slot_level})"
        if resolution.damage_roll is not None:
            detail += f", damage {resolution.damage_roll.describe()}"
        self._emit("cast", actor.name, detail=detail,
                   spell=spell.name,
                   slot_level=slot_level,
                   center=(action.center, 0) if action.center is not None else None,
                   targets=[c.name for c in chosen])

        if spell.concentration:
            # SRD 5.2, "Concentration": "You lose Concentration on an effect the
            # moment you start casting a spell that requires Concentration." An
            # unconditional release covers recasting the *same* spell on a new
            # target, which comparing names could not see.
            self._end_concentration(actor)
            actor.concentrating_on = spell.name
            self._emit("concentration", actor.name, detail=f"concentrating on {spell.name}",
                       spell=spell.name, held=True, started=True)

        for result in resolution.results:
            target = self.creatures[result.name]
            self._emit("spell_effect", actor.name, target.name, result.describe(),
                       spell=spell.name,
                       damage=result.damage_dealt,
                       affected=result.affected,
                       saved=result.save.success if result.save is not None else None,
                       condition=result.condition_applied)
            if result.damage_dealt:
                # A spell attack carries a critical exactly as a weapon swing does,
                # and it matters for the same reason: two death save failures rather
                # than one against a creature already at 0. A save-based spell has no
                # attack roll and so never crits.
                self._apply_damage(
                    target,
                    result.damage_dealt,
                    rng,
                    critical=result.attack is not None and result.attack.critical,
                )
            if result.condition_applied is not None and target.conscious:
                self._apply_condition(
                    actor, target, result.condition_applied,
                    effect_name=spell.name, concentration=spell.concentration,
                )

    def auto_fails_save(self, creature: Creature, ability: Ability | None) -> bool:
        """Whether a condition forces this creature to fail the save outright.

        Public for the same reason as ``attack_advantage``: the policy needs it to
        value a spell, and must not keep a second copy of the rule.
        """
        if ability is None:
            return False
        for condition in creature.conditions:
            effect = effect_of(condition, self.condition_effects)
            if ability is Ability.STRENGTH and effect.auto_fail_strength_saves:
                return True
            if ability is Ability.DEXTERITY and effect.auto_fail_dexterity_saves:
                return True
        return False

    def save_advantage(self, creature: Creature, ability: Ability | None) -> Advantage:
        """Advantage this creature's saving throw resolves under.

        Separate from :meth:`auto_fails_save` rather than folded into it, because a
        forced failure still rolls: the two answers are independent and both are
        needed at the same call sites.
        """
        if ability is None:
            return Advantage.NONE
        return compute_save_advantage(
            conditions=creature.conditions,
            ability=ability,
            extra_advantage=(
                1
                if ability is Ability.DEXTERITY and self._dodge_benefits(creature)
                else 0
            ),
            condition_effects=self.condition_effects,
        )

    def _dodge_benefits(self, creature: Creature) -> bool:
        """Whether a Dodge taken this round is still doing anything.

        Taking the action is not the same as keeping it: the benefits are lost while
        the creature is Incapacitated or its Speed is 0. Restrained makes that
        reachable — a Restrained creature may still Dodge, and gains nothing by it,
        so its Dexterity save stays at Disadvantage instead of cancelling to a
        straight roll.
        """
        if not self._dodging[creature.name]:
            return False
        return not (
            is_incapacitated(creature.conditions, self.condition_effects)
            or speed_is_zero(creature.conditions, self.condition_effects)
        )

    def _spell_targets(
        self, actor: Creature, spell: Spell, action: Action
    ) -> list[Creature]:
        """Resolve who a spell lands on, enforcing both its range and its target cap.

        The branches are range-checked differently, and deliberately. Named targets
        are checked one at a time, exactly as a single-target spell is. An area is
        checked at its **point of origin** only: those creatures come from the
        radius rather than from the caller, so refusing the whole spell because one
        creature at the far edge of the blast sits past the range would be wrong.
        Checking *neither* — the previous behaviour — let a 150 ft Fireball land a
        thousand feet away.

        **Consciousness is not a filter.** SRD 5.2, "Spells" -> Targets -> Areas of
        Effect: "The area determines what the spell targets." A creature at 0 hit
        points is inside the blast or it is not, and the Unconscious condition's own
        clause — "You automatically fail Strength and Dexterity saving throws" —
        exists precisely for the roll a Fireball then makes it attempt. Filtering
        here meant a blast centred on a dying creature's exact square dealt it
        nothing while burning everyone around it. Corpses are still excluded; the
        named branches refuse one outright rather than dropping it silently, so a
        caller who aims at a body is told, not ignored.
        """
        if action.targets:
            chosen = [self._resolve_target(name) for name in action.targets]
            for creature in chosen:
                self._require_targetable(creature)
                self._require_in_range(actor, spell, creature.position, creature.name)
        elif spell.radius and action.center is not None:
            self._require_in_range(actor, spell, action.center, "the point of origin")
            chosen = [
                c for c in self.creatures.values()
                if not c.dead and abs(c.position - action.center) <= spell.radius
            ]
        elif action.target is not None:
            chosen = [self._resolve_target(action.target)]
            self._require_targetable(chosen[0])
            self._require_in_range(actor, spell, chosen[0].position, chosen[0].name)
        else:
            raise EncounterError(
                f"{spell.name} needs 'target', 'targets', or 'center' to be given"
            )
        if spell.radius:
            # An area is bounded by its radius, not by a head count. Every bundled
            # area spell leaves max_targets at its default of 1, so applying the cap
            # here would quietly shrink a Fireball to a single creature.
            return chosen
        if len(chosen) > spell.max_targets:
            creatures = "creature" if spell.max_targets == 1 else "creatures"
            raise EncounterError(
                f"{spell.name} affects at most {spell.max_targets} {creatures}; "
                f"{len(chosen)} were named"
            )
        return chosen

    def _require_in_range(
        self, actor: Creature, spell: Spell, position: int, what: str
    ) -> None:
        """Refuse a spell whose range does not reach ``position``."""
        if not spell.range_feet:
            return
        distance = abs(position - actor.position)
        if distance > spell.range_feet:
            raise EncounterError(
                f"{what} is {distance} ft away, beyond {spell.name}'s "
                f"{spell.range_feet} ft range"
            )

    def _do_move(self, actor: Creature, action: Action, rng: Random) -> None:
        if action.to_position is None:
            raise EncounterError("moving needs 'to_position'")
        if speed_is_zero(actor.conditions, self.condition_effects):
            held = ", ".join(sorted(actor.conditions))
            raise EncounterError(f"{actor.name} has speed 0 ({held}) and cannot move")
        distance = abs(action.to_position - actor.position)
        if distance > self._turn.movement_left:
            raise EncounterError(
                f"{actor.name} has {self._turn.movement_left} ft of movement, needs {distance} ft"
            )

        threatening = [
            enemy for enemy in self.enemies_of(actor.name)
            if actor.distance_to(enemy) <= MELEE_THRESHOLD
        ]
        origin = actor.position
        actor.position = action.to_position
        self._turn.movement_left -= distance
        self._emit("move", actor.name,
                   detail=f"{origin} ft -> {actor.position} ft ({distance} ft used)",
                   origin=(origin, 0), destination=(actor.position, 0), cost=distance)

        if self._disengaged[actor.name]:
            return
        for enemy in threatening:
            if abs(enemy.position - actor.position) <= MELEE_THRESHOLD:
                continue
            self._opportunity_attack(enemy, actor, rng)

    def _opportunity_attack(self, attacker: Creature, mover: Creature, rng: Random) -> None:
        if not self._reaction_available.get(attacker.name, False) or not attacker.active:
            return
        melee = next(
            (option for option in attacker.attacks if option.kind is AttackKind.MELEE),
            None,
        )
        if melee is None:
            return
        self._reaction_available[attacker.name] = False
        advantage = compute_attack_advantage(
            attacker_conditions=attacker.conditions,
            target_conditions=mover.conditions,
            distance=MELEE_THRESHOLD,
            extra_disadvantage=1 if self._dodge_benefits(mover) else 0,
            condition_effects=self.condition_effects,
        )
        resolution = resolve_attack(
            rng,
            attack_bonus=melee.attack_bonus,
            target_ac=mover.ac,
            damage=melee.damage,
            advantage=advantage,
            resisted=mover.resists(melee.damage_type),
            vulnerable=melee.damage_type in mover.vulnerabilities,
            immune=melee.damage_type in mover.immunities,
        )
        self._emit("opportunity_attack", attacker.name, mover.name,
                   f"{melee.name}: {resolution.describe()}",
                   attack=melee.name,
                   hit=resolution.hit,
                   critical=resolution.critical,
                   natural=resolution.attack.roll.natural,
                   total=resolution.attack.total,
                   advantage=resolution.advantage.value,
                   damage=resolution.damage_dealt)
        if resolution.hit:
            # A mover cannot currently be at 0 hit points — a dying creature has
            # Speed 0 — so the flag changes nothing today. It is passed anyway: two
            # call sites resolving the same kind of attack and only one carrying the
            # critical is the asymmetry that becomes a bug when reactions grow.
            self._apply_damage(
                mover, resolution.damage_dealt, rng, critical=resolution.critical
            )

    # --- ongoing effects --------------------------------------------------
    def _apply_condition(
        self,
        source: Creature,
        target: Creature,
        condition: str,
        *,
        effect_name: str,
        concentration: bool,
    ) -> None:
        """Impose ``condition`` and record what is imposing it.

        Registration happens *after* ``add_condition`` so a name the active table
        does not define is refused before anything is written down, and the
        ``stacked`` reading is taken *before*, because it is a statement about the
        creature as it was.
        """
        held_by_ledger = self._holders(target.name, condition)
        already_held = condition in target.conditions
        target.add_condition(condition)
        self._next_effect_id += 1
        self._effects.append(
            OngoingEffect(
                id=self._next_effect_id,
                source=source.name,
                name=effect_name,
                target=target.name,
                condition=condition,
                concentration=concentration,
                stacked=already_held and not held_by_ledger,
            )
        )

    def _holders(self, target: str, condition: str) -> list[OngoingEffect]:
        return [
            effect for effect in self._effects
            if effect.target == target and effect.condition == condition
        ]

    def _release_effect(self, effect: OngoingEffect) -> None:
        """End one ongoing effect, lifting its condition only if nothing else holds it.

        The two reasons a condition survives its own effect ending are the reason
        this is a ledger rather than a ``remove_condition`` paired with each
        ``add_condition``: a second caster may be imposing the same condition on the
        same creature, and the creature may have been carrying it before either of
        them arrived.
        """
        self._effects.remove(effect)
        target = self.creatures.get(effect.target)
        if target is None:  # pragma: no cover - names cannot leave the roster
            return
        remaining = self._holders(effect.target, effect.condition)
        if remaining or effect.stacked:
            reason = (
                "another effect still holds it" if remaining
                else "it was already held"
            )
            self._emit(
                "effect_end", effect.source, effect.target,
                f"{effect.name} ends; {effect.condition} persists ({reason})",
            )
            return
        target.remove_condition(effect.condition)
        self._emit(
            "effect_end", effect.source, effect.target,
            f"{effect.name} ends; {effect.condition} lifts",
        )

    def _end_concentration(self, actor: Creature) -> None:
        """Drop whatever ``actor`` is concentrating on, and everything it sustains."""
        for effect in list(self._effects):
            if effect.concentration and effect.source == actor.name:
                self._release_effect(effect)
        actor.concentrating_on = None

    def _reconcile_concentration(self) -> None:
        """Enforce the one invariant that makes concentration effects end.

        **An ongoing concentration effect exists exactly while its source is still
        sustaining it.** Checking that, rather than releasing at each place
        concentration can lapse, is deliberate: SRD 5.2 ends Concentration on a
        failed Constitution save, on the Incapacitated condition, on death, and on
        starting another Concentration effect — and two of those are enforced inside
        :class:`~fivee_sim.model.creature.Creature`, which cannot reach this ledger.
        A design with one release call per exit point is a design where the next
        exit point added leaks silently.

        ``dead`` and ``active`` are consulted alongside ``concentrating_on`` rather
        than trusting it alone, because they are what SRD 5.2 actually says
        ("Your Concentration ends if you have the Incapacitated condition or you
        die") and because not every route to death clears the field: a creature
        killed by its third failed death save is marked dead by
        :meth:`_death_save`, which never touches it.

        This consumes no randomness, so a batch's RNG stream is unaffected.
        """
        for effect in list(self._effects):
            if not effect.concentration:
                continue
            source = self.creatures.get(effect.source)
            if source is None:  # pragma: no cover - names cannot leave the roster
                self._release_effect(effect)
                continue
            spent = source.dead or not source.active
            if spent or source.concentrating_on != effect.name:
                self._release_effect(effect)
            # Cleared only when the creature genuinely cannot concentrate, never
            # merely because a stale entry did not match: clearing on a mismatch
            # would cancel a *new* concentration the caster had just begun.
            if spent:
                source.concentrating_on = None

    # --- damage and concentration ----------------------------------------
    def _apply_damage(
        self, target: Creature, amount: int, rng: Random, *, critical: bool = False
    ) -> None:
        if amount <= 0:
            return
        was_conscious = target.conscious
        was_dead = target.dead
        # Which rule killed the creature is not recoverable afterwards, so the
        # failure count is read here: the massive-damage rule is the only route to
        # death that accrues none.
        failures_before = target.death_save_failures
        concentrating = target.concentrating_on
        # ``critical`` only matters for a target already at 0 hit points, where a
        # critical hit costs two death save failures instead of one.
        target.take_damage(amount, critical=critical)
        self._emit("damage", target=target.name,
                   detail=f"{amount} damage, {target.hp}/{target.max_hp} hit points left",
                   amount=amount, hp=target.hp, max_hp=target.max_hp)
        if concentrating is not None and target.conscious:
            dc = concentration_dc(amount)
            save = make_d20_test(
                rng,
                modifier=target.save_modifier(Ability.CONSTITUTION),
                dc=dc,
            )
            if save.success:
                self._emit("concentration", target.name,
                           detail=f"holds {concentrating} ({save.describe()})",
                           spell=concentrating, held=True)
            else:
                target.concentrating_on = None
                self._emit("concentration", target.name,
                           detail=f"loses {concentrating} ({save.describe()})",
                           spell=concentrating, held=False)
        # Death is announced from ``dead`` rather than from a loss of consciousness.
        # Keying it on the latter meant a creature already at 0 — killed by its third
        # failure, or by damage matching its maximum — died with the state correct
        # and the log silent, which is the one event a narrator cannot do without.
        if target.dead and not was_dead:
            self._emit("death", target.name, detail=(
                "a third failed death save"
                if target.death_save_failures > failures_before
                else "damage exceeded maximum hit points"
            ))
        elif was_conscious and not target.conscious:
            self._emit("down", target.name, detail="falls unconscious and is dying")
        # Three of the four ways concentration ends pass through here — the failed
        # save above, being knocked out, and dying — so the release is reported next
        # to the loss rather than at the end of the action that caused it.
        self._reconcile_concentration()

    def _emit(
        self, kind: str, actor: str = "", target: str = "", detail: str = "", **data: Any
    ) -> None:
        # Stamping is safe at every call site: __init__ emits nothing before
        # ``order`` exists, the round event fires after ``round`` increments, and
        # turn_start fires after ``turn_index`` has moved.
        self.log.append(Event(
            kind=kind, actor=actor, target=target, detail=detail,
            seq=len(self.log), round=self.round, turn=self.current_name, data=data,
        ))


def build_encounter(
    combatants: Iterable[Creature],
    *,
    seed: int,
    spellbook: Mapping[str, Spell] | None = None,
    items: Mapping[str, ItemEffect] | None = None,
    condition_effects: ConditionTable | None = None,
) -> tuple[Encounter, Random]:
    """Create an encounter and the generator that drives it, from a seed alone."""
    rng = Random(seed)
    encounter = Encounter(
        list(combatants),
        rng,
        spellbook=spellbook,
        items=items,
        condition_effects=condition_effects,
    )
    return encounter, rng
