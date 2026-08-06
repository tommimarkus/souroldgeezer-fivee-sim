"""The encounter: initiative, turns, and the authoritative state of a fight.

This is the only place combat state changes. The kernel decides *what* happens;
this decides *what that does to the fight*. Analytics replays this same stepper
rather than reimplementing it, which is why a batch run can never disagree with
live play.

Determinism is a requirement, not a nicety. Initiative ties break on Dexterity
modifier then name, never randomly, and forced rolls are still rolled so the RNG
stream stays aligned between a live encounter and its replay.

All provenance: SRD 5.2.1 (see NOTICE).
"""

from __future__ import annotations

import heapq
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from random import Random
from typing import Any

from ..kernel.actions import (
    MELEE_THRESHOLD,
    AttackKind,
    RiderExpiry,
    compute_attack_advantage,
    melee_hit_is_critical,
    resolve_attack,
)
from ..kernel.conditions import (
    EFFECTS,
    Condition,
    ConditionTable,
    compute_ability_check_advantage,
    compute_initiative_advantage,
    compute_save_advantage,
    effect_of,
    is_incapacitated,
    speed_is_zero,
)
from ..kernel.dice import Advantage, check_faces, roll_d20, roll_dice
from ..kernel.grid import (
    FEET_PER_SQUARE,
    TERRAIN,
    CoverGrade,
    DiagonalRule,
    MovementMode,
    Path,
    Point,
    Square,
    TerrainEffect,
    TerrainTable,
    as_point,
    cone_squares,
    cover_ac_bonus,
    cube_squares,
    distance_feet,
    facing_toward,
    find_path,
    has_line_of_sight,
    line_squares,
    sphere_squares,
    square_center,
    step_cost_feet,
    terrain_effect_of,
    to_square,
)
from ..kernel.grid import cover_between as grid_cover_between
from ..kernel.items import ActionCost, ItemEffect, resolve_item_use
from ..kernel.rules import (
    Ability,
    D20Test,
    DamageType,
    Size,
    concentration_dc,
    fits_within,
    make_d20_test,
)
from ..kernel.spells import Spell, SpellShape, SpellTarget, resolve_spell
from ..map_types import (
    GROUND_LEVEL,
    LightLevel,
    MapDocument,
    MapFeatureRecord,
    MapLevel,
    MapLight,
    SquareClaim,
    TriggerMode,
)
from .creature import AttackOption, Creature, DeathRule

DEATH_SAVE_DC = 10
DEATH_SAVES_TO_STABILISE = 3
DEATH_SAVES_TO_DIE = 3
#: Undead Fortitude's save is DC this plus the damage that caused the drop.
UNDEAD_FORTITUDE_BASE_DC = 5
#: How many combatant names an unknown-target refusal spells out before it
#: summarises the remainder. Nothing bounds the size of a fight, and a mass
#: battle's whole roster in one error line would bury the name that was actually
#: wrong; a dozen is enough to spot a misspelling against.
MAX_LISTED_COMBATANTS = 12

#: :class:`~fivee_sim.map_types.LightLevel`'s members as a membership test.
#: ``_adopt_map`` asks it once per storey, and rebuilding the set there measured
#: an order of magnitude more than the lookup it was for.
_LIGHT_LEVELS: frozenset[str] = frozenset(LightLevel)


@dataclass(slots=True)
class MapState:
    """What a fight has changed about its map: which features stand open.

    The one mutable fact a fight owns about its battlefield, and the reason a
    :class:`~fivee_sim.map_types.MapDocument` can be frozen and shared: an
    encounter *references* a map and layers this over it, so the same document
    can back any number of fights at once and a finished one leaves it exactly
    as it found it.

    It lives here rather than beside the document types because it is not part
    of what a map *is*. A file records a fixture's default; where it stands
    right now is encounter state, and encounter state belongs to the module
    that is the only place combat state changes.
    """

    open_features: set[str]


class ActionKind(StrEnum):
    ATTACK = "attack"
    CAST = "cast"
    MOVE = "move"
    DASH = "dash"
    DISENGAGE = "disengage"
    DODGE = "dodge"
    USE_ITEM = "use_item"
    INTERACT = "interact"
    STAND = "stand"
    SURRENDER = "surrender"


@dataclass(frozen=True, slots=True)
class Action:
    """One thing a combatant tries to do.

    Positions are points in feet; a bare int is accepted anywhere a position goes
    and means feet along the x-axis. ``path`` names explicit waypoints for a move
    and only means something on a battle map; ``direction`` aims a cone (one of
    the eight unit offsets); ``toward`` aims a line at a combatant by name or at
    a point; ``feature`` names a map feature for an interaction, and ``set_open``
    says which way that interaction should move it.
    """

    kind: ActionKind
    target: str | None = None
    attack: str | None = None
    item: str | None = None
    spell: str | None = None
    slot_level: int | None = None
    to_position: int | Point | None = None
    targets: tuple[str, ...] = ()
    center: int | Point | None = None
    path: tuple[Point, ...] = ()
    direction: Point | None = None
    toward: str | Point | None = None
    feature: str | None = None
    #: Which state to leave a feature in, rather than flipping whatever it is
    #: now. ``None`` toggles, which is what an interaction has always done; a
    #: bool refuses when the feature is already there, so a caller driving a
    #: chain of fixtures cannot close one by asking twice for it to open.
    set_open: bool | None = None
    #: The storey a move ends on. Only meaningful for a move, and only over a
    #: connector: walk to the stairway on your own level, and it carries you.
    to_level: int | None = None
    #: Which movement speed pays for this move. Omitted preserves the legacy
    #: walking default.
    movement_mode: MovementMode | None = None
    #: Explicit intent for action kinds that can use either budget. Effects with
    #: a fixed cost validate this against their declaration.
    as_bonus_action: bool = False
    #: Where the actor ends up looking, overriding what a move would derive.
    #: Named ``facing`` rather than ``direction`` because ``direction`` is taken
    #: on this same dataclass — it aims a cone, and the two are different facts.
    facing: str | None = None
    #: The d20 faces the actor rolled on their own dice, for a person at the
    #: table who would rather roll than be rolled for. Empty means the engine
    #: rolls, which is every other caller and every auto-played batch.
    #:
    #: It covers **the actor's own d20 for this action** — a weapon or spell
    #: attack, and an ability check a fixture asks for. A saving throw somebody
    #: *else* is forced to make lands inside this same call, so there is nowhere
    #: for its owner to report a face; those stay the engine's. Death saves are
    #: rolled by ``advance`` and carry their own.
    natural: tuple[int, ...] = ()


#: What a combatant may learn about somebody on the *other* side, and what it
#: may not. Two frozen sets rather than one filter, because the interesting
#: property is that the classification is **total**: every key
#: :meth:`Encounter._creature_state` reports belongs to exactly one of them, and
#: ``tests/test_player_brief.py`` holds both against its real output.
#:
#: A single allowlist would have been shorter and worse. It answers "is this
#: shown?" but not "has anybody looked at this?" — so a field added to the state
#: payload would default to withheld and simply go missing, which reads exactly
#: like a field that was considered and withheld. Requiring a deliberate
#: classification is what makes a new secret impossible to add by accident.
ENEMY_VISIBLE_KEYS: frozenset[str] = frozenset({
    # Where it is and whether it is really there. ``elevation`` is the ground
    # under its feet, which is the room's fact rather than the creature's, and
    # ``arrival_round`` is safe only because an unarrived creature is omitted
    # from the brief entirely — by the time an entry carries one, the round it
    # names has already happened.
    "name", "team", "position", "level", "elevation", "facing", "present",
    "arrival_round",
    # What anybody at the table can see about its state.
    "conditions", "conscious", "dying", "dead", "stable", "surrendered",
    "dodging", "disengaged",
    # Where it sits in the order. Initiative is called out loud.
    "initiative",
    # Visible while it holds, and the reason a caster is worth interrupting.
    "concentrating_on",
})

#: The other half: what a stat block says and a table does not get to read.
ENEMY_WITHHELD_KEYS: frozenset[str] = frozenset({
    # The numbers the whole redaction exists for.
    "hp", "max_hp", "ac",
    # Resources, and therefore what it can still do to you.
    "spell_slots", "items", "attacks", "spells", "bonus_actions",
    "reaction_available", "redirect_attack",
    # Capability a player would have to observe rather than be told.
    "speeds", "senses", "terrain_cost_overrides", "death_rule",
    # How close it is to dying, which is the hit-point leak wearing a hat.
    "death_saves",
})


#: The battle map's own block, classified in the same two-set shape and for the
#: same reason. It was handed to :meth:`Encounter.brief` **whole** for a
#: release, so every fixture's ability-check DC reached the player who asked for
#: one — and "DCs before a roll" is the first entry on the Withhold list in
#: ``agents/game-master.md``, which is the specification the brief implements.
#: An allowlist that stops one level above the payload is a denylist wearing the
#: other one's name.
MAP_VISIBLE_KEYS: frozenset[str] = frozenset({
    "name", "width", "height", "movement_rule", "elevation", "levels", "features",
})

#: Empty, and written out so a reader can see it is empty *by decision*. The map
#: block carries no creature and no secret of its own; everything in it a player
#: may not have is one level further down, in the fixtures.
MAP_WITHHELD_KEYS: frozenset[str] = frozenset()

#: One fixture as the room shows it: where it is, what it is, which storey,
#: whether it stands open, and whether working it costs your action — the last
#: because a player choosing a turn is owed what their options cost.
FEATURE_VISIBLE_KEYS: frozenset[str] = frozenset({
    "square", "kind", "level", "open", "costs_action",
})

#: What a fixture *does elsewhere* and what it *takes*, which is the module's to
#: reveal and not the map file's to publish. ``check`` is a DC before a roll
#: outright; ``affects``, ``requires``, ``blocked_by``, ``linked_to`` and
#: ``trigger`` are the mechanism behind it, and a party handed the wiring has
#: been handed the puzzle. What they keep is every fixture's ``open`` state,
#: which is the thing the room actually shows them.
#:
#: The six after them are **decided ahead of the payload**, and that is a
#: departure worth naming. Every other entry in these pairs is a key
#: :meth:`Encounter._feature_summary` emits and
#: ``tests/test_player_brief.py`` reads back off a real fight. These six are
#: keys a :class:`~fivee_sim.map_types.MapFeatureRecord` *carries* and that
#: summary does not emit — it did not exist to emit them while a fixture was a
#: ``MapFeature``, which held none of them. Withheld rather than visible because
#: withholding is the reversible direction: serving one later is one line and a
#: reviewer, where a key sitting in the visible half is a disclosure nobody ever
#: decided on, arriving the day somebody widens the summary. The
#: double-classification case is what makes that reviewer show up.
#:
#: ``team`` is the sharpest of the six and the reason the rest are here with it:
#: on a spawn hint it answers *which side arrives where*, which is the ambusher
#: the brief works hardest to keep off the wire. ``to_level`` and
#: ``sight_to_levels`` are map wiring, which :data:`EVENT_NEVER_KEYS` already
#: serves to nobody one payload down. ``facing``, ``hinge`` and ``swing`` are
#: the weakest case — which way a door hangs is arguably just the room — but
#: nothing asks for them, ``agents/game-master.md`` does not list them, and a
#: key with no caller is a key to decide when one turns up.
FEATURE_WITHHELD_KEYS: frozenset[str] = frozenset({
    "check", "affects", "requires", "blocked_by", "linked_to", "trigger",
    "team", "to_level", "sight_to_levels", "facing", "hinge", "swing",
})


#: Which keys of an operation's answer carry events. ``events`` is what ``act``
#: and ``advance`` reply with. ``log`` is ``create``'s, and it is not merely the
#: opening pair a fresh fight has: an idempotent retry answers with the *whole*
#: log of a fight already in progress, so a projection that narrowed only
#: ``events`` would hand that over.
EVENT_LISTS: tuple[str, ...] = ("events", "log")

#: An event's own fields, minus ``data``'s contents. ``actor``, ``target`` and
#: ``turn`` are here because the *key* survives rather than its contents passing
#: unread — each is held against the cast in :meth:`Encounter.brief_events` —
#: and ``data`` for the same reason.
EVENT_ENVELOPE_VISIBLE_KEYS: frozenset[str] = frozenset({
    "kind", "actor", "target", "seq", "round", "turn", "data",
})

#: The one field no seat is served, whichever side the event belongs to.
#: ``detail`` is the GM's rendered sentence — "Longsword: d20 [15] +5 = 20 vs
#: AC 16 -> hit" — free-form prose that names the AC a swing was rolled against,
#: the DC a check was made against, and the spell behind an effect. Prose cannot
#: be classified key by key, so there is no honest way to serve part of it, and
#: it is **omitted rather than emptied**: ``""`` would say *nothing happened*,
#: where an absent key says *this seat is not served this*, which is true.
EVENT_ENVELOPE_WITHHELD_KEYS: frozenset[str] = frozenset({"detail"})

#: What the table watched happen, inside an event's ``data``. The line drawn
#: here is the one :data:`ENEMY_VISIBLE_KEYS` draws one payload over: an
#: *observation* is public and a *sheet* is not.
#:
#: Three of these are judgement calls worth naming.
#:
#: ``hit`` is here and ``total`` is not. At a real table a player learns whether
#: a blow landed the moment it lands, so withholding it would be a payload that
#: lies about the round. What the number would add is arithmetic: a hit at 19
#: says the target's AC is at most 19 and a miss at 18 says it is at least 19,
#: and a few rounds of those brackets it exactly — which is why ``total`` goes
#: only to the side that rolled it, where it discloses nothing the roller did
#: not already know.
#:
#: ``amount`` is here and ``hp`` is not, and that is the same line one step
#: over. The damage that landed is the roll everyone watched; a player tracking
#: their own damage against a health band is doing at the table exactly what a
#: table lets them do. What is refused is the *sheet's* numbers, which turn that
#: estimate into the answer.
#:
#: ``damage_type`` is here and ``damage`` is not. A wound's element is the most
#: visible thing about it and the ``damage`` event does not carry it, so
#: withholding it would leave a party unable to say what is hurting them.
#: ``damage`` can go because it is never the only account — every point that
#: lands is reported again as ``amount`` — and keeping it back keeps an
#: attachment's *formula*, which ``attach`` sends through this key as ``"1d4"``,
#: off the wire.
EVENT_VISIBLE_KEYS: frozenset[str] = frozenset({
    # the fight's own clock and cast
    "round", "attacker", "original_target", "redirected_target", "targets",
    # where things are and how they got there
    "position", "level", "origin", "destination", "squares", "from_level",
    "to_level", "movement_mode", "completed", "cost", "center",
    "original_position", "redirected_position",
    # what the table watched land
    "hit", "critical", "amount", "damage_type", "total_drained", "affected",
    "applied", "saved", "success", "condition", "expiry",
    # a condition the table imposed or lifted by ruling rather than by a rule.
    # The condition itself is already visible, and a ruling is announced out
    # loud — it is the one kind of effect whose provenance *is* the table.
    "ruling",
    # the ground, and what is between two creatures
    "cover", "total_cover", "out_of_range", "underwater", "underwater_auto_miss",
    # the action economy, which is spent in the open
    "as_bonus_action", "action_cost",
    # a fixture, as the room shows it
    "feature", "open", "automatic",
    # concentration holds or it drops, and the effect ending says so anyway
    "held", "started",
    # the round a reinforcement landed in, which is the round they landed in:
    # ``_arrive_for_round`` emits this only once the creature is present, so it
    # is never in the future. One key, one answer — it is
    # :data:`ENEMY_VISIBLE_KEYS` on the creature for the same reason.
    "arrival_round",
})

#: The other half: an event's own side's rolls, resources and repertoire — the
#: same sheet :data:`ENEMY_WITHHELD_KEYS` covers, arriving one event at a time.
#:
#: ``natural`` with ``total`` is the roller's attack bonus by subtraction, which
#: is a number off the sheet however it is spelled; ``advantage`` says which
#: circumstance produced the roll. ``attack`` and ``spell`` name one entry of
#: the ``attacks`` and ``spells`` lists that are already withheld — a table
#: watches a swing and learns that a heavy blade came down, not the catalogue
#: key — and ``item``, ``remaining``, ``slot_level``, ``successes`` and
#: ``failures`` are the resources those lists are spent from. ``movement_left``
#: is ``turn_state``, which the brief already reports on your own turn alone.
#:
#: ``ammunition_remaining`` is here for the same reason as ``remaining``, which
#: it is: a quiver is a line on the shooter's own sheet, held in the very
#: ``items`` dictionary :data:`ENEMY_WITHHELD_KEYS` already keeps back. A table
#: watches an arrow leave the bow; it does not get to count what is left in the
#: ambusher's quiver.
EVENT_WITHHELD_KEYS: frozenset[str] = frozenset({
    "hp", "max_hp", "natural", "total", "advantage", "attack", "spell",
    "slot_level", "item", "remaining", "successes", "failures", "movement_left",
    "ammunition_remaining",
    "damage", "bonus_damage", "advantage_bonus_damage", "advantage_bonus_reason",
    "detach_after_damage",
    # and the sharper half below, which no seat is served at all
    "dc", "check", "linked", "triggered_by", "planned_destination",
    "planned_to_level",
})

#: The subset of :data:`EVENT_WITHHELD_KEYS` withheld from *every* seat,
#: including the side whose event it is. The two-set classification above stays
#: total — this names a line inside its second half rather than a third bucket.
#:
#: ``dc`` and ``check`` are a difficulty class and the sentence that quotes one,
#: the first entry on the Withhold list, and ``check`` is already
#: :data:`FEATURE_WITHHELD_KEYS` one payload up. ``linked`` and ``triggered_by``
#: are the map's wiring, withheld there for the same reason: a party handed the
#: pairing has been handed the puzzle, and they can still watch both fixtures'
#: ``open`` states move. ``planned_destination`` and ``planned_to_level`` are
#: the square a cut-short move was *making for* — intent rather than
#: observation, which the battlefield never shows and which the mover already
#: knows, having asked for it.
EVENT_NEVER_KEYS: frozenset[str] = frozenset({
    "dc", "check", "linked", "triggered_by", "planned_destination",
    "planned_to_level",
})


#: Added to an opponent's entry by the brief and carried by no creature in the
#: snapshot: how far off they are, and how they look. Named so the classification
#: test can hold the projected entry against a set rather than against a list
#: written out twice.
ENEMY_DERIVED_KEYS: frozenset[str] = frozenset({"distance", "health"})

#: What replaces an enemy's hit points: the thing a game master says out loud.
#: Bands rather than a fraction, because a fraction is a hit-point total that
#: needs one division to recover. Our own descriptive vocabulary, not a rules
#: term — nothing in the engine keys off these strings.
#:
#: Upper bound of each band as a share of maximum hit points, best first. The
#: bounds live here and are never published — a client told both the band and
#: its edges is a client one subtraction away from a range, and a client told
#: ``max_hp`` as well has the number.
_HEALTH_BANDS: tuple[tuple[float, str], ...] = (
    (1.0, "unharmed"),
    (0.5, "hurt"),
    (0.25, "badly hurt"),
    (0.0, "barely standing"),
)

#: Every band :func:`health_band` will ever report, worst first. Plain language
#: on purpose: these are the words a game master says out loud, and none of them
#: is a number wearing a hat. ``dead`` and ``down`` are their own words rather
#: than the bottom of the scale, because a creature at zero is a different fact
#: from a wounded one and the table can see which it is anyway.
HEALTH_BANDS: tuple[str, ...] = (
    "dead", "down", *(described for _, described in reversed(_HEALTH_BANDS))
)


#: The action kinds that can put the *actor's* own d20 on the table, and so the
#: only ones a reported face means anything for. Two of the three are
#: conditional — a cast rolls one only when the spell attacks rather than
#: forcing a save, and an interaction only when the fixture asks for a check —
#: so each handler refuses its own remaining cases. This set is the cheap part
#: of that check: the kinds where the answer is never.
_KINDS_THAT_MAY_ROLL: frozenset[ActionKind] = frozenset({
    ActionKind.ATTACK,
    ActionKind.CAST,
    ActionKind.INTERACT,
})


