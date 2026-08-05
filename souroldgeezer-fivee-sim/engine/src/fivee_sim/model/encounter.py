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
from collections.abc import Iterable, Mapping, Sequence
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
from .battlemap import (
    GROUND_LEVEL,
    BattleMap,
    LightLevel,
    MapFeature,
    MapPlane,
    MapState,
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
    # Where it is and whether it is really there.
    "name", "team", "position", "level", "facing", "present", "arrival_round",
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


#: What replaces an enemy's hit points: the thing a game master says out loud.
#: Bands rather than a fraction, because a fraction is a hit-point total that
#: needs one division to recover. Our own descriptive vocabulary, not a rules
#: term — nothing in the engine keys off these strings.
_HEALTH_BANDS: tuple[tuple[float, str], ...] = (
    (1.0, "unharmed"),
    (0.5, "hurt"),
    (0.25, "badly hurt"),
    (0.0, "barely standing"),
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
    movement_left: int = 0
    action_used: bool = False
    attacks_left: int = 0
    interaction_used: bool = False
    bonus_action_used: bool = False


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


def _dependency_order(features: Mapping[str, MapFeature]) -> tuple[str, ...]:
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
        battle_map: BattleMap | None = None,
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
        self.battle_map = battle_map
        self.map_state: MapState | None = None
        self._trigger_sequence: tuple[str, ...] = ()
        self._trigger_active: dict[str, bool] = {}
        # Every square any fixture decides, flattened once at adoption: the two
        # resolvers below sit inside pathfinding loops, so what they need is an
        # index, not a list of features to scan.
        self._feature_squares: dict[tuple[int, Square], SquareClaim] = {}
        if battle_map is not None:
            self._adopt_map(battle_map, combatants)
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
                compute_ability_check_advantage(
                    conditions=creature.conditions,
                    condition_effects=self.condition_effects,
                ),
            )
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
    def _adopt_map(self, battle_map: BattleMap, combatants: Sequence[Creature]) -> None:
        """Validate the map against the terrain table and place the combatants.

        Everything a map can get wrong is refused here, before the first roll:
        a terrain kind the captured table does not define, a feature off the map
        or doubled up on a square, a prerequisite naming nothing, a combatant off
        the map, inside a wall, or on another combatant. Positions are snapped to
        the centre of their square — on a grid, the square is the position.

        A ``BattleMap`` can be hand-built with no document behind it, so these
        refusals are not a second opinion on the parser's — they are the only
        ones such a map ever meets. **Every square a fixture governs is claimed
        by exactly one fixture per level**, which is what makes the resolvers
        below total: there is no precedence question to answer, so a stateless
        reader of the same map cannot disagree with this fight about what a
        square is.
        """
        if battle_map.width < 1 or battle_map.height < 1:
            raise EncounterError(
                f"a battle map needs at least one square; "
                f"got {battle_map.width}x{battle_map.height}"
            )
        named: set[str] = set()
        for plane in battle_map.levels.values():
            named.update({plane.default_terrain, *plane.terrain.values()})
            for feature in plane.features.values():
                named.add(feature.closed_terrain)
                named.add(feature.open_terrain)
                for overlay in feature.affects:
                    if overlay.terrain is not None:
                        named.add(overlay.terrain.closed)
                        named.add(overlay.terrain.open)
        unknown = sorted(kind for kind in named if kind not in self.terrain_effects)
        if unknown:
            defined = ", ".join(sorted(self.terrain_effects)) or "none"
            raise EncounterError(
                f"the map names terrain the loaded content does not define: "
                f"{', '.join(unknown)}. Defined: {defined}"
            )
        for level in sorted(battle_map.levels):
            plane = battle_map.levels[level]
            for name, feature in plane.features.items():
                for square, claim in feature.claims():
                    if not self._on_map(square):
                        # The own square and an overlay cell are both off the
                        # same map, but they are different authoring mistakes.
                        reach = "sits at" if square == feature.square else "reaches"
                        raise EncounterError(
                            f"feature {name!r} {reach} {square}, off the "
                            f"{battle_map.width}x{battle_map.height} map"
                        )
                    other = self._feature_squares.get((level, square))
                    if other is not None and other.feature == name:
                        raise EncounterError(
                            f"feature {name!r} claims square {square} twice"
                        )
                    if other is not None:
                        raise EncounterError(
                            f"features {other.feature!r} and {name!r} share square {square}"
                        )
                    self._feature_squares[(level, square)] = claim
            for square, target in plane.connectors.items():
                if target not in battle_map.levels:
                    raise EncounterError(
                        f"the connector at {square} on level {level} leads to level "
                        f"{target}, which this map does not have"
                    )
        # A second pass, because a prerequisite may point forward — and across a
        # floor. ``requires`` is a prerequisite, not a reach: which storey the
        # thing it names sits on is nobody's business but the fiction's.
        catalogue = battle_map.features
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
                    f"feature {name!r} links to {partner.name!r}; that door must link "
                    f"back to {name!r}"
                )
            if battle_map.level_of(name) != battle_map.level_of(partner.name):
                raise EncounterError("linked doors must stand on the same level")
            dx = abs(feature.square[0] - partner.square[0])
            dy = abs(feature.square[1] - partner.square[1])
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
            if feature.initially_open != partner.initially_open:
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
            name for name, feature in battle_map.features.items()
            if feature.initially_open
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
            if (
                trigger.mode is TriggerMode.MAINTAINED
                and trigger.active(initially_open)
                and feature.initially_open is not trigger.set_open
            ):
                raise EncounterError(
                    f"feature {name!r} maintained trigger is true initially and sets "
                    f"it {'open' if trigger.set_open else 'closed'}, but it starts "
                    f"{'open' if feature.initially_open else 'closed'}"
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
            if creature.level not in battle_map.levels:
                declared = ", ".join(str(i) for i in sorted(battle_map.levels))
                raise EncounterError(
                    f"{creature.name} starts on level {creature.level}, which this map "
                    f"does not have. Levels: {declared}"
                )
            square = to_square(as_point(creature.position))
            if not self._on_map(square):
                raise EncounterError(
                    f"{creature.name} starts at {as_point(creature.position)}, off the "
                    f"{battle_map.width}x{battle_map.height} map"
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
        assert self.battle_map is not None
        return 0 <= square[0] < self.battle_map.width and (
            0 <= square[1] < self.battle_map.height
        )

    def _plane(self, level: int) -> MapPlane:
        """The storey a query is about. Every map query names one; none assumes."""
        assert self.battle_map is not None
        return self.battle_map.levels[level]

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
        """
        assert self.battle_map is not None and self.map_state is not None
        plane = self._plane(level)
        claim = self._feature_squares.get((level, square))
        if claim is not None and claim.terrain is not None:
            if self._is_open(claim.feature):
                return claim.terrain.open
            return claim.terrain.closed
        return plane.terrain.get(square, plane.default_terrain)

    def _terrain_at(self, square: Square) -> str:
        """The ground plane's terrain. The mapless and single-storey shorthand."""
        return self._terrain_at_level(GROUND_LEVEL, square)

    def _terrain_effect(self, level: int, square: Square) -> TerrainEffect:
        return terrain_effect_of(self._terrain_at_level(level, square), self.terrain_effects)

    def _is_underwater(self, creature: Creature) -> bool:
        if self.battle_map is None:
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
        """
        plane = self._plane(level)
        claim = self._feature_squares.get((level, square))
        if claim is not None and claim.elevation is not None:
            if self._is_open(claim.feature):
                return claim.elevation.open
            return claim.elevation.closed
        return plane.elevation.get(square, plane.default_elevation)

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
        if self.battle_map is None:
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
            bounds=(self.battle_map.width, self.battle_map.height),
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
        if self.battle_map is None or to_level not in self.battle_map.levels:
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
        if self.battle_map is None:
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
        if self.battle_map is None:
            return CoverGrade.NONE
        target = self.creatures[target_name]
        if target.level != level:
            visible = self._plane(level).sight_links.get(origin, frozenset())
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
        if self.battle_map is not None:
            origin = to_square(as_point(actor.position))
            visible_levels.update(self._plane(actor.level).sight_links.get(origin, ()))
        return [
            c for c in self.creatures.values()
            if c.team != actor.team and c.combat_active and c.level in visible_levels
        ]

    def health_band(self, creature: Creature) -> str:
        """How a creature looks, for somebody who cannot read its sheet."""
        if creature.dead:
            return "dead"
        if not creature.conscious:
            return "down"
        if creature.max_hp <= 0:
            return "unharmed"
        share = creature.hp / creature.max_hp
        for threshold, described in _HEALTH_BANDS:
            if share >= threshold:
                return described
        return "barely standing"

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
        """
        if as_name not in self.creatures:
            known = ", ".join(sorted(self.creatures))
            raise EncounterError(
                f"no combatant named {as_name!r} in this encounter; there is: {known}"
            )
        asker = self.creatures[as_name]
        payload: dict[str, Any] = {
            "as": as_name,
            "round": self.round,
            "turn": self.current_name,
            "your_turn": self.current_name == as_name,
            "over": self.over,
            "winner": self.winner,
            "you": self._creature_state(asker),
            "allies": [],
            "enemies": [],
        }
        # The asker's own budget, and only on their own turn: `_turn` belongs to
        # whoever is acting, so reporting it otherwise would describe somebody
        # else's remaining movement as though it were yours.
        if payload["your_turn"]:
            payload["turn_state"] = {
                "movement_left": self._turn.movement_left,
                "action_used": self._turn.action_used,
                "attacks_left": self._turn.attacks_left,
                "interaction_used": self._turn.interaction_used,
                "bonus_action_used": self._turn.bonus_action_used,
            }
        if self.battle_map is not None:
            payload["map"] = self._map_state()

        for name in self.order:
            if name == as_name:
                continue
            other = self.creatures[name]
            if not other.arrived:
                continue
            distance = distance_feet(
                as_point(asker.position), as_point(other.position), self.movement_rule
            )
            if other.team == asker.team:
                payload["allies"].append(
                    self._creature_state(other) | {"distance": distance}
                )
                continue
            if self.cover_between(as_name, name) is CoverGrade.TOTAL:
                continue
            seen = self._creature_state(other)
            payload["enemies"].append(
                {key: value for key, value in seen.items() if key in ENEMY_VISIBLE_KEYS}
                | {"distance": distance, "health": self.health_band(other)}
            )
        return payload

    def state(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "turn": self.current_name,
            "movement_rule": self.movement_rule.value,
            "over": self.over,
            "winner": self.winner,
            "order": list(self.order),
            "turn_state": {
                "movement_left": self._turn.movement_left,
                "action_used": self._turn.action_used,
                "attacks_left": self._turn.attacks_left,
                "interaction_used": self._turn.interaction_used,
                "bonus_action_used": self._turn.bonus_action_used,
            },
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
        if self.battle_map is None or self.map_state is None:
            return None
        return {
            "name": self.battle_map.name,
            "width": self.battle_map.width,
            "height": self.battle_map.height,
            "movement_rule": self.movement_rule.value,
            "elevation": self._elevation_summary(GROUND_LEVEL),
            "levels": [
                self._level_summary(index) for index in sorted(self.battle_map.levels)
            ],
            "features": {
                name: self._feature_summary(name, feature)
                for name, feature in sorted(self.battle_map.features.items())
            },
        }

    def _feature_summary(self, name: str, feature: MapFeature) -> dict[str, Any]:
        """One fixture, reporting only what it actually carries.

        A plain door says the four things a door has always said. Everything
        below is omitted at its default, so the common case stays exactly as
        wide as it was and the keys that do appear are the ones a caller has to
        act on: what else moves with it, what it waits for, what is still
        missing, what may operate it automatically, and what operating it costs.
        """
        assert self.battle_map is not None and self.map_state is not None
        summary: dict[str, Any] = {
            "square": list(feature.square),
            "kind": feature.kind,
            "level": self.battle_map.level_of(name),
            "open": name in self.map_state.open_features,
        }
        beyond = sorted(
            square for overlay in feature.affects for square in overlay.squares
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
        plane = self._plane(level)
        return {
            "index": level,
            "elevation": self._elevation_summary(level),
            "connectors": [
                {"square": [square[0], square[1]], "to_level": target}
                for square, target in sorted(plane.connectors.items())
            ],
        }

    def _elevation_summary(self, level: int) -> dict[str, Any]:
        """One plane's ground heights in feet, and what they do — and do not — do.

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
        assert self.battle_map is not None
        plane = self._plane(level)
        decided: dict[Square, int] = dict(plane.elevation)
        for (claimed_level, square), claim in self._feature_squares.items():
            if claimed_level == level and claim.elevation is not None:
                decided[square] = self._elevation_at(level, square)
        heights = list(decided.values())
        covered = len(decided) == (self.battle_map.width * self.battle_map.height)
        if not covered:
            heights.append(plane.default_elevation)
        return {
            "default": plane.default_elevation,
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
        if self.battle_map is not None:
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
            and option.kind is AttackKind.RANGED
            and option.normal_range > 0
            and distance > option.normal_range
        ):
            self._turn.attacks_left -= 1
            if self._turn.attacks_left == actor.attacks_per_action - 1:
                self._turn.action_used = True
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
        return compute_attack_advantage(
            attacker_conditions=actor.conditions,
            target_conditions=target.conditions,
            distance=distance,
            long_range_penalty=option.has_long_range_penalty(distance),
            extra_advantage=(
                int(self._pack_tactics_applies(actor, target))
                + int(not self._can_see(target, actor))
            ),
            extra_disadvantage=(
                int(self._dodge_benefits(target))
                + int(
                    option.kind is AttackKind.RANGED
                    and self._ranged_close_combat_penalty(actor)
                )
                + int(self._underwater_attack_penalty(actor, option))
                + int(not self._can_see(actor, target))
            ),
            condition_effects=self.condition_effects,
        )

    def _underwater_attack_penalty(
        self, actor: Creature, option: AttackOption
    ) -> bool:
        if not self._is_underwater(actor):
            return False
        if option.kind is AttackKind.RANGED:
            return True
        return actor.swim_speed <= 0 and option.damage_type is not DamageType.PIERCING

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
        if self.battle_map is None:
            return LightLevel.BRIGHT
        plane = self._plane(creature.level)
        square = to_square(as_point(creature.position))
        brightest = plane.ambient_light
        for light in plane.lights:
            distance = distance_feet(
                square_center(light.square), square_center(square), self.movement_rule
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
        """
        return compute_attack_advantage(
            attacker_conditions=actor.conditions,
            target_conditions=target.conditions,
            distance=actor.distance_to(target, self.movement_rule),
            extra_advantage=1 if self._pack_tactics_applies(actor, target) else 0,
            extra_disadvantage=(
                int(self._dodge_benefits(target))
                + int(
                    spell.attack_kind is AttackKind.RANGED
                    and self._ranged_close_combat_penalty(actor)
                )
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
        if self.battle_map is None:
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
        two can never disagree about who is inside an area. Sphere membership is
        measured in feet from the centre on an open plane and by template squares
        on a battle map; cones, lines, and cubes are square templates always,
        because their published shapes are grid figures.

        On a battle map, a creature with **total cover** from the effect's point
        of origin — a sphere's centre, a cube's minimum corner, the caster's own
        square for a cone or line — is not inside the area at all: the effect
        cannot reach behind a sealed wall, however the template falls. Range and
        the caster's own sight to the origin are the caller's checks — this
        answers only who the effect reaches.
        """
        caster = self.creatures[caster_name]
        caster_square = to_square(as_point(caster.position))
        origin_square: Square = caster_square
        match spell.effective_shape:
            case SpellShape.SPHERE:
                if center is None:
                    raise EncounterError(f"{spell.name} needs 'center'")
                centre = as_point(center)
                if self.battle_map is None:
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
            case _:
                raise EncounterError(f"{spell.name} is not an area spell")
        caught = [
            c for c in self.creatures.values()
            if c.arrived and not c.dead and to_square(as_point(c.position)) in squares
        ]
        if self.battle_map is None:
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
        are checked one at a time, exactly as a single-target spell is. A sphere or
        cube is checked at its **point of origin** only — those creatures come from
        the template rather than from the caller, so refusing the whole spell
        because one creature at the far edge of the blast sits past the range would
        be wrong — and on a battle map the origin must also be visible. A cone or
        line pours out of the caster, so there is nothing to range-check at all.

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
        ):
            if spell.effective_shape in (SpellShape.SPHERE, SpellShape.CUBE):
                if action.center is None:
                    what = (
                        "'center'" if spell.effective_shape is SpellShape.SPHERE
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
                f"'center' for a sphere or cube, 'direction' for a cone, "
                f"'toward' for a line"
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
        if self.battle_map is not None:
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
                ) <= MELEE_THRESHOLD
            ]
            actor.position = step
            for enemy in threatening:
                if distance_feet(
                    as_point(enemy.position), step, self.movement_rule
                ) <= MELEE_THRESHOLD:
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
        assert self.battle_map is not None and action.to_position is not None
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
            if to_level not in self.battle_map.levels:
                declared = ", ".join(str(i) for i in sorted(self.battle_map.levels))
                raise EncounterError(
                    f"there is no level {to_level} on this map. Levels: {declared}"
                )
            if self._plane(level).connectors.get(dest_sq) != to_level:
                raise EncounterError(
                    f"there is nothing at {dest_sq} that leads to level {to_level}; "
                    f"a move between storeys ends on a stairway"
                )
        if not self._on_map(dest_sq):
            raise EncounterError(
                f"{dest_sq} is off the {self.battle_map.width}x"
                f"{self.battle_map.height} map"
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
                    ) <= MELEE_THRESHOLD
                ]
            actor.position = square_center(step)
            for enemy in threatening:
                if distance_feet(
                    as_point(enemy.position), square_center(step), self.movement_rule
                ) <= MELEE_THRESHOLD:
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
        assert self.battle_map is not None
        if to_level not in self.battle_map.levels:
            declared = ", ".join(str(i) for i in sorted(self.battle_map.levels))
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
        if self.battle_map is None or self.map_state is None:
            raise EncounterError(
                "there is no battle map, so there is nothing to interact with"
            )
        if action.feature is None:
            raise EncounterError("interacting needs 'feature'")
        feature = self.battle_map.features.get(action.feature)
        if feature is None:
            available = ", ".join(sorted(self.battle_map.features)) or "none"
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
        level = self.battle_map.level_of(feature.name)
        if level != actor.level:
            raise EncounterError(
                f"{feature.name} is on level {level}; {actor.name} is on level "
                f"{actor.level} and cannot reach it from another storey"
            )
        actor_sq = to_square(as_point(actor.position))
        apart = max(
            abs(actor_sq[0] - feature.square[0]), abs(actor_sq[1] - feature.square[1])
        )
        if apart > 1:
            raise EncounterError(
                f"{feature.name} is at {feature.square}, out of reach from "
                f"{actor_sq}; stand on or next to it"
            )

        was_open = feature.name in self.map_state.open_features
        wants_open = (not was_open) if action.set_open is None else action.set_open
        linked = [feature.linked_to] if feature.linked_to is not None else []
        operated = [feature.name, *linked]
        for operated_name in operated:
            operated_feature = self.battle_map.features[operated_name]
            trigger = operated_feature.trigger
            if (
                trigger is not None
                and trigger.mode is TriggerMode.MAINTAINED
                and trigger.active(self.map_state.open_features)
                and wants_open is not trigger.set_open
            ):
                raise EncounterError(
                    f"{feature.name} is held "
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
                    f"{feature.name} will not move until {', '.join(unmet)} "
                    f"{'is' if len(unmet) == 1 else 'are'} open"
                )
        if action.set_open is not None and action.set_open == was_open:
            raise EncounterError(
                f"{feature.name} is already {'open' if was_open else 'closed'}"
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
                f"{feature.name} asks for no check, so there is no face to report"
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
                               f"fails to {verb} {feature.name} ({test.describe()})"
                           ),
                           feature=feature.name, open=was_open, **extras)
                return
            note = f" ({test.describe()})"
        else:
            note = ""

        if wants_open:
            self.map_state.open_features.update(operated)
        else:
            self.map_state.open_features.difference_update(operated)
        subjects = feature.name if not linked else f"{feature.name} and {linked[0]}"
        self._emit("interact", actor.name,
                   detail=f"{'opens' if wants_open else 'closes'} {subjects}{note}",
                   feature=feature.name, open=wants_open, **extras)
        self._drain_triggers(feature.name, operated)

    def _drain_triggers(self, initiating_feature: str, changed: Sequence[str]) -> None:
        """Apply automatic fixture transitions in deterministic dependency order."""
        assert self.battle_map is not None and self.map_state is not None
        revision = 1
        changed_at = {name: revision for name in changed}
        changed_by = {name: initiating_feature for name in changed}

        for name in self._trigger_sequence:
            feature = self.battle_map.features[name]
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
            # An opportunity attack is an attack roll, so Pack Tactics reads
            # here too — against the mover wherever the walk has taken it.
            extra_advantage=1 if self._pack_tactics_applies(attacker, mover) else 0,
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
                expires_phase=expires_phase,
                expires_anchor=expires_anchor,
            )
        )

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
    battle_map: BattleMap | None = None,
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
        battle_map=battle_map,
        terrain_effects=terrain_effects,
    )
    return encounter, rng