#: Every kind of event the encounter emits. ``Event.kind`` stays a plain ``str``
#: rather than an enum — this is the checklist a log consumer can rely on, pinned
#: by test, not a constraint the model enforces.
EVENT_KINDS: frozenset[str] = frozenset({
    "attack", "cast", "concentration", "damage", "dash", "death", "death_save",
    "disengage", "dodge", "down", "effect_apply", "effect_end", "heal", "interact",
    "move", "opportunity_attack", "round", "spell_effect", "stabilised", "stand",
    "attach", "attached_damage", "detach", "surrender", "redirect_attack", "arrival",
    "turn_end", "turn_start", "undead_fortitude", "use_item",
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
    the log byte for byte. The events before the first record — round 1, the
    opening turn_start, and any death saves the opening turn rolls — belong to
    ``__init__``, which a rebuild from the same seed reproduces before the first
    record is applied.
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
            # Every optional scalar :class:`Action` carries. The list is written
            # out rather than derived so a field can be deliberately withheld —
            # but a field added above and forgotten here is silently dropped
            # from a record that promises to replay the call exactly, which is
            # how ``to_level`` went missing from every cross-storey move.
            for name in ("target", "attack", "item", "spell", "slot_level",
                         "to_position", "center", "direction", "toward", "feature",
                         "set_open", "to_level", "movement_mode", "as_bonus_action",
                         "facing"):
                value = getattr(self.action, name)
                if value is not None:
                    action[name] = (
                        value.value if isinstance(value, StrEnum)
                        else list(value) if isinstance(value, tuple) else value
                    )
            if self.action.targets:
                action["targets"] = list(self.action.targets)
            if self.action.path:
                action["path"] = [list(point) for point in self.action.path]
            # With the other tuples rather than the scalars above: an empty one
            # means the engine rolled, and writing `[]` for that would put a
            # caller-supplied-nothing into every record of every ordinary fight.
            if self.action.natural:
                action["natural"] = list(self.action.natural)
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
    """What is left of the acting creature's turn, and what it has already used.

    Rebuilt from nothing at every turn boundary, which is why a gate that lasts
    exactly one turn — ``loading_used`` — belongs here rather than on the
    creature: nothing has to remember to clear it.
    """

    movement_left: int = 0
    action_used: bool = False
    attacks_left: int = 0
    interaction_used: bool = False
    bonus_action_used: bool = False
    #: A Loading weapon has been fired this turn. See :meth:`Encounter._do_attack`
    #: for why the SRD's per-activation cap is approximated per turn.
    loading_used: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The turn budget as ``state`` publishes it.

        One writer rather than a literal at each reporting site, for the reason
        :meth:`ActionRecord.as_dict` gives one field up: a key added to the
        dataclass and to only one of the hand-written copies is a payload that
        disagrees with itself depending on which door the caller came through.
        """
        return {
            "movement_left": self.movement_left,
            "action_used": self.action_used,
            "attacks_left": self.attacks_left,
            "interaction_used": self.interaction_used,
            "bonus_action_used": self.bonus_action_used,
            "loading_used": self.loading_used,
        }


@dataclass(frozen=True, slots=True)
class OngoingEffect:
    """One condition that one spell or item is currently imposing on one creature.

    A condition on a creature is a bare string in a set; it carries no memory of
    what put it there. That is fine until something has to *end* — SRD 5.2.1, Rules
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
    #: Timed expiry, for an attack rider's condition: the phase (``"start"`` or
    #: ``"end"``) and the creature whose turn boundary ends this effect. Empty
    #: strings mean no timer — the effect lasts until something else releases it.
    expires_phase: str = ""
    expires_anchor: str = ""


@dataclass(slots=True)
class Attachment:
    """A source fastened to a target and its periodic damage rider."""

    source: str
    target: str
    damage: Any
    damage_type: DamageType
    detach_after_damage: int = 0
    damage_dealt: int = 0


def _segment_samples(origin: Point, destination: Point) -> list[Point]:
    """The straight walk from ``origin`` to ``destination``, sampled every 5 ft.

    These samples are what a mapless move replays instead of a battle map's
    squares: 5-ft paces measured on the longer axis, so consecutive samples
    never differ by more than 5 ft in either coordinate. Interior points round
    to the integer lattice; both endpoints are exact.
    """
    dx = destination[0] - origin[0]
    dy = destination[1] - origin[1]
    paces = -(-max(abs(dx), abs(dy)) // FEET_PER_SQUARE)  # ceil: last pace may be short
    samples = [origin]
    for k in range(1, paces + 1):
        samples.append((
            origin[0] + (2 * k * dx + paces) // (2 * paces),
            origin[1] + (2 * k * dy + paces) // (2 * paces),
        ))
    return samples


def _trigger_cycle(edges: Mapping[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    """One deterministic trigger cycle, rotated to its smallest fixture id."""
    visited: set[str] = set()
    active: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> tuple[str, ...] | None:
        if node in active:
            cycle = path[active[node] :]
            smallest = min(range(len(cycle)), key=lambda index: cycle[index])
            ordered = cycle[smallest:] + cycle[:smallest]
            return (*ordered, ordered[0])
        if node in visited:
            return None
        active[node] = len(path)
        path.append(node)
        for dependency in sorted(edges.get(node, ())):
            found = visit(dependency)
            if found is not None:
                return found
        path.pop()
        active.pop(node)
        visited.add(node)
        return None

    for start in sorted(edges):
        found = visit(start)
        if found is not None:
            return found
    return None


def _dependency_order(features: Mapping[str, MapFeatureRecord]) -> tuple[str, ...]:
    """All fixtures dependency-first, with lexical ties."""
    indegree = {name: 0 for name in features}
    followers: dict[str, set[str]] = {name: set() for name in features}
    edges: dict[str, tuple[str, ...]] = {}
    for target, feature in features.items():
        if feature.trigger is None:
            continue
        dependencies = tuple(name for name, _ in feature.trigger.when if name in features)
        edges[target] = dependencies
        for dependency in dependencies:
            if target not in followers[dependency]:
                followers[dependency].add(target)
                indegree[target] += 1
    ready = list(name for name, count in indegree.items() if count == 0)
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        name = heapq.heappop(ready)
        ordered.append(name)
        for target in sorted(followers[name]):
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)
    if len(ordered) != len(features):
        cycle = _trigger_cycle(edges)
        rendered = " -> ".join(cycle or ())
        raise EncounterError(f"trigger cycle: {rendered}")
    return tuple(ordered)


# --- the player's brief ------------------------------------------------------
# Free functions rather than methods, because every one of them is a function of
# a snapshot and nothing else. What the brief needs from the live fight is one
# thing only — whether a wall stands between two creatures — and that is
# :meth:`Encounter.unseen_by`'s to answer.
def health_band(entry: Mapping[str, Any]) -> str:
    """How a creature looks, for somebody who cannot read its sheet.

    Deliberately lossy, and lossy in a way no arithmetic undoes: the ratio, the
    band's own bounds and the creature's ``max_hp`` are withheld together,
    because publishing any one of them turns the band back into a number.
    """
    if entry["dead"]:
        return "dead"
    if not entry["conscious"]:
        return "down"
    max_hp = int(entry["max_hp"])
    if max_hp <= 0:
        return "unharmed"
    share = int(entry["hp"]) / max_hp
    for threshold, described in _HEALTH_BANDS:
        if share >= threshold:
            return described
    return "barely standing"


def _point_of(value: Any) -> Point:
    """A snapshot's position as a point in feet.

    The payload carries ``[x, y]`` because :meth:`Encounter._creature_state`
    writes it through ``list``; the grid wants a tuple. A bare integer still
    means feet along the x-axis, which is what :func:`as_point` has always
    widened.
    """
    if isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 2:
        return (int(value[0]), int(value[1]))
    return as_point(int(value))


def _briefed_map(block: Mapping[str, Any]) -> dict[str, Any]:
    """The battlefield, minus each fixture's wiring and its DC.

    Walks the payload's own keys rather than the bucket's, so an unclassified
    key falls out here rather than being renamed into existence.
    """
    briefed = {key: value for key, value in block.items() if key in MAP_VISIBLE_KEYS}
    fixtures = briefed.get("features")
    if isinstance(fixtures, Mapping):
        briefed["features"] = {
            name: {
                key: value for key, value in one.items() if key in FEATURE_VISIBLE_KEYS
            }
            for name, one in fixtures.items()
        }
    return briefed


def _mentions(value: Any, names: Collection[str]) -> bool:
    """Whether any of ``names`` appears as a string anywhere inside ``value``.

    Compared whole rather than as a substring, so a visible ``Grelka`` is not
    mistaken for an unseen ``Grelk``. It can afford to be exact because the one
    field that would have carried a name inside prose — ``detail`` — is gone by
    the time this runs.
    """
    if isinstance(value, str):
        return value in names
    if isinstance(value, Mapping):
        return any(
            _mentions(key, names) or _mentions(one, names) for key, one in value.items()
        )
    if isinstance(value, Sequence):
        return any(_mentions(one, names) for one in value)
    return False


def _side_of(raw: Mapping[str, Any], seats: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Whose event this is: the actor's side, or the target's when there is no actor.

    One decision per event rather than one per key, and the order matters. An
    ``attack`` names the swinger first, so the roll, the weapon and the damage
    breakdown go to the side that made them and not to the side they landed on —
    which is what stops a foe's swing at your ally publishing the foe's attack
    bonus. A ``damage`` event carries no actor at all: it is emitted *about* the
    creature that took the hit, so its ``hp`` and ``max_hp`` are that creature's
    and reach only their own side.
    """
    for key in ("actor", "target"):
        name = raw.get(key)
        if name:
            entry = seats.get(str(name))
            return None if entry is None else str(entry["team"])
    return None


def _briefed_event(
    raw: Mapping[str, Any], *, own: bool, unseen: Collection[str]
) -> dict[str, Any] | None:
    """One event as this seat may see it, or ``None`` if it may not see it at all.

    Built by walking the event's own keys for the reason :func:`_briefed_map`
    walks the map's: an unclassified key falls out here rather than being
    renamed into existence.

    **An event that still names a creature this seat cannot see is dropped
    whole**, and that check is deliberately made on the *projected* entry rather
    than on ``actor`` and ``target`` alone. Several ``data`` keys carry creature
    names — ``targets``, ``attacker``, ``original_target``, ``redirected_target``
    — and a further list of "keys that are names" would be one more thing to keep
    in step with the model. Sweeping the finished entry needs no such list and
    cannot miss the name a key added tomorrow carries. It is a second guard over
    an allowlist, not a denylist standing in for one: nothing reaches this point
    that :data:`EVENT_VISIBLE_KEYS` did not already admit.

    Dropping, rather than blanking the name, is the answer the brief's
    ``enemies`` list already gives — an ambusher reported with a blank name has
    still been revealed. It leaves a gap in ``seq``, which is existence without
    identity and the same residual a nulled ``turn`` leaves.

    ``turn`` is the one name held against the cast rather than dropped for,
    because it is a stamp and not the event's subject: it is nulled exactly as
    :meth:`Encounter.brief_of` nulls the snapshot's ``turn``, so that ``round 2
    begins`` still reaches a table whose next combatant has not arrived.

    ``arrival`` needs no exemption and gets none. ``_arrive_for_round`` marks
    the creature present *before* it emits, so by the time this runs they are in
    the visible cast and the general rule admits the event on its own.
    """
    entry = {
        key: value for key, value in raw.items() if key in EVENT_ENVELOPE_VISIBLE_KEYS
    }
    permitted = (
        (EVENT_VISIBLE_KEYS | EVENT_WITHHELD_KEYS) - EVENT_NEVER_KEYS
        if own
        else EVENT_VISIBLE_KEYS
    )
    data = entry.get("data")
    if isinstance(data, Mapping):
        entry["data"] = {
            key: value for key, value in data.items() if key in permitted
        }
    if entry.get("turn") in unseen:
        entry["turn"] = None
    if _mentions(entry, unseen):
        return None
    return entry


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
        movement_rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE,
        map_document: MapDocument | None = None,
        terrain_effects: TerrainTable | None = None,
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
        for creature in combatants:
            creature.arrived = creature.arrival_round <= 1
        #: How diagonals are measured, for every distance this fight takes.
        self.movement_rule = movement_rule
        self.spellbook: dict[str, Spell] = dict(spellbook or {})
        self.items: dict[str, ItemEffect] = dict(items or {})
        # The content tables above are captured by value, so reconfiguring content
        # mid-session leaves a fight in progress resolving under what it started
        # with rather than under something swapped in beneath it.
        self.condition_effects: ConditionTable = (
            dict(condition_effects) if condition_effects is not None else EFFECTS
        )
        self.terrain_effects: TerrainTable = (
            dict(terrain_effects) if terrain_effects is not None else TERRAIN
        )
        self.map_document = map_document
        self.map_state: MapState | None = None
        self._trigger_sequence: tuple[str, ...] = ()
        self._trigger_active: dict[str, bool] = {}
        # Every square any fixture decides, flattened once at adoption: the two
        # resolvers below sit inside pathfinding loops, so what they need is an
        # index, not a list of features to scan.
        self._feature_squares: dict[tuple[int, Square], SquareClaim] = {}
        # The document's own derivations, materialised once for the same reason.
        # Each is O(features) to compute and each sits on a per-attack path —
        # ``_illumination_at`` inside a scan over every enemy, so deriving it per
        # call would be O(creatures x features) for one swing. What is
        # deliberately *not* here is a terrain index: the tiles are dense and
        # ``legend[tiles[y][x]]`` measures no slower than the sparse lookup it
        # replaced, so building one per encounter would cost a montecarlo batch
        # a second of setup to save nothing.
        self._fixtures: dict[str, MapFeatureRecord] = {}
        self._fixture_level: dict[str, int] = {}
        # The document's own two tables, held directly rather than reached
        # through it. ``_terrain_at_level`` and ``_elevation_at`` are the
        # engine's hottest reads — every step of every route goes through one —
        # and the two attribute hops they save measured 15 ns a call.
        self._levels: Mapping[int, MapLevel] = {}
        self._legend: Mapping[str, str] = {}
        self._connectors: dict[int, Mapping[Square, int]] = {}
        self._sight_links: dict[int, Mapping[Square, frozenset[int]]] = {}
        self._lights: dict[int, tuple[tuple[Square, MapLight], ...]] = {}
        self._ambient: dict[int, LightLevel] = {}
        if map_document is not None:
            self._adopt_map(map_document, combatants)
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
        self._attachments: list[Attachment] = []
        self._next_effect_id = 0
        self._dodging: dict[str, bool] = {name: False for name in names}
        self._disengaged: dict[str, bool] = {name: False for name in names}
        self._reaction_available: dict[str, bool] = {name: True for name in names}
        self._turn = TurnState()

        self.initiative: dict[str, int] = {}
        for creature in combatants:
            roll = roll_d20(
                rng,
                compute_initiative_advantage(
                    conditions=creature.conditions,
                    condition_effects=self.condition_effects,
                ),
            )
            # A printed Initiative bonus, when the stat block carries one,
            # replaces the Dexterity modifier outright — SRD 5.2.1,
            # *Initiative*: the printed line is the authority. ``None`` means
            # no such line, and the modifier is used exactly as before.
            modifier = (
                creature.initiative_bonus
                if creature.initiative_bonus is not None
                else creature.ability_mod(Ability.DEXTERITY)
            )
            self.initiative[creature.name] = roll.natural + modifier
        self.order: list[str] = sorted(
            names,
            key=lambda name: (
                -self.initiative[name],
                # The SRD tie-break in its own right, not a stand-in for the
                # bonus above: it reads the Dexterity modifier even for a
                # creature whose total came from a printed Initiative bonus.
                -self.creatures[name].ability_mod(Ability.DEXTERITY),
                name,
            ),
        )
        self.turn_index = 0
        # The fight opens the way every later turn does: round 1 and the first
        # turn_start are announced before ``_begin_turn`` rolls anything, so a
        # combatant dying at initiative rolls its death save after its
        # turn_start, exactly as on any other turn. Emitting consumes no
        # randomness, and ``order`` and ``turn_index`` exist by now, so the
        # stamps are correct and the RNG stream is unchanged. These events
        # precede the first ActionRecord: they belong to construction, and a
        # replay reproduces them by rebuilding from the same seed.
        self._emit("round", detail=f"round {self.round} begins", round=self.round)
        self._emit("turn_start", self.current_name)
        self._begin_turn(rng)

    # --- the battle map ---------------------------------------------------
    def _adopt_map(self, document: MapDocument, combatants: Sequence[Creature]) -> None:
        """Validate the map against the terrain table and place the combatants.

        Everything a map can get wrong is refused here, before the first roll:
        a malformed plane of tiles, a terrain kind the captured table does not
        define, a feature off the map or doubled up on a square, a prerequisite
        naming nothing, a combatant off the map, inside a wall, or on another
        combatant. Positions are snapped to the centre of their square — on a
        grid, the square is the position.

        A ``MapDocument`` can be hand-built with no file behind it, so these
        refusals are not a second opinion on the parser's — they are the only
        ones such a map ever meets. **Every square a fixture governs is claimed
        by exactly one fixture per level**, which is what makes the resolvers
        below total: there is no precedence question to answer, so a stateless
        reader of the same map cannot disagree with this fight about what a
        square is.

        The first pass over each storey is the one that arrived with the
        document. :meth:`~fivee_sim.map_types.MapLevel.terrain_at` reads
        ``legend[tiles[y][x]]`` at the moment a fight asks, so an undefined
        glyph and a row short of the grid's width are both ``LookupError`` —
        and a ``KeyError`` out of ``__init__`` is a 500 where an
        ``EncounterError`` is problem+json. They are refused here, and refused
        *by their distinct glyphs* rather than square by square: the scan is a
        set per row, which measures faster on a 512x512 map than the sparse
        terrain mapping this check used to walk.

        Which storey a fixture stands on, where the connectors and sight links
        are, and which squares are lit are all read off the document once here
        rather than per query, because each sits on a per-attack path.
        """
        grid = document.grid
        self._levels = document.levels
        self._legend = document.legend
        if grid.width < 1 or grid.height < 1:
            raise EncounterError(
                f"a battle map needs at least one square; "
                f"got {grid.width}x{grid.height}"
            )
        named: set[str] = set()
        for index in sorted(document.levels):
            level = document.levels[index]
            if len(level.tiles) != grid.height:
                rows = "row" if len(level.tiles) == 1 else "rows"
                raise EncounterError(
                    f"level {index} has {len(level.tiles)} {rows} on a "
                    f"{grid.width}x{grid.height} map"
                )
            for y, row in enumerate(level.tiles):
                if len(row) != grid.width:
                    raise EncounterError(
                        f"level {index} row {y} is {len(row)} squares wide on a "
                        f"{grid.width}x{grid.height} map"
                    )
            # One join and one set, rather than a set per row: the rows are
            # equal length by the check above, and both halves of this are one
            # C-level pass where the per-row form pays Python's loop per row.
            # Measured on a 512x512 storey: 525 us against 552.
            glyphs = set("".join(level.tiles))
            undrawn = sorted(glyph for glyph in glyphs if glyph not in document.legend)
            if undrawn:
                spelled = ", ".join(repr(glyph) for glyph in sorted(document.legend))
                raise EncounterError(
                    f"level {index} draws "
                    f"{', '.join(repr(glyph) for glyph in undrawn)}, which this "
                    f"map's legend does not define; the legend has: "
                    f"{spelled or 'nothing'}"
                )
            named.update(document.legend[glyph] for glyph in glyphs)

            fixtures = level.fixtures()
            for name, feature in fixtures.items():
                # Before anything reads its square: an off-map fixture with no
                # terrain of its own falls through to the tile it stands on,
                # which is not there to be read.
                if not self._on_map(feature.at):
                    raise EncounterError(
                        f"feature {name!r} sits at {feature.at}, off the "
                        f"{grid.width}x{grid.height} map"
                    )
                for square, claim in feature.claims(level, document.legend):
                    if not self._on_map(square):
                        raise EncounterError(
                            f"feature {name!r} reaches {square}, off the "
                            f"{grid.width}x{grid.height} map"
                        )
                    other = self._feature_squares.get((index, square))
                    if other is not None and other.feature == name:
                        raise EncounterError(
                            f"feature {name!r} claims square {square} twice"
                        )
                    if other is not None:
                        raise EncounterError(
                            f"features {other.feature!r} and {name!r} share square {square}"
                        )
                    self._feature_squares[(index, square)] = claim
                    if claim.terrain is not None:
                        named.add(claim.terrain.closed)
                        named.add(claim.terrain.open)
            self._fixtures.update(fixtures)
            self._fixture_level.update(dict.fromkeys(fixtures, index))
            self._connectors[index] = level.connectors()
            self._sight_links[index] = level.sight_links()
            self._lights[index] = level.lights()
            # A third refusal of the same kind as the two above: the level holds
            # a plain ``str``, and the bare ``ValueError`` the enum would raise
            # is not an ``EncounterError``, so ``service/encounters`` would let
            # it past into a 500.
            if level.ambient_light not in _LIGHT_LEVELS:
                spelled = ", ".join(light.value for light in LightLevel)
                raise EncounterError(
                    f"level {index} is lit {level.ambient_light!r}, which is not a "
                    f"light level; the light levels are: {spelled}"
                )
            self._ambient[index] = LightLevel(level.ambient_light)

            for square, target in self._connectors[index].items():
                if target not in document.levels:
                    raise EncounterError(
                        f"the connector at {square} on level {index} leads to level "
                        f"{target}, which this map does not have"
                    )
        unknown = sorted(kind for kind in named if kind not in self.terrain_effects)
        if unknown:
            defined = ", ".join(sorted(self.terrain_effects)) or "none"
            raise EncounterError(
                f"the map names terrain the loaded content does not define: "
                f"{', '.join(unknown)}. Defined: {defined}"
            )
        # A second pass, because a prerequisite may point forward — and across a
        # floor. ``requires`` is a prerequisite, not a reach: which storey the
        # thing it names sits on is nobody's business but the fiction's.
        catalogue = self._fixtures
        for name, feature in sorted(catalogue.items()):
            missing = [wanted for wanted in feature.requires if wanted not in catalogue]
            if missing:
                available = ", ".join(sorted(catalogue)) or "none"
                raise EncounterError(
                    f"feature {name!r} requires "
                    f"{', '.join(repr(wanted) for wanted in missing)}, which this map "
                    f"does not have; the map has: {available}"
                )
            if feature.linked_to is None:
                continue
            partner = catalogue.get(feature.linked_to)
            if partner is None:
                raise EncounterError(
                    f"feature {name!r} links to {feature.linked_to!r}, which this map "
                    "does not have"
                )
            if feature.kind != "door" or partner.kind != "door":
                raise EncounterError("only doors may be linked")
            if partner.linked_to != name:
                raise EncounterError(
                    f"feature {name!r} links to {partner.id!r}; that door must link "
                    f"back to {name!r}"
                )
            if self._fixture_level[name] != self._fixture_level[partner.id]:
                raise EncounterError("linked doors must stand on the same level")
            dx = abs(feature.at[0] - partner.at[0])
            dy = abs(feature.at[1] - partner.at[1])
            if dx + dy != 1:
                raise EncounterError("linked doors must stand on adjacent squares")
            if feature.orientation != partner.orientation or feature.orientation not in {
                "horizontal", "vertical",
            }:
                raise EncounterError(
                    "linked doors must share a horizontal or vertical orientation"
                )
            aligned = (feature.orientation == "horizontal" and dx == 1) or (
                feature.orientation == "vertical" and dy == 1
            )
            if not aligned:
                raise EncounterError(
                    f"linked doors must be aligned with their {feature.orientation} orientation"
                )
            if feature.state != partner.state:
                raise EncounterError("linked doors must start in the same state")
            if feature.trigger != partner.trigger:
                raise EncounterError("linked doors must have identical triggers")
            contract = (feature.requires, feature.costs_action, feature.check)
            partner_contract = (partner.requires, partner.costs_action, partner.check)
            if contract != partner_contract:
                raise EncounterError(
                    "linked doors must have the same requires, costs_action, and check"
                )
        initially_open = {
            name for name, feature in catalogue.items() if feature.state == "open"
        }
        for name, feature in sorted(catalogue.items()):
            trigger = feature.trigger
            if trigger is None:
                continue
            if not trigger.when:
                raise EncounterError(
                    f"feature {name!r} trigger must name at least one fixture"
                )
            if type(trigger.set_open) is not bool or not isinstance(
                trigger.mode, TriggerMode
            ):
                raise EncounterError(
                    f"feature {name!r} has a malformed trigger state or mode"
                )
            seen_dependencies: set[str] = set()
            for condition in trigger.when:
                if not isinstance(condition, tuple) or len(condition) != 2:
                    raise EncounterError(
                        f"feature {name!r} has a malformed trigger condition"
                    )
                dependency, expected = condition
                if (
                    not isinstance(dependency, str)
                    or not dependency.strip()
                    or type(expected) is not bool
                    or dependency in seen_dependencies
                ):
                    raise EncounterError(
                        f"feature {name!r} has a malformed trigger condition"
                    )
                seen_dependencies.add(dependency)
            for dependency, _ in trigger.when:
                if dependency not in catalogue:
                    raise EncounterError(
                        f"feature {name!r} trigger references {dependency!r}, which this "
                        "map does not have"
                    )
            if trigger.set_open:
                conditions = dict(trigger.when)
                for required in feature.requires:
                    if conditions.get(required) is not True:
                        raise EncounterError(
                            f"trigger opens feature {name!r} but does not require "
                            f"{required!r} to be open"
                        )
            starts_open = feature.state == "open"
            if (
                trigger.mode is TriggerMode.MAINTAINED
                and trigger.active(initially_open)
                and starts_open is not trigger.set_open
            ):
                raise EncounterError(
                    f"feature {name!r} maintained trigger is true initially and sets "
                    f"it {'open' if trigger.set_open else 'closed'}, but it starts "
                    f"{'open' if starts_open else 'closed'}"
                )
        ordered = _dependency_order(catalogue)
        self.map_state = MapState(open_features=initially_open)
        self._trigger_sequence = tuple(
            name for name in ordered if catalogue[name].trigger is not None
        )
        for name in self._trigger_sequence:
            trigger = catalogue[name].trigger
            assert trigger is not None
            self._trigger_active[name] = trigger.active(initially_open)

        placed: dict[tuple[int, Square], str] = {}
        for creature in combatants:
            if creature.level not in document.levels:
                declared = ", ".join(str(i) for i in sorted(document.levels))
                raise EncounterError(
                    f"{creature.name} starts on level {creature.level}, which this map "
                    f"does not have. Levels: {declared}"
                )
            square = to_square(as_point(creature.position))
            if not self._on_map(square):
                raise EncounterError(
                    f"{creature.name} starts at {as_point(creature.position)}, off the "
                    f"{grid.width}x{grid.height} map"
                )
            if self._entry_cost(creature.level, square) is None:
                raise EncounterError(
                    f"{creature.name} starts on impassable "
                    f"{self._terrain_at_level(creature.level, square)!r} at {square}"
                )
            neighbour = placed.get((creature.level, square))
            if neighbour is not None:
                raise EncounterError(
                    f"{creature.name} and {neighbour} both start in square {square}"
                )
            placed[(creature.level, square)] = creature.name
            creature.position = square_center(square)

    def _on_map(self, square: Square) -> bool:
        assert self.map_document is not None
        grid = self.map_document.grid
        return 0 <= square[0] < grid.width and 0 <= square[1] < grid.height

    def _is_open(self, feature_name: str) -> bool:
        assert self.map_state is not None
        return feature_name in self.map_state.open_features

    def _terrain_at_level(self, level: int, square: Square) -> str:
        """What one square of one storey is right now: feature state, then the map.

        One of the two choke points every movement, sight, cover and placement
        query goes through, and neither caches. That is what makes a fixture
        change land live mid-fight for the price of one dict lookup rather than
        an invalidation scheme.

        A claim carrying no terrain pair falls through as an unclaimed square
        does: a fixture that only moves a water level leaves the ground it finds.

        The fall-through reads the document's dense tiles through its legend.
        **Every caller is on the map before it arrives here** — ``_entry_cost``,
        ``_step_cost``, ``_opaque`` and ``_cover_of`` each test ``_on_map``
        first, and the two error paths that name a kind are reached only after
        one of those has passed — because
        :meth:`~fivee_sim.map_types.MapLevel.terrain_at` raises rather than
        answering for a square that is not there. Python would read
        ``tiles[-1][-1]`` as the far corner with a straight face, which is the
        wrong answer wearing a right one's clothes.
        """
        assert self.map_document is not None and self.map_state is not None
        claim = self._feature_squares.get((level, square))
        if claim is not None and claim.terrain is not None:
            if self._is_open(claim.feature):
                return claim.terrain.open
            return claim.terrain.closed
        return self._levels[level].terrain_at(square, self._legend)

    def _terrain_at(self, square: Square) -> str:
        """The ground plane's terrain. The mapless and single-storey shorthand."""
        return self._terrain_at_level(GROUND_LEVEL, square)

    def _terrain_effect(self, level: int, square: Square) -> TerrainEffect:
        return terrain_effect_of(self._terrain_at_level(level, square), self.terrain_effects)

    def _is_underwater(self, creature: Creature) -> bool:
        if self.map_document is None:
            return False
        return self._terrain_effect(
            creature.level, to_square(as_point(creature.position))
        ).underwater

    def _resisted_by_target(self, target: Creature, damage_type: DamageType) -> bool:
        return target.resists(damage_type) or (
            damage_type is DamageType.FIRE and self._is_underwater(target)
        )

    def _elevation_at(self, level: int, square: Square) -> int:
        """The ground height of a square in feet. Off-map ground is the default.

        The other choke point, and it answers a fixture the same way: a sluice
        that floods a room also drops what the room sits at, so height moves
        under a fight exactly as terrain does. A claim with no height pair falls
        through — most fixtures change what a square *is* without moving it.

        Ground height stays sparse where terrain went dense, and that is the
        document's own shape rather than a choice made here: a level records a
        datum and the squares that depart from it, so an off-map square answers
        the datum instead of raising, exactly as it did.
        """
        claim = self._feature_squares.get((level, square))
        if claim is not None and claim.elevation is not None:
            if self._is_open(claim.feature):
                return claim.elevation.open
            return claim.elevation.closed
        return self._levels[level].elevation.at(square)

    def _entry_cost(self, level: int, square: Square) -> int | None:
        """Feet to enter a square, or ``None`` off the map or into a wall.

        The per-square question, which is the one placement and "can a move end
        here" ask. What a *step* costs is :meth:`_step_cost`, because that
        depends on where the step came from.
        """
        if not self._on_map(square):
            return None
        effect = self._terrain_effect(level, square)
        if not effect.passable:
            return None
        return FEET_PER_SQUARE * effect.move_cost_multiplier

    def _step_cost(
        self,
        level: int,
        origin: Square,
        step_to: Square,
        doubled_diagonal: bool = False,
        *,
        actor: Creature | None = None,
        movement_mode: MovementMode = MovementMode.WALK,
    ) -> int | None:
        """Feet to step between two adjacent squares, or ``None`` if it cannot be taken.

        The single composer every charged move goes through — the routed path and
        the caller's explicit one alike, so a hand-written route costs exactly
        what the pathfinder would have charged for it. A change in ground height
        makes the step a slope or a climb; see
        :func:`~fivee_sim.kernel.grid.step_cost_feet` for what each costs.

        Both squares are on ``level``. Crossing between storeys is not a step:
        it goes through a connector, and :meth:`_connector_cost` prices it.
        """
        if not self._on_map(step_to):
            return None
        effect = self._terrain_effect(level, step_to)
        kind = self._terrain_at_level(level, step_to)
        if movement_mode is MovementMode.FLY:
            # Flight ignores ground drag and elevation, not solid architecture.
            # Replacing the whole effect made a wall passable as a side effect.
            effect = replace(effect, move_cost_multiplier=1)
        elif movement_mode is MovementMode.SWIM and effect.underwater:
            effect = replace(effect, move_cost_multiplier=1)
        elif actor is not None and kind in actor.terrain_cost_overrides:
            effect = replace(effect, move_cost_multiplier=1)
        rise = self._elevation_at(level, step_to) - self._elevation_at(level, origin)
        if movement_mode is MovementMode.CLIMB:
            rise = 0
        return step_cost_feet(
            effect,
            rise,
            doubled_diagonal=doubled_diagonal,
        )

    def _connector_cost(self, level: int, square: Square, to_level: int) -> int | None:
        """Feet to ride a connector to the same square one storey over.

        The rise between the two planes' heights at that square, charged through
        the very rule an ordinary step is: a ten-foot storey is a climb, a
        shallow half-landing is a slope. ``None`` if the arrival is impassable.
        """
        if self._entry_cost(to_level, square) is None:
            return None
        return step_cost_feet(
            self._terrain_effect(to_level, square),
            self._elevation_at(to_level, square) - self._elevation_at(level, square),
        )

    def _opaque(self, level: int, square: Square) -> bool:
        return self._on_map(square) and self._terrain_effect(level, square).opaque

    def _cover_of(self, level: int, square: Square) -> int:
        """The cover a square contributes to a sight line. Opaque means total."""
        if not self._on_map(square):
            return 0
        effect = self._terrain_effect(level, square)
        if effect.opaque:
            return int(CoverGrade.TOTAL)
        return effect.cover

    def _occupied(self, level: int) -> dict[Square, str]:
        """Which squares conscious creatures hold on one storey.

        A downed body blocks nothing, and neither does a creature a floor away:
        occupancy is per plane, so two fighters may stand at the same square on
        different levels.
        """
        return {
            to_square(as_point(creature.position)): creature.name
            for creature in self.creatures.values()
            if creature.combat_active and creature.level == level
        }

    def route(
        self,
        actor_name: str,
        goal: Square,
        *,
        stop_adjacent: bool = False,
        max_cost: int | None = None,
        movement_mode: MovementMode = MovementMode.WALK,
    ) -> Path | None:
        """The cheapest route the named creature could walk to ``goal``, or ``None``.

        Public for the auto-play policy, which must plan movement with the same
        rules the stepper charges for it. Squares held by conscious enemies
        block; allies can be crossed. A mapless fight has no routes — movement
        there is free-form. ``stop_adjacent`` accepts any square next to the
        goal, which is how you walk *to* a creature; ``max_cost`` abandons
        routes over budget.
        """
        if self.map_document is None:
            return None
        actor = self.creatures[actor_name]
        level = actor.level
        blocked = frozenset(
            square for square, name in self._occupied(level).items()
            if name != actor_name and self.creatures[name].team != actor.team
        )
        return find_path(
            to_square(as_point(actor.position)),
            goal,
            step_cost=lambda origin, step_to, doubled: self._step_cost(
                level,
                origin,
                step_to,
                doubled,
                actor=actor,
                movement_mode=movement_mode,
            ),
            rule=self.movement_rule,
            bounds=(self.map_document.grid.width, self.map_document.grid.height),
            blocked=blocked,
            stop_adjacent=stop_adjacent,
            max_cost=max_cost,
        )

    def flight_cost(
        self,
        actor_name: str,
        destination: Point,
        to_level: int,
    ) -> int | None:
        """Cost of a legal direct flight, or ``None`` when it cannot end there.

        This is the planning seam used by auto-play.  It mirrors the live
        action's destination, occupancy, and vertical-distance gates so a
        proposed cross-storey move will not be refused when resolved.
        """
        if self.map_document is None or to_level not in self.map_document.levels:
            return None
        actor = self.creatures[actor_name]
        if actor.fly_speed <= 0:
            return None
        dest_sq = to_square(destination)
        if not self._on_map(dest_sq) or self._entry_cost(to_level, dest_sq) is None:
            return None
        holder = self._occupied(to_level).get(dest_sq)
        if holder is not None and holder != actor_name:
            return None
        origin = as_point(actor.position)
        vertical = abs(
            self._elevation_at(to_level, dest_sq)
            - self._elevation_at(actor.level, to_square(origin))
        )
        return max(distance_feet(origin, destination, self.movement_rule), vertical)

    def cover_between(self, attacker_name: str, target_name: str) -> CoverGrade:
        """The cover the target has against the attacker, on this fight's map.

        Public for the same reason as :meth:`attack_advantage`: the auto-play
        policy must weigh cover with the same eyes the stepper resolves it, not
        re-derive it. Without a map there is no cover at all.
        """
        if self.map_document is None:
            return CoverGrade.NONE
        attacker = self.creatures[attacker_name]
        return self._cover_from_square(
            attacker.level, to_square(as_point(attacker.position)), target_name
        )

    def _cover_from_square(
        self, level: int, origin: Square, target_name: str
    ) -> CoverGrade:
        """The cover the target has against an effect measured from ``origin``.

        The one composition every cover question goes through: attacks measure
        from the attacker's square, area effects from their point of origin.
        Intervening creatures cap at half, exactly as for attacks; the origin
        and target squares themselves never block, so a creature standing in
        the origin square — the caster of a cone, say — does not screen anyone.

        A floor is opaque, so a target on another storey has total cover — which
        is what stops a fighter shooting the ceiling out from under someone
        standing at the same square one level up. That is the whole of what a
        level does to sight; within one, nothing changed.
        """
        if self.map_document is None:
            return CoverGrade.NONE
        target = self.creatures[target_name]
        if target.level != level:
            visible = self._sight_links[level].get(origin, frozenset())
            if target.level not in visible:
                return CoverGrade.TOTAL
        occupied = frozenset(
            square for square, name in self._occupied(level).items()
            if name != target_name
        )
        return grid_cover_between(
            origin,
            to_square(as_point(target.position)),
            cover_of=lambda square: self._cover_of(level, square),
            occupied=occupied,
        )

    # --- queries ----------------------------------------------------------
    @property
    def current_name(self) -> str:
        return self.order[self.turn_index]

    @property
    def current(self) -> Creature:
        return self.creatures[self.current_name]

    @property
    def action_available(self) -> bool:
        return not self._turn.action_used

    @property
    def bonus_action_available(self) -> bool:
        return not self._turn.bonus_action_used

    def teams(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for creature in self.creatures.values():
            grouped.setdefault(creature.team, []).append(creature.name)
        return grouped

    def living_teams(self) -> set[str]:
        return {c.team for c in self.creatures.values() if c.contesting}

    @property
    def over(self) -> bool:
        return len(self.living_teams()) <= 1

    @property
    def winner(self) -> str | None:
        alive = self.living_teams()
        return next(iter(alive)) if len(alive) == 1 else None

    def enemies_of(self, name: str) -> list[Creature]:
        """The conscious enemies this creature can actually reach or be reached by.

        On a map with storeys that means the ones sharing its level: a floor
        between two combatants is total cover both ways, so an enemy upstairs
        threatens nothing down here and cannot be threatened from here either.
        """
        actor = self.creatures[name]
        visible_levels = {actor.level}
        if self.map_document is not None:
            origin = to_square(as_point(actor.position))
            visible_levels.update(self._sight_links[actor.level].get(origin, ()))
        return [
            c for c in self.creatures.values()
            if c.team != actor.team and c.combat_active and c.level in visible_levels
        ]

    def brief(self, as_name: str) -> dict[str, Any]:
        """The fight as one combatant is entitled to know it.

        :meth:`state` is the referee's view and reports every creature's hit
        points, AC, slots and items — so handing it to a player hands them the
        other side's sheet. This is the same fight with the other side reduced to
        what anybody at the table can see: where it is, how far off, what
        conditions are on it, and how badly hurt it looks.

        **Your own side is not redacted.** Players share numbers with each other
        at a real table, so allies come back whole and so does the asker — the
        boundary is the other side, not other people. Which side that is depends
        on who asked: a monster's brief redacts the party.

        A creature the asker cannot see is **absent rather than redacted**, since
        "something is over there and you cannot see it" is itself a fact they do
        not have.

        **This is a projection, not an access control, and the difference
        matters.** ``as_name`` is asserted by the caller and authenticated by
        nothing — the engine has one per-launch token and no per-seat credential
        — so a client that can ask for this brief can equally ask for
        :meth:`state` and get the whole fight. What it buys is a payload a
        *cooperating* client can render without holding secrets it must remember
        not to draw, which is the failure client-side hiding actually has. It is
        not a boundary against a client that does not want to cooperate, and
        nothing here should be cited as one.
        """
        return self.brief_of(self.state(), as_name)

    def unseen_by(self, snapshot: Mapping[str, Any], as_name: str) -> frozenset[str]:
        """Everyone in this fight the seat may not be told about.

        Two reasons a creature lands here, and they are different in kind. One is
        a fact the snapshot carries: a reinforcement rolled into initiative but
        not yet on the battlefield. The other is a *relationship* the snapshot
        cannot carry — total cover is a question about two creatures and a map,
        not a field on either — so it is asked of the live encounter. That is why
        the brief is a method here rather than a function of ``state()`` output
        somewhere downstream: nothing but the fight can answer it.

        Cover is asked about the other side only. An ally behind a sealed wall is
        still reported, because a party at a real table is talking to each other.

        The asker is never here. A seat is always reported to the person sitting
        in it, including on the round before they arrive.
        """
        seats = self._seats_of(snapshot, as_name)
        side = seats[as_name]["team"]
        hidden = set()
        for name, one in seats.items():
            if name == as_name:
                continue
            if not one.get("present", True):
                hidden.add(name)
            elif (
                one["team"] != side
                and self.cover_between(as_name, name) is CoverGrade.TOTAL
            ):
                hidden.add(name)
        return frozenset(hidden)

    def brief_of(self, snapshot: Mapping[str, Any], as_name: str) -> dict[str, Any]:
        """:meth:`brief`, over a snapshot this encounter produced.

        Taking the snapshot rather than reading the fight directly is what lets
        one projection answer both doors. ``encounter.brief`` hands it
        :meth:`state`; the operations that *write* hand it the very snapshot they
        were about to answer with, so a chair posting an action is narrowed
        without a second renderer and without a second shape.

        It also means a ``map_source`` — a path on the host's filesystem, which
        ``service.encounters.resume`` staples onto its snapshot — cannot arrive
        by accident. Nothing is copied from the top of the snapshot; every key
        below is named.

        One thing is read from the live fight rather than from the argument, and
        :meth:`unseen_by` says why: total cover is not in any snapshot. For an
        idempotent retry, whose recorded answer is a snapshot one action old,
        that is the fight as it stands deciding who may be named in an answer
        about the fight as it was — the narrower reading of the two.
        """
        seats = self._seats_of(snapshot, as_name)
        asker = seats[as_name]
        unseen = self.unseen_by(snapshot, as_name)
        turn = snapshot["turn"]
        payload: dict[str, Any] = {
            "as": as_name,
            "round": snapshot["round"],
            # Nulled rather than named when an unseen creature is acting:
            # identity is withheld and existence is not, which is what a table
            # learns when the GM rolls behind a screen. Reporting the name would
            # undo every other filter here in one key.
            "turn": None if turn in unseen else turn,
            "your_turn": turn == as_name,
            "over": snapshot["over"],
            "winner": snapshot["winner"],
            "you": dict(asker),
            "allies": [],
            "enemies": [],
        }
        # The asker's own budget, and only on their own turn: the turn state
        # belongs to whoever is acting, so reporting it otherwise would describe
        # somebody else's remaining movement as though it were yours.
        if payload["your_turn"]:
            payload["turn_state"] = dict(snapshot["turn_state"])
        block = snapshot.get("map")
        if isinstance(block, Mapping):
            payload["map"] = _briefed_map(block)

        rule = DiagonalRule(snapshot["movement_rule"])
        origin = _point_of(asker["position"])
        for name in snapshot["order"]:
            if name == as_name or name in unseen:
                continue
            other = seats[name]
            distance = distance_feet(origin, _point_of(other["position"]), rule)
            if other["team"] == asker["team"]:
                payload["allies"].append(dict(other) | {"distance": distance})
                continue
            payload["enemies"].append(
                {
                    key: value
                    for key, value in other.items()
                    if key in ENEMY_VISIBLE_KEYS
                }
                | {"distance": distance, "health": health_band(other)}
            )
        return payload

    def brief_events(
        self,
        events: Sequence[Mapping[str, Any]],
        snapshot: Mapping[str, Any],
        as_name: str,
    ) -> list[dict[str, Any]]:
        """One operation's account of what just happened, narrowed to a seat.

        :meth:`brief` serves no events, so for a release the four operations that
        do answered a seat with the fight's own account of itself, unredacted.
        The brief said a foe was "hurt" and the ``damage`` event beside it said
        6594/7700; an ``attack`` event's ``total`` bracketed the AC it was rolled
        against; ``use_item`` reported an item's remaining charges. An event is
        therefore classified exactly as a creature is.

        **The cast is the same cast**, because it is computed the same way from
        the same snapshot: a creature :meth:`brief_of` omitted must not be named
        by an event served beside it, or the payload that redacted them reveals
        them.
        """
        seats = self._seats_of(snapshot, as_name)
        side = seats[as_name]["team"]
        unseen = self.unseen_by(snapshot, as_name)
        briefed: list[dict[str, Any]] = []
        for raw in events:
            entry = _briefed_event(
                raw, own=_side_of(raw, seats) == side, unseen=unseen
            )
            if entry is not None:
                briefed.append(entry)
        return briefed

    def _seats_of(
        self, snapshot: Mapping[str, Any], as_name: str
    ) -> dict[str, Mapping[str, Any]]:
        """This fight's cast by name, refusing a seat it does not hold.

        The refusal is not politeness: a projection keyed on team membership
        answers an unknown name with a brief in which every creature is an
        opponent — well formed, plausible, and a lie about who asked.

        It deliberately does **not** list the cast, and that is a change from the
        sentence this used to raise. Naming everybody handed a player-chair
        client the very names the projection exists to withhold, ambushers
        included: a refusal that discloses is the leak wearing an error's
        clothes.
        """
        seats: dict[str, Mapping[str, Any]] = {
            str(one["name"]): one for one in snapshot["combatants"]
        }
        if as_name not in seats:
            raise EncounterError(f"no combatant named {as_name!r} in this encounter")
        return seats

    def set_condition(self, target_name: str, condition: str, *, applied: bool) -> None:
        """Impose or lift a condition by the table's ruling rather than by a rule.

        Every other condition here arrives from something that models its own
        ending: a spell holds it under concentration, an attack rider anchors it
        to a turn boundary, prone ends by standing. A ruling has none of those,
        so it registers **no ongoing effect** and lasts until the table lifts it.
        Nothing can expire it and no lost concentration breaks it.

        That gap is why this exists. A combatant could be given a condition when
        the fight was built and then never be rid of it, because the three
        removal paths all belong to a mechanism that imposed the condition
        itself. A circumstance the rules name but do not mechanise had no way
        back out — SRD surprise is Disadvantage on one Initiative roll and
        nothing afterwards, so the condition carrying it must come off once
        initiative is past.

        Lifting also clears any ongoing effect sustaining the same condition on
        the same creature, so a ruling ends a spell's grip rather than being
        quietly reimposed by the ledger the next time it is consulted.
        """
        if target_name not in self.creatures:
            known = ", ".join(sorted(self.creatures)[:MAX_LISTED_COMBATANTS])
            raise EncounterError(
                f"no combatant named {target_name!r} in this encounter; there is: {known}"
            )
        target = self.creatures[target_name]
        if applied:
            # ``add_condition`` looks the name up before recording it, so an
            # unknown condition is refused here rather than carried. It is
            # also the immunity gate: a ruling is a fourth path into it, and
            # gets no exemption from what an attack, a spell or an item
            # already cannot do to this target.
            if not target.add_condition(condition):
                self._emit(
                    "effect_apply", "", target_name,
                    f"{condition} not imposed by ruling — {target_name} is immune",
                    condition=condition, applied=False, ruling=True,
                )
                return
            self._emit(
                "effect_apply", "", target_name,
                f"{condition} imposed by ruling",
                condition=condition, applied=True, ruling=True,
            )
            return
        self._effects[:] = [
            effect for effect in self._effects
            if not (effect.target == target_name and effect.condition == condition)
        ]
        target.remove_condition(condition)
        self._emit(
            "effect_end", "", target_name,
            f"{condition} lifted by ruling",
            condition=condition, ruling=True,
        )

    def state(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "turn": self.current_name,
            "movement_rule": self.movement_rule.value,
            "over": self.over,
            "winner": self.winner,
            "order": list(self.order),
            "turn_state": self._turn.as_dict(),
            "map": self._map_state(),
            "ongoing_effects": [
                {
                    "id": effect.id,
                    "source": effect.source,
                    "name": effect.name,
                    "target": effect.target,
                    "condition": effect.condition,
                    "concentration": effect.concentration,
                    "stacked": effect.stacked,
                    "expires_phase": effect.expires_phase,
                    "expires_anchor": effect.expires_anchor,
                }
                for effect in self._effects
            ],
            "combatants": [self._creature_state(c) for c in
                           (self.creatures[n] for n in self.order)],
        }

    def _map_state(self) -> dict[str, Any] | None:
        if self.map_document is None or self.map_state is None:
            return None
        return {
            "name": self.map_document.name,
            "width": self.map_document.grid.width,
            "height": self.map_document.grid.height,
            "movement_rule": self.movement_rule.value,
            "elevation": self._elevation_summary(GROUND_LEVEL),
            "levels": [
                self._level_summary(index) for index in sorted(self.map_document.levels)
            ],
            "features": {
                name: self._feature_summary(name, feature)
                for name, feature in sorted(self._fixtures.items())
            },
        }

    def _feature_summary(self, name: str, feature: MapFeatureRecord) -> dict[str, Any]:
        """One fixture, reporting only what it actually carries.

        A plain door says the four things a door has always said. Everything
        below is omitted at its default, so the common case stays exactly as
        wide as it was and the keys that do appear are the ones a caller has to
        act on: what else moves with it, what it waits for, what is still
        missing, what may operate it automatically, and what operating it costs.

        **Written out key by key from a record that carries more**, which is the
        one thing to keep about it. A ``MapFeatureRecord`` also holds ``team``,
        ``to_level``, ``sight_to_levels``, ``facing``, ``hinge`` and ``swing``;
        none is emitted, and each is classified in
        :data:`FEATURE_WITHHELD_KEYS` so that adding one is a decision somebody
        makes rather than a field that arrives because the shape widened
        underneath. ``self._fixtures`` is the other half of that: a spawn hint
        carries a ``team`` naming which side arrives where, and it is not a
        fixture, so it is not here at all.
        """
        assert self.map_document is not None and self.map_state is not None
        summary: dict[str, Any] = {
            "square": list(feature.at),
            "kind": feature.kind,
            "level": self._fixture_level[name],
            "open": name in self.map_state.open_features,
        }
        beyond = sorted(
            square for overlay in feature.affects for square in overlay.cells
        )
        if beyond:
            summary["affects"] = [list(square) for square in beyond]
        if feature.requires:
            summary["requires"] = list(feature.requires)
            unmet = [
                wanted for wanted in feature.requires
                if wanted not in self.map_state.open_features
            ]
            if unmet:
                summary["blocked_by"] = unmet
        if feature.costs_action:
            summary["costs_action"] = True
        if feature.check is not None:
            summary["check"] = {
                "ability": feature.check.ability.value,
                "dc": feature.check.dc,
            }
        if feature.linked_to is not None:
            summary["linked_to"] = feature.linked_to
        if feature.trigger is not None:
            summary["trigger"] = {
                "when": {
                    dependency: "open" if expected else "closed"
                    for dependency, expected in feature.trigger.when
                },
                "set": "open" if feature.trigger.set_open else "closed",
                "mode": feature.trigger.mode.value,
            }
        return summary

    def _level_summary(self, level: int) -> dict[str, Any]:
        """One storey: its heights and the squares that lead off it."""
        return {
            "index": level,
            "elevation": self._elevation_summary(level),
            "connectors": [
                {"square": [square[0], square[1]], "to_level": target}
                for square, target in sorted(self._connectors[level].items())
            ],
        }

    def _elevation_summary(self, level: int) -> dict[str, Any]:
        """One storey's ground heights in feet, and what they do — and do not — do.

        Live, not authored. The block this sits in already reports every
        fixture's real state, and the creature standing in a flooded room
        already reports the height it fell to, so a range read off the file
        would contradict both inside one payload. It resolves through the same
        ``_feature_squares`` index :meth:`_elevation_at` consults, and the index
        is keyed by ``(level, square)`` — a sluice on the ground moves no
        gallery.

        ``flat`` is the fact a reader needs first. The default only counts
        toward the range when some square actually falls back to it, so a map
        whose sparse layer covers every square reports the heights it really
        has — and a claimed square never falls back, in either state, so it
        covers the plane exactly as an authored height does.
        """
        assert self.map_document is not None
        elevation = self._levels[level].elevation
        grid = self.map_document.grid
        decided: dict[Square, int] = dict(elevation.squares)
        for (claimed_level, square), claim in self._feature_squares.items():
            if claimed_level == level and claim.elevation is not None:
                decided[square] = self._elevation_at(level, square)
        heights = list(decided.values())
        covered = len(decided) == (grid.width * grid.height)
        if not covered:
            heights.append(elevation.default)
        return {
            "default": elevation.default,
            "min": min(heights),
            "max": max(heights),
            "flat": min(heights) == max(heights),
            "affects": "movement only; sight, cover, and areas are measured flat",
        }

    def _creature_state(self, creature: Creature) -> dict[str, Any]:
        state: dict[str, Any] = {
            "name": creature.name,
            "team": creature.team,
            "hp": creature.hp,
            "max_hp": creature.max_hp,
            "ac": creature.ac,
            "speeds": {
                "walk": creature.speed,
                "climb": creature.climb_speed,
                "swim": creature.swim_speed,
                "fly": creature.fly_speed,
            },
            "senses": {
                "darkvision": creature.darkvision,
                "blindsight": creature.blindsight,
            },
            "terrain_cost_overrides": sorted(creature.terrain_cost_overrides),
            "death_rule": creature.death_rule.value,
            "bonus_actions": sorted(creature.bonus_actions),
            "redirect_attack": creature.redirect_attack,
            "arrival_round": creature.arrival_round,
            "present": creature.arrived,
            "position": list(as_point(creature.position)),
            # Omitted when nobody is tracking it, which is what keeps every
            # existing fight's payload byte-identical — and what the replay
            # bundle's state slots inherit, since they are this dictionary.
            **({"facing": creature.facing} if creature.facing is not None else {}),
            "initiative": self.initiative[creature.name],
            "conditions": sorted(creature.conditions),
            "concentrating_on": creature.concentrating_on,
            "dodging": self._dodging[creature.name],
            "disengaged": self._disengaged[creature.name],
            "reaction_available": self._reaction_available[creature.name],
            "conscious": creature.conscious,
            "surrendered": creature.surrendered,
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
        # Only where it means something: a fight on the open plane has no ground
        # to stand on, and reporting 0 feet there would read as a fact.
        if self.map_document is not None:
            state["level"] = creature.level
            state["elevation"] = self._elevation_at(
                creature.level, to_square(as_point(creature.position))
            )
        return state

    # --- turn lifecycle ---------------------------------------------------
    def _begin_turn(self, rng: Random, natural: tuple[int, ...] = ()) -> None:
        creature = self.current
        self._dodging[creature.name] = False
        self._disengaged[creature.name] = False
        self._reaction_available[creature.name] = True
        if not creature.arrived:
            self._turn = TurnState(
                movement_left=0,
                action_used=True,
                bonus_action_used=True,
                attacks_left=0,
            )
            return
        self._resolve_attached_damage(creature, rng)
        if creature.dying:
            self._death_save(creature, rng, natural)
        elif natural:
            # Refused rather than dropped, as everywhere else a face is
            # reported for a roll that is not going to happen.
            raise EncounterError(
                f"{creature.name} makes no death save this turn, so there is "
                "no face to report"
            )
        # The budget is derived *after* the death save: a natural 20 regains
        # 1 hit point (SRD 5.2.1, "Death Saving Throws", Rolling 20), and the
        # revived creature is conscious for the rest of this turn — nothing in
        # the rules forfeits its movement for having been down when the turn
        # began. Deriving it first froze ``movement_left`` at 0 for the whole
        # turn while ``attacks_left`` was granted regardless.
        maximum_speed = max(
            creature.speed,
            creature.climb_speed,
            creature.swim_speed,
            creature.fly_speed,
        )
        if any(link.source == creature.name for link in self._attachments):
            maximum_speed = 0
        self._turn = TurnState(
            movement_left=0 if not creature.conscious else maximum_speed,
            action_used=False,
            attacks_left=creature.attacks_per_action,
        )
        # A death save can kill, and :meth:`_death_save` marks the creature dead
        # without going through ``take_damage``, so nothing else would notice.
        self._reconcile_concentration()

    def _attach(
        self, source: Creature, target: Creature, option: AttackOption
    ) -> None:
        assert option.attached_damage is not None
        assert option.attached_damage_type is not None
        for link in list(self._attachments):
            if link.source == source.name:
                self._detach(link, detail="attaches to a new target")
        link = Attachment(
            source=source.name,
            target=target.name,
            damage=option.attached_damage,
            damage_type=option.attached_damage_type,
            detach_after_damage=option.detach_after_damage,
        )
        self._attachments.append(link)
        self._emit(
            "attach",
            source.name,
            target.name,
            detail=f"{source.name} attaches to {target.name}",
            damage=str(option.attached_damage),
            damage_type=option.attached_damage_type.value,
            detach_after_damage=option.detach_after_damage,
        )

    def _resolve_attached_damage(self, source: Creature, rng: Random) -> None:
        for link in list(self._attachments):
            if link.source != source.name:
                continue
            target = self.creatures[link.target]
            if source.dead or target.dead:
                self._detach(link, detail="attachment ended")
                continue
            roll = roll_dice(link.damage, rng)
            damage = roll.total
            if self._resisted_by_target(target, link.damage_type):
                damage //= 2
            if link.damage_type in target.immunities:
                damage = 0
            elif link.damage_type in target.vulnerabilities:
                damage *= 2
            link.damage_dealt += damage
            self._emit(
                "attached_damage",
                source.name,
                target.name,
                detail=f"attached damage {roll.describe()} -> {damage}",
                damage=damage,
                damage_type=link.damage_type.value,
                total_drained=link.damage_dealt,
            )
            self._apply_damage(target, damage, rng, damage_types=(link.damage_type,))
            if target.dead or (
                link.detach_after_damage > 0
                and link.damage_dealt >= link.detach_after_damage
            ):
                self._detach(link, detail="detaches after feeding")

    def _detach(self, link: Attachment, *, detail: str) -> None:
        if link not in self._attachments:
            return
        self._attachments.remove(link)
        self._emit("detach", link.source, link.target, detail=detail)

    def _death_save(
        self, creature: Creature, rng: Random, natural: tuple[int, ...] = ()
    ) -> None:
        roll = roll_d20(rng, supplied=natural or None)
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
            # SRD 5.2.1, Death Saving Throws: "The number of both is reset to zero when
            # you regain any Hit Points or become Stable." ``Creature.heal`` already
            # covers the first clause; this is the second. Leaving the counters
            # standing let a creature that was stabilised, then knocked down again,
            # re-stabilise on its very next roll — even a *failed* one, since the
            # failure landed short of three while the stale successes still tripped
            # this branch.
            creature.death_save_successes = 0
            creature.death_save_failures = 0
            self._emit("stabilised", creature.name, detail="three successful death saves")

    def _arrive_for_round(self) -> None:
        """Make every scheduled reinforcement for the current round present."""
        for creature in sorted(self.creatures.values(), key=lambda entry: entry.name):
            if creature.arrived or creature.arrival_round > self.round:
                continue
            creature.arrived = True
            self._emit(
                "arrival",
                creature.name,
                detail=f"{creature.name} arrives in round {self.round}",
                arrival_round=creature.arrival_round,
                position=list(as_point(creature.position)),
                level=creature.level,
            )

    def advance(self, rng: Random, natural: tuple[int, ...] = ()) -> list[Event]:
        """End the current turn and begin the next, wrapping the round.

        ``natural`` is the face the creature whose turn is *starting* rolled for
        its own death save, for a player who would rather roll their own. At
        most one death save happens per advance — it is taken at the start of a
        dying creature's turn — so one reported face is never ambiguous.
        """
        before = len(self.log)
        in_round, by = self.round, self.current_name
        self._emit("turn_end", self.current_name)
        self._expire_timed("end", self.current_name)
        if not self.over:
            for _ in range(len(self.order)):
                self.turn_index += 1
                if self.turn_index >= len(self.order):
                    self.turn_index = 0
                    self.round += 1
                    self._emit("round", detail=f"round {self.round} begins",
                               round=self.round)
                    self._arrive_for_round()
                if (
                    not self.creatures[self.current_name].dead
                    and not self.creatures[self.current_name].surrendered
                ):
                    break
                # A dead creature's slot still passes: both its turn boundaries
                # go by without it acting, and a rider anchored to either must
                # expire now rather than never. The slot passing is the trigger,
                # not the creature acting.
                self._expire_timed("start", self.current_name)
                self._expire_timed("end", self.current_name)
            self._emit("turn_start", self.current_name)
            self._expire_timed("start", self.current_name)
            self._begin_turn(rng, natural)
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
        if not actor.arrived:
            raise EncounterError(
                f"{actor.name} does not arrive until round {actor.arrival_round}"
            )
        if not actor.conscious:
            raise EncounterError(f"{actor.name} is not conscious and cannot act")
        if not actor.active:
            held = ", ".join(sorted(actor.conditions))
            raise EncounterError(f"{actor.name} is incapacitated ({held}) and cannot act")
        if action.natural and action.kind not in _KINDS_THAT_MAY_ROLL:
            # Refused rather than quietly dropped. Somebody rolled a die and
            # said what it read; an engine that ignored it would be telling them
            # their roll counted when it did not.
            raise EncounterError(
                f"a {action.kind.value} rolls no d20, so there is no face to report"
            )

        match action.kind:
            case ActionKind.ATTACK:
                self._do_attack(actor, action, rng)
            case ActionKind.CAST:
                self._do_cast(actor, action, rng)
            case ActionKind.MOVE:
                self._do_move(actor, action, rng)
            case ActionKind.DASH:
                self._spend_action_budget(actor, action, "dash")
                dash_mode = action.movement_mode or MovementMode.WALK
                dash_speed = self._movement_speed(actor, dash_mode)
                self._turn.movement_left += dash_speed
                self._emit("dash", actor.name,
                           detail=f"movement now {self._turn.movement_left} ft",
                           movement_left=self._turn.movement_left,
                           movement_mode=dash_mode.value,
                           as_bonus_action=action.as_bonus_action)
            case ActionKind.DISENGAGE:
                self._spend_action_budget(actor, action, "disengage")
                self._disengaged[actor.name] = True
                self._emit(
                    "disengage", actor.name,
                    detail="no opportunity attacks this turn",
                    as_bonus_action=action.as_bonus_action,
                )
            case ActionKind.USE_ITEM:
                self._do_use_item(actor, action, rng)
            case ActionKind.INTERACT:
                self._do_interact(actor, action, rng)
            case ActionKind.STAND:
                self._do_stand(actor)
            case ActionKind.DODGE:
                self._require_action(actor)
                self._turn.action_used = True
                self._dodging[actor.name] = True
                self._emit("dodge", actor.name,
                           detail="attacks against this creature have disadvantage")
            case ActionKind.SURRENDER:
                actor.surrendered = True
                self._emit(
                    "surrender", actor.name,
                    detail=f"{actor.name} surrenders and leaves the fight",
                )
        # The fourth route: an action can land an incapacitating condition on a
        # creature that is concentrating, and ``Creature.add_condition`` clears the
        # field from inside the model, where no release could be issued. A spell or
        # item that imposes one without dealing damage reaches nothing else.
        #
        # This runs before the action is recorded so that any release it emits falls
        # inside that record's event span; recording first would leave the events
        # orphaned between one action and the next.
        self._reconcile_concentration()
        # After the action, so an explicit facing beats whatever a move derived,
        # and so it applies to actions that derive nothing. Set even on a
        # creature nobody was tracking: naming a facing on the call *is* the act
        # of tracking it, which is the one way an untracked creature gains one.
        if action.facing is not None:
            actor.facing = action.facing
        self.actions.append(ActionRecord(
            index=len(self.actions), round=self.round, actor=actor.name, action=action,
            first_event=before, event_count=len(self.log) - before,
        ))
        return self.log[before:]

    def _require_action(self, actor: Creature) -> None:
        if self._turn.action_used:
            raise EncounterError(f"{actor.name} has already taken an action this turn")

    def _spend_action_budget(
        self, actor: Creature, action: Action, action_name: str
    ) -> None:
        """Spend the ordinary or explicitly authored Bonus Action budget."""
        if action.as_bonus_action:
            if action_name not in actor.bonus_actions:
                raise EncounterError(
                    f"{actor.name} cannot {action_name} as a bonus action"
                )
            if self._turn.bonus_action_used:
                raise EncounterError(
                    f"{actor.name} has already taken a bonus action this turn"
                )
            self._turn.bonus_action_used = True
            return
        self._require_action(actor)
        self._turn.action_used = True

    def _resolve_target(self, name: str | None) -> Creature:
        if name is None:
            raise EncounterError("this action needs a target")
        target = self.creatures.get(name)
        if target is None:
            # Named, not merely refused. Every sibling lookup in this file answers
            # with what it *does* have — the actor's attacks, the actor's items,
            # the map's features — because the caller's next move is to pick one
            # of them. A bare "no combatant named 'Bob'" left a caller unable to
            # tell a misspelling from an absent creature, and unable to guess the
            # label a ``{"creature": ...}`` spec assigned on their behalf.
            names = sorted(self.creatures)
            listed = ", ".join(names[:MAX_LISTED_COMBATANTS])
            if len(names) > MAX_LISTED_COMBATANTS:
                listed += f", ... and {len(names) - MAX_LISTED_COMBATANTS} more"
            raise EncounterError(f"no combatant named {name!r}; the fight has: {listed}")
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
        if not target.arrived:
            raise EncounterError(
                f"{target.name} does not arrive until round {target.arrival_round}"
            )
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

    def _require_loaded(self, actor: Creature, option: AttackOption) -> None:
        """Refuse a shot the weapon cannot take, before anything at all is spent.

        Both refusals *raise*, where being out of reach or behind total cover
        only emits. That line is deliberate and it is about who can see the
        reason: geometry is the engine's to know — a caller cannot work out from
        a snapshot that a pillar is in the way — while an empty quiver is a
        number on the shooter's own sheet, already published as
        ``state()["combatants"][…]["items"]``. So the first is reported and the
        second is refused, in the shape of the spell-slot refusal, which likewise
        raises before the slot or the action is spent.

        The ammunition name is matched exactly rather than case-folded, unlike
        :meth:`_pick_item`'s caller-supplied string: nothing here came from a
        caller. Both halves are authored — the attack names the entry and the
        stat block carries it — so a mismatch is a content defect, and one that
        refuses every shot loudly rather than firing a phantom arrow.
        """
        if option.ammunition is not None and actor.items.get(option.ammunition, 0) <= 0:
            raise EncounterError(
                f"{actor.name} has no {option.ammunition} left to fire {option.name}"
            )
        if option.loading and self._turn.loading_used:
            raise EncounterError(
                f"{option.name} has the Loading property and {actor.name} has "
                "already fired one this turn"
            )

    def _fire(self, actor: Creature, option: AttackOption, distance: int) -> int | None:
        """Spend one piece of what this attack fires; the count left, or ``None``.

        Called only where a swing is actually charged for, which is why it is not
        folded into :meth:`_require_loaded`: a refused, out-of-reach or
        totally-covered attack costs the turn nothing and must cost the quiver
        nothing either.

        **A thrown weapon used in melee spends nothing**, and that is a ruling
        rather than an optimisation. SRD 5.2.1's Thrown property (catalog
        ``583-9-4-8-thrown``) is what *"enables a ranged attack by throwing the
        weapon"*: a javelin thrown leaves the hand and lies where it landed, a
        javelin used to stab does not. So a melee-resolved swing returns before
        the count is touched and the event carries no ``ammunition_remaining``.

        :meth:`_require_loaded` is deliberately *not* given the same exemption.
        The count is the javelins held, not a magazine beside them, so a
        thrower who has thrown all three has nothing left in hand to stab with
        and is refused. Possession is required either way; only spending is
        conditional.

        ``loading`` returns early with it, for the same reason and with no
        SRD case behind it: no printed weapon is both Loading and Thrown, but a
        stab is not a shot and must not consume the turn's one shot.
        """
        if option.resolves_as_melee(distance):
            return None
        if option.loading:
            self._turn.loading_used = True
        if option.ammunition is None:
            return None
        remaining = actor.items[option.ammunition] - 1
        actor.items[option.ammunition] = remaining
        return remaining

    def _do_attack(self, actor: Creature, action: Action, rng: Random) -> None:
        """One attack, from the turn's budget through to the damage it deals.

        **Loading is approximated per turn.** SRD 5.2.1, "Equipment" ->
        "Weapon Properties", Loading, caps the weapon at one attack per
        *activation* — an action, a Bonus Action or a Reaction — and the catalog
        records those three (``580-9-4-5-loading``). This gate is per turn, which
        is behaviourally identical under everything this stepper does today:
        nothing here consults ``as_bonus_action`` when attacking, and the only
        reaction attack is :meth:`_opportunity_attack`, which picks a melee
        option and so can never be a Loading one. Give the stepper a Bonus Action
        attack or a ranged reaction and the two readings part company — that is
        the moment to move the flag off :class:`TurnState` and onto whatever
        represents the activation.

        A melee swing after a Loading shot stays legal, and that is not an
        oversight: the property restricts the weapon, not the wielder, so a
        crossbow-then-blade turn is exactly what the rule allows.
        """
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
        # Before ``_redirect_attack_target``, which is not the query it reads as:
        # it swaps two creatures' squares, spends the defender's reaction and
        # emits. A refusal after it would bill the defender for a shot that was
        # never taken.
        self._require_loaded(actor, option)
        target = self._redirect_attack_target(actor, target)
        distance = actor.distance_to(target, self.movement_rule)
        reach = option.max_distance()
        if distance > reach:
            self._emit("attack", actor.name, target.name,
                       f"{option.name} cannot reach ({distance} ft > {reach} ft)",
                       attack=option.name, out_of_range=True)
            return
        underwater = self._is_underwater(actor)
        if (
            underwater
            and not option.resolves_as_melee(distance)
            and option.normal_range > 0
            and distance > option.normal_range
        ):
            self._turn.attacks_left -= 1
            if self._turn.attacks_left == actor.attacks_per_action - 1:
                self._turn.action_used = True
            # An arrow loosed into the water is an arrow gone. This branch is
            # the one place a shot is charged for outside the ordinary path
            # below, so it has to charge for the ammunition too.
            spent: dict[str, Any] = {}
            fired = self._fire(actor, option, distance)
            if fired is not None:
                spent["ammunition_remaining"] = fired
            self._emit(
                "attack",
                actor.name,
                target.name,
                f"{option.name} automatically misses beyond normal range underwater",
                attack=option.name,
                hit=False,
                underwater=True,
                underwater_auto_miss=True,
                damage=0,
                **spent,
            )
            return
        # Total cover refuses the attack before it is spent, exactly as being out
        # of reach does: there is no roll to make against a target that cannot be
        # targeted, so nothing is consumed and nothing random happens.
        grade = self.cover_between(actor.name, target.name)
        if grade is CoverGrade.TOTAL:
            self._emit("attack", actor.name, target.name,
                       f"{option.name} has no line to {target.name} (total cover)",
                       attack=option.name, total_cover=True)
            return

        # Both computed before the attack is charged for. ``check_faces`` refuses
        # a reported face that this roll cannot use, and a refusal that had
        # already decremented ``attacks_left`` would cost the swing as well —
        # leaving a caller who mistyped their die unable to retry it.
        advantage = self.attack_advantage(actor, target, option)
        check_faces(action.natural or None, advantage)

        self._turn.attacks_left -= 1
        if self._turn.attacks_left == actor.attacks_per_action - 1:
            self._turn.action_used = True
        # Beside the swing it is spent with, and before the roll: a hit and a
        # miss cost the same arrow.
        fired = self._fire(actor, option, distance)

        cover_bonus = cover_ac_bonus(grade)
        resolution = resolve_attack(
            rng,
            attack_bonus=option.attack_bonus,
            target_ac=target.ac + cover_bonus,
            damage=option.damage,
            advantage=advantage,
            forced_critical=self.attack_forced_critical(actor, target),
            resisted=self._resisted_by_target(target, option.damage_type),
            vulnerable=option.damage_type in target.vulnerabilities,
            immune=option.damage_type in target.immunities,
            supplied=action.natural or None,
            **self._rider_damage_arguments(actor, option, target),
        )
        cover_note = ""
        if grade is not CoverGrade.NONE:
            label = "half" if grade is CoverGrade.HALF else "three-quarters"
            cover_note = f" ({label} cover, +{cover_bonus} AC)"
        extras: dict[str, Any] = {}
        if resolution.advantage_damage is not None:
            extras["advantage_bonus_damage"] = resolution.advantage_damage.total
            extras["advantage_bonus_reason"] = resolution.advantage_damage_reason
        if resolution.bonus_damage is not None:
            extras["bonus_damage"] = resolution.bonus_damage_dealt
        # Only when the attack names ammunition, so no other attack event in the
        # engine changes shape for a rule it does not use.
        if fired is not None:
            extras["ammunition_remaining"] = fired
        self._emit("attack", actor.name, target.name,
                   f"{option.name}: {resolution.describe()}{cover_note}",
                   attack=option.name,
                   hit=resolution.hit,
                   critical=resolution.critical,
                   natural=resolution.attack.roll.natural,
                   total=resolution.attack.total,
                   advantage=resolution.advantage.value,
                   damage=resolution.total_damage_dealt,
                   cover=int(grade),
                   underwater=underwater,
                   **extras)
        if resolution.hit:
            self._apply_damage(
                target, resolution.total_damage_dealt, rng,
                critical=resolution.critical,
                damage_types=self._attack_damage_types(option),
            )
            if option.on_hit_condition is not None:
                self._apply_attack_rider(actor, target, option, rng)
            if option.on_hit_attach and target.conscious:
                self._attach(actor, target, option)

    @staticmethod
    def _attack_damage_types(option: AttackOption) -> tuple[DamageType, ...]:
        """Every damage type one hit with this attack delivers as one instance.

        The main pool's type, plus the bonus pool's when the attack carries one.
        Undead Fortitude reads this: Radiant in either pool bypasses the save.
        """
        if option.bonus_damage_type is None:
            return (option.damage_type,)
        return (option.damage_type, option.bonus_damage_type)

    def _rider_damage_arguments(
        self, actor: Creature, option: AttackOption, target: Creature
    ) -> dict[str, Any]:
        """The damage-rider keywords one attack passes to ``resolve_attack``.

        Shared by the attack action and the opportunity attack, because the
        riders belong to the attack option itself: a centipede's bite is the
        same bite as a reaction. The bonus pool's defenses are read against its
        **own** type, which is the point of it having one.
        """
        return {
            "advantage_bonus_damage": option.advantage_bonus_damage,
            "advantage_bonus_damage_applies": (
                option.advantage_bonus_with_adjacent_ally
                and self._capable_ally_adjacent(actor=actor, target=target)
            ),
            "bonus_damage": option.bonus_damage,
            "bonus_resisted": (
                self._resisted_by_target(target, option.bonus_damage_type)
                if option.bonus_damage_type is not None else False
            ),
            "bonus_vulnerable": (
                option.bonus_damage_type in target.vulnerabilities
                if option.bonus_damage_type is not None else False
            ),
            "bonus_immune": (
                option.bonus_damage_type in target.immunities
                if option.bonus_damage_type is not None else False
            ),
        }

    def _redirect_attack_target(
        self, attacker: Creature, target: Creature
    ) -> Creature:
        """Apply an authored Redirect Attack reaction, returning the new target."""
        if (
            not target.redirect_attack
            or not self._reaction_available.get(target.name, False)
            or not target.active
            or not self._can_see(target, attacker)
        ):
            return target
        eligible = sorted(
            (
                ally
                for ally in self.creatures.values()
                if ally is not target
                and ally.team == target.team
                and ally.active
                and ally.level == target.level
                and fits_within(ally.size, Size.MEDIUM)
                and ally.distance_to(target, self.movement_rule) <= MELEE_THRESHOLD
            ),
            key=lambda ally: (ally.hp, ally.name),
        )
        if not eligible:
            return target
        redirected = eligible[0]
        target.position, redirected.position = redirected.position, target.position
        self._reaction_available[target.name] = False
        self._emit(
            "redirect_attack",
            target.name,
            redirected.name,
            f"{target.name} swaps places with {redirected.name}, redirecting the attack",
            attacker=attacker.name,
            original_target=target.name,
            redirected_target=redirected.name,
            original_position=as_point(target.position),
            redirected_position=as_point(redirected.position),
        )
        return redirected

    def attack_advantage(
        self, actor: Creature, target: Creature, option: AttackOption
    ) -> Advantage:
        """Advantage an attack would resolve under, worked out without rolling it.

        Public because the auto-play policy has to weigh an attack before taking it.
        A policy that re-derived advantage could quietly disagree with the stepper it
        is driving, so both ask this one function instead.
        """
        distance = actor.distance_to(target, self.movement_rule)
        unseen_advantage, unseen_disadvantage = self._sight_advantage(actor, target)
        return compute_attack_advantage(
            attacker_conditions=actor.conditions,
            target_conditions=target.conditions,
            distance=distance,
            long_range_penalty=option.has_long_range_penalty(distance),
            extra_advantage=(
                int(self._pack_tactics_applies(actor, target))
                + unseen_advantage
            ),
            extra_disadvantage=(
                int(self._dodge_benefits(target))
                + int(
                    not option.resolves_as_melee(distance)
                    and self._ranged_close_combat_penalty(actor)
                )
                + int(self._underwater_attack_penalty(actor, option, distance))
                + unseen_disadvantage
            ),
            condition_effects=self.condition_effects,
        )

    def _underwater_attack_penalty(
        self, actor: Creature, option: AttackOption, distance: int
    ) -> bool:
        # Both branches key off how the swing *resolves*, not off ``kind``: a
        # javelin at arm's length is a melee attack made underwater and takes
        # the melee rule (none, here — it is Piercing), while the same javelin
        # thrown is a ranged one and always takes the penalty.
        if not self._is_underwater(actor):
            return False
        if not option.resolves_as_melee(distance):
            return True
        return actor.swim_speed <= 0 and option.damage_type is not DamageType.PIERCING

    def _sight_advantage(
        self, actor: Creature, target: Creature
    ) -> tuple[int, int]:
        """What sight alone contributes to an attack roll: ``(advantage, disadvantage)``.

        **This is where the Invisible condition's "Attacks Affected" clause lives**,
        and it lives here whole rather than half here and half in the kernel table.
        SRD 5.2.1, Invisible: "Attack rolls against you have Disadvantage, and your
        attack rolls have Advantage. If a creature can somehow see you, you don't
        gain this benefit against that creature." That last sentence is a
        relationship between two creatures — and, on a map, between them and the
        light and cover around them — so :mod:`~fivee_sim.kernel.conditions` cannot
        state it: a row there knows one condition and no creatures at all.

        The bundled row therefore sets ``unseen`` and stops. It used to *also* set
        ``attacked_with_disadvantage`` and ``own_attacks_have_advantage``, which
        said the same thing a second time and said it unconditionally; the two
        copies disagreed for any attacker with Blindsight in range, and only the
        2024 combination rule — where Advantage never stacks — kept the
        disagreement out of sight everywhere else. One rule, one owner, and this is
        the owner because :meth:`_can_see` is the thing that can answer it.

        Both flags remain declarable by a content pack, because a pack may want the
        unconditional shape. A pack that wants Invisible's shape declares ``unseen``
        and gets the withdrawal for free, exactly as the bundled condition does.
        """
        return (
            int(not self._can_see(target, actor)),
            int(not self._can_see(actor, target)),
        )

    def _can_see(self, observer: Creature, subject: Creature) -> bool:
        """Whether ``observer`` can see ``subject`` for a rule that requires sight."""
        distance = observer.distance_to(subject, self.movement_rule)
        if observer.blindsight > 0 and distance <= observer.blindsight:
            return self.cover_between(observer.name, subject.name) is not CoverGrade.TOTAL
        if any(
            effect_of(condition, self.condition_effects).cannot_see
            for condition in observer.conditions
        ):
            return False
        if any(
            effect_of(condition, self.condition_effects).unseen
            for condition in subject.conditions
        ):
            return False
        if self.cover_between(observer.name, subject.name) is CoverGrade.TOTAL:
            return False
        illumination = self._illumination_at(subject)
        if illumination is not LightLevel.DARKNESS:
            return True
        return observer.darkvision > 0 and distance <= observer.darkvision

    def _illumination_at(self, creature: Creature) -> LightLevel:
        """How well lit the square this creature stands on is.

        The lights are a tuple of ``(square, light)`` pairs read off the storey
        once at adoption, rather than a runtime type carrying its own square.
        There was one — ``LightSource`` — and it held nothing a
        :class:`~fivee_sim.map_types.MapLight` and the square it was found on do
        not already say, so it went with the rest of the bridge. A pair also
        keeps what a keyed table would have lost: two features may stand on one
        square, and both burn.
        """
        if self.map_document is None:
            return LightLevel.BRIGHT
        square = to_square(as_point(creature.position))
        brightest = self._ambient[creature.level]
        for at, light in self._lights[creature.level]:
            distance = distance_feet(
                square_center(at), square_center(square), self.movement_rule
            )
            if light.bright > 0 and distance <= light.bright:
                return LightLevel.BRIGHT
            if light.dim > 0 and distance <= light.dim:
                brightest = LightLevel.DIM
        return brightest

    def _ranged_close_combat_penalty(self, actor: Creature) -> bool:
        """Whether a capable, nearby enemy can see a ranged attacker."""
        return any(
            enemy is not actor
            and enemy.team != actor.team
            and enemy.active
            and enemy.distance_to(actor, self.movement_rule) <= MELEE_THRESHOLD
            and self._can_see(enemy, actor)
            for enemy in self.creatures.values()
        )

    def _pack_tactics_applies(self, actor: Creature, target: Creature) -> bool:
        """Whether Pack Tactics grants ``actor`` Advantage against ``target``.

        SRD 5.2.1 (Wolf): Advantage on an attack roll "if at least one of the
        wolf's allies is within 5 feet of the creature and the ally doesn't have
        the Incapacitated condition." An ally is another member of the actor's
        team — never the actor, never the target — and a capable one:
        :attr:`Creature.active` reads consciousness plus the ``incapacitated``
        flag off the fight's own condition table, so a pack-defined condition
        that incapacitates disqualifies exactly as the SRD ones do. Fed into
        :func:`compute_attack_advantage` as one more Advantage source rather
        than applied afterwards, so it cancels against Disadvantage under the
        one combination rule.
        """
        if not actor.pack_tactics:
            return False
        return self._capable_ally_adjacent(actor=actor, target=target)

    def _capable_ally_adjacent(
        self, *, actor: Creature, target: Creature
    ) -> bool:
        """Whether the attacking team has another capable creature by the target."""
        return any(
            ally is not actor
            and ally is not target
            and ally.team == actor.team
            and ally.active
            and ally.distance_to(target, self.movement_rule) <= MELEE_THRESHOLD
            for ally in self.creatures.values()
        )

    def attack_forced_critical(self, actor: Creature, target: Creature) -> bool:
        """Whether a landed hit would be upgraded to a critical one. See above.

        **One function serves the swing path and the cast path**, and takes no
        :class:`AttackOption`, because the rule reads nothing about the attack: SRD
        5.2.1 scopes it on the target's condition and the attacker's distance alone —
        "Any attack roll that hits you is a Critical Hit if the attacker is within 5
        feet of you." A second copy taking a weapon would be a copy that could
        disagree with this one.
        """
        return melee_hit_is_critical(
            target_conditions=target.conditions,
            distance=actor.distance_to(target, self.movement_rule),
            condition_effects=self.condition_effects,
        )

    def spell_attack_advantage(
        self, actor: Creature, target: Creature, spell: Spell
    ) -> Advantage:
        """Advantage a spell attack against ``target`` would resolve under.

        The cast path's counterpart to :meth:`attack_advantage`, and deliberately
        the same call underneath rather than a second derivation: SRD 5.2.1 defines
        an attack roll as "a D20 Test that represents making an attack with a
        weapon, an Unarmed Strike, or a spell", and no source of Advantage
        distinguishes them. A Blinded caster, a Dodging target and a Restrained one
        have to read the same either way, which they cannot if two functions decide
        it.

        The spell's attack kind matters only for the stateful close-combat source.
        Existing packs default to ranged; melee spell attacks opt in explicitly.

        Pack Tactics rides along for the same reason the call is shared: the
        trait names "an attack roll", and a spell attack is one.

        :meth:`_sight_advantage` rides along for a sharper version of the same
        reason. It was the one source this method assembled differently, and the
        paragraph above is the promise that broke: a Blinded caster read the same
        either way only because Blinded's own kernel flag happened to agree with
        the sight term the swing path added and this one did not. Once Invisible's
        withdrawal moved into that term, a spell attack on an Invisible target
        would have read Advantage where a swing read Disadvantage.
        """
        unseen_advantage, unseen_disadvantage = self._sight_advantage(actor, target)
        return compute_attack_advantage(
            attacker_conditions=actor.conditions,
            target_conditions=target.conditions,
            distance=actor.distance_to(target, self.movement_rule),
            extra_advantage=(
                int(self._pack_tactics_applies(actor, target)) + unseen_advantage
            ),
            extra_disadvantage=(
                int(self._dodge_benefits(target))
                + int(
                    spell.attack_kind is AttackKind.RANGED
                    and self._ranged_close_combat_penalty(actor)
                )
                + unseen_disadvantage
            ),
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
        if action.item is None:
            raise EncounterError("using an item needs 'item'")
        for attack in actor.attacks:
            if attack.ammunition is not None and attack.ammunition.casefold() == (
                action.item.casefold()
            ):
                raise EncounterError(
                    f"{attack.ammunition!r} is ammunition — {actor.name} spends it "
                    f"automatically when firing {attack.name}, not by using it"
                )
        name = self._pick_item(actor, action.item)
        effect = self.items.get(name)
        if effect is None:
            available = ", ".join(sorted(self.items)) or "none"
            raise EncounterError(
                f"{name!r} is not defined by the loaded content; defined: {available}"
            )
        if effect.action_cost is ActionCost.BONUS_ACTION:
            if self._turn.bonus_action_used:
                raise EncounterError(f"{actor.name} has already used a bonus action this turn")
        else:
            if action.as_bonus_action:
                raise EncounterError(f"{name} takes an action, not a bonus action")
            self._require_action(actor)

        if action.target is not None:
            target = self._resolve_target(action.target)
        elif effect.targets_others:
            raise EncounterError(f"{name} needs a target")
        else:
            target = actor
        if target is not actor:
            distance = actor.distance_to(target, self.movement_rule)
            if distance > MELEE_THRESHOLD:
                raise EncounterError(
                    f"{target.name} is {distance} ft away; an item can only be used on "
                    f"another creature within {MELEE_THRESHOLD} ft"
                )
        self._require_targetable(target)

        if effect.action_cost is ActionCost.BONUS_ACTION:
            self._turn.bonus_action_used = True
        else:
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
            action_cost=effect.action_cost.value,
        )
        if resolution.healed:
            before = target.hp
            target.heal(resolution.healed)
            self._emit("heal", target=target.name,
                       detail=f"{target.hp - before} hit points restored, "
                              f"{target.hp}/{target.max_hp}",
                       amount=target.hp - before, hp=target.hp, max_hp=target.max_hp)
        if resolution.damage_dealt:
            self._apply_damage(
                target, resolution.damage_dealt, rng,
                damage_types=(
                    (effect.damage_type,) if effect.damage_type is not None else ()
                ),
            )
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
        if action.spell is None:
            raise EncounterError("casting needs a spell name")
        if action.spell not in actor.spells:
            raise EncounterError(f"{actor.name} does not have {action.spell!r} prepared")
        spell = self.spellbook.get(action.spell)
        if spell is None:
            raise EncounterError(f"unknown spell {action.spell!r}")
        # Which budget this cast wants, checked here rather than at the top of the
        # method, because the answer belongs to the spell and the spell is only just
        # resolved. Same two branches as :meth:`_do_use_item` — SRD 5.2.1 prints
        # "Casting Time: Bonus Action" on Healing Word and Mass Healing Word, and an
        # ordinary spell offered the wrong budget is refused rather than quietly
        # promoted.
        if spell.action_cost is ActionCost.BONUS_ACTION:
            if self._turn.bonus_action_used:
                raise EncounterError(
                    f"{actor.name} has already used a bonus action this turn"
                )
        else:
            if action.as_bonus_action:
                raise EncounterError(
                    f"{spell.name} takes an action, not a bonus action"
                )
            self._require_action(actor)
        slot_level = action.slot_level if action.slot_level is not None else spell.level
        # Every reason to refuse is gathered before a single thing is spent. The
        # slot-level check in particular used to live only inside ``resolve_spell``,
        # which runs after the action is marked used and the slot decremented — so a
        # refusal cost the caster both, and arrived as a bare ``ValueError`` no
        # adapter catches. Validate here, in the layer that owns the state and
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

        chosen, area_origin = self._spell_targets(actor, spell, action)
        if not chosen:
            raise EncounterError(f"{spell.name} has no valid targets")
        if spell.requires_attack_roll:
            chosen = [self._redirect_attack_target(actor, target) for target in chosen]

        # Cover shields a spell exactly as it shields a weapon swing: +2 behind
        # half cover, +5 behind three-quarters, on AC and on Dexterity saves
        # alike (SRD 5.2.1, "Cover"). Only the origin differs between the branches
        # — an area measures from its point of origin, a named cast from the
        # caster's own square — so both go through one dictionary rather than the
        # area alone, which is how named targets came to consult cover nowhere.
        #
        # Total cover never appears here: ``area_targets`` drops anyone sealed
        # off from the origin, and ``_spell_targets`` refuses a named one. That
        # is load-bearing rather than incidental — ``cover_ac_bonus`` raises on
        # TOTAL rather than inventing a number for "cannot be hit at all".
        origin = (
            area_origin if area_origin is not None
            else to_square(as_point(actor.position))
        )
        cover_grades: dict[str, CoverGrade] = {
            c.name: self._cover_from_square(actor.level, origin, c.name)
            for c in chosen
        }

        def save_modifier(creature: Creature) -> int:
            if spell.save_ability is None:
                return 0
            modifier = creature.save_modifier(spell.save_ability)
            grade = cover_grades.get(creature.name, CoverGrade.NONE)
            if spell.save_ability is Ability.DEXTERITY and grade is not CoverGrade.NONE:
                modifier += cover_ac_bonus(grade)
            return modifier

        # Everything about a reported face is settled here, before the action,
        # the slot, and any held concentration are spent. ``resolve_spell``
        # refuses the same two cases, but only once all three are already gone —
        # and a caster who mistyped a die would have paid for a spell that never
        # resolved.
        if action.natural:
            if not spell.requires_attack_roll:
                raise EncounterError(
                    f"{spell.name} makes no attack roll, so there is no face to "
                    "report; its targets roll their own saves and the engine "
                    "rolls those"
                )
            if len(chosen) > 1:
                raise EncounterError(
                    f"{spell.name} rolls a separate attack against each of "
                    f"{len(chosen)} targets, so one reported face cannot say "
                    "which roll it is; cast at one target, or let the engine roll"
                )
            check_faces(
                action.natural, self.spell_attack_advantage(actor, chosen[0], spell)
            )

        if spell.action_cost is ActionCost.BONUS_ACTION:
            self._turn.bonus_action_used = True
        else:
            self._turn.action_used = True
        if spell.level > 0:
            actor.spell_slots[slot_level] = actor.spell_slots.get(slot_level, 0) - 1

        if spell.concentration:
            # SRD 5.2.1, "Concentration": "You lose Concentration on an effect the
            # moment you start casting a spell that requires Concentration." The
            # release sits exactly here: after every refusal, because a refused
            # cast never starts and must not drop the hold — and before
            # ``resolve_spell``, because the old spell's conditions must not
            # shape the new one's resolution. Releasing after resolution let a
            # caster recast at its own victim and have the victim auto-fail the
            # new save through a paralysis the first cast was still holding. An
            # unconditional release covers recasting the *same* spell on a new
            # target, which comparing names could not see.
            self._end_concentration(actor)

        resolution = resolve_spell(
            rng,
            spell,
            slot_level=slot_level,
            save_dc=actor.spell_save_dc,
            spell_attack_bonus=actor.spell_attack_bonus,
            spellcasting_modifier=actor.spellcasting_modifier,
            targets=tuple(
                SpellTarget(
                    name=c.name,
                    ac=c.ac + cover_ac_bonus(
                        cover_grades.get(c.name, CoverGrade.NONE)
                    ),
                    save_modifier=save_modifier(c),
                    auto_fail_save=self.auto_fails_save(c, spell.save_ability),
                    save_advantage=self.save_advantage(c, spell.save_ability),
                    # Filled for every target rather than only for attack-roll
                    # spells, the way the save fields are filled for every target
                    # of an attack-roll one. Both are cheap, neither consumes
                    # randomness, and ``resolve_spell`` reads each pair only on the
                    # branch it belongs to.
                    attack_advantage=self.spell_attack_advantage(actor, c, spell),
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
            supplied=action.natural or None,
        )
        detail = f"{spell.name} (slot {slot_level})"
        if resolution.damage_roll is not None:
            detail += f", damage {resolution.damage_roll.describe()}"
        if resolution.healing_roll is not None:
            detail += f", healing {resolution.healing_roll.describe()}"
        self._emit("cast", actor.name, detail=detail,
                   spell=spell.name,
                   slot_level=slot_level,
                   # Which budget this cost, the same way ``use_item`` reports it: a
                   # Healing Word and a Cure Wounds are different kinds of turn, and
                   # a log that renders them identically loses that. Already in
                   # :data:`EVENT_VISIBLE_KEYS` — an action economy is spent in the
                   # open at a real table.
                   action_cost=spell.action_cost.value,
                   center=as_point(action.center) if action.center is not None else None,
                   targets=[c.name for c in chosen])

        if spell.concentration:
            # The old effect was released before ``resolve_spell``; the new one
            # is recorded here, before the results are applied, so a caster
            # caught in its own damaging area rolls the concentration save for
            # the spell it just cast.
            actor.concentrating_on = spell.name
            self._emit("concentration", actor.name, detail=f"concentrating on {spell.name}",
                       spell=spell.name, held=True, started=True)

        for result in resolution.results:
            target = self.creatures[result.name]
            shielding: dict[str, Any] = {}
            grade = cover_grades.get(result.name, CoverGrade.NONE)
            if grade is not CoverGrade.NONE:
                shielding["cover"] = int(grade)
            self._emit("spell_effect", actor.name, target.name, result.describe(),
                       spell=spell.name,
                       damage=result.damage_dealt,
                       affected=result.affected,
                       saved=result.save.success if result.save is not None else None,
                       condition=result.condition_applied,
                       **shielding)
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
                    damage_types=(
                        (spell.damage_type,) if spell.damage_type is not None else ()
                    ),
                )
            if result.healed:
                before = target.hp
                target.heal(result.healed)
                self._emit(
                    "heal",
                    actor.name,
                    target.name,
                    detail=f"{target.hp - before} hit points restored, "
                    f"{target.hp}/{target.max_hp}",
                    spell=spell.name,
                    amount=target.hp - before,
                    hp=target.hp,
                    max_hp=target.max_hp,
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

    def origin_visible(self, caster_name: str, origin: Point | int) -> bool:
        """Whether the caster has a sight line to a point of origin.

        Public because the auto-play policy filters candidate placements with it,
        and it must use the same eyes the stepper refuses with. Without a map
        there is nothing to hide behind.
        """
        if self.map_document is None:
            return True
        caster = self.creatures[caster_name]
        return has_line_of_sight(
            to_square(as_point(caster.position)),
            to_square(as_point(origin)),
            opaque=lambda square: self._opaque(caster.level, square),
        )

    _DIRECTIONS = frozenset({
        (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1),
    })

    def area_targets(
        self,
        spell: Spell,
        caster_name: str,
        *,
        center: Point | int | None = None,
        direction: Point | None = None,
        toward: str | Point | None = None,
    ) -> list[Creature]:
        """Every living creature one placement of an area spell catches.

        The single membership authority: the stepper resolves a cast through it
        and the auto-play policy values candidate placements through it, so the
        two can never disagree about who is inside an area. Sphere and cylinder
        membership is measured in feet from the centre on an open plane and by
        template squares on a battle map; cones, lines, cubes, and emanations
        are square templates always, because their published shapes are grid
        figures.

        On a battle map, a creature with **total cover** from the effect's point
        of origin — a sphere or cylinder's centre, a cube's minimum corner, the
        caster's own square for a cone, line, or emanation — is not inside the
        area at all: the effect cannot reach behind a sealed wall, however the
        template falls. Range and the caster's own sight to the origin are the
        caller's checks — this answers only who the effect reaches.

        An emanation pours from the caster like a cone or line and always
        excludes the caster (SRD 5.2.1: "isn't included in the area of effect
        unless its creator decides otherwise" — no bundled spell opts otherwise,
        so there is no way to ask for it yet). A cylinder is centred like a
        sphere and, unlike an emanation, includes its origin square — that
        inclusion/exclusion split is the one crisp behavioural difference between
        the two shapes. A cylinder's ``height`` is not consulted here: the
        engine's areas are 2-D.
        """
        caster = self.creatures[caster_name]
        caster_square = to_square(as_point(caster.position))
        origin_square: Square = caster_square
        match spell.effective_shape:
            case SpellShape.SPHERE:
                if center is None:
                    raise EncounterError(f"{spell.name} needs 'center'")
                centre = as_point(center)
                if self.map_document is None:
                    return [
                        c for c in self.creatures.values()
                        if c.arrived and not c.dead
                        and distance_feet(
                            as_point(c.position), centre, self.movement_rule
                        ) <= spell.radius
                    ]
                origin_square = to_square(centre)
                squares = sphere_squares(
                    origin_square, spell.radius, rule=self.movement_rule
                )
            case SpellShape.CONE:
                if direction is None or tuple(direction) not in self._DIRECTIONS:
                    raise EncounterError(
                        f"{spell.name} needs 'direction': one of the eight unit "
                        f"offsets, such as [1, 0] or [-1, 1]"
                    )
                squares = cone_squares(caster_square, direction, spell.length)
            case SpellShape.LINE:
                if toward is None:
                    raise EncounterError(
                        f"{spell.name} needs 'toward': a combatant name or a point "
                        f"to aim the line at"
                    )
                if isinstance(toward, str):
                    aim = to_square(as_point(self._resolve_target(toward).position))
                else:
                    aim = to_square(as_point(toward))
                if aim == caster_square:
                    raise EncounterError(
                        f"{spell.name} cannot be aimed at the caster's own square"
                    )
                squares = line_squares(caster_square, aim, spell.length)
            case SpellShape.CUBE:
                if center is None:
                    raise EncounterError(
                        f"{spell.name} needs 'center': the cube's minimum corner"
                    )
                origin_square = to_square(as_point(center))
                squares = cube_squares(origin_square, spell.size)
            case SpellShape.EMANATION:
                # Pours from the caster's own square like a cone or line, so
                # there is no separate origin to name — see the docstring above.
                squares = sphere_squares(
                    caster_square, spell.radius, rule=self.movement_rule
                )
            case SpellShape.CYLINDER:
                if center is None:
                    raise EncounterError(f"{spell.name} needs 'center'")
                centre = as_point(center)
                if self.map_document is None:
                    return [
                        c for c in self.creatures.values()
                        if c.arrived and not c.dead
                        and distance_feet(
                            as_point(c.position), centre, self.movement_rule
                        ) <= spell.radius
                    ]
                origin_square = to_square(centre)
                squares = sphere_squares(
                    origin_square, spell.radius, rule=self.movement_rule
                )
            case _:
                raise EncounterError(f"{spell.name} is not an area spell")
        caught = [
            c for c in self.creatures.values()
            if c.arrived and not c.dead and to_square(as_point(c.position)) in squares
        ]
        if spell.effective_shape is SpellShape.EMANATION:
            # SRD 5.2.1: the origin "isn't included in the area of effect
            # unless its creator decides otherwise" — always excluded here.
            caught = [c for c in caught if c.name != caster_name]
        if self.map_document is None:
            return caught
        return [
            c for c in caught
            if self._cover_from_square(caster.level, origin_square, c.name)
            is not CoverGrade.TOTAL
        ]
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
    ) -> tuple[list[Creature], Square | None]:
        """Resolve who a spell lands on, enforcing its range, sight, and target cap.

        Returns the creatures caught and, when the spell resolved *as an area*,
        the square its effect pours out of — the origin cover against the effect
        is measured from. Named-target casts return ``None`` there: they are
        aimed at creatures, not at a point.

        The branches are range-checked differently, and deliberately. Named targets
        are checked one at a time, exactly as a single-target spell is. A sphere,
        cube, or cylinder is checked at its **point of origin** only — those
        creatures come from the template rather than from the caller, so refusing
        the whole spell because one creature at the far edge of the blast sits
        past the range would be wrong — and on a battle map the origin must also
        be visible. A cone, line, or emanation pours out of the caster, so there
        is nothing to range-check at all.

        **Consciousness is not a filter.** SRD 5.2.1, "Spells" -> Targets -> Areas of
        Effect: "The area determines what the spell targets." A creature at 0 hit
        points is inside the blast or it is not, and the Unconscious condition's own
        clause — "You automatically fail Strength and Dexterity saving throws" —
        exists precisely for the roll a Fireball then makes it attempt. Filtering
        here meant a blast centred on a dying creature's exact square dealt it
        nothing while burning everyone around it. Corpses are still excluded; the
        named branches refuse one outright rather than dropping it silently, so a
        caller who aims at a body is told, not ignored.
        """
        area_origin: Square | None = None
        if action.targets:
            chosen = [self._resolve_target(name) for name in action.targets]
            for creature in chosen:
                self._require_targetable(creature)
                self._require_in_range(actor, spell, creature.position, creature.name)
                self._require_line_to(actor, spell, creature)
        elif spell.is_area and (
            action.center is not None
            or action.direction is not None
            or action.toward is not None
            # An emanation, like a cone or line, pours from the caster's own
            # square, so it needs no aim at all — it is always in play once its
            # spell is cast as an area.
            or spell.effective_shape is SpellShape.EMANATION
        ):
            if spell.effective_shape in (
                SpellShape.SPHERE, SpellShape.CUBE, SpellShape.CYLINDER
            ):
                if action.center is None:
                    what = (
                        "'center'" if spell.effective_shape
                        in (SpellShape.SPHERE, SpellShape.CYLINDER)
                        else "'center' (its minimum corner)"
                    )
                    raise EncounterError(f"{spell.name} needs {what}")
                self._require_in_range(
                    actor, spell, action.center, "the point of origin"
                )
                if not self.origin_visible(actor.name, action.center):
                    raise EncounterError(
                        f"{actor.name} cannot see {spell.name}'s point of origin "
                        f"at {as_point(action.center)}"
                    )
                area_origin = to_square(as_point(action.center))
            else:
                area_origin = to_square(as_point(actor.position))
            chosen = self.area_targets(
                spell, actor.name,
                center=action.center,
                direction=action.direction,
                toward=action.toward,
            )
        elif action.target is not None:
            chosen = [self._resolve_target(action.target)]
            self._require_targetable(chosen[0])
            self._require_in_range(actor, spell, chosen[0].position, chosen[0].name)
            self._require_line_to(actor, spell, chosen[0])
        else:
            raise EncounterError(
                f"{spell.name} needs 'target', 'targets', or an area aim — "
                f"'center' for a sphere, cube, or cylinder, 'direction' for a "
                f"cone, 'toward' for a line"
            )
        if spell.is_area:
            # An area is bounded by its template, not by a head count. Every
            # bundled area spell leaves max_targets at its default of 1, so
            # applying the cap here would quietly shrink a Fireball to a single
            # creature.
            return chosen, area_origin
        if len(chosen) > spell.max_targets:
            creatures = "creature" if spell.max_targets == 1 else "creatures"
            raise EncounterError(
                f"{spell.name} affects at most {spell.max_targets} {creatures}; "
                f"{len(chosen)} were named"
            )
        return chosen, area_origin

    def _require_line_to(
        self, actor: Creature, spell: Spell, target: Creature
    ) -> None:
        """Refuse a named target the caster cannot see past total cover.

        SRD 5.2.1, "Cover": a target with Total Cover "can't be targeted directly".
        *Directly* is the word that keeps this out of the area branch — a blast
        reaches whoever its template catches, and ``area_targets`` drops the
        sealed ones without comment because nobody named them. Someone who names
        a creature behind a wall is told instead, the way naming one out of range
        is told.

        Raising rather than emitting is the spell path's own idiom: every other
        refusal here — range, a corpse, a slot too small — raises, and all of them
        run before the action or the slot is spent. The weapon path emits for the
        same case because a refused swing is one of several in a Multiattack and
        the log has to show which.
        """
        if self.cover_between(actor.name, target.name) is CoverGrade.TOTAL:
            raise EncounterError(
                f"{actor.name} has no line to {target.name} (total cover), "
                f"so {spell.name} cannot target it"
            )

    def _require_in_range(
        self, actor: Creature, spell: Spell, position: Point | int, what: str
    ) -> None:
        """Refuse a spell whose range does not reach ``position``."""
        if not spell.range_feet:
            return
        distance = distance_feet(
            as_point(position), as_point(actor.position), self.movement_rule
        )
        if distance > spell.range_feet:
            raise EncounterError(
                f"{what} is {distance} ft away, beyond {spell.name}'s "
                f"{spell.range_feet} ft range"
            )

    def _face_along(self, actor: Creature, frm: Point, to: Point) -> None:
        """Turn a facing-tracking creature to look the way its move went.

        Three rules, and each is a decision rather than a convenience.

        A creature with ``facing is None`` stays untracked. Facing is opt-in per
        combatant, so a first step must not silently enrol one — that would add
        a key to a payload its caller never asked for.

        A move with **no horizontal displacement leaves the facing alone**: a
        zero-distance move, a connector ride from the square you already stand
        on, a flying storey change straight up. The creature did not travel, so
        it did not turn, and :func:`facing_toward` refuses a bearing from a
        square to itself rather than inventing north.

        And the bearing is taken over the leg that ended the move, not the whole
        journey, so a mover that rounded a corner faces the way it was last
        going rather than back at where it set off.
        """
        if actor.facing is None or frm == to:
            return
        actor.facing = str(facing_toward(frm, to))

    def _do_move(self, actor: Creature, action: Action, rng: Random) -> None:
        """Move on the open plane: straight to the destination, provoking on the way.

        The cost is charged up front and the walk replays the straight segment
        in 5-ft samples, exactly as the mapped path replays its route square by
        square, so leaving any threatening enemy's reach on the way provokes —
        a pass-through is not laundered by where the move ends. The segment is
        straight and reach is convex, so a move that ends still within reach
        never left it and provokes nothing.
        """
        if action.to_position is None:
            raise EncounterError("moving needs 'to_position'")
        if speed_is_zero(actor.conditions, self.condition_effects):
            held = ", ".join(sorted(actor.conditions))
            raise EncounterError(f"{actor.name} has speed 0 ({held}) and cannot move")
        if self.map_document is not None:
            self._do_move_mapped(actor, action, rng)
            return
        if action.path:
            raise EncounterError("waypoints need a battle map; this fight has none")
        origin = as_point(actor.position)
        destination = as_point(action.to_position)
        distance = distance_feet(origin, destination, self.movement_rule)
        if distance > self._turn.movement_left:
            raise EncounterError(
                f"{actor.name} has {self._turn.movement_left} ft of movement, needs {distance} ft"
            )

        self._turn.movement_left -= distance
        self._emit("move", actor.name,
                   detail=f"{origin} -> {destination} ({distance} ft used)",
                   origin=origin, planned_destination=destination,
                   destination=destination, cost=distance, completed=True)
        move_event = self.log[-1]

        if self._disengaged[actor.name]:
            actor.position = destination
            self._face_along(actor, origin, destination)
            return
        samples = _segment_samples(origin, destination)
        for previous, step in zip(samples, samples[1:], strict=False):
            threatening = [
                enemy for enemy in self.enemies_of(actor.name)
                if distance_feet(
                    as_point(enemy.position), previous, self.movement_rule
                ) <= self._opportunity_attack_reach(enemy)
            ]
            actor.position = step
            for enemy in threatening:
                if distance_feet(
                    as_point(enemy.position), step, self.movement_rule
                ) <= self._opportunity_attack_reach(enemy):
                    continue
                self._opportunity_attack(enemy, actor, rng)
            if not actor.conscious:
                # Dropped mid-stride: the walk ends where the mover fell, and the
                # state — not the move event's declared destination — is the truth.
                move_event.data["destination"] = as_point(actor.position)
                move_event.data["completed"] = False
                self._face_along(actor, origin, as_point(actor.position))
                return
        actor.position = destination
        self._face_along(actor, origin, destination)

    def _do_move_mapped(self, actor: Creature, action: Action, rng: Random) -> None:
        """Move on the battle map: routed, terrain-costed, provoking on the way.

        The destination must be an on-map, passable, unoccupied square. The route
        is either the caller's explicit ``path``, validated leg by leg, or the
        cheapest one :func:`~fivee_sim.kernel.grid.find_path` finds; enemy-held
        squares block either way, allies' squares can be crossed but not ended
        in. The cost is charged up front and the walk replays square by square,
        so leaving any threatening enemy's reach on the way provokes — a
        pass-through is not laundered by where the move ends.
        """
        assert self.map_document is not None and action.to_position is not None
        origin = as_point(actor.position)
        origin_sq = to_square(origin)
        dest_sq = to_square(as_point(action.to_position))
        destination = square_center(dest_sq)
        level = actor.level
        movement_mode = action.movement_mode or MovementMode.WALK
        self._movement_speed(actor, movement_mode)
        # The connector is the last leg: walk to the stairway on this level, then
        # ride it. So every check below is about the square the walk ends on, on
        # the level the walk happens on, and the arrival is checked after.
        to_level = level if action.to_level is None else action.to_level
        if to_level != level and movement_mode is MovementMode.FLY:
            self._do_flying_level_change(
                actor, destination, dest_sq, to_level, movement_mode
            )
            return
        if to_level != level:
            if to_level not in self.map_document.levels:
                declared = ", ".join(str(i) for i in sorted(self.map_document.levels))
                raise EncounterError(
                    f"there is no level {to_level} on this map. Levels: {declared}"
                )
            if self._connectors[level].get(dest_sq) != to_level:
                raise EncounterError(
                    f"there is nothing at {dest_sq} that leads to level {to_level}; "
                    f"a move between storeys ends on a stairway"
                )
        if not self._on_map(dest_sq):
            raise EncounterError(
                f"{dest_sq} is off the {self.map_document.grid.width}x"
                f"{self.map_document.grid.height} map"
            )
        if self._entry_cost(level, dest_sq) is None:
            raise EncounterError(
                f"cannot end a move on impassable {self._terrain_at_level(level, dest_sq)!r} "
                f"at {dest_sq}"
            )
        occupied = self._occupied(level)
        holder = occupied.get(dest_sq)
        if holder is not None and holder != actor.name:
            raise EncounterError(f"square {dest_sq} is occupied by {holder}")
        enemy_squares = frozenset(
            square for square, name in occupied.items()
            if self.creatures[name].team != actor.team
        )

        if action.path:
            route = [origin_sq, *(to_square(as_point(point)) for point in action.path)]
            if route[-1] != dest_sq:
                raise EncounterError(
                    f"the path ends at {route[-1]}, not at the destination {dest_sq}"
                )
            cost = 0
            parity = 0
            for previous, step in zip(route, route[1:], strict=False):
                dx = abs(step[0] - previous[0])
                dy = abs(step[1] - previous[1])
                if max(dx, dy) != 1:
                    raise EncounterError(
                        f"path step {previous} -> {step} is not to an adjacent square"
                    )
                if not self._on_map(step):
                    raise EncounterError(
                        f"the path leaves the map at {step}"
                    )
                diagonal = bool(dx and dy) and (
                    self.movement_rule is DiagonalRule.FIVE_TEN_FIVE
                )
                entering = self._step_cost(
                    level,
                    previous,
                    step,
                    diagonal and bool(parity),
                    actor=actor,
                    movement_mode=movement_mode,
                )
                if entering is None:
                    raise EncounterError(
                        f"the path enters impassable "
                        f"{self._terrain_at_level(level, step)!r} at {step}"
                    )
                if step in enemy_squares:
                    raise EncounterError(
                        f"the path passes through {occupied[step]}'s square {step}"
                    )
                cost += entering
                if diagonal:
                    parity ^= 1
        else:
            found = self.route(actor.name, dest_sq, movement_mode=movement_mode)
            if found is None:
                raise EncounterError(
                    f"no route to {dest_sq}: walls, terrain, or enemies block the way"
                )
            route = list(found.squares)
            cost = found.cost_feet
        if to_level != level:
            climb = self._connector_cost(level, dest_sq, to_level)
            if climb is None:
                raise EncounterError(
                    f"the connector at {dest_sq} arrives on impassable "
                    f"{self._terrain_at_level(to_level, dest_sq)!r} on level {to_level}"
                )
            arrival = self._occupied(to_level).get(dest_sq)
            if arrival is not None and arrival != actor.name:
                raise EncounterError(
                    f"square {dest_sq} on level {to_level} is occupied by {arrival}"
                )
            cost += climb
        if cost > self._turn.movement_left:
            raise EncounterError(
                f"{actor.name} has {self._turn.movement_left} ft of movement, "
                f"needs {cost} ft"
            )

        self._turn.movement_left -= cost
        travel_detail = (
            f"{origin} -> {destination}"
            if level == to_level
            else f"{origin} [level {level}] -> {destination} [level {to_level}]"
        )
        self._emit("move", actor.name,
                   detail=f"{travel_detail} ({cost} ft used)",
                   origin=origin, planned_destination=destination,
                   destination=destination, cost=cost,
                   from_level=level, planned_to_level=to_level,
                   to_level=to_level, completed=True,
                   movement_mode=movement_mode.value,
                   squares=[list(square) for square in route])
        move_event = self.log[-1]
        suppressed = self._disengaged[actor.name]
        for previous, step in zip(route, route[1:], strict=False):
            threatening: list[Creature] = []
            if not suppressed:
                threatening = [
                    enemy for enemy in self.enemies_of(actor.name)
                    if distance_feet(
                        as_point(enemy.position), square_center(previous),
                        self.movement_rule,
                    ) <= self._opportunity_attack_reach(enemy)
                ]
            actor.position = square_center(step)
            for enemy in threatening:
                if distance_feet(
                    as_point(enemy.position), square_center(step), self.movement_rule
                ) <= self._opportunity_attack_reach(enemy):
                    continue
                self._opportunity_attack(enemy, actor, rng)
            if not actor.conscious:
                # Dropped mid-stride: the walk ends where the mover fell, and the
                # state — not the move event's declared destination — is the truth.
                move_event.data["destination"] = as_point(actor.position)
                move_event.data["to_level"] = actor.level
                move_event.data["completed"] = False
                self._face_along(actor, square_center(previous), square_center(step))
                return
        # The walk completed, so the last leg of the route is the leg that ended
        # it. A route of one square — a connector ride from where you already
        # stand — has no leg, and _face_along leaves the facing alone.
        if len(route) >= 2:
            self._face_along(actor, square_center(route[-2]), square_center(route[-1]))
        # The connector is ridden last, and only by a mover still standing: one
        # dropped on the stairs falls at its foot, on the level it was walking.
        if to_level != level:
            actor.level = to_level

    @staticmethod
    def _movement_speed(actor: Creature, mode: MovementMode) -> int:
        speed = {
            MovementMode.WALK: actor.speed,
            MovementMode.CLIMB: actor.climb_speed,
            MovementMode.SWIM: actor.swim_speed,
            MovementMode.FLY: actor.fly_speed,
        }[mode]
        if speed <= 0:
            raise EncounterError(f"{actor.name} has no {mode.value} speed")
        return speed

    def _do_flying_level_change(
        self,
        actor: Creature,
        destination: Point,
        dest_sq: Square,
        to_level: int,
        mode: MovementMode,
    ) -> None:
        """Fly directly between planes; a connector is unnecessary."""
        assert self.map_document is not None
        if to_level not in self.map_document.levels:
            declared = ", ".join(str(i) for i in sorted(self.map_document.levels))
            raise EncounterError(f"there is no level {to_level} on this map. Levels: {declared}")
        if not self._on_map(dest_sq) or self._entry_cost(to_level, dest_sq) is None:
            raise EncounterError(f"cannot end a flight at {dest_sq} on level {to_level}")
        holder = self._occupied(to_level).get(dest_sq)
        if holder is not None and holder != actor.name:
            raise EncounterError(f"square {dest_sq} on level {to_level} is occupied by {holder}")
        origin = as_point(actor.position)
        vertical = abs(
            self._elevation_at(to_level, dest_sq)
            - self._elevation_at(actor.level, to_square(origin))
        )
        cost = max(distance_feet(origin, destination, self.movement_rule), vertical)
        if cost > self._turn.movement_left:
            raise EncounterError(
                f"{actor.name} has {self._turn.movement_left} ft of movement, needs {cost} ft"
            )
        self._turn.movement_left -= cost
        prior_level = actor.level
        actor.position = destination
        actor.level = to_level
        self._face_along(actor, origin, destination)
        self._emit(
            "move",
            actor.name,
            detail=f"{origin} on level {prior_level} -> {destination} on level {to_level} "
            f"({cost} ft used)",
            origin=origin,
            destination=destination,
            cost=cost,
            from_level=prior_level,
            to_level=to_level,
            movement_mode=mode.value,
            squares=[list(to_square(origin)), list(dest_sq)],
        )

    def _do_interact(self, actor: Creature, action: Action, rng: Random) -> None:
        """Operate a named map fixture — a door, in the common case.

        Every gate before the spend is free, so a party learns *why* a thing
        will not move without paying for the lesson. In order: the fixture must
        exist, the actor must have the budget it costs, must be able to reach it
        on its own storey, must have met whatever it waits for, and must not be
        asking for the state it is already in.

        Only then is the action or the interaction spent, and only then is any
        check rolled — a failed check spends the budget and moves nothing.
        """
        if self.map_document is None or self.map_state is None:
            raise EncounterError(
                "there is no battle map, so there is nothing to interact with"
            )
        if action.feature is None:
            raise EncounterError("interacting needs 'feature'")
        # ``self._fixtures``, not the document's features. A spawn hint is not a
        # thing a fight can work, so it is neither found here nor listed in the
        # refusal — and that second half matters more than the first: this
        # message is player-visible, and a map's spawn hints spell out which
        # side is coming in where. The brief works hard to keep an ambusher off
        # the wire; a refusal that names one would hand it over.
        feature = self._fixtures.get(action.feature)
        if feature is None:
            available = ", ".join(sorted(self._fixtures)) or "none"
            raise EncounterError(
                f"no feature named {action.feature!r}; the map has: {available}"
            )

        if feature.costs_action:
            self._require_action(actor)
        elif self._turn.interaction_used:
            raise EncounterError(
                f"{actor.name} has already interacted with a feature this turn"
            )

        # Reach is a question about a storey, not only about a square. The
        # feature table merges every plane under one set of names, so comparing
        # squares alone let a creature work a hatch directly above its head.
        level = self._fixture_level[feature.id]
        if level != actor.level:
            raise EncounterError(
                f"{feature.id} is on level {level}; {actor.name} is on level "
                f"{actor.level} and cannot reach it from another storey"
            )
        actor_sq = to_square(as_point(actor.position))
        apart = max(
            abs(actor_sq[0] - feature.at[0]), abs(actor_sq[1] - feature.at[1])
        )
        if apart > 1:
            raise EncounterError(
                f"{feature.id} is at {feature.at}, out of reach from "
                f"{actor_sq}; stand on or next to it"
            )

        was_open = feature.id in self.map_state.open_features
        wants_open = (not was_open) if action.set_open is None else action.set_open
        linked = [feature.linked_to] if feature.linked_to is not None else []
        operated = [feature.id, *linked]
        for operated_name in operated:
            operated_feature = self._fixtures[operated_name]
            trigger = operated_feature.trigger
            if (
                trigger is not None
                and trigger.mode is TriggerMode.MAINTAINED
                and trigger.active(self.map_state.open_features)
                and wants_open is not trigger.set_open
            ):
                raise EncounterError(
                    f"{feature.id} is held "
                    f"{'open' if trigger.set_open else 'closed'} by its maintained trigger"
                )
        # Prerequisites gate *opening* only. Held as an invariant they would also
        # bar closing the gate once a spike went back in, which is not the
        # fiction: a thing that opened can always be shut again.
        if wants_open and feature.requires:
            unmet = [
                wanted for wanted in feature.requires
                if wanted not in self.map_state.open_features
            ]
            if unmet:
                raise EncounterError(
                    f"{feature.id} will not move until {', '.join(unmet)} "
                    f"{'is' if len(unmet) == 1 else 'are'} open"
                )
        if action.set_open is not None and action.set_open == was_open:
            raise EncounterError(
                f"{feature.id} is already {'open' if was_open else 'closed'}"
            )

        check_advantage = compute_ability_check_advantage(
            conditions=actor.conditions,
            condition_effects=self.condition_effects,
        )
        # Before the action or the free interaction is spent, for the reason
        # ``_do_attack`` checks before decrementing: a failed check already
        # costs the turn, and a *refused* one must not.
        if feature.check is not None:
            check_faces(action.natural or None, check_advantage)
        elif action.natural:
            raise EncounterError(
                f"{feature.id} asks for no check, so there is no face to report"
            )

        if feature.costs_action:
            self._turn.action_used = True
        else:
            self._turn.interaction_used = True

        verb = "open" if wants_open else "close"
        extras: dict[str, Any] = {"linked": linked} if linked else {}
        if feature.check is not None:
            # A raw ability check: creatures carry no skill proficiencies, so a
            # DC here was set as if untrained.
            test = make_d20_test(
                rng,
                modifier=actor.ability_mod(feature.check.ability),
                dc=feature.check.dc,
                advantage=check_advantage,
                supplied=action.natural or None,
            )
            extras.update({"success": test.success, "check": test.describe()})
            if not test.success:
                # ``open`` is always the state *after* the attempt, so a replay
                # reading it needs to know nothing about checks.
                self._emit("interact", actor.name,
                           detail=(
                               f"fails to {verb} {feature.id} ({test.describe()})"
                           ),
                           feature=feature.id, open=was_open, **extras)
                return
            note = f" ({test.describe()})"
        else:
            note = ""

        if wants_open:
            self.map_state.open_features.update(operated)
        else:
            self.map_state.open_features.difference_update(operated)
        subjects = feature.id if not linked else f"{feature.id} and {linked[0]}"
        self._emit("interact", actor.name,
                   detail=f"{'opens' if wants_open else 'closes'} {subjects}{note}",
                   feature=feature.id, open=wants_open, **extras)
        self._drain_triggers(feature.id, operated)

    def _drain_triggers(self, initiating_feature: str, changed: Sequence[str]) -> None:
        """Apply automatic fixture transitions in deterministic dependency order."""
        assert self.map_document is not None and self.map_state is not None
        revision = 1
        changed_at = {name: revision for name in changed}
        changed_by = {name: initiating_feature for name in changed}

        for name in self._trigger_sequence:
            feature = self._fixtures[name]
            trigger = feature.trigger
            assert trigger is not None
            active = trigger.active(self.map_state.open_features)
            was_active = self._trigger_active[name]
            self._trigger_active[name] = active
            is_open = name in self.map_state.open_features
            should_apply = active and is_open is not trigger.set_open and (
                trigger.mode is TriggerMode.MAINTAINED or not was_active
            )
            if not should_apply:
                continue

            changed_dependencies = [
                dependency
                for dependency, _ in trigger.when
                if dependency in changed_at
            ]
            if changed_dependencies:
                dependency = max(
                    changed_dependencies,
                    key=lambda item: (changed_at[item], item),
                )
                triggered_by = changed_by[dependency]
            else:  # pragma: no cover - an active transition needs a changed predicate
                triggered_by = initiating_feature

            linked = [feature.linked_to] if feature.linked_to is not None else []
            operated = [name, *linked]
            if trigger.set_open:
                self.map_state.open_features.update(operated)
            else:
                self.map_state.open_features.difference_update(operated)
            subjects = name if not linked else f"{name} and {linked[0]}"
            extras: dict[str, Any] = {"linked": linked} if linked else {}
            self._emit(
                "interact",
                detail=(
                    f"trigger {'opens' if trigger.set_open else 'closes'} {subjects}"
                ),
                automatic=True,
                triggered_by=triggered_by,
                feature=name,
                open=trigger.set_open,
                **extras,
            )
            revision += 1
            for operated_name in operated:
                changed_at[operated_name] = revision
                changed_by[operated_name] = name

    def stand_cost(self, actor_name: str) -> int:
        """Feet of movement standing from Prone costs the named creature.

        SRD 5.2.1, Rules Glossary, "Prone": the condition ends when the creature
        stands, "which costs an amount of movement equal to half your Speed."
        Movement is tracked in whole feet, so an odd Speed rounds the cost down.
        Public because the auto-play policy prices the act before taking it.
        """
        return self.creatures[actor_name].speed // 2

    def can_stand(self, actor_name: str) -> bool:
        """Whether the named creature could legally take the stand act right now.

        Public for the auto-play policy, which must decide with the same eyes
        the stepper refuses with rather than re-derive the rule. Reads the
        current turn's movement budget, so the answer is only meaningful for
        the creature whose turn it is — the only creature that can act at all.
        """
        creature = self.creatures[actor_name]
        return (
            Condition.PRONE in creature.conditions
            and creature.conscious
            and creature.speed > 0
            and not speed_is_zero(creature.conditions, self.condition_effects)
            and self._turn.movement_left >= self.stand_cost(actor_name)
        )

    def _do_stand(self, actor: Creature) -> None:
        """Stand from Prone: no action, half the actor's Speed in movement.

        The refusals mirror the SRD's own gates — you cannot stand if your
        Speed is 0 or if the movement left is less than the cost. Standing
        ends Prone outright, whatever imposed it, so any ledger entry holding
        Prone on this creature is dropped with it: a rider's later timed
        expiry must not report lifting a condition the creature already shed,
        and a fresh knockdown must not read a stale holder. The purge emits
        nothing — the stand event itself is the record of the condition ending.
        """
        if Condition.PRONE not in actor.conditions:
            raise EncounterError(f"{actor.name} is not prone")
        if actor.speed == 0:
            raise EncounterError(f"{actor.name} has a speed of 0 and cannot stand")
        if speed_is_zero(actor.conditions, self.condition_effects):
            held = ", ".join(sorted(actor.conditions))
            raise EncounterError(f"{actor.name} has speed 0 ({held}) and cannot stand")
        cost = self.stand_cost(actor.name)
        if cost > self._turn.movement_left:
            raise EncounterError(
                f"{actor.name} has {self._turn.movement_left} ft of movement, "
                f"needs {cost} ft to stand"
            )
        self._turn.movement_left -= cost
        actor.remove_condition(Condition.PRONE)
        self._effects = [
            effect for effect in self._effects
            if not (effect.target == actor.name and effect.condition == Condition.PRONE)
        ]
        self._emit("stand", actor.name,
                   detail=f"stands up ({cost} ft used, "
                          f"{self._turn.movement_left} ft left)",
                   cost=cost, movement_left=self._turn.movement_left)

    def _opportunity_attack_reach(self, attacker: Creature) -> int:
        """The reach an Opportunity Attack from ``attacker`` would threaten with.

        :meth:`_opportunity_attack` always swings the *first* melee option in
        ``attacker.attacks``, so the radius that triggers the attack must be
        that option's reach — a Reach weapon "adds 5 feet ... as well as when
        determining your reach for Opportunity Attacks with it" (SRD 5.2.1).
        Deriving both from the same lookup is what keeps them from disagreeing;
        a creature with no melee option at all threatens nothing, but the
        ordinary 5-ft default is returned rather than 0 so a caller that only
        wants to know "how close is close" gets a sane answer either way.

        "Melee option" is :meth:`AttackOption.melee_capable`, not
        ``kind is MELEE``, so a thrown weapon counts: the SRD grants the
        Opportunity Attack for "one melee attack with a weapon", and inside its
        reach a javelin makes exactly that. A creature carrying nothing but
        javelins threatens the square beside it rather than nothing at all.
        """
        melee = next(
            (option for option in attacker.attacks if option.melee_capable()),
            None,
        )
        return melee.reach if melee is not None else MELEE_THRESHOLD

    def _opportunity_attack(self, attacker: Creature, mover: Creature, rng: Random) -> None:
        if not self._reaction_available.get(attacker.name, False) or not attacker.active:
            return
        # The same predicate :meth:`_opportunity_attack_reach` picks with, and
        # it has to stay the same one: a radius derived from a different option
        # than the swing is the disagreement T4 removed.
        melee = next(
            (option for option in attacker.attacks if option.melee_capable()),
            None,
        )
        if melee is None:
            return
        # "You can make an Opportunity Attack when a creature that you can
        # see leaves your reach" (SRD 5.2.1) — checked after the reaction and
        # attacker.active guards above but before the reaction is spent, so an
        # attacker that cannot see the mover keeps its Reaction for something
        # else rather than burning it on a swing it was never entitled to.
        if not self._can_see(attacker, mover):
            return
        self._reaction_available[attacker.name] = False
        # The third call site of the sight pair, and the reason it is a method
        # rather than three copies of one expression: an Invisible *attacker*
        # takes its Advantage from here, and the SRD withdraws it "against that
        # creature" — a mover with Blindsight on the attacker gets neither.
        #
        # The disadvantage half is structurally zero here and is passed anyway,
        # because the guard that makes it so is above rather than in this
        # expression: an attacker who cannot see the mover does not swing at
        # all, so the only reachable swing against an Invisible mover is one
        # the SRD has already withdrawn the Disadvantage from. Recomputing it
        # rather than writing 0 keeps that a consequence of the gate instead of
        # a constant somebody has to re-derive when the gate changes.
        unseen_advantage, unseen_disadvantage = self._sight_advantage(attacker, mover)
        advantage = compute_attack_advantage(
            attacker_conditions=attacker.conditions,
            target_conditions=mover.conditions,
            distance=melee.reach,
            # An opportunity attack is an attack roll, so Pack Tactics reads
            # here too — against the mover wherever the walk has taken it.
            extra_advantage=(
                int(self._pack_tactics_applies(attacker, mover)) + unseen_advantage
            ),
            extra_disadvantage=(
                int(self._dodge_benefits(mover)) + unseen_disadvantage
            ),
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
            **self._rider_damage_arguments(attacker, melee, mover),
        )
        self._emit("opportunity_attack", attacker.name, mover.name,
                   f"{melee.name}: {resolution.describe()}",
                   attack=melee.name,
                   hit=resolution.hit,
                   critical=resolution.critical,
                   natural=resolution.attack.roll.natural,
                   total=resolution.attack.total,
                   advantage=resolution.advantage.value,
                   damage=resolution.total_damage_dealt,
                   position=as_point(mover.position),
                   level=mover.level)
        if resolution.hit:
            # A mover cannot currently be at 0 hit points — a dying creature has
            # Speed 0 — so the flag changes nothing today. It is passed anyway: two
            # call sites resolving the same kind of attack and only one carrying the
            # critical is the asymmetry that becomes a bug when reactions grow.
            self._apply_damage(
                mover, resolution.total_damage_dealt, rng,
                critical=resolution.critical,
                damage_types=self._attack_damage_types(melee),
            )
            if melee.on_hit_condition is not None:
                # The same bite carries the same rider as a reaction. One edge is
                # deliberate and literal: a rider timed to the end of the target's
                # next turn, landed by an opportunity attack *during* the target's
                # own turn, expires at that same turn's end — the pointer reaching
                # the anchor's boundary is the trigger, with no memory of whose
                # turn the hit interrupted.
                self._apply_attack_rider(attacker, mover, melee, rng)

    # --- ongoing effects --------------------------------------------------
    def _apply_condition(
        self,
        source: Creature,
        target: Creature,
        condition: str,
        *,
        effect_name: str,
        concentration: bool,
        expires_phase: str = "",
        expires_anchor: str = "",
    ) -> bool:
        """Impose ``condition`` and record what is imposing it.

        Registration happens *after* ``add_condition`` so a name the active table
        does not define is refused before anything is written down, and the
        ``stacked`` reading is taken *before*, because it is a statement about the
        creature as it was.

        Returns whether the condition took hold. A target immune to it gets no
        :class:`OngoingEffect` — there would be nothing for it to later release —
        and a refusal event instead, so a GM watching ``effect_name`` land on an
        immune target sees why nothing happened rather than reading it as
        dropped silently. The spell and item paths call this directly; the
        attack-rider path (:meth:`_apply_attack_rider`) checks immunity itself
        first, to skip a saving throw that could never matter, and so never
        reaches this branch — but the refusal here still fires for it if that
        pre-check is ever missed, because the true gate is ``add_condition``.
        """
        held_by_ledger = self._holders(target.name, condition)
        already_held = condition in target.conditions
        if not target.add_condition(condition):
            self._emit(
                "effect_apply", source.name, target.name,
                f"{effect_name}: {target.name} is immune to {condition}",
                condition=condition, applied=False,
            )
            return False
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
                expires_phase=expires_phase,
                expires_anchor=expires_anchor,
            )
        )
        return True

    def _apply_attack_rider(
        self, actor: Creature, target: Creature, option: AttackOption, rng: Random
    ) -> None:
        """Resolve an attack's on-hit condition rider against a landed hit.

        A target the damage just dropped receives nothing — the same stance
        spells and items take: a condition is imposed only on a conscious
        creature, and skipping the save with it keeps a state-determined RNG
        stream. The save, when the stat block prints one, is rolled with the
        target's own advantage and auto-fail circumstances, exactly as an item's
        save is. The timed forms register their anchor here; :meth:`advance`
        fires them when the pointer reaches that turn boundary.

        A size gate is checked *before* the save, and the order is load-bearing
        twice over: a save the gate has already made moot must not be rolled,
        because rolling it would consume the stream and move every later roll in
        the fight; and the refusal is emitted rather than silent, because "why am
        I still standing" is exactly the question the log exists to answer.

        Both call sites — the attack action and the opportunity attack — reach
        the rider through here, so the gate covers the reaction without a second
        copy. ``tests/test_riders.py::TestSizeGatedRiders`` pins that.
        """
        condition = option.on_hit_condition
        assert condition is not None
        if not target.conscious:
            return
        if option.on_hit_max_size is not None and not fits_within(
            target.size, option.on_hit_max_size
        ):
            self._emit(
                "effect_apply", actor.name, target.name,
                f"{option.name}: {target.name} is {target.size} and the rider "
                f"reaches {option.on_hit_max_size} or smaller",
                attack=option.name, condition=condition, applied=False, saved=None,
            )
            return
        # Checked before the save for the same reason the size gate is: a save
        # an immune target could never fail must not be rolled, or the draw
        # would move every later roll in the fight. ``add_condition`` is the
        # actual gate and would refuse this regardless — this only spares the
        # roll. See the ruling on ``Creature.condition_immunities``.
        if condition in target.condition_immunities:
            self._emit(
                "effect_apply", actor.name, target.name,
                f"{option.name}: {target.name} is immune to {condition}",
                attack=option.name, condition=condition, applied=False, saved=None,
            )
            return
        save: D20Test | None = None
        if option.on_hit_save_ability is not None:
            save = make_d20_test(
                rng,
                modifier=target.save_modifier(option.on_hit_save_ability),
                dc=option.on_hit_save_dc,
                advantage=self.save_advantage(target, option.on_hit_save_ability),
                auto_fail=self.auto_fails_save(target, option.on_hit_save_ability),
            )
            if save.success:
                self._emit(
                    "effect_apply", actor.name, target.name,
                    f"{option.name}: {target.name} saves against {condition} "
                    f"({save.describe()})",
                    attack=option.name, condition=condition, applied=False,
                    saved=True,
                )
                return
        phase, anchor = "", ""
        if option.on_hit_expiry == RiderExpiry.START_OF_ATTACKER_NEXT_TURN:
            phase, anchor = "start", actor.name
        elif option.on_hit_expiry == RiderExpiry.END_OF_TARGET_NEXT_TURN:
            phase, anchor = "end", target.name
        self._apply_condition(
            actor, target, condition,
            effect_name=option.name, concentration=False,
            expires_phase=phase, expires_anchor=anchor,
        )
        until = ""
        if phase == "start":
            until = f" until the start of {anchor}'s next turn"
        elif phase == "end":
            until = f" until the end of {anchor}'s next turn"
        detail = f"{option.name}: {target.name} has {condition}{until}"
        if save is not None:
            detail += f" ({save.describe()})"
        self._emit(
            "effect_apply", actor.name, target.name, detail,
            attack=option.name, condition=condition, applied=True,
            saved=False if save is not None else None,
            expiry=str(option.on_hit_expiry),
        )

    def _expire_timed(self, phase: str, anchor: str) -> None:
        """Release every timed effect whose turn boundary has just passed.

        The trigger is the turn *slot*, not the creature acting: ``advance``
        calls this for a dead creature's skipped slot exactly as for a living
        one's turn, so a poison anchored to a dead centipede still ends on
        schedule. Release goes through :meth:`_release_effect`, so a condition
        something else still imposes — another live rider, or an application
        outside this ledger — persists.
        """
        for effect in list(self._effects):
            if effect.expires_phase == phase and effect.expires_anchor == anchor:
                self._release_effect(effect)

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
        concentration can lapse, is deliberate: SRD 5.2.1 ends Concentration on a
        failed Constitution save, on the Incapacitated condition, on death, and on
        starting another Concentration effect — and two of those are enforced inside
        :class:`~fivee_sim.model.creature.Creature`, which cannot reach this ledger.
        A design with one release call per exit point is a design where the next
        exit point added leaks silently.

        ``dead`` and ``active`` are consulted alongside ``concentrating_on`` rather
        than trusting it alone, because they are what SRD 5.2.1 actually says
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
        self,
        target: Creature,
        amount: int,
        rng: Random,
        *,
        critical: bool = False,
        damage_types: tuple[DamageType, ...] = (),
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
        fortitude = self._undead_fortitude_save(
            target, amount, rng, critical=critical, damage_types=damage_types
        )
        if fortitude is not None and fortitude.success:
            # SRD 5.2.1 (Zombie): "On a successful save, the zombie drops to 1 Hit
            # Point instead." It never reaches 0, so none of the drop's machinery
            # runs: no Unconscious, no Prone, no death saves — and the
            # concentration check below still fires, because damage was taken.
            target.hp = 1
        else:
            # ``critical`` only matters for a target already at 0 hit points, where
            # a critical hit costs two death save failures instead of one.
            target.take_damage(amount, critical=critical)
        self._emit("damage", target=target.name,
                   detail=f"{amount} damage, {target.hp}/{target.max_hp} hit points left",
                   amount=amount, hp=target.hp, max_hp=target.max_hp)
        if fortitude is not None:
            held = fortitude.success
            self._emit("undead_fortitude", target.name,
                       detail=(
                           f"{'holds at 1 hit point' if held else 'drops'} "
                           f"({fortitude.describe()})"
                       ),
                       success=held, dc=fortitude.dc)
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
                else "drops to 0 hit points"
                if target.death_rule is DeathRule.INSTANT
                else "damage exceeded maximum hit points"
            ))
        elif was_conscious and not target.conscious:
            self._emit("down", target.name, detail="falls unconscious and is dying")
        # Three of the four ways concentration ends pass through here — the failed
        # save above, being knocked out, and dying — so the release is reported next
        # to the loss rather than at the end of the action that caused it.
        self._reconcile_concentration()

    def _undead_fortitude_save(
        self,
        target: Creature,
        amount: int,
        rng: Random,
        *,
        critical: bool,
        damage_types: tuple[DamageType, ...],
    ) -> D20Test | None:
        """Roll Undead Fortitude against a drop to 0, or ``None`` when it cannot apply.

        SRD 5.2.1 (Zombie): "If damage reduces the zombie to 0 Hit Points, it
        makes a Constitution saving throw (DC 5 plus the damage taken) unless
        the damage is Radiant or from a Critical Hit."

        ``damage_types`` names every type in the dropping instance — the main
        pool and, on an attack, the rider's bonus pool. Radiant in **any**
        component disqualifies: the rule reads "the damage", singular, and a hit
        is not less radiant for splitting its dice. The DC uses the amount
        actually dealt, after the target's defenses, because that is the damage
        taken. Two more gates are drops the rule never sees: a creature already
        at 0 is not *reduced* to it, and overflow at or past the maximum is
        instant death rather than a drop — the massive-damage rule wins.
        Eligibility is a pure function of state, so the roll consumes
        randomness exactly when a replay would consume it. The save itself goes
        through the same machinery as every other save the fight rolls, with
        the target's own advantage and auto-fail circumstances.
        """
        if not target.undead_fortitude or not target.conscious:
            return None
        if amount < target.hp or amount - target.hp >= target.max_hp:
            return None
        if critical or DamageType.RADIANT in damage_types:
            return None
        return make_d20_test(
            rng,
            modifier=target.save_modifier(Ability.CONSTITUTION),
            dc=UNDEAD_FORTITUDE_BASE_DC + amount,
            advantage=self.save_advantage(target, Ability.CONSTITUTION),
            auto_fail=self.auto_fails_save(target, Ability.CONSTITUTION),
        )

    def _emit(
        self, kind: str, actor: str = "", target: str = "", detail: str = "", **data: Any
    ) -> None:
        # Stamping is safe at every call site: __init__ emits only after
        # ``order`` and ``turn_index`` exist, the round event fires after
        # ``round`` increments, and turn_start fires after ``turn_index`` has
        # moved.
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
    movement_rule: DiagonalRule = DiagonalRule.FIVE_FIVE_FIVE,
    map_document: MapDocument | None = None,
    terrain_effects: TerrainTable | None = None,
) -> tuple[Encounter, Random]:
    """Create an encounter and the generator that drives it, from a seed alone."""
    rng = Random(seed)
    encounter = Encounter(
        list(combatants),
        rng,
        spellbook=spellbook,
        items=items,
        condition_effects=condition_effects,
        movement_rule=movement_rule,
        map_document=map_document,
        terrain_effects=terrain_effects,
    )
    return encounter, rng
